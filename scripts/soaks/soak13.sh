#!/usr/bin/env bash
# Sanctioned diversity harvest — soak 13 = soak 12 recipe + two fixes that
# soak 12 measured its way to.
#
# 1. Budget starts when WORK starts (commit 11c49494c9, default ON): the
#    roadmap batch waited 16-20 min in FIFO behind the boot burst and reached
#    workers with most of its 2400 s already spent; VALIDATE got 30-46 s.
#    The pool now re-bases pipeline_deadline at pickup. No knob needed.
# 2. Acceptance threshold 0.999 (interim). Hunk-level similarity between two
#    DIFFERENT small edits is intrinsically 0.96-0.99 -- the changed
#    statement is often one large literal and the edit one token of it --
#    so 0.95 (even flow-scaled to 0.97) retracted 20 draws and paired one
#    group in soak 12, where soak 11 at 0.999 paired four. Exact duplicates
#    (1.0000) are still refused; anything with a real delta reaches the
#    corpus and grpo_preflight --min-spread decides. The principled fix is
#    a threshold scaled by hunk SIZE; it is a code change, not a knob.
# 3. Retract seam hash-collision fix (commit f2a8e0e1d5): an identical twin
#    no longer takes the accepted candidate with it.
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
