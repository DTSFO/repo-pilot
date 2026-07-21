from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .errors import RepoPilotError
from .ingestion import DocumentTooLargeError, RepositoryIngestor, chunk_lines
from .observability import HTTP_REQUESTS, REQUEST_LATENCY, configure_logging, metrics_payload
from .providers.factory import build_provider
from .retrieval import HybridRetriever
from .service import TERMINAL_STATUSES, TaskService
from .storage.database import Database
from .storage.models import (
    EvidenceRecord,
    MemoryItemRecord,
    ResearchTaskRecord,
    TaskEventRecord,
)
from .storage.repositories import DocumentStore, EvidenceStore, MemoryStore, TaskStore
from .workflow import ResearchWorkflow

STREAM_POLL_SECONDS = 0.2


class AuthenticationError(RepoPilotError):
    code = "unauthorized"
    safe_message = "A valid API token is required."
    http_status = HTTPStatus.UNAUTHORIZED


class CreateTaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: str
    current_node: str | None
    final_report: str | None
    error_code: str | None
    degraded: bool
    version: int

    @classmethod
    def from_record(cls, record: ResearchTaskRecord) -> TaskResponse:
        return cls(
            id=record.id,
            goal=record.goal,
            status=record.status,
            current_node=record.current_node,
            final_report=record.final_report,
            error_code=record.error_code,
            degraded=record.degraded,
            version=record.version,
        )


class IngestRequest(BaseModel):
    path: str | None = Field(default=None, max_length=1024)


class IngestResponse(BaseModel):
    scanned_files: int
    ingested_documents: int
    unchanged_documents: int
    skipped_files: int
    chunks: int


class SearchHit(BaseModel):
    citation: str
    source_uri: str
    score: float
    content: str


class EvidenceResponse(BaseModel):
    id: str
    claim: str
    quote: str
    source_uri: str
    citation: str
    score: float
    review_status: str

    @classmethod
    def from_record(cls, record: EvidenceRecord) -> EvidenceResponse:
        return cls(
            id=record.id,
            claim=record.claim,
            quote=record.quote,
            source_uri=record.source_uri,
            citation=str(record.metadata_json.get("citation", record.source_uri)),
            score=record.score,
            review_status=record.review_status,
        )


class MemoryCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    memory_type: str = Field(default="note", max_length=24)
    scope: str = Field(default="global", max_length=128)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class MemoryResponse(BaseModel):
    id: str
    memory_type: str
    scope: str
    content: str
    source: str
    importance: float

    @classmethod
    def from_record(cls, record: MemoryItemRecord) -> MemoryResponse:
        return cls(
            id=record.id,
            memory_type=record.memory_type,
            scope=record.scope,
            content=record.content,
            source=record.source,
            importance=record.importance,
        )


