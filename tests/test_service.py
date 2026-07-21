from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from repopilot.config import Settings
from repopilot.models import AgentRunResult, ModelResponse, ToolCall, TraceEvent
from repopilot.providers.deterministic import DeterministicProvider
from repopilot.runtime import AsyncAgentRuntime
from repopilot.service import TaskService
from repopilot.storage import Database, TaskStore
from repopilot.storage.models import CheckpointRecord
from repopilot.tools import ToolRegistry, add, multiply


def make_runtime(provider: DeterministicProvider, **overrides: object) -> AsyncAgentRuntime:
    settings = Settings.model_validate(
        {"provider": "deterministic", "tool_retry_base_seconds": 0.0, **overrides}
    )
    tools = ToolRegistry()
    tools.register("add", "计算两个数字之和", add)
    tools.register("multiply", "计算两个数字之积", multiply)
    return AsyncAgentRuntime(provider, tools, settings)


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/service-test.db")
    await database.initialize()
    yield database
    await database.close()


async def test_checkpoint_resume_restores_conversation(database: Database) -> None:
    """A task interrupted after tool execution resumes from the checkpoint."""
    scripted = DeterministicProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="multiply", arguments={"a": 6, "b": 7}, call_id="c1"),)
            ),
        ]
    )
    service = TaskService(TaskStore(database), make_runtime(scripted, max_steps=1))

    record = await service.create_task("6*7")
    await service.wait_for_task(record.id)
    first = await service.get_task(record.id)
    assert first.status == "guarded"

    checkpoint = await service.store.latest_checkpoint(record.id)
    assert checkpoint is not None
    roles = [message["role"] for message in checkpoint.state_json["messages"]]
    assert roles[-1] == "tool"

    await service.resume_task(record.id)
    await service.wait_for_task(record.id)

    final = await service.get_task(record.id)
    assert final.status == "completed"

    events = await service.store.list_events(record.id)
    types = [event.event_type for event in events]
    assert "task.resumed" in types
    assert types.count("task.started") == 2


async def test_cancel_running_task_persists_cancelled_state(database: Database) -> None:
    class SlowProvider(DeterministicProvider):
        async def complete(self, request: object) -> ModelResponse:  # type: ignore[override]
            import asyncio

            await asyncio.sleep(30)
            raise AssertionError("should have been cancelled")

    service = TaskService(TaskStore(database), make_runtime(SlowProvider()))
    record = await service.create_task("slow goal")

    cancelled = await service.cancel_task(record.id)
    assert cancelled.status == "cancelled"

    events = await service.store.list_events(record.id)
    assert events[-1].event_type == "task.cancelled"


async def test_checkpoint_uses_latest_workflow_node_metadata(database: Database) -> None:
    class WorkflowRunner:
        async def run(self, goal: str, **kwargs: object) -> AgentRunResult:
            on_step = kwargs["on_step"]
            assert callable(on_step)
            messages = [
                {"role": "user", "content": goal},
                {
                    "role": "system",
                    "content": "RepoPilot workflow checkpoint",
                    "_repopilot_state": {"next_node": "researcher"},
                },
            ]
            trace = [
                TraceEvent(1, "workflow", "planned", {"node": "planner"}),
                TraceEvent(1, "checkpoint", "durable", {"node": "researcher"}),
            ]
            await on_step(1, messages, trace)
            return AgentRunResult(
                "guarded",
                tuple(messages),
                tuple(trace),
                1,
                "guarded",
            )

    service = TaskService(TaskStore(database), WorkflowRunner())
    record = await service.create_task("inspect repository")
    await service.wait_for_task(record.id)

    async with database.session() as session:
        checkpoints = list(
            await session.scalars(
                select(CheckpointRecord)
                .where(CheckpointRecord.task_id == record.id)
                .order_by(CheckpointRecord.version)
            )
        )

    assert [checkpoint.node for checkpoint in checkpoints] == ["researcher", "final"]
    assert checkpoints[0].state_json["messages"][-1]["_repopilot_state"] == {
        "next_node": "researcher"
    }


async def test_checkpoint_falls_back_to_step_name_for_generic_runtime(database: Database) -> None:
    scripted = DeterministicProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="multiply", arguments={"a": 2, "b": 3}, call_id="c1"),)
            )
        ]
    )
    service = TaskService(TaskStore(database), make_runtime(scripted, max_steps=1))

    record = await service.create_task("2*3")
    await service.wait_for_task(record.id)

    async with database.session() as session:
        checkpoints = list(
            await session.scalars(
                select(CheckpointRecord)
                .where(CheckpointRecord.task_id == record.id)
                .order_by(CheckpointRecord.version)
            )
        )

    assert checkpoints[0].node == "step-1"
