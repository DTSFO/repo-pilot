from __future__ import annotations

import asyncio
import json
import logging
import traceback
import unittest

import httpx
from pydantic import SecretStr

from repopilot.errors import (
    ConfigurationError,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from repopilot.models import ModelResponse, TokenUsage, ToolCall
from repopilot.providers import (
    CircuitBreaker,
    DeterministicProvider,
    ModelRequest,
    OpenAICompatibleProvider,
    ResilientProvider,
    RetryPolicy,
    provider_event_sink,
)
from repopilot.providers.base import ProviderHealth
from repopilot.providers.telemetry import ProviderEvent


def provider_with_handler(
    handler: httpx.AsyncBaseTransport | httpx.MockTransport,
    **kwargs: object,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://provider.example/v1",
        api_key=SecretStr("test-key"),
        model="test-model",
        timeout_seconds=1,
        transport=handler,
        **kwargs,
    )


class DelayedSSEStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, initial_delay: float = 0) -> None:
        self.chunks = chunks
        self.initial_delay = initial_delay

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        if self.initial_delay:
            await asyncio.sleep(self.initial_delay)
        for chunk in self.chunks:
            yield chunk


class OpenAICompatibleProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_parses_text_tool_calls_and_usage(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://provider.example/v1/chat/completions")
            payload = json.loads(request.content)
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["stream_options"], {"include_usage": True})
            return httpx.Response(
                200,
                json={
                    "id": "response-1",
                    "model": "served-model",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_repository",
                                            "arguments": '{"query":"agent"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                    },
                },
            )

        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            response = await provider.complete(
                ModelRequest(
                    messages=({"role": "user", "content": "search"},),
                    tools=(
                        {
                            "name": "search_repository",
                            "description": "search",
                            "parameters": {"type": "object"},
                        },
                    ),
                )
            )
        finally:
            await provider.close()

        self.assertEqual(response.model, "served-model")
        self.assertEqual(response.tool_calls[0].name, "search_repository")
        self.assertEqual(response.tool_calls[0].arguments, {"query": "agent"})
        self.assertIsNotNone(response.usage)
        assert response.usage is not None
        self.assertEqual(response.usage.total_tokens, 14)

    async def test_buffers_sse_text_fragmented_tool_calls_usage_and_safe_events(self) -> None:
        chunks = [
            b'data: {"id":"response-2","model":"served-model","choices":[{"delta":'
            b'{"content":"Working ","tool_calls":[{"index":0,"id":"call-2",'
            b'"function":{"name":"search_","arguments":"{\\"query\\":\\"ag"}}]},'
            b'"finish_reason":null}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"now","tool_calls":[{"index":0,'
            b'"function":{"name":"repository","arguments":"ent\\"}"}}]},'
            b'"finish_reason":"tool_calls"}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":5,'
            b'"total_tokens":16}}\n\n',
            b"data: [DONE]\n\n",
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DelayedSSEStream(chunks),
            )

        events: list[ProviderEvent] = []
        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            with provider_event_sink(events.append):
                response = await provider.complete(
                    ModelRequest(
                        messages=({"role": "user", "content": "search"},),
                        purpose="researcher",
                    )
                )
        finally:
            await provider.close()

        self.assertEqual(response.text, "Working now")
        self.assertEqual(response.tool_calls[0].name, "search_repository")
        self.assertEqual(response.tool_calls[0].arguments, {"query": "agent"})
        self.assertEqual(response.response_id, "response-2")
        self.assertIsNotNone(response.usage)
        assert response.usage is not None
        self.assertEqual(response.usage.total_tokens, 16)
        self.assertEqual([event.phase for event in events], ["started", "first_byte", "completed"])
        completed = events[-1]
        self.assertEqual(completed.purpose, "researcher")
        self.assertTrue(completed.metadata["usage_reported"])
        self.assertNotIn("Working now", repr(completed.metadata))

    async def test_stream_eof_without_done_or_finish_reason_is_rejected(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DelayedSSEStream(
                    [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n']
                ),
            )

        events: list[ProviderEvent] = []
        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            with self.assertRaises(ProviderResponseError), provider_event_sink(events.append):
                await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        self.assertEqual([event.phase for event in events], ["started", "first_byte", "failed"])
        self.assertEqual(events[-1].metadata["error_code"], "provider_invalid_response")

    async def test_terminal_finish_reason_allows_usage_only_tail_without_done(self) -> None:
        chunks = [
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n',
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DelayedSSEStream(chunks),
            )

        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            response = await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        self.assertEqual(response.text, "ok")
        self.assertEqual(response.finish_reason, "stop")
        self.assertIsNotNone(response.usage)
        assert response.usage is not None
        self.assertEqual(response.usage.total_tokens, 9)

    async def test_done_marker_is_terminal_without_finish_reason(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DelayedSSEStream(
                    [
                        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                        b"data: [DONE]",
                    ]
                ),
            )

        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            response = await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        self.assertEqual(response.text, "ok")
        self.assertIsNone(response.finish_reason)

    def test_usage_normalization_is_non_negative_and_conservative(self) -> None:
        missing_total = OpenAICompatibleProvider._parse_usage(
            {"prompt_tokens": 10, "completion_tokens": 4}
        )
        contradictory = OpenAICompatibleProvider._parse_usage(
            {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 3}
        )
        malformed = OpenAICompatibleProvider._parse_usage(
            {"prompt_tokens": -10, "completion_tokens": "bad", "total_tokens": -1}
        )
        numeric_strings = OpenAICompatibleProvider._parse_usage(
            {"prompt_tokens": "6", "completion_tokens": 2.0, "total_tokens": "8"}
        )

        self.assertEqual(missing_total, TokenUsage(10, 4, 14))
        self.assertEqual(contradictory, TokenUsage(10, 8, 18))
        self.assertIsNone(malformed)
        self.assertEqual(numeric_strings, TokenUsage(6, 2, 8))

    def test_usage_estimator_is_conservative_for_chinese_code_and_tool_json(self) -> None:
        self.assertEqual(OpenAICompatibleProvider._estimate_text_tokens("你好世界"), 4)
        self.assertEqual(OpenAICompatibleProvider._estimate_text_tokens("x = f(a)"), 4)
        response = ModelResponse(
            text="结果",
            tool_calls=(
                ToolCall(
                    name="run_code",
                    arguments={"代码": "def f(x):\n    return x + 1"},
                    call_id="call-1",
                ),
            ),
        )
        estimated = OpenAICompatibleProvider._estimate_usage(
            {"messages": [{"role": "user", "content": "分析 `def f(x): return x`"}]},
            response,
        )
        self.assertGreaterEqual(estimated.prompt_tokens, 15)
        self.assertGreaterEqual(estimated.completion_tokens, 2)
        self.assertEqual(
            estimated.total_tokens,
            estimated.prompt_tokens + estimated.completion_tokens,
        )

    async def test_emits_waiting_progress_before_first_stream_byte(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DelayedSSEStream(
                    [
                        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                        b"data: [DONE]\n\n"
                    ],
                    initial_delay=0.035,
                ),
            )

        events: list[ProviderEvent] = []
        provider = provider_with_handler(
            httpx.MockTransport(handler),
            stream_progress_interval_seconds=0.01,
        )
        try:
            with provider_event_sink(events.append):
                await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        waiting = [event for event in events if event.phase == "progress"]
        self.assertGreaterEqual(len(waiting), 2)
        self.assertTrue(all(event.metadata["state"] == "waiting_first_byte" for event in waiting))
        first_byte_index = next(i for i, event in enumerate(events) if event.phase == "first_byte")
        self.assertLess(events.index(waiting[0]), first_byte_index)

    async def test_non_stream_mode_remains_supported(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertFalse(payload["stream"])
            self.assertNotIn("stream_options", payload)
            return httpx.Response(
                200,
                json={
                    "model": "served-model",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                },
            )

        events: list[ProviderEvent] = []
        provider = provider_with_handler(
            httpx.MockTransport(handler),
            streaming_enabled=False,
        )
        try:
            with provider_event_sink(events.append):
                response = await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        self.assertEqual(response.text, "ok")
        self.assertIsNotNone(response.usage)
        assert response.usage is not None
        self.assertGreater(response.usage.total_tokens, 0)
        self.assertFalse(events[-1].metadata["usage_reported"])
        self.assertTrue(events[-1].metadata["usage_estimated"])

    async def test_maps_authentication_and_invalid_payload_errors(self) -> None:
        async def auth_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid token"})

        auth_provider = provider_with_handler(httpx.MockTransport(auth_handler))
        with self.assertRaises(ProviderAuthenticationError):
            await auth_provider.complete(ModelRequest(messages=()))
        await auth_provider.close()

        async def invalid_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        invalid_provider = provider_with_handler(httpx.MockTransport(invalid_handler))
        with self.assertRaises(ProviderResponseError):
            await invalid_provider.complete(ModelRequest(messages=()))
        await invalid_provider.close()

    async def test_health_uses_models_under_configured_base_path(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://provider.example/v1/models")
            return httpx.Response(200, json={"data": []})

        provider = provider_with_handler(httpx.MockTransport(handler))
        health = await provider.health()
        await provider.close()

        self.assertTrue(health.available)

    async def test_health_maps_transport_timeout_without_raising(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        provider = provider_with_handler(httpx.MockTransport(handler))
        health = await provider.health()
        await provider.close()

        self.assertFalse(health.available)
        self.assertEqual(health.detail, "provider_timeout")

    async def test_maps_rate_limit_server_and_transport_timeout_errors(self) -> None:
        async def rate_limit_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "slow down"})

        rate_limited = provider_with_handler(httpx.MockTransport(rate_limit_handler))
        with self.assertRaises(ProviderRateLimitError):
            await rate_limited.complete(ModelRequest(messages=()))
        await rate_limited.close()

        async def server_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "unavailable"})

        unavailable = provider_with_handler(httpx.MockTransport(server_handler))
        with self.assertRaises(ProviderUnavailableError):
            await unavailable.complete(ModelRequest(messages=()))
        await unavailable.close()

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        events: list[ProviderEvent] = []
        timed_out = provider_with_handler(httpx.MockTransport(timeout_handler))
        with self.assertRaises(ProviderTimeoutError), provider_event_sink(events.append):
            await timed_out.complete(ModelRequest(messages=(), purpose="reviewer"))
        await timed_out.close()

        self.assertEqual(events[-1].phase, "timeout")
        self.assertEqual(events[-1].metadata["timeout_kind"], "read")
        self.assertEqual(events[-1].metadata["attempt"], 1)

    async def test_transport_errors_suppress_sensitive_exception_chains(self) -> None:
        endpoint = "https://private-provider.example/v1/chat/completions?key=secret"

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed to connect to {endpoint}", request=request)

        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            with self.assertRaises(ProviderUnavailableError) as raised:
                await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(endpoint, str(raised.exception))
        formatted = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertNotIn(endpoint, formatted)

    async def test_invalid_json_body_is_not_retained_in_exception_chain(self) -> None:
        secret_body = "invalid-json https://private-provider.example/v1?token=secret"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=secret_body,
            )

        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            with self.assertRaises(ProviderResponseError) as raised:
                await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        formatted = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(secret_body, formatted)

    async def test_telemetry_sink_warning_logs_only_exception_type(self) -> None:
        secret = "https://private-provider.example/v1?token=secret"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            )

        def failing_sink(event: ProviderEvent) -> None:
            raise RuntimeError(f"sink failed for {secret}")

        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            with (
                self.assertLogs("repopilot.providers.telemetry", level="WARNING") as logs,
                provider_event_sink(failing_sink),
            ):
                response = await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()

        self.assertEqual(response.text, "ok")
        joined = "\n".join(logs.output)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("sink failed for", joined)

    async def test_http_transport_info_logs_do_not_expose_provider_endpoint(self) -> None:
        endpoint = "https://provider.example/v1/chat/completions"
        captured: list[str] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            )

        httpx_logger = logging.getLogger("httpx")
        httpcore_logger = logging.getLogger("httpcore")
        root_logger = logging.getLogger()
        old_httpx_level = httpx_logger.level
        old_httpcore_level = httpcore_logger.level
        old_root_level = root_logger.level
        capture = CaptureHandler()
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(capture)
        provider = provider_with_handler(httpx.MockTransport(handler))
        try:
            response = await provider.complete(ModelRequest(messages=()))
        finally:
            await provider.close()
            root_logger.removeHandler(capture)
            root_logger.setLevel(old_root_level)
            httpx_logger.setLevel(old_httpx_level)
            httpcore_logger.setLevel(old_httpcore_level)

        self.assertEqual(response.text, "ok")
        self.assertNotIn(endpoint, "\n".join(captured))

    async def test_cancellation_is_reported_and_propagated(self) -> None:
        gate = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return httpx.Response(200, json={})

        events: list[ProviderEvent] = []
        provider = provider_with_handler(httpx.MockTransport(handler))
        with provider_event_sink(events.append):
            task = asyncio.create_task(provider.complete(ModelRequest(messages=())))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await provider.close()

        self.assertEqual(events[-1].phase, "cancelled")


