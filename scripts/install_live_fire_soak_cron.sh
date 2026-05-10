#!/usr/bin/env bash
# install_live_fire_soak_cron.sh — Phase 9.1 cron installer.
#
# Installs the cron entry for the Live-Fire Graduation Soak harness.
# Per PRD §9 P9.1: 3 sessions/day rotating through pickable flags ≈
# 4-6 weeks to fully graduated.
#
# Defaults:
#   * Schedule: 0 */8 * * * (every 8 hours = 3 sessions/day)
#   * Cost cap: $0.50/soak
#   * Wall-clock cap: 2400s (40 min)
#   * Subprocess timeout: 3600s (60 min hard kill)
#
# Usage:
#   bash scripts/install_live_fire_soak_cron.sh           # install
#   bash scripts/install_live_fire_soak_cron.sh --dry-run # preview only
#   bash scripts/install_live_fire_soak_cron.sh --remove  # uninstall
#   bash scripts/install_live_fire_soak_cron.sh --once    # run ONE soak now
#
# Idempotent: re-running on an already-installed cron updates the
# entry in-place rather than duplicating.
#
# Authority posture:
#   * Operator must explicitly run this script — never auto-installed
#   * Crontab is the operator's; this script appends/replaces a single
#     marked block bracketed by # === LIVE_FIRE_SOAK_BEGIN/END ===
#   * Pre-flight checks: repo path exists, harness flag-default-false
#     proven via dry-run before any real soak
#
# Hot-rollback:
#   bash scripts/install_live_fire_soak_cron.sh --remove
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS_SCRIPT="$REPO_ROOT/scripts/live_fire_graduation_soak.py"
LOG_DIR="$REPO_ROOT/.jarvis/live_fire_soak_logs"
LOG_FILE_TEMPLATE='$(date +\%Y\%m\%d-\%H\%M\%S).log'

CRON_SCHEDULE_DEFAULT="0 */8 * * *"
COST_CAP_DEFAULT="0.50"
WALL_CAP_DEFAULT="2400"
TIMEOUT_DEFAULT="3600"

CRON_SCHEDULE="${CRON_SCHEDULE:-$CRON_SCHEDULE_DEFAULT}"
COST_CAP="${COST_CAP:-$COST_CAP_DEFAULT}"
WALL_CAP="${WALL_CAP:-$WALL_CAP_DEFAULT}"
TIMEOUT="${TIMEOUT:-$TIMEOUT_DEFAULT}"

BEGIN_MARKER="# === LIVE_FIRE_SOAK_BEGIN ==="
END_MARKER="# === LIVE_FIRE_SOAK_END ==="

# ANSI colors
if [[ -t 1 ]]; then
    BOLD="\033[1m"; CYAN="\033[36m"; GREEN="\033[32m"
    YELLOW="\033[33m"; RED="\033[31m"; DIM="\033[2m"; RESET="\033[0m"
else
    BOLD=""; CYAN=""; GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
fi

usage() {
    cat <<EOF
Usage: $0 [option]

Cron path (legacy; macOS may TCC-deny):
  --install         Install/update the cron entry (idempotent).
  --dry-run         Print the cron entry that WOULD be installed.
  --remove          Remove the live-fire soak block from crontab.
  --once            Run a single soak NOW (skips cron, first proof).
  --status          Show current crontab + ledger queue + cadence health.

Launchd path (recommended on macOS — Cadence Slice 4):
  --launchd         Install User Agent at
                    ~/Library/LaunchAgents/com.jarvis.live-fire-soak.plist
                    + launchctl load -w. Reuses the same
                    run_live_fire_graduation_soak.sh wrapper as
                    the cron path → single env-block source of
                    truth. Closes the macOS TCC EPERM gap that
                    bit cron #1 on 2026-05-06.
  --launchd-dry-run Print the plist that WOULD be installed.
  --remove-launchd  launchctl unload + delete plist.

Misc:
  --help            This message.

Environment overrides:
  CRON_SCHEDULE  default: $CRON_SCHEDULE_DEFAULT (every 8 hours)
                 Drives launchd StartInterval too — same source.
  COST_CAP       default: \$$COST_CAP_DEFAULT  (per-soak USD cap)
  WALL_CAP       default: $WALL_CAP_DEFAULT s   (per-soak wall-clock cap)
  TIMEOUT        default: $TIMEOUT_DEFAULT s   (subprocess kill timeout)

Examples:
  $0 --launchd                                    # recommended on macOS
  $0 --dry-run                                    # legacy cron preview
  CRON_SCHEDULE='0 */12 * * *' $0 --launchd       # 12h cadence
  $0 --once
  $0 --remove-launchd
EOF
}

