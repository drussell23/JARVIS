#!/usr/bin/env bash
# =============================================================================
# arm_and_launch.sh — Slice 138: The Indestructible Deployment
# One foolproof command to arm the cryptographic gate and launch the T5 Sovereign
# Organism as reboot-surviving systemd services (the soak + the state vault).
#
#   1. Provision the Ed25519 operator key (if needed) + sign the roadmap.
#   2. Render + install the systemd units (agent + state vault) as USER services.
#   3. enable --now both + enable-linger so they survive logout AND reboot.
#
# Linux + systemd only. Run from anywhere inside the repo. Re-runnable (idempotent).
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
JARVIS_DIR="$REPO_ROOT/.jarvis"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$JARVIS_DIR" "$UNIT_DIR"

log() { printf '\033[36m[arm]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[arm] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

command -v systemctl >/dev/null 2>&1 || die "systemctl not found — this host has no systemd. \
Run the soak on a dedicated Linux server (see deploy/README.md for the launchd/nohup fallback)."

# ── Preflight: funded creds present? (loaded by launch_shadow_soak.sh) ────────
if [ ! -f "$REPO_ROOT/.env" ]; then
  log "WARNING: no .env at repo root — DOUBLEWORD_API_KEY / ANTHROPIC_API_KEY must be present for a real soak."
fi

# ── Phase 1: cryptographic provisioning + roadmap signing ────────────────────
PUB="$JARVIS_DIR/layer4_operator.pub"
if [ ! -f "$PUB" ] && [ -z "${JARVIS_LAYER4_OPERATOR_PUBKEY:-}" ]; then
  log "No operator key found — provisioning (you'll be prompted for a passphrase; it is NEVER stored)."
  python3 -m backend.core.ouroboros.governance.sovereign_keys provision
else
  log "Operator key already provisioned."
fi

DRAFT="$JARVIS_DIR/roadmap.draft.yaml"
SIGNED="$JARVIS_DIR/roadmap.signed.yaml"
if [ ! -f "$DRAFT" ]; then
  die "No roadmap draft at $DRAFT. Author your SAFE authorized scopes there first \
(an unsigned, authority-free draft), then re-run. The un-signable floor still holds: \
Order-2/M10, recursion, governance, and APPROVAL_REQUIRED/BLOCKED always escalate to you."
fi
log "Signing roadmap: $DRAFT → $SIGNED (passphrase prompt)."
python3 -m backend.core.ouroboros.governance.sovereign_keys sign --draft "$DRAFT" --out "$SIGNED"
[ -f "$SIGNED" ] || die "Signing did not produce $SIGNED."

# ── Phase 2: operator parameters (sane defaults) ─────────────────────────────
COST_CAP="${JARVIS_T5_COST_CAP:-500}"
BACKUP_BACKEND="${JARVIS_BACKUP_BACKEND:-}"
BACKUP_TARGET="${JARVIS_BACKUP_TARGET:-}"
BACKUP_INTERVAL_S="${JARVIS_BACKUP_INTERVAL_S:-900}"
if [ -z "$BACKUP_TARGET" ]; then
  read -r -p "[arm] State-vault backend (rsync/s3/git) [skip]: " BACKUP_BACKEND || true
  if [ -n "${BACKUP_BACKEND:-}" ]; then
    read -r -p "[arm] State-vault target (e.g. user@host:/vault | s3://bucket/jarvis | git-remote): " BACKUP_TARGET || true
  fi
fi

# ── Phase 3: render + install the systemd units ──────────────────────────────
render() { # render <template> <dest>
  sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
      -e "s|@COST_CAP@|$COST_CAP|g" \
      -e "s|@BACKUP_BACKEND@|${BACKUP_BACKEND:-rsync}|g" \
      -e "s|@BACKUP_TARGET@|${BACKUP_TARGET:-}|g" \
      -e "s|@BACKUP_INTERVAL_S@|$BACKUP_INTERVAL_S|g" \
      "$1" > "$2"
}
render "$DEPLOY/jarvis-agent.service.template" "$UNIT_DIR/jarvis-agent.service"
log "Installed $UNIT_DIR/jarvis-agent.service (cost-cap=\$$COST_CAP)."

systemctl --user daemon-reload

# Survive logout AND reboot without an interactive session.
loginctl enable-linger "$USER" 2>/dev/null || log "enable-linger skipped (may need: sudo loginctl enable-linger $USER)."

if [ -n "${BACKUP_TARGET:-}" ]; then
  render "$DEPLOY/jarvis-state-vault.service.template" "$UNIT_DIR/jarvis-state-vault.service"
  systemctl --user daemon-reload
  systemctl --user enable --now jarvis-state-vault.service
  log "State Vault ARMED → ${BACKUP_TARGET} (every ${BACKUP_INTERVAL_S}s)."
else
  log "WARNING: no backup target — .jarvis/ is NOT protected against host-death. Re-run with a target to arm the vault."
fi

systemctl --user enable --now jarvis-agent.service

# ── Status ───────────────────────────────────────────────────────────────────
log "═══════════════════════════════════════════════════════════════"
log "T5 SOVEREIGN ORGANISM IGNITED."
log "  Soak:        systemctl --user status jarvis-agent.service"
log "  State Vault: systemctl --user status jarvis-state-vault.service"
log "  Live logs:   tail -f $JARVIS_DIR/t5_soak.out"
log "  Stop:        systemctl --user stop jarvis-agent.service jarvis-state-vault.service"
log "═══════════════════════════════════════════════════════════════"
