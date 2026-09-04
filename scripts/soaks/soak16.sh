#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 16: the CLEAN BASELINE.
#
# Env is soak 15's byte-for-byte. Everything that changed is code, commit
# 383faafe73 (jarvis), and all of it is default-on:
#
#  1. The local seat gets its own kill line. Soak 15 gave the local 30B
#     30s of a 9,882s budget and reserved the rest for a Claude call that
#     could never be made (no credit, package not installed). The free lane
#     now counts as a structurally dead fallback and the local primary is
#     bounded by JARVIS_LOCAL_INFERENCE_ABSOLUTE_CEILING_MS (1200s), not the
#     DW reflex cap.
#  2. The streaming watchdog reads the draw: first-token deadline scales
#     with prompt size (32K -> 4x) and honours the stall penalty the ledger
#     already recorded; steady-state ceiling scales with the draw's entropy.
#  3. race_or_wait_for consumes the losing task's exception (no more
#     "never retrieved" PANICs at shutdown).
#  4. anthropic installed in the ov venv -- the Tier-1 seam is well-formed;
#     the budget preflight still (correctly) refuses it.
#
# This run exists to answer ONE question with a graceful exit:
#   does the widened reward geometry clear --min-spread 0.01 on a
#   full-length, unstarved sample?
#
# Proof lines, in order:
#   "Tier3_cap_active: primary_budget=30.0s"   MUST BE ABSENT for the local seat
#   "autarky"                                   structural, on every local primary
#   "inter-token watchdog="                     values other than 30s
#   "per-draw sampling applied:"                knob reached the engine
#   "structurally distinct"                     siblings really differ
#   stop_reason=idle_timeout|wall_clock_cap     NOT session_exhausted
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
