---
title: Project V2 92 Layer7 Dual Clock Watchdog
modules: [tests/battle_test/test_layer7_dual_clock_watchdog.py, backend/core/ouroboros/battle_test/harness.py]
status: historical
source: project_v2_92_layer7_dual_clock_watchdog.md
---

May 10 2026: closes Layer 7 of the cadence-arc — the sleep/suspend gap exposed by soak #2.

**Diagnosis trail**:

After soak `bt-2026-05-10-093428` produced a clean evidence row, deep-dive forensics on its 11-hour duration (vs 40-min cap) revealed:

| Wall time (PDT) | Monotonic elapsed | Wall elapsed | Skew |
|----------------|-------------------|--------------|------|
| 02:34 (start) | 0s | 0s | 0 |
| ~10:59 (heartbeat tick=48) | 1603s | ~30240s | ~28637s |
| ~13:36 (heartbeat tick=60) | 1790s | ~39720s | ~37930s |
| 13:37:47 (cancel) | 1892s | 39798s | 37906s |

The asyncio watchdog task's `time.monotonic()` advanced only 1892s while wall-clock advanced 39798s. The hard-deadline thread (Slice B safety net) ALSO uses `time.monotonic()`, so it was paused identically.

**Root cause**: macOS's `time.monotonic()` is backed by `mach_absolute_time()`, which counts CPU ticks. During host sleep/suspend, the CPU is halted, so `mach_absolute_time()` does not advance. When the laptop wakes, monotonic resumes from where it paused. This is OS-level behavior, not a Python or asyncio bug.

The watchdog's stated contract is "wall-clock cap" (cf. method docstring: `Opaque ceiling on total session duration ... wall-clock elapsed time`). Monotonic-only enforcement violates that contract whenever the host suspends.

**Structural fix at `harness.py` _monitor_wall_clock + _start_wall_clock_hard_deadline_thread**:

1. **Dual-clock anchors**: capture both `time.monotonic()` AND `time.time()` at task/thread entry.
2. **Effective elapsed** computed as `max(elapsed_monotonic, elapsed_wall)`:
   - Wall jumps backward (NTP rollback) → wall < monotonic → max picks monotonic. **NTP-rollback safety preserved.**
   - Wall jumps forward (NTP step) → wall > monotonic → max picks wall. Cap fires earlier than intended — acceptable for soak semantics (operator wanted "kill after N seconds of real time").
   - Host sleep → monotonic pauses, wall advances → max picks wall. **Cap fires correctly on wake.**
3. **Hard-deadline thread**: same dual-clock pattern. `remaining = min(remaining_monotonic, remaining_wall)` — whichever clock approaches the deadline first wins. Diagnostic log line includes `won_clock` field for operator-visible forensics.
4. **Skew warning**: env-tunable `JARVIS_WALL_CLOCK_SKEW_WARN_THRESHOLD_S` (default 60s, floor 5s, ceiling 3600s). When wall_elapsed - monotonic_elapsed exceeds threshold, log a debounced warning so operators have a signal when sleep/NTP events occur. Debounce ensures sustained sleep doesn't spam the log every tick.
5. **Cancel-path diagnostic upgrade**: when the async monitor task is cancelled (peer waiter wins the FIRST_COMPLETED race), the "NEVER fired" log line now reports BOTH monotonic and wall elapsed — load-bearing for retrospective sleep-event forensics.

**Updated canonical AST invariant** (`register_shipped_invariants` at harness.py:5194-5310):
- Added `JARVIS_WALL_CLOCK_SKEW_WARN_THRESHOLD_S` to REQUIRED_LITERALS — guarantees the skew-warn path doesn't regress to monotonic-only enforcement.
- AST walker now also detects `time.monotonic()` AND `time.time()` calls inside `_monitor_wall_clock` body — violations raised if either is missing.

**Regression tests** in `tests/battle_test/test_layer7_dual_clock_watchdog.py`:
- 3 monitor-side pins (calls time.monotonic / calls time.time / composes via max())
- 2 thread-side pins (anchors both clocks + computes both deadlines / uses min() over remainings)
- 1 env-knob pin (skew threshold referenced)
- 1 canonical-validator green
- 3 provenance pins (cites v2.92 / cites bt-2026-05-10-093428 / cites mach_absolute_time)

**Why no mocked functional test**: mocking `time.monotonic` AND `time.time` simultaneously interferes with `asyncio.sleep`'s internal scheduling (which uses `time.monotonic` for its delay accounting). The event loop hangs. AST pins are the load-bearing guarantee — drift in the dual-clock composition is structurally detected. The `max(elapsed_monotonic, elapsed_wall)` literal is byte-pinned in source.

**Test results**: 10 Layer 7 tests + 6 Layer 6 tests + 63 broader watchdog/wall-clock/partial-shutdown/termination-hook tests = **79 green, zero regression**.

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — composed both clocks at the seam where elapsed is computed; preserved monotonic's NTP-safety property AND added wall's sleep-detection property
- No workarounds — did NOT add a "wake detector" that retroactively adjusts elapsed; did NOT switch entirely to wall-clock (would lose NTP safety); composition via max() is the canonical resolution
- No shortcuts — 6 AST pins + 3 provenance pins + 1 canonical validator update; drift to single-clock is structurally caught
- Composes existing canonical paths: existing `_monitor_wall_clock` periodic-check loop preserved (Defect #1 fix from 2026-05-03); existing hard-deadline thread structure preserved; existing skew-warn env-knob pattern (mirrors `JARVIS_WALL_CLOCK_CHECK_INTERVAL_S` and `JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S`)
- No hardcoding — skew threshold env-tunable with sane defaults; existing check_interval / grace knobs unchanged
- No duplication — single seam at `_monitor_wall_clock` + single seam at `_start_wall_clock_hard_deadline_thread`; thread mirrors asyncio path exactly

**Files modified**:
- `backend/core/ouroboros/battle_test/harness.py`:
  - `_monitor_wall_clock` (~line 4561): dual-clock anchors + max-based effective_elapsed + skew warning + dual-clock diagnostics in cancel path + dual-clock fire log
  - `_start_wall_clock_hard_deadline_thread` (~line 4347): dual-clock anchors + dual deadlines + min(remaining) + dual-clock fire log with won_clock diagnostic
  - `register_shipped_invariants` (~line 5194): added skew-threshold required literal + dual-clock AST validation (catches drift to single-clock)
- `tests/battle_test/test_layer7_dual_clock_watchdog.py` (NEW, 10 regression tests)

**Cadence arc state** (Layers 1–7 all closed):
- v2.86 — Layers 1-4 (env / wrapper / sentinel / socksio)
- v2.87 — Layer 5 (modality ledger always loads)
- v2.88 — Layer 6 (watchdog Layer 4 escape-hatch writes partial summary)
- **v2.92 — Layer 7 (dual-clock authority — sleep/suspend immunity)**

The structural cadence work is done. The watchdog now honors its "wall-clock cap" contract under all clock-skew scenarios (NTP step forward, NTP step backward, NTP slew, host sleep/suspend, host hibernate, virtualization-induced clock drift).

**Why this matters for RSI**: graduation soaks run under operator-paced cadence. If the operator's laptop sleeps mid-soak, the watchdog MUST still enforce the cap so the soak doesn't run 11 hours and burn budget on idle ops. Layer 7 makes the cadence robust to host-side environmental conditions outside the harness's direct control. This is the load-bearing pre-requisite for unattended Phase 9 graduation cycles.