build_cron_block() {
    # Four env vars armed in the cron entry (parent harness process):
    #
    # 0. JARVIS_GRADUATION_LEDGER_ENABLED=true — required so the harness
    #    process can call GraduationLedger.record_session after the soak
    #    subprocess returns (subprocess already gets ledger=true via
    #    live_fire_soak._build_env_for_flag; parent must match for writes).
    #
    # 1. JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true — original P9.1
    #    master switch. Cron-only authority; no production-runtime side
    #    effect when off.
    #
    # 2. JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true — P9.2 contract
    #    consultation. Without this, the default classifier silently
    #    graduates 0-op sessions as CLEAN (the once-proof on session
    #    bt-2026-04-27-162115 demonstrated this: outcome=clean,
    #    ops_count=0, $0.0299 cost — would have ticked the flag's
    #    clean-count by 1 toward graduation despite the substrate
    #    never actually firing). Contract consultation forces the
    #    P9.2 predicate_requires_decision_trace_rows guard to
    #    DOWNGRADE 0-op CLEAN to RUNNER, blocking false graduation.
    #
    # 3. JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true — Option C
    #    circuit breaker. When DW topology blocks BG generation
    #    (e.g. Gemma 4 31B stream-stalls), op skips GENERATE phase
    #    entirely + emits ONE clean [CircuitBreaker] log line vs
    #    today's multi-line cascade. Late-detection path remains
    #    the fallback if circuit-breaker raises (try/except wrap
    #    in orchestrator).
    #
    # 4. JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true — §3.6.2 vector #6
    #    producer-loop wiring (2026-05-07). Without this, cmd_run's
    #    interaction-matrix recorder no-ops and /phase9 partners
    #    stays empty regardless of cadence runs. Setting it here
    #    populates .jarvis/graduation_interaction_matrix.jsonl
    #    automatically — operator-binding decision: explicit
    #    cadence-host opt-in (no silent no-op in production).
    #
    # All default OFF in the codebase. Cron sets them locally for this
    # invocation only — no global state mutation.
    cat <<EOF
$BEGIN_MARKER
# Phase 9.1 — Live-Fire Graduation Soak Harness (auto-installed)
# Master flag JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED gates execution.
# Operator pause: export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true
# Contract consultation (P9.2) blocks 0-op false-graduation.
# Circuit breaker (Option C) keeps logs mathematically pure on DW topology block.
# Graduation ledger enabled so parent harness persists clean counts.
# Phase 9 orchestrator enabled so interaction-matrix populates as cadence runs.
$CRON_SCHEDULE cd $REPO_ROOT && JARVIS_CADENCE_KIND=cron /usr/bin/env python3 $REPO_ROOT/scripts/cadence_preflight.py --cadence-kind cron && JARVIS_GRADUATION_LEDGER_ENABLED=true JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true OUROBOROS_BATTLE_SEED_INTENTS=3 /usr/bin/env python3 $HARNESS_SCRIPT run --cost-cap $COST_CAP --max-wall-seconds $WALL_CAP --timeout $TIMEOUT >> $LOG_DIR/$LOG_FILE_TEMPLATE 2>&1
$END_MARKER
EOF
}

preflight() {
    if [[ ! -f "$HARNESS_SCRIPT" ]]; then
        echo -e "${RED}ERROR${RESET} harness script not found: $HARNESS_SCRIPT"
        exit 1
    fi
    if [[ ! -x "$HARNESS_SCRIPT" ]]; then
        chmod +x "$HARNESS_SCRIPT" 2>/dev/null || true
    fi
    mkdir -p "$LOG_DIR"
}

current_crontab() {
    crontab -l 2>/dev/null || echo ""
}

remove_block() {
    current_crontab | awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
        $0 == begin { inblock=1; next }
        $0 == end   { inblock=0; next }
        !inblock    { print }
    '
}

