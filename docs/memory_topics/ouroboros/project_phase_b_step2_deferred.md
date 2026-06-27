---
title: Phase B Step-2 Deferred Work (2026-04-20)
modules: [backend/core/ouroboros/governance/general_driver.py, backend/core/ouroboros/governance/governed_loop_service.py, tests/governance/test_general_driver.py, tests/governance/test_general_gls_wire_in.py, orchestrator.py, backend/core/ouroboros/governance/providers.py]
status: merged
source: project_phase_b_step2_deferred.md
---

# Phase B Step-2 Deferred Work (2026-04-20)

Phase B closed on 2026-04-20 with all four subagent types
infrastructure-graduated: EXPLORE (graduated 2026-04-18), REVIEW +
PLAN (graduated 2026-04-20), GENERAL (infrastructure graduated
2026-04-20). The firewall perimeter, dispatch routing, DAG validation,
verdict synthesis, and output quarantine are all production-active.

**Phase C Slice 1a+1b CLOSED 2026-04-20** — `general_driver.py`
shipped, GLS wire-in landed, `JARVIS_GENERAL_LLM_DRIVER_ENABLED`
graduated to default `true` after the live battle test matrix
(Happy Path / Boundary Push / Tool Violation) proved 3/3 safety
properties against the real Claude API: allowlist enforcement,
scope containment, and mutation cap honored. Ticket 1 below is
CLOSED; Tickets 2+3+4+5 remain open; Tickets 6+7 added during
Slice 1b are tracked individually (7 CLOSED, 6 open).

What remains is explicitly the **cognitive interior** — the LLM-driven
execution bodies. These are scaffolded as dependency-injection seams
on each subagent's constructor; swapping a driver in is additive, not
a rewrite. The infrastructure never needs to change.

## High-priority tickets

### 1. GENERAL subagent — LLM-driven execution body — **CLOSED 2026-04-20**

**Status**: shipped in Phase C Slices 1a+1b. `general_driver.py` is
the canonical driver; `build_llm_general_factory` is wired at
`governed_loop_service.py`; default flag flipped to `true` after the
3-test live battle test matrix proved allowlist + scope + mutation
cap enforcement against the real Claude API. 32/32 tests green
under new defaults (test_general_driver.py + test_general_gls_wire_in.py).

**Latent sub-tickets spun out during Slice 1b** (tracked below as
Tickets 6/7/8/9 rather than reopening Ticket 1):
- Ticket 6: `ClaudeProvider.generate_text` public method (open)
- Ticket 7: Tool-call JSON schema in system prompt (CLOSED c34f504ed2 / 88b55c2107)
- Ticket 8 (NEW): max_mutations COUNT enforcement — **CLOSED
  2026-04-20** (Epoch 1). `ScopedToolBackend` now carries a
  per-instance mutation counter; every call to a tool in
  `_MUTATION_TOOLS` consumes one slot, and the adapter returns
  `POLICY_DENIED` with `reason=mutation_budget_exhausted` once
  `mutations_count >= max_mutations`. Slot consumed at
  authorization time (not inner-call success) so retry cannot
  bypass the cap. Regression pin:
  `test_max_mutations_count_gate_denies_second_edit_under_cap_1`
  + 4 companion tests. Exposes `mutations_count` / `max_mutations`
  properties for Ticket 9 hard_kill records preservation.
- Ticket 9 (NEW): hard_kill path preserves records — **CLOSED
  2026-04-20** (Epoch 2). `ScopedToolBackend` now writes every
  execute_async decision into a shared `state_mirror` dict; the
  executor's hard-kill wrapper reads the mirror to build a complete
  exec_trace with `tool_calls_made`, `mutations_count`,
  `mutation_records`, `call_records`, and `tool_names` preserved
  through cancellation. Driver's `except asyncio.CancelledError`
  emits a WARNING-level audit log before re-raising. Every failure
  path in `run_general_tool_loop` now routes through a uniform
  `_build_partial_trace` helper so the exec_trace shape is
  structurally identical across no_provider_wired / backend_init /
  policy_init / tool_loop_error / malformed_final / completed /
  hard_kill / malformed_driver_output. Regression spine: 9
  ScopedToolBackend tests (records + mirror) + 4 driver tests
  (failure-path preservation + CancelledError re-raise) + 3
  subagent integration tests (full-stack hard_kill + malformed +
  success). 111/111 total GENERAL-surface tests green.

