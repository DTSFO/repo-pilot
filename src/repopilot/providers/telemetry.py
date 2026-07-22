from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from ..observability import PROVIDER_TELEMETRY_DROPPED

ProviderEventPhase = Literal[
    "started",
    "first_byte",
    "progress",
    "completed",
    "retry",
    "timeout",
    "failed",
    "cancelled",
]
type ProviderEventValue = str | int | float | bool | None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderEvent:
    """Content-free lifecycle metadata for one provider call."""

    phase: ProviderEventPhase
    call_id: str
    provider: str
    model: str | None
    purpose: str | None
    elapsed_ms: float
    metadata: Mapping[str, ProviderEventValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


type ProviderEventSink = Callable[[ProviderEvent], Awaitable[None] | None]


@dataclass(frozen=True)
class ProviderCallContext:
    call_id: str
    attempt: int = 1
    max_attempts: int = 1


_provider_event_sink: ContextVar[ProviderEventSink | None] = ContextVar(
    "repopilot_provider_event_sink",
    default=None,
)
_provider_call_context: ContextVar[ProviderCallContext | None] = ContextVar(
    "repopilot_provider_call_context",
    default=None,
)


@contextmanager
def provider_event_sink(sink: ProviderEventSink | None) -> Iterator[None]:
    """Install an async-context-local event sink for provider lifecycle events."""

    token = _provider_event_sink.set(sink)
    try:
        yield
    finally:
        _provider_event_sink.reset(token)


@contextmanager
def provider_call_context(context: ProviderCallContext) -> Iterator[None]:
    """Propagate a stable logical call id and retry attempt to the concrete provider."""

    token = _provider_call_context.set(context)
    try:
        yield
    finally:
        _provider_call_context.reset(token)


def get_provider_call_context() -> ProviderCallContext | None:
    return _provider_call_context.get()


async def emit_provider_event(event: ProviderEvent) -> None:
    """Emit best-effort telemetry without allowing observers to break inference."""

    sink = _provider_event_sink.get()
    if sink is None:
        return
    try:
        outcome = sink(event)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception as exc:
        # Observability must never change provider success or failure semantics.
        PROVIDER_TELEMETRY_DROPPED.inc()
        logger.warning("Provider telemetry sink failed: %s", type(exc).__name__)
        return
