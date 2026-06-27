---
title: Project Wave3 Item7 Mid Op Cancel Scope
modules: [tests/governance/test_cancel_token_protocol.py, tests/governance/test_cancel_origin_class_d.py, tests/governance/test_cancel_origin_class_e.py, tests/governance/test_cancel_origin_class_f.py, tests/governance/test_cancel_phase_dispatcher_propagation.py, tests/governance/test_cancel_tool_loop_subprocess_kill.py, tests/governance/test_cancel_plan_exploit_parallel.py, tests/governance/test_cancel_parallel_dispatch_worktree.py, tests/governance/test_cancel_record_artifact_schema.py, tests/governance/test_cancel_sse_event_emitted.py, tests/governance/test_cancel_bounded_deadline_settle.py, backend/core/ouroboros/governance/]
status: merged
source: project_wave3_item7_mid_op_cancel_scope.md
---

## Status

- **OFFLINE SCOPE DOC ONLY** — no code, no flag flip, no battle-test session. Operator will pick which (if any) implementation slices to authorize after reading this.
- Substrate available on main: `_attribute_cancel` helper (`1a495c5ee9`) + cancel-attribution telemetry hooks at `_call_primary` / `_call_fallback`.
- Existing cooperative cancellation: `REPL /cancel <op-id>` (per CLAUDE.md) checked **at GENERATE and APPLY phase boundaries only**. This scope expands cancellation to fire **mid-phase**, deterministically, with sub-resource teardown.

---

## 1. Goals (in order of priority)

1. **Mid-phase cancellability**: an operator (or watchdog) can cancel an op while it is in flight inside any phase — including during a Claude stream, mid-Venom-tool-round, mid-PLAN-EXPLOIT parallel synthesis, or while an L3 worktree subagent is executing — and the op transitions to a terminal state within a bounded time.
2. **Cancel-source attribution**: every cancellation produces a single deterministic `cancel_origin` record (operator REPL / cost watchdog / wall watchdog / SIGTERM / sibling-task / retry-harness / etc.) that downstream POSTMORTEM can read without re-parsing exception traces. Class A/B/C from the existing `_attribute_cancel` extends to D/E/F (see §3).
3. **Sub-resource teardown**: the cancellation guarantees that no orphan subprocess, no orphan Claude/DW HTTP connection, no orphan worktree, and no orphan tool-loop task survives past the op's terminal state. (Best-effort for OS-level resources; deterministic for asyncio-level.)
4. **Audit completeness**: every cancellation produces a `cancel_record` durable artifact in the session dir, parseable by `LastSessionSummary` schema v1.2 (additive). Manifesto §8 — every autonomous decision is visible.

## 2. Non-goals

- **Auto-cancellation policy** (i.e., *when* to cancel — productivity-trip, cost-burn-too-fast, divergence-detected). That's a separate watchdog/policy concern; W3(7) provides the *mechanism*, not the policy.
- **Recovery from cancellation** (retry / resume / partial-state-replay). Cancelled ops go to terminal POSTMORTEM; retry is a separate decision the orchestrator already owns.
- **Cross-session cancellation persistence** (cancel that survives session restart). Cancel state is in-memory + per-session-log only.
- **Cancellation of *other* ops triggered by one cancel** (e.g., "cancel parent and all children"). Only the targeted op + its directly-owned sub-resources are in scope.
- **Reactive cancel-on-cancel** (e.g., re-cancel if cleanup itself stalls). Cleanup has a hard deadline; if it exceeds, we fall through to `os._exit`-class escape (out of scope here, lives in harness epic item 3).
- **Implementation in this scope doc.** Slices, code, flag flips — all wait for explicit operator authorization.

## 3. In-scope cancel classes

The taxonomy extends the existing A/B/C from `_attribute_cancel` (S7 telemetry first-pass) with three more:

| Class | Source | err_class signature | Deterministic? | Notes |
|---|---|---|---|---|
| A | Per-call timeout (`_FALLBACK_MAX_TIMEOUT_S`, etc.) | `TimeoutError`, own deadline expired | Yes | Already classified by `_attribute_cancel`. Extends to GENERATE retry-harness. |
| B | Outer wait_for (ToolLoop per-round budget) | `TimeoutError`, parent's deadline cap | Yes | Already classified. Extends to PLAN-EXPLOIT per-stream timeout. |
| C | Sibling-task cancel inside `asyncio.gather` | `CancelledError`, `cancelling()>0` (3.11+) | Best-effort (Py 3.9 `cancelling()` is no-op) | Already classified. Extends to L3 worktree subagent sibling-fail. |
| **D** | Operator REPL `/cancel <op-id>` (mid-phase, NOT phase-boundary) | `CancelledError`, attribution = `repl_operator` | Yes | NEW. Existing REPL only fires at boundaries. |
| **E** | Watchdog (cost cap, wall cap, productivity-trip, idle-timeout) | `CancelledError`, attribution = `watchdog:<which>` | Yes | NEW. Watchdogs currently can't fire mid-phase. |
| **F** | System (SIGTERM, SIGINT, harness shutdown, parent-process-died) | `CancelledError`, attribution = `system:<signal>` | Yes for signal source; best-effort for ordering across child tasks | NEW. Extends harness ticket B (partial summary on interrupt) into structured cancel rather than ad-hoc exit. |

**Class C ambiguity is preserved as-is.** If multiple cancel sources race (e.g., wall watchdog fires + operator REPL fires within 100ms), the *first*-emitted `cancel_record` wins for attribution; the second is logged as `superseded_by=<id>` and treated as a no-op.

## 4. Deterministic vs best-effort guarantees

### Deterministic (must hold every time)

1. **Origin attribution** — for cancel classes D/E/F, the `cancel_record.origin` field is filled at the *trigger* site (where the cancel was decided), not at the catch site. The trigger writes a sentinel before issuing the cancel; catch sites read it.
2. **Single terminal state** — once a cancel record is committed for an op, the orchestrator will not transition it back to in-flight. POSTMORTEM is the only valid next phase.
3. **Telemetry emission** — exactly one `[CancelOrigin]` log line per cancel decision (at trigger), and one `[CancelAttribution]` log line per task that catches the resulting `CancelledError`/`TimeoutError`. Counts match: `[CancelOrigin]` = unique cancels; `[CancelAttribution]` = unique tasks affected (≥ origins).
4. **In-process resources** — every asyncio task launched on the op's behalf either completes or raises `CancelledError` within `JARVIS_CANCEL_BOUNDED_DEADLINE_S` (default 30s) of the trigger.

### Best-effort (target, not guaranteed)

