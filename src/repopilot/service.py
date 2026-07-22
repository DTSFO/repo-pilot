from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from dataclasses import asdict
from http import HTTPStatus
from typing import Any, Literal, Protocol

from .errors import RepoPilotError
from .models import AgentRunResult, TraceEvent
from .observability import (
    PROVIDER_EVENTS,
    PROVIDER_REQUEST_LATENCY,
    PROVIDER_TIME_TO_FIRST_BYTE,
    TASKS_CREATED,
    TASKS_FINISHED,
    log_exception_safely,
)
from .providers.telemetry import ProviderEvent, provider_event_sink
from .runtime import StepCallback
from .storage.models import ResearchTaskRecord
from .storage.repositories import TaskStore

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"completed", "guarded", "failed", "cancelled"})
MAX_CHECKPOINT_NODE_LENGTH = 32
SAFE_PROVIDER_PURPOSES = frozenset({"planner", "researcher", "reviewer", "writer"})
SAFE_PROVIDER_PROGRESS_STATES = frozenset({"waiting_first_byte", "receiving"})
SAFE_PROVIDER_TIMEOUT_PHASES = frozenset({"connect", "read", "write", "pool", "unknown"})
SAFE_PROVIDER_BOOL_FIELDS = frozenset(
    {
        "streaming",
        "usage_reported",
        "usage_estimated",
        "fallback_used",
        "will_retry",
        "circuit_open",
        "fallback_available",
        "during_retry",
    }
)
SAFE_PROVIDER_INT_RANGES = {
    "attempt": (0, 8),
    "max_attempts": (1, 8),
    "delta_count": (0, 1_000_000),
    "bytes_received": (0, 1_073_741_824),
    "tool_call_count": (0, 1_000),
    "prompt_tokens": (0, 10_000_000),
    "completion_tokens": (0, 10_000_000),
    "total_tokens": (0, 10_000_000),
}
SAFE_PROVIDER_CODE_FIELDS = frozenset({"finish_reason", "fallback_reason", "error_code"})
SAFE_PROVIDER_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]*$")
SENSITIVE_PROVIDER_VALUE_RE = re.compile(
    r"(?:bearer\s+|(?:api[-_]?key|authorization|password|secret)[=:]"
    r"|(?:sk|pk|ghp|github_pat|xox[baprs]?|AIza)[-_]?[A-Za-z0-9])",
    re.IGNORECASE,
)
OPAQUE_PROVIDER_VALUE_RE = re.compile(r"^[A-Za-z0-9_+=/\-]{48,}$")


def _checkpoint_node(step: int, fresh_trace: list[TraceEvent]) -> str:
    """Use the latest workflow node while retaining generic-runtime compatibility."""
    for event in reversed(fresh_trace):
        node = event.metadata.get("node")
        if isinstance(node, str) and node and len(node) <= MAX_CHECKPOINT_NODE_LENGTH:
            return node
    return f"step-{step}"


def _safe_provider_text(
    value: object,
    *,
    max_length: int,
    allowed_values: frozenset[str] | None = None,
) -> str | None:
    """Accept short identifier-like values while rejecting credential-shaped content."""

    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if allowed_values is not None and value not in allowed_values:
        return None
    if not SAFE_PROVIDER_IDENTIFIER_RE.fullmatch(value):
        return None
    if "://" in value or SENSITIVE_PROVIDER_VALUE_RE.search(value):
        return None
    if OPAQUE_PROVIDER_VALUE_RE.fullmatch(value):
        return None
    return value


