#!/usr/bin/env bash
# ============================================================================
# Sovereign Remote Execution Bridge — launch the Ouroboros battle test on a
# dedicated high-compute Linux host (2026-06-19).
#
# Does what the local 16GB M1 could not: gives pytest room, scales the AST
# ProcessPool + background pool to the host's core count, and runs a real
# cost-capped soak so the loop can actually reach state=applied.
#
# Usage (from repo root on the Linux host, with .env present for secrets):
#   ./scripts/launch_linux_prod.sh [extra battle-test args...]
#
# Secrets come from .env (DOUBLEWORD_API_KEY / ANTHROPIC_API_KEY) — this
# script never embeds them. Non-Linux hosts are refused (the whole point).
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

# --- Refuse non-Linux: this profile exists precisely to escape the Mac. ---
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: launch_linux_prod.sh is for a dedicated Linux host." >&2
  echo "       Detected $(uname -s). On macOS the nested event loop starves" >&2
  echo "       (the empirically-proven wall). Use a Linux box / container." >&2
  exit 2
fi

# --- Load the production env profile. ---
PROFILE="${REPO_ROOT}/deploy/ouroboros_linux_prod.env"
if [[ ! -f "$PROFILE" ]]; then
  echo "ERROR: missing $PROFILE" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$PROFILE"

# --- Dynamically scale the pools to the host's core count. ---
CORES="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
# AST parse pool: cores - 1 (leave one for the event loop), min 2.
AST_WORKERS=$(( CORES > 2 ? CORES - 1 : 2 ))
# Background worker pool: half the cores, min 3, cap 12.
BG_WORKERS=$(( CORES / 2 )); (( BG_WORKERS < 3 )) && BG_WORKERS=3; (( BG_WORKERS > 12 )) && BG_WORKERS=12
export JARVIS_AST_HELPER_POOL_MAX_WORKERS="$AST_WORKERS"
export JARVIS_BG_POOL_SIZE="$BG_WORKERS"

# --- Load secrets from .env (never logged). ---
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a; # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"; set +a
fi

echo "── Sovereign Linux Prod launch ────────────────────────────────"
echo "  host cores       : ${CORES}"
echo "  AST pool workers : ${JARVIS_AST_HELPER_POOL_MAX_WORKERS} (default was 1)"
echo "  BG pool size     : ${JARVIS_BG_POOL_SIZE} (default was 3)"
echo "  pytest timeout   : ${JARVIS_INTENT_PYTEST_TIMEOUT_S}s (default was 30)"
echo "  cost cap         : \$${OUROBOROS_BATTLE_COST_CAP}"
echo "  wall cap         : ${OUROBOROS_BATTLE_MAX_WALL_SECONDS}s"
echo "  fleet evaluator  : ${JARVIS_FLEET_EVALUATOR_ENABLED}"
echo "  quota isolation  : ${JARVIS_PROVIDER_QUOTA_ISOLATION_ENABLED}"
echo "  DW key           : ${DOUBLEWORD_API_KEY:+present (masked)}"
echo "───────────────────────────────────────────────────────────────"

exec python3 scripts/ouroboros_battle_test.py \
  --cost-cap "${OUROBOROS_BATTLE_COST_CAP}" \
  --max-wall-seconds "${OUROBOROS_BATTLE_MAX_WALL_SECONDS}" \
  --headless -v "$@"