1. **OS-level subprocesses** — Venom `bash`, `run_tests`, `monitor` tools spawn subprocesses; cancel sends `proc.terminate()` then `proc.kill()` after a 5s grace. Process refusal (uninterruptible kernel state) is logged but not retried.
2. **HTTP streams (Claude, DW)** — cancel closes the underlying connection. The provider-side request may continue running until the upstream closes the socket; we don't wait for ack.
3. **L3 worktrees** — cancel triggers `WorktreeManager.kill(<unit-id>)` which `git worktree remove --force`s. Disk-level orphans are caught by `reap_orphans()` on next boot (already shipped per §2 progressive awakening).
4. **Class C disambiguation pre-Py-3.11** — `cancelling()` returns 0 on 3.9; we tag as `C_ambiguous` and rely on best-effort `all_tasks()` walk. This is acceptable: the *taxonomy* still helps; only the *automatic* disambiguation is degraded.

## 5. Interaction with existing systems

### 5.1 PhaseDispatcher

- Each `<Phase>Runner.run()` accepts a `cancel_token` (new) — a deterministic readable that returns the cancel record once a cancel has been triggered for the op. Runners check at every `await` boundary; long-running awaits (`asyncio.wait_for(provider.generate(...), timeout=N)`) wrap a `cancel_token.race(...)` helper that races the wait with the token.
- The dispatcher's existing terminal-status returns gain a new `next_phase = POSTMORTEM` shortcut for `status=cancelled`.
- No change to the per-phase artifacts threading: the cancel record rides on `pctx.extras["cancel_record"]` (single-writer at trigger).

### 5.2 ToolLoop (`tool_executor.py`)

- The outer `run_tool_loop` adds `cancel_token` propagation to every per-tool execution.
- Subprocess-spawning tools (`bash`, `run_tests`, `monitor`, `pytest`) gain a `cancel_token`-aware `proc.communicate()` wrapper: on cancel, `proc.terminate()` → 5s grace → `proc.kill()`.
- Parallel tool execution (`asyncio.gather` of multiple tools in one round) propagates cancel cooperatively: cancel of the round = cancel of all in-flight tool tasks.

### 5.3 PLAN-EXPLOIT (`plan_exploit.py`)

- §3 Disciplined Concurrency runs N parallel Claude streams (typically 3). On cancel, the orchestrating gather() cancels all N children. Each child catches `CancelledError`, attributes via `_attribute_cancel`, and contributes a partial-stream record to the cancel artifact.
- The merged-files synthesis is *abandoned* — no partial merge persists. The `[PLAN-EXPLOIT] status=cancelled` log line replaces the usual `status=completed`/`status=fallback`.

### 5.4 parallel_dispatch (`parallel_dispatch.py` — W3(6) Slice 4)

- The enforce path (`enforce_evaluate_fanout`) is already wrapped in `asyncio.wait_for(scheduler.wait_for_graph(...), timeout=wait_budget)`. On cancel, the timeout-style cancel propagates into the SubagentScheduler.
- Each L3 worktree subagent gets a `cancel_token` injected via `WorkUnitContext`. Subagents check the token at each I/O boundary; on cancel, the subprocess is killed and the worktree is `--force` removed.
- The fan-out result records `cancel_record` if any unit was cancelled; downstream POSTMORTEM reads from there.

### 5.5 Repair Engine (`repair_engine.py`)

- L2 self-repair iterations check the cancel token between iterations. Cancel mid-iteration aborts the current iteration's provider call but lets the *iteration* complete its bookkeeping (so the postmortem has clean iter/timebox accounting).

### 5.6 Existing REPL `/cancel <op-id>`

- The current implementation cancels at GENERATE/APPLY phase boundaries. Under W3(7), it gains a `--immediate` flag that fires mid-phase. Backward-compat preserved: bare `/cancel <op-id>` keeps phase-boundary behavior; `/cancel --immediate <op-id>` is the new mid-phase trigger.

## 6. Telemetry contracts

### 6.1 Log emissions (one-line, structured)

```
[CancelOrigin] op=<op-id> origin=<class>:<source> phase=<phase> cancel_id=<uuid>
  at_monotonic=<ts> reason=<short-text> initiator_task=<name>
  bounded_deadline_s=<float>
```

```
[CancelAttribution] op=<op-id> cancel_id=<uuid|->
  label=<call-site> err=<class> elapsed=<float>s remaining=<float>s
  class=<A|B|C_external|C_ambiguous|D|E|F|non_cancel>
  own_cancelling=<int> canceller_task=<name>
  cleanup_status=<clean|orphan_subprocess|orphan_worktree|orphan_http>
```

`cancel_id` is `-` for classes A/B (own-deadline timeouts have no upstream origin record).

### 6.2 Durable artifact

`<session_dir>/cancel_records.jsonl` — one JSON object per `[CancelOrigin]`. Schema:

```json
{
  "schema_version": "cancel.1",
  "cancel_id": "uuid",
  "op_id": "op-...",
  "origin": "D:repl_operator|E:cost_watchdog|F:sigterm|...",
  "phase_at_trigger": "GENERATE|VALIDATE|...",
  "trigger_monotonic": 12345.67,
  "trigger_wall_iso": "2026-04-25T01:23:45Z",
  "bounded_deadline_s": 30.0,
  "reason": "operator-supplied or watchdog-supplied short text",
  "tasks_cancelled": [{"name": "...", "elapsed_s": ..., "cleanup_status": "..."}],
  "settle_monotonic": 12345.92,
  "settle_within_deadline": true
}
```

Read-only consumers: `LastSessionSummary` (digest), `POSTMORTEM` runner (root_cause classification), `IDEObservabilityRouter` (`/observability/cancels` GET — additive).

### 6.3 SSE event type

New `cancel_origin_emitted` SSE event in `IDEStreamRouter`'s 10-event vocab → 11-event vocab (additive; existing 10 unchanged). Payload: cancel_id + op_id + origin + phase. Full record at GET endpoint.

## 7. Test plan outline

### 7.1 Unit tests (offline; no live battle session)

