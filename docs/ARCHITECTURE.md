# RepoPilot v1.3 Architecture

## System overview

```text
Web UI (static, served at /)          MCP client (stdio)
  │ REST + SSE                          │ JSON-RPC
FastAPI application  ◄──────────────  repopilot mcp
  ├── TaskService        events · WorkflowState checkpoints · resume · cancel
  ├── LangGraph          compiled StateGraph · conditional revision edge · typed workflow state
  ├── Role harnesses     model-driven tool loops · schema contracts · evidence-scoped prompts
  ├── ToolRegistry       schema-validated allowlist · read-only execution · observations
  ├── AsyncAgentRuntime  task-global budgets · retries · concurrency · fallback provenance
  ├── HybridRetriever    BM25 × weak deterministic hashed-embedding bonus
  ├── MemoryStore        task summaries · bounded recall · expiry
  ├── RepositoryIngestor safe walk · versioned documents · line-window chunks
  ├── Providers          Resilient → buffered upstream SSE / deterministic fallback
  ├── Observability      lifecycle timeline · TTFT/latency · JSON redaction · Prometheus
  └── Storage            SQLite/SQLAlchemy async (PostgreSQL-ready URL switch)
```

RepoPilot is a bounded multi-role Agent, not four independent autonomous processes. A real
LangGraph `StateGraph` is the default control plane. Model calls make
the decisions that benefit from language understanding; the Harness owns every permission,
budget, state transition, citation rule, and fallback.

LangGraph is used for durable, inspectable orchestration—not as a substitute for model agency.
Nodes delimit responsibility and checkpoint boundaries. Inside a role, the model can inspect its
observation and choose the next allowed action; the graph does not encode a branch for every tool
choice. This split keeps evidence and termination rules deterministic while avoiding a brittle
prompt pipeline disguised as an Agent.

## Compiled StateGraph

```text
Planner
  │ structured ResearchPlan
  ▼
Researcher ── candidate evidence ──► Reviewer
  ▲                                  │
  │ additional_queries               ├─ evidence gap + review budget remains
  └──────────────────────────────────┘
                                     │ accepted evidence / limit reached
                                     ▼
                                   Writer
                                     │
                                     ▼
                           citation validation → report
                                └─ failure → evidence-only
```

`max_review_rounds` means the number of additional Researcher → Reviewer rounds allowed after the
initial review. Tool calls and Tokens are task-global counters, not fresh allowances per revision.
The state machine always terminates because review rounds, model steps, tool calls, tokens,
timeouts, duplicate tool fingerprints, query novelty, and evidence-increment checks are bounded by
code.

The graph has four named nodes and one conditional decision after Reviewer:

- `planner → researcher → reviewer` is the initial path;
- Reviewer routes to `researcher` only when it identifies missing completion requirements, returns
  novel validated `additional_queries`, has not stagnated, and both revision and task-global budgets
  remain;
- otherwise Reviewer routes to `writer`, which either completes normally or returns an explicit
  guarded result after citation validation.

Why use LangGraph here instead of a plain `while` loop? RepoPilot now needs a first-class graph
topology, named node boundaries, conditional revision, durable state hand-off, and a clear place to
add future approval or parallel research branches. Why not model every action as a graph node? Tool
selection is language-dependent and belongs in the role harness; forcing it into static routing
would enlarge the graph without strengthening safety.

### Planner

In live-provider mode the Planner requests a JSON plan containing retrieval queries,
subquestions, and completion criteria. RepoPilot parses, deduplicates, length-limits, and
count-limits that output locally. Malformed JSON, Provider failure, or Provider fallback selects a
deterministic lexical plan and marks the run degraded; it does not hand an invalid plan to later
nodes. Deterministic mode directly creates the local plan and follows the same state transitions.

### Researcher

The Researcher is the only role allowed to request repository tools. Its tool surface is an
explicit registry of read-only, idempotent operations such as repository search and chunk read.
Every argument is checked against JSON Schema, every result becomes an observation, and repository
content is treated as untrusted data that cannot change system rules or tool permissions.

The model may choose which registered tool to call within its budget. Unknown tools, invalid
arguments, timeouts, repeat fingerprints, and partial failures are recorded and mark the run
degraded without discarding already collected valid evidence. Same-turn concurrency is allowed
only when every selected tool is read-only.

This is an inner agent loop: model response → validated tool calls → observations → next model
response, until the model stops or a hard budget terminates the node. The outer StateGraph sees one
Researcher node result, while trace events retain the internal tool trajectory.

### Reviewer

Review is deliberately split into two layers:

1. A deterministic hard gate verifies the latest stored chunk still exists, removes duplicates,
   applies score/coverage thresholds, and requires a resolvable source citation.
2. In live-provider mode an LLM reviews relevance and entailment among hard-gate candidates and may
   request bounded additional queries.

