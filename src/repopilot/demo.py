from __future__ import annotations

from .agent import AgentLoop, Message
from .models import ModelResponse, ToolCall
from .tools import ToolRegistry, add, multiply


class ScriptedModel:
    """Deterministic stand-in for an LLM, useful for learning and tests."""

    def respond(self, messages: list[Message], tools: list[dict[str, str]]) -> ModelResponse:
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return ModelResponse(
                tool_calls=(
                    ToolCall(name="multiply", arguments={"a": 6, "b": 7}, call_id="call-1"),
                )
            )
        return ModelResponse(text=f"工具计算结果是 {tool_messages[-1]['content']}。")


class DirectAnswerModel:
    def respond(
        self,
        messages: list[Message],
        tools: list[dict[str, str]],
    ) -> ModelResponse:
        return ModelResponse(text="我是直接回答模型，不需要调用工具。")


def main() -> None:
    tools = ToolRegistry()
    tools.register("add", "计算两个数字之和", add)
    tools.register("multiply", "计算两个数字之积", multiply)
    agent = AgentLoop(DirectAnswerModel(), tools)
    result = agent.run("6 * 7 等于多少？")

    print(result.answer)
    print("\nTrace:")
    for event in result.trace:
        print(f"- step={event.step} event={event.event}: {event.detail}")


if __name__ == "__main__":
    main()
