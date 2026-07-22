# RepoPilot v1.4 Product Specification

## v1.4 user journey

1. Register one or more server-visible repositories from the UI, CLI or REST API. Local paths are
   constrained to an allowlist; Git onboarding accepts only HTTPS URLs without embedded credentials.
2. Select a repository and run **重新索引**. The operation creates a new immutable revision and keeps
   the previous ready revision available until the new snapshot passes ingestion.
3. Create a task with the selected `repository_id`. The API resolves and persists the current
   `revision_id`; later refreshes do not alter that task's corpus.
4. Follow durable task events over SSE, inspect evidence, read sanitized HTML, and download original
   Markdown, standalone offline HTML, or structured JSON.

Uploaded UTF-8 documents are not in-place mutations: they are copied into a new overlay revision and
therefore cannot invalidate a task that is already running. A subsequent repository sync carries the
latest upload overlays into the new source revision.

The browser form's local path refers to the RepoPilot host, not an arbitrary path on the user's laptop.
Historical tasks remain readable after a repository is archived. A running task is not exportable and
returns HTTP 409 instead of an empty or misleading file.

## Product statement

RepoPilot is a model-driven, evidence-first repository research Agent constrained by an engineering
Harness. A user submits a question, bug report, implementation request, or research topic. The
system creates a structured plan, lets a model choose registered read-only repository tools,
reviews candidate evidence with deterministic and semantic gates, performs bounded gap-filling, and
produces a cited report whose execution can be inspected and resumed.

The release is complete for a single-user, self-hosted, read-only multi-repository research portfolio
product. It
does not claim enterprise multi-tenancy, autonomous code modification, internet-scale throughput,
or provider-independent model quality.

## Product invariants

1. No repository claim is presented without accepted, resolvable evidence.
2. Model output cannot add tools, permissions, budget, or state transitions.
3. Semantic review can narrow deterministic evidence acceptance, never widen it.
4. Writer sees only accepted evidence, and its numbered references are verified after generation.
5. Missing evidence produces refusal; optional model failure produces an evidence-only degraded
   report rather than fabricated synthesis.
6. Deterministic and live-provider modes execute the same compiled LangGraph and persistence contract.
7. Provider fallback is visible as `degraded=true`; it never impersonates the configured live model.
8. Provider lifecycle persistence is content-free and allowlisted; prompts, completions, keys, URLs,
   response IDs, raw errors, token deltas, and tool arguments are excluded.
9. Tool and Token budgets are task-global across all revision rounds; a revision never resets them.
10. Every loop, retry, tool call, model step, timeout, and Token budget is finite.

## Primary user journeys

1. Add a local repository or upload a supported UTF-8 text document.
2. Index source into content-versioned documents and line-addressable chunks.
3. Create a research task with a goal, constraints, and optional budget.
4. Watch Planner, Researcher, Reviewer, bounded revision, Writer, and content-free Provider
   lifecycle events over task SSE.
5. Inspect plan, tool calls, accepted/rejected evidence, citations, timings, degraded reasons, and
   final report.
6. Resume an interrupted task from its latest node/round WorkflowState checkpoint.
7. Run deterministic evaluation and compare retrieval, review, fallback, and workflow versions.
8. Use the separate read-only repository tools through the bundled MCP server.

## Required capabilities

### Agentic workflow

- The default orchestrator is a compiled LangGraph `StateGraph` with named Planner, Researcher,
  Reviewer, and Writer nodes plus a bounded conditional Reviewer → Researcher edge.
- Role internals remain model-driven: graph routing defines responsibility and termination, while
  the Researcher model chooses allowed tools from observations inside its bounded node loop.
- Live-model Planner emits schema-validated queries, subquestions, and completion criteria.
- Invalid/fallback Planner output safely selects a bounded deterministic plan.
- Live-model Researcher selects only registered read-only repository search/read tools.
- Tool arguments use explicit JSON Schema and fail closed for unknown tools or invalid input.
- Deterministic hard review validates corpus freshness, deduplication, score, coverage, and citation.
- Live-model Reviewer evaluates relevance/entailment only inside the hard-gate candidate set.
- Reviewer receives completion requirements and executed-query history; it may request at most two
  novel additional queries per round and must identify missing requirements.
- Revision stops on no novel query, no evidence increment, global budget exhaustion, or
  `max_review_rounds`; these terminal guard conditions are not reported as normal completion.
