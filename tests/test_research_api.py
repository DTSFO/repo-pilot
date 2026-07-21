from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from repopilot.api import create_app
from repopilot.config import Settings


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "runtime.md").write_text(
        "# Runtime\n\nThe async runtime executes read-only tools concurrently\n"
        "and retries transient failures with exponential backoff.\n",
        encoding="utf-8",
    )
    (root / "unrelated.md").write_text(
        "# Cooking\n\nBoil water and add noodles for dinner tonight.\n", encoding="utf-8"
    )
    return root


@pytest.fixture
async def client(tmp_path: Path, workspace: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings.model_validate(
        {
            "provider": "deterministic",
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/research-test.db",
            "workspace_root": str(workspace),
        }
    )
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def wait_terminal(client: httpx.AsyncClient, task_id: str) -> dict[str, object]:
    for _ in range(100):
        body = (await client.get(f"/api/tasks/{task_id}")).json()
        if body["status"] in {"completed", "guarded", "failed", "cancelled"}:
            return dict(body)
        await asyncio.sleep(0.02)
    raise AssertionError("task never reached a terminal status")


async def test_ingest_reports_document_counts(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/ingest", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["ingested_documents"] == 2
    assert body["chunks"] >= 2


async def test_ingest_rejects_traversal(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/ingest", json={"path": "../../etc"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ingestion_path_rejected"


async def test_search_returns_cited_hits(client: httpx.AsyncClient) -> None:
    await client.post("/api/ingest", json={})
    response = await client.get("/api/search", params={"q": "concurrent runtime retries"})
    assert response.status_code == 200
    hits = response.json()
    assert hits
    assert hits[0]["source_uri"] == "runtime.md"
    assert ":L" in hits[0]["citation"]


async def test_research_task_produces_cited_report_and_evidence(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/ingest", json={})
    task_id = (
        await client.post("/api/tasks", json={"goal": "runtime concurrency and retries"})
    ).json()["id"]

    body = await wait_terminal(client, task_id)
    assert body["status"] == "completed"
    report = str(body["final_report"])
    assert "runtime.md:L" in report
    assert "证据与发现" in report

    evidence = (await client.get(f"/api/tasks/{task_id}/evidence")).json()
    assert evidence
    accepted = [item for item in evidence if item["review_status"] == "accepted"]
    assert accepted
    assert all(":L" in item["citation"] for item in accepted)


async def test_unrelated_goal_refuses_unsupported_conclusions(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/ingest", json={})
    task_id = (await client.post("/api/tasks", json={"goal": "量子引力常数推导"})).json()["id"]

    body = await wait_terminal(client, task_id)
    assert body["status"] == "completed"
    report = str(body["final_report"])
    assert "不做推断" in report or "无法给出有依据的结论" in report
