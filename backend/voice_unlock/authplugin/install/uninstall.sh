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
# The whole operation -- restore from the backup pointer, falling back to
# stripping our mechanism out of whatever is live -- lives in common.sh. It moved
# there when the sentinel needed the identical repair. Two implementations of
# "how do we get our mechanism out of the lock screen" would drift, and the copy
# that drifted would be discovered by whoever it failed, at a machine that will
# not unlock.
if ! jarvis_revert_auth_rule; then
    _note_failure "could not remove ${JARVIS_PLUGIN_NAME} from ${JARVIS_AUTH_RIGHT}"
fi

# =============================================================================
# 1b. DISARM THE SENTINEL
# =============================================================================
# After the rule is clean, never before. The sentinel's whole purpose is to pull
# our mechanism out of a chain it should not be in; removing it first would open
# exactly the unwatched window this uninstall is walking through.
#
# The sanctioned-shape record goes too. Leaving it behind would tell a future
# sentinel that a rule naming us had been proven, when nothing had.
remove_sentinel() {
    if launchctl print "system/${JARVIS_SENTINEL_LABEL}" >/dev/null 2>&1; then
        _jarvis_log "disarming ${JARVIS_SENTINEL_LABEL}"
        launchctl bootout "system/${JARVIS_SENTINEL_LABEL}" 2>/dev/null \
            || _note_failure "could not unload ${JARVIS_SENTINEL_LABEL}"
        jarvis_wait_for_service_gone "${JARVIS_SENTINEL_LABEL}" || true
    else
        _jarvis_log "${JARVIS_SENTINEL_LABEL} is not loaded"
    fi

    rm -f "${JARVIS_SENTINEL_PLIST}" "${JARVIS_SANCTIONED_SHAPE_FILE}" 2>/dev/null || true

    # The tools directory last, and only its own files: it holds the copy of
    # uninstall.sh that may be the very script running right now. Deleting a
    # running bash script is safe on macOS -- the interpreter holds the inode --
    # but removing the directory wholesale would take verify.sh with it before
    # anyone could use it to check this removal worked.
    if [ -d "${JARVIS_SYSTEM_TOOLS_DIR}" ]; then
        for _tool in ${JARVIS_SYSTEM_TOOLS}; do
            rm -f "${JARVIS_SYSTEM_TOOLS_DIR}/${_tool}" 2>/dev/null || true
        done
        rmdir "${JARVIS_SYSTEM_TOOLS_DIR}" 2>/dev/null || true
    fi
}

remove_sentinel

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
