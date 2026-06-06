#!/usr/bin/env bash
# =============================================================================
# launch_shadow_soak.sh — Slice 108
# Ignite O+V into full production SHADOW MODE for 12–18 month wall-clock evidence
# accrual. The advanced cognitive substrates + the OS-level Docker runtime cage run
# live; the fail-closed graduation actuator accrues receipts/advisories but NEVER
# flips a master flag. The human is the sole actuator (§51.11.2 / shadow_soak_runbook.md).
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
SBX="backend/core/ouroboros/governance/sandbox_profiles"
IMAGE="jarvis-governance-sandbox:latest"

log() { printf '\033[36m[shadow-soak]\033[0m %s\n' "$*"; }

# ---- 1. Docker daemon ----
log "verifying Docker daemon..."
if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  log "Docker not responding — attempting to start Docker Desktop..."
  open -a Docker 2>/dev/null || true
  for i in $(seq 1 60); do
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1 && break
    sleep 2
  done
  docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || {
    log "ERROR: Docker daemon did not come up. Start Docker Desktop and re-run."; exit 1; }
fi
log "Docker $(docker version --format '{{.Server.Version}}') is live."

# ---- 2. Layer-cached production VERIFY image (build only if requirements/Dockerfile changed) ----
export JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE="$IMAGE"
export JARVIS_SANDBOX_REQUIREMENTS_FILE="requirements-governance.txt"
export JARVIS_SANDBOX_DOCKERFILE="Dockerfile.production-sandbox"
WANT_HASH="$(python3 -c 'from backend.core.ouroboros.governance.image_provisioner import image_state_hash; print(image_state_hash())')"
HAVE_HASH="$(docker image inspect "$IMAGE" --format '{{index .Config.Labels "org.jarvis.state-hash"}}' 2>/dev/null || true)"
if [ "$WANT_HASH" != "$HAVE_HASH" ]; then
  log "building layer-cached production image ($IMAGE, hash=$WANT_HASH)..."
  DOCKER_BUILDKIT=1 docker build \
    -f "$SBX/Dockerfile.production-sandbox" \
    --build-arg REQUIREMENTS=requirements-governance.txt \
    --label "org.jarvis.state-hash=$WANT_HASH" \
    -t "$IMAGE" "$SBX"
else
  log "production image $IMAGE is hash-current ($WANT_HASH) — pre-warmed, no rebuild."
fi

# ---- 3. Ignite the cognitive bus + substrates (observational / shadow) ----
log "enabling cognitive substrates + OS-level runtime cage (shadow)..."
export JARVIS_COGNITIVE_BUS_ENABLED=1
export JARVIS_BELIEF_REVISION_ENABLED=1
export JARVIS_SLEEP_DAEMON_ENABLED=1
export JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED=1
export JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED=1
export JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED=1
export JARVIS_PROOF_CARRIER_ENABLED=1
# Recursion bound is default-ON (JARVIS_RECURSION_DEPTH_GATE_ENABLED / MAX=3).

# OS-level Docker containment for VERIFY (uses the layer-cached production image).
export JARVIS_RUNTIME_SANDBOX_ENABLED=1
export JARVIS_RUNTIME_SANDBOX_BACKEND=container
export JARVIS_IMAGE_PROVISIONER_ENABLED=1

# Graduation engine in SHADOW — fail-closed: receipts/advisories only, NO OS flip.
export JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED=1
# JARVIS_GRADUATION_SHADOW_MODE defaults TRUE — DO NOT unset/falsify it here.
# The apply gate is left OFF so the boot applier never flips. The human un-shadows
# manually, later, only after the empirical threshold is undeniable.
unset JARVIS_GRADUATION_SHADOW_MODE 2>/dev/null || true
unset JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED 2>/dev/null || true

log "SHADOW INVARIANT: graduation is fail-closed (default-TRUE). No master flag will flip."

# ---- 3b. Slice 109 — God-Tier Observability Matrix + Karen's voice ----
# The SSE Why-Snapshot stream (confidence_aura + Shannon entropy + decision
# prior distribution + recursion depth) is published on every post_apply /
# post_failure for the TUI / VS Code extension to render. Karen narrates only
# HIGH-severity cognitive events (containment breach, graduation threshold,
# load shedding, failure) — gated + mute-respecting.
log "igniting observability matrix + Karen's voice..."
export JARVIS_COGNITIVE_OBSERVABILITY_ENABLED=1     # SSE Why-Snapshot projection
export JARVIS_IDE_STREAM_ENABLED=1                  # SSE transport (GET /observability/stream)
export JARVIS_IDE_OBSERVABILITY_ENABLED=1           # read-only GET projections
export JARVIS_OP_LIFECYCLE_SSE_ENABLED=1            # operation_terminal SSE wake

