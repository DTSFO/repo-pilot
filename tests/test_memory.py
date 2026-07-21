from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.ingestion import RepositoryIngestor
from repopilot.providers.deterministic import DeterministicProvider
from repopilot.storage import Database, DocumentStore, EvidenceStore, MemoryStore
from repopilot.workflow import ResearchWorkflow


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/memory-test.db")
    await database.initialize()
    yield database
    await database.close()


async def test_memory_round_trip_and_expiry(database: Database) -> None:
    memory = MemoryStore(database)
    await memory.add_memory(memory_type="note", content="keep", source="test", importance=0.9)
    await memory.add_memory(
        memory_type="note",
        content="expired",
        source="test",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    visible = await memory.list_memories()
    assert [item.content for item in visible] == ["keep"]

    removed = await memory.prune_expired()
    assert removed == 1


async def test_workflow_writes_and_recalls_memory(database: Database, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "cache.md").write_text(
        "# Cache\n\nThe cache invalidation strategy uses version stamps.\n", encoding="utf-8"
    )
    settings = Settings.model_validate(
        {
            "provider": "deterministic",
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/memory-test.db",
            "workspace_root": str(workspace),
        }
    )
    documents = DocumentStore(database)
    await RepositoryIngestor(documents, settings).ingest_path()
    memory = MemoryStore(database)
    workflow = ResearchWorkflow(
        DeterministicProvider(),
        documents,
        EvidenceStore(database),
        settings,
        memory=memory,
    )

    first = await workflow.run("cache invalidation version stamps", task_id="task-1")
    assert first.status == "completed"
    summaries = await memory.list_memories(memory_type="task_summary")
    assert len(summaries) == 1
    assert "cache.md" in summaries[0].content

    second = await workflow.run("cache invalidation strategy", task_id="task-2")
    recalled = [event for event in second.trace if "recalled" in event.detail.lower()]
    assert recalled
