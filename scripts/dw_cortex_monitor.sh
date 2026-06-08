#!/usr/bin/env bash
# =============================================================================
# dw_cortex_monitor.sh — observe what the DW predictive cortex has LEARNED.
# Reads the host-persisted per-model calibration thresholds (.jarvis) + the most
# recent cortex log lines from the running soak container. The rupture rings are
# in-process (not persisted), so the live forecast % is best seen via the Discord
# 🔮 spine or the container logs; the *learned thresholds* persist and show here.
# =============================================================================
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JARVIS_DIR="$REPO_ROOT/.jarvis"
CONTAINER="jarvis-dw-cortex-soak"

printf '\033[36m── DW Predictive Cortex — learned state ─────────────────────────\033[0m\n'

printf '\n\033[1mPer-model self-calibrated thresholds (persisted, Slice 174/175):\033[0m\n'
shopt -s nullglob
_found=0
for f in "$JARVIS_DIR"/dw_threshold_calibration_*.json; do
  _found=1
  model="$(basename "$f" .json | sed 's/^dw_threshold_calibration_//')"
  thr="$(python3 -c "import json,sys;print(f\"{json.load(open(sys.argv[1])).get('threshold',0):.3f}\")" "$f" 2>/dev/null || echo '?')"
  printf '  %-28s threshold = %s\n' "$model" "$thr"
done
[ "$_found" = 0 ] && printf '  (none yet — the cortex calibrates after predictions complete their forecast window)\n'

printf '\n\033[1mRecent cortex activity (container logs):\033[0m\n'
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
  docker logs --tail 2000 "$CONTAINER" 2>&1 \
    | grep -E "Cortex|forecast preempt|INTRA_FAILOVER|reroute|live_transport" \
    | tail -20 || printf '  (no cortex events yet — DW must produce failures for the cortex to learn from)\n'
else
  printf '  (container %s not running — launch with ./scripts/launch_dw_cortex_soak.sh)\n' "$CONTAINER"
fi
printf '\n\033[36m─────────────────────────────────────────────────────────────────\033[0m\n'
