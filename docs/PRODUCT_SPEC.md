# RepoPilot v1.5 Product Specification

## v1.5 user journey

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
11. LangChain owns standard model integration concerns; RepoPilot owns product policy and never
    delegates authorization, evidence acceptance, or state-transition invariants to a model SDK.

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
- Planner and Reviewer use LangChain Pydantic structured output; successful schema decoding still
  passes RepoPilot's local count, membership, novelty, and policy checks.
- Invalid/fallback Planner output safely selects a bounded deterministic plan.
- Live-model Researcher receives registered tools through LangChain Tool Calling and executes them
  only through the shared RepoPilot `ToolCallingHarness`.
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

- Existing default-workspace records survive mount relocation: update the legacy repository path,
  preserve historical tasks/evidence/revisions, and require a fresh index before new tasks use the
  relocated workspace. A fresh database must not advertise an unscanned synthetic revision.
- Provider-neutral RepoPilot contract with deterministic offline mode and a LangChain-backed live
  implementation.
- `langchain-core` provides message/Runnable contracts; `langchain-openai` provides `ChatOpenAI`,
  Tool Calling, Pydantic structured output, SDK error types, and streaming chunk aggregation.
- Live Provider configuration is supplied only through environment variables. The preferred value
  is `REPOPILOT_PROVIDER=langchain_openai`; `openai_compatible` remains a v1.4 compatibility alias.
- Upstream streaming is aggregated before exposing one complete `ModelResponse` to role logic;
  partial Planner/Reviewer structures and unvalidated Writer drafts never become task output.
- Independent streaming and `stream_options.include_usage` compatibility switches.
- Bounded retry/backoff, connect/read/write/pool timeouts, circuit breaker, and fallback provenance.
- Content-free Provider lifecycle events for started, first byte, periodic progress, retry, timeout,
  failure, cancellation, and completion; TTFT and terminal latency metrics.
- Missing Provider usage is conservatively estimated, explicitly marked, and used only for budget
  accounting rather than represented as official billing data; structured-output estimates include
  the LangChain function Schema sent to the model.
- Concurrent execution only for same-turn tools whose registered specs are all read-only.
- Step/tool/Token budgets, duplicate-call detection, cancellation, and structured errors; tool and
  Token counters remain cumulative across Researcher revisions.
- Full WorkflowState checkpoints and node/round resume without duplicate Evidence rows.
- SQLAlchemy TaskStore is the only durable checkpoint authority; no second LangGraph saver or
  dual-write recovery path is used.
- Durable tasks, events, checkpoints, documents, chunks, evidence, memories, and evaluation runs.
- REST API, configurable SQLite short-polling SSE replay with `Last-Event-ID`, unnumbered transport
  heartbeat, health/readiness endpoints, and Prometheus metrics.
- Public-Demo control-plane separation: quota-limited task creation and UUID-addressed task results
  can remain public, while task enumeration, repository mutation, ingestion/upload, memory, and
  metrics require a separate administrator token or fail closed when no administrator token exists.
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
- CLI evaluation uses a fresh database and the dataset-declared `corpus_path`; application documents
  cannot contaminate retrieval metrics, benchmark documents are not copied into the application
  database, and only the final immutable evaluation-run record is retained for audit history.

## Non-goals

- Autonomous code writing, shell execution, or repository mutation.
- Unbounded crawling, unrestricted filesystem access, arbitrary URL fetching, or model-defined tools.
- Four autonomous Agents conversing without an explicit state machine.
- Formal proof that every natural-language conclusion is correct.
- Fabricated production traffic, DAU, throughput, revenue, accuracy, or cost claims.
- Mandatory dependence on a model vendor, live API, vector database, Redis, or frontend framework
  during tests. LangChain model components and LangGraph are intentional runtime dependencies, while
  deterministic tests remain independent of an external model service.
- A nested `langchain.agents.create_agent` graph. RepoPilot's domain LangGraph is the single control
  plane; a generic inner Agent graph would duplicate routing and termination ownership.
- Direct token streaming of partial Planner/Reviewer JSON or unvalidated Writer drafts to users.
- Exactly-once Provider execution, replay of in-flight HTTP requests, or lifecycle-event completion
  guarantees across process crashes.

## Release policy

Historical release measurements and artifact hashes are immutable. A v1.5 release requires fresh
code checks, branch coverage, the current 30-case deterministic regression, browser acceptance,
reproducible wheel/sdist validation, clean-wheel installation, tracked-source and artifact secret
inspection, and hardened Docker/Compose smoke. A real endpoint run demonstrates only interface and
workflow compatibility unless it uses a labeled dataset and documented load methodology; it is not
model-quality, throughput, capacity, or cost evidence.
