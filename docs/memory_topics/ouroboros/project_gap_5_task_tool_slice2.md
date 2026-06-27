---
title: Gap #5 Slice 2 — Venom task tools (2026-04-20)
modules: [backend/core/ouroboros/governance/task_tool.py, backend/core/ouroboros/governance/tool_executor.py, tests/governance/test_task_tool.py]
status: historical
source: project_gap_5_task_tool_slice2.md
---

# Gap #5 Slice 2 — Venom task tools (2026-04-20)

Closes Slice 2 of Gap #5. Wires three policy-gated Venom tools on top
of the Slice 1 TaskBoard primitive — matches the Ticket #4 Slice 2
Monitor tool discipline.

## Non-negotiables honored (per authorization)

1. **Deny-by-default master env flag**: `JARVIS_TOOL_TASK_BOARD_ENABLED`
   defaults `false`. Policy Rule 17 in GoverningToolPolicy.evaluate
   fires BEFORE any allow path. Explicit `"false"` string edge tested.
2. **Manifest: read-only capability set**: all three tools have
   `capabilities=frozenset()` (empty). Not in `_MUTATION_TOOLS`.
   Allowed under `is_read_only` scope. Never `"write"`.
3. **Tight per-tool JSON schemas**: each tool's `arg_schema` names
   exactly what it needs. `task_update` uses a mutually-exclusive
   action-vs-content shape enforced at the classifier.
4. **Invalid args → deterministic deny codes**:
   `tool.denied.task_bad_args` with detail message from the shared
   `classify_task_args` helper (used by both policy layer + handler
   defense-in-depth).
5. **Wiring**: lazy `ctx.task_board` on first tool touch via
   `get_or_create_task_board(op_id)` process-wide registry. Tools
   only mutate through `TaskBoard` APIs — no parallel state.
6. **Single canonical close**: `close_task_board(op_id, reason)` is
   the ONE shutdown API; orchestrator call-site hook deferred to
   Slice 3 (the hook itself is a one-liner — users asked for it but
   also said Slice 3 scope freeze; documented as TODO in Slice 3
   authorization brief).
7. **Authority boundary**: no Iron Gate, no tool-policy side effects
   beyond this tool's own allow/deny, no merge/repair branching.
   Grep-enforced via `test_task_tool_module_does_not_import_gate_modules`.

## What shipped

### `backend/core/ouroboros/governance/task_tool.py` (~330 lines)

- Env helper: `task_tools_enabled()` — default false
- Registry: `get_or_create_task_board` / `close_task_board` /
  `registry_size` / `reset_task_board_registry` (test-only)
- Shared validator: `classify_task_args(tool_name, args)` — single
  source of truth for arg validation, called from both policy layer
  + handler
- Serializer: `_serialize_result(task, board)` — produces the
  documented JSON output shape
- Dispatcher: `run_task_tool(call, policy_ctx, timeout, cap)`

### `tool_executor.py` edits

- 3 new manifests in `_L1_MANIFESTS` with empty capability sets +
  tight arg schemas
- Async-native dispatch branch routing `task_create`/`task_update`/
  `task_complete` to `run_task_tool`
- Policy Rule 17: master env gate + shared `classify_task_args` call

## Output shape (all three tools)

    {
      "task_id":  str,
      "op_id":    str,
      "state":    "pending" | "in_progress" | "completed" | "cancelled",
      "title":    str,
      "body":     str,
      "sequence": int,
      "active_task_id": str | null,
      "board_size":     int,
    }

## Registry lifecycle

- **Lazy create**: first `get_or_create_task_board(op_id)` allocates
  a fresh `TaskBoard(op_id=op_id)` and stashes it in the
  `_BOARDS: Dict[str, TaskBoard]` registry.
- **Per-op isolation**: two ops get two independent boards.
  Pinned by `test_multiple_ops_isolate_in_registry`.
- **Canonical close**: `close_task_board(op_id, reason)` calls
  `board.close(reason)` then evicts from registry. Idempotent —
  returns False on unknown op_id. Pinned by 3 tests.

## Documented semantic: close-then-touch re-creates

Calling a tool on an op whose board was already closed lazily
creates a FRESH board (sequence restart at 1). This is deliberate
— matches the "ephemeral per-op" contract and avoids raising
on the very first touch after a registry eviction. Pinned by
`test_handler_rejects_mutations_after_board_close` which documents
the actual semantic. If future design requires "closed means
permanently closed for this op_id", the registry can track
closed-op ids separately; not needed today.

## Regression spine — 35 tests green

- Manifest + authority (3): registered with empty caps, not in
  _MUTATION_TOOLS, allowed under read-only scope
- Policy deny/allow matrix (8): master-absent deny, master-false
  deny, master-true allow, bad-args × 5 (create empty title,
  update missing fields, invalid action enum, action+content mix,
  complete missing task_id)
- Handler happy paths (6): create, update start, update cancel,
  update content, complete from pending, full lifecycle
- Handler failure modes (4): bad-args defense-in-depth, missing
  op_id, unknown task_id, close behavior
- Registry (4): lazy create, close unknown returns False, idempotent,
  multi-op isolation
- Preserved Slice 1 invariants (2): single-focus at tool level,
  terminal-sticky at tool level
- Authority invariants (2): module doesn't import gate modules,
  module imports primitive
- Helper pins (6): classify_task_args × 4 cases, env helper default
  + explicit true

## What Slice 2 intentionally did NOT do

- **No orchestrator ctx.close_task_board() call site.** User's
  scope authorization said "single canonical close() from existing
  ctx shutdown path — acceptable minimal orchestration touch";
  that one-liner is trivially deferred to Slice 3 where the
  orchestrator already gets touched for prompt injection wiring.
  For now, `close_task_board(op_id)` is a module-level API that
  any caller CAN invoke — it just isn't yet invoked automatically
  from the orchestrator.
- No advisory prompt injection (Slice 3)
- No default flip / live-fire (Slice 4)

## Files

- `backend/core/ouroboros/governance/task_tool.py` (~330 lines) NEW
- `backend/core/ouroboros/governance/tool_executor.py` (edits —
  3 manifests + dispatch branch + Rule 17)
- `tests/governance/test_task_tool.py` (~610 lines, 35 tests) NEW
