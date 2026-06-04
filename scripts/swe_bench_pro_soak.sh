#!/usr/bin/env bash
# =============================================================================
# SWE-Bench-Pro soak launcher — composes scripts/ouroboros_battle_test.py.
#
# This script does NOT reimplement the cost/wall/idle/headless caps; it
# PASSES them to the existing battle-test harness. It only sets the
# swe_bench_pro env bundle around that harness. No new modules, no
# duplicated logic, no hardcoded instance ids or absolute paths.
#
# Usage:
#   bash scripts/swe_bench_pro_soak.sh phase1
#       Wiring dry-run on the checked-in fixture (~$0.01-0.10).
#
#   SWEBP_INSTANCE_IDS="id1,id2" \
#     bash scripts/swe_bench_pro_soak.sh phase3 --confirm-spend
#       Real soak. REFUSES to run without BOTH SWEBP_INSTANCE_IDS and
#       --confirm-spend (anti-accidental-spend). $2.00 cap, one session.
#
# See docs/operations/swe_bench_pro_soak_runbook.md for the full plan
# and the pre-soak operator checklist.
# =============================================================================
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

# ---- Phase 0 rails (MANDATORY; composed via battle-test flags) -------------
# Slice 63 — COST_CAP is operator-overridable: DW-down forces the costly
# Claude-only lane (~$0.50/op), so a 106-file instance needs more headroom than
# the $2 default (e.g. COST_CAP=8 bash scripts/swe_bench_pro_soak.sh phase3 ...).
COST_CAP="${COST_CAP:-2.00}"   # operator-bound USD ceiling (env-overridable)
MAX_WALL="${MAX_WALL:-2400}"   # hard wall-clock seconds (env-overridable for
                               # multi-instance sweeps; retry-storm-proof)
IDLE_TIMEOUT="${IDLE_TIMEOUT:-1800}"  # per-op liveness seconds (env-overridable)
# Slice 81 — temporal CALIBRATION for the Slice 79/80 adaptive budgets.
# An op's adaptive GENERATE budget can reach MAX_WALL × ADAPTIVE_WALL_FRACTION;
# the per-op staleness threshold MUST exceed that, else a long-but-active op is
# false-killed (`stale_ops_detected`, the sweep-#4 failure). We cap the per-op
# budget fraction AND set the stale threshold above the resulting ceiling.
export JARVIS_ADAPTIVE_GEN_WALL_FRACTION="${JARVIS_ADAPTIVE_GEN_WALL_FRACTION:-0.40}"
# stale threshold = ceil(MAX_WALL × fraction) + 300s grace, floored at 1200s.
_PEROP_CEIL=$(python3 -c "import math,os; print(int(math.ceil(${MAX_WALL}*float(os.environ['JARVIS_ADAPTIVE_GEN_WALL_FRACTION']))+300))" 2>/dev/null || echo 2700)
export OUROBOROS_OP_STALE_THRESHOLD_S="${OUROBOROS_OP_STALE_THRESHOLD_S:-$_PEROP_CEIL}"
# Slice 81 — raise Claude's base output budget so large single-file rewrites
# (~1800 lines) complete in one round instead of truncating at 16384.
export JARVIS_CLAUDE_MAX_OUTPUT_TOKENS="${JARVIS_CLAUDE_MAX_OUTPUT_TOKENS:-32768}"
# Slice 82 — attest DW's strong agentic coders as TRUSTED so the cheap DW
# fleet carries GENERATE instead of Claude (PRD §50 spend audit: Claude was
# ~99.9% of spend). These are live on the account + Claude-class on SWE-bench
# Pro (GLM-5.1 58.4%, Kimi-K2.6 58.6%, DeepSeek-V4-Pro 80.6% Verified) at
# 5-25× lower cost. Selection stays dynamic (catalog → gate → rank → ledger);
# this only *admits* them. Operator-overridable.
export JARVIS_DW_TRUSTED_MODELS="${JARVIS_DW_TRUSTED_MODELS:-Qwen/Qwen3.5-397B-A17B-FP8,Qwen/Qwen3.5-35B-A3B-FP8,deepseek-ai/DeepSeek-V4-Pro,zai-org/GLM-5.1-FP8,moonshotai/Kimi-K2.6,deepseek-ai/DeepSeek-V4-Flash}"
# Restricted-env: sandbox blocks .git/config under the repo root, so the
# benchmark repo cache + worktrees live under TMPDIR (NOT the repo).
SWEBP_CACHE="${TMPDIR:-/tmp}/swebp_cache"
SWEBP_WT="${TMPDIR:-/tmp}/swebp_wt"
mkdir -p "$SWEBP_CACHE" "$SWEBP_WT"