| File | Coverage |
|---|---|
| `tests/governance/test_cancel_token_protocol.py` | `CancelToken` primitive: creation, `is_cancelled`, `race(coro)`, `wait()`, idempotent set |
| `tests/governance/test_cancel_origin_class_d.py` | REPL operator path triggers `[CancelOrigin] origin=D:repl_operator` on test orchestrator |
| `tests/governance/test_cancel_origin_class_e.py` | Each watchdog (cost, wall, productivity, idle) triggers correct `E:<watchdog>` origin |
| `tests/governance/test_cancel_origin_class_f.py` | SIGTERM / SIGINT / SIGHUP route to `F:<signal>` origin |
| `tests/governance/test_cancel_phase_dispatcher_propagation.py` | Cancel fired mid-phase: each runner returns `status=cancelled`, dispatcher routes to POSTMORTEM |
| `tests/governance/test_cancel_tool_loop_subprocess_kill.py` | `bash` / `run_tests` subprocess: terminate→5s→kill chain pinned |
| `tests/governance/test_cancel_plan_exploit_parallel.py` | 3-stream PLAN-EXPLOIT: cancel fires once → all 3 streams catch CancelledError → single merged cancel_record |
| `tests/governance/test_cancel_parallel_dispatch_worktree.py` | L3 subagent: cancel propagates → worktree `--force` removed |
| `tests/governance/test_cancel_record_artifact_schema.py` | `cancel_records.jsonl` schema v1; LastSessionSummary digest reads it |
| `tests/governance/test_cancel_sse_event_emitted.py` | `cancel_origin_emitted` SSE event payload + IDE GET endpoint |
| `tests/governance/test_cancel_bounded_deadline_settle.py` | Cancel fires → all tasks settle within `JARVIS_CANCEL_BOUNDED_DEADLINE_S` (mock subprocess that ignores SIGTERM tests the kill escalation) |

### 7.2 Live-fire (after operator authorization, separately)

- One battle-test session that triggers REPL `/cancel --immediate <op-id>` mid-Venom-round; expects `cancel_records.jsonl` populated, no orphan subprocesses (`pgrep -f bash` clean), op terminal in POSTMORTEM.

### 7.3 Anti-regression pin

`grep -rn "asyncio.gather.*return_exceptions=False" backend/core/ouroboros/governance/` should match all existing call sites; W3(7) implementation must NOT introduce silent-cancel-loss patterns (e.g. `return_exceptions=True` swallowing CancelledError).

## 8. Flags / env

| Flag | Default | Purpose |
|---|---|---|
| `JARVIS_MID_OP_CANCEL_ENABLED` | `false` | Master switch — if false, REPL `/cancel` keeps existing phase-boundary behavior; watchdogs cannot trigger mid-phase. |
| `JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE` | `true` (when master is on) | Sub-flag — gates the `--immediate` REPL extension specifically. |
| `JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED` | `false` (when master is on) | Sub-flag — gates Class E (watchdog-initiated). Defaults off so first rollout is operator-only. |
| `JARVIS_CANCEL_BOUNDED_DEADLINE_S` | `30.0` | Time budget for all in-flight tasks to settle after a cancel trigger before fall-through to harness escape (item 3). |
| `JARVIS_CANCEL_SUBPROCESS_GRACE_S` | `5.0` | terminate→kill grace period for OS-level subprocesses. |
| `JARVIS_CANCEL_RECORD_PERSIST_ENABLED` | `true` (when master is on) | Sub-flag — gates `cancel_records.jsonl` write. Off = log-only. |

Master-off rollback: every new code path is gated; with master false, behavior is byte-for-byte identical to pre-W3(7).

## 9. Rollback strategy

### Per-slice rollback
Each implementation slice (when authorized) ships behind its own flag and is independently revertable. Anticipated slice breakdown:
1. `CancelToken` primitive + Class D wiring (`JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE`).
2. PhaseDispatcher + tool_loop propagation (no new flag — gated by master).
3. Class E (watchdog hooks) (`JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED`).
4. Class F (signal hooks).
5. PLAN-EXPLOIT + parallel_dispatch propagation.
6. SSE + IDE GET endpoint additive (`JARVIS_CANCEL_SSE_ENABLED`).
7. Graduation (master flag flip + 3-clean-session arc).

### Master-off invariant
Tests must include one source-grep regression: `JARVIS_MID_OP_CANCEL_ENABLED=false` → no `[CancelOrigin]` log emissions, no `cancel_records.jsonl` writes, no SSE events. The behavior must be bit-for-bit pre-W3(7) when master is off.

### Hot-revert path
If a graduated W3(7) misbehaves in production, single env var flip (`JARVIS_MID_OP_CANCEL_ENABLED=false`) restores pre-W3(7) behavior without a code revert. Same revert pattern as W3(6) Slice 4.

## 10. Out of scope (explicit)

- **Implementation.** No code, no slices, no tests in this pass. The slice breakdown in §9 is a *plan*, not a contract. Operator picks which slices land and in what order.
- **Auto-cancel policy** (when watchdog should fire). W3(7) adds the *plumbing* for watchdog-initiated cancel; the *trigger logic* (cost burn rate, divergence detection, productivity trip) belongs to the watchdog modules.
- **Cancel coordination across ops** (cancel parent → cancel all children). Only the targeted op's directly-owned sub-resources.
- **Persistent / cross-session cancel state.** Cancels are session-scoped.
- **Recovery from cancellation** (retry / resume / partial replay). POSTMORTEM is the terminal phase for cancelled ops.
- **SerpentREPL TUI affordances** beyond the `--immediate` flag. No new modal screens, no graphical cancel UI; cancel remains a CLI sub-command.
- **gRPC / external cancel API.** Cancel triggers are local: REPL, watchdog, signal. External clients (IDE) get *observability* via SSE/GET, not *trigger* authority.

## 11. Cross-links

- **Substrate**: `_attribute_cancel` helper landed in `1a495c5ee9` (commit message: "feat(candidate_generator): cancel-source attribution telemetry (S6 (A))"). Class A/B/C taxonomy is from there.
- **Cooperative cancel today**: CLAUDE.md "REPL /cancel: cancel <op-id> cooperative cancellation, checked at GENERATE and APPLY phase boundaries".
- **Manifesto**: §3 (structured concurrency — no event loop starvation), §6 (Iron Gate — every gate must hold; cancel is the meta-gate), §8 (absolute observability — every autonomous decision visible).
- **Harness epic item 3** (bounded post-summary shutdown): the same "30s deadline + os._exit fallback" idiom W3(7) uses for the cancel-bounded-deadline. Implementation should share the bounded-deadline primitive between cancel and shutdown rather than re-deriving.
- **Harness epic item 7** (`WallClockWatchdog` not firing): the wall watchdog is a Class E source; W3(7)'s implementation needs the watchdog plumbing fixed first OR can be done in parallel if the wall-cap arc lands its own fix.

## 12. Operator decision points

When you scope an implementation:

1. **Slice breakdown approval** — does §9 §1–7 match the right granularity? Consolidate or split?
2. **Default-off vs default-on for graduation** — Wave 3 (6) Slice 5 graduated default-off; same pattern here?
3. **Class E watchdog priority** — which watchdogs get mid-phase cancel first? (Cost likely highest value; wall second; productivity third.)
4. **Class F signal handling** — coordinate with harness epic item B (partial summary on interrupt) so the two don't double-emit cancel records?
5. **Live-fire timing** — after which slice does the first live battle session run? My read: after slice 5 (PLAN-EXPLOIT + parallel_dispatch propagation) so the cancel surface is exercised on real concurrency.

