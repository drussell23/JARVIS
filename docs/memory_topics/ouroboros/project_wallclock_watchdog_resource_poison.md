---
title: Project Wallclock Watchdog Resource Poison
modules: [backend/core/ouroboros/battle_test/harness.py]
status: historical
source: project_wallclock_watchdog_resource_poison.md
---

Step 6 corrected re-run #3 (session bt-2026-05-17-024509, HEAD 17ae95d7d6, Phase C active) hung 22+ min past the ratified `--max-wall-seconds 2400` and required SIGKILL (SIGTERM-immune → no summary.json).

**Phase C is PROVEN** by this run: `verdict=injected`, both instances, ABSOLUTE worktree paths. The relative-path defect is closed.

**New distinct defect (the 4th in this arc):** WallClockWatchdog hard-deadline thread armed correctly at 19:47:56 (`hard-deadline thread alive` + `armed` + `async monitor task alive` all logged). At 20:22:29 — mid django post-APPLY pytest-subprocess phase (44 test targets; a pytest child killed at 20:22:24) — **every Python thread went silent simultaneously** (async monitor, hard-deadline thread, all sensors) and stayed a 25-min zombie. Not loop-only starvation: a process-wide all-threads freeze (D-state subprocess `waitpid` and/or `logging`-lock deadlock).

**Why the thread didn't save it:** the watchdog kill path is NOT resource-isolated. Layer 2 = `loop.call_soon_threadsafe` (wedged loop) + `_wd_log.warning` (poisoned logging lock); Layer 3 = SIGTERM self (signal path wedged — proven: process ignored external SIGTERM); Layer 4 `os._exit` only after L2/L3, never reached. A wedge poisoning {loop, logging, signals} defeats a watchdog whose fire path depends on exactly those.

**Why:** harness.py `_watch` (~L4670-4830) keeps correct dual-clock math but its escalation touches shared poisoned resources.

**How to apply:** the fix (operator-gated, Phase-C-style) is a resource-ZERO kill path — separate process or pure daemon thread, gated only on monotonic/wall clocks, diagnostic via raw `os.write(preopened_fd)` (never `logging`), then `os.kill(getpid, SIGKILL)`/`os._exit` — touching no loop, no logging, no SIGTERM. Compose the existing `_watch` dual-clock skeleton; sever shared deps. Step 6 arc has now hit 4 distinct defects (contamination → relative-path → [Phase C fixed] → watchdog resource-poison); each only surfaced on a real run. See [[project-swebp-worktree-advisor-prefix]], [[project-v3-7-phase-2-harness-inject]]. Stage 2 still BLOCKED — no valid rubric signal ever produced.
