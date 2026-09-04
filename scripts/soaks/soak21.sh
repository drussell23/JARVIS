#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 21: the ledger bypass, exercised.
#
# Soak 20 was a NULL RUN. It ran its full 9135s and added ZERO trainable
# rows, because WorkOrderSensor emitted 0 work orders: the cross-session
# seen-hash ledger (.jarvis/work_order_seen.json, 48 hashes written by soak
# 19 itself) suppressed all 41 staged tasks. Its premise -- "the same tasks
# reproduce the same prompts, so a second pass DEEPENS soak 19's groups" --
# was impossible by construction, and for TWO independent reasons:
#
#   1. The ledger exists precisely to stop a stable roadmap re-emitting.
#      Fixed properly in 5dc973d6c7: JARVIS_ALLOW_ROADMAP_REVISIT shadows
#      the ledger IN MEMORY, so the hashes stop suppressing without the
#      operator's file being deleted or rewritten. Measured live against
#      the real ledger: flag OFF emits 0, flag ON emits 41, and the file is
#      byte-identical afterwards.
#
#   2. GROUPS CANNOT BE DEEPENED ACROSS SOAKS AT ALL. grpo_preflight groups
#      by the exact `user_input` string, and every prompt embeds its own
#      Op-ID ("Op-ID: op-01a06629-..." in the ## Task section). Measured on
#      soak 19: 62 distinct prompts, 85 rows, and prompts shared by more
#      than one op_id = 0. A group is one op's primary + its siblings,
#      never two ops on the same task. New soak -> new Op-IDs -> new
#      prompts -> necessarily new groups.
#
# So this run buys BREADTH (more trainable groups), not depth on soak 19's.
# That is still the thing the basis needs: 4 trainable groups is a
# demonstration, not a training set.
#
# Env is soak 20's byte-for-byte with exactly two deliberate changes:
#
#   JARVIS_ALLOW_ROADMAP_REVISIT=true   the fix under test
#   JARVIS_WORK_ORDER_INTERVAL_S 3600 -> 86400
#
# The interval change is not cosmetic. Revisit drains the shadow over
# successive polls (measured 41 -> 7 -> 0), and those 7 are the OLD
# docs-only batch -- cosmetic whole-file rewrites that produce near-zero
# within-group difference and would pollute the corpus with flat groups.
# One boot poll emits exactly the 41-task design-freedom batch and nothing
# else, which is also precisely what soak 19 emitted.
#
# The corpus is NOT archived: soak 19's 85 rows stay, every row carries a
# session id and draw_kind, so 19 / 20 / 21 remain separable while
# iter_trajectory_rows reads them as one accumulating body.
#
# Baseline to beat: 49 trainable rows / 29 prompts / 4 trainable groups
# (reward ON, best spread 0.020558) / 2 groups (reward OFF).
#
# Proof lines, in order:
#   "revisit ON -- 48 seen hash(es) shadowed"   the flag armed at init
#   "emitted 41 work order(s) from 1 source(s)" the batch actually went out
#   corpus rows strictly exceed 107                appended, not replaced
#   three distinct session_ids in the corpus       19 and 20 preserved
#   stop_reason=idle_timeout|wall_clock_cap        graceful, full length
#
# CHECK THE SECOND PROOF LINE AT ~T+6min. Absent = the run is already
# worthless and should be killed rather than left to burn 2.5h, which is
# exactly the check soak 20 did not get.
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
export JARVIS_WORK_ORDER_INTERVAL_S=86400
export JARVIS_WORK_ORDER_RECENT_N=41
export JARVIS_WORK_ORDER_MAX_ITEMS=41
export JARVIS_WORK_ORDER_DEFAULT_URGENCY=high

# --- the fix under test: revisit a seen roadmap, keep the ledger ----------
export JARVIS_ALLOW_ROADMAP_REVISIT=true

# --- the arc under test ----------------------------------------------------
export JARVIS_TRAJECTORY_RECORDER_ENABLED=true
export JARVIS_SIBLING_ENTROPY_ENABLED=true
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

# --- graceful end so the autotrain trigger fires (idle_timeout|wall_clock_cap)
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 1800 --max-wall-seconds 9000 -v
