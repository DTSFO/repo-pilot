from __future__ import annotations

import unittest

from repopilot.errors import InvalidToolArgumentsError, ToolUnavailableError, UnknownToolError
from repopilot.tools import ToolRegistry


class ToolRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_generates_schema_and_executes_sync_and_async_tools(self) -> None:
        registry = ToolRegistry()

        def multiply(a: float, b: float = 2) -> float:
            return a * b

        async def greet(name: str) -> str:
            return f"hello {name}"

        registry.register("multiply", "multiply", multiply)
        registry.register("greet", "greet", greet)

        descriptions = {item["name"]: item for item in registry.descriptions()}
        multiply_schema = descriptions["multiply"]["parameters"]
        self.assertEqual(multiply_schema["properties"]["a"], {"type": "number"})
        self.assertEqual(multiply_schema["required"], ["a"])
        self.assertEqual(await registry.aexecute("multiply", {"a": 3}), 6)
        self.assertEqual(await registry.aexecute("greet", {"name": "RepoPilot"}), "hello RepoPilot")

    async def test_unknown_and_invalid_calls_use_stable_errors(self) -> None:
        registry = ToolRegistry()
        registry.register("echo", "echo", lambda text: text)

        with self.assertRaises(UnknownToolError):
            await registry.aexecute("missing", {})
        with self.assertRaises(InvalidToolArgumentsError):
            await registry.aexecute("echo", {})

    async def test_transient_system_error_is_retryable(self) -> None:
        registry = ToolRegistry()

        def unavailable() -> None:
            raise ConnectionError("internal endpoint details")

        registry.register("network", "network", unavailable, retryable=True)

        with self.assertRaises(ToolUnavailableError) as raised:
            await registry.aexecute("network", {})
        self.assertNotIn("internal endpoint details", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
