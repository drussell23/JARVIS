#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 14.
#
# ONE variable differs from soak 13: commit e4b2db1c7c threads the WHOLE
# sampling point (top_p, top_k, repeat_penalty, seed) through
# candidate_generator -> PrimeProvider -> LocalPrimeClient into the request
# body. Soak 13 sent temperature and nothing else, so redraws at T=1.10 with
# fresh seeds came back byte-identical (structural similarity 1.0000, one
# group of 8 draws on a single structure_id) and the corpus reached
# grpo_preflight with a reward spread of 6e-05.
#
# Every knob below is soak 13's, unchanged and deliberately so -- including
# the interim 0.999 acceptance threshold. Changing the threshold at the same
# time would make the resulting spread unattributable. The comparison:
#
#   soak 13: 31 rows / 20 trainable / 9 prompts / 5 groups, max spread 0.0052
#   soak 14: does any group clear --min-spread 0.01?
#
# Watch for "[LocalPrimeClient] per-draw sampling applied:" in debug.log --
# that line is the proof the knob reached the engine, and its absence means
# the fix is not live regardless of what the ladder logs.
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