PHASE="${1:-}"; shift || true
CONFIRM_SPEND="no"
for a in "$@"; do [ "$a" = "--confirm-spend" ] && CONFIRM_SPEND="yes"; done

# Common swe_bench_pro env (default-FALSE everywhere EXCEPT what each
# phase explicitly sets below). Documented per flag.
export JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH="$SWEBP_CACHE"     # benchmark clones (TMPDIR)
export JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH="$SWEBP_WT"     # per-problem worktrees (TMPDIR)
# Slice 61 — the closed-loop autoscore evaluator subscribes to the
# operation_terminal SSE to wake on each solve op's terminal. That publish
# is gated by JARVIS_OP_LIFECYCLE_SSE_ENABLED (§33.1 default-FALSE). Without
# it the eval can only fall back to the slow post-timeout ledger query, so a
# bounded soak times out before scoring. Enable it for the fast (+seconds)
# wake. (The operation_ledger fallback, wired in harness.py, remains the
# correctness backstop if this is ever off.)
export JARVIS_OP_LIFECYCLE_SSE_ENABLED=true
# Slice 61 — result persistence is also §33.1 default-FALSE: EvaluationResultStore
# .record() updates the in-memory cache but only appends the durable
# results.jsonl row when this is ON. Without it EVERY scored result (fixture
# AND real phase3 benchmark) is lost on process exit — no verdict artifact.
# This is the durable-row gate the report_card reads.
export JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED=true
# Slice 63 — benchmark isolation: suppress ALL autonomous sensors so the
# injected benchmark instances own 100% of the execution + token budget.
# The bt-2026-06-02-074655 soak burned its budget on OpportunityMiner ('torch')
# + GitHubIssue ('#65637') noise ops before either swe_bench instance could
# GENERATE. Injected ops are sensor-independent, so this never blocks the
# benchmark. (Budget Safe-Halt already exists — candidate_generator's
# SessionBudgetPreflightRefused — so isolation + an adequate COST_CAP is the fix.)
export JARVIS_BENCHMARK_ISOLATION_MODE=true

_battle() {
  # Single composition point. The harness owns the caps.
  echo ">>> python3 scripts/ouroboros_battle_test.py \
--cost-cap $COST_CAP --max-wall-seconds $MAX_WALL \
--idle-timeout $IDLE_TIMEOUT --headless -v"
  python3 scripts/ouroboros_battle_test.py \
    --cost-cap "$COST_CAP" --max-wall-seconds "$MAX_WALL" \
    --idle-timeout "$IDLE_TIMEOUT" --headless -v
}

