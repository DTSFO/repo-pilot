# ADR 0001: LangChain / LangGraph / RepoPilot responsibility boundary

- Status: Accepted
- Date: 2026-07-27
- Release: 1.5.0

## Context

RepoPilot v1.4 used LangGraph for the outer workflow but implemented the standard OpenAI chat
transport, streaming SSE parsing, fragmented tool-call assembly and JSON role output parsing itself.
It also had two different model/tool loops: the API workflow used a one-turn Researcher path while a
separate runtime contained the real observation loop. The documentation described the latter as a
production capability even though the API did not execute it.

Those choices created three avoidable risks:

1. common Provider protocol code was larger than the repository-specific domain code;
2. tests could pass against a runtime that the production API did not use;
3. an interviewer or maintainer could reasonably ask why a LangGraph project bypassed the maintained
   LangChain model and structured-output integrations.

## Decision

Use the LangChain ecosystem in layers with one control plane:

| Layer | Owner | Responsibility |
| --- | --- | --- |
| Model integration | `langchain-core` + `langchain-openai` | messages, `ChatOpenAI`, Tool Calling, Pydantic structured output, streaming chunk aggregation, SDK response normalization |
| Workflow | LangGraph `StateGraph` | Planner → Researcher ⇄ Reviewer → Writer, conditional revision and bounded termination |
| Inner tool loop | RepoPilot `ToolCallingHarness` | tool allowlist, argument validation, observations, retries, duplicate detection, per-step/task budgets |
| Domain | RepoPilot | repository/revision isolation, ingestion, retrieval, Evidence Hard Gate, citations, reports, recovery, events, API and security |

Planner and Reviewer send explicit Pydantic schemas through `ModelRequest.response_schema`.
`LangChainOpenAIProvider` uses `with_structured_output(..., method="function_calling")`. Researcher
uses `bind_tools`; LangChain returns normalized tool calls and the RepoPilot Harness alone executes
them. The production workflow and runtime tests now use the same Harness.

`REPOPILOT_PROVIDER=langchain_openai` is the preferred configuration. The old
`openai_compatible` value and `OpenAICompatibleProvider` import remain compatibility aliases for
v1.4 deployments, but both route to the LangChain implementation.

## Why not `langchain.agents.create_agent`?

`create_agent` is appropriate when the product is primarily one general model/tool loop. RepoPilot's
top-level product behavior is a domain workflow with four different authorities:

- Planner cannot use repository tools;
- Researcher can use only registered read-only tools;
- Reviewer can remove evidence but cannot promote evidence rejected by deterministic gates;
- Writer cannot retrieve new facts and sees only accepted evidence.

Adding a generic agent graph around or inside that graph would duplicate routing, checkpoint and
termination ownership. RepoPilot therefore uses LangChain's model components and a small bounded
inner Harness, while LangGraph remains the only top-level state machine.

## Why not keep the custom HTTP adapter?

The deleted adapter duplicated maintained SDK behavior and exceeded 700 lines. The remaining adapter
contains only RepoPilot-specific concerns: safe lifecycle telemetry, error taxonomy, conservative
usage fallback, health probing and the provider-neutral `ModelResponse` boundary. Retry and circuit
breaking remain outside `ChatOpenAI` with `max_retries=0`, so there is one visible retry authority.

## Compatibility boundary

`ChatOpenAI` targets the official OpenAI API specification. A third-party endpoint that implements
standard chat completions and Tool Calling can be used through `base_url`. Non-standard fields such
as provider-specific reasoning payloads are not claimed to be preserved. Such providers require a
dedicated LangChain integration before RepoPilot advertises support.

## Consequences

Positive:

- less custom protocol code and a smaller security/maintenance surface;
- schema and tool-call behavior follow current LangChain abstractions;
- the documented Researcher observation loop is now the production path;
- model/provider replacement remains behind `ModelProvider`;
- framework selection has an explicit Build-vs-Buy rationale.

Trade-offs:

- `langchain-openai`, `openai`, and tokenizer dependencies increase the runtime image;
- Provider-specific extensions need an explicit integration instead of accidental passthrough;
- RepoPilot still owns domain-level retries, circuit breaking and telemetry, so the Provider layer is
  not zero code;
- deterministic retrieval remains the fallback and is not a learned embedding service.

## Rejected alternatives

1. Keep the v1.4 raw HTTP/SSE adapter: rejected because it duplicated commodity protocol work.
2. Replace the entire workflow with `create_agent`: rejected because it weakens explicit role and
   evidence boundaries.
3. Add the `langchain` meta-package without using its APIs: rejected as dependency theatre.
4. Treat every graph node as an independent Agent: rejected because nodes are roles in one bounded
   system, not autonomous principals with separate goals, memory and permissions.
