from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import SecretStr

from ..errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..models import ModelResponse, TokenUsage, ToolCall
from .base import ModelRequest, ProviderHealth
from .telemetry import ProviderEvent, emit_provider_event, get_provider_call_context


@dataclass
class _ToolCallBuffer:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _StreamProgress:
    first_byte_seen: bool = False
    delta_count: int = 0
    bytes_received: int = 0


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float | None = None,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
        write_timeout_seconds: float | None = None,
        pool_timeout_seconds: float | None = None,
        streaming_enabled: bool = True,
        stream_include_usage: bool = True,
        stream_progress_interval_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._suppress_transport_endpoint_logs()
        self.base_url = f"{base_url.rstrip('/')}/"
        self.model = model
        self.streaming_enabled = streaming_enabled
        self.stream_include_usage = stream_include_usage
        self.stream_progress_interval_seconds = stream_progress_interval_seconds
        aggregate_timeout = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds or aggregate_timeout or 10.0,
                read=read_timeout_seconds or aggregate_timeout or 120.0,
                write=write_timeout_seconds or aggregate_timeout or 30.0,
                pool=pool_timeout_seconds or aggregate_timeout or 10.0,
            ),
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "User-Agent": "RepoPilot/1.3",
            },
            transport=transport,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": self.streaming_enabled,
        }
        if self.streaming_enabled and self.stream_include_usage:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in request.tools]
            payload["tool_choice"] = "auto"

        context = get_provider_call_context()
        call_id = context.call_id if context is not None else uuid4().hex
        attempt = context.attempt if context is not None else 1
        max_attempts = context.max_attempts if context is not None else 1
        started_at = monotonic()
        progress = _StreamProgress()
        ticker: asyncio.Task[None] | None = None

        try:
            await self._emit(
                "started",
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                metadata={"streaming": self.streaming_enabled},
            )
            ticker = asyncio.create_task(
                self._emit_progress_ticks(
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
            )
            if self.streaming_enabled:
                result = await self._complete_streaming(
                    payload,
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
            else:
                result = await self._complete_non_streaming(
                    payload,
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
        except asyncio.CancelledError:
            await self._emit(
                "cancelled",
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                metadata={"streaming": self.streaming_enabled},
            )
            raise
        except httpx.TimeoutException as exc:
            timeout_kind = self._timeout_kind(exc)
            timeout_error = ProviderTimeoutError(details={"timeout_kind": timeout_kind})
            await self._emit(
                "timeout",
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                metadata={
                    "streaming": self.streaming_enabled,
                    "timeout_kind": timeout_kind,
                    "error_code": timeout_error.code,
                },
            )
            raise timeout_error from None
        except httpx.TransportError:
            unavailable_error = ProviderUnavailableError()
            await self._emit_failure(
                unavailable_error,
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
            )
            raise unavailable_error from None
        except ProviderError as exc:
            phase: Literal["timeout", "failed"] = (
                "timeout" if isinstance(exc, ProviderTimeoutError) else "failed"
            )
            metadata: dict[str, str | int | float | bool | None] = {
                "streaming": self.streaming_enabled,
                "error_code": exc.code,
            }
            provider_timeout_kind = exc.details.get("timeout_kind")
            if isinstance(provider_timeout_kind, str):
                metadata["timeout_kind"] = provider_timeout_kind
            await self._emit(
                phase,
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                metadata=metadata,
            )
            raise
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            response_error = ProviderResponseError()
            await self._emit_failure(
                response_error,
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
            )
            raise response_error from None
        else:
            usage_reported = result.usage is not None
            if result.usage is None:
                result = replace(result, usage=self._estimate_usage(payload, result))
            usage = result.usage
            assert usage is not None
            await self._emit(
                "completed",
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                metadata={
                    "streaming": self.streaming_enabled,
                    "response_model": result.model,
                    "finish_reason": result.finish_reason,
                    "tool_call_count": len(result.tool_calls),
                    "usage_reported": usage_reported,
                    "usage_estimated": not usage_reported,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "fallback_used": False,
                },
            )
            return result
        finally:
            if ticker is not None:
                ticker.cancel()
                with suppress(asyncio.CancelledError):
                    await ticker

    async def health(self) -> ProviderHealth:
        try:
            response = await self._client.get("models")
            self._raise_for_status(response)
        except httpx.TimeoutException as exc:
            detail = ProviderTimeoutError(details={"timeout_kind": self._timeout_kind(exc)}).code
            return ProviderHealth(False, self.name, self.model, detail)
        except httpx.TransportError:
            return ProviderHealth(False, self.name, self.model, ProviderUnavailableError.code)
        except (ProviderUnavailableError, ProviderTimeoutError) as exc:
            return ProviderHealth(False, self.name, self.model, exc.code)
        except (ProviderAuthenticationError, ProviderRateLimitError, ProviderResponseError) as exc:
            return ProviderHealth(False, self.name, self.model, exc.code)
        return ProviderHealth(True, self.name, self.model)

    async def close(self) -> None:
        await self._client.aclose()

    async def _complete_streaming(
        self,
        payload: dict[str, Any],
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _StreamProgress,
    ) -> ModelResponse:
        async with self._client.stream("POST", "chat/completions", json=payload) as response:
            self._raise_for_status(response)
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                raw = await response.aread()
                progress.bytes_received += len(raw)
                await self._emit_first_byte(
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
                return self._parse_response_body(json.loads(raw))
            return await self._parse_sse_response(
                response,
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                progress=progress,
            )

    async def _complete_non_streaming(
        self,
        payload: dict[str, Any],
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _StreamProgress,
    ) -> ModelResponse:
        response = await self._client.post("chat/completions", json=payload)
        self._raise_for_status(response)
        progress.bytes_received = len(response.content)
        await self._emit_first_byte(
            request=request,
            call_id=call_id,
            attempt=attempt,
            max_attempts=max_attempts,
            started_at=started_at,
            progress=progress,
        )
        return self._parse_response_body(response.json())

    async def _parse_sse_response(
        self,
        response: httpx.Response,
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _StreamProgress,
    ) -> ModelResponse:
        text_parts: list[str] = []
        tool_buffers: dict[int, _ToolCallBuffer] = {}
        response_id: str | None = None
        response_model: str | None = None
        finish_reason: str | None = None
        usage: TokenUsage | None = None
        data_lines: list[str] = []
        done_received = False

        async for line in response.aiter_lines():
            progress.bytes_received += len(line.encode("utf-8")) + 1
            if line and not progress.first_byte_seen:
                await self._emit_first_byte(
                    request=request,
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=started_at,
                    progress=progress,
                )
            if not line:
                if not data_lines:
                    continue
                event_data = "\n".join(data_lines)
                data_lines.clear()
                if event_data == "[DONE]":
                    done_received = True
                    break
                chunk = json.loads(event_data)
                progress.delta_count += 1
                (
                    response_id,
                    response_model,
                    finish_reason,
                    usage,
                ) = self._consume_stream_chunk(
                    chunk,
                    text_parts=text_parts,
                    tool_buffers=tool_buffers,
                    response_id=response_id,
                    response_model=response_model,
                    finish_reason=finish_reason,
                    usage=usage,
                )
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))

        if data_lines:
            event_data = "\n".join(data_lines)
            if event_data == "[DONE]":
                done_received = True
            else:
                chunk = json.loads(event_data)
                progress.delta_count += 1
                response_id, response_model, finish_reason, usage = self._consume_stream_chunk(
                    chunk,
                    text_parts=text_parts,
                    tool_buffers=tool_buffers,
                    response_id=response_id,
                    response_model=response_model,
                    finish_reason=finish_reason,
                    usage=usage,
                )

        if not done_received and not finish_reason:
            raise ProviderResponseError(details={"reason": "incomplete_stream"})

        tool_calls = tuple(
            self._build_tool_call(buffer) for _, buffer in sorted(tool_buffers.items())
        )
        text = "".join(text_parts) if text_parts else None
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=response_model or self.model,
            usage=usage,
            response_id=response_id,
        )

    def _consume_stream_chunk(
        self,
        chunk: object,
        *,
        text_parts: list[str],
        tool_buffers: dict[int, _ToolCallBuffer],
        response_id: str | None,
        response_model: str | None,
        finish_reason: str | None,
        usage: TokenUsage | None,
    ) -> tuple[str | None, str | None, str | None, TokenUsage | None]:
        if not isinstance(chunk, dict):
            raise ValueError("stream chunk must be an object")
        chunk_id = chunk.get("id")
        chunk_model = chunk.get("model")
        if isinstance(chunk_id, str) and chunk_id:
            response_id = chunk_id
        if isinstance(chunk_model, str) and chunk_model:
            response_model = chunk_model

        usage_data = chunk.get("usage")
        if isinstance(usage_data, dict):
            usage = self._parse_usage(usage_data)

        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise ValueError("stream choices must be an array")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("stream choice must be an object")
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                finish_reason = reason
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                raise ValueError("stream delta must be an object")
            content = delta.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif content is not None:
                raise ValueError("stream content must be text")
            fragments = delta.get("tool_calls") or []
            if not isinstance(fragments, list):
                raise ValueError("stream tool calls must be an array")
            for position, fragment in enumerate(fragments):
                if not isinstance(fragment, dict):
                    raise ValueError("stream tool call must be an object")
                index = fragment.get("index", position)
                if not isinstance(index, int):
                    raise ValueError("stream tool call index must be an integer")
                buffer = tool_buffers.setdefault(index, _ToolCallBuffer())
                fragment_id = fragment.get("id")
                if isinstance(fragment_id, str):
                    if not buffer.call_id:
                        buffer.call_id = fragment_id
                    elif buffer.call_id != fragment_id:
                        buffer.call_id += fragment_id
                function = fragment.get("function") or {}
                if not isinstance(function, dict):
                    raise ValueError("stream tool function must be an object")
                name = function.get("name")
                arguments = function.get("arguments")
                if isinstance(name, str):
                    buffer.name += name
                if isinstance(arguments, str):
                    buffer.arguments += arguments
        return response_id, response_model, finish_reason, usage

    def _parse_response_body(self, body: object) -> ModelResponse:
        if not isinstance(body, dict):
            raise ValueError("provider response must be an object")
        choices = body["choices"]
        if not isinstance(choices, list):
            raise ValueError("provider choices must be an array")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("provider choice must be an object")
        message = choice["message"]
        if not isinstance(message, dict):
            raise ValueError("provider message must be an object")
        raw_tool_calls = message.get("tool_calls", [])
        if not isinstance(raw_tool_calls, list):
            raise ValueError("provider tool calls must be an array")
        tool_calls = tuple(self._parse_tool_call(item) for item in raw_tool_calls)
        usage_data = body.get("usage")
        usage = self._parse_usage(usage_data) if isinstance(usage_data, dict) else None
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("provider content must be text")
        return ModelResponse(
            text=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            model=body.get("model", self.model),
            usage=usage,
            response_id=body.get("id"),
        )

    async def _emit_progress_ticks(
        self,
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _StreamProgress,
    ) -> None:
        while True:
            await asyncio.sleep(self.stream_progress_interval_seconds)
            await self._emit(
                "progress",
                request=request,
                call_id=call_id,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                metadata={
                    "streaming": self.streaming_enabled,
                    "state": "receiving" if progress.first_byte_seen else "waiting_first_byte",
                    "delta_count": progress.delta_count,
                    "bytes_received": progress.bytes_received,
                },
            )

    async def _emit_first_byte(
        self,
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        progress: _StreamProgress,
    ) -> None:
        if progress.first_byte_seen:
            return
        progress.first_byte_seen = True
        await self._emit(
            "first_byte",
            request=request,
            call_id=call_id,
            attempt=attempt,
            max_attempts=max_attempts,
            started_at=started_at,
            metadata={"streaming": self.streaming_enabled},
        )

    async def _emit_failure(
        self,
        error: ProviderError,
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
    ) -> None:
        await self._emit(
            "failed",
            request=request,
            call_id=call_id,
            attempt=attempt,
            max_attempts=max_attempts,
            started_at=started_at,
            metadata={"streaming": self.streaming_enabled, "error_code": error.code},
        )

    async def _emit(
        self,
        phase: Literal[
            "started",
            "first_byte",
            "progress",
            "completed",
            "timeout",
            "failed",
            "cancelled",
        ],
        *,
        request: ModelRequest,
        call_id: str,
        attempt: int,
        max_attempts: int,
        started_at: float,
        metadata: dict[str, str | int | float | bool | None],
    ) -> None:
        await emit_provider_event(
            ProviderEvent(
                phase=phase,
                call_id=call_id,
                provider=self.name,
                model=self.model,
                purpose=request.purpose,
                elapsed_ms=(monotonic() - started_at) * 1000,
                metadata={"attempt": attempt, "max_attempts": max_attempts, **metadata},
            )
        )

    @staticmethod
    def _parse_usage(usage_data: dict[str, Any]) -> TokenUsage | None:
        prompt = OpenAICompatibleProvider._safe_token_count(usage_data.get("prompt_tokens"))
        completion = OpenAICompatibleProvider._safe_token_count(usage_data.get("completion_tokens"))
        total = OpenAICompatibleProvider._safe_token_count(usage_data.get("total_tokens"))
        if prompt is None and completion is None and total is None:
            return None

        prompt_tokens = prompt or 0
        completion_tokens = completion or 0
        component_total = prompt_tokens + completion_tokens
        total_tokens = max(total or 0, component_total)
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _estimate_usage(payload: dict[str, Any], response: ModelResponse) -> TokenUsage:
        """Conservatively account for compatible providers that omit streaming usage."""

        prompt_payload = {
            "messages": payload.get("messages", []),
            "tools": payload.get("tools", []),
        }
        prompt_text = json.dumps(prompt_payload, ensure_ascii=False, default=str)
        completion_text = response.text or ""
        if response.tool_calls:
            completion_text += json.dumps(
                [{"name": call.name, "arguments": call.arguments} for call in response.tool_calls],
                ensure_ascii=False,
                default=str,
            )
        prompt_tokens = OpenAICompatibleProvider._estimate_text_tokens(prompt_text)
        completion_tokens = OpenAICompatibleProvider._estimate_text_tokens(completion_text)
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

    @staticmethod
    def _safe_token_count(value: object) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float):
            return int(value) if value >= 0 and value.is_integer() else None
        if isinstance(value, str):
            normalized = value.strip()
            if normalized.isascii() and normalized.isdigit():
                return int(normalized)
        return None

    @staticmethod
    def _estimate_text_tokens(value: str) -> int:
        if not value:
            return 0
        ascii_chars = sum(character.isascii() for character in value)
        non_ascii_tokens = sum(
            max(1, (len(character.encode("utf-8")) + 2) // 3)
            for character in value
            if not character.isascii()
        )
        # JSON, source code and tool arguments tokenize more densely than prose.
        ascii_tokens = (ascii_chars + 1) // 2
        return max(1, ascii_tokens + non_ascii_tokens)

    @staticmethod
    def _parse_tool_call(item: object) -> ToolCall:
        if not isinstance(item, dict):
            raise ValueError("tool call must be an object")
        function = item["function"]
        if not isinstance(function, dict):
            raise ValueError("tool function must be an object")
        arguments = json.loads(function.get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return ToolCall(
            name=str(function["name"]),
            arguments=arguments,
            call_id=str(item["id"]),
        )

    @staticmethod
    def _build_tool_call(buffer: _ToolCallBuffer) -> ToolCall:
        if not buffer.call_id or not buffer.name:
            raise ValueError("stream tool call is incomplete")
        arguments = json.loads(buffer.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return ToolCall(name=buffer.name, arguments=arguments, call_id=buffer.call_id)

    @staticmethod
    def _suppress_transport_endpoint_logs() -> None:
        # httpx logs full request URLs at INFO and httpcore logs connection targets
        # at DEBUG. Provider lifecycle telemetry already supplies safe diagnostics.
        for logger_name in ("httpx", "httpcore"):
            transport_logger = logging.getLogger(logger_name)
            if transport_logger.level < logging.WARNING:
                transport_logger.setLevel(logging.WARNING)

    @staticmethod
    def _timeout_kind(
        exc: httpx.TimeoutException,
    ) -> Literal["connect", "read", "write", "pool", "unknown"]:
        if isinstance(exc, httpx.ConnectTimeout):
            return "connect"
        if isinstance(exc, httpx.ReadTimeout):
            return "read"
        if isinstance(exc, httpx.WriteTimeout):
            return "write"
        if isinstance(exc, httpx.PoolTimeout):
            return "pool"
        return "unknown"

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code in {401, 403}:
            raise ProviderAuthenticationError()
        if response.status_code == 429:
            raise ProviderRateLimitError()
        if response.status_code in {408, 504}:
            raise ProviderTimeoutError(details={"timeout_kind": "read"})
        if response.status_code >= 500:
            raise ProviderUnavailableError()
        raise ProviderResponseError(details={"status_code": response.status_code})
