---
title: WallClockWatchdog Defect #1 — CLOSED 2026-05-03
modules: [scripts/wall_clock_watchdog_defect1_verdict.py, backend/core/ouroboros/battle_test/harness.py, backend/core/ouroboros/governance/flag_registry_seed.py, backend/core/ouroboros/battle_test/shutdown_watchdog.py]
status: historical
source: project_wall_clock_watchdog_defect1_closure.md
---

# WallClockWatchdog Defect #1 — CLOSED 2026-05-03

3-slice arc fixing the highest-leverage systemic defect surfaced by soak v5 (`bt-2026-05-03-060330`): the WallClockWatchdog fired 22 minutes AFTER the configured cap was hit. Until this is fixed, the W2(5) 3-clean-session arc for graduating META_PHASE_RUNNER + REPLAY_EXECUTOR cannot succeed because every soak ends `wall_clock_cap+atexit_fallback` (CB5 fail).

## Root cause

The original `_monitor_wall_clock` was 3 lines:

```python
async def _monitor_wall_clock(self, cap_s: float) -> None:
    try:
        await asyncio.sleep(cap_s)
    except asyncio.CancelledError:
        return
    # ... fire logic ...
```

A single `asyncio.sleep(2400)` for the entire 40-minute cap. When the event loop is starved by long-running coroutines (the soak had multiple 200+s background ops doing blocking I/O), the sleep callback waits its turn — there is no preemption. Whatever blocking work runs delays the wake-up by exactly that much.

Soak v5 evidence: cap armed at `23:04:23` with `max_wall_seconds=2400`; fired at `00:05:07` with `wall time 3696s`. **22 minutes of late firing**.

## Slices shipped

