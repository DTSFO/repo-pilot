from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from ..models import ModelResponse, TokenUsage
from .base import ModelRequest, ProviderHealth


class DeterministicProvider:
    """Offline provider used by tests, demos, and provider fallback."""

    name = "deterministic"

    def __init__(self, responses: Iterable[ModelResponse] = ()) -> None:
        self._responses = deque(responses)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._responses:
            return self._responses.popleft()

        user_text = next(
            (
                str(message.get("content", ""))
                for message in reversed(request.messages)
                if message.get("role") == "user"
            ),
            "",
        )
        text = f"RepoPilot is running in deterministic offline mode. Received: {user_text[:200]}"
        return ModelResponse(
            text=text,
            finish_reason="stop",
            model="deterministic-v1",
            usage=TokenUsage(),
            response_id="deterministic-response",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, self.name, "deterministic-v1", "offline")

    async def close(self) -> None:
        return None
