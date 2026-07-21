from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.ingestion import IngestionPathError, RepositoryIngestor, chunk_lines
from repopilot.retrieval import LexicalRetriever, tokenize
from repopilot.storage import Database, DocumentStore


def make_settings(workspace: Path, database_url: str) -> Settings:
    return Settings.model_validate(
        {
            "provider": "deterministic",
            "database_url": database_url,
            "workspace_root": str(workspace),
        }
    )


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/ingest-test.db")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "README.md").write_text(
        "# Sample\n\nThe agent loop retries transient tool failures.\n", encoding="utf-8"
    )
    (root / "loop.py").write_text(
        "def agent_loop():\n    '''Retry transient failures with backoff.'''\n    return 42\n",
        encoding="utf-8",
    )
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / "binary.py").write_bytes(b"\xff\xfe\x00broken")
    return root


def test_chunk_lines_keeps_line_citations() -> None:
    content = "\n".join(f"line {i}" for i in range(1, 151))
    chunks = chunk_lines(content, chunk_size=60, overlap=10)
    assert chunks[0]["line_start"] == 1
    assert chunks[0]["line_end"] == 60
    assert chunks[1]["line_start"] == 51
    assert chunks[-1]["line_end"] == 150


def test_chunk_lines_rejects_bad_window() -> None:
    with pytest.raises(ValueError):
        chunk_lines("x", chunk_size=5, overlap=5)


def test_tokenize_handles_mixed_language() -> None:
    tokens = tokenize("Agent 循环重试 tool_failures")
    assert "agent" in tokens
    assert "tool_failures" in tokens
    assert "循环" in tokens and "环重" in tokens and "重试" in tokens


async def test_ingest_skips_git_and_binary(
    database: Database, workspace: Path, tmp_path: Path
) -> None:
    documents = DocumentStore(database)
    settings = make_settings(workspace, f"sqlite+aiosqlite:///{tmp_path}/ingest-test.db")
    ingestor = RepositoryIngestor(documents, settings)

    report = await ingestor.ingest_path()
    assert report.ingested_documents == 2  # README.md + loop.py; .git and binary skipped
    assert report.skipped_files == 1  # undecodable binary.py

    second = await ingestor.ingest_path()
    assert second.ingested_documents == 0
    assert second.unchanged_documents == 2


async def test_ingest_rejects_path_traversal(
    database: Database, workspace: Path, tmp_path: Path
) -> None:
    documents = DocumentStore(database)
    settings = make_settings(workspace, f"sqlite+aiosqlite:///{tmp_path}/ingest-test.db")
    ingestor = RepositoryIngestor(documents, settings)

    with pytest.raises(IngestionPathError):
        ingestor.resolve_safe_path("../outside.txt")


async def test_ingest_rejects_symlink_escape(
    database: Database, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside workspace", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    settings = make_settings(workspace, f"sqlite+aiosqlite:///{tmp_path}/ingest-test.db")
    ingestor = RepositoryIngestor(DocumentStore(database), settings)

    with pytest.raises(IngestionPathError):
        ingestor.resolve_safe_path("escape")


async def test_reingest_after_edit_creates_new_version(
    database: Database, workspace: Path, tmp_path: Path
) -> None:
    documents = DocumentStore(database)
    settings = make_settings(workspace, f"sqlite+aiosqlite:///{tmp_path}/ingest-test.db")
    ingestor = RepositoryIngestor(documents, settings)

    await ingestor.ingest_path()
    (workspace / "README.md").write_text("# Changed content entirely\n", encoding="utf-8")
    report = await ingestor.ingest_path()
    assert report.ingested_documents == 1

    rows = await documents.latest_chunk_rows()
    readme_rows = [row for row in rows if row.source_uri == "README.md"]
    assert len(readme_rows) == 1
    assert "Changed content" in readme_rows[0].content


async def test_reingest_after_revert_restores_previous_content_as_latest(
    database: Database, workspace: Path, tmp_path: Path
) -> None:
    documents = DocumentStore(database)
    settings = make_settings(workspace, f"sqlite+aiosqlite:///{tmp_path}/ingest-test.db")
    ingestor = RepositoryIngestor(documents, settings)
    source = workspace / "README.md"
    original = source.read_text(encoding="utf-8")

    await ingestor.ingest_path()
    source.write_text("# Temporary replacement\n", encoding="utf-8")
    await ingestor.ingest_path()
    source.write_text(original, encoding="utf-8")
    report = await ingestor.ingest_path()

    assert report.ingested_documents == 1
    rows = await documents.latest_chunk_rows()
    readme_rows = [row for row in rows if row.source_uri == "README.md"]
    assert readme_rows
    assert "agent loop retries" in readme_rows[0].content
    assert "Temporary replacement" not in readme_rows[0].content


async def test_retriever_ranks_relevant_chunk_first(
    database: Database, workspace: Path, tmp_path: Path
) -> None:
    documents = DocumentStore(database)
    settings = make_settings(workspace, f"sqlite+aiosqlite:///{tmp_path}/ingest-test.db")
    await RepositoryIngestor(documents, settings).ingest_path()

    retriever = LexicalRetriever(await documents.latest_chunk_rows())
    hits = retriever.search("retry transient failures", top_k=2)
    assert hits
    assert hits[0].score > 0
    assert hits[0].chunk.source_uri in {"README.md", "loop.py"}
    assert hits[0].citation.startswith(hits[0].chunk.source_uri + ":L")


def test_retriever_empty_index_returns_nothing() -> None:
    retriever = LexicalRetriever([])
    assert retriever.search("anything") == []
