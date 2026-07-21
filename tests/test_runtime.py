from __future__ import annotations

import asyncio
import unittest

from repopilot.config import Settings
from repopilot.errors import ProviderUnavailableError
from repopilot.models import ModelResponse, TokenUsage, ToolCall
from repopilot.providers import DeterministicProvider
from repopilot.providers.base import ModelRequest, ProviderHealth
from repopilot.runtime import AsyncAgentRuntime
from repopilot.tools import ToolRegistry


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "max_steps": 4,
        "tool_timeout_seconds": 1,
        "tool_retry_base_seconds": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class AsyncAgentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_answer_completes(self) -> None:
        provider = DeterministicProvider(
            [ModelResponse(text="final", usage=TokenUsage(total_tokens=7))]
        )
        runtime = AsyncAgentRuntime(provider, ToolRegistry(), settings())

        result = await runtime.run("question")

        self.assertEqual(result.answer, "final")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.total_tokens, 7)

    async def test_provider_fallback_marks_run_degraded_and_is_traced(self) -> None:
        provider = DeterministicProvider(
            [ModelResponse(text="fallback answer", fallback_used=True)]
        )
        runtime = AsyncAgentRuntime(provider, ToolRegistry(), settings())

        result = await runtime.run("question")

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.degraded)
        model_event = next(event for event in result.trace if event.event == "model")
        self.assertTrue(model_event.metadata["fallback_used"])

    async def test_read_only_tools_start_concurrently(self) -> None:
        both_started = asyncio.Event()
        started = 0

        async def read_value(value: str) -> str:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            return value

        tools = ToolRegistry()
        tools.register("search", "search", lambda: read_value("evidence"))
        tools.register("memory", "memory", lambda: read_value("preference"))
        provider = DeterministicProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("search", {}, "call-search"),
                        ToolCall("memory", {}, "call-memory"),
                    )
                ),
                ModelResponse(text="combined"),
            ]
        )
        runtime = AsyncAgentRuntime(provider, tools, settings())

        result = await runtime.run("research")

        self.assertEqual(result.answer, "combined")
        tool_messages = [message for message in result.messages if message["role"] == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertFalse(result.degraded)

    async def test_mutating_tools_run_sequentially(self) -> None:
        active = 0
        max_active = 0

        async def mutate(value: str) -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return value

        tools = ToolRegistry()
        tools.register("write_a", "write", lambda: mutate("a"), read_only=False)
        tools.register("write_b", "write", lambda: mutate("b"), read_only=False)
        provider = DeterministicProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("write_a", {}, "a"),
                        ToolCall("write_b", {}, "b"),
                    )
                ),
                ModelResponse(text="done"),
            ]
        )

        result = await AsyncAgentRuntime(provider, tools, settings()).run("write")

        self.assertEqual(result.status, "completed")
        self.assertEqual(max_active, 1)

    async def test_retryable_tool_failure_is_retried(self) -> None:
        attempts = 0

        def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("temporary internal failure")
            return "recovered"

        tools = ToolRegistry()
        tools.register("flaky", "flaky", flaky, retryable=True)
        provider = DeterministicProvider(
            [
                ModelResponse(tool_calls=(ToolCall("flaky", {}, "flaky-1"),)),
                ModelResponse(text="done"),
            ]
        )

        result = await AsyncAgentRuntime(provider, tools, settings()).run("retry")

        self.assertEqual(attempts, 2)
        self.assertTrue(any(event.event == "retry" for event in result.trace))
        self.assertFalse(result.degraded)

    async def test_partial_failure_is_safe_and_marks_degraded(self) -> None:
        def fail() -> None:
            raise RuntimeError("password=do-not-leak")

        tools = ToolRegistry()
        tools.register("good", "good", lambda: "evidence")
        tools.register("bad", "bad", fail)
        provider = DeterministicProvider(
            [
                ModelResponse(
                    tool_calls=(ToolCall("good", {}, "good"), ToolCall("bad", {}, "bad"))
                ),
                ModelResponse(text="partial answer"),
            ]
        )

        with self.assertLogs("repopilot.runtime", level="ERROR"):
            result = await AsyncAgentRuntime(provider, tools, settings()).run("partial")

        self.assertTrue(result.degraded)
        public_messages = " ".join(str(message) for message in result.messages)
        self.assertNotIn("do-not-leak", public_messages)
        self.assertIn("tool_execution_failed", public_messages)

    async def test_duplicate_call_is_guarded(self) -> None:
        tools = ToolRegistry()
        tools.register("echo", "echo", lambda text: text)
        repeated = ModelResponse(tool_calls=(ToolCall("echo", {"text": "x"}, "call"),))
        provider = DeterministicProvider([repeated, repeated])

        result = await AsyncAgentRuntime(provider, tools, settings()).run("repeat")

        self.assertEqual(result.status, "guarded")
        self.assertIn("Repeated tool call blocked", result.answer)

    async def test_tool_timeout_is_reported_and_keeps_later_answer(self) -> None:
        async def slow() -> str:
            await asyncio.sleep(0.05)
            return "late"

        tools = ToolRegistry()
        tools.register("slow", "slow", slow)
        provider = DeterministicProvider(
            [
                ModelResponse(tool_calls=(ToolCall("slow", {}, "slow-1"),)),
                ModelResponse(text="answer from remaining evidence"),
            ]
        )

        result = await AsyncAgentRuntime(
            provider,
            tools,
            settings(tool_timeout_seconds=0.01),
        ).run("timeout")

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.degraded)
        tool_message = next(message for message in result.messages if message["role"] == "tool")
        self.assertIn("tool_timeout", str(tool_message["content"]))

    async def test_token_and_tool_call_budgets_fail_closed(self) -> None:
        token_result = await AsyncAgentRuntime(
            DeterministicProvider(
                [ModelResponse(text="over budget", usage=TokenUsage(total_tokens=11))]
            ),
            ToolRegistry(),
            settings(max_total_tokens=10),
        ).run("tokens")
        self.assertEqual(token_result.status, "guarded")
        self.assertIn("token budget", token_result.answer)

        tools = ToolRegistry()
        tools.register("echo", "echo", lambda text: text)
        tool_result = await AsyncAgentRuntime(
            DeterministicProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall("echo", {"text": "a"}, "a"),
                            ToolCall("echo", {"text": "b"}, "b"),
                        )
                    )
                ]
            ),
            tools,
            settings(max_tool_calls=1),
        ).run("tools")
        self.assertEqual(tool_result.status, "guarded")
        self.assertIn("tool-call budget", tool_result.answer)

    async def test_cancellation_propagates_without_becoming_a_failed_result(self) -> None:
        started = asyncio.Event()

        async def slow() -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

        tools = ToolRegistry()
        tools.register("slow", "slow", slow)
        runtime = AsyncAgentRuntime(
            DeterministicProvider(
                [ModelResponse(tool_calls=(ToolCall("slow", {}, "slow-cancel"),))]
            ),
            tools,
            settings(),
        )
        task = asyncio.create_task(runtime.run("cancel"))
        await started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task


class FailedProvider:
    name = "failed"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ProviderUnavailableError()

    async def health(self) -> ProviderHealth:
        return ProviderHealth(False, self.name)

    async def close(self) -> None:
        return None


class FailedProviderRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_becomes_safe_failed_result(self) -> None:
        runtime = AsyncAgentRuntime(FailedProvider(), ToolRegistry(), settings())

        with self.assertLogs("repopilot.runtime", level="ERROR"):
            result = await runtime.run("question")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.answer, ProviderUnavailableError.safe_message)


if __name__ == "__main__":
    unittest.main()
