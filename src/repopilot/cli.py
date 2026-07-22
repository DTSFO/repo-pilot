from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .evaluation import EvaluationRunner
from .ingestion import RepositoryIngestor
from .observability import configure_logging
from .providers.factory import build_provider
from .repository_manager import RepositoryManager, RepositoryNotFoundError
from .storage.database import Database
from .storage.models import RepositoryRecord
from .storage.repositories import DocumentStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repopilot", description="RepoPilot repository research agent"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the HTTP API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    ingest = commands.add_parser("ingest", help="Ingest the configured workspace")
    ingest.add_argument("--path", default=None, help="Relative path inside the workspace")
    ingest.add_argument("--repository-id", default=None)

    repository = commands.add_parser("repository", help="Manage indexed repositories")
    repository_sub = repository.add_subparsers(dest="repository_command", required=True)
    repository_add_local = repository_sub.add_parser("add-local")
    repository_add_local.add_argument("path")
    repository_add_local.add_argument("--name", default=None)
    repository_add_git = repository_sub.add_parser("add-git")
    repository_add_git.add_argument("url")
    repository_add_git.add_argument("--name", default=None)
    repository_sub.add_parser("list")
    repository_sync = repository_sub.add_parser("sync")
    repository_sync.add_argument("repository_id")
    repository_archive = repository_sub.add_parser("archive")
    repository_archive.add_argument("repository_id")

    commands.add_parser("mcp", help="Run the read-only MCP server on stdio")

    evaluate = commands.add_parser("eval", help="Run the fixed evaluation dataset")
    evaluate.add_argument("--dataset", default="evals/dataset.json")
    evaluate.add_argument("--output", default="evals/report.json")

    return parser


async def run_ingest(
    settings: Settings, path: str | None, repository_id: str | None = None
) -> dict[str, Any]:
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(settings.resolved_workspace_root))
    try:
        manager = RepositoryManager(database, settings)
        repository = (
            await manager.ensure_default()
            if repository_id is None
            else await manager.store.get_repository(repository_id)
        )
        if repository is None:
            raise ValueError(f"unknown repository: {repository_id}")
        if path is not None:
            # Validate the requested relative path, then rebuild a complete immutable
            # repository snapshot rather than mutating the active revision in place.
            RepositoryIngestor(
                DocumentStore(database, repository.id, repository.indexed_revision_id),
                settings,
                root_path=Path(repository.root_path),
            ).resolve_safe_path(path)
        revision = await manager.refresh(repository.id)
        return revision.stats_json
    finally:
        await database.close()


async def run_eval(settings: Settings, dataset: Path, output: Path) -> dict[str, Any]:
    database = Database(settings.database_url)
    await database.initialize()
    provider = build_provider(settings)
    try:
        runner = EvaluationRunner(settings, database, provider)
        result = await runner.run(dataset)
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        await asyncio.to_thread(_write_report, output, payload)
        return result
    finally:
        await provider.close()
        await database.close()


async def run_repository_command(settings: Settings, command: str, arguments: Any) -> Any:
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(settings.resolved_workspace_root))
    manager = RepositoryManager(database, settings)
    try:
        record: RepositoryRecord | None = None
        if command == "add-local":
            record = await manager.add_local(arguments.path, name=arguments.name)
            await manager.refresh(record.id)
        elif command == "add-git":
            record = await manager.add_git(arguments.url, name=arguments.name)
            await manager.refresh(record.id)
        elif command == "sync":
            await manager.refresh(arguments.repository_id)
            record = await manager.store.get_repository(arguments.repository_id)
            if record is None:
                raise RepositoryNotFoundError(details={"repository_id": arguments.repository_id})
        elif command == "archive":
            await manager.archive(arguments.repository_id)
            record = await manager.store.get_repository(arguments.repository_id)
            if record is None:
                raise RepositoryNotFoundError(details={"repository_id": arguments.repository_id})
        else:
            return [
                await _repository_payload(manager, item)
                for item in await manager.store.list_repositories()
            ]
        if record is None:
            raise RepositoryNotFoundError(details={"reason": "repository_command_failed"})
        refreshed = await manager.store.get_repository(record.id)
        if refreshed is None:
            raise RepositoryNotFoundError(details={"repository_id": record.id})
        return await _repository_payload(manager, refreshed)
    finally:
        await database.close()


def _write_report(output: Path, payload: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")


async def _repository_payload(
    manager: RepositoryManager, record: RepositoryRecord
) -> dict[str, Any]:
    revision = (
        await manager.store.get_revision(record.indexed_revision_id)
        if record.indexed_revision_id
        else None
    )
    return {
        "id": record.id,
        "name": record.name,
        "source_type": record.source_type,
        "source_location": record.source_location,
        "status": record.status,
        "indexed_revision_id": revision.id if revision else None,
        "indexed_revision": revision.revision if revision else None,
        "last_error": record.last_error,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)

    if arguments.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(
            create_app(settings),
            host=arguments.host,
            port=arguments.port,
            log_level=settings.log_level.lower(),
        )
        return 0

    if arguments.command == "ingest":
        report = asyncio.run(run_ingest(settings, arguments.path, arguments.repository_id))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if arguments.command == "repository":
        result = asyncio.run(
            run_repository_command(settings, arguments.repository_command, arguments)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if arguments.command == "mcp":
        from .mcp import serve_stdio

        asyncio.run(serve_stdio(settings))
        return 0

    if arguments.command == "eval":
        result = asyncio.run(run_eval(settings, Path(arguments.dataset), Path(arguments.output)))
        print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
