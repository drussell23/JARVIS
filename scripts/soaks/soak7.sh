#!/usr/bin/env bash
# Sanctioned diversity harvest — soak 7.
#
# Soak 6 (bt-2026-09-01-213353) generated NOTHING: the DocStaleness sensor
# filled the 16-slot BackgroundAgentPool with in-cage targets
# (backend/core/ouroboros/**) that the self-modification gate correctly
# refused, the six roadmap work orders were parked behind them
# (pool_capacity_full) and never dequeued, and the idle watchdog — which is
# poked only by PROGRESSING ops — fired at 900s.
#
# Every lever below is an existing, documented knob. No code is changed to
# tune the run, and no signature is minted: the targets are ordinary files
# outside every cage sentinel, so they need no authorization at all.
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1

# --- Phase 2: queue hygiene for the harvest ---------------------------------
# Quiet the one sensor that saturated the pool with caged work. Its master
# gates BOTH scan paths (poll and fs.changed.*). Temporary, for this run.
export JARVIS_DOC_STALENESS_ENABLED=false
# WorkOrderSensor: poll early and often (default 900s meant the first scan
# landed ~6 min after boot), read exactly the staged batch (9 orders), and
# rank it ahead of ambient sensor noise.
export JARVIS_WORK_ORDER_SENSOR_ENABLED=true
export JARVIS_WORK_ORDER_INTERVAL_S=120
export JARVIS_WORK_ORDER_RECENT_N=9
export JARVIS_WORK_ORDER_DEFAULT_URGENCY=high

# --- The arc under test -----------------------------------------------------
export JARVIS_TRAJECTORY_RECORDER_ENABLED=true
export JARVIS_SIBLING_ENTROPY_ENABLED=true
export JARVIS_SIBLING_DIVERSITY_THRESHOLD=0.95
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

# --- Phase 3: give dispatch room ---------------------------------------------
# idle-timeout is reset only by ops in flight; 1800s covers the poll +
# dispatch of nine orders plus VALIDATE per sibling. The wall cap is the
# static safety margin the watchdog invariant requires (never activity-gated).
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 1800 --max-wall-seconds 3600 -v
