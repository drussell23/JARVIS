---
title: Project Op Lifecycle Stream
modules: [backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/governance/orchestrator.py, tests/governance/test_op_lifecycle_stream.py, backend/core/ouroboros/governance/task_tool.py, backend/core/ouroboros/governance/op_context.py, ledger.py, backend/core/ouroboros/governance/repair_engine.py, backend/core/ouroboros/governance/operation_advisor.py, backend/core/ouroboros/governance/intake/unified_intake_router.py]
status: historical
source: project_op_lifecycle_stream.md
---

May 12 2026 — closes the missing infrastructure piece exposed during SWE-Bench-Pro Phase B.2.2 substrate scoping. Dedicated branch `ouroboros/observability/op-lifecycle-stream`.

## Why the split (mirrors B.2.0's discipline)

Operator binding 2026-05-12: this PR is a structural improvement on its own merits, closing the SSE primary-path rendezvous gap for L3 + in-repo corpus + IDE extensions + SWE-Bench-Pro all simultaneously. Not a SWE-Bench-Pro special case. Shipping it as a standalone PR lets it merge, soak, and graduate on its own ladder without waiting on the envelope_builder + façade + spine work of B.2.1-3.

## Why the gap existed

The B.2.0 follow-on substrate audit revealed a structural mismatch between the operator binding ("primary path: SSE subscribe by op_id, await documented terminal event types") and the existing substrate:

- `StreamEventBroker.subscribe(op_id_filter=...)` exists ✓
- `task_completed` / `task_cancelled` exist as event-type constants ✓
- BUT these are scoped to the `task_tool.py` TaskBoard concept (tool-call boards), NOT to orchestrator operation-FSM terminal phases
- `OpsDigestObserver` is single-slot (battle-test harness registers SessionRecorder) AND only fires for APPLY/VERIFY/commit — misses advisor_blocked / cancel / pre-APPLY-failure paths entirely
- `OperationLedger` writes JSONL with no observer hook (would require polling to discover terminal state)

So the operator's "primary path" rendezvous could not actually be composed from existing surfaces. A B.2.2 evaluator built on the binding-as-written would hang.

## Architectural decisions

**Root problem solved at source — no shortcut**:

The shortcut would have been (Option D from the earlier `AskUserQuestion`): use a process-local `Dict[op_id, asyncio.Event]` inside the evaluator + observer-multiplex hook. That creates a parallel terminal channel the operator explicitly flagged "avoid unless necessary" — and it would have been a SWE-Bench-Pro-only piece of plumbing.

Another shortcut would have been extending OpsDigestObserver to multi-observer with partial-coverage caveat. But that protocol only fires for APPLY/VERIFY/commit; the evaluator would still hang on advisor_blocked, cancel, or any pre-APPLY failure path. Forbidden by operator binding "never unbounded wait" — even with `wait_for`, mis-classifying half the terminals as "never reached" defeats the benchmark's measurement integrity.

The structural fix: orchestrator's `_record_ledger` is the single chokepoint every state transition flows through. Adding a best-effort SSE publish AFTER a successful `ledger.append()` makes operation-FSM terminals first-class observable across the entire ledger surface — every terminal state, every phase, every operator-visible end-of-op outcome.

**Single seam + AST-pinned discipline**:

- `publish_operation_terminal` referenced EXACTLY ONCE in `orchestrator.py` (AST pin walks `Call` nodes by name; drift would mean parallel call sites issuing duplicate events)
- That single call lives INSIDE `_record_ledger`'s body (AST pin walks `AsyncFunctionDef`)
- That call is wrapped in `try/except` (AST pin walks `Try` nodes, asserts `publish_operation_terminal` appears inside one within `_record_ledger`)
- `ledger.append` positionally precedes the publish (AST pin uses `ast.unparse` + `find()` index comparison rather than line numbers — line numbers can lie under formatter shuffles, source-order text comparison is the truth-table)
- `op_context.py` does NOT reference `publish_operation_terminal` (defensive AST pin — `OperationContext.advance()` stays pure; hoisting the publish "closer to" the FSM transition is forbidden by construction)

**Naming distinction — operator binding hardening note 1 ("naming collision")**:

