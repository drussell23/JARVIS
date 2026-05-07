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
# Phase 9 Slice 3 — synthetic workload injection for cadence soaks.
# Closes the headless zero-ops blocker. The harness's
# `_build_env_for_flag` defaults this to 3 when unset; setting it
# explicitly here is documentation + lets the operator override via
# `OUROBOROS_BATTLE_SEED_INTENTS=N bash run_live_fire_graduation_soak.sh`
# (operator value wins; harness default applied only when unset).
export OUROBOROS_BATTLE_SEED_INTENTS="${OUROBOROS_BATTLE_SEED_INTENTS:-3}"

HARNESS="$REPO_ROOT/scripts/live_fire_graduation_soak.py"
if [[ ! -f "$HARNESS" ]]; then
    echo "error: missing $HARNESS" >&2
    exit 1
fi

# Cadence Slice 2 (2026-05-06) — pre-invocation capability
# probe. Records ONE row to .jarvis/cadence_health.jsonl
# BEFORE the heavy harness imports. Closes the EPERM-before-
# Python silent-failure mode (cron #1 fired 2026-05-06 but
# macOS TCC denied the harness; history.jsonl never appended,
# detector blind). The probe itself is lightweight so a TCC-
# restricted Python can still execute it. Non-zero exit
# blocks the harness — preflight failures DO NOT proceed.
PREFLIGHT="$REPO_ROOT/scripts/cadence_preflight.py"
if [[ -f "$PREFLIGHT" ]]; then
    # Detect cadence kind from environment hints set by cron
    # / launchd installers. Falls back to "adhoc" for manual
    # operator invocations.
    CADENCE_KIND_HINT="${JARVIS_CADENCE_KIND:-adhoc}"
    if ! /usr/bin/env python3 "$PREFLIGHT" --cadence-kind "$CADENCE_KIND_HINT"; then
        echo "error: cadence_preflight failed; aborting before harness" >&2
        exit 2
    fi
fi

if [[ $# -eq 0 ]]; then
    exec /usr/bin/env python3 "$HARNESS" run
fi
exec /usr/bin/env python3 "$HARNESS" "$@"
