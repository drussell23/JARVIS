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

# Slice 115: --siege-mode runs the Blue/Red Adversarial Falsification Matrix in
# the background DURING the soak — firing hostile payloads at the cage + the
# recursion-depth bound and recording tamper-evident receipts into
# dissertation_evidence.jsonl, accelerating the evidence clock without touching
# the production state (read-only over the cage; siege fires off-hot-path).
SIEGE_ARGS=()
for _a in "$@"; do
  case "$_a" in
    --siege-mode)
      export JARVIS_RED_BLUE_MATRIX_ENABLED=1
      export JARVIS_SIEGE_MODE=1
      ;;
    --layer4-autonomous)
      # Slice 120: arm the Sovereign Layer-4 Roadmap Authority. The system
      # reads .jarvis/roadmap.signed.yaml, verifies the operator's HMAC
      # signature, and — for the SAFE, explicitly-authorized scopes ONLY —
      # auto-resolves the approval prompt so the evidence clock runs unattended.
      # The un-signable floor still holds absolutely: Order-2/M10, recursion
      # breach, governance touches, and APPROVAL_REQUIRED/BLOCKED tiers ALWAYS
      # escalate to a live operator, regardless of signature. A missing/forged/
      # expired roadmap fails CLOSED → per-PR human review (legacy behavior).
      export JARVIS_LAYER4_ROADMAP_ENABLED=1
      ;;
    *) SIEGE_ARGS+=("$_a") ;;
  esac
done
set -- "${SIEGE_ARGS[@]:-}"

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
# Slice 123: each flag now respects an existing env value, so the operator can
# silence the whole voice channel for a run with `JARVIS_KAREN_VOICE_ENABLED=0
# ./scripts/launch_shadow_soak.sh ...` (temporary, no code edit).
export JARVIS_KAREN_VOICE_ENABLED="${JARVIS_KAREN_VOICE_ENABLED:-1}"   # autonomous cognitive narration channel
export OUROBOROS_NARRATOR_ENABLED="${OUROBOROS_NARRATOR_ENABLED:-1}"   # Karen master
export JARVIS_KAREN_TOOL_VOICE_ENABLED="${JARVIS_KAREN_TOOL_VOICE_ENABLED:-1}"  # tool/phase sub-voice
export JARVIS_KAREN_VOICE="${JARVIS_KAREN_VOICE:-Karen}"

# ---- 3c. Slice 110 — Native Command Center (live engine + gateway + React UI) ----
# COMMAND_CENTER=1 brings up the SOVEREIGN INTERFACE: it runs the SOAK HARNESS
# (the O+V engine — the cognitive-event PRODUCER) with the gateway co-booted IN
# THE SAME PROCESS + EVENT LOOP (JARVIS_COMMAND_CENTER_GATEWAY=1). Because the
# producer (GLS, which registers the bus→WS bridge at boot) and the gateway WS
# server share one process, the bridge's live broadcasts reach the dashboard —
# so the command center PULSES with real cognitive telemetry, not an idle shell.
# The React UI is served on :3000. (Running backend/main.py would serve the UI
# but NOT boot the engine → an idle dashboard; that was the prior gap.)
export JARVIS_OBSERVABILITY_GATEWAY_ENABLED=1
if [ "${COMMAND_CENTER:-0}" = "1" ]; then
  BACKEND_PORT="${JARVIS_BACKEND_PORT:-8000}"
  FRONTEND_PORT="${JARVIS_FRONTEND_PORT:-3000}"
  CC_COST_CAP="${SHADOW_COST_CAP:-0.50}"
  CC_IDLE="${SHADOW_IDLE_TIMEOUT:-600}"
  CC_WALL="${SHADOW_MAX_WALL_SECONDS:-0}"      # 0 = INFINITE (12–18 mo deployment); set SHADOW_MAX_WALL_SECONDS for a bounded watch
  export JARVIS_COMMAND_CENTER_GATEWAY=1        # co-boot the gateway inside the engine loop
  # Slice 112/113: run the Oracle in its OWN process so the 1.1 GB / 166 s graph
  # load happens on a separate GIL and NEVER freezes the engine loop — this is
  # what keeps the gateway responsive THROUGH the hydration window (the soak's
  # proof). Plus isolated-node graph hygiene to shrink the cache. Both
  # overridable; default ON for the command-center soak.
  export JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED="${JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED:-1}"
  export JARVIS_ORACLE_GRAPH_PRUNE_ENABLED="${JARVIS_ORACLE_GRAPH_PRUNE_ENABLED:-1}"
  # Slice 114: run the gateway in its OWN process (cross-process telemetry
  # queue) so the command center is immune to EVERY engine-loop freeze —
  # ChromaDB / embeddings / Oracle / any GIL-heavy op. The UI never blinks.
  export JARVIS_GATEWAY_DECOUPLED_ENABLED="${JARVIS_GATEWAY_DECOUPLED_ENABLED:-1}"
  export JARVIS_BACKEND_PORT FRONTEND_PORT JARVIS_FRONTEND_PORT="$FRONTEND_PORT"
  CC_PIDS=()
  cleanup_cc() {
    log "shutting down command center..."
    for pid in "${CC_PIDS[@]:-}"; do
      [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
  }
  trap cleanup_cc EXIT INT TERM

  log "booting O+V engine (soak) + co-booted gateway on :$BACKEND_PORT (cost=\$$CC_COST_CAP wall=${CC_WALL}s)..."
  python3 scripts/ouroboros_battle_test.py \
    --cost-cap "$CC_COST_CAP" --idle-timeout "$CC_IDLE" --max-wall-seconds "$CC_WALL" \
    --headless -v &
  CC_PIDS+=("$!")

  # Wait for the co-booted gateway health endpoint (async, bounded — the engine
  # boots the full 6-layer stack before serving, so allow generous time).
  for i in $(seq 1 90); do
    if curl -sf "http://127.0.0.1:$BACKEND_PORT/api/observability/health" >/dev/null 2>&1; then
      log "live gateway up — engine producing telemetry (/api/observability/health)."
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
    log "frontend/ or npm missing — live gateway REST/WS is up at :$BACKEND_PORT; build the UI with 'cd frontend && npm install && npm start'."
  fi

  log "command center LIVE. Engine + gateway + UI share one process tree; the dashboard pulses as the engine works. Ctrl-C to tear down."
  wait
  exit 0
fi

# ---- 4. Launch the soak (headless evidence-accrual mode) ----
# Slice 111: the wall cap defaults to 0 = INFINITE for the true 12–18 month
# evidence-accrual deployment (the harness treats 0/unset as "no wall cap" —
# liveness then rests on --idle-timeout + the cost budget). NOTE: this removes
# the OOM/hang safety ceiling the watchdog provides; set SHADOW_MAX_WALL_SECONDS
# (e.g. 2400) to restore a bounded soak for graduation runs or CI.
COST_CAP="${SHADOW_COST_CAP:-0.50}"
IDLE_TIMEOUT="${SHADOW_IDLE_TIMEOUT:-600}"
MAX_WALL="${SHADOW_MAX_WALL_SECONDS:-0}"

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
