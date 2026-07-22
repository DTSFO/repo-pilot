from __future__ import annotations

import asyncio
import json
import sys
from pathlib import PurePosixPath
from typing import Any

from .config import Settings
from .errors import RepoPilotError
from .repository_manager import RepositoryNotFoundError, RepositoryRequestError
from .retrieval import HybridRetriever
from .service import TaskNotFoundError
from .storage.database import Database
from .storage.models import RepositoryRecord
from .storage.repositories import DocumentStore, EvidenceStore, RepositoryStore, TaskStore

PROTOCOL_VERSION = "2024-11-05"
MAX_FILE_CHARS = 20_000

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_repository",
        "description": (
            "Search one repository's active immutable index revision and return citations. "
            "repository_id is required when more than one repository is registered."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                "repository_id": {"type": "string", "minLength": 1},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from one repository's active immutable index revision. "
            "repository_id is required when more than one repository is registered."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "repository_id": {"type": "string", "minLength": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task",
        "description": "Get status, immutable repository scope, and final report of a task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_evidence",
        "description": "List reviewed evidence quotes for an existing research task.",
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
        self.repositories = RepositoryStore(database)
        self.tasks = TaskStore(database)
        self.evidence = EvidenceStore(database)

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            return self._result(
                message_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "repopilot", "version": "1.4.0"},
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
            except RepoPilotError as exc:
                return self._tool_error(
                    message_id,
                    {"code": exc.code, "message": exc.safe_message},
                )
            except Exception:
                return self._tool_error(
                    message_id,
                    {
                        "code": "mcp_tool_failed",
                        "message": "The MCP tool could not complete the operation.",
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
            repository, revision_id = await self._resolve_repository(arguments.get("repository_id"))
            query = str(arguments.get("query", "")).strip()
            if not query:
                raise RepositoryRequestError(details={"reason": "search_query_required"})
            documents = DocumentStore(self.database, repository.id, revision_id)
            retriever = HybridRetriever(await documents.latest_chunk_rows())
            top_k = min(20, max(1, int(arguments.get("top_k", 5))))
            hits = retriever.search(query, top_k=top_k)
            return [
                {
                    "repository_id": repository.id,
                    "revision_id": revision_id,
                    "citation": hit.citation,
                    "source_uri": hit.chunk.source_uri,
                    "score": hit.score,
                    "content": hit.chunk.content[:600],
                }
                for hit in hits
            ]
        if name == "read_file":
            repository, revision_id = await self._resolve_repository(arguments.get("repository_id"))
            source_uri = self._source_uri(str(arguments.get("path", "")))
            document = await DocumentStore(
                self.database, repository.id, revision_id
            ).latest_document(source_uri)
            if document is None:
                raise RepositoryRequestError(details={"reason": "indexed_file_not_found"})
            return {
                "repository_id": repository.id,
                "revision_id": revision_id,
                "path": source_uri,
                "content": document.content[:MAX_FILE_CHARS],
                "truncated": len(document.content) > MAX_FILE_CHARS,
            }
        if name == "get_task":
            record = await self.tasks.get_task(str(arguments["task_id"]))
            if record is None:
                raise TaskNotFoundError(details={"task_id": arguments["task_id"]})
            return {
                "id": record.id,
                "repository_id": record.repository_id,
                "revision_id": record.revision_id,
                "goal": record.goal,
                "status": record.status,
                "degraded": record.degraded,
                "final_report": record.final_report,
            }
        if name == "list_evidence":
            task_id = str(arguments["task_id"])
            task = await self.tasks.get_task(task_id)
            if task is None:
                raise TaskNotFoundError(details={"task_id": task_id})
            records = await self.evidence.list_evidence(task_id)
            return [
                {
                    "repository_id": record.repository_id,
                    "revision_id": record.revision_id,
                    "citation": str(record.metadata_json.get("citation", record.source_uri)),
                    "quote": record.quote,
                    "score": record.score,
                    "review_status": record.review_status,
                }
                for record in records
            ]
        raise RepositoryRequestError(details={"reason": "unknown_mcp_tool"})

    async def _resolve_repository(self, repository_id: Any) -> tuple[RepositoryRecord, str]:
        if repository_id:
            repository = await self.repositories.get_repository(str(repository_id))
        else:
            active = await self.repositories.list_repositories()
            repository = active[0] if len(active) == 1 else None
        if repository is None or repository.status == "archived":
            if repository_id:
                raise RepositoryNotFoundError(details={"repository_id": repository_id})
            raise RepositoryRequestError(details={"reason": "repository_id_required"})

        revision = (
            await self.repositories.get_revision(repository.indexed_revision_id)
            if repository.indexed_revision_id
            else None
        )
        if (
            revision is None
            or revision.repository_id != repository.id
            or revision.status != "ready"
        ):
            revision = await self.repositories.get_latest_ready_revision(repository.id)
        if revision is None:
            raise RepositoryRequestError(details={"reason": "repository_not_indexed"})
        return repository, revision.id

    @staticmethod
    def _source_uri(path: str) -> str:
        requested = PurePosixPath(path.strip())
        if (
            not path.strip()
            or requested.is_absolute()
            or ".." in requested.parts
            or str(requested) in {"", "."}
        ):
            raise RepositoryRequestError(details={"reason": "repository_path_rejected"})
        return requested.as_posix()

    @staticmethod
    def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @classmethod
    def _tool_error(cls, message_id: Any, error: dict[str, str]) -> dict[str, Any]:
        return cls._result(
            message_id,
            {
                "content": [{"type": "text", "text": json.dumps({"error": error})}],
                "isError": True,
            },
        )


async def serve_stdio(settings: Settings) -> None:
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(settings.resolved_workspace_root))
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