install_cron() {
    preflight
    local new_block
    new_block="$(build_cron_block)"
    local without_block
    without_block="$(remove_block)"
    # Reinstall: existing-entries-without-our-block PLUS our new block.
    {
        # Strip trailing blank lines from existing.
        echo -n "$without_block"
        # Ensure exactly one newline before our block.
        if [[ -n "$without_block" ]] && [[ "${without_block: -1}" != $'\n' ]]; then
            echo
        fi
        echo "$new_block"
    } | crontab -
    echo -e "${GREEN}✓${RESET} crontab installed (schedule: ${BOLD}$CRON_SCHEDULE${RESET})"
    echo -e "${DIM}  cost_cap=\$$COST_CAP wall_cap=${WALL_CAP}s timeout=${TIMEOUT}s${RESET}"
    echo -e "${DIM}  logs: $LOG_DIR/{timestamp}.log${RESET}"
    # Cadence Slice 1 (2026-05-06) — write cadence manifest as
    # the single source of truth for schedule + interval. Slice 3
    # overdue detector reads this; no magic numbers in detection
    # modules. Best-effort: failure logs warning but doesn't block
    # the install (operator can re-run later).
    if write_cadence_manifest; then
        echo -e "${DIM}  cadence_manifest: .jarvis/cadence_manifest.json${RESET}"
    else
        echo -e "${YELLOW}!${RESET} cadence manifest write failed (non-fatal)"
    fi
    echo
    echo -e "${CYAN}Next steps:${RESET}"
    echo "  1. Verify: $0 --status"
    echo "  2. First proof run: $0 --once"
    echo "  3. Operator pause: export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true"
    echo "  4. Check progress: python3 $HARNESS_SCRIPT status"
}

write_cadence_manifest() {
    # Cadence Slice 1 — invoke the canonical CLI seam on the
    # harness so the Python-side manifest substrate
    # (backend/.../graduation/cadence_manifest.py) is the SOLE
    # writer. Composes existing CRON_SCHEDULE / COST_CAP /
    # WALL_CAP / TIMEOUT env vars as `extras` on the manifest —
    # operators get a complete forensic witness without schema
    # bumps.
    cd "$REPO_ROOT" || return 1
    /usr/bin/env python3 "$HARNESS_SCRIPT" write-cadence-manifest \
        --kind cron \
        --schedule "$CRON_SCHEDULE" \
        --installer-version "1.0" \
        --extra "cost_cap_usd=$COST_CAP" \
        --extra "wall_cap_s=$WALL_CAP" \
        --extra "timeout_s=$TIMEOUT" \
        --extra "log_dir=$LOG_DIR" \
        --extra "harness_script=$HARNESS_SCRIPT" \
        > /dev/null 2>&1
}

dry_run() {
    preflight
    echo -e "${BOLD}${CYAN}Cron entry preview${RESET}  ${DIM}(--dry-run; nothing installed)${RESET}"
    echo
    build_cron_block
    echo
    echo -e "${DIM}To install: $0 --install${RESET}"
}

remove_cron() {
    if ! current_crontab | grep -q "$BEGIN_MARKER"; then
        echo -e "${YELLOW}!${RESET} no live-fire soak block found in crontab"
        return 0
    fi
    remove_block | crontab -
    echo -e "${GREEN}✓${RESET} live-fire soak block removed from crontab"
}

run_once() {
    preflight
    echo -e "${BOLD}${CYAN}Running ONE live-fire soak now${RESET}  ${DIM}(--once)${RESET}"
    echo
    cd "$REPO_ROOT"
    # 2026-05-09 — delegate to the canonical wrapper instead of
    # inlining a duplicate env block. The wrapper already sets every
    # JARVIS_* + OUROBOROS_* var the cron + launchd paths use AND
    # loads $REPO_ROOT/.env so DOUBLEWORD_API_KEY / ANTHROPIC_API_KEY
    # reach the subprocess. This is the same single-source-of-truth
    # discipline the launchd plist already honors (line 311 comment:
    # "single source of truth for the env block"). Removing the
    # duplicate inline block here closes the gap that bit the
    # 2026-05-09 21:35 PDT --once attempt — DW connectivity passed
    # raw curl in 500ms but the soak's harness saw 30s timeouts
    # because the parent shell's env never carried the API keys.
    if [[ ! -x "$WRAPPER_SCRIPT" ]]; then
        echo -e "${RED}ERROR${RESET} wrapper not executable: $WRAPPER_SCRIPT"
        exit 1
    fi
    # cadence_preflight enum is {cron, launchd, adhoc} — `--once` is an
    # adhoc one-shot (the wrapper's own default at line 59). Operator
    # override path: setting JARVIS_CADENCE_KIND in the parent env wins.
    JARVIS_CADENCE_KIND="${JARVIS_CADENCE_KIND:-adhoc}" \
        bash "$WRAPPER_SCRIPT" run \
        --cost-cap "$COST_CAP" \
        --max-wall-seconds "$WALL_CAP" \
        --timeout "$TIMEOUT"
}

