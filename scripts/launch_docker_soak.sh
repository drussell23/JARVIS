#!/usr/bin/env bash
# =============================================================================
# launch_docker_soak.sh — Slice 140: one-command containerized T5 soak
# Build the soak image, launch it DETACHED under dockerd (survives this session +
# host reboot, decoupled from any agent event loop), and tail its logs.
#
#   ./scripts/launch_docker_soak.sh           # build + up -d + follow logs
#   ./scripts/launch_docker_soak.sh --no-logs # build + up -d, then return
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
COMPOSE="docker-compose.soak.yml"
log() { printf '\033[36m[docker-soak]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[docker-soak] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

docker info >/dev/null 2>&1 || die "docker daemon not reachable."

# Slice 152 — --oracle builds the Oracle-CAPABLE image (lean + Oracle graph deps:
# networkx/scipy/scikit-learn/tree_sitter/aiofiles) so the Oracle BOOTS in-container
# on macOS/Docker (no separate Linux host). Default = lean (Oracle-degraded).
for _arg in "$@"; do
  if [ "$_arg" = "--oracle" ]; then
    export SOAK_REQUIREMENTS="requirements-soak-oracle.txt"
    log "ORACLE-CAPABLE build selected (SOAK_REQUIREMENTS=$SOAK_REQUIREMENTS)."
  fi
done

# Preflight: the runtime-mounted secrets + crypto must exist on the host.
[ -f "$REPO_ROOT/.env" ] || die "no .env at repo root (funded DOUBLEWORD_API_KEY / ANTHROPIC_API_KEY required)."
[ -f "$REPO_ROOT/.jarvis/roadmap.signed.yaml" ] || die "no .jarvis/roadmap.signed.yaml — provision + sign first (sovereign_keys), the Layer-4 gate is fail-closed."

DC=(docker compose)
docker compose version >/dev/null 2>&1 || DC=(docker-compose)

log "Building the soak image (heavy deps — first build takes a few minutes)…"
"${DC[@]}" -f "$COMPOSE" build

log "Igniting detached (restart=always → survives reboot; decoupled from this session)…"
"${DC[@]}" -f "$COMPOSE" up -d

log "═══════════════════════════════════════════════════════════════"
log "T5 SOAK CONTAINER RUNNING (detached, dockerd-supervised)."
log "  Status: ${DC[*]} -f $COMPOSE ps"
log "  Logs:   ${DC[*]} -f $COMPOSE logs -f jarvis-soak"
log "  Stop:   ${DC[*]} -f $COMPOSE down"
log "  Reboot-survival: sudo systemctl enable docker   (so dockerd starts on boot)"
log "═══════════════════════════════════════════════════════════════"

_no_logs=0
for _arg in "$@"; do [ "$_arg" = "--no-logs" ] && _no_logs=1; done
if [ "$_no_logs" -eq 0 ]; then
  log "Tailing logs (Ctrl-C detaches; the container keeps running)…"
  "${DC[@]}" -f "$COMPOSE" logs -f jarvis-soak
fi
