from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic
from typing import Literal
from uuid import uuid4

from ..errors import CircuitOpenError, ProviderUnavailableError, RepoPilotError
from ..models import ModelResponse
from .base import ModelProvider, ModelRequest, ProviderHealth
from .telemetry import (
    ProviderCallContext,
    ProviderEvent,
    emit_provider_event,
    provider_call_context,
)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0
    jitter_ratio: float = 0.1

    def delay(self, attempt: int) -> float:
        exponent = max(attempt - 1, 0)
        raw = float(min(self.max_delay_seconds, self.base_delay_seconds * pow(2.0, exponent)))
        jitter = raw * self.jitter_ratio * float(random.random())
        return float(raw + jitter)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        recovery_seconds: float,
        clock: Clock = monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_call_active = False

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow_call(self) -> bool:
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            assert self._opened_at is not None
            if self._clock() - self._opened_at < self.recovery_seconds:
                return False
            self._state = CircuitState.HALF_OPEN
        if self._half_open_call_active:
            return False
        self._half_open_call_active = True
        return True

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = None
        self._half_open_call_active = False

    def record_failure(self) -> None:
        self._half_open_call_active = False
        self._failures += 1
        if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()

    def record_cancelled(self) -> None:
        """Release an in-flight probe without treating cancellation as success.

        A cancelled half-open probe is inconclusive.  Keep the circuit in
        ``HALF_OPEN`` so a later call can probe again, but always clear the
        single-probe permit; otherwise one user cancellation permanently
        bricks the circuit.
        """

        self._half_open_call_active = False