show_status() {
    echo -e "${BOLD}${CYAN}Crontab status${RESET}"
    if current_crontab | grep -q "$BEGIN_MARKER"; then
        echo -e "${GREEN}✓${RESET} live-fire soak block installed:"
        echo
        current_crontab | awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
            $0 == begin { inblock=1 }
            inblock     { print "    " $0 }
            $0 == end   { inblock=0 }
        '
    else
        echo -e "${YELLOW}!${RESET} live-fire soak block NOT installed"
        echo -e "${DIM}  install with: $0 --install${RESET}"
    fi
    echo
    echo -e "${BOLD}${CYAN}Pause flag${RESET}"
    if [[ "${JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED:-}" == "true" ]]; then
        echo -e "  ${YELLOW}PAUSED${RESET} (JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true)"
    else
        echo -e "  ${GREEN}NOT paused${RESET}"
    fi
    echo
    echo -e "${BOLD}${CYAN}Graduation queue (read-only)${RESET}"
    cd "$REPO_ROOT"
    python3 "$HARNESS_SCRIPT" status 2>&1 | head -30
}

# ---------------------------------------------------------------------------
# Cadence Slice 4 (2026-05-06) — launchd User Agent path.
#
# macOS LaunchAgents run in a less-restricted user TCC context
# than cron, so $HOME/Documents access works without granting
# /usr/sbin/cron Full Disk Access. The plist invokes the SAME
# run_live_fire_graduation_soak.sh wrapper as the cron path —
# single source of truth for the env block (cadence_preflight
# probe + 4 Phase 9 vars + JARVIS_CADENCE_KIND=launchd hint).
#
# StartInterval is derived from CRON_SCHEDULE via the canonical
# manifest parser (cadence_manifest.derive_interval_hint_s).
# Single source of cadence-string truth: same env knob drives
# both cron and launchd paths.
# ---------------------------------------------------------------------------

LAUNCHD_LABEL="com.jarvis.live-fire-soak"
LAUNCHD_PLIST_PATH="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
WRAPPER_SCRIPT="$REPO_ROOT/scripts/run_live_fire_graduation_soak.sh"

derive_launchd_interval_s() {
    # Compose the canonical Python parser. Returns 0 on success
    # with derived seconds on stdout. Falls back to 28800 (8h
    # = the cron default) on parser failure so the plist still
    # installs with a sane interval the operator can hand-edit.
    local result
    result="$(cd "$REPO_ROOT" && /usr/bin/env python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from backend.core.ouroboros.governance.graduation.cadence_manifest import (
    derive_interval_hint_s,
)
v = derive_interval_hint_s('$CRON_SCHEDULE')
if v <= 0:
    sys.exit(1)
print(v)
" 2>/dev/null)"
    if [[ -z "$result" ]]; then
        echo "28800"  # 8h fallback matching CRON_SCHEDULE_DEFAULT
        return
    fi
    echo "$result"
}

build_launchd_plist() {
    local interval_s="$1"
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Phase 9 Cadence Slice 4 (2026-05-06) — Live-Fire Graduation
     Soak User Agent. Reuses run_live_fire_graduation_soak.sh
     so the env block stays the single source of truth.
     Cadence is derived from CRON_SCHEDULE='$CRON_SCHEDULE'.

     Operator commands:
       launchctl load -w  $LAUNCHD_PLIST_PATH
       launchctl unload   $LAUNCHD_PLIST_PATH
       launchctl list | grep $LAUNCHD_LABEL

     Hot-revert: bash scripts/install_live_fire_soak_cron.sh --remove-launchd
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LAUNCHD_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$WRAPPER_SCRIPT</string>
        <string>run</string>
        <string>--cost-cap</string>
        <string>$COST_CAP</string>
        <string>--max-wall-seconds</string>
        <string>$WALL_CAP</string>
        <string>--timeout</string>
        <string>$TIMEOUT</string>
    </array>
    <key>StartInterval</key>
    <integer>$interval_s</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>JARVIS_CADENCE_KIND</key>
        <string>launchd</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/launchd.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/launchd.stderr.log</string>
</dict>
</plist>
EOF
}

