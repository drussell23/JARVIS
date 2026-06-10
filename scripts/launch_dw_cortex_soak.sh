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

# Slice 212 — runtime attestation. Stamp the image with the EXACT commit +
# dirty flag of this build context, and pin the same commit as the boot-time
# expectation. A stale rebuild (checkout behind what you merged) or a
# dirty-tree build then FAILS CLOSED at boot with DEPLOYMENT_INTEGRITY_MISMATCH
# instead of silently running old code (the 2026-06-10 drift class).
GIT_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unstamped)"
# Dirty = dirt that actually ENTERS the image. .jarvis (runtime state mutated
# through the bind-mount, e.g. roadmap.draft.yaml) and __pycache__ are in
# .dockerignore — they can never reach the image, so they must not dirty the
# stamp (first live trip 2026-06-10 was exactly this false positive).
GIT_DIRTY="$([ -n "$(git status --porcelain -- ':\!.jarvis' ':\!**/__pycache__' 2>/dev/null)" ] && echo true || echo false)"
export GIT_COMMIT GIT_DIRTY
export JARVIS_ATTESTATION_EXPECTED_COMMIT="$GIT_COMMIT"
log "Attestation: stamping ${GIT_COMMIT:0:12} (dirty=$GIT_DIRTY) + pinning as boot expectation."
[ "$GIT_DIRTY" = "true" ] && log "WARNING: dirty tree — strict attestation will REFUSE this image at boot. Commit or stash first."

if [ "${1:-}" = "--monitor" ]; then
  exec "$REPO_ROOT/scripts/dw_cortex_monitor.sh"
fi

log "Building the cortex-soak image (oracle-capable)…"
"${DC[@]}" -f "$COMPOSE" build
log "Igniting DW-only cortex soak (Claude DISABLED; 172/174 ON; restart=always)…"
"${DC[@]}" -f "$COMPOSE" up -d
log "Live. The cortex learns from real DW failures (per-model rings, multi-signal, self-calibration)."
log "  Forecasts/calibration:  docker compose -f $COMPOSE logs -f | grep -E 'Cortex|reroute'"
log "  Learned thresholds:     ./scripts/dw_cortex_monitor.sh"
log "  Stop:                   docker compose -f $COMPOSE down"

if [ "${1:-}" = "--no-logs" ]; then exit 0; fi
log "Following the cortex's reasoning (Ctrl-C detaches; the soak keeps running)…"
"${DC[@]}" -f "$COMPOSE" logs -f 2>&1 | grep --line-buffered -E "Cortex|reroute|forecast|preempt|INTRA_FAILOVER|live_transport" || true
