#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 26: every self-evidencing draw survives.
#
# Soak 25 was the first run with every recorder fix that existed at its
# launch. Thirty-two minutes in, it had produced the pair this whole arc
# exists for -- a patch and a parse_error/noop sibling on ONE op -- three
# times, and the trainable ledger read ZERO mixed ops, because the
# contrasting half was dropped `not_train`:
#
#     op-...7084  patch/primary + patch/sibling + parse_error/sibling
#     op-...7081  patch/primary + noop/sibling  + patch/sibling
#     op-...7096  patch/primary + parse_error/sibling x2
#
# fbf227d4ca / 525ba81739 made a REFUSAL self-evidencing but keyed on
# `is_noop`; a parse_error row is not a noop, so it fell through to
# _UNKNOWN. 93cce42284 generalises the rule: `_self_evidencing_policy(gen)`
# returns _NOOP for a refusal and _FAILURE for an all-parse_error draw --
# the parser IS the verdict, established deterministically at generation
# time, and nothing downstream could have rescued code that does not parse.
# A PATCH stays excluded: its correctness is unknown until VALIDATE.
#
# Soak 25 could not use that fix -- Python loads a module once per process,
# and 93cce42284 landed 33 minutes after its launch. Its own contribution
# at cut time: 28 trainable rows / 19 prompts / 1 trainable group, ceiling
# = patch-vs-patch spread, mostly flat. Its refusals and parse errors
# survived the recorder for the first time (75% survival vs 25%) and then
# formed nothing, because nothing could pair with them.
#
# Required commits, all by sha (the guard IS the fix for "ran stale code"):
#   4c8f4d3af9  refusals become rows
#   06c0d51705  unparseable draws become rows
#   fbf227d4ca  refusal needs no external verdict -- joined path
#   525ba81739  ...and the EXPIRY path
#   93cce42284  parse_error is self-evidencing -> _FAILURE
# plus reactor 8f36af0 (re-drawn refusal admitted as genuine; retroactive).
#
# Env is soak 25's byte-for-byte. NOTHING else moves. The question:
#   do mixed-shape ops now SURVIVE, and how many groups clear 0.01?
# Baseline entering: 17 cumulative. Target >= 25, then the untrained
# baseline development test, then GRPO.
#
# Proof lines:
#   "emitted 41 work order(s)"                      the batch went out
#   survival ~100% at 30 rows                        both write paths fixed
#   ops with patch AND refusal/parse_error SURVIVING > 0   THE PAIR EXISTS
#   stop_reason=idle_timeout|wall_clock_cap          graceful, full length
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
_require_commit 93cce42284 "parse_error is self-evidencing (_FAILURE) — the parser IS the verdict"

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
