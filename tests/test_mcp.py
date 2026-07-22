from __future__ import annotations

import json
from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.mcp import McpServer
from repopilot.repository_manager import RepositoryManager
from repopilot.storage import Database


@pytest.fixture
async def server(tmp_path: Path) -> McpServer:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "auth.py").write_text(
        "def verify_token(token):\n    '''Bearer token verification.'''\n", encoding="utf-8"
    )
    settings = Settings.model_validate(
        {
            "provider": "deterministic",
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/mcp.db",
            "workspace_root": str(workspace),
            "allowed_repository_roots": str(tmp_path),
        }
    )
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(workspace))
    manager = RepositoryManager(database, settings)
    repository = await manager.ensure_default()
    await manager.refresh(repository.id)
    yield McpServer(settings, database)
    await database.close()


async def test_initialize_and_list_tools(server: McpServer) -> None:
    init = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init is not None
    assert init["result"]["serverInfo"]["name"] == "repopilot"

    listing = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listing is not None
    names = {tool["name"] for tool in listing["result"]["tools"]}
    assert names == {"search_repository", "read_file", "get_task", "list_evidence"}
    read_file = next(tool for tool in listing["result"]["tools"] if tool["name"] == "read_file")
    assert "repository_id" in read_file["inputSchema"]["properties"]


async def test_search_tool_returns_citations(server: McpServer) -> None:
    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_repository",
                "arguments": {"query": "bearer token verification"},
            },
        }
    )
    assert response is not None
    hits = json.loads(response["result"]["content"][0]["text"])
    assert hits
    assert hits[0]["source_uri"] == "auth.py"
    assert ":L" in hits[0]["citation"]


async def test_read_file_rejects_traversal(server: McpServer) -> None:
    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "../secret.txt"}},
        }
    )
    assert response is not None
    assert response["result"]["isError"] is True


async def test_read_file_uses_indexed_revision_not_mutated_worktree(
    server: McpServer,
) -> None:
    workspace = server.settings.resolved_workspace_root
    (workspace / "auth.py").write_text("changed after indexing", encoding="utf-8")

    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "auth.py"}},
        }
    )
    assert response is not None
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "Bearer token verification" in payload["content"]
    assert "changed after indexing" not in payload["content"]


async def test_multiple_repositories_require_explicit_scope(server: McpServer) -> None:
    other = server.settings.resolved_workspace_root.parent / "other"
    other.mkdir()
    (other / "README.md").write_text("OTHER UNIQUE CONTENT", encoding="utf-8")
    manager = RepositoryManager(server.database, server.settings)
    repository = await manager.add_local(str(other))
    await manager.refresh(repository.id)

    ambiguous = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "search_repository",
                "arguments": {"query": "OTHER UNIQUE CONTENT"},
            },
        }
    )
    assert ambiguous is not None
    assert ambiguous["result"]["isError"] is True
    error = json.loads(ambiguous["result"]["content"][0]["text"])
    assert error["error"]["code"] == "repository_request_rejected"

    scoped = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "search_repository",
                "arguments": {
                    "query": "OTHER UNIQUE CONTENT",
                    "repository_id": repository.id,
                },
            },
        }
    )
    assert scoped is not None
    hits = json.loads(scoped["result"]["content"][0]["text"])
    assert hits
    assert {hit["repository_id"] for hit in hits} == {repository.id}


async def test_unknown_method_returns_error(server: McpServer) -> None:
    response = await server.handle({"jsonrpc": "2.0", "id": 5, "method": "bogus"})
    assert response is not None
    assert response["error"]["code"] == -32601
