from __future__ import annotations

import asyncio
import json
import logging
import traceback
import unittest
from collections.abc import AsyncIterator

import httpx
from pydantic import BaseModel, SecretStr

from repopilot.errors import (
    ConfigurationError,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from repopilot.models import ModelResponse
from repopilot.providers import (
    CircuitBreaker,
    DeterministicProvider,
    LangChainOpenAIProvider,
    ModelRequest,
    OpenAICompatibleProvider,
    ResilientProvider,
    RetryPolicy,
    provider_event_sink,
)
from repopilot.providers.base import ProviderHealth
from repopilot.providers.telemetry import ProviderEvent


def provider_with_handler(
    handler: httpx.AsyncBaseTransport,
    **kwargs: object,
) -> LangChainOpenAIProvider:
    options: dict[str, object] = {
        "base_url": "https://provider.example/v1",
        "api_key": SecretStr("test-key"),
        "model": "test-model",
        "connect_timeout_seconds": 1,
        "read_timeout_seconds": 1,
        "write_timeout_seconds": 1,
        "pool_timeout_seconds": 1,
        "transport": handler,
    }
    options.update(kwargs)
    return LangChainOpenAIProvider(**options)  # type: ignore[arg-type]


class DelayedSSEStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, initial_delay: float = 0) -> None:
        self.chunks = chunks
        self.initial_delay = initial_delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.initial_delay:
            await asyncio.sleep(self.initial_delay)
        for chunk in self.chunks:
            yield chunk


class PlanOutput(BaseModel):
    queries: list[str]


