#!/usr/bin/env bash
# Sentinel-mode production soak under the terminal-state supervisor.
#
#   launch_supervised_soak.sh [MAX_WALL_SECONDS]   (default 21600 = 6 h)
#
# Runs in the FOREGROUND on purpose: the WSL VM lives only while a wsl.exe
# client is attached, so whatever runs this must stay alive for the soak.
# Start it from scripts/windows/start_detached_soak.ps1, which holds the
# wsl.exe in a Scheduled Task instead of an interactive session. Soak
# bt-2026-09-23-005910 died with the session that held its wsl.exe.
set -u
MAX_WALL="${1:-21600}"
REPO="${OV_REPO:-/home/jarvis_svc/jarvis}"
PY="${OV_PYTHON:-/home/jarvis_svc/.venvs/ov/bin/python3}"
LOGS="${OV_SOAK_LOG_DIR:-$HOME/soak_logs}"   # not /tmp: it dies with the VM
mkdir -p "$LOGS"
LOG="$LOGS/soak-$(date '+%Y%m%d-%H%M%S').log"
exec >>"$LOG" 2>&1 </dev/null

cd "$REPO" || { echo "no repo at $REPO"; exit 1; }
echo "launch $(date '+%F %T') HEAD $(git rev-parse --abbrev-ref HEAD) $(git log --oneline -1)"

curl -s --max-time 5 http://localhost:11434/api/tags \
  | "$PY" -c 'import json,sys; m=json.load(sys.stdin).get("models",[]); print(len(m),"model(s):",[x["name"] for x in m]); sys.exit(0 if m else 1)' \
  || { echo "no inference lane"; exit 1; }

"$PY" - <<'PY' || exit 1
import os
from backend.core.ouroboros.battle_test.singleton_lock import live_incumbent_pid
from backend.core.ouroboros.cli.thin_client import repo_root
pid = live_incumbent_pid(repo_root(), exclude_pid=os.getpid())
print("incumbent:", pid or "none")
raise SystemExit(1 if pid else 0)
PY

export JARVIS_ORANGE_PR_ENABLED=false
export JARVIS_REMOTE_PUSH_AIRGAP=true
export JARVIS_SENTINEL_MODE_ENABLED=true
export JARVIS_GOAL_DISCOVERY_ENABLED=true
export JARVIS_REPAIR_TRAJECTORY_EMIT_ENABLED=true
# Continuous local promotion: verified landings fast-forward main (never pushed).
export JARVIS_ACCUMULATION_PROMOTION_ENABLED=${JARVIS_ACCUMULATION_PROMOTION_ENABLED:-true}
export PYTHONUNBUFFERED=1

exec "$PY" -m backend.core.ouroboros.battle_test.terminal_supervisor \
  --sessions-root "$REPO/.ouroboros/sessions" --log-dir "$LOGS" -- \
  "$PY" scripts/ouroboros_battle_test.py --production-soak --headless \
  --max-wall-seconds "$MAX_WALL"
