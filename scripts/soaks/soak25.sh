#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 25: the first FULLY-PATCHED, unstarved master run.
#
# Every earlier soak ran with at least one defect between generation and the
# trainer. This is the first with none of them:
#
#   4c8f4d3af9  refusals become rows            (soak 23 onward)
#   06c0d51705  unparseable draws become rows    (soak 23 onward)
#   8f36af0     reactor admits a re-drawn refusal as genuine   (retroactive)
#   fbf227d4ca  a refusal needs no external verdict -- joined path   (soak 24)
#   525ba81739  ...and on the EXPIRY path too    (NOT in soak 24 -- landed
#               after it launched; that is why soak 24's survival slid
#               100% -> 85% -> 67% as ops aged out unreported)
#
# Soak 24 measured the cost of that last gap precisely: every dropped row
# was `not_train`, none `not_genuine`. The reactor filter was already right.
#
# Two supply-side knobs, both about what reaches the queue FIRST:
#   JARVIS_TESTWATCHER_BOOT_HYDRATION_ENABLED=false   (soak 24 onward)
#   JARVIS_TEST_FAILURE_CACHE_FIRST_ENABLED=false     (NEW)
# Soak 24 still front-loaded 17 ambient ops from the pytest lastfailed cache
# (`cache_first_hydration_enabled`, default true, seeds up to 16). All 17 were
# correctly refused -- "test infrastructure, not code logic" -- and burned the
# first ~30 minutes. With both off, the 41 farming orders are the queue.
#
# THE GUARD IS THE ROOT-CAUSE FIX for "ran stale code". Python loads a module
# once per process; soak 24 was stale because its fix landed after launch.
# Hot-reloading the recorder mid-run would swap a module holding the live
# pending map and writer task. The correct guarantee is refusing to START
# unless every required commit is proven in the working tree -- and the
# handoff moves the tree to main first, because a ref advancing does not
# move the tree the soak executes.
#
# Question: with nothing between generation and the trainer, how many
# groups clear 0.01? Baseline entering: 15 (unchanged since soak 22,
# because the two filter defects swallowed everything after).
# Target: >= 25, then the untrained baseline development test, then GRPO.
#
# Proof lines, in order:
#   "revisit ON -- 48 seen hash(es) shadowed"    ledger bypass armed
#   "emitted 41 work order(s) from 1 source(s)"  the batch went out
#   first op is a -cau (farming) op, not a -sig  supply fix bit
#   survival rate ~100% at 30 rows               expiry fix bit
#   ops with patch AND refusal surviving > 0     the pair exists
#   stop_reason=idle_timeout|wall_clock_cap      graceful, full length
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1
# A soak that silently runs WITHOUT the code it exists to exercise produces
# a confident wrong answer. Both commits are required, by sha.
_require_commit() {
  if ! git merge-base --is-ancestor "$1" HEAD 2>/dev/null; then
    echo "REFUSING: HEAD does not contain $1 ($2)" >&2
    exit 2
  fi
}
_require_commit 4c8f4d3af9 "noop ingestion — refusals become rows"
_require_commit 06c0d51705 "parse-error capture — unparseable draws become rows"
_require_commit fbf227d4ca "verdict fallback — refusal self-evidencing (joined path)"
_require_commit 525ba81739 "verdict fallback on the EXPIRY path too"

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
export JARVIS_TESTWATCHER_BOOT_HYDRATION_ENABLED=false
export JARVIS_TEST_FAILURE_CACHE_FIRST_ENABLED=false
export JARVIS_TODO_SCAN_INTERVAL_S=86400
export JARVIS_EXPLORATION_INTERVAL_S=86400
export JARVIS_INTAKE_BACKLOG_SCAN_INTERVAL_S=86400

# --- the work: exactly the staged batch, emitted ONCE ---------------------
export JARVIS_WORK_ORDER_SENSOR_ENABLED=true
export JARVIS_WORK_ORDER_INTERVAL_S=86400
export JARVIS_WORK_ORDER_RECENT_N=41
export JARVIS_WORK_ORDER_MAX_ITEMS=41
export JARVIS_WORK_ORDER_DEFAULT_URGENCY=high

# --- revisit a seen roadmap, keep the ledger (5dc973d6c7) -----------------
export JARVIS_ALLOW_ROADMAP_REVISIT=true

# --- the arc under test ----------------------------------------------------
export JARVIS_TRAJECTORY_RECORDER_ENABLED=true
export JARVIS_SIBLING_ENTROPY_ENABLED=true
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

# --- escalation multiplier REVERTED to baseline (measured: saved nothing) ---
export JARVIS_SIBLING_ESCALATION_MULTIPLIER=1.0

# --- graceful end so the autotrain trigger fires (idle_timeout|wall_clock_cap)
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 1800 --max-wall-seconds 9000 -v
