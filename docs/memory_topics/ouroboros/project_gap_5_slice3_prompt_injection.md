---
title: Gap #5 Slice 3 — Advisory prompt injection + ctx shutdown hook (2026-04-20)
modules: []
status: merged
source: project_gap_5_slice3_prompt_injection.md
---

# Gap #5 Slice 3 — Advisory prompt injection + ctx shutdown hook (2026-04-20)

Closes the third slice of Gap #5. Two deliverables:

1. `TaskBoard.render_prompt_section()` + orchestrator wiring at
   CONTEXT_EXPANSION — advisory "## Current tasks (advisory)" subsection
2. Single canonical `close_task_board(ctx.op_id, reason=...)` hook in
   `orchestrator.py::OperationRunner.run()` `finally` block (the
   Slice-2-deferred one-liner)

## Authority posture (locked by authorization)

- **Advisory and clearly delimited** — `## Current tasks (advisory)`
  header, explicit "**Not authoritative** — does not gate Iron
  Gate, validation, tool policy, or approval" preamble.
- **Tier -1 sanitization via `sanitize_for_log`** — title content
  passes through the same sanitizer the ConversationBridge applies.
  If the sanitizer strips a title to empty, task renders as
  `<redacted>` (not dropped) so audit coherence holds. "Don't fight
  the sanitizer blindly" contract honored.
- **Zero gating anywhere** — grep-enforced by
  `test_render_prompt_section_is_pure_read` + the Slice 2 import-
  surface test from `test_task_tool.py`.

## Slice 3 env knobs (all additive)

- `JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED` — default **`true`**
  (authority-free, default on; contrast with Slice 2 Venom tool
  flag which is deny-by-default). Opt-out via `"false"`.
- `JARVIS_TASK_BOARD_PROMPT_MAX_TASKS` — default 5. How many pending
  tasks show in the subsection; remainder collapses to
  `- ... (+N more pending)`.
- `JARVIS_TASK_BOARD_PROMPT_TITLE_PREVIEW` — default 120 chars.
  Per-task title preview length cap; prevents one monster title
  from blowing the prompt.

## render_prompt_section() contract

Returns `Optional[str]`. Returns `None` when:
- Env injection disabled
- Board is closed
- Board has no active + no pending tasks (all terminal / empty)

Otherwise renders:

    ## Current tasks (advisory)

    Model's per-op scratchpad. **Not authoritative** — does not gate
    Iron Gate, validation, tool policy, or approval. The self-declared
    work-in-progress view.

    ### Active (in_progress)
    - [task-op-X-0003] refactor auth module

    ### Pending
    - [task-op-X-0001] read test coverage
    - [task-op-X-0002] check existing patterns
    - ... (+3 more pending)

Completed + cancelled tasks are NOT shown — those live in the §8 audit
log via per-transition INFO lines, not in the model-visible prompt.

## Orchestrator edits

### CONTEXT_EXPANSION injection (line ~1516)

Mirrors the SemanticIndex injection pattern immediately preceding
it. Lazy read from the task_tool registry (`_BOARDS.get(ctx.op_id)`)
— does NOT lazy-create. If the model hasn't touched a task tool
during this op, no board exists → no subsection injected. Avoids
noisy empty subsections on every op.

When present, appends to `ctx.strategic_memory_prompt` with the
same `"\n\n"` separator pattern. Emits
`[TaskBoard] op=X inject_site=context_expansion prompt_chars=N` at
INFO for observability.

Failure path: catch-all wraps the whole block; `[Orchestrator]
TaskBoard injection skipped` at DEBUG. NEVER crashes CONTEXT_EXPANSION.

### Shutdown hook (line ~1160, `finally:` block)

Single canonical `close_task_board(ctx.op_id, reason="op terminal
phase=<PHASE>")` call. Sits alongside `self._cost_governor.finish()`
+ `self._forward_progress.finish()` — the pattern for per-op
cleanup. Idempotent (safe if the op never touched a task tool —
`close_task_board` returns False in that case). Wrapped in
try/except to match the neighbors' defensive posture — shutdown
cleanup NEVER crashes the pipeline.

## Regression spine — 22 new tests (90/90 combined across all Gap #5 slices)

- Env gates (5): default-on, opt-out, case-insensitive, max_tasks
  cap + default, title_preview cap + default
- Empty/closed/terminal-only states return None (4): master-off,
  empty board, closed board, all-terminal
- Content shape (5): single active, pending order, cap enforced,
  active+pending together, terminal states excluded
- Authority + sanitization (4): disclaimer present, Tier -1
  applied (control chars stripped), title preview cap, `<redacted>`
  fallback when sanitizer strips to empty
- Orchestrator wiring pins (2 CRITICAL): grep-enforced presence of
  `close_task_board` + `render_prompt_section` in orchestrator.py
- Pure-read invariant (2): render is side-effect-free, module
  docstring bit-rot guard for authority language

## Combined Gap #5 scorecard

| Slice | Scope | Tests |
|---|---|---|
| 1 | TaskBoard primitive (Option A) | 33 |
| 2 | Venom task tools (deny-by-default) | 35 |
| 3 | Advisory prompt + ctx shutdown hook | **22** |
| **Total** | | **90/90 green** |

## What Slice 3 intentionally did NOT do

- **No default flip for `JARVIS_TOOL_TASK_BOARD_ENABLED`** — stays
  `false`. Slice 4 owns the Venom tool graduation.
- **No live-fire battle test** — Slice 4 includes the live-fire
  proof alongside the graduation flip, per Ticket #4 pattern.
- **No extra orchestrator touches beyond the two authorized
  hooks** (CONTEXT_EXPANSION injection + `finally` shutdown).
- **No caller-site migration** — existing callers that don't touch
  task tools see zero behavior change.

## What Slice 4 will do

- Flip `JARVIS_TOOL_TASK_BOARD_ENABLED` default `false` → `true`
  (mirrors Ticket #4 Slice 4 two-flag discipline, only one flag
  here since Slice 3's prompt flag is already default-on by design)
- Add graduation-proof tests pinning new default + opt-out path
- Live-fire script exercising the full create→start→complete loop
  through the Venom surface under graduated defaults
- Closure record

Manifesto §1 (tasks never become execution authority) + §6 (no
validator / merge branches on task state) + §8 (audit trail via
logger, not model-rewritable) — all preserved through Slice 3.
