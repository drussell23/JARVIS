---
title: Gap #5 Slice 4 — Graduation + live-fire (2026-04-20)
modules: [tests/governance/test_gap5_slice4_graduation.py, scripts/task_board_livefire.py]
status: merged
source: project_gap_5_slice4_graduation.md
---

# Gap #5 Slice 4 — Graduation + live-fire (2026-04-20)

Closes Gap #5 — "Structured to-do lists (TaskCreate/TaskUpdate) —
the lightweight 'what am I working on right now' view."

## Flipped default

- `JARVIS_TOOL_TASK_BOARD_ENABLED`: `false` → **`true`**
  Model-facing task tools are available by default. Slice 2's
  per-call structural validation + bounded caps + empty-capability
  manifest all remain in force — graduation flips opt-in friction,
  NOT authority shape.

Unchanged (already default-on by Slice 3 design):
- `JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED` — advisory prompt
  subsection stays default-on (authority-free observability).

## Authorization-bar survival — all pinned by tests

17 graduation tests in `tests/governance/test_gap5_slice4_graduation.py`:

### Graduation pins (3)
- `test_4a` — default is True without env
- `test_4b` — prompt injection still default-on
- `test_4c` — policy ALLOWS with zero env overrides

### Opt-out pins (3)
- `test_4d` — explicit `"false"` opts out
- `test_4e` — case-insensitive (FALSE / False / ws-padded)
- `test_4f` — policy still DENIES on explicit opt-out

### Authority invariants preserved (3)
- `test_4g` — all 3 manifests still `frozenset()` (empty caps)
- `test_4h` — tools still NOT in `_MUTATION_TOOLS`
- `test_4i` — module still doesn't import iron_gate /
  risk_tier_floor / semantic_guardian

### Structural safeguards preserved (2)
- `test_4j` — bad_args deny still fires post-graduation
- `test_4k` — capacity caps still fire post-graduation

### Slice 3 orchestrator wiring preserved (2)
- `test_4l` — orchestrator still calls `close_task_board` in
  the `finally:` block
- `test_4m` — orchestrator still calls `render_prompt_section`
  at CONTEXT_EXPANSION

### Mixed-state matrix (2)
- `test_4n` — full-revert matrix (single-flag version of Ticket #4
  Slice 4 two-flag matrix)
- `test_4o` — task-tool off + prompt-on is a valid state

### Documentation (1)
- `test_4p` — env helper docstring carries graduation language
  (bit-rot guard)

### End-to-end (1)
- `test_4q` — full create → start → complete lifecycle works
  through the Venom surface with zero env overrides

## Live-fire proof — session livefire-gap5-1776743088

`scripts/task_board_livefire.py` spawns a real op + exercises the
full Venom dispatch surface (policy → handler → primitive → audit
log) with ZERO env overrides:

    [pre-flight]
      task_tools_enabled()        = True (graduated default)
      _prompt_injection_enabled() = True (authority-free default)

    [live] 6 tool calls through policy:
      task_create task A          (pending)
      task_create task B          (pending)
      task_update(start) task A   (in_progress)
      task_update(edit)  task B   (title change)
      task_complete task A        (completed)
      task_update(cancel) task B  (cancelled, reason captured)

    [log-grep] 8 [TaskBoard]/[TaskTool] INFO lines captured:
      - registry_created
      - task_created × 2
      - task_started × 1
      - task_updated × 1 (fields=title)
      - task_completed × 1
      - task_cancelled × 1 (reason=...)
      - board_closed final_task_count=2

    [shutdown] close_task_board returned True
    [artifact] .ouroboros/sessions/livefire-gap5-1776743088/
      summary.json (3925 bytes)
      debug.log    (1139 bytes)

    VERDICT: 8/8 checks pass.

Matches rigor bar of:
- Ticket #4 Slice 4 live-fire (livefire-1776740783)
- Phase C Slice 3d Semantic Index graduation
- GENERAL driver Slice 1b live battle matrix

## Combined Gap #5 scorecard — 107/107 green

| Slice | Scope | Tests |
|---|---|---|
| 1 | TaskBoard primitive (Option A) | 33 |
| 2 | Venom task tools (deny-by-default) | 35 |
| 3 | Advisory prompt + ctx shutdown hook | 22 |
| 4 | Graduation + invariant pins | **17** |
| **Total** | | **107** |

Plus: 2 existing tests renamed + reframed (encoded old defaults):
- `test_policy_denies_when_master_switch_absent` →
  `test_policy_denies_when_master_switch_explicitly_off`
- `test_classify_task_tools_enabled_default_false` →
  `test_classify_task_tools_enabled_default_post_graduation_is_true`

## Operational state post-closure

- **Fresh install**: model CAN call `task_create`/`task_update`/
  `task_complete`; CONTEXT_EXPANSION prompt carries the advisory
  subsection when tasks exist; per-op registry lazy-creates a
  TaskBoard on first touch and the orchestrator `finally:` hook
  closes it at op shutdown.
- **Kill switch**: `JARVIS_TOOL_TASK_BOARD_ENABLED=false` reverts
  Venom surface to Slice-2 deny-by-default behavior. Orthogonal
  flag for advisory prompt: `JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED=false`
  silences the subsection without disabling tools.
- **Manifesto compliance**: §1 (tasks never gain execution
  authority) + §4 (no silent persistence of scratch state — board
  is ephemeral, registry is process-local) + §6 (no Iron Gate /
  validator / merge branches on task state) + §8 (audit trail
  via synchronous INFO logs, not model-rewritable).

Gap #5 CLOSED.
