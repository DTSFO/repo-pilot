# RepoPilot v1.3 Release Gates

`v1.0.0`, `v1.1.0`, and `v1.2.0` are frozen historical releases. Their acceptance numbers,
artifact hashes, and image digests must not be rewritten. `v1.3.0` is frozen only after every open
gate below is backed by command output or an inspectable test against the final packaged source.

## v1.3 workflow and convergence

- [x] Default orchestration remains the compiled LangGraph Planner → Researcher ⇄ Reviewer → Writer
  `StateGraph`; Provider streaming does not replace role or graph boundaries.
- [x] Reviewer receives completion criteria, executed queries, and candidates; code—not the model
  boolean alone—owns routing.
- [x] Additional queries are fingerprinted for novelty, bounded to two per round, and require
  explicit missing requirements.
- [x] Revision stops when queries repeat, a revision adds no candidate, review limit is reached, or
  task-global Tool/Token budgets are exhausted.
- [x] Resume and cancel lifecycle decisions use a per-task in-process lock, so concurrent resume
  requests cannot launch duplicate runners; the guarantee is intentionally single-process.
- [x] `review_limit_reached`, `review_stagnated`, `tool_budget_exhausted`, and
  `token_budget_exhausted` produce terminal `guarded`; missing requirements remain visible in the
  report.
- [x] Tool and Token counters are cumulative across all Researcher revisions. Remaining Tool budget
  gates later repository calls; remaining Token budget gates later Researcher, Reviewer, and Writer
  model calls.
- [x] `degraded` remains orthogonal to `guarded`: fallback/protocol/corpus-drift degradation can
  accompany completed or guarded outcomes and is never hidden.

## v1.3 Provider streaming and observability

- [x] OpenAI-compatible calls default to upstream `stream=true`, buffer/merge text and fragmented
  tool calls, validate the final structure, and return one `ModelResponse` to role logic.
- [x] A Provider returning ordinary JSON for a streaming request remains compatible; streaming and
  `stream_options.include_usage` can be disabled independently.
- [x] Connect/read/write/pool timeouts are separately configurable; retries share a logical
  `call_id` and attempt counter; fallback emits a terminal lifecycle event.
- [x] Lifecycle phases cover started, first byte, progress, retry, timeout, failure, cancellation,
  and completion. Progress distinguishes `waiting_first_byte` from `receiving`.
- [x] Prometheus exposes Provider phase counts, TTFT, terminal request latency, and dropped telemetry.
- [x] A telemetry sink failure is best-effort, safely logged, and cannot turn Provider success into
  inference failure.
- [x] Missing Provider usage receives a conservative, explicitly marked estimate so task-global
  budget accounting cannot silently remain zero. Docs do not present estimates as billing data.
- [x] Durable lifecycle metadata passes an explicit allowlist and excludes URL, key, prompt,
  content/completion, raw exception text, token deltas, response IDs, and tool arguments.

## v1.3 task SSE, persistence, and recovery

- [x] Provider lifecycle events are bound to the active task through async-context-local state and
  are persisted before a workflow node completes.
- [x] Task SSE replays durable events newer than `Last-Event-ID` and includes event `created_at`.
- [x] SSE `: keep-alive` is unnumbered and non-persistent; docs distinguish it from durable
  `provider.request.progress`.
- [x] Poll and heartbeat intervals are configurable; `Cache-Control: no-cache, no-transform` and
  proxy-buffering guidance protect timely delivery.
- [x] Same-task event sequence allocation is serialized per process with bounded uniqueness retry,
  covering concurrent Provider ticks and node events.
- [x] Docs limit that sequence guarantee to one RepoPilot process and persisted database; they do
  not claim a cross-replica event bus or pub/sub.
- [x] Provider events are diagnostic timeline entries, not checkpoints. Recovery remains committed
  node/round recovery and does not claim in-flight replay, terminal event completion after crash, or
  exactly-once execution.
