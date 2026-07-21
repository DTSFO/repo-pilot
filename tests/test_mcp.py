from __future__ import annotations

import json
from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.ingestion import RepositoryIngestor
from repopilot.mcp import McpServer
from repopilot.storage import Database, DocumentStore


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
        }
    )
    database = Database(settings.database_url)
    await database.initialize()
    await RepositoryIngestor(DocumentStore(database), settings).ingest_path()
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


async def test_unknown_method_returns_error(server: McpServer) -> None:
    response = await server.handle({"jsonrpc": "2.0", "id": 5, "method": "bogus"})
    assert response is not None
    assert response["error"]["code"] == -32601
