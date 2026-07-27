from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolCall:
    """A model-proposed tool invocation."""

    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class TokenUsage:
    """Provider token accounting used by budgets and evaluation."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    """A provider-neutral model response.

    A response contains either final text or one or more tool calls. Keeping this
    shape independent from a vendor SDK makes the Agent Loop easy to test.
    """

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None
    usage_estimated: bool = False
    response_id: str | None = None
    fallback_used: bool = False

    def __post_init__(self) -> None:
        if self.text is None and not self.tool_calls:
            raise ValueError("ModelResponse needs text or at least one tool call")


@dataclass(frozen=True)
class TraceEvent:
    step: int
    event: Literal[
        "model",
        "tool",
        "retry",
        "finish",
        "guard",
        "error",
        "checkpoint",
        "workflow",
        "retrieval",
    ]
    detail: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolObservation:
    call_id: str
    name: str
    ok: bool
    content: str
    duration_ms: float
    attempts: int
    error_code: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    answer: str
    messages: tuple[dict[str, Any], ...]
    trace: tuple[TraceEvent, ...]
    steps: int
    status: Literal["completed", "guarded", "failed", "cancelled"]
    total_tokens: int = 0
    degraded: bool = False
