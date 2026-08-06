#!/bin/bash
# JARVIS -- Ephemeral Probe for the system.login.screensaver authorization rule.
#
# WHAT THIS MEASURES
# ------------------
# Your lock screen currently uses:
#
#     class = rule
#     rule  = ( use-login-window-ui )
#
# which means loginwindow owns authentication and there is no mechanism chain an
# Authorization Plugin could join. Switching the right to a SecurityAgent-
# evaluated rule is the only way to host a plugin -- but it may sever the
# loginwindow-native paths for Touch ID and Apple Watch unlock.
#
# Nobody should guess at that. This script switches the rule for a bounded
# window so you can test Touch ID and Watch unlock yourself, then puts it back.
#
# WHY IT CANNOT LEAVE YOUR MACHINE MUTATED
# ----------------------------------------
# The revert is armed BEFORE the mutation is applied, as a detached background
# process holding nothing but a sleep and a file path. It survives this script
# crashing, the terminal closing, the SSH session dropping, and the parent shell
# being SIGKILLed -- none of those are conditions it observes. It is a dead man's
# switch, not a cleanup handler, and it is deliberately blind to whether the
# probe "succeeded": a watchdog that shares state with the thing it guards is not
# a watchdog.
#
# Three independent layers put the rule back:
#   1. the detached timer          (survives everything short of power loss)
#   2. an EXIT/INT/TERM trap here  (instant revert on Ctrl-C or normal finish)
#   3. install/uninstall.sh        (manual recovery from Recovery or SSH)
#
# USAGE
#   sudo ./probe_screensaver_rule.sh                 # 60-second window
#   sudo JARVIS_PROBE_WINDOW_S=120 ./probe_...sh     # longer window

set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${_here}/common.sh"

jarvis_require_macos
jarvis_require_root

# --- Tunables (no hardcoded literals in the body) ----------------------------
# Default sized to the protocol this script actually prints: lock, wait for
# Apple Watch to trigger (10-20s on its own), retry Touch ID, test the password,
# unlock, return to the terminal. A 60s window fit about three of those five
# steps, so the first run measured nothing.
PROBE_WINDOW_S="${JARVIS_PROBE_WINDOW_S:-180}"
# The SecurityAgent-evaluated rule we swap in. Named by the stock rule's own
# comment as the supported alternative to use-login-window-ui.
PROBE_RULE_NAME="${JARVIS_PROBE_RULE:-authenticate-session-owner-or-admin}"
# The marker that proves the live rule is the stock, loginwindow-owned one.
STOCK_MARKER="${JARVIS_PROBE_STOCK_MARKER:-use-login-window-ui}"

PROBE_LOG="${JARVIS_STATE_DIR}/probe.log"

case "${PROBE_WINDOW_S}" in
    ''|*[!0-9]*) _jarvis_die "JARVIS_PROBE_WINDOW_S must be a positive integer" ;;
esac
[ "${PROBE_WINDOW_S}" -ge 15 ] || _jarvis_die "window must be >= 15s to be testable"
[ "${PROBE_WINDOW_S}" -le 600 ] || _jarvis_die "window must be <= 600s; this is a probe, not a config change"

# =============================================================================
# 1. BACK UP THE EXACT LIVE XML
# =============================================================================
mkdir -p "${JARVIS_AUTHDB_BACKUP_DIR}"
chmod 700 "${JARVIS_STATE_DIR}" "${JARVIS_AUTHDB_BACKUP_DIR}" 2>/dev/null || true

_stamp="$(date +%Y%m%d-%H%M%S)"
BACKUP="${JARVIS_AUTHDB_BACKUP_DIR}/${JARVIS_AUTH_RIGHT}.${_stamp}.probe.plist"

_jarvis_log "reading live ${JARVIS_AUTH_RIGHT}"
if ! jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" > "${BACKUP}"; then
    rm -f "${BACKUP}"
    _jarvis_die "could not read ${JARVIS_AUTH_RIGHT}; refusing to touch anything"
fi

# =============================================================================
# 2. PRE-FLIGHT -- abort before any mutation if the backup is not trustworthy
# =============================================================================
_jarvis_log "pre-flight: validating backup"

[ -s "${BACKUP}" ] || { rm -f "${BACKUP}"; _jarvis_die "backup is empty; aborting without touching the system"; }

