#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 22: sibling FULFILLMENT, exercised.
#
# Soak 19 recorded 36 ops with a primary and 15 with no sibling row --
# against SIBLING_CANDIDATES=3. Every one of the 15 was accounted for by
# joining the corpus to the log (jarvis 95c1031e51):
#
#   8  sibling came back 2b.1-noop -> read as EMPTY -> _stop -> every
#      remaining slot forfeited.
#   6  redundant after the ONE re-draw, at similarity 1.0000 in 14 of 15
#      drops: the re-draw stepped one rung up INSIDE the collapsed region.
#   1  legacy-cascade path that never reaches the loop (not fixed).
#   2  (session-wide) all_candidates_syntax_error -> exception -> _stop.
#
# Soak 21 (unmodified main, the CONTROL) reproduced it at 56 min:
# 12 primaries / 9 paired / 3 singletons (2 noop-empty, 1 syntax).
#
# The fix: a no-op and an unparseable sibling are ANSWERS, not a dead
# lane. Both re-draw at higher entropy within the slot and then drop the
# SLOT, never the loop. A collapse streak feeds an escalation multiplier
# that jumps rungs exponentially instead of stepping inside an exhausted
# region. And `sibling_fulfillment` logs a per-slot ledger for EVERY op.
#
# Env is soak 21's byte-for-byte plus ONE knob:
#   JARVIS_SIBLING_ESCALATION_MULTIPLIER=2.0   (default 1.0 = off)
# The noop/syntax "answer" change is code, not a knob, so it is on
# unconditionally. Attribution is per-slot from the ledger: a slot that
# reads `noop>merged` was saved by the answer change; one that reads
# `redundant:1.0000>merged` on a streak>0 draw was saved by the jump.
#
# Question: does the singleton rate fall from 3/12 (soak 21) toward 0,
# and does trainable_groups rise above soak 19's 4?
#
# Proof lines, in order:
#   "revisit ON -- 48 seen hash(es) shadowed"    ledger bypass armed
#   "emitted 41 work order(s) from 1 source(s)"  the batch went out
#   "sibling_fulfillment op=... wanted=2 got="    the ledger is live
#   grep -c "SINGLETON" << soak 21's rate         the fix bit
#   stop_reason=idle_timeout|wall_clock_cap       graceful, full length
#
# PRECONDITION: main must carry 95c1031e51 (merge fix/sibling-fulfillment
# after soak 21 exits -- the main tree is executing main until then).
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1
if ! git merge-base --is-ancestor 95c1031e51 HEAD 2>/dev/null; then
  echo "REFUSING: HEAD does not contain 95c1031e51 (the fulfillment fix)" >&2
  exit 2
fi

export JARVIS_SIBLING_DIVERSITY_THRESHOLD=0.999
export JARVIS_PIPELINE_DEADLINE_AT_START=true

# --- the kill that ended soak 11: loop stalls on DrvFS stat sweeps ------
export JARVIS_EXTERNAL_WATCHDOG_STALE_S=600
export JARVIS_EXTERNAL_WATCHDOG_MARGIN_S=120

# --- VALIDATE gets a real slice ------------------------------------------
export JARVIS_VALIDATION_RESERVE_ENABLED=true
export JARVIS_VALIDATION_RESERVE_COLD_S=240
export JARVIS_VALIDATION_RESERVE_SAFETY=1.5
export JARVIS_VALIDATION_RESERVE_MAX_FRACTION=0.5
export JARVIS_TEST_TIMEOUT_S=180

# --- budgets: room for 3 draws (~900 s) + the reserve + retries ------------
export JARVIS_PIPELINE_TIMEOUT_S=4968
export JARVIS_GEN_TIMEOUT_STANDARD_S=3726
export JARVIS_GENERATION_TIMEOUT_S=3726
export JARVIS_BG_WORKER_OP_TIMEOUT_S=4968
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
export JARVIS_WORK_ORDER_INTERVAL_S=86400
export JARVIS_WORK_ORDER_RECENT_N=41
export JARVIS_WORK_ORDER_MAX_ITEMS=41
export JARVIS_WORK_ORDER_DEFAULT_URGENCY=high

# --- revisit a seen roadmap, keep the ledger (5dc973d6c7) -----------------
export JARVIS_ALLOW_ROADMAP_REVISIT=true

# --- the arc under test ----------------------------------------------------
export JARVIS_TRAJECTORY_RECORDER_ENABLED=true
export JARVIS_SIBLING_ENTROPY_ENABLED=true
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

# --- the fulfillment knob under test (95c1031e51) --------------------------
export JARVIS_SIBLING_ESCALATION_MULTIPLIER=2.0

# --- graceful end so the autotrain trigger fires (idle_timeout|wall_clock_cap)
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 1800 --max-wall-seconds 9000 -v
