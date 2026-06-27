---
title: Stale test_sensor_start_stop in test_backlog_sensor.py
modules: [tests/governance/intake/sensors/test_backlog_sensor.py, backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py]
status: historical
source: project_followup_stale_test_backlog_sensor_start_stop.md
---

# Stale test_sensor_start_stop in test_backlog_sensor.py

**Status:** open, non-blocking. Filed 2026-04-23 as part of F3 commit `768637046f` landing.

## Failure

`tests/governance/intake/sensors/test_backlog_sensor.py::test_sensor_start_stop` asserts `sensor._running is False` after `sensor.stop()`, but `_running` is `True` in the current main baseline. Pre-existing — confirmed by stashed-baseline rerun pre-F3.

## Bisect owner

Most-recent touch: BacklogSensor FS-events migration (Gap #4 arc) introduced `_fs_events_mode` + `_fs_events_handled` state and may have shifted `_running` lifecycle semantics relative to the test's expectation. Likely commit range: `fcfc26df71..5a320cfe3f` (Gap #4 sensor migrations, Apr 20–22 2026). Assigning no active owner; whoever next touches `backlog_sensor.py::start`/`stop` should fix this assertion or document why it should be removed.

## Non-blocking

Does not affect F3 (verified via stash). Does not affect Wave 3 (6) Slice 5a graduation.