class LangChainOpenAIProviderTest(unittest.IsolatedAsyncioTestCase):
    def test_legacy_class_name_resolves_to_langchain_provider(self) -> None:
        self.assertIs(OpenAICompatibleProvider, LangChainOpenAIProvider)

    async def test_non_stream_tool_binding_and_usage_mapping(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://provider.example/v1/chat/completions")
            payload = json.loads(request.content)
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["tool_choice"], "auto")
            self.assertEqual(payload["tools"][0]["function"]["name"], "search_repository")
            return httpx.Response(
                200,
                json={
                    "id": "response-1",
                    "model": "served-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
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

        provider = provider_with_handler(
            httpx.MockTransport(handler),
            streaming_enabled=False,
        )
        response = await provider.complete(
            ModelRequest(
                messages=({"role": "user", "content": "find agent"},),
                tools=(
                    {
                        "name": "search_repository",
                        "description": "Search repository",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                ),
                purpose="researcher",
            )
        )
        await provider.close()

        self.assertEqual(response.tool_calls[0].name, "search_repository")
        self.assertEqual(response.tool_calls[0].arguments, {"query": "agent"})
        self.assertEqual(response.usage.total_tokens if response.usage else None, 14)
        self.assertFalse(response.usage_estimated)
        self.assertEqual(response.model, "served-model")

    async def test_langchain_assembles_streamed_text_and_fragmented_tool_arguments(self) -> None:
        chunks = [
            b'data: {"id":"response-2","object":"chat.completion.chunk",'
            b'"model":"served-model","choices":[{"index":0,"delta":'
            b'{"role":"assistant","content":"Working ","tool_calls":[{"index":0,'
            b'"id":"call-2","type":"function","function":{"name":"search_repository",'
            b'"arguments":"{\\"query\\":\\"ag"}}]},"finish_reason":null}]}\n\n',
            b'data: {"id":"response-2","object":"chat.completion.chunk",'
            b'"model":"served-model","choices":[{"index":0,"delta":'
            b'{"content":"now","tool_calls":[{"index":0,"function":'
            b'{"arguments":"ent\\"}"}}]},"finish_reason":null}]}\n\n',
            b'data: {"id":"response-2","object":"chat.completion.chunk",'
            b'"model":"served-model","choices":[{"index":0,"delta":{},'
            b'"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":11,'
            b'"completion_tokens":5,"total_tokens":16}}\n\n',
            b"data: [DONE]\n\n",
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["stream_options"], {"include_usage": True})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DelayedSSEStream(chunks),
            )

        events: list[ProviderEvent] = []
        provider = provider_with_handler(httpx.MockTransport(handler))
        with provider_event_sink(events.append):
            response = await provider.complete(
                ModelRequest(
                    messages=({"role": "user", "content": "research"},),
                    tools=(
                        {
                            "name": "search_repository",
                            "description": "Search repository",
                            "parameters": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                        },
                    ),
                    purpose="researcher",
                )
            )
        await provider.close()

        self.assertEqual(response.text, "Working now")
        self.assertEqual(response.tool_calls[0].arguments, {"query": "agent"})
        self.assertEqual(response.usage.total_tokens if response.usage else None, 16)
        self.assertEqual([event.phase for event in events], ["started", "first_byte", "completed"])
        self.assertFalse(events[-1].metadata["usage_estimated"])

    async def test_structured_output_uses_langchain_schema_and_hides_protocol_tool_call(
        self,
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            schema_tool = payload["tools"][0]["function"]
            self.assertEqual(schema_tool["name"], "PlanOutput")
            self.assertEqual(payload["tool_choice"]["function"]["name"], "PlanOutput")
            return httpx.Response(
                200,
                json={
                    "id": "structured-1",
                    "model": "served-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "schema-1",
                                        "type": "function",
                                        "function": {
                                            "name": "PlanOutput",
                                            "arguments": '{"queries":["agent loop"]}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
                },
            )

        provider = provider_with_handler(
            httpx.MockTransport(handler),
            streaming_enabled=False,
        )
        response = await provider.complete(
            ModelRequest(
                messages=({"role": "user", "content": "plan"},),
                response_schema=PlanOutput,
                purpose="planner",
            )
        )
        await provider.close()

        self.assertEqual(json.loads(response.text or ""), {"queries": ["agent loop"]})
        self.assertEqual(response.tool_calls, ())

    async def test_missing_usage_uses_conservative_estimate_and_marks_telemetry(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "served-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "你好世界"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        events: list[ProviderEvent] = []
        provider = provider_with_handler(
            httpx.MockTransport(handler),
            streaming_enabled=False,
        )
        with provider_event_sink(events.append):
            response = await provider.complete(
                ModelRequest(messages=({"role": "user", "content": "分析代码"},))
            )
        await provider.close()

        self.assertTrue(response.usage_estimated)
        self.assertGreater(response.usage.total_tokens if response.usage else 0, 0)
        self.assertTrue(events[-1].metadata["usage_estimated"])
        self.assertFalse(events[-1].metadata["usage_reported"])
        self.assertEqual(LangChainOpenAIProvider._estimate_text_tokens("你好世界"), 4)
        self.assertEqual(LangChainOpenAIProvider._estimate_text_tokens("x = f(a)"), 4)

    async def test_structured_output_schema_is_included_in_fallback_usage_estimate(self) -> None:
        provider = provider_with_handler(httpx.MockTransport(lambda request: httpx.Response(200)))
        messages = ({"role": "user", "content": "plan"},)
        try:
            plain = provider._estimate_usage(ModelRequest(messages=messages), "result", ())
            structured = provider._estimate_usage(
                ModelRequest(messages=messages, response_schema=PlanOutput),
                "result",
                (),
            )

            self.assertGreater(structured.prompt_tokens, plain.prompt_tokens)
        finally:
            await provider.close()

    async def test_emits_waiting_progress_before_first_stream_chunk(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            chunk = (
                b'data: {"id":"x","object":"chat.completion.chunk","model":"m",'
                b'"choices":[{"index":0,"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DelayedSSEStream([chunk], initial_delay=0.035),
            )

        events: list[ProviderEvent] = []

        async def slow_started_sink(event: ProviderEvent) -> None:
            if event.phase == "started":
                await asyncio.sleep(0.02)
            events.append(event)

        provider = provider_with_handler(
            httpx.MockTransport(handler),
            stream_progress_interval_seconds=0.01,
            stream_include_usage=False,
        )
        with provider_event_sink(slow_started_sink):
            response = await provider.complete(
                ModelRequest(messages=({"role": "user", "content": "x"},))
            )
        await provider.close()

        self.assertEqual(response.text, "ok")
        self.assertEqual(events[0].phase, "started")
        progress = [event for event in events if event.phase == "progress"]
        self.assertTrue(progress)
        self.assertEqual(progress[0].metadata["state"], "waiting_first_byte")

    async def test_maps_standard_sdk_failures_to_stable_errors(self) -> None:
        cases = [
            (401, ProviderAuthenticationError),
            (429, ProviderRateLimitError),
            (503, ProviderUnavailableError),
        ]
        for status, error_type in cases:
            with self.subTest(status=status):

                async def handler(request: httpx.Request, code: int = status) -> httpx.Response:
                    return httpx.Response(
                        code,
                        json={
                            "error": {
                                "message": "provider rejected request",
                                "type": "provider_error",
                                "code": "rejected",
                            }
                        },
                    )

                provider = provider_with_handler(
                    httpx.MockTransport(handler),
                    streaming_enabled=False,
                )
                with self.assertRaises(error_type):
                    await provider.complete(
                        ModelRequest(messages=({"role": "user", "content": "x"},))
                    )
                await provider.close()

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        provider = provider_with_handler(
            httpx.MockTransport(timeout_handler),
            streaming_enabled=False,
        )
        with self.assertRaises(ProviderTimeoutError):
            await provider.complete(ModelRequest(messages=({"role": "user", "content": "x"},)))
        await provider.close()

    async def test_health_uses_models_endpoint_and_maps_outage(self) -> None:
        async def healthy(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://provider.example/v1/models")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            return httpx.Response(200, json={"data": []})

        provider = provider_with_handler(httpx.MockTransport(healthy))
        health = await provider.health()
        await provider.close()
        self.assertTrue(health.available)

        async def unavailable(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        provider = provider_with_handler(httpx.MockTransport(unavailable))
        health = await provider.health()
        await provider.close()
        self.assertFalse(health.available)
        self.assertEqual(health.detail, ProviderUnavailableError.code)

    async def test_sensitive_transport_error_is_suppressed_from_traceback(self) -> None:
        endpoint = "https://private-provider.example/v1?key=secret"

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed to connect to {endpoint}", request=request)

        provider = provider_with_handler(
            httpx.MockTransport(handler),
            streaming_enabled=False,
        )
        with self.assertRaises(ProviderUnavailableError) as raised:
            await provider.complete(ModelRequest(messages=({"role": "user", "content": "x"},)))
        await provider.close()

        formatted = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertNotIn(endpoint, formatted)
        self.assertTrue(raised.exception.__suppress_context__)

    async def test_transport_info_logs_do_not_expose_provider_endpoint(self) -> None:
        captured: list[str] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        root = logging.getLogger()
        capture = CaptureHandler()
        old_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(capture)
        provider = provider_with_handler(
            httpx.MockTransport(handler),
            streaming_enabled=False,
        )
        try:
            await provider.complete(ModelRequest(messages=({"role": "user", "content": "x"},)))
        finally:
            await provider.close()
            root.removeHandler(capture)
            root.setLevel(old_level)

        self.assertNotIn("provider.example", "\n".join(captured))

    async def test_cancellation_is_reported_and_propagated(self) -> None:
        gate = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return httpx.Response(200, json={})

        events: list[ProviderEvent] = []
        provider = provider_with_handler(
            httpx.MockTransport(handler),
            streaming_enabled=False,
        )
        with provider_event_sink(events.append):
            task = asyncio.create_task(
                provider.complete(ModelRequest(messages=({"role": "user", "content": "x"},)))
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await provider.close()
        self.assertEqual(events[-1].phase, "cancelled")

    def test_model_request_rejects_tools_plus_structured_output(self) -> None:
        with self.assertRaises(ValueError):
            ModelRequest(
                messages=(),
                tools=({"name": "x"},),
                response_schema=PlanOutput,
            )


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
        self.assertEqual(primary.calls, 2)
        self.assertEqual(delays, [0.1])
        self.assertEqual([event.phase for event in events], ["retry"])

    async def test_cancellation_during_retry_releases_breaker(self) -> None:
        primary = StubProvider([ProviderUnavailableError()])
        sleeping = asyncio.Event()

        async def sleep(delay: float) -> None:
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
        self.assertEqual(str(breaker.state), "closed")

    async def test_cancelled_half_open_probe_releases_permit(self) -> None:
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

    async def test_open_circuit_rejects_without_primary_call(self) -> None:
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

        with self.assertRaises(ProviderUnavailableError) as raised:
            await provider.complete(ModelRequest(messages=()))
        formatted = "".join(
            traceback.format_exception(
                type(raised.exception), raised.exception, raised.exception.__traceback__
            )
        )
        self.assertNotIn(endpoint, formatted)

    async def test_auth_and_configuration_rejections_do_not_open_circuit(self) -> None:
        for rejection in (ProviderAuthenticationError(), ConfigurationError()):
            with self.subTest(error=rejection.code):
                primary = StubProvider([rejection, ModelResponse(text="ok")])
                breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
                provider = ResilientProvider(
                    primary,
                    retry_policy=RetryPolicy(max_attempts=3),
                    circuit_breaker=breaker,
                    fallback=DeterministicProvider([ModelResponse(text="must not run")]),
                )

                with self.assertRaises(type(rejection)):
                    await provider.complete(ModelRequest(messages=()))
                response = await provider.complete(ModelRequest(messages=()))

                self.assertEqual(str(breaker.state), "closed")
                self.assertEqual(response.text, "ok")

    async def test_fallback_provenance_is_preserved_for_every_role(self) -> None:
        for purpose in ("planner", "researcher", "reviewer", "writer"):
            with self.subTest(purpose=purpose):
                provider = ResilientProvider(
                    StubProvider([ProviderUnavailableError()]),
                    retry_policy=RetryPolicy(max_attempts=1),
                    circuit_breaker=CircuitBreaker(failure_threshold=1, recovery_seconds=10),
                    fallback=DeterministicProvider([ModelResponse(text=f"{purpose} fallback")]),
                )
                response = await provider.complete(ModelRequest(messages=(), purpose=purpose))
                self.assertEqual(response.text, f"{purpose} fallback")
                self.assertTrue(response.fallback_used)


if __name__ == "__main__":
    unittest.main()