`operation_terminal` is unambiguously distinct from the `task_*` prefix (TaskBoard tool-call events). IDE clients consuming the SSE stream can filter on prefix to distinguish op-FSM lifecycle from tool-call lifecycle. AST pin asserts `not EVENT_TYPE_OPERATION_TERMINAL.startswith("task_")` so accidental renames can't collide.

**Substrate independence**:

The `ide_observability_stream.py` module does NOT import from `op_context.py` or `ledger.py`. The publish helper consumes duck-typed `ctx` + `state` parameters; the four terminal state strings are mirrored verbatim in a frozenset. This keeps the observability module's dep graph shallow and prevents an observability → state-machine cycle. AST pin asserts the four strings are a subset of the live `OperationState` enum (so the verbatim mirror stays honest).

**Idempotency via ledger dedup (operator binding hardening note 5)**:

`OperationLedger.append()` returns `False` on duplicate `(op_id, state)`. The wiring's `if written:` guard means duplicate appends suppress the follow-up publish naturally. No new dedup state; no race conditions. End-to-end integration test verifies: 3× `_record_ledger(..., OperationState.FAILED, ...)` on the same op_id → exactly ONE `operation_terminal` event on the broker.

## Composition discipline — what was deliberately NOT done

- No new SSE broker / parallel observability surface — composes existing `publish_task_event`
- No new event-type prefix collision — `operation_terminal` is unambiguously distinct from `task_*`
- No data-class side effects — `OperationContext.advance()` stays pure (defensive AST pin)
- No fan-out to multiple call sites — single-seam at `_record_ledger` (AST-enforced)
- No `op_started` symmetric event — minimal scope; intake visibility is a follow-on if a consumer needs it
- No new authority module imports in `ide_observability_stream.py` — substrate stays independent of `op_context` / `ledger`
- No graduation flip — master flag stays default-FALSE until soak evidence is collected
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/ide_observability_stream.py` — substrate (event constant + valid-types entry + env var + closed taxonomy + master-flag query + publish helper + register_flags)
- `backend/core/ouroboros/governance/orchestrator.py:~10113` — single-seam wiring inside `_record_ledger`
- `tests/governance/test_op_lifecycle_stream.py` — 36-test spine + 6 AST pins
- `docs/architecture/OUROBOROS_VENOM_PRD.md` — §40.7.10-b205 paragraph

## Master flag (FlagRegistry auto-seeded via §33.3 walker)

- `JARVIS_OP_LIFECYCLE_SSE_ENABLED` (BOOL/SAFETY, default FALSE) — master switch

## What this enables (next consumers)

1. **SWE-Bench-Pro Phase B.2.2 evaluator** (next PR): `evaluate_problem` façade composes `IntakeLayerService.ingest_envelope` + `StreamEventBroker.subscribe(op_id_filter=envelope.causal_id)` + `asyncio.wait_for(...)` for bounded terminal-event rendezvous. Source-agnostic. Composes only canonical surfaces.
2. **VS Code / Cursor / Sublime / JetBrains extensions** (Gap #6 IDE stream consumers): operation-FSM lifecycle becomes first-class observable. No code changes on the extension side — the existing SSE consumer auto-receives `operation_terminal` events.
3. **In-repo L2 exercise corpus** (PRD §40.7.10 follow-up arc C): the Router→Dispatcher accounting trace becomes auditable in real-time via the broker, not just via `debug.log` forensics.
4. **L3 worktree-isolated work**: parallel unit terminals become operator-observable as they happen.

## What's next

PR 3 — B.2.1 envelope builder + B.2.2 `evaluate_problem` façade + B.2.3 spine. Operator-bound design notes carried forward:
1. **Canonical evidence key**: B.2.1 MUST use `EVIDENCE_REPO_ROOT_KEY` constant from `operation_advisor.py` (B.2.0) — no parallel spellings.
2. **B.2.2 terminal wait via SSE primary path** — now actually composable thanks to this PR. Subscribe by `envelope.causal_id` (which becomes `ctx.op_id` downstream via `unified_intake_router.py:1159`), `await asyncio.wait_for(...)` with bounded timeout (env-overridable, never unbounded), one-shot ledger fallback on timeout at most (never polling-loop).
3. **B.2.3 AST pin** asserts terminal resolution goes through SSE broker first + documents the timeout fallback + asserts no unbounded `asyncio.wait` anywhere in the façade.