if ! plutil -lint "${BACKUP}" >/dev/null 2>&1; then
    _jarvis_warn "backup did not parse as a plist: ${BACKUP}"
    _jarvis_die "aborting without touching the system"
fi

if ! grep -q "${STOCK_MARKER}" "${BACKUP}"; then
    _jarvis_warn "live rule does not contain '${STOCK_MARKER}'."
    _jarvis_warn "current rule:"
    sed 's/^/    /' "${BACKUP}" >&2
    _jarvis_warn "the machine is not in the stock configuration this probe knows how to restore."
    _jarvis_die "aborting without touching the system"
fi

# Prove the restore path works on this exact file BEFORE relying on it: write the
# backup back over the identical live value. A no-op if it succeeds, and if it
# fails we learn that now rather than 60 seconds from now.
_jarvis_log "pre-flight: verifying the restore path with a no-op write"
if ! jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${BACKUP}" >/dev/null 2>&1; then
    rm -f "${BACKUP}"
    _jarvis_die "restore path does not work on this machine; aborting without mutating"
fi

if ! jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" | grep -q "${STOCK_MARKER}"; then
    _jarvis_die "no-op restore changed the rule unexpectedly; STOP. Restore by hand from ${BACKUP}"
fi

_jarvis_log "pre-flight OK; backup at ${BACKUP}"
printf '%s' "${BACKUP}" > "${JARVIS_AUTHDB_BACKUP_POINTER}"

# =============================================================================
# 3. ARM THE DEAD MAN'S SWITCH  (before the mutation, on purpose)
# =============================================================================
# Holds only a duration and a path. Reads nothing about whether the probe is
# alive, healthy, or finished -- so nothing the probe does can wedge it.
mkdir -p "$(dirname "${PROBE_LOG}")"
nohup bash -c "
    sleep ${PROBE_WINDOW_S}
    for attempt in 1 2 3; do
        if /usr/bin/security authorizationdb write '${JARVIS_AUTH_RIGHT}' < '${BACKUP}' 2>/dev/null; then
            printf '%s dead-man revert OK (attempt %s)\n' \"\$(date -u +%FT%TZ)\" \"\$attempt\" >> '${PROBE_LOG}'
            exit 0
        fi
        sleep 2
    done
    printf '%s DEAD-MAN REVERT FAILED -- restore by hand: security authorizationdb write %s < %s\n' \
        \"\$(date -u +%FT%TZ)\" '${JARVIS_AUTH_RIGHT}' '${BACKUP}' >> '${PROBE_LOG}'
" >/dev/null 2>&1 &
REVERTER_PID=$!
disown "${REVERTER_PID}" 2>/dev/null || true
_jarvis_log "dead man's switch armed (pid ${REVERTER_PID}, fires in ${PROBE_WINDOW_S}s)"

# Layer 2: instant revert on Ctrl-C or normal exit. The timer remains the backstop.
_revert_now() {
    _jarvis_log "reverting ${JARVIS_AUTH_RIGHT} now"
    if jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${BACKUP}" >/dev/null 2>&1; then
        _jarvis_log "reverted"
    else
        _jarvis_warn "REVERT FAILED. Run by hand:"
        _jarvis_warn "  sudo security authorizationdb write ${JARVIS_AUTH_RIGHT} < ${BACKUP}"
        _jarvis_warn "or: sudo ${_here}/uninstall.sh"
    fi
}
trap _revert_now EXIT INT TERM

# =============================================================================
# 4. MUTATE
# =============================================================================
_jarvis_log "switching ${JARVIS_AUTH_RIGHT} -> ${PROBE_RULE_NAME}"
if ! security authorizationdb write "${JARVIS_AUTH_RIGHT}" "${PROBE_RULE_NAME}"; then
    _jarvis_die "mutation failed; trap and dead-man switch will restore the stock rule"
fi

_live_now="$(jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" | tr -d '\n')"
if printf '%s' "${_live_now}" | grep -q "${STOCK_MARKER}"; then
    _jarvis_die "rule still reads as stock after the write; probe is not measuring anything"
fi

# =============================================================================
# 5. THE MEASUREMENT WINDOW
# =============================================================================
cat <<BANNER

================================================================================
  PROBE ACTIVE -- ${PROBE_WINDOW_S} SECONDS