The semantic reviewer receives the completion requirements, already executed queries, and current
candidates. It can only remove evidence from the hard-gate set; it cannot promote a stale,
low-coverage, duplicate, or unresolvable chunk. The local controller—not the model's
`needs_revision` boolean alone—decides the next edge. It fingerprints additional queries, permits at
most two novel queries per round, requires an explicit missing-requirement rationale, and stops when
a revision adds no candidates. Malformed/fallback review output uses the hard-gate decision and
marks the task degraded.

### Writer

The Writer receives only final accepted evidence; rejected chunks, raw repository indexes, and
untrusted tool instructions are excluded from its prompt. A live model may add a synthesis with
numbered `[n]` references. RepoPilot then validates that citations exist and stay within the
accepted-evidence range. Invalid output, Provider fallback, or generation failure is discarded and
the deterministic evidence section remains the report. With no accepted evidence, Writer is
skipped and the workflow refuses unsupported conclusions.

## Durable state and recovery

Each node writes a versioned WorkflowState snapshot containing at least:

- schema version, goal, and next node;
- plan queries, subquestions, completion criteria, executed queries, and pending revision queries;
- candidate chunk IDs with retrieval score and coverage;
- reviewed chunk IDs with accepted/rejected status, reason, and optional semantic score;
- current review round, previous revision candidate IDs, missing requirements, and task-global
  token/tool budget counters;
- `degraded` plus structured degradation reasons.

On resume, the graph restores this state and continues from the recorded node/round instead
of discarding the checkpoint and restarting the entire workflow. Candidate IDs are resolved against
the latest corpus; missing IDs indicate corpus drift, which is disclosed as degradation and causes
safe re-research. Evidence persistence replaces the task's final review snapshot transactionally,
so resume and review loops do not create duplicate rows.

Duplicate tool fingerprints are scoped to one Researcher node execution and are not presented as an
intra-node resumable log. Checkpoints are committed at node boundaries. Provider lifecycle events
are a diagnostic timeline, not checkpoints: a process crash may leave an old `call_id` without a
terminal event, and recovery starts a new Provider call. This is node/round-level recovery, not
replay of an in-flight HTTP/model request. If interruption happens before a node boundary commits,
that node's bounded idempotent work may run again; external side effects would require operation IDs
and a durable effect log before they could be advertised as exactly-once.

SQLAlchemy `TaskStore` is the single durable source of truth for tasks, events, and versioned
WorkflowState checkpoints. LangGraph owns graph execution and routing, but RepoPilot deliberately
does not attach a second LangGraph checkpoint saver. Two persistence authorities would create
dual-write ordering, reconciliation, and ambiguous-resume problems; keeping one store also lets the
REST API, SSE, MCP, evaluation, cancel, and resume paths observe the same committed state.

## Provider and task streaming semantics

There are two separate streaming layers:

1. The OpenAI-compatible adapter sends `stream=true` upstream by default. It consumes SSE chunks,
   merges text and fragmented tool calls, captures finish reason/model/usage, validates the complete
   structure, and only then returns one `ModelResponse` to Planner, Researcher, Reviewer, or Writer.
   Unvalidated JSON fragments and Writer drafts are never exposed as the task answer. A Provider
   that returns ordinary JSON despite `stream=true` is also accepted. Streaming and
   `stream_options.include_usage` can be disabled independently for compatibility.
2. The FastAPI task endpoint streams durable task events to the browser. It is not a token proxy for
   the upstream model response.

Provider calls emit content-free lifecycle phases: `started`, `first_byte`, `progress`, `completed`,
`retry`, `timeout`, `failed`, and `cancelled`. Periodic progress records distinguish
`waiting_first_byte` from `receiving` and include only bounded counters. TTFT, terminal request
latency, phase counts, and dropped telemetry are exported to Prometheus. A telemetry sink failure is
counted and safely logged but cannot change inference success.

Task events are appended to SQLite with monotonically ordered per-task sequences. The SSE endpoint
first queries events newer than `Last-Event-ID`, then continues by configurable short polling until
terminal state or disconnect. When no durable event is available it can emit an unnumbered,
non-persistent `: keep-alive` comment. This heartbeat proves only that RepoPilot and the client
transport are alive; it must not be interpreted as upstream model progress. Response headers disable
transforming/buffering proxies where supported.

Provider callbacks and node checkpoints can append concurrently. `TaskStore` serializes sequence
allocation per task with an in-process lock and bounded uniqueness retry. This gives continuous
ordering for a single RepoPilot process and persisted database, not multi-process coordination,
push-based pub/sub, cross-replica fan-out, or exactly-once delivery.

## Interview-grade trade-offs

- **Why LangGraph?** Named durable nodes and a conditional review loop are now product concepts,
  not incidental control flow. The dependency earns its place through inspectability and extension
  points, while repository/provider/storage interfaces remain framework-independent.
- **Why four roles?** They separate prompt context and authority: only Researcher gets repository
  tools, Reviewer cannot promote hard-rejected evidence, and Writer cannot retrieve new facts.
- **Why deterministic gates around an LLM?** Source freshness, citation resolution, permissions,
  budgets, and termination are invariants. Letting a model waive them would make recovery and
  evaluation non-reproducible.
