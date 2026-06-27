---
title: Phase 1 Subagent Graduation — POSTMORTEM (2026-04-18)
modules: [tests/governance/test_read_only_advisor_bypass.py, tests/governance/test_bg_readonly_cascade.py, tests/governance/test_read_only_schema_swap.py, tests/governance/test_read_only_graduation_polish.py, tests/governance/test_claude_stream_hard_kill.py, backend/core/ouroboros/governance/plan_generator.py, backend/core/ouroboros/consciousness/memory_engine.py, backend/core/ouroboros/governance/semantic_guardian.py, ledger.py, backend/core/ouroboros/governance/intake/unified_intake_router.py, backend/core/ouroboros/governance/providers.py]
status: historical
source: project_phase_1_subagent_graduation.md
---

# Phase 1 Subagent Graduation — POSTMORTEM (2026-04-18)

Manifesto §6 graduation criterion met: three consecutive clean Trinity
cartography sessions end-to-end. Master switch
`JARVIS_SUBAGENT_DISPATCH_ENABLED` flipped to default `true`. The
Synthetic Soul retains this knowledge for Phase B (CodeReviewerAgent,
ResearchAgent, RefactorAgent) and Phase C (PlanAgent replacing
plan_generator.py).

## Why: the organism needed fan-out

A single Claude call iterating tool rounds sequentially cannot
architect-map three disjoint subsystems inside one context budget.
Trinity cartography (`memory_engine.py` + `semantic_guardian.py` +
`ledger.py`) has no shared file paths and ~2000 lines of source per
subsystem. Sequential exploration poisons the main context with
cross-subsystem noise; parallel fan-out preserves context locality
per subsystem and produces independent finding rollups the parent
can synthesize cleanly.

Phase 1 shipped the **explore** subagent type only. It uses the
existing deterministic AST+regex backbone (`ExplorationSubagent` +
`ExplorationFleet`), NOT a separate LLM — each subagent costs
$0.0000 in provider charges. The dispatch is model-driven (Claude
calls `dispatch_subagent` as a Venom tool during the parent's tool
loop), the execution is deterministic. This inverts the OpenClaw
paradigm: Claude as orchestrator, AST as worker, not the reverse.

## What the graduation arc looked like

Sessions 14 / 15 / 16 on the Trinity coupling-map backlog task. Each
produced identical structural output:

```
Trinity op lifecycle (all 3 sessions, identical):
  BacklogSensor enqueued
  Worker 0 picked up → read-only ceiling 900s  [Layer 1]
  Read-only intent inferred  (pre-CLASSIFY stamp)
  Advisor recommend  read_only=True  blast=50  (bypass)
  PLAN phase failed (JSON parse) → continue to GENERATE
  gen_timeout 180s → 630s  [Layer 2]
  Nervous-System Reflex: BG topology skip bypassed → cascade Claude
  Fallback sem acquire  max_cap=570s  [Layer 3]
  ClaudeProvider: Venom tool loop kept active (Option A)
  Round 0: 3 × dispatch_subagent in parallel via asyncio.gather
    sub-01 → semantic_guardian: 37 findings / 6 tool_calls / diversity=3
    sub-02 → memory_engine:     21 findings / 4 tool_calls / diversity=3
    sub-03 → ledger:            22 findings / 4 tool_calls / diversity=3
  tool_round_complete  (80 findings delivered to parent)
  Claude synthesis round: 35-68KB gap analysis
  parser short-circuit: accepting any output shape (Option α)
  is_noop=True terminal_reason_code=read_only_complete
  POSTMORTEM root_cause=read_only_complete  seq=8
  ctx.advance(COMPLETE)  ledger APPLIED{reason:read_only_complete}
```

Session timings: 7m10s / 9m24s / 11m29s. Costs within $0.75 BG
read-only cap each (5× the mutating BG cap, multiplier specifically
for subagent fan-out + synthesis payloads).

## The five budget layers (all must align for success)

Scale wrong on any one layer and the op dies. Previous sessions in
the arc discovered each layer by failure:

