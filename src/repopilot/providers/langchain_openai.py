from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import suppress
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

import httpx
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, convert_to_messages
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, SecretStr, ValidationError

from ..errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RepoPilotError,
)
from ..models import ModelResponse, TokenUsage, ToolCall
from .base import ModelRequest, ProviderHealth
from .telemetry import ProviderEvent, emit_provider_event, get_provider_call_context


@dataclass
class _Progress:
    first_chunk_seen: bool = False
    chunk_count: int = 0


class LangChainOpenAIProvider:
    """LangChain-backed adapter for the standard OpenAI chat/tool protocol.

    LangChain owns message conversion, tool binding, structured output and
    streaming chunk assembly. RepoPilot keeps only its provider-neutral result,
    safe telemetry, conservative budget accounting and error taxonomy.
    """

    name = "langchain_openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        write_timeout_seconds: float,
        pool_timeout_seconds: float,
        streaming_enabled: bool = True,
        stream_include_usage: bool = True,
        stream_progress_interval_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.model = model
        self.streaming_enabled = streaming_enabled
        self.stream_progress_interval_seconds = stream_progress_interval_seconds
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=write_timeout_seconds,
            pool=pool_timeout_seconds,
        )
        self._http_client = httpx.AsyncClient(transport=transport, timeout=self._timeout)
        self._suppress_transport_endpoint_logs()
        self._chat = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=self.base_url,
            timeout=self._timeout,
            max_retries=0,
            stream_usage=stream_include_usage,
            streaming=streaming_enabled,
            http_async_client=self._http_client,
            http_socket_options=(),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        context = get_provider_call_context()
        call_id = context.call_id if context is not None else uuid4().hex
        attempt = context.attempt if context is not None else 1
        max_attempts = context.max_attempts if context is not None else 1
        started_at = monotonic()
        progress = _Progress()
        stopped = asyncio.Event()
        ticker: asyncio.Task[None] | None = None
        try:
            await self._emit(
                "started",
                request=request,
                call_id=call_id,
                started_at=started_at,
                metadata={
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "streaming": self.streaming_enabled and request.response_schema is None,
                },
            )
            ticker = asyncio.create_task(
                self._emit_progress_ticks(
                    request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                    stopped=stopped,
                )
            )
            messages = convert_to_messages(list(request.messages))
            runnable = self._runnable(request)
            if request.response_schema is not None:
                response = await self._complete_structured(
                    runnable,
                    messages,
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
            elif self.streaming_enabled:
                response = await self._complete_streaming(
                    runnable,
                    messages,
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
            else:
                message = await runnable.ainvoke(messages)
                if not isinstance(message, AIMessage):
                    raise ProviderResponseError()
                await self._emit_first_chunk(
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
                response = self._model_response(message, request)
        except asyncio.CancelledError:
            await self._emit(
                "cancelled",
                request=request,
                call_id=call_id,
                started_at=started_at,
                metadata={
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "fallback_used": False,
                },
            )
            raise
        except Exception as exc:
            error = self._provider_error(exc)
            phase: Literal["timeout", "failed"] = (
                "timeout" if isinstance(error, ProviderTimeoutError) else "failed"
            )
            await self._emit(
                phase,
                request=request,
                call_id=call_id,
                started_at=started_at,
                metadata={
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "error_code": error.code,
                    "fallback_used": False,
                },
            )
            raise error from None
        finally:
            stopped.set()
            if ticker is not None:
                ticker.cancel()
                with suppress(asyncio.CancelledError):
                    await ticker

        usage = response.usage
        usage_estimated = response.usage_estimated
        await self._emit(
            "completed",
            request=request,
            call_id=call_id,
            started_at=started_at,
            model=response.model or self.model,
            metadata={
                "attempt": attempt,
                "max_attempts": max_attempts,
                "finish_reason": response.finish_reason,
                "tool_call_count": len(response.tool_calls),
                "usage_reported": not usage_estimated,
                "usage_estimated": usage_estimated,
                "prompt_tokens": usage.prompt_tokens if usage is not None else None,
                "completion_tokens": usage.completion_tokens if usage is not None else None,
                "total_tokens": usage.total_tokens if usage is not None else None,
                "fallback_used": False,
            },
        )
        return response

    def _runnable(self, request: ModelRequest) -> Runnable[Any, Any]:
        invocation = {
            "temperature": request.temperature,
            "max_completion_tokens": request.max_tokens,
        }
        if request.response_schema is not None:
            return self._chat.with_structured_output(
                request.response_schema,
                method="function_calling",
                include_raw=True,
            ).bind(**invocation)
        if request.tools:
            return self._chat.bind_tools(list(request.tools), tool_choice="auto").bind(**invocation)
        return self._chat.bind(**invocation)

    async def _complete_structured(
        self,
        runnable: Runnable[Any, Any],
        messages: list[BaseMessage],
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _Progress,
    ) -> ModelResponse:
        result = await runnable.ainvoke(messages)
        await self._emit_first_chunk(
            request=request,
            call_id=call_id,
            attempt=attempt,
            max_attempts=max_attempts,
            started_at=started_at,
            progress=progress,
        )
        if not isinstance(result, dict):
            raise ProviderResponseError()
        raw = result.get("raw")
        parsed = result.get("parsed")
        parsing_error = result.get("parsing_error")
        if parsing_error is not None or not isinstance(raw, AIMessage):
            raise ProviderResponseError()
        if isinstance(parsed, BaseModel):
            text = parsed.model_dump_json()
        elif isinstance(parsed, dict):
            text = json.dumps(parsed, ensure_ascii=False)
        else:
            raise ProviderResponseError()
        return replace(self._model_response(raw, request, text_override=text), tool_calls=())

    async def _complete_streaming(
        self,
        runnable: Runnable[Any, Any],
        messages: list[BaseMessage],
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _Progress,
    ) -> ModelResponse:
        aggregate: AIMessageChunk | None = None
        async for chunk in runnable.astream(messages):
            if not isinstance(chunk, AIMessageChunk):
                raise ProviderResponseError()
            progress.chunk_count += 1
            await self._emit_first_chunk(
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                progress=progress,
            )
            aggregate = chunk if aggregate is None else aggregate + chunk
        if aggregate is None:
            raise ProviderResponseError()
        return self._model_response(aggregate, request)

    def _model_response(
        self,
        message: AIMessage | AIMessageChunk,
        request: ModelRequest,
        *,
        text_override: str | None = None,
    ) -> ModelResponse:
        invalid_calls = getattr(message, "invalid_tool_calls", None) or []
        if invalid_calls:
            raise ProviderResponseError()
        tool_calls = tuple(
            ToolCall(
                name=str(item["name"]),
                arguments=dict(item.get("args") or {}),
                call_id=str(item.get("id") or uuid4().hex),
            )
            for item in (getattr(message, "tool_calls", None) or [])
        )
        text = text_override if text_override is not None else self._message_text(message)
        if not text and not tool_calls:
            raise ProviderResponseError()
        metadata = dict(getattr(message, "response_metadata", None) or {})
        usage = self._usage(message)
        usage_estimated = usage is None
        if usage_estimated:
            usage = self._estimate_usage(request, text, tool_calls)
        return ModelResponse(
            text=text or None,
            tool_calls=tool_calls,
            finish_reason=self._optional_text(metadata.get("finish_reason")),
            model=(
                self._optional_text(metadata.get("model_name"))
                or self._optional_text(metadata.get("model"))
                or self.model
            ),
            usage=usage,
            usage_estimated=usage_estimated,
            response_id=self._optional_text(getattr(message, "id", None)),
        )

    @staticmethod
    def _message_text(message: AIMessage | AIMessageChunk) -> str:
        text = getattr(message, "text", "")
        return text if isinstance(text, str) else str(text)

    @staticmethod
    def _usage(message: AIMessage | AIMessageChunk) -> TokenUsage | None:
        metadata = getattr(message, "usage_metadata", None)
        if not isinstance(metadata, dict):
            return None
        prompt = LangChainOpenAIProvider._safe_int(metadata.get("input_tokens"))
        completion = LangChainOpenAIProvider._safe_int(metadata.get("output_tokens"))
        total = LangChainOpenAIProvider._safe_int(metadata.get("total_tokens"))
        if prompt is None and completion is None and total is None:
            return None
        prompt = prompt or 0
        completion = completion or 0
        total = max(total or 0, prompt + completion)
        return TokenUsage(prompt, completion, total)

    def _estimate_usage(
        self,
        request: ModelRequest,
        text: str,
        tool_calls: tuple[ToolCall, ...],
    ) -> TokenUsage:
        prompt_tools = list(request.tools)
        if request.response_schema is not None:
            # with_structured_output(method="function_calling") sends the schema as a
            # forced function tool. Include that protocol payload in the fallback estimate;
            # otherwise providers that omit usage metadata would systematically undercount
            # planner/reviewer calls against the global token budget.
            prompt_tools.append(convert_to_openai_tool(request.response_schema))
        prompt_payload = json.dumps(
            {"messages": request.messages, "tools": prompt_tools},
            ensure_ascii=False,
            default=str,
        )
        completion_payload = text + json.dumps(
            [{"name": call.name, "arguments": call.arguments} for call in tool_calls],
            ensure_ascii=False,
            default=str,
        )
        prompt = self._estimate_text_tokens(prompt_payload)
        completion = self._estimate_text_tokens(completion_payload)
        return TokenUsage(prompt, completion, prompt + completion)

    @staticmethod
    def _estimate_text_tokens(value: str) -> int:
        if not value:
            return 0
        return max(1, math.ceil(max(len(value) / 2, len(value.encode("utf-8")) / 3)))

    @staticmethod
    def _safe_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        return None

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    async def _emit_first_chunk(
        self,
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _Progress,
    ) -> None:
        if progress.first_chunk_seen:
            return
        progress.first_chunk_seen = True
        await self._emit(
            "first_byte",
            request=request,
            call_id=call_id,
            started_at=started_at,
            metadata={
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delta_count": progress.chunk_count,
            },
        )

    async def _emit_progress_ticks(
        self,
        request: ModelRequest,
        *,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _Progress,
        stopped: asyncio.Event,
    ) -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(
                    stopped.wait(), timeout=self.stream_progress_interval_seconds
                )
            except TimeoutError:
                await self._emit(
                    "progress",
                    request=request,
                    call_id=call_id,
                    started_at=started_at,
                    metadata={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "state": (
                            "receiving" if progress.first_chunk_seen else "waiting_first_byte"
                        ),
                        "delta_count": progress.chunk_count,
                    },
                )

    async def _emit(
        self,
        phase: Literal[
            "started", "first_byte", "progress", "completed", "timeout", "failed", "cancelled"
        ],
        *,
        request: ModelRequest,
        call_id: str,
        started_at: float,
        metadata: dict[str, str | int | float | bool | None],
        model: str | None = None,
    ) -> None:
        await emit_provider_event(
            ProviderEvent(
                phase=phase,
                call_id=call_id,
                provider=self.name,
                model=model or self.model,
                purpose=request.purpose,
                elapsed_ms=(monotonic() - started_at) * 1000,
                metadata=metadata,
            )
        )

    @staticmethod
    def _provider_error(exc: Exception) -> RepoPilotError:
        if isinstance(exc, RepoPilotError):
            return exc
        if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
            return ProviderAuthenticationError()
        if isinstance(exc, RateLimitError):
            return ProviderRateLimitError()
        if isinstance(exc, (APITimeoutError, httpx.TimeoutException, TimeoutError)):
            return ProviderTimeoutError()
        if isinstance(exc, (APIConnectionError, httpx.TransportError)):
            return ProviderUnavailableError()
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in {401, 403}:
                return ProviderAuthenticationError()
            if status == 429:
                return ProviderRateLimitError()
            if status in {408, 504}:
                return ProviderTimeoutError()
            if status >= 500:
                return ProviderUnavailableError()
            return ProviderResponseError()
        if isinstance(exc, APIStatusError):
            if exc.status_code in {401, 403}:
                return ProviderAuthenticationError()
            if exc.status_code == 429:
                return ProviderRateLimitError()
            if exc.status_code in {408, 504}:
                return ProviderTimeoutError()
            if exc.status_code >= 500:
                return ProviderUnavailableError()
            return ProviderResponseError()
        if isinstance(
            exc,
            (BadRequestError, OutputParserException, ValidationError, TypeError, ValueError),
        ):
            return ProviderResponseError()
        return ProviderResponseError()

    @staticmethod
    def _suppress_transport_endpoint_logs() -> None:
        """Keep internal provider URLs out of ordinary application logs."""

        for logger_name in ("httpx", "httpcore", "openai"):
            transport_logger = logging.getLogger(logger_name)
            if transport_logger.level < logging.WARNING:
                transport_logger.setLevel(logging.WARNING)

    async def health(self) -> ProviderHealth:
        try:
            response = await self._http_client.get(
                f"{self.base_url}models",
                headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}"},
            )
            response.raise_for_status()
        except Exception as exc:
            error = self._provider_error(exc)
            return ProviderHealth(False, self.name, self.model, error.code)
        return ProviderHealth(True, self.name, self.model)

    async def close(self) -> None:
        await self._http_client.aclose()
