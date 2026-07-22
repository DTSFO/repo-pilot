from __future__ import annotations

import httpx

from ..config import Settings
from ..errors import ConfigurationError
from .base import ModelProvider
from .deterministic import DeterministicProvider
from .openai_compatible import OpenAICompatibleProvider
from .resilient import CircuitBreaker, ResilientProvider, RetryPolicy


def build_provider(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelProvider:
    if settings.provider == "deterministic":
        return DeterministicProvider()

    if not settings.llm_base_url or settings.llm_api_key is None or not settings.llm_model:
        raise ConfigurationError()

    primary = OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        connect_timeout_seconds=settings.resolved_llm_connect_timeout_seconds,
        read_timeout_seconds=settings.resolved_llm_read_timeout_seconds,
        write_timeout_seconds=settings.resolved_llm_write_timeout_seconds,
        pool_timeout_seconds=settings.resolved_llm_pool_timeout_seconds,
        streaming_enabled=settings.llm_streaming_enabled,
        stream_include_usage=settings.llm_stream_include_usage,
        stream_progress_interval_seconds=settings.llm_stream_progress_interval_seconds,
        transport=transport,
    )
    return ResilientProvider(
        primary,
        retry_policy=RetryPolicy(
            max_attempts=settings.llm_max_attempts,
            base_delay_seconds=settings.llm_retry_base_seconds,
            max_delay_seconds=settings.llm_retry_max_seconds,
        ),
        circuit_breaker=CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            recovery_seconds=settings.llm_circuit_recovery_seconds,
        ),
        fallback=DeterministicProvider(),
    )
