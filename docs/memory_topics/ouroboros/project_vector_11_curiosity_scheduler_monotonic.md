---
title: Project Vector 11 Curiosity Scheduler Monotonic
modules: [backend/core/ouroboros/governance/adaptation/curiosity_scheduler.py, tests/governance/test_curiosity_scheduler.py]
status: historical
source: project_vector_11_curiosity_scheduler_monotonic.md
---

May 9 2026: Vector #11 closed. Cheapest unblock from autonomous self-development arc audit (~2h estimate matched).

**Bug**: `CuriosityScheduler.tick()` populated `_fire_history` and `_last_fire_ts` with wall-clock seconds (`time.time()`). Rate-cap window pruned via `now - 3600.0`; cooldown computed via `now - last_fire_ts`. NTP correction mid-session:
- Backward Δs jump: `elapsed = -Δ` → `remaining = cooldown_s + Δ` → spurious throttle for `cooldown_s + Δ` seconds
- Forward Δs jump: rolling-hour window evicted true-recent fires aggressively → spurious cap-bypass

**Fix** (`backend/core/ouroboros/governance/adaptation/curiosity_scheduler.py`):
- Internal state replaced: `_fire_history_mono: List[float]` + `_last_fire_mono: Optional[float]` driven by `time.monotonic()` for ALL gating
- `_last_fire_ts` (wall-clock) preserved as audit-only field → `SchedulerResult.ts_epoch`
- `tick()` accepts new `now_mono: Optional[float] = None` kwarg alongside existing `now_unix`
- Production: both unset → real `time.monotonic()` + `time.time()`
- Tests: when only `now_unix` is supplied, it is mirrored as `mono_ts` for backward-compat — 46 pre-existing tests pass byte-identical
- **Bonus fix**: `seconds_since_last_fire` now reports honest monotonic delta and returns `None` (not `ts - 0`) before first fire — closes legacy bug where the field carried `time.time() ≈ 1.7e9` as "elapsed"

**Regression** (`tests/governance/test_curiosity_scheduler.py:Section J`):
- `TestVector11MonotonicGating` (6): backward NTP correction does NOT extend cooldown / forward jump does NOT evict history early / `seconds_since_last_fire` uses monotonic delta / pre-fire reports None / production path uses real `time.monotonic()` / internal state uses `_fire_history_mono` + `_last_fire_mono` fields
- `TestVector11SourcePins` (4): bytes-pinned source assertions on the `mono_now` parameter signatures + `now_mono` kwarg + production `time.monotonic()` fallback + "Vector #11" + "NTP-immune" provenance citations

**Test results**: 56/56 scheduler (46 backward-compat + 10 new) + **1033/1033 cumulative** across §38.11 + §39 Tier-1+2+3+4+5+7 + scheduler + canonical sources.

**§35 row 🟡 #8** + **§3.6.3 row #7** both flipped ✅ Shipped 2026-05-09.

**Architectural discipline**: re-used canonical Python stdlib `time.monotonic` (NTP-immune) — ZERO new dependencies, ZERO parallel state, ZERO duplicated time math. The fix's audit trail ("Vector #11" + "NTP-immune") is cited in source so future maintainers can find the rationale.

**NEXT** (autonomy arc remaining):
- Vector #9 (FlagChangeEvent credential masking) — was flagged closed in Wave 3 v2.25 but Forward Priority Roadmap §3.6.3 #5 still listed it; verify status
- Vector #10 (AutoCommitter race) — same situation; verify Wave 3 closure vs current
- M10 ArchitectureProposer (~7-10d substrate move closing weak-form ontogeny gap)
- Vector #5 cross-session coherence harness (~1-2 wks empirical validation arc)
