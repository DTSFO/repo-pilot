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
        ".aws",
        ".azure",
        ".gnupg",
        ".kube",
        ".ssh",
    }
)
SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        ".npmrc",
        ".netrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_ed25519",
    }
)
SAFE_SPECIAL_NAMES = frozenset({"dockerfile", "makefile", "license"})
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
    safe_message = "The requested path is not an allowed workspace source."
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
        if relative is None:
            return root
        requested = Path(relative)
        # The public API accepts workspace-relative paths only.  Reject parent
        # segments before resolving so a symlink cannot be combined with `..`
        # to evade the lexical policy.
        if requested.is_absolute() or ".." in requested.parts:
            raise IngestionPathError(details={"path": str(relative)})
        lexical = root / requested
        cursor = root
        for part in requested.parts:
            if part in {"", "."}:
                continue
            cursor /= part
            if cursor.is_symlink():
                raise IngestionPathError(details={"path": str(relative)})
        candidate = lexical.resolve()
        if candidate != root and root not in candidate.parents:
            raise IngestionPathError(details={"path": str(relative)})
        if not candidate.exists():
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
        root = self.settings.resolved_workspace_root
        if target.is_file():
            if not self._is_allowed_file(target, root):
                raise IngestionPathError(details={"path": target.relative_to(root).as_posix()})
            return [target]
        files: list[Path] = []
        for path in sorted(target.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if not self._is_allowed_file(path, root):
                continue
            files.append(path)
        return files

    @staticmethod
    def _is_allowed_file(path: Path, root: Path) -> bool:
        """Apply identical filtering to directory walks and explicit files."""

        try:
            relative = path.relative_to(root)
        except ValueError:
            return False
        parts = relative.parts
        if any(part in SKIPPED_DIRECTORIES for part in parts[:-1]):
            return False
        name = path.name.lower()
        if name in SENSITIVE_FILE_NAMES or (name.startswith(".env.") and name != ".env.example"):
            return False
        if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".der"}:
            return False
        return (
            path.suffix.lower() in TEXT_SUFFIXES
            or name.endswith(".env.example")
            or name in SAFE_SPECIAL_NAMES
        )

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
