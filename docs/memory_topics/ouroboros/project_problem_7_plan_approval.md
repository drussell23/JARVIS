---
title: Project Problem 7 Plan Approval
modules: [backend/core/ouroboros/governance/plan_approval.py, scripts/livefire_plan_approval.py]
status: merged
source: project_problem_7_plan_approval.md
---

## Status: CLOSED 2026-04-21

Shipped 5 slices. Combined 112/112 tests green.

## Context — what was already there

The orchestrator already had:
- `PlanGenerator` as a pipeline phase (schema plan.1 JSON output) between CONTEXT_EXPANSION and GENERATE.
- A plan-approval hard gate for COMPLEX/ARCHITECTURAL/HEAVY_CODE ops via `self._approval_provider` (`request_plan` / `await_decision`).
- An `InMemoryApprovalProvider` implementation.
- Env knobs `JARVIS_PLAN_APPROVAL_ENABLED` (default true), `JARVIS_PLAN_APPROVAL_ROUTES` (default "complex"), `JARVIS_PLAN_APPROVAL_COMPLEXITIES`, `JARVIS_PLAN_APPROVAL_TIMEOUT_S`.
- `plan_mode.py` — a DIFFERENT module, a dry-run pipeline simulator.

The gap: the approval only engages via complexity heuristics. Operators couldn't say "I want plan mode ON — halt EVERY op for my review."

## 5-slice arc

