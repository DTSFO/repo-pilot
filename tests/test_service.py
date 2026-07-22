from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from repopilot.config import Settings
from repopilot.models import AgentRunResult, ModelResponse, ToolCall, TraceEvent
from repopilot.providers.deterministic import DeterministicProvider
from repopilot.providers.telemetry import ProviderEvent, emit_provider_event
from repopilot.runtime import AsyncAgentRuntime
from repopilot.service import TaskService, TaskStateError, _safe_provider_event_payload
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


async def test_resume_rejects_guarded_workflow_that_already_reached_end(
    database: Database,
) -> None:
    service = TaskService(TaskStore(database), make_runtime(DeterministicProvider()))
    record = await service.store.create_task("already terminal")
    await service.store.update_task(record.id, status="guarded")
    await service.store.save_checkpoint(
        record.id,
        "final",
        {
            "messages": [
                {
                    "role": "system",
                    "content": "RepoPilot LangGraph workflow checkpoint",
                    "_repopilot_state": {
                        "next_node": "end",
                        "status": "guarded",
                    },
                }
            ]
        },
    )

    with pytest.raises(TaskStateError):
        await service.resume_task(record.id)


async def test_concurrent_resume_claims_one_runner_atomically(database: Database) -> None:
    class BlockingRunner:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, goal: str, **kwargs: object) -> AgentRunResult:
            del kwargs
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return AgentRunResult(
                "done",
                ({"role": "user", "content": goal},),
                (),
                1,
                "completed",
            )

    runner = BlockingRunner()
    service = TaskService(TaskStore(database), runner)
    record = await service.store.create_task("resume exactly once in-process")
    await service.store.update_task(record.id, status="failed")

    results = await asyncio.gather(
        service.resume_task(record.id),
        service.resume_task(record.id),
        return_exceptions=True,
    )
    await runner.started.wait()

    assert runner.calls == 1
    assert sum(isinstance(result, TaskStateError) for result in results) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1
    events = await service.store.list_events(record.id)
    assert [event.event_type for event in events].count("task.resumed") == 1

    runner.release.set()
    await service.wait_for_task(record.id)
    assert (await service.get_task(record.id)).status == "completed"


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


async def test_unexpected_runner_exception_persists_failed_terminal_state(
    database: Database,
) -> None:
    class BrokenRunner:
        async def run(self, goal: str, **kwargs: object) -> AgentRunResult:
            del goal, kwargs
            raise RuntimeError("unsafe upstream detail")

    service = TaskService(TaskStore(database), BrokenRunner())
    record = await service.create_task("must terminate")
    await service.wait_for_task(record.id)

    final = await service.get_task(record.id)
    events = await service.store.list_events(record.id)
    assert final.status == "failed"
    assert final.error_code == "internal_error"
    assert events[-1].event_type == "task.failed"
    assert events[-1].payload_json == {"error_code": "internal_error"}
    assert service.is_running(record.id) is False


