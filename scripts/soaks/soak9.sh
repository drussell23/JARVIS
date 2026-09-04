#!/usr/bin/env bash
# Sanctioned diversity harvest — soak 9.
#
# Soak 8 (bt-2026-09-01-230027) cleared the cage (0 caged roadmap ops) and
# the WAL backlog (8 replays, not 252), and STILL never ran a roadmap op:
# 0 of 15 worker pickups in 25 min. Mechanism, from its debug.log:
#   * the throughput governor stood 3 of 6 workers down ("local throughput
#     drives 3 lane(s) inside the route window");
#   * every op is route=standard priority=3 (roadmap is not an
#     _IMMEDIATE_SOURCE, so urgency cannot lift it), so the pool is FIFO;
#   * the 16-slot queue was full when the work orders arrived, so they went
#     BEHIND the ambient burst, and 88 capacity-full parkings kept
#     re-queuing older sensor rows ahead of them via the WAL.
#
# Every lever below is an existing, documented knob. No code changed.
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1

# --- room: nothing parks, nothing replays, FIFO simply drains -------------
export JARVIS_THROUGHPUT_GOVERNOR_ENABLED=false   # keep all 6 workers
export JARVIS_BG_POOL_SIZE=6
export JARVIS_BG_QUEUE_SIZE=64                    # ambient burst + batch fit; no WAL churn

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
# Soak 8's second scan re-emitted 7 older docs-only orders because, once the
# batch was marked seen, the RECENT_N window slid back onto them.
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
  --headless --cost-cap 0.50 --idle-timeout 1200 --max-wall-seconds 4200 -v
