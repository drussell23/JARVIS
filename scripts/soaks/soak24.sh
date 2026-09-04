#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 24: WORK-ORDER SUPPLY, unstarved.
#
# Soak 23 spent its first hour on AMBIENT ops and produced only flat,
# refusal-only groups. Measured cause: the env slows the TestWatcher POLL
# (JARVIS_INTENT_TEST_INTERVAL_S=86400) but leaves BOOT HYDRATION on, which
# front-loads the queue from existing test failures before the 41 work
# orders are ever emitted. All 8 of those boot ops were also submitted
# TWICE at the A1Trace->GLS seam (2 bgops per op_id), while all 45 -cau
# ops were submitted once -- so disabling boot hydration removes the only
# population exhibiting that duplicate-submission defect as well.
#
# NOT a priority rewrite: work orders already carry urgency=high and
# source=roadmap, and JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED is on. The
# problem was arrival ORDER at boot, not rank.
#
# One variable moves against soak 23:
#   JARVIS_TESTWATCHER_BOOT_HYDRATION_ENABLED=false
#
# Soak 19 lost 8 of its 15 singleton ops to a refusal, and soak 22 logged
# one op that declined at FOUR distinct sampling points and left the
# corpus nothing. `record_generation` returned False whenever a result
# carried no candidates, and a `2b.1-noop` carries none by construction.
#
# A refusal is an ANSWER, and against a working patch it is the cleanest
# negative-preference pair the corpus can hold. Measured end to end
# (jarvis writes, reactor's UNMODIFIED pipeline reads):
#
#     refusal  -> Verdict(0.450, t1, no_source_by_shape)
#     patch    -> Verdict(0.809, t3, quality:...)
#     spread    = 0.3586        the trainable gate asks for 0.01
#     all-refusal group -> _is_flat True, safely excluded
#
# Reactor needed NO change: `extract_sources` already knows 2b.1-noop and
# `verify_static` scores it at the syntax ceiling -- full credit for a
# well-formed answer, zero for substance it never claimed. The fix was to
# store the decline ENVELOPE so the grader reads what it already handles.
#
# Env is soak 22's with ONE deliberate change:
#
#   JARVIS_SIBLING_ESCALATION_MULTIPLIER 2.0 -> 1.0 (baseline, OFF)
#
# Reverted on evidence, not taste. In soak 22 it engaged on the two ops
# whose slots collapsed consecutively and saved NEITHER: rung >=3 already
# sits at the 1.15 temperature ceiling, so past it the jump moves only the
# seed, and the model answered with another refusal and another
# unparseable draw rather than diversifying. Carrying a knob that changes
# nothing invites re-litigating it later. The noop/syntax "answer" change
# from 95c1031e51 stays on -- it is code, not a knob, and soak 22 proved
# it (op ...05a8: slot 3 `noop>merged`, a 3-row group that would have been
# 2 rows and a stopped loop).
#
# So exactly ONE variable moves against soak 22: refusals now become rows.
#
# The corpus is NOT archived: every row carries a session id, draw_kind and
# now candidate_status, so 19-23 stay separable while iter_trajectory_rows
# reads them as one accumulating body.
#
# Baseline to beat (cumulative 19+20+21): 91 rows / 50 prompts /
# 6 trainable groups reward-ON, 5 reward-OFF.
#
# HONEST EXPECTATION: this does NOT rescue an all-refusal op. Those score
# identically, stay flat, and are correctly excluded. The yield comes from
# ops holding at least one refusal AND one patch -- which is exactly the
# shape 95c1031e51 now produces, since a refused slot is re-drawn instead
# of stopping the loop. The two changes compound; neither alone is enough.
#
# Proof lines, in order:
#   "revisit ON -- 48 seen hash(es) shadowed"     ledger bypass armed
#   "emitted 41 work order(s) from 1 source(s)"   the batch went out
#   grep '"candidate_status":"noop"' the corpus   refusals now persist
#   trainable_groups > 6                          the yield expansion
#   stop_reason=idle_timeout|wall_clock_cap       graceful, full length
#
# PRECONDITION: main must carry 4c8f4d3af9 (merge feat/noop-ingestion
# after soak 22 exits -- the main tree is executing main until then).
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
