from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .models import ModelResponse, TraceEvent
from .tools import ToolRegistry

Message = dict[str, object]


class ModelClient(Protocol):
    def respond(self, messages: list[Message], tools: list[dict[str, str]]) -> ModelResponse:
        """Return final text or tool calls."""


@dataclass(frozen=True)
class AgentResult:
    answer: str
    messages: tuple[Message, ...]
    trace: tuple[TraceEvent, ...]
    steps: int


class AgentLoop:
    """Minimal provider-neutral Agent Harness.

    The model proposes actions. The harness validates, executes, observes and
    decides when to stop. Production features will be added in later lessons.
    """

    def __init__(self, model: ModelClient, tools: ToolRegistry, max_steps: int = 8) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.model = model
        self.tools = tools
        self.max_steps = max_steps

    def run(self, user_input: str) -> AgentResult:
        messages: list[Message] = [{"role": "user", "content": user_input}]
        trace: list[TraceEvent] = []
        seen_calls: set[str] = set()

        for step in range(1, self.max_steps + 1):
            response = self.model.respond(messages, self.tools.descriptions())
            trace.append(
                TraceEvent(
                    step,
                    "model",
                    "Model response received",
                    {"tool_calls": len(response.tool_calls)},
                )
            )

            if response.text is not None and not response.tool_calls:
                messages.append({"role": "assistant", "content": response.text})
                trace.append(TraceEvent(step, "finish", "Final answer returned"))
                return AgentResult(response.text, tuple(messages), tuple(trace), step)

            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": call.call_id, "name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                }
            )

            for call in response.tool_calls:
                fingerprint = json.dumps(
                    {"name": call.name, "arguments": call.arguments},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if fingerprint in seen_calls:
                    detail = f"Repeated tool call blocked: {call.name}"
                    trace.append(TraceEvent(step, "guard", detail))
                    return AgentResult(detail, tuple(messages), tuple(trace), step)
                seen_calls.add(fingerprint)

                try:
                    output = self.tools.execute(call.name, call.arguments)
                    content = str(output)
                    metadata = {"tool": call.name, "ok": True}
                except (KeyError, ValueError, RuntimeError) as exc:
                    content = f"ToolError: {exc}"
                    metadata = {"tool": call.name, "ok": False, "error": type(exc).__name__}

                trace.append(TraceEvent(step, "tool", content, metadata))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": content,
                    }
                )

        detail = f"Stopped after reaching max_steps={self.max_steps}"
        trace.append(TraceEvent(self.max_steps, "guard", detail))
        return AgentResult(detail, tuple(messages), tuple(trace), self.max_steps)
