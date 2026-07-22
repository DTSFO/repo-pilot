from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .errors import RepoPilotError
from .ingestion import DocumentTooLargeError, RepositoryIngestor
from .observability import HTTP_REQUESTS, REQUEST_LATENCY, configure_logging, metrics_payload
from .providers.factory import build_provider
from .reporting import export_html, export_json, render_markdown
from .repository_manager import (
    RepositoryManager,
    RepositoryNotFoundError,
    RepositoryRequestError,
)
from .retrieval import HybridRetriever
from .service import TERMINAL_STATUSES, TaskService
from .storage.database import Database
from .storage.models import (
    EvidenceRecord,
    MemoryItemRecord,
    RepositoryRecord,
    RepositoryRevisionRecord,
    ResearchTaskRecord,
    TaskEventRecord,
)
from .storage.repositories import (
    DocumentStore,
    EvidenceStore,
    MemoryStore,
    RepositoryStore,
    TaskStore,
)
from .workflow import ResearchWorkflow


class AuthenticationError(RepoPilotError):
    code = "unauthorized"
    safe_message = "A valid API token is required."
    http_status = HTTPStatus.UNAUTHORIZED


class RepositoryRequiredError(RepoPilotError):
    code = "repository_required"
    safe_message = "Select a repository before using this operation."
    http_status = HTTPStatus.BAD_REQUEST


class ReportNotReadyError(RepoPilotError):
    code = "report_not_ready"
    safe_message = "The task does not have an exportable report yet."
    http_status = HTTPStatus.CONFLICT


class UnsupportedExportError(RepoPilotError):
    code = "unsupported_export_format"
    safe_message = "The requested report format is not supported."
    http_status = HTTPStatus.BAD_REQUEST


class CreateTaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    repository_id: str | None = Field(default=None, min_length=1, max_length=36)


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: str
    current_node: str | None
    final_report: str | None
    error_code: str | None
    degraded: bool
    version: int
    repository_id: str | None
    revision_id: str | None

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
            repository_id=record.repository_id,
            revision_id=record.revision_id,
        )


class TaskSummaryResponse(BaseModel):
    id: str
    goal: str
    status: str
    current_node: str | None
    error_code: str | None
    degraded: bool
    version: int
    repository_id: str | None
    revision_id: str | None
    has_report: bool

    @classmethod
    def from_record(cls, record: ResearchTaskRecord) -> TaskSummaryResponse:
        return cls(
            id=record.id,
            goal=record.goal,
            status=record.status,
            current_node=record.current_node,
            error_code=record.error_code,
            degraded=record.degraded,
            version=record.version,
            repository_id=record.repository_id,
            revision_id=record.revision_id,
            has_report=record.final_report is not None,
        )


class RepositoryResponse(BaseModel):
    id: str
    name: str
    source_type: str
    source_location: str
    status: str
    indexed_revision_id: str | None
    indexed_revision: str | None = None
    indexed_at: datetime | None = None
    last_error: str | None = None

    @classmethod
    def from_record(
        cls,
        record: RepositoryRecord,
        revision: RepositoryRevisionRecord | None = None,
    ) -> RepositoryResponse:
        return cls(
            id=record.id,
            name=record.name,
            source_type=record.source_type,
            source_location=record.source_location,
            status=record.status,
            indexed_revision_id=revision.id if revision else record.indexed_revision_id,
            indexed_revision=revision.revision if revision else None,
            indexed_at=revision.completed_at if revision else None,
            last_error=record.last_error,
        )


