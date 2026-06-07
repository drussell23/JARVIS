#!/usr/bin/env bash
# =============================================================================
# provision_host.sh — Slice 139: Infrastructure-as-Code bootstrap
# Provisions a fresh Linux host with the exact dependencies the JARVIS sovereign
# organism needs, then builds its venv. Idempotent; run ON the target host from
# inside the extracted artifact directory (migrate_to_host.sh does this for you).
#
# Provides: Python 3.11+, build toolchain (numpy / fastembed native deps),
# z3-solver system prereqs, git + rsync (state vault), systemd (already present
# on a standard Linux server). Supports apt (Debian/Ubuntu) + dnf (RHEL/Fedora).
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
log() { printf '\033[36m[provision]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[provision] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

# ── 1. System dependencies ───────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then
  log "apt: installing system deps…"
  $SUDO apt-get update -y
  $SUDO apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    build-essential git rsync ca-certificates curl
elif command -v dnf >/dev/null 2>&1; then
  log "dnf: installing system deps…"
  $SUDO dnf install -y python3 python3-pip python3-devel \
    gcc gcc-c++ make git rsync ca-certificates curl
else
  die "no apt-get or dnf — install python3.11+, build tools, git, rsync manually."
fi

# ── 2. Python version gate (3.11+ ideal; 3.9 hard minimum per CLAUDE.md) ─────
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
log "python3 = $PYV"
python3 - <<'PY' || die "Python 3.9+ required (3.11+ recommended)."
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)
PY
case "$PYV" in 3.9|3.10) log "WARNING: $PYV works but 3.11+ is recommended for the soak." ;; esac

# ── 3. Virtualenv + Python deps ──────────────────────────────────────────────
if [ ! -d .venv ]; then log "creating .venv…"; python3 -m venv .venv; fi
# shellcheck disable=SC1091
. .venv/bin/activate
log "installing requirements (this pulls anthropic / aiohttp / cryptography / numpy / fastembed / z3-solver)…"
pip install --upgrade pip wheel setuptools >/dev/null
pip install -r requirements.txt

# ── 4. Sanity: the load-bearing imports actually resolve ─────────────────────
python3 - <<'PY' || die "dependency import sanity check failed."
import importlib
for m in ("anthropic", "aiohttp", "cryptography", "numpy", "yaml"):
    importlib.import_module(m)
# z3 + fastembed are import-guarded/optional in-app; warn but don't fail.
for opt in ("z3", "fastembed"):
    try:
        importlib.import_module(opt)
    except Exception:
        print(f"[provision] note: optional '{opt}' unavailable (app fails-closed/falls back).")
print("[provision] core imports OK")
PY

# ── 5. systemd presence + crypto perms ───────────────────────────────────────
command -v systemctl >/dev/null 2>&1 || log "WARNING: no systemctl — arm_and_launch.sh needs systemd for the reboot-surviving daemon."
chmod 700 .jarvis 2>/dev/null || true
chmod 600 .jarvis/layer4_* .jarvis/roadmap.signed.yaml 2>/dev/null || true

log "═══════════════════════════════════════════════════════════════"
log "HOST PROVISIONED."
log "  1. Place funded creds: write .env at $REPO_ROOT (DOUBLEWORD_API_KEY / ANTHROPIC_API_KEY)"
log "  2. Ignite:             ./scripts/arm_and_launch.sh"
log "═══════════════════════════════════════════════════════════════"
