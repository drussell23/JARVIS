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
COST_CAP="2.00"          # operator-bound USD ceiling
MAX_WALL="2400"          # hard wall-clock seconds (retry-storm-proof)
IDLE_TIMEOUT="1800"      # per-op liveness seconds
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
    echo "=== Phase 1 — wiring dry-run (~\$0.01-0.10) ==="
    echo "fixture : $JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH"
    echo "cache   : $SWEBP_CACHE"
    echo "worktree: $SWEBP_WT"
    echo "caps    : cost=$COST_CAP wall=$MAX_WALL idle=$IDLE_TIMEOUT headless"
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
    export JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS="$SWEBP_INSTANCE_IDS"  # operator-chosen
    export JARVIS_SWE_BENCH_PRO_SCORE_REJECT_TEST_MODS=true     # cheat-detection ON (rubric integrity)
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
