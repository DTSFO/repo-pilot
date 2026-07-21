from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .retrieval import HybridRetriever, ScoredChunk
from .tools import ToolRegistry

MAX_TOOL_TOP_K = 10
TOOL_CONTENT_CHARS = 700


@dataclass
class RepositoryResearchTools:
    """Read-only repository tools plus the evidence collected through them."""

    retriever: HybridRetriever

    def __post_init__(self) -> None:
        self._rows = {row.chunk_id: row for row in self.retriever.rows}
        self._hits: dict[str, ScoredChunk] = {}
        self.registry = ToolRegistry()
        self.registry.register(
            "search_repository",
            "Search the ingested repository and return cited code/document chunks.",
            self.search_repository,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_TOP_K},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            read_only=True,
            idempotent=True,
            retryable=False,
        )
        self.registry.register(
            "read_repository_chunk",
            "Read one repository chunk returned by search_repository using its chunk_id.",
            self.read_repository_chunk,
            parameters={
                "type": "object",
                "properties": {"chunk_id": {"type": "string", "minLength": 1}},
                "required": ["chunk_id"],
                "additionalProperties": False,
            },
            read_only=True,
            idempotent=True,
            retryable=False,
        )

    @property
    def hits(self) -> list[ScoredChunk]:
        return sorted(
            self._hits.values(),
            key=lambda item: (-item.score, item.chunk.source_uri, item.chunk.ordinal),
        )

    def seed(self, hits: list[ScoredChunk]) -> None:
        for hit in hits:
            self._hits[hit.chunk.chunk_id] = hit

    def search_repository(self, query: str, top_k: int = 4) -> dict[str, Any]:
        normalized = query.strip()
        if not normalized:
            return {"query": query, "hits": []}
        limit = max(1, min(int(top_k), MAX_TOOL_TOP_K))
        hits = self.retriever.search(normalized, top_k=limit)
        for hit in hits:
            current = self._hits.get(hit.chunk.chunk_id)
            if current is None or hit.score > current.score:
                self._hits[hit.chunk.chunk_id] = hit
        return {
            "query": normalized,
            "hits": [self._serialize_hit(hit, include_content=True) for hit in hits],
        }

    def read_repository_chunk(self, chunk_id: str) -> dict[str, Any]:
        row = self._rows.get(chunk_id)
        if row is None:
            return {"found": False, "chunk_id": chunk_id}
        scored = self._hits.get(chunk_id)
        citation = row.source_uri
        if row.line_start is not None and row.line_end is not None:
            citation = f"{row.source_uri}:L{row.line_start}-L{row.line_end}"
        return {
            "found": True,
            "chunk_id": row.chunk_id,
            "citation": citation,
            "source_uri": row.source_uri,
            "content": row.content,
            "score": scored.score if scored is not None else None,
            "coverage": scored.coverage if scored is not None else None,
        }

    @staticmethod
    def _serialize_hit(hit: ScoredChunk, *, include_content: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chunk_id": hit.chunk.chunk_id,
            "citation": hit.citation,
            "source_uri": hit.chunk.source_uri,
            "score": hit.score,
            "coverage": hit.coverage,
        }
        if include_content:
            content = hit.chunk.content.strip()
            if len(content) > TOOL_CONTENT_CHARS:
                content = content[:TOOL_CONTENT_CHARS].rstrip() + "…"
            payload["content"] = content
        return payload
