from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..errors import DailyQuotaExceededError
from .database import Database
from .models import (
    LEGACY_REPOSITORY_ID,
    CheckpointRecord,
    ChunkRecord,
    DailyUsageRecord,
    EvidenceRecord,
    MemoryItemRecord,
    RepositoryRecord,
    RepositoryRevisionRecord,
    ResearchTaskRecord,
    SourceDocumentRecord,
    TaskEventRecord,
    utc_now,
)


class TaskStore:
    """Transactional task, event, and checkpoint persistence."""

    def __init__(self, database: Database) -> None:
        self.database = database
        # RepoPilot currently advertises a single-process persistence model. Provider telemetry
        # can append events concurrently with node checkpoints, so serialize sequence allocation
        # per task inside that process instead of racing on max(sequence) + 1.
        self._event_locks: dict[str, asyncio.Lock] = {}

    async def create_task(
        self,
        goal: str,
        *,
        repository_id: str | None = None,
        revision_id: str | None = None,
        constraints: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> ResearchTaskRecord:
        record = ResearchTaskRecord(
            id=str(uuid4()),
            repository_id=repository_id,
            revision_id=revision_id,
            goal=goal,
            constraints_json=constraints or {},
            budget_json=budget or {},
            status="pending",
        )
        async with self.database.session() as session:
            session.add(record)
        return record

    async def get_task(self, task_id: str) -> ResearchTaskRecord | None:
        async with self.database.session() as session:
            return await session.get(ResearchTaskRecord, task_id)

    async def list_tasks(
        self, *, limit: int = 50, repository_id: str | None = None
    ) -> list[ResearchTaskRecord]:
        async with self.database.session() as session:
            query = select(ResearchTaskRecord).order_by(ResearchTaskRecord.created_at.desc())
            if repository_id is not None:
                query = query.where(ResearchTaskRecord.repository_id == repository_id)
            result = await session.scalars(query.limit(limit))
            return list(result)

    async def update_task(self, task_id: str, **changes: Any) -> ResearchTaskRecord:
        async with self.database.session() as session:
            record = await session.get(ResearchTaskRecord, task_id)
            if record is None:
                raise KeyError(task_id)
            allowed = {
                "status",
                "current_node",
                "final_report",
                "error_code",
                "degraded",
                "version",
                "constraints_json",
                "budget_json",
            }
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"Unsupported task fields: {sorted(unknown)}")
            for field, value in changes.items():
                setattr(record, field, value)
            await session.flush()
            return record

    async def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> TaskEventRecord:
        lock = self._event_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            for attempt in range(3):
                try:
                    async with self.database.session() as session:
                        task = await session.get(ResearchTaskRecord, task_id)
                        if task is None:
                            raise KeyError(task_id)
                        last_sequence = await session.scalar(
                            select(func.max(TaskEventRecord.sequence)).where(
                                TaskEventRecord.task_id == task_id
                            )
                        )
                        event = TaskEventRecord(
                            task_id=task_id,
                            sequence=(last_sequence or 0) + 1,
                            event_type=event_type,
                            payload_json=payload or {},
                        )
                        session.add(event)
                        await session.flush()
                    return event
                except IntegrityError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0)
        raise RuntimeError("event sequence allocation exhausted")

    async def list_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[TaskEventRecord]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(TaskEventRecord)
                .where(
                    TaskEventRecord.task_id == task_id,
                    TaskEventRecord.sequence > after_sequence,
                )
                .order_by(TaskEventRecord.sequence)
                .limit(limit)
            )
            return list(result)

    async def save_checkpoint(
        self,
        task_id: str,
        node: str,
        state: dict[str, Any],
    ) -> CheckpointRecord:
        async with self.database.session() as session:
            task = await session.get(ResearchTaskRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            last_version = await session.scalar(
                select(func.max(CheckpointRecord.version)).where(
                    CheckpointRecord.task_id == task_id
                )
            )
            checkpoint = CheckpointRecord(
                task_id=task_id,
                version=(last_version or 0) + 1,
                node=node,
                state_json=state,
            )
            session.add(checkpoint)
            task.version = checkpoint.version
            task.current_node = node
            await session.flush()
            return checkpoint

    async def latest_checkpoint(self, task_id: str) -> CheckpointRecord | None:
        async with self.database.session() as session:
            result = await session.scalars(
                select(CheckpointRecord)
                .where(CheckpointRecord.task_id == task_id)
                .order_by(CheckpointRecord.version.desc())
                .limit(1)
            )
            return result.first()


class DailyQuotaStore:
    """Persistent per-client daily task quota with atomic database updates."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def consume(self, client_hash: str, usage_date: str, limit: int) -> int | None:
        if limit <= 0:
            return None
        key = (client_hash, usage_date)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            for attempt in range(3):
                try:
                    async with self.database.session() as session:
                        record = await session.get(
                            DailyUsageRecord,
                            {"client_hash": client_hash, "usage_date": usage_date},
                        )
                        if record is None:
                            session.add(
                                DailyUsageRecord(
                                    client_hash=client_hash,
                                    usage_date=usage_date,
                                    task_count=1,
                                )
                            )
                            await session.flush()
                            return limit - 1
                        if record.task_count >= limit:
                            raise DailyQuotaExceededError(retry_after_seconds=0)
                        record.task_count += 1
                        await session.flush()
                        return limit - record.task_count
                except IntegrityError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0)
        raise RuntimeError("daily quota allocation exhausted")


class RepositoryStore:
    """Persistent repository registry and immutable index revision metadata."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def ensure_legacy(self, root_path: str) -> RepositoryRecord:
        async with self.database.session() as session:
            record = await session.get(RepositoryRecord, LEGACY_REPOSITORY_ID)
            if record is None:
                record = RepositoryRecord(
                    id=LEGACY_REPOSITORY_ID,
                    name="Default workspace",
                    source_type="local",
                    identity_key=f"legacy:{root_path}",
                    source_location=root_path,
                    root_path=root_path,
                    status="ready",
                    metadata_json={"legacy": True},
                )
                session.add(record)
                await session.flush()
            elif not record.metadata_json.get("legacy"):
                record.metadata_json = {**record.metadata_json, "legacy": True}
                record.source_location = root_path
                record.root_path = root_path
                await session.flush()
            return record

    async def create_local(
        self,
        *,
        name: str,
        identity_key: str,
        source_location: str,
        root_path: str,
        source_type: str = "local",
    ) -> RepositoryRecord:
        async with self.database.session() as session:
            existing = await session.scalar(
                select(RepositoryRecord).where(RepositoryRecord.identity_key == identity_key)
            )
            if existing is not None:
                return existing
            record = RepositoryRecord(
                id=str(uuid4()),
                name=name,
                source_type=source_type,
                identity_key=identity_key,
                source_location=source_location,
                root_path=root_path,
                status="ready",
            )
            session.add(record)
            await session.flush()
            return record

    async def get_repository_by_identity(self, identity_key: str) -> RepositoryRecord | None:
        async with self.database.session() as session:
            result = await session.scalars(
                select(RepositoryRecord).where(RepositoryRecord.identity_key == identity_key)
            )
            return result.first()

    async def list_repositories(self, *, include_archived: bool = False) -> list[RepositoryRecord]:
        async with self.database.session() as session:
            query = select(RepositoryRecord).order_by(RepositoryRecord.updated_at.desc())
            if not include_archived:
                query = query.where(RepositoryRecord.status != "archived")
            return list(await session.scalars(query))

    async def get_repository(self, repository_id: str) -> RepositoryRecord | None:
        async with self.database.session() as session:
            return await session.get(RepositoryRecord, repository_id)

    async def update_repository(self, repository_id: str, **changes: Any) -> RepositoryRecord:
        allowed = {"name", "status", "indexed_revision_id", "last_error", "metadata_json"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported repository fields: {sorted(unknown)}")
        async with self.database.session() as session:
            record = await session.get(RepositoryRecord, repository_id)
            if record is None:
                raise KeyError(repository_id)
            for field, value in changes.items():
                setattr(record, field, value)
            await session.flush()
            return record

    async def create_revision(
        self,
        repository_id: str,
        *,
        revision: str,
        root_path: str,
        stats: dict[str, Any] | None = None,
    ) -> RepositoryRevisionRecord:
        async with self.database.session() as session:
            existing = await session.scalar(
                select(RepositoryRevisionRecord).where(
                    RepositoryRevisionRecord.repository_id == repository_id,
                    RepositoryRevisionRecord.revision == revision,
                )
            )
            if existing is not None:
                return existing
            record = RepositoryRevisionRecord(
                id=str(uuid4()),
                repository_id=repository_id,
                revision=revision,
                root_path=root_path,
                status="indexing",
                stats_json=stats or {},
            )
            session.add(record)
            await session.flush()
            return record

    async def begin_revision(self, revision_id: str) -> RepositoryRevisionRecord:
        """Start or retry an index build without changing the active ready revision."""

        async with self.database.session() as session:
            record = await session.get(RepositoryRevisionRecord, revision_id)
            if record is None:
                raise KeyError(revision_id)
            record.status = "indexing"
            record.stats_json = {}
            record.error_code = None
            record.completed_at = None
            await session.flush()
            return record

    async def reset_revision_documents(self, repository_id: str, revision_id: str) -> None:
        """Remove partial index writes before a revision build or retry."""

        document_ids = select(SourceDocumentRecord.id).where(
            SourceDocumentRecord.repository_id == repository_id,
            SourceDocumentRecord.revision_id == revision_id,
        )
        async with self.database.session() as session:
            await session.execute(
                delete(ChunkRecord).where(ChunkRecord.document_id.in_(document_ids))
            )
            await session.execute(
                delete(SourceDocumentRecord).where(
                    SourceDocumentRecord.repository_id == repository_id,
                    SourceDocumentRecord.revision_id == revision_id,
                )
            )

    async def finish_revision(
        self,
        revision_id: str,
        *,
        status: str,
        stats: dict[str, Any],
        error_code: str | None = None,
    ) -> RepositoryRevisionRecord:
        async with self.database.session() as session:
            record = await session.get(RepositoryRevisionRecord, revision_id)
            if record is None:
                raise KeyError(revision_id)
            record.status = status
            record.stats_json = stats
            record.error_code = error_code
            record.completed_at = datetime.now(UTC)
            if status == "ready":
                repository = await session.get(RepositoryRecord, record.repository_id)
                if repository is not None:
                    repository.indexed_revision_id = record.id
                    repository.status = "ready"
                    repository.last_error = None
            else:
                repository = await session.get(RepositoryRecord, record.repository_id)
                if repository is not None:
                    # A failed refresh must never evict the last known-good index.
                    repository.status = "ready" if repository.indexed_revision_id else "failed"
                    repository.last_error = error_code
            await session.flush()
            return record

    async def get_revision(self, revision_id: str) -> RepositoryRevisionRecord | None:
        async with self.database.session() as session:
            return await session.get(RepositoryRevisionRecord, revision_id)

    async def get_latest_ready_revision(
        self, repository_id: str
    ) -> RepositoryRevisionRecord | None:
        async with self.database.session() as session:
            result = await session.scalars(
                select(RepositoryRevisionRecord)
                .where(
                    RepositoryRevisionRecord.repository_id == repository_id,
                    RepositoryRevisionRecord.status == "ready",
                )
                .order_by(
                    RepositoryRevisionRecord.completed_at.desc(),
                    RepositoryRevisionRecord.created_at.desc(),
                )
                .limit(1)
            )
            return result.first()


@dataclass(frozen=True)
class ChunkRow:
    """A retrieval-ready chunk joined with its source document."""

    chunk_id: str
    document_id: str
    source_uri: str
    title: str
    content: str
    ordinal: int
    line_start: int | None
    line_end: int | None
    repository_id: str = LEGACY_REPOSITORY_ID
    revision_id: str | None = None


class DocumentStore:
    """Versioned source documents and their retrieval chunks."""

    def __init__(
        self,
        database: Database,
        repository_id: str = LEGACY_REPOSITORY_ID,
        revision_id: str | None = None,
    ) -> None:
        self.database = database
        self.repository_id = repository_id
        self.revision_id = revision_id

    def _scope(self) -> tuple[Any, ...]:
        if self.revision_id is None:
            return (SourceDocumentRecord.repository_id == self.repository_id,)
        return (
            SourceDocumentRecord.repository_id == self.repository_id,
            SourceDocumentRecord.revision_id == self.revision_id,
        )

    def scoped(self, repository_id: str, revision_id: str | None = None) -> DocumentStore:
        return DocumentStore(self.database, repository_id, revision_id)

    async def replace_revision_snapshot(self, source_revision_id: str | None) -> tuple[int, int]:
        """Atomically copy a ready revision into this revision.

        This is used for upload overlays: an uploaded document creates a new immutable revision
        rather than mutating the revision already bound to historical tasks. Retrying a failed
        overlay first clears the incomplete target snapshot.
        """

        if self.revision_id is None:
            raise ValueError("target revision is required")
        async with self.database.session() as session:
            target_ids = list(
                await session.scalars(
                    select(SourceDocumentRecord.id).where(
                        SourceDocumentRecord.repository_id == self.repository_id,
                        SourceDocumentRecord.revision_id == self.revision_id,
                    )
                )
            )
            if target_ids:
                await session.execute(
                    delete(ChunkRecord).where(ChunkRecord.document_id.in_(target_ids))
                )
                await session.execute(
                    delete(SourceDocumentRecord).where(SourceDocumentRecord.id.in_(target_ids))
                )
            if source_revision_id is None:
                return 0, 0

            source_documents = list(
                await session.scalars(
                    select(SourceDocumentRecord)
                    .where(
                        SourceDocumentRecord.repository_id == self.repository_id,
                        SourceDocumentRecord.revision_id == source_revision_id,
                    )
                    .order_by(SourceDocumentRecord.source_uri, SourceDocumentRecord.version)
                )
            )
            source_ids = [record.id for record in source_documents]
            chunks = (
                list(
                    await session.scalars(
                        select(ChunkRecord)
                        .where(ChunkRecord.document_id.in_(source_ids))
                        .order_by(ChunkRecord.document_id, ChunkRecord.ordinal)
                    )
                )
                if source_ids
                else []
            )
            document_ids: dict[str, str] = {}
            for source_document in source_documents:
                clone_id = str(uuid4())
                document_ids[source_document.id] = clone_id
                session.add(
                    SourceDocumentRecord(
                        id=clone_id,
                        repository_id=self.repository_id,
                        revision_id=self.revision_id,
                        source_uri=source_document.source_uri,
                        source_type=source_document.source_type,
                        title=source_document.title,
                        content_hash=source_document.content_hash,
                        version=source_document.version,
                        content=source_document.content,
                        metadata_json=dict(source_document.metadata_json),
                    )
                )
            for source_chunk in chunks:
                session.add(
                    ChunkRecord(
                        id=str(uuid4()),
                        document_id=document_ids[source_chunk.document_id],
                        ordinal=source_chunk.ordinal,
                        content=source_chunk.content,
                        token_count=source_chunk.token_count,
                        line_start=source_chunk.line_start,
                        line_end=source_chunk.line_end,
                        embedding_json=list(source_chunk.embedding_json),
                        metadata_json=dict(source_chunk.metadata_json),
                    )
                )
            await session.flush()
            return len(source_documents), len(chunks)

    async def upsert_document(
        self,
        *,
        source_uri: str,
        source_type: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[SourceDocumentRecord, bool]:
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        async with self.database.session() as session:
            latest_result = await session.scalars(
                select(SourceDocumentRecord)
                .where(*self._scope(), SourceDocumentRecord.source_uri == source_uri)
                .order_by(SourceDocumentRecord.version.desc())
                .limit(1)
            )
            latest = latest_result.first()
            if latest is not None and latest.content_hash == content_hash:
                return latest, False

            existing = await session.scalars(
                select(SourceDocumentRecord).where(
                    *self._scope(),
                    SourceDocumentRecord.source_uri == source_uri,
                    SourceDocumentRecord.content_hash == content_hash,
                )
            )
            found = existing.first()
            if found is not None:
                found.source_type = source_type
                found.title = title
                found.repository_id = self.repository_id
                found.revision_id = self.revision_id
                found.version = (latest.version if latest is not None else 0) + 1
                found.content = content
                found.metadata_json = metadata or {}
                found.created_at = utc_now()
                await session.flush()
                return found, True
            record = SourceDocumentRecord(
                id=str(uuid4()),
                repository_id=self.repository_id,
                revision_id=self.revision_id,
                source_uri=source_uri,
                source_type=source_type,
                title=title,
                content_hash=content_hash,
                version=(latest.version if latest is not None else 0) + 1,
                content=content,
                metadata_json=metadata or {},
            )
            session.add(record)
            await session.flush()
            return record, True

    async def replace_chunks(
        self,
        document_id: str,
        chunks: list[dict[str, Any]],
    ) -> list[ChunkRecord]:
        async with self.database.session() as session:
            await session.execute(delete(ChunkRecord).where(ChunkRecord.document_id == document_id))
            records = [
                ChunkRecord(
                    id=str(uuid4()),
                    document_id=document_id,
                    ordinal=ordinal,
                    content=chunk["content"],
                    token_count=chunk.get("token_count", 0),
                    line_start=chunk.get("line_start"),
                    line_end=chunk.get("line_end"),
                    metadata_json=chunk.get("metadata", {}),
                )
                for ordinal, chunk in enumerate(chunks, start=1)
            ]
            session.add_all(records)
            await session.flush()
            return records

    async def list_documents(self, *, limit: int = 500) -> list[SourceDocumentRecord]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(SourceDocumentRecord)
                .where(*self._scope())
                .order_by(SourceDocumentRecord.created_at.desc())
                .limit(limit)
            )
            return list(result)

    async def latest_document(self, source_uri: str) -> SourceDocumentRecord | None:
        async with self.database.session() as session:
            result = await session.scalars(
                select(SourceDocumentRecord)
                .where(*self._scope(), SourceDocumentRecord.source_uri == source_uri)
                .order_by(SourceDocumentRecord.version.desc())
                .limit(1)
            )
            return result.first()

    async def latest_documents(self, *, limit: int | None = None) -> list[SourceDocumentRecord]:
        """Return only the newest version of each source document."""
        async with self.database.session() as session:
            latest = (
                select(
                    SourceDocumentRecord.source_uri,
                    func.max(SourceDocumentRecord.version).label("max_version"),
                )
                .where(*self._scope())
                .group_by(SourceDocumentRecord.source_uri)
                .subquery()
            )
            query = (
                select(SourceDocumentRecord)
                .join(
                    latest,
                    (SourceDocumentRecord.source_uri == latest.c.source_uri)
                    & (SourceDocumentRecord.version == latest.c.max_version),
                )
                .where(*self._scope())
                .order_by(SourceDocumentRecord.source_uri)
            )
            if limit is not None:
                query = query.limit(limit)
            result = await session.scalars(query)
            return list(result)

    async def latest_documents_by_source_type(self, source_type: str) -> list[SourceDocumentRecord]:
        """Return the current document for each URI of one source type."""

        async with self.database.session() as session:
            latest = (
                select(
                    SourceDocumentRecord.source_uri,
                    func.max(SourceDocumentRecord.version).label("max_version"),
                )
                .where(*self._scope(), SourceDocumentRecord.source_type == source_type)
                .group_by(SourceDocumentRecord.source_uri)
                .subquery()
            )
            result = await session.scalars(
                select(SourceDocumentRecord)
                .join(
                    latest,
                    (SourceDocumentRecord.source_uri == latest.c.source_uri)
                    & (SourceDocumentRecord.version == latest.c.max_version),
                )
                .where(*self._scope(), SourceDocumentRecord.source_type == source_type)
                .order_by(SourceDocumentRecord.source_uri)
            )
            return list(result)

    async def latest_chunk_rows(self) -> list[ChunkRow]:
        """Chunks of the latest version of every source document."""
        async with self.database.session() as session:
            latest = (
                select(
                    SourceDocumentRecord.source_uri,
                    func.max(SourceDocumentRecord.version).label("max_version"),
                )
                .where(*self._scope())
                .group_by(SourceDocumentRecord.source_uri)
                .subquery()
            )
            result = await session.execute(
                select(ChunkRecord, SourceDocumentRecord)
                .join(
                    SourceDocumentRecord,
                    ChunkRecord.document_id == SourceDocumentRecord.id,
                )
                .join(
                    latest,
                    (SourceDocumentRecord.source_uri == latest.c.source_uri)
                    & (SourceDocumentRecord.version == latest.c.max_version),
                )
                .where(*self._scope())
                .order_by(SourceDocumentRecord.source_uri, ChunkRecord.ordinal)
            )
            return [
                ChunkRow(
                    chunk_id=chunk.id,
                    document_id=document.id,
                    source_uri=document.source_uri,
                    title=document.title,
                    content=chunk.content,
                    ordinal=chunk.ordinal,
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    repository_id=document.repository_id or LEGACY_REPOSITORY_ID,
                    revision_id=document.revision_id,
                )
                for chunk, document in result.all()
            ]


class EvidenceStore:
    """Evidence quotes that link report claims back to source chunks."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def add_evidence(
        self,
        *,
        task_id: str,
        repository_id: str | None = None,
        revision_id: str | None = None,
        claim: str,
        quote: str,
        source_uri: str,
        score: float,
        chunk_id: str | None = None,
        review_status: str = "pending",
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            id=str(uuid4()),
            task_id=task_id,
            repository_id=repository_id,
            revision_id=revision_id,
            chunk_id=chunk_id,
            claim=claim,
            quote=quote,
            source_uri=source_uri,
            score=score,
            review_status=review_status,
            metadata_json=metadata or {},
        )
        async with self.database.session() as session:
            session.add(record)
            await session.flush()
        return record

    async def list_evidence(self, task_id: str) -> list[EvidenceRecord]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(EvidenceRecord)
                .where(EvidenceRecord.task_id == task_id)
                .order_by(EvidenceRecord.created_at, EvidenceRecord.id)
            )
            return list(result)

    async def replace_task_evidence(
        self,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        repository_id: str | None = None,
        revision_id: str | None = None,
    ) -> list[EvidenceRecord]:
        """Idempotently replace a task's reviewed evidence snapshot."""
        async with self.database.session() as session:
            await session.execute(delete(EvidenceRecord).where(EvidenceRecord.task_id == task_id))
            records = [
                EvidenceRecord(
                    id=str(uuid4()),
                    task_id=task_id,
                    repository_id=repository_id or item.get("repository_id"),
                    revision_id=revision_id or item.get("revision_id"),
                    chunk_id=item.get("chunk_id"),
                    claim=str(item["claim"]),
                    quote=str(item["quote"]),
                    source_uri=str(item["source_uri"]),
                    score=float(item["score"]),
                    review_status=str(item["review_status"]),
                    metadata_json=dict(item.get("metadata") or {}),
                )
                for item in items
            ]
            session.add_all(records)
            await session.flush()
            return records

    async def set_review_status(self, evidence_id: str, review_status: str) -> None:
        async with self.database.session() as session:
            record = await session.get(EvidenceRecord, evidence_id)
            if record is None:
                raise KeyError(evidence_id)
            record.review_status = review_status
            await session.flush()


class MemoryStore:
    """Long-lived memories recalled across tasks."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def add_memory(
        self,
        *,
        memory_type: str,
        content: str,
        source: str,
        scope: str = "global",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryItemRecord:
        record = MemoryItemRecord(
            id=str(uuid4()),
            memory_type=memory_type,
            scope=scope,
            content=content,
            source=source,
            importance=importance,
            metadata_json=metadata or {},
            expires_at=expires_at,
        )
        async with self.database.session() as session:
            session.add(record)
            await session.flush()
        return record

    async def list_memories(
        self,
        *,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 100,
    ) -> list[MemoryItemRecord]:
        async with self.database.session() as session:
            query = select(MemoryItemRecord).order_by(
                MemoryItemRecord.importance.desc(), MemoryItemRecord.created_at.desc()
            )
            if scope is not None:
                query = query.where(MemoryItemRecord.scope == scope)
            if memory_type is not None:
                query = query.where(MemoryItemRecord.memory_type == memory_type)
            now = utc_now()
            query = query.where(
                (MemoryItemRecord.expires_at.is_(None)) | (MemoryItemRecord.expires_at > now)
            )
            result = await session.scalars(query.limit(limit))
            return list(result)

    async def prune_expired(self) -> int:
        async with self.database.session() as session:
            expired = await session.scalars(
                select(MemoryItemRecord.id).where(
                    MemoryItemRecord.expires_at.is_not(None),
                    MemoryItemRecord.expires_at <= utc_now(),
                )
            )
            ids = list(expired)
            if ids:
                await session.execute(delete(MemoryItemRecord).where(MemoryItemRecord.id.in_(ids)))
            return len(ids)