case "$PHASE" in
  phase1)
    # Wiring-validation: checked-in fixture, trivially-passing test_patch.
    export JARVIS_SWE_BENCH_PRO_ENABLED=true                    # Phase A master (session-only)
    export JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=true     # boot auto-inject (session-only)
    export JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH="tests/fixtures/swe_bench_pro/problems.jsonl"
    export JARVIS_SWE_BENCH_PRO_INJECT_COUNT=1                  # first-1 from the fixture
    # Slice 61 — phase1 is the FULL closed-loop wiring proof: the solve op is
    # scored against its gold test_patch (Phase C) and recorded (Phase D) so
    # the run writes the pristine results.jsonl validation row. Composes the
    # existing parallel_evaluate rig; no new code. (Pre-Slice-61 phase1 ran the
    # open-loop ingest only, which never wrote a row.) The fixture is trivially-
    # passing so cost stays ~$0.01-0.10.
    export JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED=true          # closed loop (score + record)
    echo "=== Phase 1 — closed-loop wiring proof (~\$0.01-0.10) ==="
    echo "fixture : $JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH"
    echo "cache   : $SWEBP_CACHE"
    echo "worktree: $SWEBP_WT"
    echo "caps    : cost=$COST_CAP wall=$MAX_WALL idle=$IDLE_TIMEOUT headless"
    echo "loop    : autoscore=ON sse=ON (fast terminal wake + ledger backstop)"
    _battle
    echo
    echo "=== Phase 1 proof (verdict source — NOT stdout) ==="
    echo "  jq -c 'select(.envelope.source==\"swe_bench_pro\")' .jarvis/swe_bench_pro/results.jsonl 2>/dev/null | tail -3"
    echo "  grep -i swe_bench_pro .ouroboros/sessions/*/debug.log | tail -5"
    echo "STOP. Report results. Do NOT run phase3 without operator 'Phase 3 go' + ids."
    ;;

  phase3)
    if [ "${SWEBP_INSTANCE_IDS:-}" = "" ] || [ "$CONFIRM_SPEND" != "yes" ]; then
      echo "REFUSING phase3: requires BOTH" >&2
      echo "  SWEBP_INSTANCE_IDS=\"id1,id2,...\"   (operator-selected; none hardcoded)" >&2
      echo "  --confirm-spend                      (explicit spend acknowledgement)" >&2
      echo "This is the anti-accidental-spend gate. See the runbook." >&2
      exit 2
    fi
    export JARVIS_SWE_BENCH_PRO_ENABLED=true                    # Phase A master (session-only)
    export JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=true     # Path B: full autonomous loop
    export JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED=true          # closed loop (score + record)
    export JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS="$SWEBP_INSTANCE_IDS"  # operator-chosen
    export JARVIS_SWE_BENCH_PRO_SCORE_REJECT_TEST_MODS=true     # cheat-detection ON (rubric integrity)
    # Slice 65 — score inside each problem's prepared Docker image (full repo
    # env). REQUIRES a running Docker daemon + the image pullable
    # (jefzda/sweap-images:<dockerhub_tag>); on Apple Silicon the engine injects
    # --platform linux/amd64 automatically. Without this, scoring runs in the
    # bare local python env and real repos (PyQt/Node/etc.) can't execute their
    # tests → unresolved. Opt-out: SWEBP_CONTAINER_EVAL=off.
    export JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED="${SWEBP_CONTAINER_EVAL:-true}"
    # Path A (cheaper, rubric-only) opt-in; Path B is default.
    if [ "${SWEBP_PATH:-B}" = "A" ]; then
      export JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=false
      export JARVIS_SWE_BENCH_PRO_PARALLEL_CONCURRENCY="${SWEBP_CONCURRENCY:-2}"
      echo "(Path A: parallel_evaluate rubric-only, concurrency=${SWEBP_CONCURRENCY:-2})"
    fi
    # R2 rubric soak profile — serial / high-urgency / sensor-throttled
    # / adaptive (see project_rubric_soak_profile memory). Compose, do
    # not reinvent; these are the documented controlled-soak knobs.
    export OUROBOROS_BATTLE_HEADLESS=1
    echo "=== Phase 3 — soak (\$$COST_CAP cap, one session, no re-spend) ==="
    echo "ids   : $SWEBP_INSTANCE_IDS"
    echo "path  : ${SWEBP_PATH:-B}  (B = harness-inject full GENERATE->APPLY->VERIFY)"
    echo "rubric: SCORE_REJECT_TEST_MODS=true"
    [ -n "${JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH:-}" ] && \
      echo "WARNING: JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH still set — unset it for the HF source (checklist #4)" >&2
    _battle
    echo
    echo "=== Phase 4 verdict (run after the session ends) ==="
    echo "  python3 -c \"from backend.core.ouroboros.governance.swe_bench_pro.report_card import build_report_card, render_markdown; from backend.core.ouroboros.governance.swe_bench_pro.result_store import get_default_store; print(render_markdown(build_report_card(get_default_store())))\""
    echo "  + cross-check .ouroboros/sessions/*/debug.log (NOT summary.json alone)"
    echo "EXHAUSTION/timeout => INCONCLUSIVE, never FAIL. No capability claims."
    ;;

  *)
    echo "usage: bash scripts/swe_bench_pro_soak.sh {phase1|phase3 --confirm-spend}" >&2
    echo "phase2 (selection) and phase4/5 (verdict/graduation) are no-spend," >&2
    echo "operator+assistant steps — see docs/operations/swe_bench_pro_soak_runbook.md" >&2
    exit 1
    ;;
esac
