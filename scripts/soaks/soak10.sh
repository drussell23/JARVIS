#!/usr/bin/env bash
# Sanctioned diversity harvest — soak 10 = soak 9 recipe + op budgets sized
# for THREE local draws.
#
# Soak 9 (bt-2026-09-01-232929) finally ran roadmap ops on the local lane
# and the first one produced a candidate — then:
#   "sibling 2/3 skipped: 307.7s budget left, previous sibling cost 292.0s"
# A first draw on the 30B costs ~290 s here (10k-token prompt, 3 tool
# rounds, 0.4-2.7 tok/s), and the defaults — pipeline 600 s, STANDARD
# generation 220 s, background-worker op 360 s — leave no room for a second
# draw, let alone a third. Siblings are drawn ONLY from budget slack, by
# design, so the budget has to carry three draws or the harvest is one row
# per op and nothing can pair.
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1

# --- budgets: room for 3 draws (~900 s) + VALIDATE ------------------------
export JARVIS_PIPELINE_TIMEOUT_S=2400
export JARVIS_GEN_TIMEOUT_STANDARD_S=1800
export JARVIS_GENERATION_TIMEOUT_S=1800
export JARVIS_BG_WORKER_OP_TIMEOUT_S=2400
export JARVIS_LOCAL_SIBLING_BUDGET_MARGIN=1.0      # floor: pay exactly what the last draw cost

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
export JARVIS_SIBLING_DIVERSITY_THRESHOLD=0.95
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

# --- graceful end so the autotrain trigger fires (idle_timeout|wall_clock_cap)
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 1500 --max-wall-seconds 5400 -v