# Karen's voice (macOS `say` — needs an audio device, NOT a TTY → works in the
# headless soak too). Unmuted by default; say "Karen mute" any time to silence.
export JARVIS_KAREN_VOICE_ENABLED=1                 # autonomous cognitive narration channel
export OUROBOROS_NARRATOR_ENABLED=1                 # Karen master (unmuted)
export JARVIS_KAREN_TOOL_VOICE_ENABLED=1            # tool/phase sub-voice (unmuted)
export JARVIS_KAREN_VOICE="${JARVIS_KAREN_VOICE:-Karen}"

# ---- 3c. Slice 110 — Native Command Center (FastAPI gateway + React UI) ----
# COMMAND_CENTER=1 brings up the SOVEREIGN INTERFACE instead of the headless
# soak: the FastAPI backend (backend/main.py) hosts the O+V engine + the
# observability gateway + the bus→WS bridge in ONE process, and the React
# command center is served on :3000. The cognitive bus, the Why-Snapshot
# bridge, and the WS fan-out all share that process, so the dashboard paints
# live cognitive telemetry. The headless evidence soak and the native UI are
# alternative front-ends to the same engine — we never double-boot it.
export JARVIS_OBSERVABILITY_GATEWAY_ENABLED=1
if [ "${COMMAND_CENTER:-0}" = "1" ]; then
  BACKEND_PORT="${JARVIS_BACKEND_PORT:-8000}"
  FRONTEND_PORT="${JARVIS_FRONTEND_PORT:-3000}"
  CC_PIDS=()
  cleanup_cc() {
    log "shutting down command center..."
    for pid in "${CC_PIDS[@]:-}"; do
      [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
  }
  trap cleanup_cc EXIT INT TERM

  log "starting O+V engine + FastAPI observability gateway on :$BACKEND_PORT..."
  python3 backend/main.py --port "$BACKEND_PORT" &
  CC_PIDS+=("$!")

  # Wait for the gateway health endpoint (async, bounded).
  for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$BACKEND_PORT/api/observability/health" >/dev/null 2>&1; then
      log "observability gateway is live (/api/observability/health)."
      break
    fi
    sleep 1
  done

  if [ -d frontend ] && command -v npm >/dev/null 2>&1; then
    log "serving the React command center on :$FRONTEND_PORT (→ /command-center)..."
    ( cd frontend && BROWSER=none PORT="$FRONTEND_PORT" \
        REACT_APP_API_URL="http://127.0.0.1:$BACKEND_PORT" npm start ) &
    CC_PIDS+=("$!")
    log "OPEN → http://localhost:$FRONTEND_PORT/command-center"
  else
    log "frontend/ or npm missing — gateway REST/WS is live at :$BACKEND_PORT, build the UI with 'cd frontend && npm install && npm start'."
  fi

  log "command center up. Ctrl-C to tear down. (engine + gateway + UI share one process tree)"
  wait
  exit 0
fi

# ---- 4. Launch the soak (headless evidence-accrual mode) ----
COST_CAP="${SHADOW_COST_CAP:-0.50}"
IDLE_TIMEOUT="${SHADOW_IDLE_TIMEOUT:-600}"
MAX_WALL="${SHADOW_MAX_WALL_SECONDS:-2400}"

# SHADOW_INTERACTIVE=1 drops --headless so the operator watches the live Rich
# TUI (token stream + diff overlays + status line) and the SerpentFlow REPL on
# a real terminal — the "let Karen narrate the boot" ignition session. The
# default (unset) stays headless for the long unattended soak; the TUI's visual
# surface needs a TTY, so it falls through to plain rendering when headless.
HEADLESS_FLAG="--headless"
if [ "${SHADOW_INTERACTIVE:-0}" = "1" ]; then
  HEADLESS_FLAG="--no-headless"
  log "INTERACTIVE ignition: live TUI + REPL enabled (TTY required)."
fi

log "launching O+V shadow soak (cost-cap=\$$COST_CAP idle=$IDLE_TIMEOUT wall=$MAX_WALL ${HEADLESS_FLAG})..."
exec python3 scripts/ouroboros_battle_test.py \
  --cost-cap "$COST_CAP" --idle-timeout "$IDLE_TIMEOUT" --max-wall-seconds "$MAX_WALL" \
  "$HEADLESS_FLAG" -v