| # | Layer | Name | Value | Failure mode if wrong |
|---|---|---|---|---|
| 1 | Outer | BackgroundAgentPool worker ceiling | 900s for read-only | Worker reaps op mid-synthesis (Session 8) |
| 2 | Middle | Orchestrator `asyncio.wait_for(generate, ...)` gen_timeout | 630s for read-only BG | Outer wait_for cancels before synthesis returns (Session 6) |
| 3 | Inner | `_call_fallback` `_max_cap` | 570s for read-only BG | Fallback budget starves Claude synthesis (Session 5) |
| 4 | Stall | DW stall budget for read-only BG | 60s | DW paused-state lockup (Session 3b) |
| 5 | Hard-kill | `asyncio.wait` grace around Claude stream | soft_timeout+30s | SDK wedged-cancel → 90-min silent deadlock (Session 13) |

Formula for layers 2+3: `base + MAX_PARALLEL_SCOPES(3) × PRIMARY_PROVIDER_TIMEOUT_S(90) + synthesis_reserve(180)`. Layer 1 is env-tunable via `JARVIS_BG_WORKER_OP_TIMEOUT_READONLY_S`. Layer 5 is hardcoded at the stream call site — it's defense-in-depth, not an operator knob.

## The non-budget fixes

Budget alignment alone is not enough. Three non-budget patches were structurally required:

### Read-only intent propagation + Advisor bypass (Manifesto §1 Zero-Trust)
- `OperationContext.is_read_only` — new hash-chained field
- `unified_intake_router.py` infers at submit via `infer_read_only_intent()` (word-boundary regex, conservative: positive signal AND no mutation verbs)
- `OperationAdvisor.advise(..., is_read_only=True)` skips blast_radius + test_coverage blocks
- Policy engine **Rule 0d** refuses every mutating tool under the read-only contract (reuses `scoped_tool_access._MUTATION_TOOLS`)
- Orchestrator APPLY short-circuit: VALIDATE→COMPLETE (no GATE, no APPLY, no VERIFY)

This is the *cryptographic guarantee* the Advisor bypass rests on: the Advisor says "blast radius is irrelevant because no mutation can happen", and Rule 0d + orchestrator short-circuit *make that true*. A label alone would be unsafe.

### Weaponized prompt schema (Option α — Manifesto §7 Attention Mechanism Supremacy)
When `ctx.is_read_only=True` the code-generation schema is replaced entirely with:

```
<CRITICAL_SYSTEM_DIRECTIVE>
This operation is mathematically locked into READ-ONLY mode. You are
forbidden from generating code.
You must execute deep architectural cartography. To do this, you MUST
utilize the 'dispatch_subagent' tool (subagent_type=explore).
Do not attempt to answer the prompt using your internal knowledge.
You must dispatch the subagent, await the JSON result, and then
summarize the findings.
</CRITICAL_SYSTEM_DIRECTIVE>
```

Without this, Claude emits `file_path`/`full_content` keys by reflex and the parser rejects them as `unexpected_keys`. With it, Claude calls `dispatch_subagent` in round 0 on every session.

### Parser short-circuit for read-only ops
`_parse_generation_response` returns `GenerationResult(is_noop=True)` immediately when `ctx.is_read_only`. No schema validation — the orchestrator's APPLY short-circuit doesn't care about candidate shape. Accepts any output. `raw_bytes` observed: 35KB–76KB across sessions.

### Hard-kill wrapper around Claude stream (Manifesto §3 Disciplined Concurrency)
The Session-13 lesson: `asyncio.wait_for` in Python 3.9 awaits the cancelled task. If the SDK doesn't respond to cancel, `wait_for` blocks forever. `asyncio.wait({task}, timeout=...)` returns `(done, pending)` without awaiting — abandons wedged tasks. The microkernel retains control over its own threads.

Pattern at the Claude stream call site:
```python
_stream_task = asyncio.create_task(_stream_with_resilience())
_hard_kill_budget_s = timeout_s + 30.0
done, pending = await asyncio.wait(
    {_stream_task}, timeout=_hard_kill_budget_s,
)
if pending:
    for _t in pending: _t.cancel()   # fire-and-forget, DO NOT await
    raise asyncio.TimeoutError("claude_stream_hard_kill:...")
await _stream_task  # re-raise exception if task completed
```

## What's NOT proven and what remains guarded

