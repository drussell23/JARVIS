---
title: Project Followup Battle Test Post Summary Hang
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_followup_battle_test_post_summary_hang.md
---

Harness defect surfaced during Wave 3 (6) Slice 5a S3 attempt (2026-04-23). After `stop_reason=idle_timeout` and `summary.json` write, the Python process can remain alive indefinitely in interpreter finalization, never releasing `.jarvis/intake_router.lock`. This wedges the next battle-test session at boot with `RouterAlreadyRunningError` — the zombie-reaper correctly skips live PIDs by design, so a live-but-wedged zombie is a gap.

**Why:** Manifesto §3 (structured concurrency — no event loop starvation) and §8 (absolute observability — every session must produce auditable, deterministic lifecycle). A process that writes its summary then refuses to exit violates both. Two concurrent `ouroboros_battle_test.py` runs on the same repo are now classified as operator error / parallel-run contamination; single-flight is binding going forward.

**How to apply:** When re-encountered, capture `sample <pid> 30 10` before SIGKILL and append to this ticket. Do not regress the zombie-reaper's "skip live PIDs" rule — fix the root cause instead.

## Reproduction context

- Session: `bt-2026-04-24-033849` (not mine — another Claude Code agent's S3 attempt)
- Launch: 2026-04-23T20:38:43, args `--headless --cost-cap 2.00 --idle-timeout 600 --max-wall-seconds 2400 -v`
- Last productive log: `20:56:28 Session stopping: idle_timeout`
- summary.json written: `21:12:01` (≈16 min into shutdown)
- Last log line: `21:12:01 [durable_jsonl_transport] WARNING [DurableJSONL] write error (suppressing further): Executor shutdown has been called`
- Forensic snapshot: 2026-04-23T21:27:21Z, 46:25 elapsed, process still alive
- Snapshot file: `.jarvis/forensics/pid49285_sample_1777004841.txt`

## Stack signature

**Main thread (`Thread_3820985`, DispatchQueue_1)**
```
Py_FinalizeEx
  → PyObject_VectorcallMethod
    → _PyEval_EvalFrameDefault (×N)
      → _threadmodule.c (via load_address+0x1b34c4/0x1b37e0)
        → PyThread_acquire_lock_timed + 552
          → _pthread_cond_wait + 984
            → __psynch_cvwait
```
All 2742 samples (entire 30s capture) on the same condvar-wait. Zero progress.

**Secondary thread (`Thread_3824145`)**
```
_PyEval_EvalFrameDefault (deep Python frames)
  → _threadmodule.c
    → PyThread_acquire_lock_timed + 268
      → _pthread_cond_wait
        → __psynch_cvwait
```
Also stuck on a condvar. Classic circular-wait / uncollectable non-daemon thread.

## Root-cause hypothesis

`threading._shutdown()` (invoked from `Py_FinalizeEx`) joins all non-daemon threads. One of JARVIS's background threads is both:
1. **Non-daemon** (so atexit blocks on joining it), and
2. **Waiting on a lock or condvar that will never be released** (because the object that would signal it was already torn down, or the signaller is itself one of the threads being joined — deadlock).

Likely suspects (verify before fixing):
- `DurableJSONL` executor — last log line cites "Executor shutdown has been called"
- `HibernationProber` — owns long-lived background tasks
- asyncio `loop.close()` without prior `shutdown_asyncgens()` + `shutdown_default_executor()` — Python 3.9 teardown hygiene (already partially fixed in `5a320cfe3f` but may have remaining gaps)
- Any `threading.Thread(daemon=False)` that runs a long blocking call not interruptible by shutdown signal

## Impact

- **Direct:** Wedges `.jarvis/intake_router.lock`, blocks `IntakeLayer.start()` in next session (`RouterAlreadyRunningError`).
- **Cascading:** All 16 sensors skipped; zero INTENT emissions; next session idles to timeout with ops=0, producing no attributable data. S3 bt-2026-04-24-035702 is the observed instance (duration 718s, 0 markers, 0 ops).
- **Classification pollution:** Without this ticket, infra-failed sessions get mis-read as starvation failures, corrupting the Wave 3 (6) Slice 5a graduation ledger.

## Suggested fix scope (one paragraph, per operator)

After `idle_timeout`, the process must exit. Post-`summary.json` cleanup must be bounded (suggest: 30s hard deadline in a finally-clause wrapping the cleanup chain, with `os._exit()` as the escape hatch). `.jarvis/intake_router.lock` release must live in a `finally` block or equivalent unconditional path — not contingent on clean executor shutdown. The zombie reaper (`JARVIS_BATTLE_REAP_ZOMBIES`) should treat "PID alive but past wall-clock cap + 2×cleanup-budget" as killable, adding a `wedged_zombie` reason code to the reaper's exit counter. All non-daemon threads owned by the harness/governance stack should either flip `daemon=True` or register an explicit shutdown hook invoked before `Py_FinalizeEx`.

## Session contamination policy (operator-binding 2026-04-23)

- Single-flight rule: `pgrep -f ouroboros_battle_test` must be empty AND `.jarvis/intake_router.lock` must not exist before launching S*.
- Concurrent runs on the same repo → operator error / contaminated state.
- `bt-2026-04-24-033849` is marked **superseded / parallel-run contamination**; its artifacts should be excluded from Wave 3 (6) Slice 5a graduation ledger analysis.
- `bt-2026-04-24-035702` (my S3, infra-failed on lock contention) is a **harness-class waiver row**, not a runner-attributable session.

## Status

- Forensic snapshot captured ✓ (PID 49285 + 2026-04-23T21:40 batch: 20089, 22798, 26749 + 2026-04-23T21:47 batch: 47079, 51435)
- **Mass cleanup 2026-04-23T21:50Z**: 21 Python wedges + 11 zsh wrappers terminated (32 PIDs total)
- Lock cleared ✓
- Field verified clean: `pgrep -f ouroboros_battle_test` exit 1
- Fix: harness epic below — track as post-Wave-3-(6)-FINAL unless it blocks again.

## Mass cleanup record (2026-04-23T21:50Z)

Cleanup expanded from 10 PIDs (original ticket scope) to **32 PIDs** after discovering the contamination was cross-session. Each Python PID sampled or pattern-matched the identical `Py_FinalizeEx → PyThread_acquire_lock_timed → __psynch_cvwait` signature as PID 49285.

**Override predicate invoked** (operator-binding 2026-04-23): kill a live Python child when all hold:
1. Elapsed ≫ `--max-wall-seconds` (>2× wall, minimum >2h floor when wall is 2400s)
2. No session-dir writes for ≥10 min
3. Long-sleeping + low CPU consistent with interpreter shutdown wedge

**21 Python processes terminated**:

| PID | Elapsed | Parent | Sample taken? | Signature match |
|---|---|---|---|---|
| 270 | 1d 1h 35m | 1 (orphan) | pattern-only | by pattern |
| 2260 | 1d 1h 6m | 1 (orphan) | pattern-only | by pattern |
| 3960 | 1d 0h 38m | 1 (orphan) | pattern-only | by pattern |
| 5989 | 1d 0h 11m | 1 (orphan) | pattern-only | by pattern |
| 7772 | 23h 45m | 1 (orphan) | pattern-only | by pattern |
| 9461 | 23h 21m | 1 (orphan) | pattern-only | by pattern |
| 11382 | 22h 21m | 1 (orphan) | pattern-only | by pattern |
| 20089 | 7h 57m | 20084 | ✓ 20s | `Py_FinalizeEx`×1, `PyThread_acquire_lock_timed`×10, `__psynch_cvwait`×17 |
| 22798 | 7h 23m | 22795 | ✓ 20s | `Py_FinalizeEx`×1, `PyThread_acquire_lock_timed`×9, `__psynch_cvwait`×18 |
| 26749 | 6h 27m | 26746 | ✓ 20s | `Py_FinalizeEx`×1, `PyThread_acquire_lock_timed`×9, `__psynch_cvwait`×17 |
| 28993 | 5h 55m | 28990 | pattern-only | by pattern |
| 30736 | 5h 28m | 30733 | pattern-only | by pattern |
| 32433 | 4h 49m | 32428 | pattern-only | by pattern |
| 34441 | 4h 19m | 34438 | pattern-only | by pattern |
| 42660 | 2h 31m | 42657 | pattern-only | by pattern |
| 47079 | 1h 35m | 47077 | ✓ 15s (borderline) | `Py_FinalizeEx`×1, `PyThread_acquire_lock_timed`×9, `__psynch_cvwait`×18 |
| 51435 | 44m | 51433 | ✓ 15s (borderline) | `Py_FinalizeEx`×1, `PyThread_acquire_lock_timed`×8, `__psynch_cvwait`×17 |
| 93223 | 1d 3h 18m | 1 (orphan) | pattern-only | by pattern |
| 94801 | 1d 2h 56m | 1 (orphan) | pattern-only | by pattern |
| 94968 | 1d 2h 55m | 1 (orphan) | pattern-only | by pattern |
| 95183 | 1d 2h 53m | 95181 | pattern-only | by pattern |
| 95286 | 1d 2h 52m | 95282 | pattern-only | by pattern |
| 97332 | 1d 2h 23m | 97328 | pattern-only | by pattern |
| 98555 | 1d 2h 2m | 98551 | pattern-only | by pattern |

**11 zsh wrappers terminated**: 28990, 30733, 32428, 34438, 42657, 47077, 51433, 95181, 95282, 97328, 98551. Each a `/bin/zsh -c source <shell-snapshot> && eval '...python3 scripts/ouroboros_battle_test.py...' < /dev/null && pwd -P >| /tmp/claude-*` pattern — Claude Code Bash-tool signature.

## 9th zombie record (2026-04-24, Wave 3 (6) Slice 5b / F2 graduation S1)

- **session_id**: `bt-2026-04-24-062608` (F2 graduation S1 under MERGE_HEAD_SHA `4bdc9f58d5`)
- **python PID**: 57884, **zsh wrapper PID**: 57881
- **Elapsed at kill**: 34:28 (python); wall cap was 2400s (40min). Under the 2×-wall floor but past session completion (summary written 20min prior to kill).
- **Signature**: `Py_FinalizeEx`×1, `PyThread_acquire_lock_timed`×9, `__psynch_cvwait`×20. Identical to the 8 prior samples (same offset chain via `_threadmodule.c +0x1b34c4/+0x1b37e0`). Full-capture idle on condvar.
- **Sample file**: `.jarvis/forensics/pid57884_sample_<ts>.txt`.
- **Lock held**: yes — lock file contained `{"pid": 57884, "ts": 1777011993.534831}`. Wedged Python held it for 20+ min post-summary.
- **Last log line**: `[DurableJSONL] write error (suppressing further): Executor shutdown has been called` — same signature as zombies #1–#8.
- **Kill sequence**: sample → SIGTERM → exited cleanly on TERM (both wrapper + python). Lock removed manually.
- **Escalation**: this is now 9 identical reproductions. The harness epic (item #3 bounded-shutdown fix) is P0 per operator 2026-04-24 binding ("harness bounded shutdown / os._exit fallback + executor audit — P0 for operator reliability"). Recommended to co-ship with F1 (item #1 intake priority scheduling).

**7 Python orphans (270, 2260, 3960, 5989, 7772, 9461, 11382)** survived the Phase-1 kill of their original zsh wrappers because `tail -f /dev/null` in the wrapper's pipeline kept stdout un-closed — so the Python child never received SIGPIPE when the wrapper died. The borderline sample evidence (47079, 51435 both showing identical `Py_FinalizeEx` wedge even at <2h elapsed) confirms the 7 orphans are same-class zombies despite pattern-only classification.

**Root cause, verified across 5 sampled PIDs (49285, 20089, 22798, 26749, 47079, 51435)**: `threading._shutdown()` in `Py_FinalizeEx` deadlocks on a non-daemon thread's lock-acquire. Per `sample`, the wait chain crosses `_threadmodule.c` at offset `+0x1b34c4`/`+0x1b37e0` → `PyThread_acquire_lock_timed` → `_pthread_cond_wait` → `__psynch_cvwait`. Full-capture idle with zero frame movement. Reproducible across 6 independent Python processes spanning 44m to 1d+3h elapsed time.

## Harness epic — five deterministic fix items (operator-authorized 2026-04-23)

1. **Ban `tail -f /dev/null | python ...` stdin guard** in all runbooks and agent wrappers — `--headless` only (Ticket C). Add CI grep guard: `git grep -E "tail -f /dev/null \\| python" docs/ scripts/` must return empty. Existing scaffolding references (if any) get deleted or migrated.
2. **`intake_router.lock` lifecycle hardening**: acquire/release wrapped in a `finally` block at the `UnifiedIntakeRouter.start`/`stop` boundary; lock content must include PID + monotonic timestamp; add stale-lock TTL (suggest: `max(2 × max_wall_seconds, 7200s)` — configurable via `JARVIS_INTAKE_LOCK_STALE_TTL_S`) or PID-aliveness check at acquire time so a dead PID's lock is adopted, not blocking. §3 structured concurrency.
3. **Bounded post-`summary.json` shutdown**: wrap the post-idle-stop cleanup chain in a watchdog (suggest: `asyncio.wait_for(self._cleanup(), timeout=30)` followed by `os._exit(<reason_code>)` on timeout). Explicit interpreter exit path — never block `Py_FinalizeEx` on a lock-wait. Add a `stop_reason=wedged_shutdown` code + session_outcome=`incomplete_shutdown` if watchdog fires. §2 readiness + §8 audit completion.
4. **`pgrep` hygiene**: canonical soak-liveness probe documented in `docs/runbooks/battle_test.md`: `pgrep -f "python3? scripts/ouroboros_battle_test\\.py"` — avoids matching zsh wrappers' eval text. Agent-conducted-soak runbook section should reference this probe by name.
5. **Single-flight enforcement in launcher**: preflight in `scripts/ouroboros_battle_test.py` that `pgrep -f "python3? scripts/ouroboros_battle_test\\.py"` returns at most 1 (the current process); and that `.jarvis/intake_router.lock` either doesn't exist or has a dead PID. Fail-fast with exit-code 75 (`EX_TEMPFAIL`) if either check fails, printing the violators. Parallel-run contamination becomes impossible at the process level.

These land as a single harness-ticket epic; slice breakdown TBD once operator authorizes the arc (expected post-Wave-3-(6)-FINAL). Forensic evidence for each item: this ticket's sample files under `.jarvis/forensics/`.

## 13th incident (2026-04-24, F1 Slice 4 S5) — NEW failure class: partial-summary insurance regression

- **session_id**: `bt-2026-04-24-220418` (F1 Slice 4 S5 under MERGE_HEAD_SHA `87ccbcee15`)
- **Failure mode**: **DIFFERENT from incidents #1–#12**. Process exited cleanly on SIGTERM (exit code 0, no zombie wedge). But `summary.json` was **never written** — not even a partial v1.1b incomplete_kill record.
- **Kill method**: `kill -TERM 71229` at 2026-04-24T15:16Z. Process exited within 5s. No SIGKILL escalation needed.
- **Artifacts intact**: `debug.log` (116KB, last write 15:14:27). No `summary.json`, no `report.ipynb`, no `notebook/`. Just the debug log in the session dir.

**Why this matters**: `CLAUDE.md` documents partial-shutdown insurance (`docs/architecture/...` + this ticket's existence) as binding:
> "Partial-shutdown insurance: the harness registers an `atexit` fallback **and** a sync signal-handler write so every session dir ends up with a v1.1a-parseable `summary.json` — even when SIGTERM arrives mid-cleanup..."

S5 falsifies that claim. SIGTERM arrived during steady-state operation (not mid-cleanup — the process was actively dispatching DocStaleness BG ops at 15:14:27, ~2 min before kill). Either (a) the SIGTERM handler fired but threw before writing, (b) the handler isn't installed, or (c) the handler is installed but uses `asyncio` primitives that can't run from a sync signal handler.

**Hypothesis**: The signal-handler partial-summary-write path requires the asyncio event loop to be alive to gather context (cost_governor totals, ops_completed counts, etc.). When SIGTERM fires *during* steady-state, the loop IS alive but the handler may try to enter it via `loop.run_until_complete()` — which deadlocks if the loop is already running. The atexit fallback then never fires because the process isn't actually exiting through the normal `_generate_report` path.

**Operator impact**: Without summary.json, this session is invisible to the W3(6) graduation ledger format and the `LastSessionSummary.read_recent()` digest. The session is "lost" for graduation accounting purposes — only the debug log can be hand-parsed.

**Stack signature**: not captured (no zombie — process exited cleanly). Adding this as **harness epic item #6** below.

### Harness epic item 6 — SIGTERM-during-steady-state partial-summary write

The signal-handler path must produce a v1.1b-parseable `summary.json` regardless of whether the SIGTERM arrives during:
- Boot (no ops yet)
- Steady-state (loop running, ops in flight)
- Post-`stop_reason` cleanup (the existing well-tested path)
- Post-summary teardown (the Py_FinalizeEx zombie window — incidents #1–#12)

Suggested fix scope: the signal handler should write a **minimal-fields-only** partial summary using only sync introspection (no asyncio loop invocation): `{"schema_version": 2, "session_outcome": "incomplete_kill", "stop_reason": "<sigterm|sighup|sigint>", "session_id": <id>, "started_at": <epoch>, "ended_at": <epoch>, "kill_pid_signal": <signum>}`. Anything richer (cost totals, ops counts) is best-effort and may be omitted if the loop is unreachable from signal-handler context.

Co-ships naturally with item #3 (bounded post-summary shutdown).

## 14th incident (2026-04-24, F1 Slice 4 S6) — Item 6 reproduces; NEW Item 7 surfaces

### Item 6 reproduction

- **session_id**: `bt-2026-04-24-225137` (F1 Slice 4 S6 under MERGE_HEAD_SHA `25fdf09d24`)
- **Failure mode**: identical to S5 incident (#13). SIGTERM did NOT exit within 8s — required SIGKILL escalation. `summary.json` not written. Only `debug.log` (158KB) in session dir.
- **Distinct from #1–#12**: process exited cleanly on SIGKILL (no Py_FinalizeEx wedge). The SIGTERM handler either deadlocked or the signal was masked.
- **Confirms hypothesis from #13**: signal handler attempts asyncio loop invocation that deadlocks when loop is running. Fix scope unchanged.

### Item 7 — `WallClockWatchdog` did not fire (NEW class)

The harness arms a `max_wall_seconds=2400` watchdog at boot:

```
2026-04-24T15:51:55 [WallClockWatchdog] armed: max_wall_seconds=2400s — session
will terminate with stop_reason=wall_clock_cap if not already stopped.
```

S6 boot at 15:51:55 → cap should fire at 16:31:55. Session ran past 16:42:17 (51min wall, 25% over cap) without firing `stop_reason=wall_clock_cap` and without any other shutdown event. Required external SIGKILL.

**Operator impact**: For headless / agent-conducted soaks, the wall watchdog is the only safety bound on a runaway session (idle-timeout doesn't fire when a hung asyncio task keeps the loop nominally alive; cost-cap can be evaded by ops that never bill a provider, e.g. the BACKGROUND-route DW-blocked-by-topology pattern that fires `next_phase=None status=fail` without calling any provider).

**Hypothesis**: the watchdog is implemented as an asyncio task (likely `asyncio.create_task(_watchdog())` with `asyncio.sleep(max_wall_seconds)`). If the event loop is starving — long blocking call, sync introspection in handlers, etc. — the watchdog's sleep can drift indefinitely. Or the watchdog is shielded but never gets scheduled because something else is monopolizing the loop.

**Suggested fix scope**:
1. Add per-tick wall-clock log: `[WallClockWatchdog] tick wall_elapsed=Ns remaining=Ms` at ≥1/30s cadence so silent-fire vs starved is observable.
2. Implement watchdog as a thread (not asyncio task) with `os._exit(<reason_code>)` escape hatch — guarantees fire regardless of event-loop state. Threading-based timeouts are the standard idiom for "must terminate even if Python is wedged" in long-running services.
3. Add `stop_reason=wall_clock_cap_starved` distinct from `wall_clock_cap` to disambiguate "fired correctly" from "fired late" from "had to escape via thread fallback".

**Cross-link**: item 7 co-ships naturally with item 3 (bounded post-summary shutdown) since both want a sync-thread-based deadline escape hatch from asyncio-land.

### S6 forensic snapshot — none captured

Process exited on SIGKILL within seconds; no opportunity for `sample <pid>`. The wall-cap-not-firing class is observable in `debug.log` alone (presence of `[WallClockWatchdog] armed` + absence of `stop_reason=wall_clock_cap` + actual session duration > cap).


