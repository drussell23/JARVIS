#!/bin/bash
# JARVIS Authorization Plugin -- complete removal.
#
# WRITTEN BEFORE THE PLUGIN IT REMOVES, ON PURPOSE.
#
# This script is the recovery path for a machine whose screen will not unlock.
# Everything about it is shaped by that: it must run from a Recovery Terminal or
# a bare SSH session, must not depend on the JARVIS repo, python, a virtualenv,
# or any state the plugin itself wrote beyond a single backup file, and must
# leave the system in the stock configuration even if it is run twice, run
# half-way through a failed install, or run when nothing is installed at all.
#
#   ssh you@mac 'sudo /path/to/uninstall.sh'
#
# ORDER MATTERS. The authorization database is restored FIRST. If the machine is
# wedged, the rule is what is wedging it -- removing the bundle first would leave
# a rule pointing at a mechanism that no longer exists, which is a worse state
# than either the installed or the stock configuration.

set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${_here}/common.sh"

jarvis_require_macos
jarvis_require_root

_failures=0
_note_failure() { _jarvis_warn "$1"; _failures=$((_failures + 1)); }

_jarvis_log "starting removal"

# =============================================================================
# 1. RESTORE THE AUTHORIZATION RULE  (first -- this is what unwedges a machine)
# =============================================================================
restore_auth_rule() {
    # The backup-pointer restore lives in common.sh because install.sh needs the
    # identical operation when it has to abandon a half-finished install. Two
    # copies of "how do we put the authorization rule back" is the last thing
    # this system should have -- they would drift, and the one that drifted would
    # be discovered by someone whose screen will not unlock.
    #
    # Every path this function can take is a REPAIR, so it degrades rather than
    # aborting: a failed restore falls through to stripping our mechanism out of
    # whatever rule is live.
    if jarvis_restore_auth_rule_from_pointer; then
        _jarvis_log "authorization rule restored from backup"
        return
    fi

    _jarvis_warn "no usable backup (absent pointer, missing file, or unparseable plist)"
    _remove_mechanism_in_place
}

# Fallback: no usable backup. Strip only our own mechanism from whatever rule is
# currently live, leaving every other entry untouched. Never invents a rule.
_remove_mechanism_in_place() {
    local current
    if ! current="$(jarvis_authdb_read "${JARVIS_AUTH_RIGHT}")"; then
        _note_failure "cannot read ${JARVIS_AUTH_RIGHT}; leaving it alone"
        return
    fi

    if ! printf '%s' "${current}" | grep -q "${JARVIS_PLUGIN_NAME}"; then
        _jarvis_log "${JARVIS_AUTH_RIGHT} does not reference ${JARVIS_PLUGIN_NAME}; nothing to strip"
        return
    fi

    local scratch
    scratch="$(mktemp -t jarvis-authdb)" || { _note_failure "mktemp failed"; return; }
    printf '%s' "${current}" > "${scratch}"

    # Delete every mechanisms[] entry whose value mentions our plugin. Uses
    # PlistBuddy (present in the base OS and in Recovery) rather than python,
    # which Recovery does not ship.
    local idx entry
    idx=0
    while entry="$(/usr/libexec/PlistBuddy -c "Print :mechanisms:${idx}" "${scratch}" 2>/dev/null)"; do
        case "${entry}" in
            *"${JARVIS_PLUGIN_NAME}"*)
                /usr/libexec/PlistBuddy -c "Delete :mechanisms:${idx}" "${scratch}" >/dev/null 2>&1 || true
                # Do not advance: entries shift down after a delete.
                ;;
            *)
                idx=$((idx + 1))
                ;;
        esac
    done

    if jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${scratch}"; then
        _jarvis_log "stripped ${JARVIS_PLUGIN_NAME} from ${JARVIS_AUTH_RIGHT}"
    else
        _note_failure "could not write stripped rule for ${JARVIS_AUTH_RIGHT}"
    fi
    rm -f "${scratch}"
}

restore_auth_rule

# =============================================================================
# 2. STOP AND REMOVE THE GRANT BROKER
# =============================================================================
remove_broker() {
    if launchctl print "system/${JARVIS_BROKER_LABEL}" >/dev/null 2>&1; then
        _jarvis_log "unloading ${JARVIS_BROKER_LABEL}"
        launchctl bootout "system/${JARVIS_BROKER_LABEL}" 2>/dev/null \
            || launchctl unload "${JARVIS_BROKER_PLIST}" 2>/dev/null \
            || _note_failure "could not unload ${JARVIS_BROKER_LABEL}"
    else
        _jarvis_log "${JARVIS_BROKER_LABEL} is not loaded"
    fi

    for path in "${JARVIS_BROKER_PLIST}" "${JARVIS_BROKER_BIN}"; do
        if [ -e "${path}" ]; then
            rm -f "${path}" && _jarvis_log "removed ${path}" \
                || _note_failure "could not remove ${path}"
        fi
    done
}

remove_broker

# =============================================================================
# 3. REMOVE THE PLUGIN BUNDLE
# =============================================================================
if [ -e "${JARVIS_PLUGIN_PATH}" ]; then
    rm -rf "${JARVIS_PLUGIN_PATH}" && _jarvis_log "removed ${JARVIS_PLUGIN_PATH}" \
        || _note_failure "could not remove ${JARVIS_PLUGIN_PATH}"
else
    _jarvis_log "${JARVIS_PLUGIN_PATH} not present"
fi

# =============================================================================
# 4. VERIFY -- report what is actually true, not what we attempted
# =============================================================================
_jarvis_log "verifying"

if [ -e "${JARVIS_PLUGIN_PATH}" ]; then
    _note_failure "plugin bundle still present at ${JARVIS_PLUGIN_PATH}"
fi
if [ -e "${JARVIS_BROKER_PLIST}" ]; then
    _note_failure "broker plist still present at ${JARVIS_BROKER_PLIST}"
fi
if jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" | grep -q "${JARVIS_PLUGIN_NAME}"; then
    _note_failure "${JARVIS_AUTH_RIGHT} still references ${JARVIS_PLUGIN_NAME}"
fi

# The state directory is kept: it holds the authdb backups, which are the only
# record of the pre-install configuration. Removing them would make a future
# recovery harder, and they contain no secrets.
_jarvis_log "authdb backups retained at ${JARVIS_AUTHDB_BACKUP_DIR}"

if [ "${_failures}" -ne 0 ]; then
    _jarvis_warn "removal finished with ${_failures} problem(s) -- see above"
    _jarvis_warn "the screen unlock right is: $(jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" | tr -d '\n' | cut -c1-200)"
    exit 1
fi

_jarvis_log "removal complete; ${JARVIS_AUTH_RIGHT} is stock"
