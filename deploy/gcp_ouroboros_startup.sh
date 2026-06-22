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
# A1: the HMAC secret that validates the SIGNED roadmap.yaml. The reader's
# REQUIRE_SIGNATURE defaults TRUE, so without this the hydrated roadmap fails
# verification → zero goals → silent no-op. Travels as instance metadata
# (project-scoped; same channel as the DW key), never in git/the image.
HMAC_SECRET="$(meta jarvis-roadmap-hmac-secret)"
{
  [ -n "$DW_KEY" ] && echo "DOUBLEWORD_API_KEY=$DW_KEY"
  [ -n "$PIN" ] && echo "JARVIS_DW_PRIMARY_OVERRIDE=$PIN"
  # JIT GitHub auth — gh CLI + git both read GH_TOKEN; never logged (len only).
  [ -n "$GH_TOK" ] && echo "GH_TOKEN=$GH_TOK"
  [ -n "$GH_TOK" ] && echo "GITHUB_TOKEN=$GH_TOK"
  # A1 roadmap signature secret — read by roadmap_reader (env_file → container).
  [ -n "$HMAC_SECRET" ] && echo "JARVIS_ROADMAP_READER_HMAC_SECRET=$HMAC_SECRET"
} > "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
echo "[ignition] .env written (DW key len=${#DW_KEY}, pin=${PIN:-<default>}, gh_token len=${#GH_TOK}, hmac len=${#HMAC_SECRET})"
[ -z "$DW_KEY" ] && echo "[ignition] WARNING: no jarvis-dw-api-key metadata — DW calls will 401"
[ -z "$GH_TOK" ] && echo "[ignition] WARNING: no github-token secret / jarvis-gh-token metadata — graduation PRs cannot be pushed"
[ -z "$HMAC_SECRET" ] && echo "[ignition] WARNING: no jarvis-roadmap-hmac-secret metadata — signed roadmap will FAIL verification (no file-00 will emit)"
mkdir -p "$APP_DIR/.jarvis"

# ---- 3c. A1 Strategic Ignition — hydrate the SIGNED roadmap from the GCS Vault.
# The Vault (gs://<project>-deployments/crucible-state/.jarvis) is the source of
# truth where the operator-signed roadmap + graduation ledgers live. Pull
# roadmap.yaml from it so roadmap_reader has GOAL-001 (the first file-00) to
# decompose + emit. Uses the same host gcloud already relied on above (gcloud
# secrets, line ~67). On any miss, fall back to the committed UNSIGNED draft
# (reader needs REQUIRE_SIGNATURE=false to parse that) so the node still has *a*
# roadmap rather than silently emitting nothing.
ROADMAP_VAULT="gs://${GCP_PROJECT:-jarvis-473803}-deployments/crucible-state/.jarvis/roadmap.yaml"
echo "[ignition] hydrating signed roadmap <- $ROADMAP_VAULT"
if command -v gcloud >/dev/null 2>&1 && \
   gcloud storage cp "$ROADMAP_VAULT" "$APP_DIR/.jarvis/roadmap.yaml" 2>/dev/null; then
  echo "[ignition] roadmap.yaml hydrated from GCS Vault (signed source of truth)"
elif [ -f "$APP_DIR/.jarvis/roadmap.draft.yaml" ]; then
  cp "$APP_DIR/.jarvis/roadmap.draft.yaml" "$APP_DIR/.jarvis/roadmap.yaml"
  echo "[ignition] WARNING: Vault miss — using committed roadmap.draft.yaml (UNSIGNED; set JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE=false to parse it)"
else
  echo "[ignition] ERROR: no roadmap source (Vault miss + no committed draft) — roadmap reader will be a NO_ROADMAP no-op"
fi

# ---- 3b. Amnesia-proofing: RESTORE handled NATIVELY inside the container ----
# Preemption-resume (pulling the prior .jarvis ledger from GCS) is done by the
# Crucible cadence entrypoint (crucible_cadence.sh) via the NATIVE
# google-cloud-storage SDK (ADC from the metadata server) BEFORE its first soak —
# not here with a host gsutil. Single native source of truth; no CLI reliance.

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