================================================================================
  system.login.screensaver is now: ${PROBE_RULE_NAME}
  (SecurityAgent-evaluated -- the configuration a plugin would need)

  GO TEST NOW, in this order:

    1. Lock the screen            (Ctrl-Cmd-Q)
    2. Does TOUCH ID prompt/work?          -> yes / no
    3. Does APPLE WATCH auto-unlock work?  -> yes / no
    4. Does your PASSWORD still unlock?    -> yes / no   <- must be yes
    5. Unlock and come back here

  The rule reverts automatically when the timer fires, even if this window
  closes, this script dies, or your SSH session drops.

  Revert immediately:  Ctrl-C
  Revert by hand:      sudo security authorizationdb write \\
                         ${JARVIS_AUTH_RIGHT} < ${BACKUP}
  Full recovery:       sudo ${_here}/uninstall.sh
  Dead-man log:        ${PROBE_LOG}
================================================================================

BANNER

_remaining="${PROBE_WINDOW_S}"
while [ "${_remaining}" -gt 0 ]; do
    printf '\r[jarvis-authplugin] reverting in %3ds (Ctrl-C to revert now) ' "${_remaining}" >&2
    sleep 1
    _remaining=$((_remaining - 1))
done
printf '\r%*s\r' 60 '' >&2

# EXIT trap performs the revert; step 6 reports what is actually true afterwards.
trap - EXIT INT TERM
_revert_now

_jarvis_log "final state of ${JARVIS_AUTH_RIGHT}:"
if jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" | grep -q "${STOCK_MARKER}"; then
    _jarvis_log "  STOCK (${STOCK_MARKER}) -- machine is back to how it started"
    rm -f "${JARVIS_AUTHDB_BACKUP_POINTER}"
else
    _jarvis_warn "  NOT STOCK. Restore by hand:"
    _jarvis_warn "    sudo security authorizationdb write ${JARVIS_AUTH_RIGHT} < ${BACKUP}"
    exit 1
fi

# =============================================================================
# 7. CAPTURE THE MEASUREMENT  (a result that lives only in a terminal is not a
#    result -- the first run reverted cleanly and recorded nothing)
# =============================================================================
PROBE_RESULTS="${JARVIS_STATE_DIR}/probe-results.log"

if [ -t 0 ]; then
    printf '\n[jarvis-authplugin] recording the measurement (Enter to skip any answer)\n' >&2

    _ask() {  # _ask <prompt> -> echoes normalised answer
        local reply=""
        read -r -p "  $1 [y/n/skip]: " reply </dev/tty || reply=""
        case "${reply}" in
            [Yy]*) printf 'yes' ;;
            [Nn]*) printf 'no' ;;
            *)     printf 'not-tested' ;;
        esac
    }

    _locked="$(_ask 'Did you actually LOCK the screen during the window?')"
    if [ "${_locked}" != "yes" ]; then
        _jarvis_warn "screen was not locked -- this run measured nothing."
        _jarvis_warn "re-run and lock the screen inside the window:"
        _jarvis_warn "  sudo JARVIS_PROBE_WINDOW_S=${PROBE_WINDOW_S} $0"
    fi

    _touchid="$(_ask 'Did TOUCH ID work?')"
    _watch="$(_ask 'Did APPLE WATCH auto-unlock work?')"
    _password="$(_ask 'Did your PASSWORD unlock?')"

    {
        printf '%s rule=%s window=%ss locked=%s touchid=%s watch=%s password=%s\n' \
            "$(date -u +%FT%TZ)" "${PROBE_RULE_NAME}" "${PROBE_WINDOW_S}" \
            "${_locked}" "${_touchid}" "${_watch}" "${_password}"
    } >> "${PROBE_RESULTS}"

    _jarvis_log "recorded to ${PROBE_RESULTS}"

    if [ "${_password}" = "no" ]; then
        _jarvis_warn "PASSWORD FAILED under SecurityAgent. Do not build the plugin."
    fi
else
    _jarvis_log "non-interactive; skipping capture. Record answers in ${PROBE_RESULTS}"
fi

cat <<'NEXT'

Report back with the four yes/no answers. They decide the architecture:

  Touch ID + Watch SURVIVE  -> the plugin is viable; build it against
                               evaluate-mechanisms with no biometric cost.
  Touch ID or Watch BREAK   -> hosting a plugin costs you native biometrics.
                               That is a real price, and it is your call
                               whether voice unlock is worth paying it.
  PASSWORD fails            -> stop entirely; the SecurityAgent handoff is not
                               safe on this machine and no plugin gets built.

NEXT