class ResilientProvider:
    name = "resilient"

    def __init__(
        self,
        provider: ModelProvider,
        *,
        retry_policy: RetryPolicy,
        circuit_breaker: CircuitBreaker,
        fallback: ModelProvider | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.retry_policy = retry_policy
        self.circuit_breaker = circuit_breaker
        self.fallback = fallback
        self._sleep = sleep

    async def complete(self, request: ModelRequest) -> ModelResponse:
        call_id = uuid4().hex
        started_at = monotonic()
        if not self.circuit_breaker.allow_call():
            await self._emit_circuit_event(
                "started",
                request=request,
                call_id=call_id,
                started_at=started_at,
                metadata={
                    "attempt": 0,
                    "max_attempts": self.retry_policy.max_attempts,
                    "circuit_open": True,
                    "fallback_available": self.fallback is not None,
                },
            )
            return await self._fallback_or_raise(
                request,
                CircuitOpenError(),
                call_id=call_id,
                started_at=started_at,
                attempt=0,
                circuit_open=True,
            )

        last_error: RepoPilotError | None = None
        try:
            for attempt in range(1, self.retry_policy.max_attempts + 1):
                try:
                    context = ProviderCallContext(call_id, attempt, self.retry_policy.max_attempts)
                    with provider_call_context(context):
                        response = await self.provider.complete(request)
                except RepoPilotError as exc:
                    last_error = exc
                    if not exc.retryable:
                        # Authentication, configuration and protocol rejections do not
                        # indicate temporary provider unavailability and must not poison
                        # the availability circuit or activate long-lived fallback.
                        self.circuit_breaker.record_success()
                        raise
                    if attempt < self.retry_policy.max_attempts:
                        delay = self.retry_policy.delay(attempt)
                        await emit_provider_event(
                            ProviderEvent(
                                phase="retry",
                                call_id=call_id,
                                provider=self.provider.name,
                                model=getattr(self.provider, "model", None),
                                purpose=request.purpose,
                                elapsed_ms=(monotonic() - started_at) * 1000,
                                metadata={
                                    "attempt": attempt,
                                    "max_attempts": self.retry_policy.max_attempts,
                                    "will_retry": True,
                                    "delay_ms": delay * 1000,
                                    "error_code": exc.code,
                                },
                            )
                        )
                        try:
                            await self._sleep(delay)
                        except asyncio.CancelledError:
                            # No concrete provider call is active during backoff,
                            # so the wrapper owns the terminal lifecycle event.
                            self.circuit_breaker.record_cancelled()
                            await self._emit_circuit_event(
                                "cancelled",
                                request=request,
                                call_id=call_id,
                                started_at=started_at,
                                metadata={
                                    "attempt": attempt,
                                    "max_attempts": self.retry_policy.max_attempts,
                                    "circuit_open": False,
                                    "fallback_used": False,
                                    "during_retry": True,
                                },
                            )
                            raise
                        continue
                    self.circuit_breaker.record_failure()
                    return await self._fallback_or_raise(
                        request,
                        exc,
                        call_id=call_id,
                        started_at=started_at,
                        attempt=attempt,
                    )
                else:
                    self.circuit_breaker.record_success()
                    return response
        except asyncio.CancelledError:
            # Concrete providers such as OpenAICompatibleProvider emit their
            # own cancelled event.  This cleanup is deliberately independent
            # of telemetry so a cancellation can never strand a half-open
            # permit.  Backoff cancellation above already emitted the wrapper
            # terminal event and is idempotent here.
            self.circuit_breaker.record_cancelled()
            raise

        assert last_error is not None
        raise last_error

    async def health(self) -> ProviderHealth:
        health = await self.provider.health()
        detail = f"circuit={self.circuit_breaker.state}"
        if health.detail:
            detail = f"{health.detail}; {detail}"
        return ProviderHealth(health.available, health.provider, health.model, detail)

    async def close(self) -> None:
        await self.provider.close()
        if self.fallback is not None:
            await self.fallback.close()

    async def _fallback_or_raise(
        self,
        request: ModelRequest,
        error: RepoPilotError,
        *,
        call_id: str,
        started_at: float,
        attempt: int,
        circuit_open: bool = False,
    ) -> ModelResponse:
        if self.fallback is None:
            if circuit_open:
                await self._emit_circuit_event(
                    "failed",
                    request=request,
                    call_id=call_id,
                    started_at=started_at,
                    metadata={
                        "attempt": attempt,
                        "max_attempts": self.retry_policy.max_attempts,
                        "circuit_open": True,
                        "fallback_used": False,
                        "error_code": error.code,
                    },
                )
            raise error
        try:
            response = await self.fallback.complete(request)
        except asyncio.CancelledError:
            await self._emit_circuit_event(
                "cancelled",
                request=request,
                call_id=call_id,
                started_at=started_at,
                metadata={
                    "attempt": attempt,
                    "max_attempts": self.retry_policy.max_attempts,
                    "circuit_open": circuit_open,
                    "fallback_used": True,
                },
            )
            raise
        except RepoPilotError as exc:
            await self._emit_circuit_event(
                "failed",
                request=request,
                call_id=call_id,
                started_at=started_at,
                metadata={
                    "attempt": attempt,
                    "max_attempts": self.retry_policy.max_attempts,
                    "circuit_open": circuit_open,
                    "fallback_used": True,
                    "error_code": exc.code,
                },
            )
            raise
        except Exception:
            await self._emit_circuit_event(
                "failed",
                request=request,
                call_id=call_id,
                started_at=started_at,
                metadata={
                    "attempt": attempt,
                    "max_attempts": self.retry_policy.max_attempts,
                    "circuit_open": circuit_open,
                    "fallback_used": True,
                    "error_code": "fallback_failed",
                },
            )
            raise ProviderUnavailableError(details={"stage": "fallback"}) from None
        result = replace(response, fallback_used=True)
        usage = result.usage
        await self._emit_circuit_event(
            "completed",
            request=request,
            call_id=call_id,
            started_at=started_at,
            model=result.model or getattr(self.provider, "model", None),
            metadata={
                "attempt": attempt,
                "max_attempts": self.retry_policy.max_attempts,
                "circuit_open": circuit_open,
                "fallback_used": True,
                "fallback_reason": error.code,
                "usage_reported": usage is not None,
                "prompt_tokens": usage.prompt_tokens if usage is not None else None,
                "completion_tokens": usage.completion_tokens if usage is not None else None,
                "total_tokens": usage.total_tokens if usage is not None else None,
            },
        )
        return result

    async def _emit_circuit_event(
        self,
        phase: Literal["started", "completed", "failed", "cancelled"],
        *,
        request: ModelRequest,
        call_id: str,
        started_at: float,
        metadata: dict[str, str | int | float | bool | None],
        model: str | None = None,
    ) -> None:
        await emit_provider_event(
            ProviderEvent(
                phase=phase,
                call_id=call_id,
                provider=self.name,
                model=model or getattr(self.provider, "model", None),
                purpose=request.purpose,
                elapsed_ms=(monotonic() - started_at) * 1000,
                metadata=metadata,
            )
        )
