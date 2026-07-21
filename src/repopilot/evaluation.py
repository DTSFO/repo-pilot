from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from typing import Any
from uuid import uuid4

from .config import Settings
from .ingestion import RepositoryIngestor
from .models import AgentRunResult, ModelResponse
from .providers.base import ModelProvider, ModelRequest, ProviderHealth
from .storage.database import Database
from .storage.models import EvaluationRunRecord
from .storage.repositories import DocumentStore, EvidenceStore
from .workflow import ResearchWorkflow

CITATION_PATTERN = re.compile(r"`([^`\s]+):L(\d+)-L(\d+)`")
RECALL_K = 5
EVALUATION_SCHEMA_VERSION = "1.1"


@dataclass(frozen=True)
class EvalCase:
    id: str
    goal: str
    expected_source: str | None = None
    expected_keywords: list[str] = field(default_factory=list)
    expect_refusal: bool = False


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    completed: bool
    refused: bool
    recall_hit: bool | None
    citations_total: int
    citations_resolvable: int
    keywords_hit: bool
    latency_ms: float
    degraded: bool
    degraded_reasons: tuple[str, ...]
    provider_calls: int
    fallback_responses: int
    revision_requests: int
    revision_rounds: int
    revision_limit_reached: bool

    @property
    def fallback_used(self) -> bool:
        return self.fallback_responses > 0

    @property
    def revision_requested(self) -> bool:
        return self.revision_requests > 0

    @property
    def revision_executed(self) -> bool:
        return self.revision_rounds > 0

    @property
    def success(self) -> bool:
        if not self.completed:
            return False
        if self.recall_hit is None:
            return self.refused
        return self.recall_hit and self.keywords_hit and not self.refused


def _parse_dataset(raw: Any) -> list[EvalCase]:
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise ValueError("Evaluation dataset must contain a cases list")
    cases = [EvalCase(**item) for item in raw["cases"]]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Evaluation case ids must be unique")
    for case in cases:
        if case.expect_refusal and case.expected_source is not None:
            raise ValueError(f"Evaluation case {case.id!r} cannot expect source and refusal")
        if not case.expect_refusal and case.expected_source is None:
            raise ValueError(
                f"Evaluation case {case.id!r} needs expected_source or expect_refusal=true"
            )
    return cases


def load_dataset(path: Path) -> list[EvalCase]:
    return _parse_dataset(json.loads(path.read_text(encoding="utf-8")))


