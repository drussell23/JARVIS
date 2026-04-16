#!/usr/bin/env bash
# run_gemma_bg_repro.sh
#
# Option B for the 2026-04-16 DW benchmark report — a full ouroboros_battle_test
# reproduction of `bt-2026-04-14-182446` (Gemma 4 31B BACKGROUND isolation) on
# today's date. Low cost cap, short idle timeout — stops early on first SSE stall.
#
# Intent
# ------
# Dated full-harness reproduction of the original Apr 14 Gemma 0/13 failure mode.
# Confirms the blocker is still current as of the report date, proves the topology
# seal is doing what it claims, exercises the whole governance stack (router, cost
# governor, failback FSM, exhaustion watcher).
#
# Security
# --------
# Reads DOUBLEWORD_API_KEY and ANTHROPIC_API_KEY from environment only.
# Does NOT embed any keys in this script. If either is missing, the battle-test
# harness will refuse to start.
#
# Usage
# -----
#     bash scripts/benchmarks/run_gemma_bg_repro.sh
#
# Override knobs (all optional):
#     COST_CAP=0.50 bash scripts/benchmarks/run_gemma_bg_repro.sh
#     IDLE_TIMEOUT=600 bash scripts/benchmarks/run_gemma_bg_repro.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Pre-flight — required env
# ---------------------------------------------------------------------------
if [[ -z "${DOUBLEWORD_API_KEY:-}" ]]; then
  echo "ERROR: DOUBLEWORD_API_KEY not set. Export it before running:"
  echo "       export DOUBLEWORD_API_KEY='...'"
  echo "       NEVER commit this key to the repo."
  exit 2
fi
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY not set. The harness uses Claude for cascade routes."
  echo "       export ANTHROPIC_API_KEY='...'"
  exit 2
fi

# ---------------------------------------------------------------------------
# Tunables (defaults match Option B spec: low cap, short timeout, stop on stall)
# ---------------------------------------------------------------------------
COST_CAP="${COST_CAP:-0.30}"
IDLE_TIMEOUT="${IDLE_TIMEOUT:-300}"

# ---------------------------------------------------------------------------
# Overrides — force Gemma 31B into BACKGROUND route via topology override,
# sever Claude fallback so the stall surfaces (matches Apr 14 conditions)
# ---------------------------------------------------------------------------
export JARVIS_DOUBLEWORD_TOPOLOGY_OVERRIDE_ROUTE="background"
export DOUBLEWORD_MODEL="google/gemma-4-31B-it"
export JARVIS_FALLBACK_DISABLED_ROUTES="background"

# Observability: make sure the SSE stall log line (`SSE stream stalled (no data for 30s)`)
# is visible at INFO level so it lands in debug.log cleanly.
export JARVIS_LOG_LEVEL="${JARVIS_LOG_LEVEL:-INFO}"

echo "=============================================================="
echo "DW Gemma 31B BG repro — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=============================================================="
echo "Model:         $DOUBLEWORD_MODEL"
echo "Route:         background (topology override)"
echo "Fallback:      DISABLED (fallback_disabled_by_env:background)"
echo "Cost cap:      \$$COST_CAP"
echo "Idle timeout:  ${IDLE_TIMEOUT}s"
echo "=============================================================="
echo ""
echo "Expected outcome: SSE stream stalls, ops fail with"
echo "  background_dw_error:RuntimeError:..."
echo "Session should terminate on cost cap or idle timeout without"
echo "any valid candidates produced."
echo ""
echo "New session artifacts will write to:"
echo "  $REPO_ROOT/.ouroboros/sessions/bt-<timestamp>/"
echo ""
echo "Press Ctrl+C to abort. Starting in 3s..."
sleep 3

python3 scripts/ouroboros_battle_test.py \
    --cost-cap "$COST_CAP" \
    --idle-timeout "$IDLE_TIMEOUT" \
    -v

echo ""
echo "=============================================================="
echo "Repro complete. Latest session:"
ls -dt "$REPO_ROOT"/.ouroboros/sessions/bt-* | head -1
echo "=============================================================="
