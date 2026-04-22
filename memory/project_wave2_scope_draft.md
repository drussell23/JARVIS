---
name: Wave 2 + Wave 3 scope draft — PLANNING ONLY (no implementation authorized beyond W2(5) Slice 1)
description: Apr 21 2026 — binding scope for orchestrator PhaseRunner extraction (W2 item 5) and capped-curiosity ask_human (W2 item 4); Wave 3 (6) and (7) gated on W2(5) stability. Read-only scope doc; only W2(5) Slice 1 authorized for implementation concurrently with this commit.
type: project
originSessionId: a1d89f9d-006f-4e50-8c53-c3d2ec21c317
---
# Wave 2 + Wave 3 — Scope Draft

**Status:** PLANNING ONLY for W2 item (4) + all of Wave 3. **W2 item (5) Slice 1 authorized** (operator go 2026-04-21).

**Non-goals:**
- Do NOT re-scope W2 as "build DirectionInferrer again" — Wave 1 #1 is delivered; W2 composes on top
- Do NOT widen execution authority (§1 unchanged)
- Do NOT touch §6 Iron Gate semantics (meaning preserved)
- Tier −1 Semantic Firewall applies to any new operator-visible persistence

---

## Wave 2 execution order (binding)

1. **(5) Orchestrator PhaseRunner extraction** — FIRST
2. **(4) Curiosity + capped `ask_human` variant** — after (5) first mergeable milestone

## Wave 3 — gated on W2(5) stability

- **(6) asyncio.gather fan-out rework** — blocked until (5) merged + stable
- **(7) mid-token `/cancel`** — blocked until (5) merged + stable

Dependency graph:
```
W2(5) PhaseRunner extraction arc ───► full graduation ───► [operator re-authorization]
                                                                    │
                                                          ┌─────────┴─────────┐
                                                      W3(6) fan-out        W3(7) mid-cancel
```

---

## W2 (5) — Orchestrator PhaseRunner extraction

### Motivation

`backend/core/ouroboros/governance/orchestrator.py` is **8,906 lines**; `_run_pipeline()` alone is **5,867 lines** implementing 11 phases as inline sequential blocks with no phase-handler abstraction. 193 broad-excepts live inside. Before any concurrency surgery (W3(6)) or mid-phase cancellation (W3(7)) is safe, the phases need a crisp, testable boundary.

### Goal

Move each of the 11 phases into a `PhaseRunner` subclass with a common `async run(ctx) -> PhaseResult` contract. **Zero behavior change per slice.** Orchestrator remains the caller; phase logic moves into named classes + files.

### Non-goals

- No rewrite of phase internals
- No policy changes
- No authority-surface widening
- No new features
- No cross-phase reorganization
- Orchestrator FSM shape (sequential advance via `ctx.advance(phase, ...)`) is preserved — we extract WHAT each phase does, not HOW they're sequenced

### Contract (Slice 1)

```python
# backend/core/ouroboros/governance/phase_runner.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

from backend.core.ouroboros.governance.op_context import (
    OperationContext, OperationPhase,
)


@dataclass(frozen=True)
class PhaseResult:
    """Uniform return shape across all PhaseRunners."""
    next_ctx: OperationContext
    next_phase: Optional[OperationPhase]  # None = terminal
    status: Literal["ok", "retry", "skip", "fail"]
    reason: Optional[str] = None
    artifacts: Mapping[str, Any] = field(default_factory=dict)


class PhaseRunner(ABC):
    """One phase of the orchestrator pipeline, extracted for clarity.

    Implementations must:
      * Be pure over (ctx, self-injected dependencies) — no global state reads
      * Return a new ctx via ctx.advance(...) rather than mutating
      * Honor the hash chain (context_hash, previous_hash) semantics
      * Be behaviorally identical to the pre-extraction inline block
        for the same input ctx (parity tests pin this)
    """
    phase: OperationPhase  # class attribute set by each subclass

    @abstractmethod
    async def run(self, ctx: OperationContext) -> PhaseResult:
        ...
```

### Slice plan (6 slices)

