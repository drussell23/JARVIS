#!/usr/bin/env bash
# One Training Lifecycle Handoff cycle, in the foreground of a detached task.
#
#   run_training_handoff.sh [TRIGGER]
#
# Started by scripts/windows/start_detached_wsl.ps1 (Task Scheduler holds the
# wsl.exe, so the VM outlives whoever requested the cycle). Logs to
# ~/.jarvis/logs/training_handoff-<ts>.log; state in ~/.jarvis/training_handoff/.
set -u
REPO="${OV_REPO:-/home/jarvis_svc/jarvis}"
PY="${OV_PYTHON:-/home/jarvis_svc/.venvs/ov/bin/python3}"
LOGS="$HOME/.jarvis/logs"; mkdir -p "$LOGS"
exec >>"$LOGS/training_handoff-$(date '+%Y%m%d-%H%M%S').log" 2>&1 </dev/null
cd "$REPO" || { echo "no repo at $REPO"; exit 1; }
export PYTHONUNBUFFERED=1
exec "$PY" -m backend.core.ouroboros.governance.observability.training_handoff run --trigger "${1:-manual}"
