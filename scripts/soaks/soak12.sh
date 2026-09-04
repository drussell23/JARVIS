#!/usr/bin/env bash
# Sanctioned diversity harvest — soak 12 = soak 11 + a validation reserve
# that can actually run pytest on this box.
#
# Soak 11 (bt-2026-09-02-003459) cleared Gate 1: four roadmap ops drew 2+
# structurally distinct candidates (clean_vision_response 3/3 with no
# redundancy). Then VALIDATE failed them all:
#   [python:FAIL] pytest timed out after 10.7-19.4s          (fc=test)
#   [python:FAIL] budget exhausted before adapter run        (fc=infra)
# The validation reserve was ON but sized for a warm runner: 45 s cold x
# 1.5 safety = ~68 s, and pytest collection alone costs 10-20 s here (the
# harness conftest is heavy). Three ~200 s draws then left VALIDATE with
# whatever was under the reserve. An fc=infra verdict is recorded as
# should_train=False, so a starved VALIDATE turns a pairable group into
# untrainable rows -- Gate 3 would fail for an infrastructure reason,
# which is not the question this soak is asking.
#
# Nothing else changed. Every knob is an existing, documented one.
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1

# --- the kill that ended soak 11 -----------------------------------------
# `[ExternalWatchdog] SIGKILL parent reason=heartbeat_stale`: the main loop
# stalled 47 s inside evidence_collectors._gather_test_set_hash_stable —
# a synchronous `Path.is_file()` sweep on /mnt/c (DrvFS stat is slow) run
# from POSTMORTEM — and repeated stalls crossed the 120 s stale threshold.
# A SIGKILL is not a graceful stop: no autotrain, and every pending
# generation (four pairable groups) died un-flushed. Widening the backstop
# is a RUN-level mitigation; the fix is to move that sweep off the loop.
export JARVIS_EXTERNAL_WATCHDOG_STALE_S=600
export JARVIS_EXTERNAL_WATCHDOG_MARGIN_S=120

# --- VALIDATE gets a real slice ------------------------------------------
export JARVIS_VALIDATION_RESERVE_ENABLED=true
export JARVIS_VALIDATION_RESERVE_COLD_S=240        # x1.5 safety -> 360 s reserved
export JARVIS_VALIDATION_RESERVE_SAFETY=1.5
export JARVIS_VALIDATION_RESERVE_MAX_FRACTION=0.5
export JARVIS_TEST_TIMEOUT_S=180

# --- acceptance threshold -------------------------------------------------
# Soak 11 ran 0.999 as a workaround for whole-file dilution. Similarity is
# now measured over the CHANGED HUNKS against the on-disk baseline
# (sibling_entropy.changed_hunks), and the bar scales by what the hunks
# are made of, so the calibrated 0.95 is right again: two edits that differ
# in what they DO measure ~0.49 at hunk level; a comment twin still 1.0.
export JARVIS_SIBLING_DIVERSITY_THRESHOLD=0.95

# --- budgets: room for 3 draws (~900 s) + the reserve + retries ------------
export JARVIS_PIPELINE_TIMEOUT_S=2400
export JARVIS_GEN_TIMEOUT_STANDARD_S=1800
export JARVIS_GENERATION_TIMEOUT_S=1800
export JARVIS_BG_WORKER_OP_TIMEOUT_S=2400
export JARVIS_LOCAL_SIBLING_BUDGET_MARGIN=1.0

# --- room: nothing parks, nothing replays, FIFO simply drains -------------
export JARVIS_THROUGHPUT_GOVERNOR_ENABLED=false
export JARVIS_BG_POOL_SIZE=6
export JARVIS_BG_QUEUE_SIZE=64

# --- quiet what can be quieted; slow the rest to ONE boot scan ------------
export JARVIS_DOC_STALENESS_ENABLED=false
export JARVIS_RUNTIME_HEALTH_SENSOR_ENABLED=false
export JARVIS_GITHUB_ISSUE_SENSOR_ENABLED=false
export JARVIS_OPPORTUNITY_MINER_SENSOR_ENABLED=false
export JARVIS_INTENT_TEST_INTERVAL_S=86400
export JARVIS_TODO_SCAN_INTERVAL_S=86400
export JARVIS_EXPLORATION_INTERVAL_S=86400
export JARVIS_INTAKE_BACKLOG_SCAN_INTERVAL_S=86400

# --- the work: exactly the staged batch, emitted ONCE ---------------------
export JARVIS_WORK_ORDER_SENSOR_ENABLED=true
export JARVIS_WORK_ORDER_INTERVAL_S=3600
export JARVIS_WORK_ORDER_RECENT_N=9
export JARVIS_WORK_ORDER_MAX_ITEMS=9
export JARVIS_WORK_ORDER_DEFAULT_URGENCY=high

# --- the arc under test ----------------------------------------------------
export JARVIS_TRAJECTORY_RECORDER_ENABLED=true
export JARVIS_SIBLING_ENTROPY_ENABLED=true
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

# --- graceful end so the autotrain trigger fires (idle_timeout|wall_clock_cap)
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 1500 --max-wall-seconds 5400 -v