No work begins until you address one or more of these.

---

## Operator resolutions (2026-04-25)

Inline resolutions to §12, binding for implementation:

### Policy clarification — 2026-04-25 (post-graduation, on `d031802d9b`)

**Default state on main after Slice 7 graduation:**

| Env knob | Default on main (post-Slice-7) | Slice that set it |
|---|---|---|
| `JARVIS_MID_OP_CANCEL_ENABLED` | **`true`** (was `false` during Slices 1–6) | **Slice 7 graduation flip** |
| `JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE` | `true` (when master on) | Slice 1 design |
| `JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED` | `false` (operator opt-in) | Slice 3, per resolution-2 |
| `JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED` | `false` (operator opt-in) | Slice 4, per resolution-2 |
| `JARVIS_CANCEL_SSE_ENABLED` | `false` (operator opt-in) | Slice 6, per resolution-2 |
| `JARVIS_CANCEL_RECORD_PERSIST_ENABLED` | `true` (when master on) | Slice 1 design |

**Hot-revert remains `JARVIS_MID_OP_CANCEL_ENABLED=false`** — restores byte-for-byte pre-W3(7) by force-disabling every sub-flag regardless of their individual env values. Single env knob, no code revert.

### Reconciling the earlier "default OFF" wording

Resolution-2 originally said *"Default OFF for all new flags; preserve master-off = pre-W3(7) behavior."* This was the **build-time** invariant during Slices 1–6 — every new flag landed at default false so each merged slice was observably-no-op until graduation. Slice 7 IS the graduation flip; the master flag default changes by design at this slice. The *spirit* of resolution-2 is preserved at the **actuation layer**: every sub-flag that would cause an operator-visible cancellation (WATCHDOG, SIGNAL, SSE) stays default-false post-graduation. Operators must explicitly opt into Class E / F / SSE.

This is the standard graduation pattern from Wave 1 (DirectionInferrer + FlagRegistry + SensorGovernor — each flipped its own master flag at graduation), Wave 2 (5) (8 sub-slice flags flipped over the W2 (5) graduation matrix), and W3(6) (the structural wiring fix). Live-fire validation 2026-04-25 (session `bt-2026-04-25-024021`) confirmed the master-on default causes zero observable cancel actuation when sub-flags stay at their post-graduation defaults.

If a future change wants to flip a sub-flag default to true, that is a **separate authorization** — the same way Slice 7 was a separate operator authorization beyond the per-slice approvals.

---

> (1) Implement Slice 1 only first, thin slices, no big-bang.
>
> (2) Default OFF for all new flags; preserve master-off = pre-W3(7) behavior.
>
> (3) Class E must respect single terminal + precedence: operator/safety > idle watchdog; no double-terminal.
>
> (4) Class F coordinated with documented harness interactions, but no dependency on harness epic fixes for correctness.
>
> (5) No live-fire/battle until unit slice is green and I explicitly authorize live-fire.
>
> Authorization: W3(7) Slice 1 go — implement only what Slice 1 is in the scope doc + its tests; no Test A audit, no harness code, no F5, no W2(4), no new battle sessions in this PR.

### Slice 1 implementation notes (post-resolution, pre-merge)

- **Module landed**: `backend/core/ouroboros/governance/cancel_token.py` — `CancelToken` primitive, `CancelRecord` dataclass (schema `cancel.1`), `CancelTokenRegistry` (per-session op→token map), `CancelOriginEmitter` (Class D path).
- **Trigger surface landed**: `backend/core/ouroboros/battle_test/serpent_flow.py` `_handle_cancel(op_id, immediate=False)` extension. New CLI surface is `cancel <op-id> --immediate` (or `-i`); existing `cancel <op-id>` keeps phase-boundary semantics unchanged.
- **Master-off invariant honored** — when `JARVIS_MID_OP_CANCEL_ENABLED=false` (default), the new Class D emit path returns None without committing a record, writing a log line, or touching `cancel_records.jsonl`. Existing `request_cancel` code path is unchanged on both branches.
- **Slice 2 boundary** — Slice 1 is observability-only. The mid-phase propagation (PhaseDispatcher / ToolLoop / parallel-path consumption of the CancelToken) is Slice 2 and explicitly NOT in this PR. The REPL message acknowledges this: "Class D recorded; will take effect at next phase boundary (Slice 1 observability only)" when `--immediate` is used.
- **Tests**: `tests/governance/test_cancel_token_protocol.py` — 25 tests / 25 passing. Coverage:
  - flag defaults (master off, sub-flags force-off when master off, persist-off when master off, bounded-deadline parse incl. malformed env)
  - CancelToken sync surface (uncancelled init, idempotent set, ValueError on op_id mismatch)
  - CancelToken async surface (`wait()` blocks, returns immediately if pre-cancelled, `race()` coro-wins, `race()` cancel-wins)
  - CancelTokenRegistry (per-op uniqueness, prefix match, ambiguous→None, discard semantics)
  - CancelOriginEmitter Class D path (master-off no-op, master-on log+record, persist artifact write, idempotent supersede)

### Commit→slice mapping (this PR)

| Commit | Slice | What it ships |
|---|---|---|
| `<TBD>` | W3(7) Slice 1 | `cancel_token.py` (primitive, registry, Class D emitter) + `serpent_flow.py` REPL `--immediate` extension + 25 protocol tests |

(Single-commit PR; no rebase / squash needed.)

### Out of scope for this PR (per resolution-5)

- No live-fire / battle session.
- No PhaseDispatcher / ToolLoop / parallel_dispatch / RepairEngine integration (Slice 2+).
- No watchdog hooks (Slice 3).
- No signal handler hooks (Slice 4).
- No SSE / IDE GET (Slice 6).
- No graduation / master flag flip (Slice 7).
- No Test A audit, no harness code, no F5, no W2(4), no new battle sessions (operator-binding).

### Slice 1 merge (2026-04-25)

PR #19014 merged to `main` at commit **`165639c6cb`** (admin-merged after CI: all Python test suites SUCCESS; failures isolated to pre-existing infra-validation jobs unrelated to the code change).

---

## Slice 2 implementation notes (2026-04-25)

Operator authorization (verbatim):

> W3(7) Slice 2 go — implement only what the scope doc defines for PhaseDispatcher + tool_loop propagation (per your §9/§5 paragraph): cancel_token through Runner.run(), cancel_token.race(...) at long-running asyncio.wait_for(provider.generate(...), timeout=...) sites, subprocess tools get terminate → 5s grace → kill on token-set, PhaseDispatcher terminal cancelled → next_phase=POSTMORTEM, pctx.extras["cancel_record"] single-writer discipline. No new master flag; JARVIS_MID_OP_CANCEL_ENABLED stays default false; master-off = no behavior change. d378dea968 wiring must remain intact.

