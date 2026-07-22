from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .config import Settings
from .ingestion import RepositoryIngestor
from .retrieval import HybridRetriever
from .service import TaskNotFoundError
from .storage.database import Database
from .storage.repositories import DocumentStore, EvidenceStore, TaskStore

PROTOCOL_VERSION = "2024-11-05"
MAX_FILE_CHARS = 20_000

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_repository",
        "description": "BM25+semantic search over ingested repository chunks; returns citations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a file inside the configured workspace (path traversal rejected).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task",
        "description": "Get status and final report of a research task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_evidence",
        "description": "List reviewed evidence quotes for a research task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
]


class McpServer:
    """Read-only Model Context Protocol server over stdio JSON-RPC."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.documents = DocumentStore(database)
        self.tasks = TaskStore(database)
        self.evidence = EvidenceStore(database)
        self.ingestor = RepositoryIngestor(self.documents, settings)

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            return self._result(
                message_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "repopilot", "version": "1.3.0"},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._result(message_id, {"tools": TOOL_DEFINITIONS})
        if method == "tools/call":
            params = message.get("params") or {}
            try:
                content = await self._call_tool(
                    str(params.get("name")), params.get("arguments") or {}
                )
            except Exception as exc:
                return self._result(
                    message_id,
                    {
                        "content": [{"type": "text", "text": f"error: {type(exc).__name__}"}],
                        "isError": True,
                    },
                )
            return self._result(
                message_id,
                {"content": [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}]},
            )
        if message_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return None

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "search_repository":
            retriever = HybridRetriever(await self.documents.latest_chunk_rows())
            hits = retriever.search(str(arguments["query"]), top_k=int(arguments.get("top_k", 5)))
            return [
                {
                    "citation": hit.citation,
                    "source_uri": hit.chunk.source_uri,
                    "score": hit.score,
                    "content": hit.chunk.content[:600],
                }
                for hit in hits
            ]
        if name == "read_file":
            target = self.ingestor.resolve_safe_path(str(arguments["path"]))
            return {
                "path": str(arguments["path"]),
                "content": target.read_text("utf-8")[:MAX_FILE_CHARS],
            }
        if name == "get_task":
            record = await self.tasks.get_task(str(arguments["task_id"]))
            if record is None:
                raise TaskNotFoundError(details={"task_id": arguments["task_id"]})
            return {
                "id": record.id,
                "goal": record.goal,
                "status": record.status,
                "degraded": record.degraded,
                "final_report": record.final_report,
            }
        if name == "list_evidence":
            records = await self.evidence.list_evidence(str(arguments["task_id"]))
            return [
                {
                    "citation": str(record.metadata_json.get("citation", record.source_uri)),
                    "quote": record.quote,
                    "score": record.score,
                    "review_status": record.review_status,
                }
                for record in records
            ]
        raise KeyError(name)

    @staticmethod
    def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}


async def serve_stdio(settings: Settings) -> None:
    database = Database(settings.database_url)
    await database.initialize()
    server = McpServer(settings, database)
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = await server.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        await database.close()
