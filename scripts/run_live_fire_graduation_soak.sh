#!/usr/bin/env bash
# run_live_fire_graduation_soak.sh — Phase 9 empirical cadence helper.
#
# Wraps scripts/live_fire_graduation_soak.py with the operator env block
# so the harness process can append to .jarvis/graduation_ledger.jsonl
# after each soak (GraduationLedger.record_session requires
# JARVIS_GRADUATION_LEDGER_ENABLED in the parent, not only inside the
# battle-test subprocess).
#
# Usage (from anywhere):
#   bash scripts/run_live_fire_graduation_soak.sh
#   bash scripts/run_live_fire_graduation_soak.sh queue
#   bash scripts/run_live_fire_graduation_soak.sh run JARVIS_DECISION_TRACE_LEDGER_ENABLED
#   bash scripts/run_live_fire_graduation_soak.sh evidence JARVIS_DECISION_TRACE_LEDGER_ENABLED
#
# Default with no args: same as "run" (pick-next flag).
#
# See also: scripts/install_live_fire_soak_cron.sh --install | --once
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

export JARVIS_GRADUATION_LEDGER_ENABLED=true
export JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true
export JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true
export JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true

HARNESS="$REPO_ROOT/scripts/live_fire_graduation_soak.py"
if [[ ! -f "$HARNESS" ]]; then
    echo "error: missing $HARNESS" >&2
    exit 1
fi

if [[ $# -eq 0 ]]; then
    exec /usr/bin/env python3 "$HARNESS" run
fi
exec /usr/bin/env python3 "$HARNESS" "$@"