### Threading approach — `contextvars.ContextVar` (deviation from §9 wording)

The §9 paragraph says "threads a `cancel_token` parameter into every `<Phase>Runner.run()`". Rather than break 9 Runner signatures + every call-site of `candidate_generator.generate(...)` + ToolExecutor methods, Slice 2 uses `contextvars.ContextVar("ouroboros.cancel_token", default=None)` set by `dispatch_pipeline` at the iteration boundary. The token is reachable via `cancel_token_var.get()` (or `current_cancel_token()`) throughout the asyncio task chain — `asyncio.create_task(coro)` copies the parent's context to the child, so the token survives nested `gather()` / `wait_for()` boundaries naturally.

Net effect: same observable behavior the §9 paragraph specifies, with zero signature breakage. `pctx.cancel_token` slot still exists (added per §9) and the dispatcher still owns the assignment; the ContextVar is the *transport*, not a parallel ownership channel.

### What Slice 2 ships

| Module | Change |
|---|---|
| `cancel_token.py` | + `cancel_token_var: ContextVar`, `current_cancel_token()`, `OperationCancelledError`, `race_or_wait_for(coro, timeout, cancel_token)` helper, `subprocess_grace_s()` env reader |
| `phase_dispatcher.py` | + `PhaseContext.cancel_token` slot; pre-iteration cancel-check that routes to POSTMORTEM with `pctx.extras["cancel_record"]` populated; sets `cancel_token_var` from `pctx.cancel_token` after registry lookup |
| `governed_loop_service.py` | + `self._cancel_token_registry` (CancelTokenRegistry) attached in `__init__` next to `_cancel_requested` set |
| `orchestrator.py` | + `_cancel_token_registry` property aliasing through `self._stack.governed_loop_service._cancel_token_registry` (mirrors `_subagent_scheduler` alias pattern from W3(6) wiring fix) |
| `candidate_generator.py` | `_call_primary` and `_call_fallback` provider.generate awaits switched from plain `asyncio.wait_for` to `race_or_wait_for(..., cancel_token=current_cancel_token())` |
| `tool_executor.py` | `_run_tests_async` (the only `wait_for(proc.communicate())` site) gets `race_or_wait_for` + `OperationCancelledError` handler that performs `proc.terminate() → JARVIS_CANCEL_SUBPROCESS_GRACE_S grace → proc.kill()` chain. (Note: `_bash` uses sync `subprocess.run` — non-cancellable mid-call without async rewrite; documented out-of-scope.) |

### Master-off invariant honored (verified)

- `cancel_token_var` defaults to None.
- `current_cancel_token()` returns None outside any binding.
- `race_or_wait_for(coro, timeout, cancel_token=None)` falls through to plain `asyncio.wait_for(coro, timeout=timeout)` — bit-for-bit pre-W3(7).
- Slice 1's `emit_class_d` returns None when master-off, never calling `token.set(...)`. `is_cancelled` stays False on the token, race always returns the coro result.

Test pin: `test_master_off_emit_no_op_does_not_set_token` in `test_cancel_propagation_slice2.py`.

### `d378dea968` (W3(6) wiring) preserved

Verified: `_subagent_scheduler` property on `GovernedOrchestrator` is untouched. The new `_cancel_token_registry` property follows the same idiom (forward-through to `self._stack.governed_loop_service`).

### Tests

`tests/governance/test_cancel_propagation_slice2.py` — 14/14 passing locally. Coverage:

- (D) ContextVar — default None, propagates to `create_task` (1 test)
- env — subprocess_grace_s default 5.0 (1)
- (B) `race_or_wait_for` — token-None fall-through, coro wins, timeout wins, cancel wins, pre-cancelled short-circuit doesn't start coro (5)
- (A) PhaseContext slot + extras["cancel_record"] structure (2)
- (A) Integration — dispatcher routes to POSTMORTEM on cancel, dispatcher invokes runners normally without cancel (2)
- (E) GLS attaches CancelTokenRegistry (1)
- (F) Master-off invariant — emit_class_d no-op, token never set, race falls through (1)

Plus regression: 28/28 prior tests (Slice 1 + W3(6) wiring + adjacent W3(6) suites) re-verified green.

### Slice 2 commit-mapping (this PR)

| Commit | Slice | What it ships |
|---|---|---|
| `<TBD>` | W3(7) Slice 2 | `cancel_token.py` ContextVar+helpers, `phase_dispatcher.py` cancel-check + token wiring, `governed_loop_service.py` registry attach, `orchestrator.py` registry property, `candidate_generator.py` race-wrap on 2 provider.generate sites, `tool_executor.py` `_run_tests_async` terminate→grace→kill, 14 propagation tests |

(Single-commit PR, branched off main at `165639c6cb`.)

### NOT in this PR (Slice 3+ remain queued)

- Class E watchdog hooks (Slice 3).
- Class F signal hooks (Slice 4).
- PLAN-EXPLOIT + parallel_dispatch propagation (Slice 5).
- SSE / IDE GET endpoint (Slice 6).
- Graduation / master flag flip (Slice 7).
- `_bash` subprocess.run conversion to async (out of scope per "thin slice"; needs operator scoping if/when wanted).
- Live-fire / battle session.
- Test A audit, harness code, F5, W2(4), new battle sessions (operator standing orders).

### Slice 2 merge (2026-04-25)

PR #19043 merged to `main` at commit **`29a28e2065`**.

---

## Slice 3 implementation notes (2026-04-25)

Operator authorization (verbatim): `W3(7) Slice 3 go`.

### What Slice 3 ships

| Module | Change |
|---|---|
| `cancel_token.py` | + `watchdog_enabled()` env reader (default false even when master is on, per resolution-2); + `CancelOriginEmitter._ALLOWED_WATCHDOGS` frozenset (`cost`, `wall`, `productivity`, `idle`); + `CancelOriginEmitter.emit_class_e(watchdog, ...)` parallel to `emit_class_d`; + `emit_watchdog_cancel(...)` convenience helper; + ValueError typo guard on unknown watchdog name |
| `cost_governor.py` | + `CostGovernor.attach_cancel_surface(registry, session_dir)`; + `_emit_class_e_cancel(...)` private; + hook call inside `charge()` cap-exceeded branch (best-effort, never raises into accounting path) |
| `governed_loop_service.py` | After orchestrator construction, calls `cost_governor.attach_cancel_surface(...)` so the cost watchdog has the registry |
| Tests | `test_cancel_class_e_watchdog_slice3.py` — 20/20 passing |

### Master-off / sub-flag-off invariants (verified)

