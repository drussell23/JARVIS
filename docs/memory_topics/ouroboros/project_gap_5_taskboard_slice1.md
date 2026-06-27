---
title: Gap #5 Slice 1 — TaskBoard primitive (2026-04-20)
modules: [backend/core/ouroboros/governance/task_board.py, tests/governance/test_task_board.py]
status: historical
source: project_gap_5_taskboard_slice1.md
---

# Gap #5 Slice 1 — TaskBoard primitive (2026-04-20)

First slice of Gap #5. Closes the "structured to-do lists
(TaskCreate/TaskUpdate)" gap at the primitive layer.

## Authority posture (locked by authorization)

- **Observability-only**. Slice 1 emits structured state + a §8
  per-transition audit log. NOTHING branches on task state. Never
  Iron Gate, never policy, never validator / approval / merge gates.
- **Per-op lifetime** via Option A — lazy attachment to whatever owns
  the op (typically `OperationContext`). No orchestrator FSM hooks.
  Option B (per-FSM-terminal seal) explicitly rejected for
  maintenance hazard.
- **No `__del__` reliance**. Caller-driven lifecycle via explicit
  `close()` method. Grep-enforced: `test_taskboard_does_not_implement_dunder_del`
  fails loudly if anyone adds one.
- **Audit trail lives in the logging pipeline**, NOT in
  model-rewritable structures (§8).

## Design decisions

- **Post-terminal mutation behavior**: `TaskBoardClosedError` (explicit
  RuntimeError via custom exception). Chosen per authorization —
  "avoid silent corruption" bias.
- **Single-focus invariant**: at most ONE task in `in_progress` at a
  time. CC's surface allows multiple simultaneous active tasks;
  Slice 1 intentionally diverges + documents. If Slice 2 Venom
  wiring surfaces a concrete need to relax this, it gets its own
  test + comment.
- **Bounded capacity** (all env-tunable, captured at board birth
  to prevent mid-op env drift):
  - `JARVIS_TASK_BOARD_MAX_TASKS` default 50
  - `JARVIS_TASK_BOARD_MAX_TITLE_LEN` default 200
  - `JARVIS_TASK_BOARD_MAX_BODY_LEN` default 2000
  - Overflow → `TaskBoardCapacityError` (deterministic reject, NOT
    coalesce — would corrupt audit ordering).
- **Stable IDs**: `task-{op_id}-{seq:04d}`. Zero-padded so string
  sort matches numeric; monotonic per-board counter.
- **State machine** (strict):
  - `pending → in_progress` (start)
  - `pending → completed` (quick-win path)
  - `pending → cancelled`
  - `in_progress → completed`
  - `in_progress → cancelled`
  - Terminal states sticky — `completed`/`cancelled` raise on any
    further transition attempt.

## §8 audit contract

Every state transition emits a synchronous INFO line BEFORE the
method returns:

    [TaskBoard] task_created op=X task_id=Y sequence=N title=<preview>
    [TaskBoard] task_started op=X task_id=Y
    [TaskBoard] task_completed op=X task_id=Y
    [TaskBoard] task_cancelled op=X task_id=Y reason=<R>
    [TaskBoard] task_updated op=X task_id=Y fields=<F>
    [TaskBoard] board_closed op=X reason=<R> final_task_count=N

These are the authoritative history — operators grep debug.log for
`[TaskBoard]` markers. The in-memory board is just a read surface
for immediate queries; its state is ephemeral per Option A.

## Regression spine — 33 tests green

- Construction (2): op_id required, caps captured at birth
- Create (5): shape + monotonic IDs, empty/oversize title, oversize
  body, capacity cap deterministic reject
- State transitions (8): pending→in_progress, quick-win
  pending→completed, in_progress→completed, pending→cancelled,
  in_progress→cancelled clears active, terminal-sticky (completed),
  terminal-sticky (cancelled), unknown task_id raises
- Single-focus (3): second start raises, slot freed after complete,
  active_task None when nothing in progress
- Update (4): title/body, terminal-state rejected, empty update
  rejected, no-op on identical values
- Close semantics (4): close flag set, idempotent, post-close
  mutations all raise TaskBoardClosedError, reads still work
- Audit log (3): task_created format + op_id/task_id/sequence,
  task_started + task_completed, board_closed with
  final_task_count
- Immutability (2): Task frozen, snapshot is Tuple
- Authorization invariants (2): no `__del__` method (grep-enforced),
  module docstring carries Option A contract language

## What Slice 1 intentionally did NOT do

- No orchestrator wiring — the TaskBoard is a standalone module.
  Slice 2 will attach it via `ctx` alongside Venom tool registration.
- No Venom tool surface — `task_create` / `task_update` /
  `task_complete` come in Slice 2.
- No prompt injection — Slice 3 adds optional advisory surface.
- No bus events — logger-only in Slice 1. Bus integration is a
  future slice if a consumer needs it.
- No `__del__` / GC-time logging — per Option A, lifecycle is
  explicit via `close()`.

## What Slice 2 will consume

- `TaskBoard` + exceptions imported DIRECTLY from this module.
- Venom tools (`task_create` / `task_update` / `task_complete`)
  wrap the primitive with policy-gated handlers matching the
  Ticket #4 Slice 2 pattern (deny-by-default master env flag;
  when enabled, per-call validation + structured JSON result).
- `ctx.task_board` attachment (lazy) happens when the first tool
  touch fires; ctx shutdown path calls `board.close(reason=...)`.

## Files

- `backend/core/ouroboros/governance/task_board.py` (~380 lines) — primitive
- `tests/governance/test_task_board.py` (~475 lines, 33 tests)
