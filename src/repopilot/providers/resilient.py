from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic

from ..errors import CircuitOpenError, RepoPilotError
from ..models import ModelResponse
from .base import ModelProvider, ModelRequest, ProviderHealth

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
        if not self.circuit_breaker.allow_call():
            return await self._fallback_or_raise(request, CircuitOpenError())

        last_error: RepoPilotError | None = None
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                response = await self.provider.complete(request)
            except RepoPilotError as exc:
                last_error = exc
                if not exc.retryable:
                    self.circuit_breaker.record_failure()
                    raise
                if attempt < self.retry_policy.max_attempts:
                    await self._sleep(self.retry_policy.delay(attempt))
                    continue
                self.circuit_breaker.record_failure()
                return await self._fallback_or_raise(request, exc)
            else:
                self.circuit_breaker.record_success()
                return response

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
    ) -> ModelResponse:
        if self.fallback is None:
            raise error
        response = await self.fallback.complete(request)
        return replace(response, fallback_used=True)