class StubProvider:
    name = "stub"

    def __init__(self, outcomes: list[ModelResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, self.name)

    async def close(self) -> None:
        return None


class BlockingProvider(StubProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ResilientProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_transient_failure_then_recovers(self) -> None:
        primary = StubProvider([ProviderUnavailableError(), ModelResponse(text="recovered")])
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            delays.append(delay)

        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0.1,
                max_delay_seconds=0.1,
                jitter_ratio=0,
            ),
            circuit_breaker=CircuitBreaker(failure_threshold=2, recovery_seconds=10),
            sleep=sleep,
        )

        events: list[ProviderEvent] = []
        with provider_event_sink(events.append):
            response = await provider.complete(ModelRequest(messages=()))

        self.assertEqual(response.text, "recovered")
        self.assertFalse(response.fallback_used)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(delays, [0.1])
        self.assertEqual([event.phase for event in events], ["retry"])
        self.assertEqual(events[0].metadata["attempt"], 1)
        self.assertTrue(events[0].metadata["will_retry"])

    async def test_cancellation_during_retry_backoff_emits_terminal_event(self) -> None:
        primary = StubProvider([ProviderUnavailableError()])
        sleeping = asyncio.Event()

        async def sleep(delay: float) -> None:
            self.assertEqual(delay, 0.1)
            sleeping.set()
            await asyncio.Event().wait()

        breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=10)
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0.1,
                max_delay_seconds=0.1,
                jitter_ratio=0,
            ),
            circuit_breaker=breaker,
            sleep=sleep,
        )

        events: list[ProviderEvent] = []
        with provider_event_sink(events.append):
            task = asyncio.create_task(provider.complete(ModelRequest(messages=())))
            await sleeping.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual([event.phase for event in events], ["retry", "cancelled"])
        self.assertEqual(events[0].call_id, events[1].call_id)
        self.assertTrue(events[-1].metadata["during_retry"])
        self.assertEqual(str(breaker.state), "closed")

    async def test_cancelled_half_open_probe_releases_single_probe_permit(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_seconds=1,
            clock=lambda: now[0],
        )
        breaker.record_failure()
        now[0] = 2.0
        primary = BlockingProvider()
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(max_attempts=1),
            circuit_breaker=breaker,
        )

        task = asyncio.create_task(provider.complete(ModelRequest(messages=())))
        await primary.started.wait()
        self.assertEqual(str(breaker.state), "half_open")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(str(breaker.state), "half_open")
        self.assertTrue(breaker.allow_call())
        breaker.record_cancelled()

    async def test_exhausted_failure_uses_explicit_fallback(self) -> None:
        primary = StubProvider([ProviderUnavailableError()])
        fallback = DeterministicProvider([ModelResponse(text="offline fallback")])
        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(max_attempts=1),
            circuit_breaker=breaker,
            fallback=fallback,
        )

        events: list[ProviderEvent] = []
        with provider_event_sink(events.append):
            response = await provider.complete(ModelRequest(messages=()))

        self.assertEqual(response.text, "offline fallback")
        self.assertTrue(response.fallback_used)
        self.assertEqual(str(breaker.state), "open")
        self.assertEqual(events[-1].phase, "completed")
        self.assertTrue(events[-1].metadata["fallback_used"])
        self.assertFalse(events[-1].metadata["usage_reported"])

    async def test_open_circuit_fallback_is_marked(self) -> None:
        primary = StubProvider([ProviderUnavailableError()])
        fallback = DeterministicProvider(
            [ModelResponse(text="first fallback"), ModelResponse(text="circuit fallback")]
        )
        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(max_attempts=1),
            circuit_breaker=breaker,
            fallback=fallback,
        )

        first = await provider.complete(ModelRequest(messages=()))
        events: list[ProviderEvent] = []
        with provider_event_sink(events.append):
            second = await provider.complete(ModelRequest(messages=()))

        self.assertTrue(first.fallback_used)
        self.assertEqual(second.text, "circuit fallback")
        self.assertTrue(second.fallback_used)
        self.assertEqual(primary.calls, 1)
        self.assertEqual([event.phase for event in events], ["started", "completed"])
        self.assertEqual(events[0].call_id, events[1].call_id)
        self.assertTrue(events[0].metadata["circuit_open"])

    async def test_open_circuit_rejects_without_calling_primary(self) -> None:
        primary = StubProvider([ProviderUnavailableError()])
        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(max_attempts=1),
            circuit_breaker=breaker,
        )

        with self.assertRaises(ProviderUnavailableError):
            await provider.complete(ModelRequest(messages=()))
        events: list[ProviderEvent] = []
        with self.assertRaises(ProviderUnavailableError), provider_event_sink(events.append):
            await provider.complete(ModelRequest(messages=()))

        self.assertEqual(primary.calls, 1)
        self.assertEqual([event.phase for event in events], ["started", "failed"])
        self.assertEqual(events[0].call_id, events[1].call_id)
        self.assertFalse(events[-1].metadata["fallback_used"])

    async def test_open_circuit_fallback_error_has_started_and_failed_terminal(self) -> None:
        primary = StubProvider([])
        fallback = StubProvider([ProviderAuthenticationError()])
        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
        breaker.record_failure()
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(max_attempts=2),
            circuit_breaker=breaker,
            fallback=fallback,
        )

        events: list[ProviderEvent] = []
        with self.assertRaises(ProviderAuthenticationError), provider_event_sink(events.append):
            await provider.complete(ModelRequest(messages=()))

        self.assertEqual([event.phase for event in events], ["started", "failed"])
        self.assertEqual(events[0].call_id, events[1].call_id)
        self.assertTrue(events[-1].metadata["fallback_used"])

    async def test_open_circuit_raw_fallback_error_is_safely_mapped(self) -> None:
        endpoint = "https://private-fallback.example/v1?key=secret"
        primary = StubProvider([])
        fallback = StubProvider([RuntimeError(f"failed at {endpoint}")])
        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
        breaker.record_failure()
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(max_attempts=1),
            circuit_breaker=breaker,
            fallback=fallback,
        )

        events: list[ProviderEvent] = []
        with (
            self.assertRaises(ProviderUnavailableError) as raised,
            provider_event_sink(events.append),
        ):
            await provider.complete(ModelRequest(messages=()))

        self.assertEqual([event.phase for event in events], ["started", "failed"])
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(endpoint, str(raised.exception))
        formatted = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertNotIn(endpoint, formatted)

    async def test_open_circuit_fallback_cancellation_has_terminal_event(self) -> None:
        primary = StubProvider([])
        fallback = BlockingProvider()
        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
        breaker.record_failure()
        provider = ResilientProvider(
            primary,
            retry_policy=RetryPolicy(max_attempts=1),
            circuit_breaker=breaker,
            fallback=fallback,
        )

        events: list[ProviderEvent] = []
        with provider_event_sink(events.append):
            task = asyncio.create_task(provider.complete(ModelRequest(messages=())))
            await fallback.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual([event.phase for event in events], ["started", "cancelled"])
        self.assertEqual(events[0].call_id, events[1].call_id)

    async def test_auth_and_configuration_rejections_do_not_open_circuit(self) -> None:
        for rejection in (ProviderAuthenticationError(), ConfigurationError()):
            with self.subTest(error=rejection.code):
                primary = StubProvider([rejection, ModelResponse(text="ok")])
                fallback = DeterministicProvider([ModelResponse(text="must not run")])
                breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
                provider = ResilientProvider(
                    primary,
                    retry_policy=RetryPolicy(max_attempts=3),
                    circuit_breaker=breaker,
                    fallback=fallback,
                )

                with self.assertRaises(type(rejection)):
                    await provider.complete(ModelRequest(messages=()))
                response = await provider.complete(ModelRequest(messages=()))

                self.assertEqual(str(breaker.state), "closed")
                self.assertEqual(response.text, "ok")
                self.assertFalse(response.fallback_used)
                self.assertEqual(primary.calls, 2)

    async def test_fallback_provenance_is_preserved_for_each_workflow_purpose(self) -> None:
        for purpose in ("planner", "researcher", "reviewer", "writer"):
            with self.subTest(purpose=purpose):
                primary = StubProvider([ProviderUnavailableError()])
                fallback = DeterministicProvider([ModelResponse(text=f"{purpose} fallback")])
                provider = ResilientProvider(
                    primary,
                    retry_policy=RetryPolicy(max_attempts=1),
                    circuit_breaker=CircuitBreaker(failure_threshold=1, recovery_seconds=10),
                    fallback=fallback,
                )

                response = await provider.complete(ModelRequest(messages=(), purpose=purpose))

                self.assertEqual(response.text, f"{purpose} fallback")
                self.assertTrue(response.fallback_used)


if __name__ == "__main__":
    unittest.main()
