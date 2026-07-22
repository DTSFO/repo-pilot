from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import repopilot.cli as cli
from repopilot.config import Settings


def cli_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}",
        workspace_root=tmp_path,
        repository_root=tmp_path / "managed",
        allowed_repository_roots=str(tmp_path),
    )


def test_parser_covers_repository_and_ingest_contracts() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["serve", "--port", "9000"]).port == 9000
    assert parser.parse_args(["ingest", "--repository-id", "repo"]).repository_id == "repo"
    assert parser.parse_args(["repository", "add-local", "/tmp/repo"]).repository_command == (
        "add-local"
    )
    assert (
        parser.parse_args(["repository", "add-git", "https://example.com/a.git"]).repository_command
        == "add-git"
    )
    assert parser.parse_args(["repository", "sync", "repo"]).repository_command == "sync"
    assert parser.parse_args(["repository", "archive", "repo"]).repository_command == "archive"
    assert parser.parse_args(["repository", "list"]).repository_command == "list"
    assert parser.parse_args(["mcp"]).command == "mcp"
    assert parser.parse_args(["eval"]).output == "evals/report.json"


async def test_repository_cli_lifecycle_and_ingest(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("CLI immutable revision marker\n", encoding="utf-8")
    settings = cli_settings(tmp_path)

    added = await cli.run_repository_command(
        settings,
        "add-local",
        SimpleNamespace(path=str(repository), name="CLI repository"),
    )
    repository_id = added["id"]
    assert added["status"] == "ready"
    assert added["indexed_revision"]

    listed = await cli.run_repository_command(settings, "list", SimpleNamespace())
    assert any(item["id"] == repository_id for item in listed)

    synced = await cli.run_repository_command(
        settings, "sync", SimpleNamespace(repository_id=repository_id)
    )
    assert synced["indexed_revision_id"] == added["indexed_revision_id"]

    report = await cli.run_ingest(settings, "README.md", repository_id)
    assert report["scanned_files"] == 1

    archived = await cli.run_repository_command(
        settings, "archive", SimpleNamespace(repository_id=repository_id)
    )
    assert archived["status"] == "archived"

    with pytest.raises(ValueError, match="unknown repository"):
        await cli.run_ingest(settings, None, "missing")


def test_write_report_creates_parent(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "report.json"

    cli._write_report(output, '{"ok":true}')

    assert output.read_text(encoding="utf-8") == '{"ok":true}'