| Slice | Phase(s) | Size | Notes |
|---|---|---|---|
| 1 | Contract + **COMPLETE** pilot | 60 lines extracted | Smallest, cleanest. No retry loops. Minimal dependencies. Per-phase flag gate. |
| 2 | **CLASSIFY** | 663 lines | Risk profile + complexity + consciousness + goal memory. Heavier but still linear. |
| 3 | **ROUTE** + **CONTEXT_EXPANSION** + **PLAN** | ~315 lines total | Three small phases; batched. |
| 4 | **VALIDATE** + **GATE** + **APPROVE** + **APPLY** + **VERIFY** | ~2,500 lines total | Mid-size phases. VALIDATE and GATE have nested retry loops — extract faithfully. |
| 5 | **GENERATE** | 1,926 lines | The beast. Likely needs sub-extraction (generate-retry inner loop + tool-loop driver + candidate parser). Judgment call during the slice itself. |
| 6 | Dispatcher cleanup | ~100 lines | Replace orchestrator's inline `ctx.advance(...)` calls with a loop over a PhaseRunner registry. Orchestrator becomes a thin scheduler. |

Each slice ships independently; any slice is revertible without affecting prior ones.

### Per-slice discipline

1. **Flag-gated extraction** — `JARVIS_PHASE_RUNNER_<PHASE>_EXTRACTED` defaults `false`. When off: original inline code runs. When on: orchestrator delegates to the runner. Both paths must produce identical observable output.
2. **Parity test per phase** — feed the same ctx through inline path and runner path, diff the result. Tolerances for non-deterministic fields (timestamps) explicit.
3. **Full regression** — existing orchestrator tests must stay green.
4. **Short live-fire** — per slice: 120s idle, $0.30 cap battle test with flag `true`, confirm no runtime regression in real pipeline.
5. **Slice-specific graduation note** — `memory/project_wave2_phaserunner_slice_N.md`.
6. **Graduation** — after 3 clean sessions with the flag on, remove the flag entirely; runner becomes canonical.

### Post-GENERATE soak (after Slice 5)

Longer enforcement soak — cost patterns, retry paths, L2 repair — confirming GENERATE extraction doesn't regress hot-path behavior. Target $1.00 cap, 300s idle, diff `summary.json` / `cost_by_phase` shape against a pre-extraction baseline session.

### Env flags introduced (by Wave 2 (5))

- `JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED` (Slice 1)
- `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED` (Slice 2)
- `JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED`, `_CONTEXT_EXPANSION_EXTRACTED`, `_PLAN_EXTRACTED` (Slice 3)
- `JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED`, `_GATE_EXTRACTED`, `_APPROVE_EXTRACTED`, `_APPLY_EXTRACTED`, `_VERIFY_EXTRACTED` (Slice 4)
- `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED` (Slice 5)
- `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED` (Slice 6 — final cutover)

All default `false`. Each graduates `true` at its own slice. Per prior arc discipline, graduated flags are later removed entirely.

### Rollback

Every extraction is opt-in via flag; `=false` reverts to inline code. No orchestrator data-shape changes. No new authority surfaces. Any slice can be abandoned mid-stream without affecting earlier slices.

### Pilot pick for Slice 1: **COMPLETE**

**Orchestrator lines 7073–7133.** ~60 lines. Rationale:

- Smallest footprint of all 11 phases
- Fewest dependencies: 4 orchestrator helpers (`_record_canary_for_ctx`, `_publish_outcome`, `_persist_performance_record`, `_oracle_incremental_update`)
- No retry loops, no conditional branches
- No orchestrator state mutation — ctx reads only, side effects via telemetry + ledger
- All inputs frozen
- Highest signal-to-noise ratio for establishing the contract pattern

CLASSIFY (663 lines, Slice 2) is the natural next step: more complex but still linear.

### Parity test strategy (Slice 1)

Since there's **no existing per-phase test coverage** of COMPLETE in isolation, Slice 1 writes from scratch:

1. Factory: `OperationContext.create(...)` with VERIFY-phase state set up (so COMPLETE can legally run)
2. Mock the 4 helpers via monkeypatch
3. Run inline path (flag `false`): record helper call order + args + final ctx state
4. Run runner path (flag `true`): record same
5. Assert identical: helper calls (by-arg), final `ctx.phase`, `ctx.terminal_reason_code`, `ctx.context_hash`