### Slice 1 — `PlanApprovalController` primitive (37 tests)
- `backend/core/ouroboros/governance/plan_approval.py`
- Per-op pending-plan registry keyed by op_id; asyncio Future-backed resolution
- State machine pending → approved / rejected / expired (sticky terminals)
- Bounded: 32 pending (default), 600s timeout (default), 2000-char reason cap
- Listener hooks for Slice 4 IDE wiring
- Env flag `JARVIS_PLAN_APPROVAL_MODE` (renamed from `_ENABLED` to avoid collision with orchestrator's existing gate)
- Authority invariant grep-pinned (no orchestrator/iron_gate/risk_tier/semantic_guardian/semantic_firewall/policy_engine imports)

### Slice 2 — Provider adapter + force-review hook (17 tests)
- `PlanApprovalProviderAdapter` — implements the orchestrator's `ApprovalProvider` protocol (`request_plan`/`approve`/`reject`/`await_decision`) on top of the controller. Drop-in replacement for `InMemoryApprovalProvider`.
- Maps `PlanApprovalOutcome` ↔ `ApprovalResult` / `ApprovalStatus`.
- `should_force_plan_review(ctx)` — one-line OR-in for orchestrator's `_should_gate` predicate; when plan mode is on, gate engages on EVERY op regardless of complexity heuristic.
- `await_outcome(op_id, timeout_s)` — Future-backed helper (replaced the initial polling design).
- Idempotent request_plan, caller-timeout honored, unknown request_id → EXPIRED (never hangs).

### Slice 3 — `/plan` REPL dispatcher (30 tests)
- `plan_approval_repl.py` — pure dispatcher returning `PlanDispatchResult(ok, text, matched)`.
- Commands:
  - `/plan mode [on|off]` — toggle `JARVIS_PLAN_APPROVAL_MODE`
  - `/plan pending` — list pending plans
  - `/plan show <op-id>` — render full plan detail
  - `/plan approve <op-id>` — approve
  - `/plan reject <op-id> <reason>` — reject with required reason
  - `/plan history [N]` — last N resolved plans
  - `/plan help` — usage
- `render_plan_detail(snap)` exported for Slice 4 reuse.
- Controller's `_resolve` now tolerates closed event loops in both `cancel()` and `call_soon_threadsafe()` — REPL-style sync resolution works cleanly.
- SerpentFlow integration is a 1-line downstream change — not touched in this arc.

### Slice 4 — IDE observability surface (16 tests)
- GET endpoints on `EventChannelServer`:
  - `/observability/plans` — list summaries (no full plan JSON)
  - `/observability/plans/{op_id}` — full projection
- SSE vocabulary extended with `plan_pending` / `plan_approved` / `plan_rejected` / `plan_expired` event types.
- `bridge_plan_approval_to_broker(controller, broker)` — one-call adapter wiring `PlanApprovalController.on_transition` → `StreamEventBroker.publish`. Whitelist-guarded (only plan_* events), returns unsubscribe callable.
- Summary-in-SSE / full-in-GET pattern: SSE frames stay bounded (< 500 bytes), IDE clients fetch detail on demand.

### Slice 5 — Graduation (12 tests)
- **Default stays `false`** deliberately. Unlike Gap #6 (where graduation flipped defaults), here graduation means "mechanism is production-ready; operator chooses when." Autonomy property of O+V would break with a graduated-true default.
- 12 pins prevent drift:
  - Default-false invariant + docstring bit-rot guard (names "slice 5" + "2026-04-21" + "operator choice")
  - Explicit on/off/unset full-revert matrix
  - Authority invariants on plan_approval.py + plan_approval_repl.py
  - ApprovalProvider-compatible adapter surface preserved
  - 4 SSE event_type constants pinned at wire values
  - 6 /plan REPL subcommands registered
  - IDE GET routes mounted
  - Per-op override (ctx.plan_approval_override) still wins

## Wire diagram

```
                                 [operator]
                                     │
                            ┌────────┴────────┐
                            │                 │
                  SerpentFlow REPL       IDE extension
                   /plan approve         GET /observability/plans/{op_id}
                            │                 │
                            ▼                 ▼
                   PlanApprovalController ─── read-only projection
                            │                 (no mutation via IDE)
                            │ on_transition
                            ▼
                       bridge_plan_approval_to_broker
                            │
                            ▼
                   StreamEventBroker → SSE /observability/stream
                   (plan_pending / plan_approved / ...)

Meanwhile, the orchestrator:
   PLAN phase → PlanApprovalProviderAdapter.request_plan(ctx, md)
                         │
                         ▼
                 PlanApprovalController.request_approval(op_id, plan)
                         │ (future.await_outcome)
                         ▼
                 await operator decision → APPROVE → GENERATE
                                         → REJECT  → POSTMORTEM
                                         → EXPIRED → POSTMORTEM
```

## Test counts

| Slice | Tests | File |
|-------|-------|------|
| 1 Controller primitive | 37 | `test_plan_approval.py` |
| 2 Provider adapter | 17 | `test_plan_approval_adapter.py` |
| 3 REPL dispatcher | 30 | `test_plan_approval_repl.py` |
| 4 IDE observability | 16 | `test_plan_approval_ide.py` |
| 5 Graduation pins | 12 | `test_plan_approval_graduation.py` |
| **Total** | **112** | all green |

## Env knobs

- `JARVIS_PLAN_APPROVAL_MODE` (default `false`) — operator modality toggle. `true` halts EVERY op for approval.
- `JARVIS_PLAN_APPROVAL_TIMEOUT_S` (default 600) — auto-reject timeout.
- `JARVIS_PLAN_APPROVAL_MAX_PENDING` (default 32) — registry capacity.
- `JARVIS_PLAN_APPROVAL_REASON_MAX_LEN` (default 2000) — rejection-reason cap.

Pre-existing (not touched):
- `JARVIS_PLAN_APPROVAL_ENABLED` (default `true`) — orchestrator's complexity-gate on/off.
- `JARVIS_PLAN_APPROVAL_ROUTES` / `_COMPLEXITIES` — which ops trip the complexity gate.

## Future work (not required for closure)

All resolved 2026-04-21 in commit `c3825d5503`:

- **SerpentFlow /plan dispatcher** — wired in `battle_test/serpent_flow.py` slash-command chain. `/plan` lines route through `dispatch_plan_command` before the generic ConversationBridge capture.
- **Orchestrator force-review hook** — `should_force_plan_review(ctx)` OR'd into `_should_gate` predicate. When `JARVIS_PLAN_APPROVAL_MODE=true` (or `ctx.plan_approval_override=True`), the plan approval gate engages for every op regardless of complexity.
- **Shadow-mirror over primary provider** — instead of swapping the injected `CLIApprovalProvider` (which would risk critical infra), orchestrator shadow-registers plans with the controller on `request_plan` and mirrors terminal outcomes on `approve`/`reject`. Primary approval authority stays with the injected provider; controller is read-only observability. Best-effort — mirror failures never affect the approval path.
- **End-to-end live-fire** — `scripts/livefire_plan_approval.py` simulates the orchestrator PLAN phase + halts + REPL approve/reject via dispatcher + broker captures SSE frames + history/pending commands. Journal at `.livefire/plan-approval-20260421-024018/journal.json` — PASS with both happy and reject paths proven.

Combined:
- **9 governance/observability test files** covering 112+ assertions (Python pytest)
- **1 end-to-end live-fire journal** proving the full four-item wire
- 0 changes to `CLIApprovalProvider` / `InMemoryApprovalProvider` — existing approval authority untouched
- 0 regressions in pre-existing orchestrator tests
