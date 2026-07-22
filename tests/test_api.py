from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

import repopilot.api as api_module
from repopilot.api import create_app
from repopilot.config import Settings
from repopilot.models import AgentRunResult
from repopilot.providers.telemetry import ProviderEvent, emit_provider_event


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "provider": "deterministic",
        "database_url": f"sqlite+aiosqlite:///{tmp_path}/api-test.db",
        "tool_retry_base_seconds": 0.0,
    }
    defaults.update(overrides)
    return Settings.model_validate(defaults)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(make_settings(tmp_path))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def wait_terminal(client: httpx.AsyncClient, task_id: str) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/api/tasks/{task_id}")
        body = response.json()
        if body["status"] in {"completed", "guarded", "failed", "cancelled"}:
            return dict(body)
        await asyncio.sleep(0.02)
    raise AssertionError("task never reached a terminal status")


async def test_health_and_ready(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).json() == {"status": "ready"}
    assert (await client.get("/favicon.ico")).status_code == 204


async def test_create_task_completes_offline(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/tasks", json={"goal": "总结这个仓库"})
    assert response.status_code == 202
    task_id = response.json()["id"]

    body = await wait_terminal(client, task_id)
    assert body["status"] == "completed"
    report = str(body["final_report"])
    assert "RepoPilot 研究报告" in report
    assert "尚未摄取任何仓库文档" in report
    assert body["degraded"] is True


async def test_events_are_persisted_in_order(client: httpx.AsyncClient) -> None:
    task_id = (await client.post("/api/tasks", json={"goal": "hello"})).json()["id"]
    await wait_terminal(client, task_id)

    events = (await client.get(f"/api/tasks/{task_id}/events")).json()
    sequences = [event["sequence"] for event in events]
    assert sequences == sorted(sequences)
    types = [event["event_type"] for event in events]
    assert types[0] == "task.started"
    assert types[-1] == "task.completed"


async def test_unknown_task_returns_stable_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/tasks/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"


async def test_sse_stream_replays_and_ends(client: httpx.AsyncClient) -> None:
    task_id = (await client.post("/api/tasks", json={"goal": "stream me"})).json()["id"]
    await wait_terminal(client, task_id)

    collected: list[str] = []
    async with client.stream("GET", f"/api/tasks/{task_id}/stream") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            collected.append(line)
            if line == "event: stream.end":
                break
    joined = "\n".join(collected)
    assert "event: task.started" in joined
    assert "event: task.completed" in joined


async def test_sse_last_event_id_replays_only_newer_events(
    client: httpx.AsyncClient,
) -> None:
    task_id = (await client.post("/api/tasks", json={"goal": "resume stream"})).json()["id"]
    await wait_terminal(client, task_id)
    events = (await client.get(f"/api/tasks/{task_id}/events")).json()
    assert len(events) >= 3
    cursor = int(events[len(events) // 2]["sequence"])

    ids: list[int] = []
    async with client.stream(
        "GET",
        f"/api/tasks/{task_id}/stream",
        headers={"Last-Event-ID": str(cursor)},
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                ids.append(int(line.removeprefix("id: ")))
            if line == "event: stream.end":
                break

    assert ids
    assert all(sequence > cursor for sequence in ids)
    assert ids == [int(event["sequence"]) for event in events if int(event["sequence"]) > cursor]


async def test_sse_emits_live_provider_events_and_transport_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowTelemetryWorkflow:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def run(self, goal: str, **kwargs: object) -> AgentRunResult:
            del kwargs
            common = {
                "call_id": "live-call",
                "provider": "test-provider",
                "model": "test-model",
                "purpose": "planner",
            }
            await emit_provider_event(ProviderEvent("started", elapsed_ms=0.0, **common))
            await asyncio.sleep(0.03)
            await emit_provider_event(
                ProviderEvent(
                    "progress",
                    elapsed_ms=30.0,
                    metadata={"state": "waiting_first_byte"},
                    **common,
                )
            )
            await asyncio.sleep(0.03)
            await emit_provider_event(ProviderEvent("completed", elapsed_ms=60.0, **common))
            return AgentRunResult(
                "done",
                ({"role": "user", "content": goal},),
                (),
                1,
                "completed",
            )

    monkeypatch.setattr(api_module, "ResearchWorkflow", SlowTelemetryWorkflow)
    app = create_app(make_settings(tmp_path, sse_poll_seconds=0.01, sse_heartbeat_seconds=0.01))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            task_id = (await http.post("/api/tasks", json={"goal": "observe live"})).json()["id"]
            lines: list[str] = []
            async with http.stream("GET", f"/api/tasks/{task_id}/stream") as response:
                assert response.headers["cache-control"] == "no-cache, no-transform"
                async for line in response.aiter_lines():
                    lines.append(line)
                    if line == "event: stream.end":
                        break
            persisted = (await http.get(f"/api/tasks/{task_id}/events")).json()

    assert "event: provider.request.started" in lines
    assert "event: provider.request.progress" in lines
    assert "event: provider.request.completed" in lines
    assert ": keep-alive" in lines
    assert all(event["event_type"] != "stream.heartbeat" for event in persisted)


async def test_api_token_is_enforced(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, api_token="secret-token"))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            denied = await http.post("/api/tasks", json={"goal": "x"})
            assert denied.status_code == 401
            assert denied.json()["error"]["code"] == "unauthorized"

            allowed = await http.post(
                "/api/tasks",
                json={"goal": "x"},
                headers={"Authorization": "Bearer secret-token"},
            )
            assert allowed.status_code == 202


async def test_bearer_header_authorizes_fetch_based_task_stream(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, api_token="secret-token"))
    authorization = {"Authorization": "Bearer secret-token"}
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            denied = await http.get("/api/tasks/missing/stream")
            assert denied.status_code == 401

            task_id = (
                await http.post(
                    "/api/tasks",
                    json={"goal": "authenticated stream"},
                    headers=authorization,
                )
            ).json()["id"]
            await app.state.service.wait_for_task(task_id)

            async with http.stream(
                "GET",
                f"/api/tasks/{task_id}/stream",
                headers={**authorization, "Accept": "text/event-stream"},
            ) as response:
                body = (await response.aread()).decode()

    assert response.status_code == 200
    assert "event: task.completed" in body
    assert "event: stream.end" in body


async def test_resume_rejected_for_completed_task(client: httpx.AsyncClient) -> None:
    task_id = (await client.post("/api/tasks", json={"goal": "done"})).json()["id"]
    await wait_terminal(client, task_id)

    response = await client.post(f"/api/tasks/{task_id}/resume")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "task_state_conflict"


async def test_metrics_and_index_page(client: httpx.AsyncClient) -> None:
    index = await client.get("/")
    assert index.status_code == 200
    assert "RepoPilot" in index.text

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "repopilot_http_requests_total" in metrics.text


async def test_index_uses_dom_safe_rendering_and_memory_only_bearer_stream(
    client: httpx.AsyncClient,
) -> None:
    html = (await client.get("/")).text

    assert "innerHTML" not in html
    assert "new EventSource" not in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "headers.set('Authorization'" in html
    assert "document.createElement('code')" in html
    assert "citation.textContent" in html
    assert "replaceChildren" in html
    assert "consumeTaskStream" in html


async def test_upload_document_ingests_and_searches(client: httpx.AsyncClient) -> None:
    upload = await client.post(
        "/api/documents",
        files={"file": ("notes.md", b"# Notes\n\nThe deploy pipeline uses blue-green rollout.\n")},
    )
    assert upload.status_code == 201
    assert upload.json()["ingested_documents"] == 1

    hits = (await client.get("/api/search", params={"q": "blue-green rollout deploy"})).json()
    assert hits
    assert hits[0]["source_uri"] == "uploads/notes.md"


async def test_oversized_upload_rejected(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, max_upload_bytes=1024))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/documents", files={"file": ("big.txt", b"x" * 2048)})
            assert response.status_code == 413
            assert response.json()["error"]["code"] == "document_too_large"