- [x] A cancelled half-open probe releases its permit, and cancellation during retry backoff emits a
  wrapper-owned terminal event while preserving `CancelledError` propagation.

## v1.3 quality evidence

- [x] Final implementation gate: 164 tests passed, Ruff and format check passed, strict Mypy
  passed, and branch coverage was 87.17% against the 85% threshold.
- [x] Real Provider minimum capability check observed HTTP 200 `text/event-stream`, multiple deltas,
  `[DONE]`, usage, no fallback, and `started → first_byte → completed`.
- [x] Reran Ruff, format check, strict Mypy, full tests, and coverage against the final v1.3
  checkout; the final numbers are recorded in `docs/ACCEPTANCE.md`.
- [x] Ran the deterministic 30-case evaluation against v1.3 and recorded its report/hash without
  rewriting historical v1.2 metrics.
- [x] Source-diverse Top-K prevents overlapping chunks from one file monopolizing evidence slots;
  the final deterministic run reached task success 1.0 and Recall@5 1.0 without changing labels.
- [x] Ran the four-role real Provider no-fallback acceptance with temporary SQLite; verified provider
  health, planner → researcher → reviewer → writer, lifecycle events, evidence/citations, final
  checkpoint, and absence of secret/content leakage.
- [x] Ran a broader multi-file goal; it persisted accepted evidence but honestly terminated
  `guarded` at the review limit instead of coercing an unresolved gap into success.
- [x] Built wheel/sdist twice, ran `scripts/check_release.py`, inspected artifact contents for secrets,
  and recorded only observed v1.3 sizes and SHA-256 values. The checker independently opens both
  archives, require exactly one wheel and one sdist, reject unsafe/link/credential-like members, and
  verify package Name/Version metadata rather than trusting the manifest alone.
- [x] Built and smoked hardened Docker/Compose as non-root; `/ready`, rootfs/capability/security
  options, writable-data-only permissions, clean CLI startup, and absence of embedded credentials
  passed. API/SSE/resume behavior remains covered by the packaged integration suite.
- [x] CI runs the release build/check twice, clean-wheel install smoke, redacted tracked-source secret
  scan, Docker/Compose validation, and a hardened container smoke under read-only-rootfs,
  dropped-capability, no-new-privileges constraints.
- [x] Package metadata, OpenAPI version, MCP server version, Provider User-Agent, changelog, README,
  architecture, product spec, and configuration example identify v1.3 consistently.

## v1.3 measurement policy

- [x] Deterministic evaluation is labeled as workflow/retrieval regression, not live-model quality or
  production throughput.
- [x] A real Provider run without a fixed labeled dataset and load methodology is labeled interface
  and state-machine acceptance only.
- [x] Citation validity remains distinct from claim entailment; unavailable labeled claim/reviewer
  metrics remain `evaluated=false`/`null` rather than being replaced by proxy metrics.
- [x] No TTFT, single-request latency, task status, or fallback-free run is advertised as a capacity,
  cost, accuracy, or SLO result.

## v1.3 freeze procedure

The v1.3 freeze ran code/evaluation/real-Provider gates first, then built into empty output
directories with
`python scripts/build_release.py --out-dir release` and verified with
`python scripts/check_release.py release`. Keep manifest and `SHA256SUMS` outside the artifacts;
the observed artifact sizes, hashes, image digest, and smoke observations are recorded in
`docs/ACCEPTANCE.md`. Historical v1.2 entries below remain immutable.

---

# RepoPilot v1.2 Historical Release Gates

`v1.0.0` remains the historical baseline and `v1.1.0` remains the last frozen measured release.
`v1.2.0` was frozen only after every required gate below passed against the packaged source.
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
- [x] Explicit-file ingestion uses the same allowlist as directory walks and rejects symlinks,
  VCS/config directories, credential-like files, private-key formats, and unsupported suffixes.
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
