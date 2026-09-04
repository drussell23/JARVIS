#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 20: CUMULATIVE BASIS.
#
# Soak 19 passed Gate 3: 85 rows / 49 trainable / 29 prompts / 21 of 21
# groups pairable, and `grpo_preflight --min-spread 0.01` cleared FOUR
# groups under the context-aware reward (best spread 0.020558) against
# two with the reward off. Breadth crossed the line; the reward doubled
# the yield. Both were needed.
#
# What Gate 3 does NOT yet give is a basis worth a GPU-hour. Of 29
# prompts, 16 are singletons and 9 of the remaining 13 groups are flat,
# so the trainable basis is 4 groups. GRPO learns from within-group
# contrast; four contrasts is a demonstration, not a training set.
#
# So soak 20 changes NOTHING and simply runs again. The env is soak 19
# byte-for-byte and the 41-task batch is already staged in progress.md
# (untouched since 00:21). Because promotion is off, ops run in
# quarantine and the operator tree is never mutated -- the same tasks
# produce the same prompts, so a second pass DEEPENS the existing groups
# rather than adding new singletons.
#
# The corpus is NOT archived first. Every row now carries a session id
# (`bt-...`) and `draw_kind`, so soak 19's and soak 20's rows are
# separable after the fact while `iter_trajectory_rows` reads them as one
# accumulating body. Archiving here would throw away the very basis this
# run exists to enlarge.
#
# The question:
#   how many groups clear --min-spread 0.01 across TWO clean broad soaks?
#
# Proof lines, in order:
#   corpus row count strictly exceeds 85              appended, not replaced
#   two distinct session_ids in the corpus            soak 19 preserved
#   harvest groups_pairable / rows_repair             purification live
#   stop_reason=idle_timeout|wall_clock_cap           graceful, full length
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
