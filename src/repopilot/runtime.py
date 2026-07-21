from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from .config import Settings
from .errors import RepoPilotError, ToolTimeoutError
from .models import AgentRunResult, ModelResponse, ToolCall, ToolObservation, TraceEvent
from .observability import log_exception_safely
from .providers.base import ModelProvider, ModelRequest
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

StepCallback = Callable[[int, list[dict[str, Any]], list[TraceEvent]], Awaitable[None]]


class AsyncAgentRuntime:
    """Provider-neutral asynchronous agent loop with safe tool orchestration."""

    def __init__(self, provider: ModelProvider, tools: ToolRegistry, settings: Settings) -> None:
        self.provider = provider
        self.tools = tools
        self.settings = settings

    async def run(
        self,
        user_input: str,
        *,
        initial_messages: list[dict[str, Any]] | None = None,
        on_step: StepCallback | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult:
        """Run the agent loop.

        ``initial_messages`` restores a conversation from a durable checkpoint;
        ``on_step`` receives the message snapshot and the trace events produced
        during each completed step so callers can persist checkpoints and events;
        ``task_id`` is an optional correlation id used only for logging.
        """
        del task_id
        if initial_messages:
            messages: list[dict[str, Any]] = [dict(message) for message in initial_messages]
        else:
            messages = [{"role": "user", "content": user_input}]
        trace: list[TraceEvent] = []
        emitted_trace = 0
        seen_calls: set[str] = set()
        total_tokens = 0
        tool_call_count = 0
        degraded = False

        async def notify(step: int) -> None:
            nonlocal emitted_trace
            if on_step is None:
                return
            fresh = trace[emitted_trace:]
            emitted_trace = len(trace)
            await on_step(step, [dict(message) for message in messages], fresh)

        try:
            for step in range(1, self.settings.max_steps + 1):
                response = await self.provider.complete(
                    ModelRequest(
                        messages=tuple(messages),
                        tools=tuple(self.tools.descriptions()),
                    )
                )
                total_tokens += response.usage.total_tokens if response.usage else 0
                degraded = degraded or response.fallback_used
                trace.append(self._model_trace(step, response))

                if total_tokens > self.settings.max_total_tokens:
                    return self._guard_result(
                        "Stopped after reaching the token budget.",
                        messages,
                        trace,
                        step,
                        total_tokens,
                        degraded,
                    )

                if not response.tool_calls:
                    assert response.text is not None
                    messages.append({"role": "assistant", "content": response.text})
                    trace.append(TraceEvent(step, "finish", "Final answer returned"))
                    return AgentRunResult(
                        response.text,
                        tuple(messages),
                        tuple(trace),
                        step,
                        "completed",
                        total_tokens,
                        degraded,
                    )

                duplicate = self._find_duplicate(response.tool_calls, seen_calls)
                if duplicate is not None:
                    return self._guard_result(
                        f"Repeated tool call blocked: {duplicate}",
                        messages,
                        trace,
                        step,
                        total_tokens,
                        degraded,
                    )

                tool_call_count += len(response.tool_calls)
                if tool_call_count > self.settings.max_tool_calls:
                    return self._guard_result(
                        "Stopped after reaching the tool-call budget.",
                        messages,
                        trace,
                        step,
                        total_tokens,
                        degraded,
                    )

                messages.append(self._assistant_tool_message(response))
                observations = await self._execute_tool_calls(response.tool_calls, trace, step)
                degraded = degraded or any(not observation.ok for observation in observations)
                messages.extend(self._tool_message(observation) for observation in observations)
                await notify(step)

            return self._guard_result(
                f"Stopped after reaching max_steps={self.settings.max_steps}",
                messages,
                trace,
                self.settings.max_steps,
                total_tokens,
                degraded,
            )
        except asyncio.CancelledError:
            trace.append(TraceEvent(len(trace) + 1, "guard", "Run cancelled"))
            raise
        except RepoPilotError as exc:
            log_exception_safely(
                logger,
                "Agent run failed",
                exc,
                extra={"error_code": exc.code},
            )
            trace.append(TraceEvent(len(trace) + 1, "error", exc.safe_message, {"code": exc.code}))
            return AgentRunResult(
                exc.safe_message,
                tuple(messages),
                tuple(trace),
                max(1, len([event for event in trace if event.event == "model"])),
                "failed",
                total_tokens,
                True,
            )

    async def _execute_tool_calls(
        self,
        calls: tuple[ToolCall, ...],
        trace: list[TraceEvent],
        step: int,
    ) -> list[ToolObservation]:
        names = [call.name for call in calls]
        if self.tools.all_read_only(names):
            return list(
                await asyncio.gather(
                    *(self._execute_tool_call(call, trace, step) for call in calls)
                )
            )

        observations: list[ToolObservation] = []
        for call in calls:
            observations.append(await self._execute_tool_call(call, trace, step))
        return observations

    async def _execute_tool_call(
        self,
        call: ToolCall,
        trace: list[TraceEvent],
        step: int,
    ) -> ToolObservation:
        started_at = perf_counter()
        attempts = 0
        last_error: RepoPilotError | None = None
        try:
            tool = self.tools.get(call.name)
        except RepoPilotError as exc:
            return self._failed_observation(call, exc, started_at, 1, trace, step)

        can_retry = tool.retryable and (tool.read_only or tool.idempotent)
        max_attempts = self.settings.tool_max_attempts if can_retry else 1
        for attempts in range(1, max_attempts + 1):
            try:
                async with asyncio.timeout(self.settings.tool_timeout_seconds):
                    value = await self.tools.aexecute(call.name, call.arguments)
            except TimeoutError as exc:
                error = ToolTimeoutError(details={"tool": call.name})
                error.__cause__ = exc
                last_error = error
            except RepoPilotError as exc:
                last_error = exc
            else:
                observation = ToolObservation(
                    call.call_id,
                    call.name,
                    True,
                    json.dumps(value, ensure_ascii=False, default=str),
                    self._elapsed_ms(started_at),
                    attempts,
                )
                trace.append(
                    TraceEvent(
                        step,
                        "tool",
                        f"Tool completed: {call.name}",
                        {
                            "tool": call.name,
                            "ok": True,
                            "attempts": attempts,
                            "duration_ms": observation.duration_ms,
                        },
                    )
                )
                return observation

            assert last_error is not None
            if not (last_error.retryable and can_retry and attempts < max_attempts):
                break
            trace.append(
                TraceEvent(
                    step,
                    "retry",
                    f"Retrying tool: {call.name}",
                    {"tool": call.name, "attempt": attempts, "error_code": last_error.code},
                )
            )
            await asyncio.sleep(self.settings.tool_retry_base_seconds * (2 ** (attempts - 1)))

        assert last_error is not None
        return self._failed_observation(call, last_error, started_at, attempts, trace, step)

    def _failed_observation(
        self,
        call: ToolCall,
        error: RepoPilotError,
        started_at: float,
        attempts: int,
        trace: list[TraceEvent],
        step: int,
    ) -> ToolObservation:
        log_exception_safely(
            logger,
            "Tool call failed",
            error,
            extra={"operation": call.name, "error_code": error.code},
        )
        observation = ToolObservation(
            call.call_id,
            call.name,
            False,
            json.dumps(
                {"ok": False, "error": {"code": error.code, "message": error.safe_message}},
                ensure_ascii=False,
            ),
            self._elapsed_ms(started_at),
            attempts,
            error.code,
        )
        trace.append(
            TraceEvent(
                step,
                "tool",
                f"Tool failed: {call.name}",
                {
                    "tool": call.name,
                    "ok": False,
                    "attempts": attempts,
                    "duration_ms": observation.duration_ms,
                    "error_code": error.code,
                },
            )
        )
        return observation

    @staticmethod
    def _find_duplicate(calls: tuple[ToolCall, ...], seen_calls: set[str]) -> str | None:
        for call in calls:
            fingerprint = json.dumps(
                {"name": call.name, "arguments": call.arguments},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if fingerprint in seen_calls:
                return call.name
            seen_calls.add(fingerprint)
        return None

    @staticmethod
    def _assistant_tool_message(response: ModelResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.text,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in response.tool_calls
            ],
        }

    @staticmethod
    def _tool_message(observation: ToolObservation) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": observation.call_id,
            "name": observation.name,
            "content": observation.content,
        }

    @staticmethod
    def _model_trace(step: int, response: ModelResponse) -> TraceEvent:
        return TraceEvent(
            step,
            "model",
            "Model response received",
            {
                "tool_calls": len(response.tool_calls),
                "model": response.model,
                "finish_reason": response.finish_reason,
                "tokens": response.usage.total_tokens if response.usage else 0,
                "fallback_used": response.fallback_used,
            },
        )

    @staticmethod
    def _guard_result(
        detail: str,
        messages: list[dict[str, Any]],
        trace: list[TraceEvent],
        step: int,
        total_tokens: int,
        degraded: bool,
    ) -> AgentRunResult:
        trace.append(TraceEvent(step, "guard", detail))
        return AgentRunResult(
            detail,
            tuple(messages),
            tuple(trace),
            step,
            "guarded",
            total_tokens,
            degraded,
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)