class _ObservedProvider:
    """Forward provider calls while recording evaluation-observable response metadata."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.name = provider.name
        self.calls = 0
        self.fallback_responses = 0
        self.models: set[str] = set()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        response = await self.provider.complete(request)
        if response.fallback_used:
            self.fallback_responses += 1
        if response.model:
            self.models.add(response.model)
        return response

    async def health(self) -> ProviderHealth:
        return await self.provider.health()

    async def close(self) -> None:
        await self.provider.close()


class EvaluationRunner:
    """Fixed-dataset evaluation of the research workflow over a real corpus."""

    def __init__(self, settings: Settings, database: Database, provider: ModelProvider) -> None:
        self.settings = settings
        self.database = database
        self.provider = provider
        self.documents = DocumentStore(database)

    async def run(self, dataset_path: Path, *, dataset_name: str | None = None) -> dict[str, Any]:
        dataset_bytes = await asyncio.to_thread(dataset_path.read_bytes)
        dataset_payload = json.loads(dataset_bytes)
        cases = _parse_dataset(dataset_payload)
        generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        dataset_metadata = {
            "name": dataset_name or dataset_path.stem,
            "path": str(dataset_path),
            "schema_version": dataset_payload.get("schema_version"),
            "fingerprint": sha256(dataset_bytes).hexdigest(),
            "cases": len(cases),
        }
        await RepositoryIngestor(self.documents, self.settings).ingest_path()
        latest_documents = await self.documents.latest_documents()
        line_counts = {
            document.source_uri: document.content.count("\n") + 1 for document in latest_documents
        }
        fingerprint = sha256()
        for document in latest_documents:
            fingerprint.update(document.source_uri.encode("utf-8"))
            fingerprint.update(b"\0")
            fingerprint.update(document.content_hash.encode("ascii"))
            fingerprint.update(b"\n")
        corpus = {
            "documents": len(latest_documents),
            "fingerprint": fingerprint.hexdigest(),
        }
        observed_provider = _ObservedProvider(self.provider)
        workflow = ResearchWorkflow(
            observed_provider,
            self.documents,
            EvidenceStore(self.database),
            self.settings,
        )

        results = [
            await self._run_case(workflow, observed_provider, case, line_counts) for case in cases
        ]
        metrics = self._metrics(results)
        configured_model = (
            "deterministic-v1"
            if self.settings.provider == "deterministic"
            else self.settings.llm_model
        )
        provider_metadata = {
            "name": self.settings.provider,
            "implementation": self.provider.name,
            "model": configured_model,
            "models_observed": sorted(observed_provider.models),
        }
        run_config = {
            "recall_k": RECALL_K,
            "max_review_rounds": self.settings.max_review_rounds,
            "max_steps": self.settings.max_steps,
            "max_tool_calls": self.settings.max_tool_calls,
            "max_total_tokens": self.settings.max_total_tokens,
            "tool_timeout_seconds": self.settings.tool_timeout_seconds,
            "llm_max_attempts": self.settings.llm_max_attempts,
        }
        record = EvaluationRunRecord(
            id=str(uuid4()),
            dataset_name=dataset_metadata["name"],
            configuration={
                "schema_version": EVALUATION_SCHEMA_VERSION,
                "generated_at": generated_at,
                "provider": provider_metadata,
                "dataset": dataset_metadata,
                "run_config": run_config,
                "corpus": corpus,
            },
            metrics_json=metrics,
            status="completed",
        )
        async with self.database.session() as session:
            session.add(record)
        return {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "generated_at": generated_at,
            "run_id": record.id,
            "dataset": record.dataset_name,
            "dataset_metadata": dataset_metadata,
            "provider": provider_metadata,
            "run_config": run_config,
            "corpus": corpus,
            "metrics": metrics,
            "metric_semantics": {
                "claim_support": {
                    "evaluated": False,
                    "unit": "claim-citation pair",
                    "label_source": None,
                    "note": (
                        "Requires explicit entailment labels; not derived from refusal behavior."
                    ),
                },
                "semantic_review": {
                    "evaluated": False,
                    "unit": "reviewer evidence decision",
                    "label_source": None,
                    "metrics": {
                        "precision": None,
                        "recall": None,
                        "f1": None,
                        "adversarial_decoy_rejection_rate": None,
                    },
                    "note": (
                        "No human-labeled semantic-review decisions are present in the dataset."
                    ),
                },
                "revision_success": {
                    "evaluated": metrics["revision_success_evaluated"],
                    "unit": "case with a reviewer revision request",
                    "note": (
                        "Successful when an executed revision completes without reaching the "
                        "review limit; null when no case requests revision."
                    ),
                },
            },
            "cases": [
                result.__dict__
                | {
                    "fallback_used": result.fallback_used,
                    "revision_requested": result.revision_requested,
                    "revision_executed": result.revision_executed,
                    "success": result.success,
                }
                for result in results
            ],
        }

    async def _run_case(
        self,
        workflow: ResearchWorkflow,
        provider: _ObservedProvider,
        case: EvalCase,
        line_counts: dict[str, int],
    ) -> CaseResult:
        calls_before = provider.calls
        fallback_before = provider.fallback_responses
        started = perf_counter()
        result = await workflow.run(case.goal)
        latency_ms = (perf_counter() - started) * 1000

        report = result.answer
        refused = "## 证据与发现" not in report
        citations = CITATION_PATTERN.findall(report)
        resolvable = sum(
            1
            for source, _start, end in citations
            if source in line_counts and int(end) <= line_counts[source]
        )
        recall_hit: bool | None = None
        if case.expected_source is not None:
            top_sources = [source for source, _s, _e in citations[:RECALL_K]]
            recall_hit = any(case.expected_source in source for source in top_sources)
        keywords_hit = all(keyword in report for keyword in case.expected_keywords)
        if case.expect_refusal:
            keywords_hit = True

        degraded_reasons, revision_requests, revision_rounds = self._workflow_observations(result)

        return CaseResult(
            case_id=case.id,
            completed=result.status == "completed",
            refused=refused,
            recall_hit=recall_hit,
            citations_total=len(citations),
            citations_resolvable=resolvable,
            keywords_hit=keywords_hit,
            latency_ms=round(latency_ms, 3),
            degraded=result.degraded,
            degraded_reasons=degraded_reasons,
            provider_calls=provider.calls - calls_before,
            fallback_responses=provider.fallback_responses - fallback_before,
            revision_requests=revision_requests,
            revision_rounds=revision_rounds,
            revision_limit_reached="review_limit_reached" in degraded_reasons,
        )

    @staticmethod
    def _workflow_observations(result: AgentRunResult) -> tuple[tuple[str, ...], int, int]:
        writer_events = [
            event
            for event in result.trace
            if event.event == "workflow" and event.metadata.get("node") == "writer"
        ]
        reasons_value = (
            writer_events[-1].metadata.get("degraded_reasons", []) if writer_events else []
        )
        degraded_reasons = (
            tuple(sorted(str(reason) for reason in reasons_value))
            if isinstance(reasons_value, list)
            else ()
        )
        reviewer_events = [
            event
            for event in result.trace
            if event.event == "workflow" and event.metadata.get("node") == "reviewer"
        ]
        revision_requests = sum(
            bool(event.metadata.get("needs_revision")) for event in reviewer_events
        )
        revision_rounds = max(
            (
                int(event.metadata.get("review_round", 0))
                for event in result.trace
                if event.event == "checkpoint"
            ),
            default=0,
        )
        return degraded_reasons, revision_requests, revision_rounds

    @staticmethod
    def _metrics(results: list[CaseResult]) -> dict[str, Any]:
        retrieval_cases = [result for result in results if result.recall_hit is not None]
        refusal_cases = [result for result in results if result.recall_hit is None]
        total_citations = sum(result.citations_total for result in results)
        resolvable = sum(result.citations_resolvable for result in results)
        latencies = sorted(result.latency_ms for result in results)
        p95 = (
            quantiles(latencies, n=20)[-1]
            if len(latencies) >= 2
            else (latencies[0] if latencies else 0.0)
        )
        result_count = len(results)
        provider_calls = sum(result.provider_calls for result in results)
        fallback_responses = sum(result.fallback_responses for result in results)
        revision_cases = [result for result in results if result.revision_requested]
        revision_success_cases = sum(
            result.revision_executed and result.success and not result.revision_limit_reached
            for result in revision_cases
        )

        def result_rate(predicate: Callable[[CaseResult], bool]) -> float:
            if not result_count:
                return 0.0
            return round(sum(predicate(result) for result in results) / result_count, 4)

        return {
            "cases": result_count,
            "task_success_rate": round(sum(result.success for result in results) / result_count, 4)
            if results
            else 0.0,
            "recall_at_5": round(
                sum(bool(result.recall_hit) for result in retrieval_cases) / len(retrieval_cases),
                4,
            )
            if retrieval_cases
            else 0.0,
            "citation_precision": round(resolvable / total_citations, 4)
            if total_citations
            else 0.0,
            "citation_validity": round(resolvable / total_citations, 4) if total_citations else 0.0,
            "refusal_accuracy": round(
                sum(result.refused for result in refusal_cases) / len(refusal_cases), 4
            )
            if refusal_cases
            else 1.0,
            "unsupported_answer_rate": round(
                1
                - (
                    sum(result.refused for result in refusal_cases) / len(refusal_cases)
                    if refusal_cases
                    else 1.0
                ),
                4,
            ),
            # Claim-level entailment needs an annotated claim/citation dataset.
            # Do not disguise refusal accuracy as groundedness.
            "claim_support_rate": None,
            "claim_support_evaluated": False,
            "semantic_review_evaluated": False,
            "semantic_review_precision": None,
            "semantic_review_recall": None,
            "semantic_review_f1": None,
            "adversarial_decoy_rejection_rate": None,
            "degraded_cases": sum(result.degraded for result in results),
            "degraded_case_rate": result_rate(lambda result: result.degraded),
            "provider_calls": provider_calls,
            "fallback_responses": fallback_responses,
            "fallback_response_rate": round(fallback_responses / provider_calls, 4)
            if provider_calls
            else 0.0,
            "fallback_cases": sum(result.fallback_used for result in results),
            "fallback_case_rate": result_rate(lambda result: result.fallback_used),
            "revision_requested_cases": sum(result.revision_requested for result in results),
            "revision_requested_case_rate": result_rate(lambda result: result.revision_requested),
            "revision_executed_cases": sum(result.revision_executed for result in results),
            "revision_executed_case_rate": result_rate(lambda result: result.revision_executed),
            "revision_success_cases": revision_success_cases,
            "revision_success_rate": round(revision_success_cases / len(revision_cases), 4)
            if revision_cases
            else None,
            "revision_success_evaluated": bool(revision_cases),
            "revision_rounds": sum(result.revision_rounds for result in results),
            "revision_limit_reached_cases": sum(
                result.revision_limit_reached for result in results
            ),
            "revision_limit_reached_case_rate": result_rate(
                lambda result: result.revision_limit_reached
            ),
            "p95_latency_ms": round(p95, 3),
        }
