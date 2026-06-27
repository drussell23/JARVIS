---
title: Project Known Preexisting Test Flakes
modules: [tests/governance/intake/sensors/test_backlog_sensor.py, tests/governance/intake/test_unified_intake_router.py, backend/core/ouroboros/governance/sensor_governor.py, backend/core/ouroboros/governance/intake/unified_intake_router.py]
status: historical
source: project_known_preexisting_test_flakes.md
---

## Status

- **Opened 2026-04-24** per operator binding after F1 Slice 2 acceptance:
  > *"Pre-existing 3 failures: Please one ticket or matrix row (not forgotten flakes)."*
- **None blocking**. All three fail identically on pre-F2 baseline (verified via `git stash` diff during F2 Slice 2 implementation). Not caused by F2 or F1 arcs.
- **Fix ownership**: unassigned. Separate one-off PRs possible at any time; none gate any active arc.

## The three flakes

### (1) `tests/governance/intake/sensors/test_backlog_sensor.py::test_sensor_start_stop`

**Failure signature**:
```
assert True is False
 +  where True = <BacklogSensor ...>._running
```

**Root cause**: Test body calls `sensor.stop()` without `await`. The method is `async def stop`, so the coroutine is never awaited — the actual stop logic never runs, and `sensor._running` stays True. Classic missed-await in test code.

**Fix**: one-line change — `sensor.stop()` → `await sensor.stop()` (plus `async def test_sensor_start_stop(tmp_path)` marker if not already present).

**Scope**: test code only. Zero production impact.

### (2) `tests/governance/intake/test_unified_intake_router.py::test_submit_called_with_correct_trigger_source`

**Failure signature**:
```
AssertionError: expected GLS.submit to be called at least once
assert 0 > 0
 +  where 0 = <AsyncMock name='mock.submit'>.call_count
```

Captured log: `[Router] governor SHADOW deny (would have thrown): sensor=backlog urgency=normal reason=governor.sensor_cap_exhausted cap=3 count=3`

**Root cause**: SensorGovernor's rolling-window counters bleed across tests. The governor is in SHADOW mode (per its default graduation state) — not actually denying, but the test's dispatch never happens because... [not fully traced]. The log line proves the governor's internal state from prior tests in the session is leaking into this one, tripping the `cap=3 count=3` threshold.

**Candidate fixes**:
- Reset governor state in a per-test fixture
- Run the suite with `pytest --forked` (isolates each test in its own process)
- Clear `_recent_ops` / rolling deques in `IntakeRouter.__init__` more aggressively

**Scope**: test infrastructure + possibly small `IntakeRouter` ctor tweak. Zero production impact under normal (non-parallel-test) operation.

### (3) `tests/governance/intake/test_unified_intake_router.py::test_dead_letter_after_max_retries`

**Failure signature**:
```
assert 0 >= 1
 +  where 0 = dead_letter_count()
```

**Root cause**: Same governor state-bleed pattern as (2). The retries that would land in dead-letter never execute because the governor shadow-deny intervenes first.

**Fix**: same as (2) — test isolation via fixture reset or `--forked`.

**Scope**: test infrastructure. Zero production impact.

## Pattern

(2) and (3) share a root cause: **test ordering produces nondeterministic governor state bleed**. A reset-governor fixture (`@pytest.fixture(autouse=True)` that clears `_recent_ops` and related rolling counters) would likely fix both with a single small change.

(1) is a standalone test bug.

## Non-goals

- **Not F1/F2-blocking**. Do not gate any active graduation cadence on these fixes.
- **Not a memory-hygiene crisis**. Flakes have been stable / not spreading; fixing them is pure code quality, not risk reduction.
- **Not urgent**. Operator explicitly said *"no need for a memory file unless you prefer it"* — so this tracking is informational, not a blocker.

## Regression discipline

When any new PR touches:
- `tests/governance/intake/sensors/test_backlog_sensor.py`
- `tests/governance/intake/test_unified_intake_router.py`
- `backend/core/ouroboros/governance/sensor_governor.py`
- `backend/core/ouroboros/governance/intake/unified_intake_router.py`

— ensure these three tests still fail identically (or get fixed atomically with the PR). New PRs should NOT introduce a *fourth* pre-existing flake to this list; if one appears, either fix it in the same PR or add it here.

## Cross-links

- **Mentioned in**: `project_followup_f2_backlog_urgency_hint_schema.md`, `project_followup_f1_intake_governor_enforcement.md` (both arcs reference these as pre-existing and out-of-scope).
- **Verified pre-F2** via `git stash` diff during F2 Slice 2 implementation (2026-04-23). Same 3 failures reproduced on clean pre-F2 baseline.
