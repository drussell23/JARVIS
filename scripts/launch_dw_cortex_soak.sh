#!/usr/bin/env bash
# =============================================================================
# launch_dw_cortex_soak.sh — DW-only predictive-cortex soak (Slices 168–176)
# Build + launch DETACHED under dockerd (survives this session + reboot), then
# follow the cortex's reasoning. No Layer-4 signed-roadmap gate (this is a cortex
# validation run, not the full T5 sovereign launch) — only a funded DW key.
#
#   ./scripts/launch_dw_cortex_soak.sh            # build + up -d + follow cortex log
#   ./scripts/launch_dw_cortex_soak.sh --no-logs  # build + up -d, then return
#   ./scripts/launch_dw_cortex_soak.sh --monitor  # just attach the cortex monitor
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
COMPOSE="docker-compose.dw-cortex-soak.yml"
log() { printf '\033[36m[dw-cortex]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[dw-cortex] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

docker info >/dev/null 2>&1 || die "docker daemon not reachable."
[ -f "$REPO_ROOT/.env" ] || die "no .env at repo root (funded DOUBLEWORD_API_KEY required)."
grep -q "DOUBLEWORD_API_KEY" "$REPO_ROOT/.env" || die ".env has no DOUBLEWORD_API_KEY."

DC=(docker compose); docker compose version >/dev/null 2>&1 || DC=(docker-compose)
export SOAK_REQUIREMENTS="requirements-soak-oracle.txt"

# Slice 213 — the stamping/build/verify logic moved to the NATIVE PYTHON
# lifecycle kernel (typed, tested, async; no set-e traps — the bash trap here
# cost a deploy cycle on 2026-06-10). The kernel refuses dirty trees upfront,
# stamps + pins the commit, builds, launches, and VERIFIES the running
# container attests MATCH before reporting success (exit 0 only on
# ATTESTED_MATCH — a phantom deploy can no longer exit 0).
log "Handing off to the native lifecycle kernel (Slice 213)…"
python3 -m backend.core.ouroboros.governance.lifecycle_kernel --compose "$COMPOSE" \
  || die "lifecycle kernel refused or could not attest the launch (see log above)."
log "Live + ATTESTED. The cortex learns from real DW failures."
log "  Forecasts/calibration:  docker compose -f $COMPOSE logs -f | grep -E 'Cortex|reroute'"
log "  Learned thresholds:     ./scripts/dw_cortex_monitor.sh"
log "  Stop:                   docker compose -f $COMPOSE down"

if [ "${1:-}" = "--no-logs" ]; then exit 0; fi
log "Following the cortex's reasoning (Ctrl-C detaches; the soak keeps running)…"
"${DC[@]}" -f "$COMPOSE" logs -f 2>&1 | grep --line-buffered -E "Cortex|reroute|forecast|preempt|INTRA_FAILOVER|live_transport" || true
