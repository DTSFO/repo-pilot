from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..models import ModelResponse

Message = dict[str, Any]
ToolSchema = dict[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...] = ()
    temperature: float = 0.0
    max_tokens: int = 2048
    purpose: str | None = None


@dataclass(frozen=True)
class ProviderHealth:
    available: bool
    provider: str
    model: str | None = None
    detail: str | None = None


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Generate one provider-neutral response."""

    async def health(self) -> ProviderHealth:
        """Report provider availability without exposing credentials."""

    async def close(self) -> None:
        """Release provider resources."""
