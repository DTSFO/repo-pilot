from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

from .config import Settings
from .errors import RepoPilotError
from .ingestion import RepositoryIngestor, chunk_lines
from .storage.database import Database
from .storage.models import (
    LEGACY_REPOSITORY_ID,
    RepositoryRecord,
    RepositoryRevisionRecord,
    SourceDocumentRecord,
)
from .storage.repositories import DocumentStore, RepositoryStore


class RepositoryRequestError(RepoPilotError):
    code = "repository_request_rejected"
    safe_message = "The repository request is not allowed."
    http_status = HTTPStatus.BAD_REQUEST


class RepositoryNotFoundError(RepoPilotError):
    code = "repository_not_found"
    safe_message = "The requested repository does not exist."
    http_status = HTTPStatus.NOT_FOUND


class RepositorySyncError(RepoPilotError):
    code = "repository_sync_failed"
    safe_message = "The repository could not be synchronized."
    retryable = True
    http_status = HTTPStatus.BAD_GATEWAY


@dataclass(frozen=True)
class DocumentUploadResult:
    revision: RepositoryRevisionRecord
    created: bool
    chunks: int


class RepositoryManager:
    """Onboard, refresh, and safely scope repositories and immutable revisions."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.store = RepositoryStore(database)
        self.repository_root = settings.resolved_repository_root
        self.allowed_roots = settings.repository_root_allowlist
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def ensure_default(self) -> RepositoryRecord:
        return await self.store.ensure_legacy(str(self.settings.resolved_workspace_root))

    async def add_local(self, path: str, *, name: str | None = None) -> RepositoryRecord:
        resolved = await asyncio.to_thread(self._validate_local_path, path)
        identity_key = f"local:{resolved}"
        async with self._lock(identity_key):
            existing = await self.store.get_repository_by_identity(identity_key)
            if existing is not None:
                if existing.status == "archived":
                    return await self.store.update_repository(existing.id, status="ready")
                return existing
            return await self.store.create_local(
                name=self._display_name(name, resolved.name),
                identity_key=identity_key,
                source_location=str(resolved),
                root_path=str(resolved),
            )

    async def add_git(self, url: str, *, name: str | None = None) -> RepositoryRecord:
        safe_url, fallback_name = await self._validate_git_url(url)
        identity_key = f"git:{safe_url}"
        async with self._lock(identity_key):
            existing = await self.store.get_repository_by_identity(identity_key)
            if existing is not None:
                if existing.status == "archived":
                    return await self.store.update_repository(existing.id, status="ready")
                return existing

            await asyncio.to_thread(self.repository_root.mkdir, parents=True, exist_ok=True)
            # The user-facing name never participates in the filesystem path.
            target = self.repository_root / uuid4().hex
            try:
                await self._run_git(
                    "-c",
                    "credential.helper=",
                    "-c",
                    "http.followRedirects=false",
                    "clone",
                    "--depth",
                    "1",
                    "--no-tags",
                    "--single-branch",
                    "--no-recurse-submodules",
                    safe_url,
                    str(target),
                    timeout_reason="git_clone_timeout",
                    failure_reason="git_clone_failed",
                )
                return await self.store.create_local(
                    name=self._display_name(name, fallback_name),
                    identity_key=identity_key,
                    source_location=safe_url,
                    root_path=str(target),
                    source_type="git",
                )
            except Exception:
                await asyncio.to_thread(self._remove_clone_target, target)
                raise

    async def refresh(self, repository_id: str) -> RepositoryRevisionRecord:
        async with self._lock(repository_id):
            repository = await self.store.get_repository(repository_id)
            if repository is None or repository.status == "archived":
                raise RepositoryNotFoundError(details={"repository_id": repository_id})

            revision_record: RepositoryRevisionRecord | None = None
            try:
                root = await asyncio.to_thread(self._validate_registered_root, repository)
                if repository.source_type == "git":
                    await self._pull(repository)
                    await self._ensure_git_worktree_clean(root)
                source_revision = await self._revision(
                    root, use_git_commit=repository.source_type == "git"
                )
                uploads = await self._active_uploads(repository)
                revision = self._snapshot_revision(source_revision, uploads)
                revision_record = await self.store.create_revision(
                    repository.id, revision=revision, root_path=str(root)
                )
                if revision_record.status == "ready":
                    # A previous crash or failed later refresh may have left the repository
                    # pointer stale even though this exact immutable index is already ready.
                    await self.store.update_repository(
                        repository.id,
                        status="ready",
                        indexed_revision_id=revision_record.id,
                        last_error=None,
                    )
                    return revision_record

                await self.store.begin_revision(revision_record.id)
                await self.store.reset_revision_documents(repository.id, revision_record.id)
                await self.store.update_repository(
                    repository.id, status="indexing", last_error=None
                )
                documents = DocumentStore(self.database, repository.id, revision_record.id)
                report = await RepositoryIngestor(
                    documents, self.settings, root_path=root
                ).ingest_path()
                for upload in uploads:
                    document, _ = await documents.upsert_document(
                        source_uri=upload.source_uri,
                        source_type="upload",
                        title=upload.title,
                        content=upload.content,
                        metadata=dict(upload.metadata_json),
                    )
                    await documents.replace_chunks(document.id, chunk_lines(upload.content))
                if repository.source_type == "git":
                    await self._ensure_git_worktree_clean(root)
                verified_source_revision = await self._revision(
                    root, use_git_commit=repository.source_type == "git"
                )
                if verified_source_revision != source_revision:
                    raise RepositorySyncError(details={"reason": "repository_changed_during_index"})
                stats = asdict(report) | {
                    "revision": revision,
                    "source_revision": source_revision,
                    "uploaded_documents": len(uploads),
                }
                return await self.store.finish_revision(
                    revision_record.id, status="ready", stats=stats
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._record_refresh_failure(
                        repository, revision_record, "repository_sync_cancelled"
                    )
                )
                raise
            except RepositorySyncError as exc:
                await self._record_refresh_failure(
                    repository, revision_record, exc.details["reason"]
                )
                raise
            except Exception as exc:
                reason = "repository_index_failed"
                await self._record_refresh_failure(repository, revision_record, reason)
                raise RepositorySyncError(details={"reason": reason}) from exc

    async def add_document(
        self,
        repository_id: str,
        *,
        name: str,
        content: str,
        content_type: str | None,
    ) -> DocumentUploadResult:
        """Add an uploaded document by publishing a new immutable overlay revision."""

        safe_name = self._upload_name(name)
        source_uri = f"uploads/{safe_name}"
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        async with self._lock(repository_id):
            repository = await self.store.get_repository(repository_id)
            if repository is None or repository.status == "archived":
                raise RepositoryNotFoundError(details={"repository_id": repository_id})

            base_revision_id = repository.indexed_revision_id
            if base_revision_id is not None:
                current = await DocumentStore(
                    self.database, repository.id, base_revision_id
                ).latest_document(source_uri)
                if current is not None and current.content_hash == content_hash:
                    revision = await self.store.get_revision(base_revision_id)
                    if revision is None:
                        raise RepositorySyncError(details={"reason": "indexed_revision_missing"})
                    return DocumentUploadResult(revision=revision, created=False, chunks=0)

            base_revision = (
                await self.store.get_revision(base_revision_id) if base_revision_id else None
            )
            fingerprint = hashlib.sha256(
                "\0".join(
                    (base_revision.revision if base_revision else "empty", source_uri, content_hash)
                ).encode("utf-8")
            ).hexdigest()
            revision_record = await self.store.create_revision(
                repository.id,
                revision=f"upload-{fingerprint}",
                root_path=repository.root_path,
            )
            if revision_record.status == "ready":
                await self.store.update_repository(
                    repository.id,
                    status="ready",
                    indexed_revision_id=revision_record.id,
                    last_error=None,
                )
                return DocumentUploadResult(revision=revision_record, created=False, chunks=0)

            try:
                await self.store.begin_revision(revision_record.id)
                await self.store.update_repository(
                    repository.id, status="indexing", last_error=None
                )
                documents = DocumentStore(self.database, repository.id, revision_record.id)
                copied_documents, copied_chunks = await documents.replace_revision_snapshot(
                    base_revision_id
                )
                document, created = await documents.upsert_document(
                    source_uri=source_uri,
                    source_type="upload",
                    title=safe_name,
                    content=content,
                    metadata={"content_type": content_type},
                )
                chunks = chunk_lines(content)
                await documents.replace_chunks(document.id, chunks)
                ready = await self.store.finish_revision(
                    revision_record.id,
                    status="ready",
                    stats={
                        "base_revision_id": base_revision_id,
                        "uploaded_document": source_uri,
                        "copied_documents": copied_documents,
                        "copied_chunks": copied_chunks,
                        "chunks": len(chunks),
                    },
                )
                return DocumentUploadResult(
                    revision=ready,
                    created=created,
                    chunks=len(chunks),
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._record_refresh_failure(
                        repository, revision_record, "document_upload_cancelled"
                    )
                )
                raise
            except Exception as exc:
                reason = "document_upload_failed"
                await self._record_refresh_failure(repository, revision_record, reason)
                raise RepositorySyncError(details={"reason": reason}) from exc

    async def archive(self, repository_id: str) -> None:
        if repository_id == LEGACY_REPOSITORY_ID:
            raise RepositoryRequestError(
                details={"reason": "default_repository_cannot_be_archived"}
            )
        repository = await self.store.get_repository(repository_id)
        if repository is None or repository.status == "archived":
            raise RepositoryNotFoundError(details={"repository_id": repository_id})
        await self.store.update_repository(repository_id, status="archived")

    async def _pull(self, repository: RepositoryRecord) -> None:
        safe_url, _ = await self._validate_git_url(repository.source_location)
        root = repository.root_path
        remote = (
            (
                await self._run_git(
                    "-C",
                    root,
                    "remote",
                    "get-url",
                    "origin",
                    timeout_reason="git_remote_timeout",
                    failure_reason="git_remote_invalid",
                )
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
        if remote != safe_url:
            raise RepositorySyncError(details={"reason": "git_remote_changed"})
        await self._run_git(
            "-C",
            root,
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "pull",
            "--ff-only",
            "--no-tags",
            timeout_reason="git_pull_timeout",
            failure_reason="git_pull_failed",
        )

    async def _run_git(
        self,
        *arguments: str,
        timeout_reason: str,
        failure_reason: str,
    ) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._git_environment(),
            )
        except OSError as exc:
            raise RepositorySyncError(details={"reason": failure_reason}) from exc
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.repository_sync_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RepositorySyncError(details={"reason": timeout_reason}) from exc
        if process.returncode != 0:
            raise RepositorySyncError(details={"reason": failure_reason})
        return stdout

    async def _revision(self, root: Path, *, use_git_commit: bool = False) -> str:
        git_dir = root / ".git"
        if use_git_commit and await asyncio.to_thread(git_dir.exists):
            try:
                stdout = await self._run_git(
                    "-C",
                    str(root),
                    "rev-parse",
                    "HEAD",
                    timeout_reason="git_revision_timeout",
                    failure_reason="git_revision_failed",
                )
            except RepositorySyncError:
                pass
            else:
                revision = stdout.decode("ascii", errors="ignore").strip()
                if revision:
                    return revision
        return await asyncio.to_thread(self._tree_revision, root)

    async def _ensure_git_worktree_clean(self, root: Path) -> None:
        status = await self._run_git(
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            timeout_reason="git_status_timeout",
            failure_reason="git_status_failed",
        )
        if status.strip():
            raise RepositorySyncError(details={"reason": "git_worktree_dirty"})

    async def _record_refresh_failure(
        self,
        repository: RepositoryRecord,
        revision: RepositoryRevisionRecord | None,
        reason: str,
    ) -> None:
        try:
            if revision is not None:
                await self.store.finish_revision(
                    revision.id, status="failed", stats={}, error_code=reason
                )
            else:
                await self.store.update_repository(
                    repository.id,
                    status="ready" if repository.indexed_revision_id else "failed",
                    last_error=reason,
                )
        except Exception:
            # Failure bookkeeping must not hide the original synchronization error.
            return

    async def _active_uploads(self, repository: RepositoryRecord) -> list[SourceDocumentRecord]:
        if repository.indexed_revision_id is None:
            return []
        return list(
            await DocumentStore(
                self.database, repository.id, repository.indexed_revision_id
            ).latest_documents_by_source_type("upload")
        )

    @staticmethod
    def _snapshot_revision(source_revision: str, uploads: list[SourceDocumentRecord]) -> str:
        if not uploads:
            return source_revision
        digest = hashlib.sha256(source_revision.encode("utf-8"))
        for upload in uploads:
            digest.update(upload.source_uri.encode("utf-8"))
            digest.update(upload.content_hash.encode("ascii"))
        return f"snapshot-{digest.hexdigest()}"

    async def _validate_git_url(self, url: str) -> tuple[str, str]:
        raw = url.strip()
        if not raw or any(ord(character) < 32 for character in raw):
            raise RepositoryRequestError(details={"reason": "invalid_git_url"})
        parsed = urlparse(raw)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RepositoryRequestError(
                details={"reason": "https_git_url_without_credentials_required"}
            )
        try:
            port = parsed.port
            host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError) as exc:
            raise RepositoryRequestError(details={"reason": "invalid_git_host"}) from exc
        if port not in {None, 443}:
            raise RepositoryRequestError(details={"reason": "https_git_port_rejected"})
        if not parsed.path or parsed.path == "/":
            raise RepositoryRequestError(details={"reason": "git_repository_path_required"})
        await self._require_public_host(host, port or 443)
        netloc = f"[{host}]" if ":" in host else host
        safe_url = urlunparse(("https", netloc, parsed.path, "", "", ""))
        fallback_name = Path(parsed.path.rstrip("/")).stem or "repository"
        return safe_url, fallback_name

    async def _require_public_host(self, host: str, port: int) -> None:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                resolved = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise RepositorySyncError(details={"reason": "git_host_resolution_failed"}) from exc
            addresses = {item[4][0] for item in resolved}
            if not addresses:
                raise RepositorySyncError(
                    details={"reason": "git_host_resolution_failed"}
                ) from None
            literals = [ipaddress.ip_address(address) for address in addresses]
        else:
            literals = [literal]
        if any(not address.is_global for address in literals):
            raise RepositoryRequestError(details={"reason": "private_git_host_rejected"})

    def _validate_local_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise RepositoryRequestError(details={"reason": "local_path_must_be_absolute"})
        if candidate.is_symlink():
            raise RepositoryRequestError(details={"reason": "symlink_root_rejected"})
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise RepositoryRequestError(details={"reason": "local_path_not_directory"})
        if not any(resolved == root or root in resolved.parents for root in self.allowed_roots):
            raise RepositoryRequestError(details={"reason": "local_path_outside_allowlist"})
        return resolved

    def _validate_registered_root(self, repository: RepositoryRecord) -> Path:
        root = Path(repository.root_path)
        if root.is_symlink():
            raise RepositorySyncError(details={"reason": "repository_root_symlinked"})
        resolved = root.resolve()
        if not resolved.is_dir():
            raise RepositorySyncError(details={"reason": "repository_root_missing"})
        allowed = (self.repository_root,) if repository.source_type == "git" else self.allowed_roots
        if not any(resolved == base or base in resolved.parents for base in allowed):
            raise RepositorySyncError(details={"reason": "repository_root_outside_allowlist"})
        return resolved

    def _tree_revision(self, root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if not RepositoryIngestor._is_allowed_file(path, root):
                continue
            try:
                if path.stat().st_size > self.settings.max_upload_bytes:
                    continue
                digest.update(path.relative_to(root).as_posix().encode("utf-8"))
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
            except OSError:
                continue
        return f"tree-{digest.hexdigest()}"

    @staticmethod
    def _display_name(name: str | None, fallback: str) -> str:
        candidate = (name or fallback or "repository").strip()
        return (candidate or "repository")[:160]

    @staticmethod
    def _upload_name(name: str) -> str:
        candidate = name.strip() or "upload.txt"
        if (
            len(candidate) > 255
            or "/" in candidate
            or "\\" in candidate
            or candidate in {".", ".."}
            or any(ord(character) < 32 for character in candidate)
        ):
            raise RepositoryRequestError(details={"reason": "invalid_upload_filename"})
        return candidate

    @staticmethod
    def _git_environment() -> dict[str, str]:
        return {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
            "SSH_ASKPASS": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }

    def _remove_clone_target(self, target: Path) -> None:
        resolved = target.resolve()
        if resolved.parent == self.repository_root and resolved != self.repository_root:
            shutil.rmtree(resolved, ignore_errors=True)
