#!/usr/bin/env bash
# Sanctioned diversity harvest -- soak 17: the UNCORRUPTED baseline.
#
# Env is soak 16's byte-for-byte. Everything that changed is code and
# default-on. Soak 16 exited `stale_ops_detected` at 127 of 150 min with
# 4 trainable rows: two roots found live, both fixed since:
#
#  1. NATIVE TRANSPORT (jarvis c455e63c70). The director spoke
#     /v1/chat/completions, which DROPS the `options` block on ollama
#     0.33.2 -- same seed, different outputs; greedy top_k=1 still varied.
#     It now speaks /api/chat: the same seed reproduces byte-for-byte
#     through the real client, top_k/repeat_penalty/seed reach the sampler,
#     and the engine's own token counts are read, not estimated.
#  2. LEDGER PARTITION + OFF-LOOP READS + SEALING. Every soak since Aug 24
#     appended to `.jarvis/determinism/default/decisions.jsonl` (578 MB)
#     because the harness never stamped the env the ledger keyed on; three
#     sync readers then scanned it on the main loop per op (10-65 s stalls,
#     300 starvation events). The session id now resolves to the canonical
#     JARVIS_OUROBOROS_SESSION_ID; the readers run on the advisor-blast
#     executor; the writer seals the live file into numbered segments at
#     JARVIS_DETERMINISM_LEDGER_MAX_BYTES and every consumer reads across
#     segments.
#
# This run exists to answer ONE question with a graceful, full-length exit:
#   does the widened reward geometry clear --min-spread 0.01 on a sample
#   whose diversity the transport no longer caps?
#
# Proof lines, in order:
#   .jarvis/determinism/bt-<this session>/decisions.jsonl   EXISTS (not default/)
#   "stream watchdog armed: first=..."                     derived deadlines live
#   "per-draw sampling applied:"                           knob reached the body
#   "AUTARKY ENGAGED ... 1200.0s budget"                    no 30s reflex cap
#   ControlPlaneStarvation count                            ~0 (soak 16: 300)
#   "structurally distinct"                                 siblings really differ
#   stop_reason=idle_timeout|wall_clock_cap                 NOT stale_ops/session_exhausted
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
  --headless --cost-cap 0.50 --idle-timeout 1800 --max-wall-seconds 9000 -v
