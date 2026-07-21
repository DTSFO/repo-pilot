from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

_REDACTION_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def redact_text(value: object) -> str:
    text = str(value)
    for pattern in _REDACTION_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def log_exception_safely(
    logger: logging.Logger,
    message: str,
    exception: BaseException,
    *,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Log a detailed traceback only after removing common secret shapes."""
    formatted = "".join(
        traceback.format_exception(type(exception), exception, exception.__traceback__)
    ).rstrip()
    logger.error(
        "%s\n%s",
        redact_text(message),
        redact_text(formatted),
        extra=dict(extra or {}),
    )


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("request_id", "task_id", "run_id", "operation", "error_code"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


TASKS_CREATED = Counter("repopilot_tasks_created_total", "Research tasks created")
TASKS_FINISHED = Counter("repopilot_tasks_finished_total", "Research tasks finished", ["status"])
HTTP_REQUESTS = Counter(
    "repopilot_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "repopilot_http_request_seconds", "HTTP request latency", ["method", "path"]
)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
