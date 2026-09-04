#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 15.
#
# Three code changes since soak 14, all default-on, no knob turns them on:
#
#  1. REWARD GEOMETRY (reactor 540c024). Passing code spanned [0.60, 0.70];
#     it now spans [passing_floor, authority - margin] = [0.60, 0.95], 3.5x
#     wider, with the ceiling DERIVED from the authority band so a
#     test-executed verdict still strictly outranks a merely-pretty one.
#     Retro-measured on the existing corpora: soak 13's best group
#     0.0052 -> 0.0158 (clears 0.01), soak 14's 0.0019 -> 0.0059.
#  2. ENTROPY-AWARE BUDGETS. A high-entropy draw produces MORE tokens, so
#     the sampling point now scales the expected OUTPUT inside the existing
#     adaptive timeout rather than multiplying the timeout by a constant.
#     Soak 14 died of exactly this: 89 TimeoutError and session_exhausted
#     25 min early, because the draws got genuinely longer once the real
#     sampling point started reaching the engine.
#  3. session_id ON EVERY ROW. `OperationContext` never had the field the
#     recorder asked for, so every corpus row was anonymous and soaks could
#     only be told apart by clustering timestamps. Partitioning is now
#     deterministic -- which is what makes THIS soak comparable to 13/14.
#
# The OUTER budgets below are the ONLY env change from soak 14, and they are
# derived, not guessed: the worst-case ladder rung now costs 2.07x the
# legacy draw, so a pipeline sized for temperature-0.2 draws would simply
# move the starvation from the local timeout to the pipeline deadline. Each
# scaled value is soak 14's number times that same factor.
#
# Watch for, in order:
#   "[LocalPrimeClient] per-draw sampling applied:"   knob reached the engine
#   "structurally distinct"                            siblings really differ
#   stop_reason=idle_timeout|wall_clock_cap            NOT session_exhausted
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
  --headless --cost-cap 0.50 --idle-timeout 1800 --max-wall-seconds 9000 -v
