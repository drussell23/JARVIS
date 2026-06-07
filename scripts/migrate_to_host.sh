#!/usr/bin/env bash
# =============================================================================
# migrate_to_host.sh — Slice 139: The Deployment Handshake
# Package the organism, fire it to the industrial Linux host, provision the host,
# and ignite — one command. Run from your workstation (the source checkout).
#
#   Usage:  ./scripts/migrate_to_host.sh <user@host> <remote_dir> [--launch]
#   e.g.    ./scripts/migrate_to_host.sh ops@gcp-jprime /opt/jarvis --launch
#
# Steps:
#   1. pack_sovereign_release.sh → lean artifact (crypto/sig/evidence in; .env + .venv + .git out).
#   2. scp the artifact to the host.
#   3. scp .env SEPARATELY (secrets out-of-band over the same authenticated SSH; never in the tarball).
#   4. ssh: extract → provision_host.sh → (with --launch) arm_and_launch.sh.
#
# Without --launch it stops after provisioning and prints the final ignition
# command (so you can place/verify .env first). The signed roadmap travels in the
# artifact, so arm_and_launch.sh on the host is non-interactive (no re-sign).
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
log() { printf '\033[36m[migrate]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[migrate] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

HOST="${1:-}"; REMOTE_DIR="${2:-}"; LAUNCH="${3:-}"
[ -n "$HOST" ] && [ -n "$REMOTE_DIR" ] || die "usage: $0 <user@host> <remote_dir> [--launch]"
command -v ssh >/dev/null 2>&1 && command -v scp >/dev/null 2>&1 || die "ssh + scp required."

# ── 1. Package ───────────────────────────────────────────────────────────────
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
ART="$REPO_ROOT/dist/jarvis-sovereign-$SHA.tgz"
log "Packaging → $ART"
"$REPO_ROOT/scripts/pack_sovereign_release.sh" "$ART" >/dev/null
[ -f "$ART" ] || die "packaging failed."
ART_BASE="$(basename "$ART")"
log "Artifact: $ART ($(du -h "$ART" | cut -f1))"

# ── 2. Ship the artifact ─────────────────────────────────────────────────────
log "ssh: preparing $REMOTE_DIR on $HOST"
ssh "$HOST" "mkdir -p '$REMOTE_DIR'"
log "scp: artifact → $HOST:$REMOTE_DIR/"
scp "$ART" "$HOST:$REMOTE_DIR/$ART_BASE"

# ── 3. Ship secrets out-of-band (NOT in the artifact) ────────────────────────
if [ -f "$REPO_ROOT/.env" ]; then
  log "scp: .env (funded creds) → $HOST:$REMOTE_DIR/jarvis-sovereign/.env  [out-of-band, over authenticated SSH]"
  ssh "$HOST" "mkdir -p '$REMOTE_DIR/jarvis-sovereign'"
  scp "$REPO_ROOT/.env" "$HOST:$REMOTE_DIR/jarvis-sovereign/.env"
else
  log "WARNING: no local .env — you must place funded creds on the host before launch."
fi

# ── 4. Extract + provision (+ optional launch) ───────────────────────────────
log "ssh: extract + provision on $HOST"
ssh "$HOST" "set -e; cd '$REMOTE_DIR'; tar xzf '$ART_BASE'; cd jarvis-sovereign; \
  chmod +x scripts/*.sh deploy/*.sh; bash deploy/provision_host.sh"

if [ "$LAUNCH" = "--launch" ]; then
  log "ssh: igniting arm_and_launch.sh on $HOST (signed roadmap travels in the artifact → non-interactive)"
  ssh "$HOST" "cd '$REMOTE_DIR/jarvis-sovereign'; bash scripts/arm_and_launch.sh"
  log "═══════════════════════════════════════════════════════════════"
  log "MIGRATION COMPLETE — organism igniting on $HOST."
  log "  Watch: ssh $HOST 'tail -f $REMOTE_DIR/jarvis-sovereign/.jarvis/t5_soak.out'"
  log "═══════════════════════════════════════════════════════════════"
else
  log "═══════════════════════════════════════════════════════════════"
  log "MIGRATION STAGED on $HOST (not yet launched)."
  log "  Verify .env, then ignite:"
  log "    ssh $HOST 'cd $REMOTE_DIR/jarvis-sovereign && ./scripts/arm_and_launch.sh'"
  log "═══════════════════════════════════════════════════════════════"
fi
