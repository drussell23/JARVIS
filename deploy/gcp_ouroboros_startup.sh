#!/bin/bash
# =============================================================================
# Sovereign Cloud Ignition — GCE metadata-startup-script for the Ouroboros soak.
# Runs ONCE on first boot of the e2-custom-8-16384 Spot node provisioned by
# scripts/ignite_sovereign_cloud_node.py (→ GCPVMManager.start_soak_vm).
#
# No human SSH: installs Docker, clones the PUBLIC repo @ main, pulls the funded
# DW key + model pin from instance metadata, and boots the Ouroboros container
# (docker-compose.prod.yml + docker-compose.gcp.yml). The container's entrypoint
# (launch_linux_prod.sh) sources deploy/ouroboros_linux_prod.env which already
# carries the pin + pure-DW autarky; the GCP overlay adds the constrained-loop
# opt-outs (advisory subsystems off the event loop, surface-health probe off).
#
# Idempotent: re-running (e.g. after a Spot STOP→restart) detects an existing
# clone + container and only re-ups. All output → /var/log/jarvis-soak.log AND
# the serial console (readable via `gcloud compute instances get-serial-port-output`).
# =============================================================================
set -uo pipefail
exec > >(tee -a /var/log/jarvis-soak.log | tee /dev/console) 2>&1
echo "🐍 [SovereignIgnition] $(date -u +%FT%TZ) startup begins"

REPO_URL="https://github.com/drussell23/JARVIS-AI-Agent.git"
APP_DIR="/opt/jarvis/jarvis-ai-agent"
META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR="Metadata-Flavor: Google"

meta() { curl -s -f -H "$HDR" "$META/$1" 2>/dev/null || echo ""; }

# ---- 1. Docker + git (idempotent; skip if already present) ------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "[ignition] installing Docker + git…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc 2>/dev/null \
    || curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
else
  echo "[ignition] Docker already present — skipping install"
fi

# ---- 2. Clone (or refresh) the public repo @ main --------------------------
mkdir -p "$(dirname "$APP_DIR")"
if [ -d "$APP_DIR/.git" ]; then
  echo "[ignition] repo present — fetching latest main"
  git -C "$APP_DIR" fetch --depth 1 origin main && git -C "$APP_DIR" reset --hard origin/main
else
  echo "[ignition] cloning $REPO_URL @ main"
  git clone --depth 1 -b main "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR" || { echo "[ignition] FATAL: cannot cd $APP_DIR"; exit 1; }

# ---- 3. Funded creds + pin + JIT GitHub token → .env -----------------------
# Secrets travel as instance metadata (project-scoped, never in git/the image).
# The GitHub token is the JIT Auth Vault: preferred source is Secret Manager
# (gcloud secrets), falling back to instance metadata `jarvis-gh-token`. It lets
# the Crucible push branches + open [SOVEREIGN GRADUATION] PRs with no human SSH.
DW_KEY="$(meta jarvis-dw-api-key)"
PIN="$(meta jarvis-dw-primary-override)"
GH_TOK="$(gcloud secrets versions access latest --secret=github-token \
  --project="${GCP_PROJECT:-jarvis-473803}" 2>/dev/null || true)"
[ -z "$GH_TOK" ] && GH_TOK="$(meta jarvis-gh-token)"
{
  [ -n "$DW_KEY" ] && echo "DOUBLEWORD_API_KEY=$DW_KEY"
  [ -n "$PIN" ] && echo "JARVIS_DW_PRIMARY_OVERRIDE=$PIN"
  # JIT GitHub auth — gh CLI + git both read GH_TOKEN; never logged (len only).
  [ -n "$GH_TOK" ] && echo "GH_TOKEN=$GH_TOK"
  [ -n "$GH_TOK" ] && echo "GITHUB_TOKEN=$GH_TOK"
} > "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
echo "[ignition] .env written (DW key len=${#DW_KEY}, pin=${PIN:-<default>}, gh_token len=${#GH_TOK})"
[ -z "$DW_KEY" ] && echo "[ignition] WARNING: no jarvis-dw-api-key metadata — DW calls will 401"
[ -z "$GH_TOK" ] && echo "[ignition] WARNING: no github-token secret / jarvis-gh-token metadata — graduation PRs cannot be pushed"
mkdir -p "$APP_DIR/.jarvis"

# ---- 3b. Amnesia-proofing: RESTORE .jarvis from GCS before boot ------------
# A preempted Spot node restarts here (startup-script runs on every boot). Pull
# the prior graduation ledger + soak history so the Crucible resumes its cadence
# (e.g. interrupted at Soak 2 → continues at Soak 3) instead of starting over.
# Fail-soft: an empty/absent bucket path is a no-op (fresh node).
GCS_STATE="gs://jarvis-473803-deployments/crucible-state/.jarvis"
if command -v gsutil >/dev/null 2>&1; then
  echo "[ignition] restoring .jarvis from ${GCS_STATE} (resume) …"
  gsutil -m rsync -r "$GCS_STATE" "$APP_DIR/.jarvis" 2>/dev/null \
    && echo "[ignition] .jarvis restored from GCS" \
    || echo "[ignition] no prior GCS state (fresh node) — starting clean"
else
  echo "[ignition] gsutil not present — skipping GCS restore"
fi

# ---- 4. Boot the container --------------------------------------------------
# `jarvis-crucible-mode=true` metadata arms the autonomic graduation cadence
# (crucible overlay: one soak at a time → propose PR → GCS sync). Absent/false
# → the legacy perpetual-battle-test soak node (unchanged).
CRUCIBLE="$(meta jarvis-crucible-mode)"
if [ "$CRUCIBLE" = "true" ]; then
  echo "[ignition] 🧬 CRUCIBLE MODE — arming the immortal graduation cadence…"
  docker compose -f docker-compose.prod.yml -f docker-compose.gcp.yml \
    -f docker-compose.crucible.yml up -d --build
  echo "🧬 [SovereignIgnition] $(date -u +%FT%TZ) Crucible cadence armed."
else
  echo "[ignition] docker compose up (prod + gcp overlay, --build)…"
  docker compose -f docker-compose.prod.yml -f docker-compose.gcp.yml up -d --build
  echo "🐍 [SovereignIgnition] $(date -u +%FT%TZ) container up — soak running."
fi
echo "[ignition] monitor:  docker logs -f jarvis-sovereign-prod   (on the VM)"