async def test_terminal_status_is_published_after_terminal_event(database: Database) -> None:
    class ImmediateRunner:
        async def run(self, goal: str, **kwargs: object) -> AgentRunResult:
            del kwargs
            return AgentRunResult(
                "done",
                ({"role": "user", "content": goal},),
                (),
                1,
                "completed",
            )

    store = TaskStore(database)
    terminal_append_started = asyncio.Event()
    release_terminal_append = asyncio.Event()
    original_append = store.append_event

    async def append_event(
        task_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        if event_type == "task.completed":
            terminal_append_started.set()
            await release_terminal_append.wait()
        return await original_append(task_id, event_type, payload)

    store.append_event = append_event  # type: ignore[method-assign]
    service = TaskService(store, ImmediateRunner())
    record = await service.create_task("publish terminal state last")
    await terminal_append_started.wait()

    assert (await service.get_task(record.id)).status == "running"

    release_terminal_append.set()
    await service.wait_for_task(record.id)
    assert (await service.get_task(record.id)).status == "completed"


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


async def test_provider_lifecycle_events_are_persisted_during_task(database: Database) -> None:
    class TelemetryRunner:
        async def run(self, goal: str, **kwargs: object) -> AgentRunResult:
            del kwargs
            common = {
                "call_id": "call-safe-1",
                "provider": "openai_compatible",
                "model": "served-model",
                "purpose": "planner",
            }
            await emit_provider_event(ProviderEvent("started", elapsed_ms=0.0, **common))
            await emit_provider_event(
                ProviderEvent(
                    "first_byte",
                    elapsed_ms=1250.0,
                    metadata={"content": "must-not-persist", "api_key": "must-not-persist"},
                    **common,
                )
            )
            await emit_provider_event(
                ProviderEvent(
                    "completed",
                    elapsed_ms=1500.0,
                    metadata={"total_tokens": 42},
                    **common,
                )
            )
            messages = ({"role": "user", "content": goal},)
            return AgentRunResult("done", messages, (), 1, "completed", total_tokens=42)

    service = TaskService(TaskStore(database), TelemetryRunner())
    record = await service.create_task("inspect telemetry")
    await service.wait_for_task(record.id)

    events = await service.store.list_events(record.id)
    provider_events = [
        event for event in events if event.event_type.startswith("provider.request.")
    ]
    assert [event.event_type for event in provider_events] == [
        "provider.request.started",
        "provider.request.first_byte",
        "provider.request.completed",
    ]
    assert provider_events[1].payload_json == {
        "call_id": "call-safe-1",
        "provider": "openai_compatible",
        "configured_model": "served-model",
        "purpose": "planner",
        "elapsed_ms": 1250.0,
    }
    assert "content" not in str(provider_events)


def test_provider_event_payload_rejects_wrong_types_bounds_and_secret_shaped_text() -> None:
    payload = _safe_provider_event_payload(
        ProviderEvent(
            "completed",
            call_id="github_pat_must_not_persist_abcdefghijklmnopqrstuvwxyz",
            provider="https://provider.invalid/key=secret",
            model="sk-must-not-persist-abcdefghijklmnopqrstuvwxyz0123456789",
            purpose="planner",
            elapsed_ms=float("inf"),
            metadata={
                "attempt": True,
                "max_attempts": 99,
                "streaming": "true",
                "state": "receiving<script>",
                "delta_count": -1,
                "bytes_received": 1_073_741_825,
                "finish_reason": "stop",
                "tool_call_count": 2,
                "usage_reported": True,
                "usage_estimated": False,
                "prompt_tokens": 42,
                "completion_tokens": False,
                "total_tokens": 10_000_001,
                "fallback_used": False,
                "fallback_reason": "api_key=must-not-persist",
                "error_code": "provider_timeout",
                "will_retry": True,
                "delay_ms": 12.5,
                "response_model": "Bearer must-not-persist",
                "timeout_kind": "read",
                "content": "must-not-persist",
            },
        )
    )

    assert payload == {
        "purpose": "planner",
        "finish_reason": "stop",
        "tool_call_count": 2,
        "usage_reported": True,
        "usage_estimated": False,
        "prompt_tokens": 42,
        "fallback_used": False,
        "error_code": "provider_timeout",
        "will_retry": True,
        "delay_ms": 12.5,
        "timeout_phase": "read",
    }
    serialized = str(payload)
    assert "github_pat" not in serialized
    assert "must-not-persist" not in serialized
    assert "api_key" not in serialized


async def test_provider_event_context_does_not_cross_concurrent_tasks(database: Database) -> None:
    class ConcurrentTelemetryRunner:
        async def run(self, goal: str, **kwargs: object) -> AgentRunResult:
            del kwargs
            event = ProviderEvent(
                "started",
                call_id=f"call-{goal}",
                provider="test-provider",
                model="test-model",
                purpose="researcher",
                elapsed_ms=0.0,
            )
            await emit_provider_event(event)
            await asyncio.sleep(0.01)
            await emit_provider_event(
                ProviderEvent(
                    "completed",
                    call_id=f"call-{goal}",
                    provider="test-provider",
                    model="test-model",
                    purpose="researcher",
                    elapsed_ms=10.0,
                )
            )
            return AgentRunResult(
                goal,
                ({"role": "user", "content": goal},),
                (),
                1,
                "completed",
            )

    service = TaskService(TaskStore(database), ConcurrentTelemetryRunner())
    first, second = await asyncio.gather(
        service.create_task("first"),
        service.create_task("second"),
    )
    await asyncio.gather(service.wait_for_task(first.id), service.wait_for_task(second.id))

    first_events, second_events = await asyncio.gather(
        service.store.list_events(first.id),
        service.store.list_events(second.id),
    )
    first_calls = {
        event.payload_json.get("call_id")
        for event in first_events
        if event.event_type.startswith("provider.request.")
    }
    second_calls = {
        event.payload_json.get("call_id")
        for event in second_events
        if event.event_type.startswith("provider.request.")
    }
    assert first_calls == {"call-first"}
    assert second_calls == {"call-second"}


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