class CreateRepositoryRequest(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    local_path: str | None = Field(default=None, max_length=2048)
    git_url: str | None = Field(default=None, max_length=2048)


class IngestRequest(BaseModel):
    path: str | None = Field(default=None, max_length=1024)
    repository_id: str | None = Field(default=None, min_length=1, max_length=36)


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


class ReportResponse(BaseModel):
    task_id: str
    repository_id: str | None
    revision_id: str | None
    markdown: str
    html: str


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
    created_at: datetime

    @classmethod
    def from_record(cls, record: TaskEventRecord) -> TaskEventResponse:
        return cls(
            sequence=record.sequence,
            event_type=record.event_type,
            payload=record.payload_json,
            created_at=record.created_at,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(app_settings.log_level)
        database = Database(app_settings.database_url)
        await database.initialize(legacy_root=str(app_settings.resolved_workspace_root))
        provider = build_provider(app_settings)
        repositories = RepositoryStore(database)
        await repositories.ensure_legacy(str(app_settings.resolved_workspace_root))
        documents = DocumentStore(database)
        evidence = EvidenceStore(database)
        memory = MemoryStore(database)
        workflow = ResearchWorkflow(provider, documents, evidence, app_settings, memory=memory)
        service = TaskService(TaskStore(database), workflow)
        repository_manager = RepositoryManager(database, app_settings)
        app.state.database = database
        app.state.provider = provider
        app.state.service = service
        app.state.documents = documents
        app.state.evidence = evidence
        app.state.ingestor = RepositoryIngestor(documents, app_settings)
        app.state.repositories = repositories
        app.state.repository_manager = repository_manager
        app.state.memory = memory
        try:
            yield
        finally:
            await service.shutdown()
            await provider.close()
            await database.close()

    app = FastAPI(title="RepoPilot", version="1.4.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.middleware("http")
    async def observe_requests(request: Request, call_next: Any) -> Any:
        started = perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(perf_counter() - started)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'",
        )
        return response

    def get_service(request: Request) -> TaskService:
        service: TaskService = request.app.state.service
        return service

    def get_repository_manager(request: Request) -> RepositoryManager:
        return cast(RepositoryManager, request.app.state.repository_manager)

    async def resolve_repository(request: Request, repository_id: str | None) -> RepositoryRecord:
        store: RepositoryStore = request.app.state.repositories
        if repository_id:
            repository = await store.get_repository(repository_id)
        else:
            active = await store.list_repositories()
            repository = active[0] if len(active) == 1 else None
        if repository is None or repository.status == "archived":
            if repository_id:
                raise RepositoryNotFoundError(details={"repository_id": repository_id})
            raise RepositoryRequiredError(details={"repository_id": repository_id})
        return repository

    async def repository_response(
        store: RepositoryStore, repository: RepositoryRecord
    ) -> RepositoryResponse:
        revision = (
            await store.get_revision(repository.indexed_revision_id)
            if repository.indexed_revision_id
            else None
        )
        if (
            revision is None
            or revision.repository_id != repository.id
            or revision.status != "ready"
        ):
            revision = await store.get_latest_ready_revision(repository.id)
        return RepositoryResponse.from_record(repository, revision)

    async def resolve_indexed_revision(
        request: Request, repository: RepositoryRecord
    ) -> RepositoryRevisionRecord | None:
        store: RepositoryStore = request.app.state.repositories
        revision = (
            await store.get_revision(repository.indexed_revision_id)
            if repository.indexed_revision_id
            else None
        )
        if (
            revision is None
            or revision.repository_id != repository.id
            or revision.status != "ready"
        ):
            revision = await store.get_latest_ready_revision(repository.id)
        if revision is None and not repository.metadata_json.get("legacy"):
            raise RepositoryRequiredError(details={"reason": "repository_not_indexed"})
        return revision

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

    @app.get(
        "/api/repositories", response_model=list[RepositoryResponse], dependencies=[Authorized]
    )
    async def list_repositories(request: Request) -> list[RepositoryResponse]:
        store: RepositoryStore = request.app.state.repositories
        records = await store.list_repositories()
        return [await repository_response(store, record) for record in records]

    @app.post("/api/repositories", status_code=201, dependencies=[Authorized])
    async def create_repository(
        payload: CreateRepositoryRequest,
        request: Request,
        manager: RepositoryManager = Depends(get_repository_manager),
    ) -> RepositoryResponse:
        if bool(payload.local_path) == bool(payload.git_url):
            raise RepositoryRequestError(details={"reason": "exactly_one_source_required"})
        record = (
            await manager.add_local(payload.local_path or "", name=payload.name)
            if payload.local_path
            else await manager.add_git(payload.git_url or "", name=payload.name)
        )
        await manager.refresh(record.id)
        store: RepositoryStore = request.app.state.repositories
        refreshed = await store.get_repository(record.id)
        if refreshed is None:
            raise RepositoryNotFoundError(details={"repository_id": record.id})
        return await repository_response(store, refreshed)

    @app.post("/api/repositories/{repository_id}/sync", dependencies=[Authorized])
    async def sync_repository(
        repository_id: str,
        request: Request,
        manager: RepositoryManager = Depends(get_repository_manager),
    ) -> RepositoryResponse:
        await manager.refresh(repository_id)
        store: RepositoryStore = request.app.state.repositories
        record = await store.get_repository(repository_id)
        if record is None:
            raise RepositoryNotFoundError(details={"repository_id": repository_id})
        return await repository_response(store, record)

    @app.delete("/api/repositories/{repository_id}", status_code=204, dependencies=[Authorized])
    async def archive_repository(
        repository_id: str,
        manager: RepositoryManager = Depends(get_repository_manager),
    ) -> Response:
        await manager.archive(repository_id)
        return Response(status_code=204)

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
        payload: CreateTaskRequest,
        request: Request,
        service: TaskService = Depends(get_service),
    ) -> TaskResponse:
        repository = await resolve_repository(request, payload.repository_id)
        revision = await resolve_indexed_revision(request, repository)
        record = await service.create_task(
            payload.goal,
            repository_id=repository.id,
            revision_id=revision.id if revision else None,
        )
        return TaskResponse.from_record(record)

    @app.get("/api/tasks", response_model=list[TaskSummaryResponse], dependencies=[Authorized])
    async def list_tasks(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        repository_id: Annotated[str | None, Query(max_length=36)] = None,
        service: TaskService = Depends(get_service),
    ) -> list[TaskSummaryResponse]:
        if repository_id:
            await resolve_repository(request, repository_id)
        return [
            TaskSummaryResponse.from_record(record)
            for record in await service.list_tasks(limit=limit, repository_id=repository_id)
        ]

    @app.get("/api/tasks/{task_id}", dependencies=[Authorized])
    async def get_task(task_id: str, service: TaskService = Depends(get_service)) -> TaskResponse:
        return TaskResponse.from_record(await service.get_task(task_id))

    @app.get(
        "/api/tasks/{task_id}/report", response_model=ReportResponse, dependencies=[Authorized]
    )
    async def get_report(
        task_id: str, service: TaskService = Depends(get_service)
    ) -> ReportResponse:
        record = await service.get_task(task_id)
        if record.status not in {"completed", "guarded"} or record.final_report is None:
            raise ReportNotReadyError(details={"task_id": task_id})
        return ReportResponse(
            task_id=record.id,
            repository_id=record.repository_id,
            revision_id=record.revision_id,
            markdown=record.final_report,
            html=render_markdown(record.final_report),
        )

    @app.get("/api/tasks/{task_id}/exports/{export_format}", dependencies=[Authorized])
    async def export_report(
        task_id: str,
        export_format: str,
        request: Request,
        service: TaskService = Depends(get_service),
    ) -> Response:
        record = await service.get_task(task_id)
        if record.status not in {"completed", "guarded"} or record.final_report is None:
            raise ReportNotReadyError(details={"task_id": task_id})
        if export_format == "md":
            body, media_type, extension = record.final_report, "text/markdown", "md"
        elif export_format == "html":
            body, media_type, extension = (
                export_html(record.final_report),
                "text/html",
                "html",
            )
        elif export_format == "json":
            evidence_store: EvidenceStore = request.app.state.evidence
            evidence = [
                {
                    "citation": str(item.metadata_json.get("citation", item.source_uri)),
                    "claim": item.claim,
                    "quote": item.quote,
                    "source_uri": item.source_uri,
                    "score": item.score,
                    "review_status": item.review_status,
                }
                for item in await evidence_store.list_evidence(task_id)
            ]
            body, media_type, extension = (
                export_json(
                    record.final_report,
                    metadata={
                        "task_id": record.id,
                        "status": record.status,
                        "degraded": record.degraded,
                        "repository_id": record.repository_id,
                        "revision_id": record.revision_id,
                    },
                    evidence=evidence,
                ),
                "application/json",
                "json",
            )
        else:
            raise UnsupportedExportError(details={"format": export_format})
        safe_task_id = "".join(
            character for character in record.id if character.isascii() and character.isalnum()
        )[:64]
        filename = f"repopilot-{safe_task_id or 'report'}.{extension}"
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

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
        repository = await resolve_repository(request, payload.repository_id)
        manager: RepositoryManager = request.app.state.repository_manager
        if payload.path is None:
            revision = await manager.refresh(repository.id)
            stats = revision.stats_json
            return IngestResponse(
                scanned_files=int(stats.get("scanned_files", stats.get("scanned", 0))),
                ingested_documents=int(stats.get("ingested_documents", stats.get("ingested", 0))),
                unchanged_documents=int(
                    stats.get("unchanged_documents", stats.get("unchanged", 0))
                ),
                skipped_files=int(stats.get("skipped_files", stats.get("skipped", 0))),
                chunks=int(stats.get("chunks", 0)),
            )
        # A partial path is a validation/filter hint only. Rebuild the complete
        # snapshot so an immutable revision can never contain a partial repository.
        RepositoryIngestor(
            DocumentStore(request.app.state.database, repository.id),
            app_settings,
            root_path=Path(repository.root_path),
        ).resolve_safe_path(payload.path)
        report = await manager.refresh(repository.id)
        stats = report.stats_json
        return IngestResponse(
            scanned_files=int(stats.get("scanned_files", 0)),
            ingested_documents=int(stats.get("ingested_documents", 0)),
            unchanged_documents=int(stats.get("unchanged_documents", 0)),
            skipped_files=int(stats.get("skipped_files", 0)),
            chunks=int(stats.get("chunks", 0)),
        )

    @app.post("/api/documents", status_code=201, dependencies=[Authorized])
    async def upload_document(
        request: Request,
        file: UploadFile = File(...),
        repository_id: Annotated[str | None, Query(max_length=36)] = None,
    ) -> IngestResponse:
        repository = await resolve_repository(request, repository_id)
        await resolve_indexed_revision(request, repository)
        raw = await file.read()
        if len(raw) > app_settings.max_upload_bytes:
            raise DocumentTooLargeError(details={"size": len(raw)})
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepoPilotError(details={"reason": "not_utf8"}) from exc
        manager: RepositoryManager = request.app.state.repository_manager
        result = await manager.add_document(
            repository.id,
            name=file.filename or "upload.txt",
            content=content,
            content_type=file.content_type,
        )
        return IngestResponse(
            scanned_files=1,
            ingested_documents=int(result.created),
            unchanged_documents=int(not result.created),
            skipped_files=0,
            chunks=result.chunks,
        )

    @app.get("/api/search", dependencies=[Authorized])
    async def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=512)],
        top_k: Annotated[int, Query(ge=1, le=20)] = 5,
        repository_id: Annotated[str | None, Query(max_length=36)] = None,
    ) -> list[SearchHit]:
        repository = await resolve_repository(request, repository_id)
        revision = await resolve_indexed_revision(request, repository)
        documents = DocumentStore(
            request.app.state.database, repository.id, revision.id if revision else None
        )
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
            last_delivery = perf_counter()
            while True:
                events = await service.store.list_events(task_id, after_sequence=position)
                for event in events:
                    position = event.sequence
                    payload = json.dumps(
                        {
                            "sequence": event.sequence,
                            "event_type": event.event_type,
                            "payload": event.payload_json,
                            "created_at": event.created_at.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                    yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"
                    last_delivery = perf_counter()
                record = await service.get_task(task_id)
                if record.status in TERMINAL_STATUSES and not service.is_running(task_id):
                    remaining = await service.store.list_events(task_id, after_sequence=position)
                    if not remaining:
                        yield "event: stream.end\ndata: {}\n\n"
                        return
                    continue
                if perf_counter() - last_delivery >= app_settings.sse_heartbeat_seconds:
                    # A transport heartbeat proves only that RepoPilot and the SSE connection are
                    # alive. Provider progress is persisted separately as provider.request.*.
                    yield ": keep-alive\n\n"
                    last_delivery = perf_counter()
                await asyncio.sleep(app_settings.sse_poll_seconds)

        return StreamingResponse(
            event_stream(cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return app
