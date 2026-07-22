from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="REPOPILOT_",
        extra="ignore",
        case_sensitive=False,
    )

    provider: Literal["deterministic", "openai_compatible"] = "deterministic"
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    # LLM_TIMEOUT_SECONDS remains a backwards-compatible aggregate override.
    # New deployments should set the phase-specific values when defaults are unsuitable.
    llm_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    llm_connect_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    llm_read_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    llm_write_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    llm_pool_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    llm_streaming_enabled: bool = True
    llm_stream_include_usage: bool = True
    llm_stream_progress_interval_seconds: float = Field(default=5.0, gt=0, le=60)
    llm_max_attempts: int = Field(default=3, ge=1, le=8)
    llm_retry_base_seconds: float = Field(default=0.25, ge=0, le=10)
    llm_retry_max_seconds: float = Field(default=4.0, ge=0, le=60)
    llm_circuit_failure_threshold: int = Field(default=5, ge=1, le=50)
    llm_circuit_recovery_seconds: float = Field(default=30.0, gt=0, le=3600)

    database_url: str = "sqlite+aiosqlite:///./data/repopilot.db"
    redis_url: str | None = None
    workspace_root: Path = Path(".")
    api_token: SecretStr | None = None
    max_steps: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=40, ge=1, le=1000)
    max_total_tokens: int = Field(default=100_000, ge=1, le=10_000_000)
    max_review_rounds: int = Field(default=2, ge=0, le=10)
    tool_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    tool_max_attempts: int = Field(default=2, ge=1, le=8)
    tool_retry_base_seconds: float = Field(default=0.1, ge=0, le=30)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=1024**3)
    allowed_fetch_hosts: str = ""
    sse_poll_seconds: float = Field(default=0.2, ge=0.01, le=5)
    sse_heartbeat_seconds: float = Field(default=15.0, ge=0.01, le=300)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> Self:
        if self.provider == "openai_compatible" and (
            not self.llm_base_url or not self.llm_model or self.llm_api_key is None
        ):
            raise ValueError("openai_compatible requires LLM_BASE_URL, LLM_MODEL, and LLM_API_KEY")
        if self.llm_retry_max_seconds < self.llm_retry_base_seconds:
            raise ValueError("LLM_RETRY_MAX_SECONDS must be >= LLM_RETRY_BASE_SECONDS")
        return self

    @property
    def resolved_workspace_root(self) -> Path:
        return self.workspace_root.expanduser().resolve()

    @property
    def fetch_host_allowlist(self) -> frozenset[str]:
        return frozenset(
            host.strip().lower() for host in self.allowed_fetch_hosts.split(",") if host.strip()
        )

    @property
    def resolved_llm_connect_timeout_seconds(self) -> float:
        return self.llm_connect_timeout_seconds or self.llm_timeout_seconds or 10.0

    @property
    def resolved_llm_read_timeout_seconds(self) -> float:
        return self.llm_read_timeout_seconds or self.llm_timeout_seconds or 120.0

    @property
    def resolved_llm_write_timeout_seconds(self) -> float:
        return self.llm_write_timeout_seconds or self.llm_timeout_seconds or 30.0

    @property
    def resolved_llm_pool_timeout_seconds(self) -> float:
        return self.llm_pool_timeout_seconds or self.llm_timeout_seconds or 10.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
