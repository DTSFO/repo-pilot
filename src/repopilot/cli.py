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
from .storage.database import Database
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

    commands.add_parser("mcp", help="Run the read-only MCP server on stdio")

    evaluate = commands.add_parser("eval", help="Run the fixed evaluation dataset")
    evaluate.add_argument("--dataset", default="evals/dataset.json")
    evaluate.add_argument("--output", default="evals/report.json")

    return parser


async def run_ingest(settings: Settings, path: str | None) -> dict[str, Any]:
    database = Database(settings.database_url)
    await database.initialize()
    try:
        report = await RepositoryIngestor(DocumentStore(database), settings).ingest_path(path)
        return report.__dict__
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


def _write_report(output: Path, payload: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")


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
        report = asyncio.run(run_ingest(settings, arguments.path))
        print(json.dumps(report, ensure_ascii=False, indent=2))
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
