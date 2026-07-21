from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import RepoPilotError
from .storage.repositories import DocumentStore

logger = logging.getLogger(__name__)

SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".idea",
        ".vscode",
        "data",
        "evals",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".rst",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".cfg",
        ".ini",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".html",
        ".sql",
        ".sh",
        ".env.example",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".rb",
        ".php",
        ".proto",
    }
)

CHUNK_LINES = 60
CHUNK_OVERLAP_LINES = 10


class IngestionPathError(RepoPilotError):
    code = "ingestion_path_rejected"
    safe_message = "The requested path is outside the allowed workspace."
    http_status = HTTPStatus.BAD_REQUEST


class DocumentTooLargeError(RepoPilotError):
    code = "document_too_large"
    safe_message = "The document exceeds the configured size limit."
    http_status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE


@dataclass(frozen=True)
class IngestionReport:
    scanned_files: int
    ingested_documents: int
    unchanged_documents: int
    skipped_files: int
    chunks: int


def chunk_lines(
    content: str,
    *,
    chunk_size: int = CHUNK_LINES,
    overlap: int = CHUNK_OVERLAP_LINES,
) -> list[dict[str, Any]]:
    """Split text into line-window chunks that keep resolvable line citations."""
    if chunk_size <= overlap:
        raise ValueError("chunk_size must be greater than overlap")
    lines = content.splitlines()
    if not lines:
        return []
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(lines):
        end = min(start + chunk_size, len(lines))
        text = "\n".join(lines[start:end])
        if text.strip():
            chunks.append(
                {
                    "content": text,
                    "token_count": len(text.split()),
                    "line_start": start + 1,
                    "line_end": end,
                }
            )
        if end == len(lines):
            break
        start = end - overlap
    return chunks


class RepositoryIngestor:
    """Walk a workspace, persist versioned documents, and build retrieval chunks."""

    def __init__(self, documents: DocumentStore, settings: Settings) -> None:
        self.documents = documents
        self.settings = settings

    def resolve_safe_path(self, relative: str | None = None) -> Path:
        root = self.settings.resolved_workspace_root
        candidate = (root / relative).resolve() if relative else root
        if candidate != root and root not in candidate.parents:
            raise IngestionPathError(details={"path": str(relative)})
        return candidate

    async def ingest_path(self, relative: str | None = None) -> IngestionReport:
        target = self.resolve_safe_path(relative)
        root = self.settings.resolved_workspace_root
        scanned = ingested = unchanged = skipped = total_chunks = 0

        for path in self._iter_files(target):
            scanned += 1
            content = self._read_text(path)
            if content is None:
                skipped += 1
                continue
            source_uri = path.relative_to(root).as_posix()
            document, created = await self.documents.upsert_document(
                source_uri=source_uri,
                source_type="repository_file",
                title=path.name,
                content=content,
                metadata={"suffix": path.suffix},
            )
            if not created:
                unchanged += 1
                continue
            chunks = chunk_lines(content)
            await self.documents.replace_chunks(document.id, chunks)
            ingested += 1
            total_chunks += len(chunks)

        return IngestionReport(scanned, ingested, unchanged, skipped, total_chunks)

    def _iter_files(self, target: Path) -> list[Path]:
        if target.is_file():
            return [target]
        files: list[Path] = []
        for path in sorted(target.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if any(part in SKIPPED_DIRECTORIES for part in path.parts):
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and path.name.lower() not in {
                "dockerfile",
                "makefile",
                "license",
            }:
                continue
            files.append(path)
        return files

    def _read_text(self, path: Path) -> str | None:
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > self.settings.max_upload_bytes:
            logger.warning("Skipping oversized file", extra={"operation": "ingest"})
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None