**Scope** (historical, retained for design context): replace
`AgenticGeneralSubagent._execute_body` stub with a restricted Venom
tool loop driven by a GENERAL-specific system prompt.

- Seam: `llm_driver: Optional[Callable]` already on constructor;
  default factory passes None → stub returns
  `NOT_IMPLEMENTED_NEEDS_LLM_WIRING` placeholder.
- Pressure to honor: `invocation.allowed_tools` as hard tool
  whitelist (policy engine already enforces via `dispatch_subagent`
  Rule 0c); `invocation.max_mutations` as hard cap on mutating tool
  calls; `invocation_reason` as system-prompt context.
- Hard-kill wrapper already present (`asyncio.wait({task},
  timeout=llm_budget_s+30.0)`); driver integration slots into it.
- Output quarantine fence stays unchanged — the driver returns raw
  output, the executor wraps.
- Regression spine target: ~15-20 tests — known-good task completion,
  injected-goal rejection at runtime, tool-whitelist honor,
  max_mutations enforcement, output-quarantine after real LLM output.
- Graduation arc: 3 consecutive clean sessions where
  (a) firewall rejects known-bad inputs at dispatch, (b) clean tasks
  complete, (c) no tool call leaks outside the explicit allowed_tools
  grant. Per Manifesto §5 discipline.

### 2. PLAN subagent — LLM-driven DAG refinement

**Scope**: replace `AgenticPlanSubagent._partition_deterministic` with
an optional LLM-driven path that enriches the DAG with actual
dependency edges derived from import-graph analysis.

- Seam: `llm_planner: Optional[Callable]` already on constructor;
  default factory passes None → deterministic file-partition (fully
  parallel, zero edges).
- Current behavior: one unit per file, no edges → fully-parallel DAG
  → plan_exploit's `edges=0` path handles this perfectly.
- Step-2 upgrade unlocks sequential DAG scheduling (see ticket 3).
- Graduation arc: verify LLM-emitted DAGs still pass
  `dag_validator.validate_plan_dag` on ≥95% of ops; fallback to
  deterministic partition on any validation failure.

### 3. plan_exploit — sequential DAG scheduling (topological sort + wave execution)

**Scope**: extend `plan_exploit.try_parallel_generate` to handle DAGs
with `edges > 0` via topological sort + wave-by-wave parallel
execution.

- Current fallback: `edges > 0` → `reason=dag_has_edges` → legacy
  serial path. Slice 1b's correct scope-out.
- Step-2 upgrade: compute topological waves (each wave = units with
  no unresolved dependencies); run each wave in parallel via the
  existing `asyncio.Semaphore`; proceed to the next wave only when
  the current wave's results are merged. Inter-wave dependency
  passing via ctx updates.
- Unlocks LLM-driven DAGs with real edges (ticket 2 prerequisite).
- Regression spine: ~10 tests — 2-wave sequential, 3-wave deep,
  diamond DAG (A→{B,C}→D), mid-wave failure → wave fallback,
  inter-wave ctx propagation.

## Secondary tickets

### 4. ExecutionGraphScheduler integration for in-repo DAGs

Currently materialized only for cross-repo execution graphs
(orchestrator.py::_materialize_execution_graph_candidate). In-repo
DAGs bypass the scheduler via plan_exploit's direct
asyncio.gather. Aligning both paths around the scheduler would
consolidate the concurrency discipline.

### 6. Refactor ClaudeProvider to expose public generate_text interface (NEW)

**Origin**: Slice 1b live battle test (2026-04-20) exposed that
``general_driver.py::_generate_fn`` must reach into
``provider._client.messages.create`` to satisfy
``ToolLoopCoordinator``'s ``(str) -> str`` generate_fn contract. The
existing ``_generate_raw`` closures in ``providers.py:3712`` and
``providers.py:5147`` use the same private-attribute pattern.

**Scope**: add a public ``async def generate_text(prompt: str, *,
system_prompt: str = "", max_tokens: int, model_name: str = "",
task_profile: str = "") -> str`` method to both ``ClaudeProvider``
and ``DoublewordProvider``. Internally it calls ``_ensure_client()``
then ``_client.messages.create`` with the provider's canonical
model. Migrate the three callers: general_driver + the two existing
``_generate_raw`` closures. Remove the private-attribute reach-in
from all of them.

