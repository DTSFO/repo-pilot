# RepoPilot v1.2 Product Specification

## Product statement

RepoPilot is a model-driven, evidence-first repository research Agent constrained by an engineering
Harness. A user submits a question, bug report, implementation request, or research topic. The
system creates a structured plan, lets a model choose registered read-only repository tools,
reviews candidate evidence with deterministic and semantic gates, performs bounded gap-filling, and
produces a cited report whose execution can be inspected and resumed.

The release is complete for a single-user, self-hosted, read-only research portfolio product. It
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
8. Every loop, retry, tool call, model step, timeout, and Token budget is finite.

## Primary user journeys

1. Add a local repository or upload a supported UTF-8 text document.
2. Index source into content-versioned documents and line-addressable chunks.
3. Create a research task with a goal, constraints, and optional budget.
4. Watch Planner, Researcher, Reviewer, bounded revision, and Writer events over SSE.
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
- Reviewer may request additional queries for at most `max_review_rounds` extra rounds.
- Writer receives only accepted evidence; invalid citations trigger evidence-only degradation.
- No accepted evidence skips model writing and returns an explicit refusal.

### Runtime and persistence

- Provider-neutral asynchronous model client with deterministic offline mode.
- OpenAI-compatible Provider configured only through environment variables.
- Bounded retry/backoff, timeout, circuit breaker, and fallback provenance.
- Concurrent execution only for same-turn tools whose registered specs are all read-only.
- Step/tool/Token budgets, duplicate-call detection, cancellation, and structured errors.
- Full WorkflowState checkpoints and node/round resume without duplicate Evidence rows.
- SQLAlchemy TaskStore is the only durable checkpoint authority; no second LangGraph saver or
  dual-write recovery path is used.
- Durable tasks, events, checkpoints, documents, chunks, evidence, memories, and evaluation runs.
- REST API, SQLite short-polling SSE replay with `Last-Event-ID`, health/readiness endpoints, and
  Prometheus metrics.

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
  during tests. LangGraph is an intentional runtime dependency in v1.2, while deterministic tests
  remain independent of an external model service.

## Release policy

`v1.0.0` remains a frozen historical release and its recorded baseline is not rewritten. `v1.1.0`
is the measured agentic baseline. `v1.2.0` changes the default orchestration implementation to a
real LangGraph StateGraph without weakening the evidence/API contracts. Its release gate must rerun
tests and the deterministic 30-case evaluation. The v1.2 checkout has passed code, evaluation,
browser, package, Docker, Compose, persistence, resume, SSE, and degraded-state gates; measured
deterministic quality remains explicitly separate from live-provider quality or production capacity.
