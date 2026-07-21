from __future__ import annotations

import json
from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.evaluation import CaseResult, EvaluationRunner, load_dataset
from repopilot.ingestion import RepositoryIngestor
from repopilot.providers.deterministic import DeterministicProvider
from repopilot.storage import Database, DocumentStore


def test_dataset_has_thirty_unique_cases() -> None:
    payload = json.loads(Path("evals/dataset.json").read_text(encoding="utf-8"))
    cases = load_dataset(Path("evals/dataset.json"))
    assert payload["schema_version"] == "1.1"
    assert len(cases) == 30
    assert sum(case.expect_refusal for case in cases) == 6


def test_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    case = {"id": "a", "goal": "g"}
    path.write_text(json.dumps({"cases": [case, case]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(path)


@pytest.mark.parametrize(
    "case",
    [
        {"id": "unlabeled", "goal": "g"},
        {
            "id": "conflicting",
            "goal": "g",
            "expected_source": "auth.py",
            "expect_refusal": True,
        },
    ],
)
def test_dataset_requires_unambiguous_case_labels(tmp_path: Path, case: dict[str, object]) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"cases": [case]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(path)


async def test_eval_runner_on_small_corpus(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "auth.py").write_text(
        "def verify_token(token):\n    '''Bearer token verification for the API.'''\n",
        encoding="utf-8",
    )
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "hit",
                        "goal": "bearer token verification",
                        "expected_source": "auth.py",
                    },
                    {"id": "miss", "goal": "今晚吃什么好呢", "expect_refusal": True},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings.model_validate(
        {
            "provider": "deterministic",
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/eval.db",
            "workspace_root": str(workspace),
        }
    )
    database = Database(settings.database_url)
    await database.initialize()
    try:
        runner = EvaluationRunner(settings, database, DeterministicProvider())
        result = await runner.run(dataset)
    finally:
        await database.close()

    metrics = result["metrics"]
    assert result["schema_version"] == "1.1"
    assert result["generated_at"].endswith("Z")
    assert result["provider"] == {
        "name": "deterministic",
        "implementation": "deterministic",
        "model": "deterministic-v1",
        "models_observed": [],
    }
    assert result["dataset_metadata"]["path"] == str(dataset)
    assert len(result["dataset_metadata"]["fingerprint"]) == 64
    assert result["run_config"]["recall_k"] == 5
    assert result["corpus"]["documents"] == 1
    assert len(result["corpus"]["fingerprint"]) == 64
    assert metrics["cases"] == 2
    assert metrics["recall_at_5"] == 1.0
    assert metrics["refusal_accuracy"] == 1.0
    assert metrics["task_success_rate"] == 1.0
    assert metrics["degraded_cases"] == 0
    assert metrics["fallback_responses"] == 0
    assert metrics["revision_requested_cases"] == 0
    assert metrics["claim_support_rate"] is None
    assert metrics["claim_support_evaluated"] is False
    assert metrics["semantic_review_evaluated"] is False
    assert metrics["semantic_review_precision"] is None
    assert metrics["semantic_review_recall"] is None
    assert metrics["semantic_review_f1"] is None
    assert metrics["adversarial_decoy_rejection_rate"] is None
    assert metrics["revision_success_rate"] is None
    assert metrics["revision_success_evaluated"] is False
    assert result["metric_semantics"]["claim_support"]["unit"] == "claim-citation pair"
    semantic_review = result["metric_semantics"]["semantic_review"]
    assert semantic_review["unit"] == "reviewer evidence decision"
    assert all(value is None for value in semantic_review["metrics"].values())


async def test_eval_uses_latest_document_version_for_citation_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = workspace / "auth.py"
    source.write_text("legacy implementation\n", encoding="utf-8")
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "latest-lines",
                        "goal": "bearer token verification",
                        "expected_source": "auth.py",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings.model_validate(
        {
            "provider": "deterministic",
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/eval.db",
            "workspace_root": str(workspace),
        }
    )
    database = Database(settings.database_url)
    await database.initialize()
    provider = DeterministicProvider()
    try:
        documents = DocumentStore(database)
        await RepositoryIngestor(documents, settings).ingest_path()
        lines = [f"filler line {index}" for index in range(1, 61)]
        lines.extend("bearer token verification" for _ in range(10))
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = await EvaluationRunner(settings, database, provider).run(dataset)
    finally:
        await provider.close()
        await database.close()

    case = result["cases"][0]
    assert case["citations_total"] >= 1
    assert case["citations_resolvable"] == case["citations_total"]
    assert result["metrics"]["citation_precision"] == 1.0


def test_metrics_report_observed_degradation_fallback_and_revision() -> None:
    results = [
        CaseResult(
            case_id="observed",
            completed=True,
            refused=False,
            recall_hit=True,
            citations_total=1,
            citations_resolvable=1,
            keywords_hit=True,
            latency_ms=10.0,
            degraded=True,
            degraded_reasons=("planner_fallback", "review_limit_reached"),
            provider_calls=4,
            fallback_responses=1,
            revision_requests=2,
            revision_rounds=1,
            revision_limit_reached=True,
        ),
        CaseResult(
            case_id="clean",
            completed=True,
            refused=True,
            recall_hit=None,
            citations_total=0,
            citations_resolvable=0,
            keywords_hit=True,
            latency_ms=20.0,
            degraded=False,
            degraded_reasons=(),
            provider_calls=1,
            fallback_responses=0,
            revision_requests=0,
            revision_rounds=0,
            revision_limit_reached=False,
        ),
    ]

    metrics = EvaluationRunner._metrics(results)

    assert metrics["degraded_cases"] == 1
    assert metrics["degraded_case_rate"] == 0.5
    assert metrics["provider_calls"] == 5
    assert metrics["fallback_responses"] == 1
    assert metrics["fallback_response_rate"] == 0.2
    assert metrics["fallback_cases"] == 1
    assert metrics["fallback_case_rate"] == 0.5
    assert metrics["revision_requested_cases"] == 1
    assert metrics["revision_executed_cases"] == 1
    assert metrics["revision_success_cases"] == 0
    assert metrics["revision_success_rate"] == 0.0
    assert metrics["revision_success_evaluated"] is True
    assert metrics["revision_rounds"] == 1
    assert metrics["revision_limit_reached_cases"] == 1
    assert metrics["claim_support_rate"] is None