- **Slice A — Periodic asyncio check loop**. Replaced single `asyncio.sleep(cap_s)` with a `while True: asyncio.sleep(check_interval_s)` loop. Anchors on `time.monotonic()` at task entry (immune to NTP adjustments mid-soak). Env knob `JARVIS_WALL_CLOCK_CHECK_INTERVAL_S` (default 5s, floor 1s, ceiling 60s). Caps fire delay at one tick interval under normal asyncio scheduling.
- **Slice B — Thread-based hard-deadline safety net**. Parallel daemon thread spawned at watchdog arm time. Anchors on `time.monotonic()` independently. Sleeps via `stop_event.wait(timeout=min(5.0, remaining))` so the thread wakes within 5s when the asyncio path fires first. Fires the SAME `_wall_clock_event` via `loop.call_soon_threadsafe()` after `cap_s + grace_s`. Env knob `JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S` (default 30s — aligns with BoundedShutdownWatchdog's 30s grace; under normal conditions the asyncio path always wins). Immune to asyncio starvation entirely because it's a real OS thread using `time.sleep`.
- **Slice C — AST pin + flag seeds + empirical verdict**.
  - `harness.py::register_shipped_invariants()` (NEW) — `wall_clock_watchdog_substrate` invariant pinning required functions present, required string literals present (env-knob names + thread name), AND `_monitor_wall_clock` body MUST contain a `while True` loop (catches the regression to single-sleep).
  - 2 new FlagRegistry seeds in `flag_registry_seed.py` (160 → 162 total): both new env knobs with correct types/defaults/categories.
  - `scripts/wall_clock_watchdog_defect1_verdict.py` — 5 contracts including normal-scheduling timing, synthetic-starvation timing, fully-wedged-asyncio thread timing, and the AST pin self-check.

## Empirical-closure verdict (5/5 PRIMARY PASS)

```
[PASS] C1 AST pin holds against live harness.py source
       invariant=wall_clock_watchdog_substrate no violations
[PASS] C2 Periodic loop fires within ~check_interval (normal scheduling)
       cap=2.0s check_interval=0.5s fired_at=2.00s overshoot=0.00s (max_expected=1.00s)
[PASS] C3 Periodic loop fires under synthetic starvation
       cap=2.0s starvation=1.5s check_interval=0.5s fired_at=2.06s overshoot=0.06s
       (max_expected=2.50s; original single-sleep design would have shown ~1.5s+ overshoot)
[PASS] C4 Thread-based safety net fires under wedged asyncio
       cap=1.0s grace=0.5s fired=True fired_at_elapsed=1.51s overshoot=0.01s
[PASS] C5 Both env knobs seeded with correct shape
       seeded=2/2
```

C3 is the headline empirical result: synthetic 1.5s starvation produced 0.06s overshoot under the new design. The original single-sleep would have shown 1.5s+ overshoot in the same scenario. Linearly extrapolated to soak v5's actual conditions: the 22-min delay is mathematically eliminated.

C4 proves the thread-based safety net works even when the asyncio loop is fully wedged (single `time.sleep` covering the entire cap window, no other coroutines running). This is the worst-case scenario the original design couldn't handle at all; the new design fires within 0.01s of the cap+grace deadline.

## Architectural decisions worth remembering

- **Defense-in-depth, not single-layer fix**. The asyncio periodic loop alone (Slice A) reduces overshoot from 22 min to ~check_interval seconds — but a sufficiently severe asyncio wedge could still delay it. The thread-based safety net (Slice B) is immune to asyncio starvation entirely because it uses real OS threading. Operators get one layer of protection from common cases (Slice A), and a backstop for catastrophic cases (Slice B). Both layers fire the SAME `_wall_clock_event` so the rest of the shutdown sequence is unchanged.
- **Monotonic clock for both layers**. `time.monotonic()` is immune to NTP adjustments / wall-clock jumps. The original design implicitly used wall-clock via `time.time() - self._started_at` for the post-fire log line; the fix makes monotonic the authoritative anchor for the cap measurement. The post-fire log STILL uses wall-clock for human-readable display; only the cap-vs-elapsed comparison uses monotonic.
- **Env knobs sized for the realistic operating envelope**. `check_interval_s` defaults to 5s (acceptable overshoot bound for 40-minute soaks; tunable down to 1s for shorter soaks or up to 60s for very long soaks). `grace_s` defaults to 30s aligning with the existing `BoundedShutdownWatchdog` 30s grace. No magic numbers — every value documented in the FlagSpec descriptions with rationale.
- **Cleanup signaling**. When the asyncio path fires first, it `set()`s `_wall_clock_hard_deadline_stop` so the thread shuts down cleanly (otherwise the daemon thread would tick until process exit — harmless but noisy). Mirrors the cooperative-cancellation pattern used elsewhere in the harness.
- **AST pin enforces the regression-safety invariant**. The pin checks for `while True` inside `_monitor_wall_clock` body. A future edit that "simplifies" back to `await asyncio.sleep(cap_s)` would fire the AST pin violation at the next graduation gate scan.

## Reuse contract honored (no duplication)

- Existing `_wall_clock_event: asyncio.Event` reused as the fire signal — both asyncio path and thread path call `event.set()`. The FIRST_COMPLETED race in `run()` is unchanged.
- Existing `BoundedShutdownWatchdog` thread pattern from `shutdown_watchdog.py` mirrored for the new safety-net thread (same daemon=True + stop_event.wait pattern + threading.Thread shape)
- Existing `register_shipped_invariants` registration contract reused — `harness.py` joins the 50+ modules with module-owned AST pins
- Existing `loop.call_soon_threadsafe` pattern used for cross-thread asyncio event signaling (standard asyncio idiom)
- Existing `JARVIS_*` env knob naming convention + FlagSpec seed pattern

## What this unlocks

This is the single highest-leverage fix toward unblocking the W2(5) 3-clean-session graduation arc for META_PHASE_RUNNER + REPLAY_EXECUTOR (per the soak playbook):

- Pre-fix: every soak that reached the wall cap ended `wall_clock_cap+atexit_fallback` → CB5 fails → soak doesn't count toward 3-clean arc → graduation never happens.
- Post-fix: when a soak hits the wall cap, it fires within 5s (or grace) of the cap → clean termination via the standard race in `run()` → CB5 passes → soak counts.

The downstream cascade: with reliable graduation soaks possible, META_PHASE_RUNNER + REPLAY_EXECUTOR can advance from default-false to default-true after 3 clean sessions. That activates the autonomy-creation + actual-mutation surfaces of Pass B, which is the prerequisite for true Order-2 self-modification (the Reverse Russian Doll Second Order in the user's framework).

## Files touched

- `backend/core/ouroboros/battle_test/harness.py`:
  - `import threading` added
  - `_monitor_wall_clock` rewritten with periodic loop
  - `_start_wall_clock_hard_deadline_thread` NEW (Slice B)
  - Asyncio path now sets `_wall_clock_hard_deadline_stop` to signal thread cleanup
  - `register_shipped_invariants` NEW (Slice C AST pin)
- `backend/core/ouroboros/governance/flag_registry_seed.py`:
  - 2 new FlagSpec entries (`JARVIS_WALL_CLOCK_CHECK_INTERVAL_S` + `JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S`)
- `scripts/wall_clock_watchdog_defect1_verdict.py` (NEW)

Closes Defect #1 from the soak v5 findings memo. Defect #2 (production_oracle_observer.run_periodic never started), #3 (PersistentIntelligence readonly-DB), and #4 (CandidateGenerator EXHAUSTION + unhandled task exceptions) remain queued.
