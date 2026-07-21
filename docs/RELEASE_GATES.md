# RepoPilot v1.2 Release Gates

`v1.0.0` remains the historical baseline and `v1.1.0` remains the last frozen measured release.
`v1.2.0` is frozen only when every required gate below passes against the packaged source.
Checkboxes must reflect command output or an inspectable test; they are not product claims by
themselves.

## Agentic workflow

- [x] Default orchestration is a compiled LangGraph `StateGraph`, and graph inspection exposes the
  Planner → Researcher ⇄ Reviewer → Writer topology.
- [x] Researcher retains a model-driven inner tool loop; LangGraph does not replace tool selection
  with hard-coded per-tool routing nodes.
- [x] Live Planner produces a schema-valid plan that actually drives repository retrieval.
- [x] Malformed, failed, or fallback Planner output uses a deterministic plan and sets degraded.
- [x] Researcher model tool calls execute only through the registered read-only repository tools.
- [x] Unknown, invalid, repeated, timed-out, and partially failed tools fail closed without losing
  previously verified evidence.
- [x] Deterministic review rejects stale, duplicate, low-score/coverage, or unresolvable evidence.
- [x] Semantic Reviewer can reject a hard-gate candidate but cannot promote a hard-gate rejection.
- [x] Evidence gaps can trigger Researcher → Reviewer revision, with exactly the configured maximum
  number of additional rounds.
- [x] Writer receives only accepted evidence; no-evidence tasks skip Writer and refuse conclusions.
- [x] Writer output with no citation or an out-of-range citation is discarded for evidence-only
  output and sets degraded.
- [x] Repository prompt injection cannot expand the tool allowlist, budgets, or system rules.

## State, fallback, and API

- [x] WorkflowState checkpoint includes next node, plan, candidates, review results, review round,
  budgets, and degraded reasons.
- [x] Resume continues from the saved node/round and does not repeat work committed by an earlier
  node-boundary checkpoint; an interrupted in-flight Researcher node may restart safely.
- [x] Missing checkpoint chunk IDs are treated as corpus drift and safely re-researched.
- [x] Final Evidence is transactionally replaced so revision/resume cannot duplicate rows.
- [x] `fallback_used` propagates from Provider response through trace/workflow to task
  `degraded=true`.
- [x] REST, SSE, web UI, MCP, and evaluation runner operate against the same persisted task state.
- [x] SSE `Last-Event-ID` replay emits only events newer than the requested sequence before live
  continuation.
- [x] Documentation and tests describe SSE as SQLite event-table short polling, not push pub/sub.
- [x] Recovery is described as committed node/round recovery, not replay of an in-flight Provider
  request or an exactly-once side-effect guarantee.
- [x] SQLAlchemy TaskStore remains the single durable recovery source; LangGraph executes the graph
  without a second saver or checkpoint dual-write path.

## Reliability and security

- [x] Provider timeout, rate limit, malformed response, 5xx, circuit breaker, and fallback are
  tested per Planner/Researcher/Reviewer/Writer purpose.
- [x] Tool timeout, cancellation, partial failure, retry eligibility, duplicate detection, and
  execution budgets are tested.
- [x] Path traversal, symlink escape, oversized upload, prompt injection, and secret-redaction tests
  pass.
- [x] No API key exists in source, `.env`, fixtures, commands, logs, traces, reports, or built
  artifacts; exception chains are redacted before log handlers.
- [x] Deterministic mode completes the same workflow while the live model endpoint is unavailable.

## Evaluation

- [x] Frozen retrieval/refusal cases remain reproducible with corpus document count and SHA-256
  fingerprint recorded.
- [x] Citation validity is measured independently from answer correctness.
- [x] Claim support is measured per claim/citation pair when explicit entailment labels exist;
  otherwise the report sets `claim_support_evaluated=false` and the rate to `null`. No metric
  derived only from refusal accuracy is labeled groundedness or unsupported-claim rate.
- [x] Semantic review precision/recall/F1 and adversarial decoy rejection are reported when labeled
  reviewer decisions exist; otherwise the report explicitly marks them unevaluated and sets each
  unavailable value to `null`.
- [x] Revision success, revision-limit, fallback, degraded, task success, and P95 latency are
  reported in `evals/v1.2-report.json`: 30 cases, task success 0.9667, Recall@5 0.9583,
  citation validity/precision 1.0, refusal accuracy 1.0, degraded/fallback 0, repeated local P95
  about 0.35–0.40s.
- [x] Retrieval labels use canonical repository paths with exact file or explicit directory-prefix
  matching, and every case records its Top-5 sources; similarly named tests/docs cannot create a
  hidden Recall hit.
- [x] Live-provider quality/latency results identify endpoint, model, dataset, date, and run config;
  otherwise docs explicitly state that only deterministic offline results exist.

## Quality and delivery

- [x] `uv run pytest` passes: 108 tests.
- [x] `uv run ruff check .` and `uv run ruff format --check .` pass for the v1.2 checkout.
- [x] `uv run mypy src` passes in strict mode for the v1.2 checkout.
- [x] Branch coverage meets the configured threshold and critical workflow/provider/state paths are
  exercised: 86.35% against the 85% gate.
- [x] Frontend JavaScript syntax checks and Playwright desktop/mobile smoke tests pass with no
  console errors or layout overflow.
- [x] `uv lock --check --offline`, dependency checks, wheel/sdist build, and artifact-content checks
  pass.
- [x] Docker image runs as non-root and passes `/ready`, ingest, task, evidence, SSE, metrics, resume,
  and degraded-state smoke tests.
- [x] Compose starts the packaged service with persistent storage and no embedded secret.

## Documentation and freeze artifact

- [x] README, architecture, product specification, release gates, and teaching-site wording describe
  the v1.2 StateGraph/inner-loop split and preserve v1.0/v1.1 measurements as historical data.
- [x] Documentation distinguishes model decisions from Harness-enforced rules and avoids calling the
  system four autonomous Agents.
- [x] Documentation does not claim live throughput/model quality without a measured benchmark.
- [x] OpenAPI, configuration, acceptance report, changelog, package version, and built artifacts
  agree on `1.2.0`.
- [x] Final command outputs, package/image digests, SHA-256 checksums, and the v1.2 evaluation report
  are recorded in `docs/ACCEPTANCE.md` without rewriting the v1.0/v1.1 record.

## Reproducible release procedure

Run `python scripts/build_release.py --out-dir release` from the repository root. The command
builds the wheel and sdist in a temporary directory, rejects forbidden credential-like paths, and
writes `release/release-manifest.json` plus `release/SHA256SUMS` outside the packaged artifacts.
Run `python scripts/check_release.py release` to verify both files. Because the manifest is external,
artifact bytes do not contain a hash of themselves (or of the checksum file). The mutable final
acceptance record and generated evaluation reports are also excluded from the sdist. The dataset and
historical baseline remain packaged, while rerunning a timestamped report cannot change the source
artifact being recorded. Docker and Compose gates should be run against the same checkout with
`docker build .` and `docker compose config`; record their observed digests and smoke output in the
acceptance record only after execution.
