from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .database import Database
from .models import (
    CheckpointRecord,
    ChunkRecord,
    EvidenceRecord,
    MemoryItemRecord,
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
        constraints: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> ResearchTaskRecord:
        record = ResearchTaskRecord(
            id=str(uuid4()),
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

    async def list_tasks(self, *, limit: int = 50) -> list[ResearchTaskRecord]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(ResearchTaskRecord)
                .order_by(ResearchTaskRecord.created_at.desc())
                .limit(limit)
            )
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


class DocumentStore:
    """Versioned source documents and their retrieval chunks."""

    def __init__(self, database: Database) -> None:
        self.database = database

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
                .where(SourceDocumentRecord.source_uri == source_uri)
                .order_by(SourceDocumentRecord.version.desc())
                .limit(1)
            )
            latest = latest_result.first()
            if latest is not None and latest.content_hash == content_hash:
                return latest, False

            existing = await session.scalars(
                select(SourceDocumentRecord).where(
                    SourceDocumentRecord.source_uri == source_uri,
                    SourceDocumentRecord.content_hash == content_hash,
                )
            )
            found = existing.first()
            if found is not None:
                found.source_type = source_type
                found.title = title
                found.version = (latest.version if latest is not None else 0) + 1
                found.content = content
                found.metadata_json = metadata or {}
                found.created_at = utc_now()
                await session.flush()
                return found, True
            record = SourceDocumentRecord(
                id=str(uuid4()),
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
                .order_by(SourceDocumentRecord.created_at.desc())
                .limit(limit)
            )
            return list(result)

    async def latest_documents(self, *, limit: int | None = None) -> list[SourceDocumentRecord]:
        """Return only the newest version of each source document."""
        async with self.database.session() as session:
            latest = (
                select(
                    SourceDocumentRecord.source_uri,
                    func.max(SourceDocumentRecord.version).label("max_version"),
                )
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
                .order_by(SourceDocumentRecord.source_uri)
            )
            if limit is not None:
                query = query.limit(limit)
            result = await session.scalars(query)
            return list(result)

    async def latest_chunk_rows(self) -> list[ChunkRow]:
        """Chunks of the latest version of every source document."""
        async with self.database.session() as session:
            latest = (
                select(
                    SourceDocumentRecord.source_uri,
                    func.max(SourceDocumentRecord.version).label("max_version"),
                )
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
    ) -> list[EvidenceRecord]:
        """Idempotently replace a task's reviewed evidence snapshot."""
        async with self.database.session() as session:
            await session.execute(delete(EvidenceRecord).where(EvidenceRecord.task_id == task_id))
            records = [
                EvidenceRecord(
                    id=str(uuid4()),
                    task_id=task_id,
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