- Writer receives only accepted evidence; invalid citations trigger evidence-only degradation.
- No accepted evidence skips model writing and returns an explicit refusal.

### Runtime and persistence

- Provider-neutral asynchronous model client with deterministic offline mode.
- OpenAI-compatible Provider configured only through environment variables.
- Buffered upstream Provider SSE by default: merge/validate text, fragmented tool calls, finish
  reason, served model, and usage before exposing one complete `ModelResponse` to a role.
- Independent streaming and `stream_options.include_usage` compatibility switches.
- Bounded retry/backoff, connect/read/write/pool timeouts, circuit breaker, and fallback provenance.
- Content-free Provider lifecycle events for started, first byte, periodic progress, retry, timeout,
  failure, cancellation, and completion; TTFT and terminal latency metrics.
- Missing Provider usage is conservatively estimated, explicitly marked, and used only for budget
  accounting rather than represented as official billing data.
- Concurrent execution only for same-turn tools whose registered specs are all read-only.
- Step/tool/Token budgets, duplicate-call detection, cancellation, and structured errors; tool and
  Token counters remain cumulative across Researcher revisions.
- Full WorkflowState checkpoints and node/round resume without duplicate Evidence rows.
- SQLAlchemy TaskStore is the only durable checkpoint authority; no second LangGraph saver or
  dual-write recovery path is used.
- Durable tasks, events, checkpoints, documents, chunks, evidence, memories, and evaluation runs.
- REST API, configurable SQLite short-polling SSE replay with `Last-Event-ID`, unnumbered transport
  heartbeat, health/readiness endpoints, and Prometheus metrics.
- Same-task event sequence serialization is guaranteed inside one RepoPilot process with bounded
  uniqueness retry; multi-process/multi-replica fan-out is outside the product contract.

### Repository evidence

- Safe workspace ingestion and deterministic line-window chunking.
- BM25 retrieval with CJK bigrams and snake_case subtokens.
- Deterministic hashed-embedding bonus disclosed as weak, non-learned similarity.
- Source citations resolving to a current stored document version.
- Repository/tool content treated as untrusted data and unable to alter Harness instructions.
- Transactional final-evidence replacement for idempotent review loops and resume.

### Delivery and verification

- Static web application for tasks, reports, evidence, and live events.
- Read-only MCP stdio server for repository search/read, task status, and evidence lookup.
- Unit, integration, API, resilience, security, evaluation, and browser smoke tests.
- Docker Compose deployment and an offline one-command demo.
- Release metrics that distinguish retrieval recall, citation validity, claim support, review quality,
  revision outcomes, fallback/degraded rate, latency, and refusal behavior.
- Retrieval labels use exact repository paths (or an explicit directory prefix), and each case records
  its Top-5 returned sources so similarly named test/docs files cannot create hidden Recall positives.

## Non-goals

- Autonomous code writing, shell execution, or repository mutation.
- Unbounded crawling, unrestricted filesystem access, arbitrary URL fetching, or model-defined tools.
- Four autonomous Agents conversing without an explicit state machine.
- Formal proof that every natural-language conclusion is correct.
- Fabricated production traffic, DAU, throughput, revenue, accuracy, or cost claims.
- Mandatory dependence on a model vendor, live API, vector database, Redis, or frontend framework
  during tests. LangGraph is an intentional runtime dependency in v1.3, while deterministic tests
  remain independent of an external model service.
- Direct token streaming of partial Planner/Reviewer JSON or unvalidated Writer drafts to users.
- Exactly-once Provider execution, replay of in-flight HTTP requests, or lifecycle-event completion
  guarantees across process crashes.

## Release policy

`v1.0.0`, `v1.1.0`, and `v1.2.0` remain frozen historical releases; their measured results and
artifact hashes are never rewritten. `v1.3.0` adds buffered upstream SSE, Provider lifecycle
telemetry, task-global budget enforcement, and Reviewer novelty/stagnation convergence without
weakening the LangGraph/evidence/API contracts. A v1.3 freeze requires fresh code checks, the same
deterministic 30-case regression, a real-provider no-fallback state-machine acceptance run,
artifact/secret inspection, and container smoke tests. A real endpoint run demonstrates interface
and workflow compatibility only unless it uses a labeled dataset and documented load methodology;
it is not model-quality, throughput, capacity, or cost evidence.
