#!/usr/bin/env bash
# Sanctioned diversity harvest — soak 11 = soak 10 + an acceptance
# threshold calibrated for EDIT-shaped candidates.
#
# Soak 10 (bt-2026-09-01-235803) ran the whole chain live: 3 draws per op,
# the ladder in the request, the structural filter deciding. Its verdicts:
#   4 x similarity 1.0000  (exact duplicates after docstring strip -> reject, correct)
#   1 x 0.9965, 1 x 0.9714 (REAL AST deltas, rejected at threshold 0.95)
# Candidates are whole-file rewrites of 80-150 line modules, so any two
# valid implementations of one small edit share 95-99% of the tree — the
# 0.95 threshold was calibrated on docstring-only twins (0.9987) and on
# from-scratch modules (0.56-0.91), not on this shape. At 0.95 the harvest
# rejects every genuinely different edit and yields one row per op.
#
# 0.999 keeps the invariant that matters — an exact duplicate is still one
# answer — and lets any draw with a real structural delta reach the corpus,
# where the reward (grpo_preflight --min-spread) decides trainability. The
# proper fix is to measure similarity over the CHANGED hunks against the
# on-disk baseline, not the whole file; that is a code change, not a knob.
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1

export JARVIS_SIBLING_DIVERSITY_THRESHOLD=0.999

# --- budgets: room for 3 draws (~900 s) + VALIDATE ------------------------
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
