from __future__ import annotations

import unittest

import httpx
from pydantic import SecretStr

from repopilot.errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from repopilot.models import ModelResponse
from repopilot.providers import (
    CircuitBreaker,
    DeterministicProvider,
    ModelRequest,
    OpenAICompatibleProvider,
    ResilientProvider,
    RetryPolicy,
)
from repopilot.providers.base import ProviderHealth


def provider_with_handler(
    handler: httpx.AsyncBaseTransport | httpx.MockTransport,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://provider.example/v1",
        api_key=SecretStr("test-key"),
        model="test-model",
        timeout_seconds=1,
        transport=handler,
    )


class OpenAICompatibleProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_parses_text_tool_calls_and_usage(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://provider.example/v1/chat/completions")
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

        timed_out = provider_with_handler(httpx.MockTransport(timeout_handler))
        with self.assertRaises(ProviderTimeoutError):
            await timed_out.complete(ModelRequest(messages=()))
        await timed_out.close()


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

        response = await provider.complete(ModelRequest(messages=()))

        self.assertEqual(response.text, "recovered")
        self.assertFalse(response.fallback_used)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(delays, [0.1])

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

        response = await provider.complete(ModelRequest(messages=()))

        self.assertEqual(response.text, "offline fallback")
        self.assertTrue(response.fallback_used)
        self.assertEqual(str(breaker.state), "open")

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
        second = await provider.complete(ModelRequest(messages=()))

        self.assertTrue(first.fallback_used)
        self.assertEqual(second.text, "circuit fallback")
        self.assertTrue(second.fallback_used)
        self.assertEqual(primary.calls, 1)

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
        with self.assertRaises(ProviderUnavailableError):
            await provider.complete(ModelRequest(messages=()))

        self.assertEqual(primary.calls, 1)

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