- **Only read-only intent unlocks dispatch_subagent fan-out by default.** Mutating ops still receive the full Iron Gate + APPLY + VERIFY pipeline unchanged. Phase B subagent types (plan/review/research/refactor) remain policy-denied in Rule 0c until their own graduation arcs.
- **The BG-pool worker pickup path** infers `is_read_only` from the op context stamped at intake. If a new intake path is added that bypasses `unified_intake_router.py`, the ordering guarantee (stamp before pool pickup) breaks and the read-only pool ceiling won't fire. Policy: every new intake entry point must call `infer_read_only_intent()` and pass to `OperationContext.create()`.
- **The subagent backbone is deterministic, not LLM-based.** Each subagent explores via AST walking + regex search. Findings are structured. `cost=$0.0000`. This is Phase 1's core economic thesis: the orchestration is LLM, the work is code.

## Commit trail (graduation arc)

```
0877e9e48c  feat(governance): read-only intent + Advisor bypass + Rule 0d
7ebf014ab8  fix(advisor): word-boundary match — dispatch ≠ patch
b71b8b86ce  feat(routing): Option A Venom unlock + Nervous System Reflex
f5ad8cda7a  feat(providers): Option α weaponized read-only schema
6270004dcc  feat(candidate_generator): fallback cap for fan-out (Layer 3)
8ae9f8a097  fix(orchestrator): gen_timeout for read-only BG (Layer 2)
cd5dd773ae  fix(intake): stamp is_read_only at submit (ordering)
e443df84e9  fix(bg_pool): worker ceiling for read-only (Layer 1)
cce6ec4f45  fix(providers): parser short-circuit for read-only
38094feec4  feat(governance): POSTMORTEM emission + cost cap multiplier
2d24eaf470  fix(budget): synthesis reserve 90s → 180s
2f3f7aa428  fix(providers): hard-kill wrapper (Layer 5)
```

## Regression spine (load-bearing)

- `tests/governance/test_read_only_advisor_bypass.py` — 34 cases (intent inference + Advisor bypass + Rule 0d + hash chain)
- `tests/governance/test_bg_readonly_cascade.py` — 17 cases (Nervous System Reflex + budget layer 3 + tight stall)
- `tests/governance/test_read_only_schema_swap.py` — 6 cases (Option α directive + schema suppression)
- `tests/governance/test_read_only_graduation_polish.py` — 7 cases (POSTMORTEM emission + cost cap multiplier)
- `tests/governance/test_claude_stream_hard_kill.py` — 5 cases (hard-kill wrapper structural + behavioral)

Total: **69 cases**. Each layer pinned; any regression breaks a test.

## How to apply this to Phase B

Phase B adds `review`/`research`/`refactor` subagent types. Graduation criteria will be structurally similar but the **contract** changes:

- **review/research** — still read-only. Can reuse every Phase 1 structural guarantee (read-only stamp, Advisor bypass, schema swap, parser short-circuit, hard-kill). The only differences are: (1) subagent implementation (review needs structural code analysis, research needs external doc fetching), (2) Rule 0c policy gate (explicitly allow the new type), (3) regression-spine additions.

- **refactor** — IS mutating. Cannot reuse the read-only short-circuit path. Needs its own Iron Gate 5-class diversity+confidence enforcement, its own APPLY fan-out (already exists for multi-file), and probably a new Rule 0e for "refactor tool allowed under specific subagent_type context". Graduation arc will need to prove *mutating* subagent fan-out produces clean APPLY+VERIFY, not just parent-synthesis prose.

- **plan** (Phase C) — replaces `plan_generator.py`. This is a *structural* change to the pipeline's PLAN phase, not a new subagent type exactly. The dispatch point moves from "Claude calls dispatch_subagent during tool loop" to "orchestrator calls PlanAgent during PLAN phase". Budget math needs re-derivation; the hard-kill wrapper should generalize to any provider call, not just Claude streams.

**How to apply:** when extending, reuse the five-budget-layer audit as a checklist for any new route/subagent combo. Don't trust `asyncio.wait_for` for external provider calls — use the `asyncio.wait` hard-kill pattern from `providers.py:5257`. The read-only contract (Rule 0d + APPLY short-circuit) is load-bearing, not decorative — preserve it or explicitly contract-design a replacement.
