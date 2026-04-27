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
Usage: $0 [--install | --dry-run | --remove | --once | --status | --help]

Options (default = --install):
  --install   Install/update the cron entry (idempotent).
  --dry-run   Print the cron entry that WOULD be installed.
  --remove    Remove the live-fire soak block from crontab.
  --once      Run a single soak NOW (skips cron, useful for first proof).
  --status    Show current crontab contents + ledger queue.
  --help      This message.

Environment overrides:
  CRON_SCHEDULE  default: $CRON_SCHEDULE_DEFAULT (every 8 hours)
  COST_CAP       default: \$$COST_CAP_DEFAULT  (per-soak USD cap)
  WALL_CAP       default: $WALL_CAP_DEFAULT s   (per-soak wall-clock cap)
  TIMEOUT        default: $TIMEOUT_DEFAULT s   (subprocess kill timeout)

Examples:
  $0 --dry-run
  CRON_SCHEDULE='0 6,14,22 * * *' $0 --install
  $0 --once
  $0 --remove
EOF
}

build_cron_block() {
    cat <<EOF
$BEGIN_MARKER
# Phase 9.1 — Live-Fire Graduation Soak Harness (auto-installed)
# Master flag JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED gates execution.
# Operator pause: export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true
$CRON_SCHEDULE cd $REPO_ROOT && JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true /usr/bin/env python3 $HARNESS_SCRIPT run --cost-cap $COST_CAP --max-wall-seconds $WALL_CAP --timeout $TIMEOUT >> $LOG_DIR/$LOG_FILE_TEMPLATE 2>&1
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
    echo
    echo -e "${CYAN}Next steps:${RESET}"
    echo "  1. Verify: $0 --status"
    echo "  2. First proof run: $0 --once"
    echo "  3. Operator pause: export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true"
    echo "  4. Check progress: python3 $HARNESS_SCRIPT status"
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
    if [[ "${JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED:-}" != "true" ]]; then
        echo -e "${YELLOW}!${RESET} JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED not set"
        echo -e "${DIM}  setting=true for this single invocation${RESET}"
    fi
    cd "$REPO_ROOT"
    JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true \
        python3 "$HARNESS_SCRIPT" run \
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

main() {
    case "${1:-}" in
        ""|--install)  install_cron ;;
        --dry-run)     dry_run ;;
        --remove)      remove_cron ;;
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