def _safe_provider_int(value: object, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _safe_provider_float(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        return None
    return round(result, 3)


def _safe_provider_event_payload(event: ProviderEvent) -> dict[str, Any]:
    """Flatten only typed, bounded, content-free metadata into the durable event log."""

    payload: dict[str, Any] = {}
    base_text_fields = (
        ("call_id", event.call_id, 40, None),
        ("provider", event.provider, 48, None),
        ("configured_model", event.model, 96, None),
        ("purpose", event.purpose, 24, SAFE_PROVIDER_PURPOSES),
    )
    for key, base_value, max_length, allowed_values in base_text_fields:
        safe_value = _safe_provider_text(
            base_value,
            max_length=max_length,
            allowed_values=allowed_values,
        )
        if safe_value is not None:
            payload[key] = safe_value

    elapsed_ms = _safe_provider_float(event.elapsed_ms, minimum=0, maximum=86_400_000)
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms

    for key, metadata_value in event.metadata.items():
        if key in SAFE_PROVIDER_BOOL_FIELDS and type(metadata_value) is bool:
            payload[key] = metadata_value
        elif key in SAFE_PROVIDER_INT_RANGES:
            minimum, maximum = SAFE_PROVIDER_INT_RANGES[key]
            safe_int = _safe_provider_int(metadata_value, minimum=minimum, maximum=maximum)
            if safe_int is not None:
                payload[key] = safe_int
        elif key == "delay_ms":
            safe_delay = _safe_provider_float(metadata_value, minimum=0, maximum=60_000)
            if safe_delay is not None:
                payload[key] = safe_delay
        elif key == "state":
            safe_state = _safe_provider_text(
                metadata_value,
                max_length=24,
                allowed_values=SAFE_PROVIDER_PROGRESS_STATES,
            )
            if safe_state is not None:
                payload[key] = safe_state
        elif key in SAFE_PROVIDER_CODE_FIELDS:
            safe_code = _safe_provider_text(metadata_value, max_length=64)
            if safe_code is not None:
                payload[key] = safe_code
        elif key == "response_model":
            safe_model = _safe_provider_text(metadata_value, max_length=96)
            if safe_model is not None:
                payload["served_model"] = safe_model
        elif key == "timeout_kind":
            safe_timeout = _safe_provider_text(
                metadata_value,
                max_length=16,
                allowed_values=SAFE_PROVIDER_TIMEOUT_PHASES,
            )
            if safe_timeout is not None:
                payload["timeout_phase"] = safe_timeout
    return payload


class TaskNotFoundError(RepoPilotError):
    code = "task_not_found"
    safe_message = "The requested task does not exist."
    http_status = HTTPStatus.NOT_FOUND


class TaskStateError(RepoPilotError):
    code = "task_state_conflict"
    safe_message = "The task is not in a state that allows this operation."
    http_status = HTTPStatus.CONFLICT


class TaskRunner(Protocol):
    """Anything that can execute a goal: the agent runtime or a workflow."""

    async def run(
        self,
        goal: str,
        *,
        initial_messages: list[dict[str, Any]] | None = None,
        on_step: StepCallback | None = None,
        task_id: str | None = None,
        repository_id: str | None = None,
        revision_id: str | None = None,
    ) -> AgentRunResult: ...


class TaskService:
    """Durable task orchestration on top of the async agent runtime.

    Every step is persisted as an append-only event stream plus a checkpoint,
    so interrupted tasks can resume from the last durable state and SSE
    consumers can replay history before following live progress.
    """

    def __init__(self, store: TaskStore, runtime: TaskRunner) -> None:
        self.store = store
        self.runtime = runtime
        self._running: dict[str, asyncio.Task[None]] = {}
        self._shutting_down = False
        # Lifecycle decisions must be serialized per task.  In particular, a
        # resume request performs several awaited reads/writes before it can
        # publish the runner in ``_running``; without this lock two concurrent
        # requests can both pass the stale ``_running`` check and start the
        # same workflow twice.
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}

    def _lifecycle_lock(self, task_id: str) -> asyncio.Lock:
        """Return the process-local lifecycle lock for one task."""

        return self._lifecycle_locks.setdefault(task_id, asyncio.Lock())

    async def create_task(
        self,
        goal: str,
        *,
        repository_id: str | None = None,
        revision_id: str | None = None,
    ) -> ResearchTaskRecord:
        record = await self.store.create_task(
            goal, repository_id=repository_id, revision_id=revision_id
        )
        TASKS_CREATED.inc()
        self._spawn(
            record.id,
            goal,
            initial_messages=None,
            repository_id=repository_id,
            revision_id=revision_id,
        )
        return record

    async def get_task(self, task_id: str) -> ResearchTaskRecord:
        record = await self.store.get_task(task_id)
        if record is None:
            raise TaskNotFoundError(details={"task_id": task_id})
        return record

    async def list_tasks(
        self, *, limit: int = 50, repository_id: str | None = None
    ) -> list[ResearchTaskRecord]:
        return await self.store.list_tasks(limit=limit, repository_id=repository_id)

    async def resume_task(self, task_id: str) -> ResearchTaskRecord:
        async with self._lifecycle_lock(task_id):
            # Re-read all state while holding the same lock used by cancel.
            # This makes the check-and-publish operation atomic within the
            # single RepoPilot process promised by the persistence model.
            record = await self.get_task(task_id)
            if task_id in self._running and not self._running[task_id].done():
                raise TaskStateError(details={"task_id": task_id, "status": record.status})
            if record.status == "completed":
                raise TaskStateError(details={"task_id": task_id, "status": record.status})
            checkpoint = await self.store.latest_checkpoint(task_id)
            if record.status == "guarded" and self._checkpoint_reached_workflow_end(checkpoint):
                raise TaskStateError(details={"task_id": task_id, "status": record.status})
            initial_messages = checkpoint.state_json.get("messages") if checkpoint else None
            await self.store.append_event(
                task_id,
                "task.resumed",
                {"from_version": checkpoint.version if checkpoint else 0},
            )
            self._spawn(
                task_id,
                record.goal,
                initial_messages=initial_messages,
                repository_id=record.repository_id,
                revision_id=record.revision_id,
            )
            return await self.get_task(task_id)

    async def cancel_task(self, task_id: str) -> ResearchTaskRecord:
        async with self._lifecycle_lock(task_id):
            record = await self.get_task(task_id)
            running = self._running.get(task_id)
            if running is None or running.done():
                raise TaskStateError(details={"task_id": task_id, "status": record.status})
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
            record = await self.get_task(task_id)
            if record.status not in TERMINAL_STATUSES:
                # The coroutine was cancelled before its own handler could persist state.
                await self.store.update_task(task_id, status="cancelled")
                await self.store.append_event(task_id, "task.cancelled", {"before_start": True})
                record = await self.get_task(task_id)
            return record

    async def wait_for_task(self, task_id: str) -> None:
        running = self._running.get(task_id)
        if running is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await running

    def is_running(self, task_id: str) -> bool:
        running = self._running.get(task_id)
        return running is not None and not running.done()

    @staticmethod
    def _checkpoint_reached_workflow_end(checkpoint: Any) -> bool:
        if checkpoint is None:
            return False
        messages = checkpoint.state_json.get("messages")
        if not isinstance(messages, list):
            return False
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            state = message.get("_repopilot_state")
            if isinstance(state, dict):
                return state.get("next_node") == "end" and state.get("status") == "guarded"
        return False

    async def shutdown(self) -> None:
        self._shutting_down = True
        # Cancel and drain one task at a time.  Concurrent cancellation handlers
        # can otherwise contend on SQLite while persisting terminal state, which
        # makes an otherwise graceful application shutdown wait for the driver's
        # busy timeout.
        for task in list(self._running.values()):
            if task.done():
                continue
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError):
                # A provider or database driver may not be interruptible while
                # it is unwinding.  Do not hold the ASGI lifespan hostage; the
                # task has already received cancellation and the event loop will
                # finish collecting it after shutdown.
                logger.warning(
                    "Task did not finish cancellation during shutdown",
                    extra={"error_code": "shutdown_task_timeout"},
                )

    def _spawn(
        self,
        task_id: str,
        goal: str,
        *,
        initial_messages: list[dict[str, Any]] | None,
        repository_id: str | None = None,
        revision_id: str | None = None,
    ) -> None:
        self._running[task_id] = asyncio.create_task(
            self._execute(task_id, goal, initial_messages, repository_id, revision_id),
            name=f"repopilot-task-{task_id}",
        )

    async def _execute(
        self,
        task_id: str,
        goal: str,
        initial_messages: list[dict[str, Any]] | None,
        repository_id: str | None,
        revision_id: str | None,
    ) -> None:
        async def on_step(
            step: int,
            messages: list[dict[str, Any]],
            fresh_trace: list[TraceEvent],
        ) -> None:
            for event in fresh_trace:
                await self.store.append_event(
                    task_id,
                    f"trace.{event.event}",
                    {"step": event.step, "detail": event.detail, "metadata": event.metadata},
                )
            await self.store.save_checkpoint(
                task_id,
                _checkpoint_node(step, fresh_trace),
                {"messages": messages},
            )

        async def on_provider_event(event: ProviderEvent) -> None:
            safe_payload = _safe_provider_event_payload(event)
            provider = str(safe_payload.get("provider", "invalid"))
            purpose = str(safe_payload.get("purpose", "unspecified"))
            elapsed_ms = safe_payload.get("elapsed_ms")
            PROVIDER_EVENTS.labels(provider, purpose, event.phase).inc()
            if event.phase == "first_byte" and isinstance(elapsed_ms, int | float):
                PROVIDER_TIME_TO_FIRST_BYTE.labels(provider, purpose).observe(elapsed_ms / 1000)
            elif event.phase in {"completed", "timeout", "failed", "cancelled"} and isinstance(
                elapsed_ms, int | float
            ):
                PROVIDER_REQUEST_LATENCY.labels(provider, purpose, event.phase).observe(
                    elapsed_ms / 1000
                )
            await self.store.append_event(
                task_id,
                f"provider.request.{event.phase}",
                safe_payload,
            )

        try:
            await self.store.update_task(task_id, status="running")
            await self.store.append_event(
                task_id, "task.started", {"resumed": bool(initial_messages)}
            )
            with provider_event_sink(on_provider_event):
                result = await self.runtime.run(
                    goal,
                    initial_messages=initial_messages,
                    on_step=on_step,
                    task_id=task_id,
                    repository_id=repository_id,
                    revision_id=revision_id,
                )
            await self._finalize(task_id, result)
        except asyncio.CancelledError:
            if not self._shutting_down:
                await self._persist_terminal_failure(task_id, "cancelled")
            raise
        except RepoPilotError as exc:
            log_exception_safely(
                logger,
                "Task failed",
                exc,
                extra={"error_code": exc.code},
            )
            await self._persist_terminal_failure(task_id, "failed", error_code=exc.code)
        except Exception as exc:
            logger.error(
                "Task failed with an unexpected exception type",
                extra={"error_code": "internal_error", "exception_type": type(exc).__name__},
            )
            await self._persist_terminal_failure(task_id, "failed", error_code="internal_error")
        finally:
            current = asyncio.current_task()
            if self._running.get(task_id) is current:
                self._running.pop(task_id, None)

    async def _persist_terminal_failure(
        self,
        task_id: str,
        status: Literal["failed", "cancelled"],
        *,
        error_code: str | None = None,
    ) -> None:
        changes: dict[str, Any] = {"status": status}
        if error_code is not None:
            changes["error_code"] = error_code
        try:
            await self.store.update_task(task_id, **changes)
        except Exception as exc:
            logger.error(
                "Could not persist task terminal status",
                extra={
                    "error_code": "terminal_persistence_failed",
                    "exception_type": type(exc).__name__,
                },
            )
            return
        payload = {"error_code": error_code} if error_code is not None else {}
        try:
            await self.store.append_event(task_id, f"task.{status}", payload)
        except Exception as exc:
            logger.error(
                "Could not append task terminal event",
                extra={"error_code": "terminal_event_failed", "exception_type": type(exc).__name__},
            )
        TASKS_FINISHED.labels(status=status).inc()

    async def _finalize(self, task_id: str, result: AgentRunResult) -> None:
        status = result.status if result.status in TERMINAL_STATUSES else "failed"
        await self.store.save_checkpoint(
            task_id,
            "final",
            {"messages": [dict(message) for message in result.messages]},
        )
        await self.store.append_event(
            task_id,
            f"task.{status}",
            {
                "steps": result.steps,
                "total_tokens": result.total_tokens,
                "degraded": result.degraded,
                "trace": [asdict(event) for event in result.trace],
            },
        )
        # Publish the terminal task status last. API pollers treat a terminal
        # status as permission to tear down the application; exposing it before
        # the final event was durable allowed lifespan shutdown to cancel a task
        # in the middle of finalization, producing slow teardown and conflicting
        # terminal state.
        await self.store.update_task(
            task_id,
            status=status,
            final_report=result.answer,
            degraded=result.degraded,
        )
        TASKS_FINISHED.labels(status=status).inc()
