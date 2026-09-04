#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 19: YIELD.
#
# Env is soak 18 byte-for-byte EXCEPT the work-order caps. Soak 18 proved
# the sample is finally honest (59 rows, draw_kind separated, 15 of 15
# groups pairable, zero duplicate hashes) and the reward refactor that
# followed clears Gate 3 on that very corpus (best group 0.00585 ->
# 0.013092 at --min-spread 0.01, reactor bff8568).
#
# What soak 18 could not give is BREADTH: only 9 authored farming tasks
# existed, so 125 attempted ops produced 6 trainable groups and exactly
# one cleared the floor. The log shows why the rest failed -- 427
# `2b.1-noop` responses against 112 real candidates: the model correctly
# DECLINES ambient sensor work that is already done. Declines are not a
# defect to suppress; they are the absence of real work to do.
#
# So the only change is more real work: the farming batch is expanded with
# defects read out of the tree, each admitting more than one correct
# implementation, and the caps are raised to let all of them through.
#
# The question:
#   how many groups clear --min-spread 0.01 when the corpus is both clean
#   AND broad, under the context-aware reward?
#
# Proof lines, in order:
#   work orders emitted == the staged batch size    caps really raised
#   corpus rows carry "draw_kind"                   discriminator live
#   harvest groups_pairable / rows_repair           purification live
#   stop_reason=idle_timeout|wall_clock_cap         graceful, full length
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1

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
export JARVIS_WORK_ORDER_INTERVAL_S=3600
export JARVIS_WORK_ORDER_RECENT_N=41
export JARVIS_WORK_ORDER_MAX_ITEMS=41
export JARVIS_WORK_ORDER_DEFAULT_URGENCY=high

# --- the arc under test ----------------------------------------------------
export JARVIS_TRAJECTORY_RECORDER_ENABLED=true
export JARVIS_SIBLING_ENTROPY_ENABLED=true
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

# --- graceful end so the autotrain trigger fires (idle_timeout|wall_clock_cap)
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 1800 --max-wall-seconds 9000 -v