Tolerance: `context_hash` must match exactly (deterministic input); `inferred_at`-style timestamps handled explicitly.

### Slice 1 deliverables

- `backend/core/ouroboros/governance/phase_runner.py` — contract (ABC + dataclass)
- `backend/core/ouroboros/governance/phase_runners/__init__.py` — new package
- `backend/core/ouroboros/governance/phase_runners/complete_runner.py` — COMPLETE extraction
- `backend/core/ouroboros/governance/orchestrator.py` — 1 conditional delegation at line 7073
- `tests/governance/phase_runner/__init__.py`
- `tests/governance/phase_runner/test_contract.py` — ABC + PhaseResult
- `tests/governance/phase_runner/test_complete_runner_parity.py` — inline vs runner diff
- `memory/project_wave2_phaserunner_slice1.md` — graduation note

---

## W2 (4) — Curiosity + capped `ask_human` variant

### Prerequisites

- W2 (5) Slice 1+ merged (per operator binding)
- Consumes existing DirectionInferrer / posture signals from Wave 1 #1 (do NOT re-scope as "build inferrer again")
- Builds on existing `ask_human` tool in Venom (authority-free, already shipped)

### Shape

Enables the model to proactively ask low-cost clarifying questions **during exploration** (not just during NOTIFY_APPLY human-gated flows) when posture is EXPLORE or CONSOLIDATE. Per-session budget cap. Deny-by-default. Graduation arc like prior waves.

### Authority posture

- §1 additive only — `ask_human` is already authority-free; this adds **when/how it fires**, not **what it can do**
- §5 Tier −1 — the model-generated question text is persisted to session state; must pass Semantic Firewall sanitization (credential patterns, prompt-injection patterns)
- §6 Iron Gate unchanged
- §8 Observability — every curiosity question persisted + SSE-emitted + visible in `/ask-human history`

### Env flags (tentative)

- `JARVIS_CURIOSITY_ENABLED` master, default `false`
- `JARVIS_CURIOSITY_QUESTIONS_PER_SESSION` default `3`
- `JARVIS_CURIOSITY_COST_CAP_USD` default `0.05` (per-question soft cap)
- `JARVIS_CURIOSITY_POSTURE_ALLOWLIST` default `EXPLORE,CONSOLIDATE` (HARDEN skips — focus on stabilization)

### Not yet sliced

Slice plan deferred until W2(5) Slice 1 lands — exact integration points will depend on the extracted PhaseRunner surface (where in CLASSIFY or CONTEXT_EXPANSION does curiosity hook in?).

---

## Wave 3 — gated, scope sketch only

### (6) asyncio.gather fan-out rework

**Goal:** consolidate the current fan-out sites (L3 SubagentScheduler, multi-file VALIDATE, possibly GENERATE candidate dispatch) into a single memory-pressure-aware primitive with cancellation propagation + partial-failure semantics.

**Why gated on W2(5):** PhaseRunner boundary makes it obvious what "one phase's fan-out" means. Without that, concurrency surgery would be on an uncharacterized surface.

**Risk:** touches §3 concurrency physics. Larger blast radius than W2(5).

### (7) mid-token `/cancel`

**Goal:** `/cancel <op-id>` interrupts an in-flight LLM call mid-stream instead of waiting for the current phase to complete.

**Dependencies:**
- Anthropic SDK stream-abort support (upstream)
- PhaseRunner checkpoints (W2(5) contract)
- Scheduler cooperation (Arc B memory gate pattern reused)

**Why gated on W2(5):** need well-defined cancellation points in `PhaseRunner.run()` before mid-phase interruption is safe.

---

## Evidence chain expected at Wave 2 + Wave 3 full closure

- 6 PhaseRunner slice graduation notes + parity test suites
- W2(5) post-full-extraction soak report (`scripts/soak_logs/wave2_phaserunner_post_full_extraction.md`)
- W2(4) curiosity graduation note + 3-session clean arc
- Wave 3 (6) + (7) each get their own scope docs + graduation arcs post-authorization

## Re-scope guard

If during implementation we realize a slice needs to touch something outside "mechanical extraction" scope (e.g. refactoring the broad-except pattern, consolidating helper methods, changing phase ordering), **stop and re-authorize**. The binding is "zero behavior change per slice." Deviation is a separate ticket.
