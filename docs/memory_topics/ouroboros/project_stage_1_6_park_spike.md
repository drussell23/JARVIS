---
title: Stage 1.6 — BG release / op park during LLM wait — feasibility spike (2026-05-13)
modules: [backend/core/ouroboros/governance/phase_runners/generate_runner.py, backend/core/ouroboros/governance/op_park_store.py, backend/core/ouroboros/governance/park_signal.py, tests/governance/test_op_park_store.py, tests/governance/test_bg_park_integration.py, docs/architecture/OUROBOROS_VENOM_PRD.md, backend/core/ouroboros/governance/ledger.py, backend/core/ouroboros/governance/flag_registry_seed.py]
status: historical
source: project_stage_1_6_park_spike.md
---

# Stage 1.6 — BG release / op park during LLM wait — feasibility spike (2026-05-13)

**Origin**: v13 (commit `f0e2e928dc`) shipped source-aware BG ceiling (Stage 1.5) and produced the first ever clean worker completions of the SWE-Bench-Pro arc, but our SWE op still hit the 900s ceiling. Stage 1.5 was a budget patch, not a coupling fix. Operator binding 2026-05-13:

> "At GENERATE entry (or the narrowest await boundary that already exists), the op must release BG pool occupancy while waiting on provider I/O, without losing single-flight / op identity / cancellation semantics. Prefer composing an existing 'park / resume / continuation' pattern if one exists; do not fork a second orchestrator."

## Why Stage 1.5 budget tuning can't close the gap structurally

The BG worker model: `worker_loop()` awaits one `_orch.run(ctx)` per iteration. The slot is held for the entire `await`. Raising the ceiling moves the kill line; it does not change the coupling between "scarce worker slot" and "long-blocking provider I/O." A SWE op consuming 3 LLM round-trips (PLAN + GENERATE + GENERATE_RETRY) under DW BACKGROUND cascade is naturally 900s+. With `JARVIS_BG_POOL_SIZE=3`, two such ops in flight saturate the pool while 13 other sensor signals starve.

## What "park" must do (correctness invariants)

1. **Slot release**: between the moment GENERATE submits its provider call and the moment the response is materialized, **no BG worker slot is held** for that op.
2. **Single-flight**: at most one outstanding provider request per `(op_id, generation_attempt)` — no double-dispatch on resume.
3. **Identity preservation**: `ctx.op_id` is stable across park→resume; ledger, SSE, and observability key off the same id.
4. **Terminal preservation**: on success/cancel/failure, the op reaches exactly one terminal ledger state (APPLIED / ROLLED_BACK / FAILED / BLOCKED). No "parked forever" failure mode (TTL + reaper).
5. **Cancellation**: `/cancel <op-id>` from REPL or harness shutdown drains parked ops cleanly.

## Why mid-`await` slot release is architecturally impossible

A BG slot **is** a `asyncio.Task` running `_worker_loop`. The worker can only return its slot to the pool by completing one iteration of the loop — which requires `await _orch.run(ctx)` to return. **You cannot release a slot from inside the `await`.**

Implication: park MUST be modeled as `orch.run(ctx)` returning early with a **park sentinel**, and a fresh `orch.run(ctx, resumed=True)` re-entering the queue when the provider call completes. This is structurally the same shape as the existing `OperationLedger.SUBAGENT_DISPATCH` + `/resume` pattern, just applied to GENERATE provider I/O instead of subagent dispatch.

## Narrowest seam (located)

`backend/core/ouroboros/governance/phase_runners/generate_runner.py`:
- Line 493: `orch._generator.generate(ctx, deadline)` — primary GENERATE provider call
- Line 1401: same call inside `_demote_to_standard_retry` path

These two sites are the only places GENERATE awaits the candidate generator. Wrapping them with a `park_around_provider(ctx, ...)` async context is the seam.

## 3-slice landing plan

### Slice 1 — substrate (default-FALSE, zero behavioral risk)

| Layer | Change |
|---|---|
| `ledger.py` | Add `OperationState.PARKED_GENERATE = "parked_generate"` enum value (additive — no existing site dispatches on enum identity beyond dedup) |
| **new** `backend/core/ouroboros/governance/op_park_store.py` | `ParkedOpStore` bounded in-memory registry: `park(token, ctx_snapshot, descriptor) -> Awaitable`, `complete(token, result)`, `cancel(token)`, `prune_stale(ttl_s)`. Backed by `asyncio.Event` + dict, weak-ref to ctx, TTL-prunable. |
| **new** `backend/core/ouroboros/governance/park_signal.py` | `ParkSignal` dataclass — the early-return sentinel carrying `(op_id, token, descriptor)`. Subclass of frozen dataclass, not exception (clean return path). |
| `flag_registry_seed.py` | Seed `JARVIS_BG_PARK_ENABLED` (BOOL/SAFETY, default FALSE per §33.1) + `JARVIS_BG_PARK_TTL_S` (INT/TIMING, default 1800) |
| **new** `tests/governance/test_op_park_store.py` | 20-30 spine: park/complete/cancel happy path, double-park rejected, TTL prune, weak-ref doesn't pin ctx, master-flag-off short-circuit, deterministic token generation, etc. |