**Why it's not Phase B**: the live tests are passing with the
current pattern; this is hygiene cleanup that consolidates a
private coupling. Tracked as signature-drift risk via 2 regression
tests (``test_run_loop_reaches_client_messages_create`` and
``test_run_loop_handles_null_client_gracefully``). The regression
tests will guard against SDK-surface changes until the public
method lands.

### 7. GENERAL tool-call JSON schema in system prompt — **CLOSED 2026-04-20**

**Status**: shipped in commits c34f504ed2 + 88b55c2107.
`GENERAL_SUBAGENT_SYSTEM_PROMPT_TEMPLATE` now includes the 2b.2-tool
schema with singular + parallel forms, a concrete `read_file`
example, and explicit "no prose, no markdown fences" directive.
Regression pinned via `test_render_prompt_includes_tool_call_schema`.

**Historical scope** (retained for design context):

**Origin**: Slice 1b live Test 2 (Boundary Push) — Claude replied
with PROSE ("I'll analyze the file and add the `_noop_guard`
function... Let me start by reading the target file.") instead of
emitting a tool-call JSON. The driver's parse_tool_call_response
returns None (not a tool call), the tool loop treats the prose as
a final answer, parse_general_final_answer returns None (not
general.final.v1), and the driver reports
``status=malformed_final``.

**Root cause**: ``GENERAL_SUBAGENT_SYSTEM_PROMPT_TEMPLATE`` tells
the model WHAT it can do (scope, tools, max_mutations) and WHAT
to emit as a final answer (general.final.v1 JSON), but does NOT
tell it HOW to format tool calls. Without the 2b.2-tool JSON
schema in the system prompt, the model can't structure a tool call
even when asked to.

**Scope**: extend the system prompt template to include the
existing ``_TOOL_SCHEMA_VERSION = "2b.2-tool"`` tool-call JSON
format (same format ``_parse_tool_call_response`` parses). Include
at least one concrete example with an allowed_tool (``read_file``)
and its arguments so the model has a template to mimic. Regression
test: new test_render_prompt_includes_tool_call_schema pins the
presence of ``tool_calls`` JSON format in the rendered prompt.

**Why it's not Slice 1b Step 0**: Step 0 proved the pipeline works
end-to-end. Test 3 (Tool Violation) succeeded because Claude
emitted a final answer, not a tool call — it didn't need the
tool-call schema to decline. Test 1 (Happy Path) succeeded because
Claude answered from prompt context alone (no tools needed for a
simple list-functions query). Test 2 is the only test that
requires actual tool-call emission, and its failure surfaces the
gap cleanly. Next slice fills the gap.

### 5. Step-2 for REVIEW — mutation testing activation

REVIEW's `mutation_runner` seam is wired but the default factory
passes None. `mutation_gate._is_critical_path` allowlist consultation
is now importable (fixed Slice 1b). Step-2: ship a default mutation
runner powered by `mutation_tester.run_mutation_test` for paths on
the allowlist. Gated by `JARVIS_REVIEW_MUTATION_RUNNER_ENABLED`.

## Why Step 2 is not Phase B

Phase B was explicitly defined as **infrastructure + boundary
enforcement**. The deterministic perimeter (firewall / DAG validator
/ verdict synthesis / output quarantine) is the non-negotiable
substrate; the fluid intelligence layer is a separate discipline
(agentic cognition) with its own risk profile (cost, prompt injection
surface under actual LLM execution, output-quality feedback loop).

Mixing the two at the same graduation gate would make the
infrastructure graduation dependent on LLM driver quality, which is a
moving target. Separating them means:
- The perimeter graduates when the perimeter is proven.
- Each driver graduates when that driver is proven.

Step-2 tickets are gated individually. Phase C (or whatever the next
epoch names) picks up from here.

## Where to start Step 2

Start with ticket **1 (GENERAL LLM driver)** — highest observable
value because it unlocks model-driven subagent execution for
open-ended tasks. The firewall surface means it's also the best-
protected driver to introduce: any prompt-injection escape attempt is
caught at dispatch, so driver development iterates under real pressure
without risking the main pipeline.

Skip tickets 2+3 until 1 has battle-test hours under its belt — they
depend on ticket 1's LLM driver pattern for their own seam fills.
