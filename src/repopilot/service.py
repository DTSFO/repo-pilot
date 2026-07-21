from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from http import HTTPStatus
from typing import Any, Protocol

from .errors import RepoPilotError
from .models import AgentRunResult, TraceEvent
from .observability import TASKS_CREATED, TASKS_FINISHED, log_exception_safely
from .runtime import StepCallback
from .storage.models import ResearchTaskRecord
from .storage.repositories import TaskStore

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"completed", "guarded", "failed", "cancelled"})
MAX_CHECKPOINT_NODE_LENGTH = 32


def _checkpoint_node(step: int, fresh_trace: list[TraceEvent]) -> str:
    """Use the latest workflow node while retaining generic-runtime compatibility."""
    for event in reversed(fresh_trace):
        node = event.metadata.get("node")
        if isinstance(node, str) and node and len(node) <= MAX_CHECKPOINT_NODE_LENGTH:
            return node
    return f"step-{step}"


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

    async def create_task(self, goal: str) -> ResearchTaskRecord:
        record = await self.store.create_task(goal)
        TASKS_CREATED.inc()
        self._spawn(record.id, goal, initial_messages=None)
        return record

    async def get_task(self, task_id: str) -> ResearchTaskRecord:
        record = await self.store.get_task(task_id)
        if record is None:
            raise TaskNotFoundError(details={"task_id": task_id})
        return record

    async def list_tasks(self, *, limit: int = 50) -> list[ResearchTaskRecord]:
        return await self.store.list_tasks(limit=limit)

    async def resume_task(self, task_id: str) -> ResearchTaskRecord:
        record = await self.get_task(task_id)
        if task_id in self._running and not self._running[task_id].done():
            raise TaskStateError(details={"task_id": task_id, "status": record.status})
        if record.status == "completed":
            raise TaskStateError(details={"task_id": task_id, "status": record.status})
        checkpoint = await self.store.latest_checkpoint(task_id)
        initial_messages = checkpoint.state_json.get("messages") if checkpoint else None
        await self.store.append_event(
            task_id,
            "task.resumed",
            {"from_version": checkpoint.version if checkpoint else 0},
        )
        self._spawn(task_id, record.goal, initial_messages=initial_messages)
        return await self.get_task(task_id)

    async def cancel_task(self, task_id: str) -> ResearchTaskRecord:
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

    async def shutdown(self) -> None:
        for task in list(self._running.values()):
            if not task.done():
                task.cancel()
        for task in list(self._running.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _spawn(
        self,
        task_id: str,
        goal: str,
        *,
        initial_messages: list[dict[str, Any]] | None,
    ) -> None:
        self._running[task_id] = asyncio.create_task(
            self._execute(task_id, goal, initial_messages),
            name=f"repopilot-task-{task_id}",
        )

    async def _execute(
        self,
        task_id: str,
        goal: str,
        initial_messages: list[dict[str, Any]] | None,
    ) -> None:
        await self.store.update_task(task_id, status="running")
        await self.store.append_event(task_id, "task.started", {"resumed": bool(initial_messages)})

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

        try:
            result = await self.runtime.run(
                goal,
                initial_messages=initial_messages,
                on_step=on_step,
                task_id=task_id,
            )
        except asyncio.CancelledError:
            await self.store.update_task(task_id, status="cancelled")
            await self.store.append_event(task_id, "task.cancelled", {})
            TASKS_FINISHED.labels(status="cancelled").inc()
            raise
        except RepoPilotError as exc:
            log_exception_safely(
                logger,
                "Task failed",
                exc,
                extra={"error_code": exc.code},
            )
            await self.store.update_task(task_id, status="failed", error_code=exc.code)
            await self.store.append_event(task_id, "task.failed", {"error_code": exc.code})
            TASKS_FINISHED.labels(status="failed").inc()
            return

        await self._finalize(task_id, result)

    async def _finalize(self, task_id: str, result: AgentRunResult) -> None:
        status = result.status if result.status in TERMINAL_STATUSES else "failed"
        await self.store.save_checkpoint(
            task_id,
            "final",
            {"messages": [dict(message) for message in result.messages]},
        )
        await self.store.update_task(
            task_id,
            status=status,
            final_report=result.answer,
            degraded=result.degraded,
        )
        TASKS_FINISHED.labels(status=status).inc()
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
