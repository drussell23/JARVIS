#!/usr/bin/env bash
# ============================================================================
# JARVIS C2 connect (2026-06-20) — one command: secure tunnel + live telemetry.
# ============================================================================
# Opens an SSH local-forward to the remote Linux engine's loopback SSE stream,
# then launches the M1 subscriber against it. The stream stays loopback-only on
# the engine; SSH is the encrypted transport (NOT log-tailing — this is the
# real-time TrinityEventBus/SSE event stream).
#
#   ./scripts/jarvis_c2_connect.sh user@linux-host [remote_port] [local_port]
#
# Ctrl-C tears down both the subscriber and the tunnel.
# ============================================================================
set -euo pipefail

HOSTSPEC="${1:?usage: jarvis_c2_connect.sh user@host [remote_port] [local_port]}"
RPORT="${2:-8099}"
LPORT="${3:-8099}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

echo "[C2] opening SSH forward localhost:${LPORT} -> ${HOSTSPEC}:127.0.0.1:${RPORT}"
ssh -N -L "${LPORT}:localhost:${RPORT}" "${HOSTSPEC}" &
SSH_PID=$!
trap 'echo "[C2] tearing down tunnel"; kill "${SSH_PID}" 2>/dev/null || true' EXIT INT TERM

# Give the tunnel a moment to establish.
sleep 2
echo "[C2] launching subscriber → http://localhost:${LPORT}"
python3 "${REPO_ROOT}/scripts/jarvis_c2_subscriber.py" --url "http://localhost:${LPORT}"
