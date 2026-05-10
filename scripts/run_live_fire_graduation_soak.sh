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

# Cadence env discipline (2026-05-09) — load $REPO_ROOT/.env BEFORE
# the Phase 9 exports so DOUBLEWORD_API_KEY / ANTHROPIC_API_KEY land
# in the subprocess inherit-set. Diagnosed root cause of the soak's
# silent zero-cost / DW-blocked-by-topology runner-failure pattern:
# the canonical providers read os.environ at module load + never
# call load_dotenv(); the wrapper is the single env-block boundary
# that all cadence paths (cron / launchd / --once / manual) flow
# through, so it is the right place to source secrets.
#
# Operator-override discipline: explicit shell-set values WIN over
# .env (operator can `DOUBLEWORD_API_KEY=alt-key bash run_…sh` for
# testing without modifying .env). Implementation does not `source`
# the .env file — that would shell-eval arbitrary content. Instead
# parses KEY=VALUE lines with a tight regex + skips comments + only
# sets keys that are unset OR empty in the current env.
if [[ -f "$REPO_ROOT/.env" ]]; then
    # Use `case` + shell glob (not bash 4 regex) so the loader runs
    # cleanly under macOS's frozen bash 3.2.57 + zsh + every cadence
    # path. Validates KEY shape: starts with [A-Z_], contains only
    # [A-Z0-9_]. Anything else (comments / blank lines / lowercase
    # / shell substitutions / malformed lines) is silently skipped.
    #
    # Glob negation portability: bash 3.2 (frozen on macOS) treats
    # `[!chars]` as a literal `!` in the character class, NOT as
    # POSIX-style negation — only `[^chars]` works as negation.
    # Using `[^...]` keeps the loader portable across bash 3.2 +
    # bash 4+ + zsh.
    #
    # Indirect lookup portability: bash 3.2 lacks the
    # `${!var:-default}` combinator (bash 4.2+). We use the
    # `eval "lookup=\${$key}"` shape instead — same semantics
    # under set -e, no fragile combinators.
    while IFS='=' read -r _env_key _env_val; do
        [[ -z "$_env_key" ]] && continue
        case "$_env_key" in
            [A-Z_]*) ;;          # head must be valid
            *) continue ;;
        esac
        case "$_env_key" in
            *[^A-Z0-9_]*) continue ;;  # body must be all-valid
        esac
        # Strip surrounding quotes from value (common .env idiom).
        _env_val="${_env_val%\"}"; _env_val="${_env_val#\"}"
        _env_val="${_env_val%\'}"; _env_val="${_env_val#\'}"
        # Operator override wins — only set if currently unset/empty.
        # bash 3.2-safe indirect lookup via eval (no ${!var:-} combinator).
        _existing=""
        eval "_existing=\${$_env_key:-}"
        if [[ -z "$_existing" ]]; then
            export "$_env_key=$_env_val"
        fi
    done < "$REPO_ROOT/.env"
    unset _env_key _env_val _existing
fi

export JARVIS_GRADUATION_LEDGER_ENABLED=true
export JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true
export JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true
export JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true
# §3.6.2 vector #6 producer-loop wiring (2026-05-07) — populate
# .jarvis/graduation_interaction_matrix.jsonl as cadence runs
# so /phase9 partners view materializes empirically.
export JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true
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