- **Why SQLite polling SSE?** It is small, durable, testable, and adequate for a single self-hosted
  instance. Redis Streams/Kafka/PostgreSQL LISTEN-NOTIFY become justified with multiple workers or
  latency/fan-out SLOs.
- **Why buffer Provider SSE?** TTFT and progress become observable without allowing partial,
  schema-invalid role output to leak across the Harness boundary. Token-level user rendering would
  require a separate validated protocol for each role and is intentionally not conflated with task
  event streaming.
- **Why deterministic baseline?** It isolates workflow/retrieval regressions from model drift. Live
  Provider experiments answer a different question and must record endpoint, model and config.
- **What is not exactly-once?** In-flight Provider calls. Checkpoints describe committed state at
  node boundaries; they do not serialize remote execution or guarantee byte-identical replay.
- **Why does Writer refuse on a last-moment corpus drift instead of looping back?** Recovery preflight
  and Reviewer normally re-retrieve changed chunks. A change after Reviewer commits is a narrower
  race: Writer clears the stale evidence snapshot, marks `corpus_drift`, and refuses without calling
  the model. Adding a Writer → Researcher edge would expand the advertised topology and can lose
  liveness under continuous ingestion unless a second retry budget is introduced.

## Provider, usage, guarded, and degraded semantics

The OpenAI-compatible adapter is optional and receives base URL, model, and key only from settings.
It has separate connect/read/write/pool timeouts. The resilient wrapper applies bounded retry and
circuit breaking before using the deterministic fallback. Retries share one logical `call_id` with
increasing attempts; fallback emits its own terminal lifecycle event. A fallback response carries
explicit provenance (`fallback_used`) through model response, runtime trace, workflow state, and
final task status; fallback never masquerades as a successful live-model answer.

When the Provider supplies streaming usage, RepoPilot uses it. Compatible endpoints that omit usage
receive a conservative character-based estimate so the task-global Token budget is not silently
recorded as zero. Lifecycle metadata marks `usage_reported` versus `usage_estimated`; an estimate is
budget accounting, not official billing data.

Durable Provider metadata is flattened through an explicit allowlist. It may include call/attempt,
configured and served model, purpose, elapsed time, progress counters, safe error codes, timeout
phase, usage counters, and fallback provenance. It excludes base URLs, keys, prompts, completions,
raw exception text, response bodies, token deltas, response IDs, and tool arguments.

`degraded` and `guarded` are different axes. `degraded=true` records fallback, protocol failure,
corpus drift, or another reduced-capability condition and may accompany either a completed or
guarded task. The workflow returns terminal `guarded` when review limit, evidence/query stagnation,
or task-global Tool/Token exhaustion prevents a safe normal completion. Optional enrichment
failures preserve verified evidence; failures that prevent evidence verification result in refusal
rather than unsupported generation.

## Evidence and retrieval

Every repository claim in the deterministic report maps to a stored chunk citation of the form
`source_uri:Lstart-Lend`. Document versions are content-addressed; current retrieval resolves the
latest version. Tokenization emits lowercase words, snake_case subtokens, and CJK character bigrams.
A deterministic feature-hash cosine score is only a weak bonus on top of BM25, not a learned semantic
embedding. Model-based review does not change that retrieval claim.

## Security boundaries

- Secrets exist only in environment variables and are redacted from JSON logs and exception chains.
- Repository documents and tool observations are untrusted data, never executable instructions.
- The production workflow exposes only registered read-only repository tools; unknown names fail
  closed.
- Workspace resolution rejects traversal and symlinks; ingestion applies one allowlist to directory
  walks and explicit files, excluding VCS/config directories, credential-like files, and private-key
  formats before content enters the index.
- Model decisions cannot increase permissions, bypass hard evidence gates, change budgets, or select
  arbitrary filesystem/network operations.

## Module map

```text
src/repopilot/
  api.py             FastAPI app: REST, SSE, upload, metrics, static UI
  cli.py             serve / ingest / eval / mcp subcommands
  workflow.py        LangGraph StateGraph, role nodes, routing and WorkflowState
  research_tools.py  schema-validated read-only repository tool registry
  runtime.py         reusable model/tool loop, budgets, retries, fallback provenance
  tools.py           ToolSpec and fail-closed ToolRegistry
  service.py         task spawn, events/checkpoints, resume, cancellation
  ingestion.py       safe workspace walk and line-citation chunking
  retrieval.py       tokenization, BM25, hashed bonus, HybridRetriever
  evaluation.py      fixed-dataset runner and release metrics
  mcp.py             separate read-only MCP stdio surface
  providers/         base, deterministic, openai_compatible, resilient, factory
  storage/           tasks, documents, checkpoints, evidence, memory, eval runs
```

## Scope boundary

The architecture is complete for a single-user, self-hosted, read-only repository research product.
It does not claim enterprise multi-tenancy, distributed scheduling, internet-scale indexing, or
measured live-provider throughput. Those require a durable worker queue, cross-replica event bus,
tenant-aware storage and authorization, learned retrieval services, capacity tests, and operational
SLOs rather than a change in marketing language.