class TaskEventResponse(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, Any]

    @classmethod
    def from_record(cls, record: TaskEventRecord) -> TaskEventResponse:
        return cls(
            sequence=record.sequence,
            event_type=record.event_type,
            payload=record.payload_json,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(app_settings.log_level)
        database = Database(app_settings.database_url)
        await database.initialize()
        provider = build_provider(app_settings)
        documents = DocumentStore(database)
        evidence = EvidenceStore(database)
        memory = MemoryStore(database)
        workflow = ResearchWorkflow(provider, documents, evidence, app_settings, memory=memory)
        service = TaskService(TaskStore(database), workflow)
        app.state.database = database
        app.state.provider = provider
        app.state.service = service
        app.state.documents = documents
        app.state.evidence = evidence
        app.state.ingestor = RepositoryIngestor(documents, app_settings)
        app.state.memory = memory
        try:
            yield
        finally:
            await service.shutdown()
            await provider.close()
            await database.close()

    app = FastAPI(title="RepoPilot", version="1.2.0", lifespan=lifespan)

    @app.middleware("http")
    async def observe_requests(request: Request, call_next: Any) -> Any:
        started = perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(perf_counter() - started)
        return response

    def get_service(request: Request) -> TaskService:
        service: TaskService = request.app.state.service
        return service

    async def require_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = app_settings.api_token
        if expected is None:
            return
        token = expected.get_secret_value()
        if authorization != f"Bearer {token}":
            raise AuthenticationError()

    Authorized = Depends(require_token)

    @app.exception_handler(RepoPilotError)
    async def repopilot_error_handler(request: Request, exc: RepoPilotError) -> JSONResponse:
        return JSONResponse(
            status_code=int(exc.http_status),
            content={"error": {"code": exc.code, "message": exc.safe_message}},
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        payload, content_type = metrics_payload()
        return Response(content=payload, media_type=content_type)

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text("utf-8"))

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        database: Database = request.app.state.database
        async with database.session() as session:
            await session.connection()
        return {"status": "ready"}

    @app.post("/api/tasks", status_code=202, dependencies=[Authorized])
    async def create_task(
        payload: CreateTaskRequest, service: TaskService = Depends(get_service)
    ) -> TaskResponse:
        record = await service.create_task(payload.goal)
        return TaskResponse.from_record(record)

    @app.get("/api/tasks", dependencies=[Authorized])
    async def list_tasks(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        service: TaskService = Depends(get_service),
    ) -> list[TaskResponse]:
        return [
            TaskResponse.from_record(record) for record in await service.list_tasks(limit=limit)
        ]

    @app.get("/api/tasks/{task_id}", dependencies=[Authorized])
    async def get_task(task_id: str, service: TaskService = Depends(get_service)) -> TaskResponse:
        return TaskResponse.from_record(await service.get_task(task_id))

    @app.get("/api/tasks/{task_id}/events", dependencies=[Authorized])
    async def list_events(
        task_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        service: TaskService = Depends(get_service),
    ) -> list[TaskEventResponse]:
        await service.get_task(task_id)
        events = await service.store.list_events(task_id, after_sequence=after)
        return [TaskEventResponse.from_record(event) for event in events]

    @app.post("/api/tasks/{task_id}/resume", status_code=202, dependencies=[Authorized])
    async def resume_task(
        task_id: str, service: TaskService = Depends(get_service)
    ) -> TaskResponse:
        return TaskResponse.from_record(await service.resume_task(task_id))

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Authorized])
    async def cancel_task(
        task_id: str, service: TaskService = Depends(get_service)
    ) -> TaskResponse:
        return TaskResponse.from_record(await service.cancel_task(task_id))

    @app.post("/api/ingest", dependencies=[Authorized])
    async def ingest(payload: IngestRequest, request: Request) -> IngestResponse:
        ingestor: RepositoryIngestor = request.app.state.ingestor
        report = await ingestor.ingest_path(payload.path)
        return IngestResponse(
            scanned_files=report.scanned_files,
            ingested_documents=report.ingested_documents,
            unchanged_documents=report.unchanged_documents,
            skipped_files=report.skipped_files,
            chunks=report.chunks,
        )

    @app.post("/api/documents", status_code=201, dependencies=[Authorized])
    async def upload_document(
        request: Request,
        file: UploadFile = File(...),
    ) -> IngestResponse:
        raw = await file.read()
        if len(raw) > app_settings.max_upload_bytes:
            raise DocumentTooLargeError(details={"size": len(raw)})
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepoPilotError(details={"reason": "not_utf8"}) from exc
        documents: DocumentStore = request.app.state.documents
        name = file.filename or "upload.txt"
        document, created = await documents.upsert_document(
            source_uri=f"uploads/{name}",
            source_type="upload",
            title=name,
            content=content,
            metadata={"content_type": file.content_type},
        )
        chunks = chunk_lines(content) if created else []
        if created:
            await documents.replace_chunks(document.id, chunks)
        return IngestResponse(
            scanned_files=1,
            ingested_documents=int(created),
            unchanged_documents=int(not created),
            skipped_files=0,
            chunks=len(chunks),
        )

    @app.get("/api/search", dependencies=[Authorized])
    async def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=512)],
        top_k: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> list[SearchHit]:
        documents: DocumentStore = request.app.state.documents
        retriever = HybridRetriever(await documents.latest_chunk_rows())
        return [
            SearchHit(
                citation=scored.citation,
                source_uri=scored.chunk.source_uri,
                score=scored.score,
                content=scored.chunk.content,
            )
            for scored in retriever.search(q, top_k=top_k)
        ]

    @app.get("/api/tasks/{task_id}/evidence", dependencies=[Authorized])
    async def list_task_evidence(
        task_id: str,
        request: Request,
        service: TaskService = Depends(get_service),
    ) -> list[EvidenceResponse]:
        await service.get_task(task_id)
        evidence: EvidenceStore = request.app.state.evidence
        return [
            EvidenceResponse.from_record(record) for record in await evidence.list_evidence(task_id)
        ]

    @app.get("/api/memory", dependencies=[Authorized])
    async def list_memory(
        request: Request,
        memory_type: Annotated[str | None, Query(max_length=24)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[MemoryResponse]:
        memory: MemoryStore = request.app.state.memory
        records = await memory.list_memories(memory_type=memory_type, limit=limit)
        return [MemoryResponse.from_record(record) for record in records]

    @app.post("/api/memory", status_code=201, dependencies=[Authorized])
    async def create_memory(payload: MemoryCreateRequest, request: Request) -> MemoryResponse:
        memory: MemoryStore = request.app.state.memory
        record = await memory.add_memory(
            memory_type=payload.memory_type,
            content=payload.content,
            source="api",
            scope=payload.scope,
            importance=payload.importance,
        )
        return MemoryResponse.from_record(record)

    @app.get("/api/tasks/{task_id}/stream", dependencies=[Authorized])
    async def stream_task(
        task_id: str,
        last_event_id: Annotated[str | None, Header()] = None,
        after: Annotated[int, Query(ge=0)] = 0,
        service: TaskService = Depends(get_service),
    ) -> StreamingResponse:
        await service.get_task(task_id)
        cursor = after
        if last_event_id and last_event_id.isdigit():
            cursor = max(cursor, int(last_event_id))

        async def event_stream(start: int) -> AsyncIterator[str]:
            position = start
            while True:
                events = await service.store.list_events(task_id, after_sequence=position)
                for event in events:
                    position = event.sequence
                    payload = json.dumps(
                        {
                            "sequence": event.sequence,
                            "event_type": event.event_type,
                            "payload": event.payload_json,
                        },
                        ensure_ascii=False,
                    )
                    yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"
                record = await service.get_task(task_id)
                if record.status in TERMINAL_STATUSES and not service.is_running(task_id):
                    remaining = await service.store.list_events(task_id, after_sequence=position)
                    if not remaining:
                        yield "event: stream.end\ndata: {}\n\n"
                        return
                    continue
                await asyncio.sleep(STREAM_POLL_SECONDS)

        return StreamingResponse(
            event_stream(cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
