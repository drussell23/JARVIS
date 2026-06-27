---
title: Project Harness Epic Scope
modules: [scripts/ouroboros_battle_test.py, backend/core/ouroboros/battle_test/shutdown_watchdog.py, backend/core/ouroboros/governance/intake/intake_layer_service.py, backend/core/ouroboros/battle_test/harness.py]
status: merged
source: project_harness_epic_scope.md
---

## Status

- **Operator-authorized 2026-04-25** post-Wave-3 closure. Quote: "let's start with the Harness epic (trust/cost) and let's make sure we resolve this rooted problem and super beef it up!"
- **First in the deferred-item queue.** After harness closes, the slate is: seed exploration offline arc, W2(4) curiosity scoping, F5, W3(7) deferrals (L3 token / PLAN-EXPLOIT partials / wall-productivity-idle watchdog wiring / bash async).
- **Standing orders honored**: thin slices, default-off where applicable, hot-revert paths, no live-fire/battle until per-slice operator authorization.

## The rooted problem (one paragraph)

`scripts/ouroboros_battle_test.py` can hang indefinitely after writing `summary.json` (or fail to write it at all). Holds `.jarvis/intake_router.lock`, blocks subsequent sessions with `RouterAlreadyRunningError`. Stack signature is identical across **14 documented incidents** (per `project_followup_battle_test_post_summary_hang.md`): main thread wedged on `Py_FinalizeEx → PyThread_acquire_lock_timed → __psynch_cvwait`, classic Python interpreter-shutdown deadlock on a non-daemon thread join. Concurrent symptom (S5/S6, 2026-04-24): SIGTERM during steady-state doesn't trigger the partial-summary write path, leaving session dirs with only `debug.log` and no `summary.json`. Concurrent symptom (S6): `WallClockWatchdog` doesn't fire at `max_wall_seconds=2400` (session ran 51min without termination). All three are shutdown-discipline failures; all three need a **single sync-thread-based deadline escape hatch from asyncio-land**.

## Goals (in priority order)

1. **Every session terminates within a bounded deadline.** No process outlives `max(max_wall_seconds + bounded_shutdown_deadline_s, idle_timeout + bounded_shutdown_deadline_s)`. Hard guarantee, not best-effort.
2. **Every session writes a v1.1b-parseable `summary.json`** to its session dir before terminating, regardless of how it terminates (clean / SIGTERM / SIGINT / SIGHUP / wall cap / idle / cost cap).
3. **No session contaminates the next.** `intake_router.lock` is released cleanly on every exit path. Single-flight launcher rejects concurrent runs at the process level.
4. **All process-tree hygiene is auditable.** `pgrep` probe is canonical; `tail -f /dev/null` stdin guards are banned.

## Non-goals