**Operator review point**: Slice 1 is byte-identical at runtime (master-FALSE default). Substrate only. Safe to land independently.

### Slice 2 — orchestrator wiring (default-FALSE)

| Layer | Change |
|---|---|
| `op_context.py` | Add `parked_continuation_token: Optional[str]` field |
| `generate_runner.py:493 + 1401` | Wrap with `async with park_around_provider(ctx) as park:` — on entry, if master flag on AND BG queue has back-pressure (any pending op), park: emit `ParkSignal` instead of awaiting generator; on resume, fetch completed result from `ParkedOpStore` and proceed. |
| `background_agent_pool.py:798` | On `ParkSignal` sentinel return from `_orch.run`: persist `PARKED_GENERATE` to ledger, release slot, schedule out-of-pool provider task that on completion re-submits ctx to BG queue with `resumed=True`. |
| **new** `tests/governance/test_bg_park_integration.py` | 3 spine claims operator named: (a) `_workers_busy_count == 0` while mock-stalled provider blocks, (b) provider called exactly once across park→resume, (c) post-resume terminal ledger entry present + correct phase. |

**Operator review point**: Slice 2 default-FALSE flip → green soak → default-TRUE. Same graduation discipline as Phase 0 worktree-aware advisor.

### Slice 3 — graduation

| Step | Criterion |
|---|---|
| Soak Bar A | ≥1 SWE-Bench-Pro op reaches APPLIED/COMPLETE under unchanged 900s lease (or a tightened 600s lease — both prove the coupling fix) |
| Default-TRUE flip | After soak Bar A green |
| PRD doc | Update §40.7.10-stage1.6 with empirical closure |

## What I am NOT building yet (deliberate)

- ❌ Generator-internal park (touches 5+ route-specific paths in `candidate_generator.py` — too wide, route-specific quirks)
- ❌ Cross-process park (in-memory only for now — TTL reaper handles process death; a parked op surviving a process restart requires durable continuation store, which is Slice 4 if needed)
- ❌ Touching the SUBAGENT_DISPATCH ledger state (separate FSM — composing the same *shape* not the same *state*)
- ❌ Replacing `BackgroundAgentPool` with a new pool (operator explicitly forbade forking the orchestrator)

## Risk surface

| Risk | Mitigation |
|---|---|
| Lost provider result on process death | Slice-1 TTL prune kills parked ops past TTL; harness reaper sweeps `PARKED_GENERATE` on boot |
| Double-dispatch on race between park-emit and provider-complete | `ParkedOpStore.park()` is single-flight by `(op_id, attempt_seq)`; `complete()` is idempotent |
| Slot starvation if ALL ops park | Worker count is decoupled from parked-op count; queue continues to dispatch. If queue empty + N parked, workers idle (correct). |
| Resume re-entry priority inversion | Resumed ops re-enter PriorityQueue with original priority (preserved in `ctx.priority`); same 4-tuple heap shape (no precedence violation) |
| Cancellation while parked | `ParkedOpStore.cancel(token)` fires; ctx returns to next worker with cancellation flag; worker writes FAILED+reason=cancelled |

## Single-line operator ask before Slice 1 lands

> "Slice plan above. Land Slice 1 (substrate only, default-FALSE, zero runtime change) now? Or refactor anything in the plan first?"

## Cross-references

- `memory/project_v3_7_phase_2_harness_inject.md` — SWE-Bench-Pro harness boot hook that surfaces the SWE ops we're trying to land
- `memory/project_op_lifecycle_stream.md` — SSE substrate that will observe park/resume terminals if we want to surface them externally
- `docs/architecture/OUROBOROS_VENOM_PRD.md` §40.7.10-stage1-v12v13 — the empirical trail leading to Stage 1.6

## File coordinates (for Slice 1 land)

- `backend/core/ouroboros/governance/ledger.py:84` — add PARKED_GENERATE enum value
- `backend/core/ouroboros/governance/op_park_store.py` — NEW
- `backend/core/ouroboros/governance/park_signal.py` — NEW
- `backend/core/ouroboros/governance/flag_registry_seed.py` — 2 new seeds
- `tests/governance/test_op_park_store.py` — NEW spine
