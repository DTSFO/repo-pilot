from __future__ import annotations

import unittest

from repopilot.agent import AgentLoop, Message
from repopilot.models import ModelResponse, ToolCall
from repopilot.tools import ToolRegistry, add, multiply


class ToolThenAnswerModel:
    def respond(self, messages: list[Message], tools: list[dict[str, str]]) -> ModelResponse:
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return ModelResponse(tool_calls=(ToolCall("add", {"a": 2, "b": 3}, "call-1"),))
        return ModelResponse(text=f"answer={tool_messages[-1]['content']}")


class RepeatingModel:
    def respond(self, messages: list[Message], tools: list[dict[str, str]]) -> ModelResponse:
        return ModelResponse(tool_calls=(ToolCall("add", {"a": 1, "b": 1}, "repeat"),))


class InvalidArgumentsModel:
    def respond(self, messages: list[Message], tools: list[dict[str, str]]) -> ModelResponse:
        if any(message.get("role") == "tool" for message in messages):
            return ModelResponse(text="handled")
        return ModelResponse(tool_calls=(ToolCall("add", {"a": 1}, "invalid"),))


def registry() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register("add", "add two values", add)
    return tools


class AgentLoopTest(unittest.TestCase):
    def test_tool_result_is_returned_to_model(self) -> None:
        result = AgentLoop(ToolThenAnswerModel(), registry()).run("calculate")
        self.assertEqual(result.answer, "answer=5")
        self.assertEqual(result.steps, 2)
        self.assertTrue(any(event.event == "tool" for event in result.trace))

    def test_repeated_tool_call_is_blocked(self) -> None:
        result = AgentLoop(RepeatingModel(), registry()).run("loop")
        self.assertIn("Repeated tool call blocked", result.answer)
        self.assertTrue(any(event.event == "guard" for event in result.trace))

    def test_invalid_arguments_become_observation(self) -> None:
        result = AgentLoop(InvalidArgumentsModel(), registry()).run("bad args")
        tool_messages = [message for message in result.messages if message.get("role") == "tool"]
        self.assertIn("ToolError", str(tool_messages[0]["content"]))
        self.assertEqual(result.answer, "handled")


class ToolTest(unittest.TestCase):
    def test_multiply(self) -> None:
        result = multiply(6, 7)

        self.assertEqual(result, 42)


class ToolRegistryTest(unittest.TestCase):
    def test_registered_tool_can_be_executed(self) -> None:
        tools = ToolRegistry()

        tools.register(
            "multiply",
            "计算两个数字的乘积",
            multiply,
        )

        result = tools.execute(
            "multiply",
            {"a": 6, "b": 7},
        )

        self.assertEqual(result, 42)


if __name__ == "__main__":
    unittest.main()