- Reducing actual battle-test cost (provider spend, compute time) — that's a separate cost-optimization arc, NOT this epic.
- Making `SIGKILL` recoverable (it's uncatchable per OS / Python contract — out of scope).
- Replacing the existing `WallClockWatchdog` asyncio task entirely — Slice 1 ADDS a thread-based escape; the asyncio task remains the primary path.
- Auto-recovery of in-flight ops on shutdown (cancellation already covered by W3(7)).

## In-scope items (from the existing harness epic + S5/S6 additions)

| # | Item | Source | Slice |
|---|---|---|---|
| 1 | Ban `tail -f /dev/null \| python` stdin guard in runbooks | original epic | Slice 3 |
| 2 | `intake_router.lock` lifecycle hardening (PID + timestamp + stale TTL) | original epic | Slice 2 |
| 3 | Bounded post-`summary.json` shutdown via thread + `os._exit` fallback | original epic | **Slice 1** |
| 4 | `pgrep` hygiene canonical probe documented in runbook | original epic | Slice 3 |
| 5 | Single-flight launcher enforcement (preflight pgrep + lock) | original epic | Slice 2 |
| 6 | SIGTERM-during-steady-state partial-summary write | S5/S6 incident | **Slice 1** |
| 7 | `WallClockWatchdog` not firing (asyncio task starvation hypothesis) | S6 incident | **Slice 1** |

Slice 1 covers items 3 + 6 + 7 because all three want the same primitive: a sync thread that holds an absolute deadline and calls `os._exit(75)` if the asyncio path doesn't terminate cleanly within the budget. They co-ship naturally per the existing memory cross-link.

## 4-slice plan

### Slice 1 — Bounded shutdown watchdog (the rooted fix)

**Module**: `backend/core/ouroboros/battle_test/shutdown_watchdog.py` (new).

**Primitive**: `BoundedShutdownWatchdog` class.
- `arm(reason: str, deadline_s: float)` — start the deadline (called when shutdown is requested).
- `disarm()` — cancel the deadline (called when clean shutdown completes within budget).
- `_thread_loop()` — daemon thread that waits for arm() event, then sleeps deadline_s, then calls `os._exit(EXIT_CODE_HARNESS_WEDGED=75)` and writes a forensic line to stderr.
- Uses `threading.Event` for the arm signal — sync-only, no asyncio dependency.

**Wire-in points** in `harness.py`:
- Construct watchdog in `__init__`. Daemon thread starts immediately (idle until arm()).
- `register_signal_handlers._handle_shutdown_signal` — call `watchdog.arm(signal_name, 30s)` BEFORE the existing summary-write path. If the asyncio path finishes within 30s, `disarm()` is called from the normal shutdown completion site. If not, `os._exit` fires.
- `WallClockWatchdog._wall_clock_alarm` — call `watchdog.arm("wall_clock_cap", 30s)` IN ADDITION to setting the asyncio event. Same disarm path.
- Clean shutdown completion (end of `_run_until_terminal` / `_generate_report`) — call `watchdog.disarm()`.

**Why this fixes items 3 + 6 + 7**:
- Item 3 (bounded post-summary): `os._exit` after deadline guarantees no Py_FinalizeEx zombie.
- Item 6 (SIGTERM partial summary): the thread fires `os._exit` after deadline, so the synchronous partial-summary write path that runs BEFORE arming has a guaranteed-bounded execution window.
- Item 7 (WallClockWatchdog asyncio starvation): the thread-based deadline runs independently of the asyncio loop, so even if the loop is wedged, `os._exit` fires.

**Master flag**: `JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED` (default `true` post-graduation). Hot-revert: `=false` reverts to pre-Slice-1 behavior (asyncio-only shutdown). Note: defaulting `true` is safe because the watchdog is `disarm()`-able; clean shutdowns don't trigger `os._exit`.

**Tests**: ~15 unit tests for the primitive (arm/disarm, event-driven, deadline elapse, `os._exit` path verified via mocked `_exit_fn`).

### Slice 2 — Lock lifecycle + single-flight launcher

**Module**: `backend/core/ouroboros/governance/intake/intake_layer_service.py` (existing).

**Changes**:
- `intake_router.lock` content schema: `{"pid": int, "monotonic_ts": float, "wall_iso": str, "session_id": str}`. Currently it's `{"pid": int, "ts": float}`. Additive — Slice 2 reads either shape, writes new shape.
- New stale-TTL: `JARVIS_INTAKE_LOCK_STALE_TTL_S` (default `max(2 * max_wall_seconds, 7200s)`). If lock's `monotonic_ts` is older than TTL, treat as stale even if PID is alive (handles wedged-but-alive zombies before zombie-reaper runs).
- `IntakeLayerService.start()` lock acquire wraps in try/finally; `stop()` unlinks the lock unconditionally.

**Module**: `scripts/ouroboros_battle_test.py` — single-flight preflight.
- Before the asyncio loop starts: `pgrep -f "python3? scripts/ouroboros_battle_test\.py"` must return ≤1 (this process).
- Lock file exists AND PID is alive AND `monotonic_ts` < TTL → exit with code 75 (`EX_TEMPFAIL`), printing the violator info.
- Lock file exists AND (PID dead OR ts > TTL) → log "stale lock adopted" and proceed with cleanup.

**Tests**: ~12 unit tests covering lock content roundtrip, TTL math, single-flight predicate.

### Slice 3 — Process hygiene + runbook

**Changes**:
- `docs/runbooks/battle_test.md` (or equivalent) — update with canonical `pgrep` probe (item 4) and explicit ban on `tail -f /dev/null | python` (item 1).
- CI grep guard: `git grep -E "tail -f /dev/null \\| python" docs/ scripts/` must return empty.
- `pgrep` canonical: `pgrep -f "python3? scripts/ouroboros_battle_test\\.py"` — avoids matching zsh wrappers' eval text.

**Tests**: ~5 source-grep tests pinning the runbook + the CI guard predicate.

### Slice 4 — Graduation

- Flip `JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED` default `true` (if not already).
- Comprehensive graduation pin tests (per W3(7) pattern).
- Hot-revert documentation.

## What ships at end of arc

- `BoundedShutdownWatchdog` primitive with `os._exit` fallback (Slice 1).
- 14-incident class structurally cured (Slice 1 + 2).
- Lock lifecycle + single-flight (Slice 2).
- Runbook + CI guards (Slice 3).
- Graduated default-on (Slice 4).
- ~50 unit tests across slices.
- Hot-revert: single env var per slice.

## Operator decision points

1. Approve 4-slice breakdown? (Or different granularity?)
2. Slice 1 default-on vs default-off at landing? My read: default-on, since `disarm()` makes clean shutdowns cost-free.
3. Live-fire validation timing: after Slice 1 (validates the rooted fix in isolation) or after Slice 4 (validates the full epic)?
4. Other deferred items — same operator-gated slice cadence?

## Cross-links

- `project_followup_battle_test_post_summary_hang.md` — 14-incident forensic record + original 5-item harness epic.
- `project_f1_w3_slice5b_s1_s6_checkpoint.md` — S5 / S6 forensics that added items 6 + 7.
- `project_wave3_item7_mid_op_cancel_scope.md` — pattern reference for thin-slice arc + graduation.