- Master off → `emit_class_e` returns None; existing `entry.exceeded=True` flag still flips. Pinned by `test_cost_governor_cap_exceeded_no_op_when_master_off`.
- Sub-flag off (default even when master on) → `emit_class_e` returns None. Pinned by `test_emit_class_e_returns_none_when_subflag_off` + `test_watchdog_flag_default_off_even_when_master_on`.
- Missing registry attach → silent no-op even with both flags on. Pinned by `test_cost_governor_cap_exceeded_no_op_when_no_registry_attached`.

### Resolution-3 (precedence) honored

Idempotent `token.set()` enforces single-terminal. When a Class E race loses to a prior cancel, `[CancelOrigin] superseded` log carries `requested_origin` + `winner_origin` for postmortem audit. Pinned by `test_emit_class_e_supersede_log_when_token_already_cancelled` (Class D pre-set, Class E:idle loses, supersede log emitted with both origins).

Per-watchdog trigger timing logic stays in each watchdog module (operator's "trigger logic belongs to the watchdog modules" doctrine). Slice 3 ships only the *emitter + reference cost-watchdog hook*.

### Hooks shipped vs scoped-but-unwired

| Watchdog | Wired in this PR? | Notes |
|---|---|---|
| `cost` | ✅ at `cost_governor.charge()` cap-exceeded branch | Reference integration; uses GLS-attached registry |
| `wall` | ❌ NOT wired | Harness epic item 7 (wall_clock_cap not firing) is its own ticket; helper available |
| `productivity` | ❌ NOT wired | Helper available |
| `idle` | ❌ NOT wired | Helper available |

Three of four hooks are deferred per resolution-1 ("thin slice"). One-line additions with the helper in place; operator-authorized follow-up can wire them.

### Slice 3 commit-mapping

| Commit | Slice | Files |
|---|---|---|
| `<TBD>` | W3(7) Slice 3 | `cancel_token.py` Class E emitter + flag + helper, `cost_governor.py` attach + hook, `governed_loop_service.py` surface attach, `test_cancel_class_e_watchdog_slice3.py` (20 tests) |

### NOT in this PR (Slice 4+ remain queued)

- Slice 4: Class F signal hooks (SIGTERM / SIGINT / SIGHUP).
- Slice 5: PLAN-EXPLOIT + parallel_dispatch propagation.
- Slice 6: SSE event + IDE GET endpoint.
- Slice 7: graduation / master flag flip.
- Wall / productivity / idle watchdog hook wiring (deferred).
- `_bash` async conversion.
- Live-fire / battle session.
- Test A audit, harness code, F5, W2(4), new battle sessions (operator standing orders).

### Slice 3 merge (2026-04-25)

PR #19079 merged to `main` at commit **`1c87b10289`**.

---

## Slice 4 implementation notes (2026-04-25)

Operator authorization (verbatim): `W3(7) Slice 4 go`.

### What Slice 4 ships

| Module | Change |
|---|---|
| `cancel_token.py` | + `signal_enabled()` env reader (default false even when master is on, per resolution-2); + `CancelOriginEmitter._ALLOWED_SIGNALS` frozenset (`sigterm`, `sigint`, `sighup`); + `CancelOriginEmitter.emit_class_f(signal_name, ...)` parallel to Class D/E; + `emit_signal_cancel(...)` fan-out helper that emits one Class F record per active op in the registry; + ValueError typo guard on unknown signal name (rejects uppercase like `SIGTERM` since canonical lowercase mirrors harness ticket B convention) |
| `harness.py` | `_handle_shutdown_signal` gains an additive Class F emission step AFTER the existing partial-summary write. Master-off / sub-flag-off → `emit_signal_cancel` returns 0 (silent no-op). Wrapped in try/except for interrupt-safety — the existing summary-write path remains the source of truth even if Class F emission fails |
| Tests | `test_cancel_class_f_signal_slice4.py` — 18/18 passing |

### Resolution-4 honored — no harness dependency for correctness

The existing `_handle_shutdown_signal` partial-summary write path is **untouched** in this PR. Class F emission is an additive observability step:

1. (existing) `self._stop_reason = signal_name` → unchanged.
2. (existing) `self._atexit_fallback_write(session_outcome="incomplete_kill")` → unchanged.
3. (NEW) `emit_signal_cancel(signal_name=..., registry=..., session_dir=...)` → gated by master + signal sub-flag; returns 0 when off; never raises.
4. (existing) `self._shutdown_event.set()` → unchanged.

If the harness epic items (#3 bounded post-summary shutdown, #6 SIGTERM partial-summary insurance) still have bugs, the Class F path **does not depend** on them. Master-off invariant ensures Class F is unobservable until operator opt-in.

### Master-off + sub-flag-off invariants (verified)

- `JARVIS_MID_OP_CANCEL_ENABLED=false` (default) → `emit_class_f` returns None, `emit_signal_cancel` returns 0. Existing partial-summary write path is fully preserved. Pinned by `test_emit_class_f_returns_none_when_master_off` + `test_emit_signal_cancel_returns_zero_when_master_off`.
- Master on + sub-flag off (default) → both return None/0. Pinned by `test_emit_class_f_returns_none_when_subflag_off` + `test_signal_flag_default_off_even_when_master_on` + `test_emit_signal_cancel_returns_zero_when_subflag_off`.
- Empty registry / no active ops → `emit_signal_cancel` returns 0. Pinned by `test_emit_signal_cancel_returns_zero_when_no_active_ops`.

### Resolution-3 (precedence) extended for F

- Slice 1's idempotent `token.set()` enforces single-terminal across all four classes (D/E/F).
- Class F losing to a prior Class D logs `[CancelOrigin] superseded` with both origins. Pinned by `test_emit_class_f_supersede_log_when_token_already_cancelled` (Class D pre-set, Class F:sigterm loses).
- `emit_signal_cancel` over a registry where some ops are pre-cancelled (Class D) handles the supersede gracefully — those ops are NOT counted in the emitted total. Pinned by `test_emit_signal_cancel_skips_already_cancelled_ops` (1 D-cancelled + 2 fresh → emit returns 2, D record preserved).

### Interrupt-safety

The signal handler is process-interrupt context — Python exceptions there are catastrophic. Test pin `test_emit_signal_cancel_never_raises_on_registry_failure` exercises a broken registry that raises on `active_op_ids()` — `emit_signal_cancel` returns 0 cleanly.

### Slice 4 commit-mapping

| Commit | Slice | Files |
|---|---|---|
| `<TBD>` | W3(7) Slice 4 | `cancel_token.py` Class F emitter + flag + helper + 18 tests, `harness.py` additive Class F emission in `_handle_shutdown_signal` |

### NOT in this PR (Slice 5+ remain queued)

- Slice 5: PLAN-EXPLOIT + parallel_dispatch propagation.
- Slice 6: SSE event + IDE GET endpoint.
- Slice 7: graduation / master flag flip.
- Wall / productivity / idle watchdog hook wiring (Slice 3 deferral).
- `_bash` async conversion (Slice 2 deferral).
- Live-fire / battle session.
- Test A audit, harness code (beyond this Slice 4 additive emission), F5, W2(4), new battle sessions (operator standing orders).

### Slice 4 merge (2026-04-25)

PR #19100 merged to `main` at commit **`9ec8ce7f62`**.

---

## Slice 5 implementation notes (2026-04-25)

Operator authorization (verbatim): `W3(7) Slice 5 go`.

### What Slice 5 ships

| Module | Change |
|---|---|
| `plan_exploit.py` | Replace `asyncio.wait_for(asyncio.gather(...))` at the parallel-stream gather with `race_or_wait_for(..., cancel_token=current_cancel_token())`. On `OperationCancelledError`: emit `[PLAN-EXPLOIT] op=... status=cancelled dag_units=N wall_ms=... cancel_origin=... cancel_id=...` log, return None (merged-files synthesis abandoned per scope doc §5.3 — "no partial state persists"). The gather's `race_or_wait_for` cleanup already cancels the child stream tasks. |
| `parallel_dispatch.py` | In `enforce_evaluate_fanout`, wrap `scheduler.wait_for_graph(...)` with `race_or_wait_for(..., cancel_token=current_cancel_token())`. On `OperationCancelledError`: emit `[ParallelDispatch enforce_cancelled]` log carrying origin + cancel_id, attempt best-effort `scheduler.cancel_graph(graph.graph_id)` so worktrees can be reaped, and return `FanoutResult(outcome=FanoutOutcome.CANCELLED, ...)` with the cancel context in `error=`. Existing `asyncio.CancelledError` branch unchanged (cooperative-cancel path still propagates). |
| Tests | `test_cancel_class_5_parallel_paths_slice5.py` — 9/9 passing |

### Master-off invariants (verified)

- Both Slice 5 sites use `current_cancel_token()` from the ContextVar; when master is off OR no token bound, `race_or_wait_for` falls through to plain `asyncio.wait_for`. PLAN-EXPLOIT and parallel_dispatch behave byte-for-byte as pre-W3(7). Pinned by `test_master_off_plan_exploit_passes_through_to_wait_for` and `test_plan_exploit_no_cancel_returns_gather_result`.

### What's NOT in Slice 5 (deferred per "thin slice")

- **L3 worktree subagent cancel-token injection** (scope doc §5.4 second paragraph). Each subagent receiving a `cancel_token` via `WorkUnitContext` and checking it at I/O boundaries is a substantial subagent-side change. Best-effort `scheduler.cancel_graph` is the Slice 5 surface; per-subagent token plumbing is a follow-up if/when operator wants tighter mid-unit cancellation.
- **Per-stream partial-record contributions to the cancel artifact** (scope doc §5.3 second sentence). Slice 5 abandons the merge entirely on cancel; per-child partial-stream records would need bookkeeping inside `_generate_unit` and a new partial-record schema. The scope doc explicitly leaves this as future work; the abandoned-merge contract is the binding deliverable.

### Slice 5 commit-mapping

| Commit | Slice | Files |
|---|---|---|
| `<TBD>` | W3(7) Slice 5 | `plan_exploit.py` cancel-aware gather, `parallel_dispatch.py` cancel-aware wait_for_graph + best-effort cancel_graph, 9 tests |

### NOT in this PR (Slice 6+ remain queued)

- Slice 6: SSE event + IDE GET endpoint.
- Slice 7: graduation / master flag flip.
- L3 worktree subagent cancel-token injection (Slice 5 deferral).
- Per-stream partial-record contributions (Slice 5 deferral).
- Wall / productivity / idle watchdog hook wiring (Slice 3 deferral).
- `_bash` async conversion (Slice 2 deferral).
- Live-fire / battle session.
- Test A audit, harness code (beyond Slice 4 additive emission), F5, W2(4), new battle sessions (operator standing orders).

### Slice 5 merge (2026-04-25)

PR #19106 merged to `main` at commit **`8ec5576ca2`**.

---

## Slice 6 implementation notes (2026-04-25)

Operator authorization (verbatim): `W3(7) Slice 6 go`.

### What Slice 6 ships

| Module | Change |
|---|---|
| `cancel_token.py` | + `sse_enabled()` env reader (`JARVIS_CANCEL_SSE_ENABLED`, default false); + `bridge_cancel_origin_to_sse(record)` best-effort publish helper that calls `IDEStreamRouter`'s broker; + post-commit hook in `emit_class_d` / `emit_class_e` / `emit_class_f` (3 sites) |
| `ide_observability_stream.py` | + `EVENT_TYPE_CANCEL_ORIGIN_EMITTED = "cancel_origin_emitted"` constant + entry in `_VALID_EVENT_TYPES` (10 → 11 event vocab, additive) |
| `ide_observability.py` | + `IDEObservabilityRouter.__init__(session_dir=None)` optional kwarg; + `GET /observability/cancels` (list with origin/op_id filter, 1..1000 limit); + `GET /observability/cancels/{cancel_id}` (detail); + `_read_cancel_records()` helper reading from `cancel_records.jsonl` |
| Tests | `test_cancel_sse_ide_get_slice6.py` — 15/15 passing |

### Master-off + sub-flag-off invariants (verified)

- `JARVIS_CANCEL_SSE_ENABLED=false` (default) → `bridge_cancel_origin_to_sse` returns silently; existing cancel record persistence + `[CancelOrigin]` log lines unchanged. Pinned by `test_bridge_no_op_when_sse_flag_off` + `test_emit_class_d_no_sse_publish_when_sse_flag_off`.
- `JARVIS_IDE_OBSERVABILITY_ENABLED=false` (default) → cancel routes return 403; same as every other observability route. Inherits the existing IDE observability authority posture.
- `IDEObservabilityRouter()` constructed without `session_dir` → cancel routes return 503 cleanly (no raises). Pinned by `test_router_returns_503_when_no_session_dir`.

### Authority invariants preserved

- New routes are **read-only** GETs (no mutation paths). Inherits the existing IDE observability authority lock (no cancel/approve/merge/invoke/retry).
- New SSE event is additive — preserves the "additive-only" contract for the event vocabulary (was 10 events from prior slices, now 11).
- `bridge_cancel_origin_to_sse` is a best-effort publisher: never raises, swallows broker construction failures, swallows publish failures. The cancel record persistence + log lines are NOT gated by SSE — they always land regardless of consumer state.

### End-to-end verified

`test_emit_class_d_publishes_sse_event_when_flags_on` exercises the full chain: master+SSE+stream-master flags on → `emit_class_d` commits record → bridge publishes event → broker history contains a `cancel_origin_emitted` event with the correct `op_id` + `cancel_id` + `origin` + `phase` payload.

### Slice 6 commit-mapping

| Commit | Slice | Files |
|---|---|---|
| `<TBD>` | W3(7) Slice 6 | `cancel_token.py` SSE flag + bridge + 3 emit hooks, `ide_observability_stream.py` event type, `ide_observability.py` 2 GET routes + reader helper, 15 tests |

### NOT in this PR (Slice 7 remains queued)

- Slice 7: graduation / master flag flip — the final slice. Soak under operator-authorized live-fire + 3-clean-session arc + flip default.
- L3 subagent token injection / per-stream partial records (Slice 5 deferrals).
- Wall / productivity / idle watchdog wiring (Slice 3 deferral).
- `_bash` async conversion (Slice 2 deferral).
- Live-fire / battle session, Test A, harness code (beyond Slice 4 additive emission), F5, W2(4), new battle sessions (operator standing orders).

### Slice 6 merge (2026-04-25)

PR #19119 merged to `main` at commit **`c48882683d`**.

---

## Slice 7 implementation notes (2026-04-25) — GRADUATION

Operator authorization (verbatim): `start on the slice 7 which is the final slice (Graduation / master flag flip)`.

### What Slice 7 ships

| Module | Change |
|---|---|
| `cancel_token.py` | `mid_op_cancel_enabled()` default flipped from `False` → `True`. Docstring rewritten to document the post-graduation contract (which sub-flags stay off, hot-revert path). |
| `tests/governance/test_w3_7_graduation_pins_slice7.py` | New 21-test pin file covering: master default-true, all actuating sub-flags still default-off, REPL_IMMEDIATE / RECORD_PERSIST default-true (Slice 1 design), hot-revert (master=false force-disables every sub-flag), authority invariants (SSE vocab size, schema cancel.1), source-grep pins for every Slice 1–6 surface. |
| Existing test files | Bulk-updated: 13 occurrences across 5 files of `monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)` swapped to `monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "false")` so master-off invariant tests still test the explicit-false path post-flip. One test renamed (`test_master_flag_default_off` → `test_master_flag_explicit_false`) with corrected docstring. |

### Why the master-flip is safe (zero observable behavior change at actuation layer)

Standard graduation pattern from Wave 1 / Wave 2 / W3(6) Slice 5b:

| Sub-flag | Default when master on | What it gates | Effect of master-flip alone |
|---|---|---|---|
| `JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE` | True | REPL `cancel <op-id> --immediate` (Class D) | Available, but only fires on explicit operator action |
| `JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED` | False | Class E (cost / wall / productivity / idle) | OFF — operator opts in |
| `JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED` | False | Class F (system signals) | OFF — operator opts in |
| `JARVIS_CANCEL_SSE_ENABLED` | False | SSE `cancel_origin_emitted` publish | OFF — operator opts in |
| `JARVIS_CANCEL_RECORD_PERSIST_ENABLED` | True | `cancel_records.jsonl` writes | True — but writes only happen when emit fires, which requires the above |

Net effect of flipping master alone:
- ContextVar token plumbing runs on every dispatched op → observably-no-op (no `set()` ever called)
- `race_or_wait_for` falls through to plain `asyncio.wait_for` (token never cancelled)
- PhaseDispatcher cancel-check evaluates `is_cancelled` → always False → loop unchanged
- candidate_generator + tool_executor + plan_exploit + parallel_dispatch race wraps → all fall through

**Master-flip alone causes ZERO observable behavior change at the cancel-actuation layer.** Pinned by 4 graduation tests (`test_watchdog_subflag_default_off_even_post_graduation`, `test_signal_subflag_default_off_even_post_graduation`, `test_sse_subflag_default_off_even_post_graduation`, plus the master-on default-true pin).

### Hot-revert path

Single env var: `JARVIS_MID_OP_CANCEL_ENABLED=false` → `mid_op_cancel_enabled()` returns False → every sub-flag is force-disabled regardless of its individual env value (per the gate composition in Slice 1) → ContextVar plumbing runs but `race_or_wait_for` falls through → byte-for-byte pre-W3(7).

Pinned by `test_hot_revert_master_off_disables_all_subflags` (sets master=false + all sub-flags=true; asserts every sub-flag returns False) and `test_hot_revert_master_off_emit_class_d_returns_none`.

### Live-fire status (separate from this PR)

Per operator resolution-5 ("No live-fire/battle until unit slice is green and I explicitly authorize live-fire"), the actual 3-clean-session live-fire arc is gated on operator authorization. This PR ships:
- Code flip (master default true)
- 21 graduation pin tests covering every invariant
- Bulk-update of 13 master-off test sites to use explicit setenv
- Documentation of hot-revert contract

What it does NOT do:
- Run any live battle-test session (operator standing order)
- Soak the graduated default in a real session

The graduation-via-flag-flip pattern matches Wave 1 / Wave 2 / W3(6) Slice 5b — those graduations also flipped the flag and pinned the contract via tests; live-fire was operator-decided afterward and the master-off hot-revert was always the safety net.

### Slice 7 commit-mapping

| Commit | Slice | Files |
|---|---|---|
| `<TBD>` | W3(7) Slice 7 | `cancel_token.py` master flip + docstring rewrite, 21 graduation pin tests, 5 test files bulk-updated to explicit-setenv (13 occurrences) |

### Combined arc totals (Slices 1–7)

- Master flag and sub-flag composition: 6 env knobs total, fully documented + grep-pinned.
- Cancel classes: 4 origin classes (D / E / F + the existing A / B / C telemetry taxonomy).
- Cancel record schema: `cancel.1` frozen dataclass.
- SSE event vocabulary: 40 events total (1 added by Slice 6, additive-only contract).
- IDE GET routes: 2 added (`/observability/cancels` + `/observability/cancels/{id}`).
- Tests: 160/160 across all 7 slice test files + parallel_dispatch + W3(6) wiring suites.
- Hot-revert: single env var (`JARVIS_MID_OP_CANCEL_ENABLED=false`).

### Wave 3 completion

Slice 7 closes Wave 3 (7), which in turn closes Wave 3 entirely (Wave 3 (6) was already CLOSED at the structural bar 2026-04-24).

### NOT in this PR (operator-authorized follow-ups)

- Live-fire / battle session validation of the graduated default (operator-binding standing order).
- L3 subagent token injection / per-stream partial records (Slice 5 deferrals).
- Wall / productivity / idle watchdog hook wiring (Slice 3 deferral — three watchdogs available via `emit_watchdog_cancel(...)` helper).
- `_bash` async conversion (Slice 2 deferral).
- Test A audit, harness code beyond Slice 4 additive emission, F5, W2(4), new battle sessions.
