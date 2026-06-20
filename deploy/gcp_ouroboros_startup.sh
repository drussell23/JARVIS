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

# ---- 3. Funded creds + pin from instance metadata → .env -------------------
# Secrets travel as instance metadata (project-scoped, never in git/the image).
DW_KEY="$(meta jarvis-dw-api-key)"
PIN="$(meta jarvis-dw-primary-override)"
{
  [ -n "$DW_KEY" ] && echo "DOUBLEWORD_API_KEY=$DW_KEY"
  # Pin override (optional): falls back to deploy/ouroboros_linux_prod.env default.
  [ -n "$PIN" ] && echo "JARVIS_DW_PRIMARY_OVERRIDE=$PIN"
} > "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
if [ -n "$DW_KEY" ]; then
  echo "[ignition] .env written (DW key len=${#DW_KEY}, pin=${PIN:-<prod-env-default>})"
else
  echo "[ignition] WARNING: no jarvis-dw-api-key metadata — DW calls will 401"
fi
mkdir -p "$APP_DIR/.jarvis"

# ---- 4. Boot the Ouroboros soak container ----------------------------------
echo "[ignition] docker compose up (prod + gcp overlay, --build)…"
docker compose -f docker-compose.prod.yml -f docker-compose.gcp.yml up -d --build
echo "🐍 [SovereignIgnition] $(date -u +%FT%TZ) container up — soak running."
echo "[ignition] monitor:  docker logs -f jarvis-sovereign-prod   (on the VM)"