install_launchd() {
    preflight
    local interval_s
    interval_s="$(derive_launchd_interval_s)"
    mkdir -p "$(dirname "$LAUNCHD_PLIST_PATH")"
    local plist_body
    plist_body="$(build_launchd_plist "$interval_s")"
    # Idempotent: unload prior copy if present, then write +
    # load. launchctl unload of a missing service is a no-op
    # error; suppress.
    if [[ -f "$LAUNCHD_PLIST_PATH" ]]; then
        launchctl unload "$LAUNCHD_PLIST_PATH" 2>/dev/null || true
    fi
    echo "$plist_body" > "$LAUNCHD_PLIST_PATH"
    if launchctl load -w "$LAUNCHD_PLIST_PATH" 2>/dev/null; then
        echo -e "${GREEN}✓${RESET} launchd User Agent loaded (interval: ${BOLD}${interval_s}s${RESET})"
    else
        echo -e "${YELLOW}!${RESET} plist written but launchctl load failed"
        echo -e "${DIM}  inspect: launchctl load -w $LAUNCHD_PLIST_PATH${RESET}"
    fi
    echo -e "${DIM}  plist:  $LAUNCHD_PLIST_PATH${RESET}"
    echo -e "${DIM}  logs:   $LOG_DIR/launchd.stdout.log${RESET}"
    # Cadence Slice 1 — write the canonical manifest with
    # kind=launchd so the overdue detector reads the same
    # cadence interval the launchd plist enforces.
    if write_launchd_cadence_manifest "$interval_s"; then
        echo -e "${DIM}  cadence_manifest: .jarvis/cadence_manifest.json${RESET}"
    else
        echo -e "${YELLOW}!${RESET} cadence manifest write failed (non-fatal)"
    fi
    echo
    echo -e "${CYAN}Next steps:${RESET}"
    echo "  1. Verify: launchctl list | grep $LAUNCHD_LABEL"
    echo "  2. First proof run: $0 --once"
    echo "  3. Inspect cadence: python3 $HARNESS_SCRIPT status"
}

write_launchd_cadence_manifest() {
    local interval_s="$1"
    cd "$REPO_ROOT" || return 1
    /usr/bin/env python3 "$HARNESS_SCRIPT" write-cadence-manifest \
        --kind launchd \
        --schedule "$interval_s" \
        --interval-hint-s "$interval_s" \
        --installer-version "1.0" \
        --extra "cost_cap_usd=$COST_CAP" \
        --extra "wall_cap_s=$WALL_CAP" \
        --extra "timeout_s=$TIMEOUT" \
        --extra "log_dir=$LOG_DIR" \
        --extra "wrapper_script=$WRAPPER_SCRIPT" \
        --extra "plist_path=$LAUNCHD_PLIST_PATH" \
        --extra "label=$LAUNCHD_LABEL" \
        --extra "cron_schedule_source=$CRON_SCHEDULE" \
        > /dev/null 2>&1
}

launchd_dry_run() {
    preflight
    local interval_s
    interval_s="$(derive_launchd_interval_s)"
    echo -e "${BOLD}${CYAN}Launchd plist preview${RESET}  ${DIM}(--launchd-dry-run; nothing installed)${RESET}"
    echo
    echo -e "${DIM}  StartInterval derived from CRON_SCHEDULE='$CRON_SCHEDULE' → ${interval_s}s${RESET}"
    echo
    build_launchd_plist "$interval_s"
    echo
    echo -e "${DIM}To install: $0 --launchd${RESET}"
}

remove_launchd() {
    if [[ ! -f "$LAUNCHD_PLIST_PATH" ]]; then
        echo -e "${YELLOW}!${RESET} launchd plist not present at $LAUNCHD_PLIST_PATH"
        return 0
    fi
    launchctl unload "$LAUNCHD_PLIST_PATH" 2>/dev/null || true
    rm -f "$LAUNCHD_PLIST_PATH"
    echo -e "${GREEN}✓${RESET} launchd User Agent unloaded + plist removed"
}

main() {
    case "${1:-}" in
        ""|--install)      install_cron ;;
        --dry-run)         dry_run ;;
        --remove)          remove_cron ;;
        --launchd)         install_launchd ;;
        --launchd-dry-run) launchd_dry_run ;;
        --remove-launchd)  remove_launchd ;;
        --once)        run_once ;;
        --status)      show_status ;;
        --help|-h)     usage ;;
        *)
            echo -e "${RED}ERROR${RESET} unknown option: $1"
            usage
            exit 2
            ;;
    esac
}

main "$@"
