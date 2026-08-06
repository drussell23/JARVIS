#!/bin/bash
# JARVIS Authorization Plugin -- shared constants and helpers.
#
# Sourced by install.sh and uninstall.sh so both agree on every path, label and
# right name. There is exactly one definition of each; neither script may
# introduce a literal of its own.
#
# Deliberately POSIX-ish bash with no dependency on the repo, python, or a
# virtualenv: uninstall must run from a Recovery shell or a bare SSH session on
# a machine whose screen will not unlock.

set -euo pipefail

# --- Identity ---------------------------------------------------------------
JARVIS_PLUGIN_NAME="JARVISUnlock"
JARVIS_BUNDLE_NAME="${JARVIS_PLUGIN_NAME}.bundle"
JARVIS_MECHANISM="${JARVIS_PLUGIN_NAME}:grant,privileged"
JARVIS_BROKER_LABEL="com.jarvis.unlockbroker"

# --- Authorization right we participate in ----------------------------------
# Only the screensaver right. system.login.console is deliberately NEVER
# touched: a defect there costs you login, not just unlock, and the difference
# is the difference between "annoying" and "boot from Recovery".
JARVIS_AUTH_RIGHT="system.login.screensaver"

# --- Filesystem layout ------------------------------------------------------
JARVIS_PLUGIN_DIR="/Library/Security/SecurityAgentPlugins"
JARVIS_PLUGIN_PATH="${JARVIS_PLUGIN_DIR}/${JARVIS_BUNDLE_NAME}"
JARVIS_BROKER_BIN="/usr/local/libexec/jarvis-unlock-broker"
JARVIS_BROKER_PLIST="/Library/LaunchDaemons/${JARVIS_BROKER_LABEL}.plist"
JARVIS_STATE_DIR="/Library/Application Support/JARVIS/authplugin"
JARVIS_AUTHDB_BACKUP_DIR="${JARVIS_STATE_DIR}/authdb-backup"
# Pointer file naming the backup taken by the most recent install. Uninstall
# restores from whatever this names, so a reinstall cannot orphan the original.
JARVIS_AUTHDB_BACKUP_POINTER="${JARVIS_AUTHDB_BACKUP_DIR}/current"

# --- Logging ----------------------------------------------------------------
_jarvis_log()  { printf '[jarvis-authplugin] %s\n' "$*" >&2; }
_jarvis_warn() { printf '[jarvis-authplugin][warn] %s\n' "$*" >&2; }
_jarvis_die()  { printf '[jarvis-authplugin][fatal] %s\n' "$*" >&2; exit 1; }

# --- Guards -----------------------------------------------------------------
jarvis_require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        _jarvis_die "must run as root (try: sudo $0)"
    fi
}

jarvis_require_macos() {
    if [ "$(uname -s)" != "Darwin" ]; then
        _jarvis_die "macOS only"
    fi
}

# Read the current authorization rule for a right to stdout.
# Returns non-zero if the right cannot be read.
jarvis_authdb_read() {
    local right="$1"
    security authorizationdb read "$right" 2>/dev/null
}

# Write a rule plist (path) into the authorization database for a right.
jarvis_authdb_write() {
    local right="$1" plist="$2"
    [ -f "$plist" ] || _jarvis_die "rule plist not found: $plist"
    security authorizationdb write "$right" < "$plist"
}

# --- Shared lifecycle helpers ------------------------------------------------
# Defined here because install.sh and uninstall.sh both need them and a second
# copy of "how do we put the authorization rule back" is the last thing this
# system should have.

# Restore the authorization rule from the backup named by the pointer file.
# Returns 0 on success, 1 if there is no usable backup (caller decides what
# that means -- uninstall falls back to stripping in place; install aborts).
jarvis_restore_auth_rule_from_pointer() {
    local backup
    [ -f "${JARVIS_AUTHDB_BACKUP_POINTER}" ] || return 1

    backup="$(cat "${JARVIS_AUTHDB_BACKUP_POINTER}" 2>/dev/null || true)"
    [ -n "${backup}" ] && [ -f "${backup}" ] || return 1

    # Never write a backup we cannot parse. Restoring garbage into the
    # authorization database is the one way recovery could make things worse
    # than it found them.
    plutil -lint "${backup}" >/dev/null 2>&1 || return 1

    jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${backup}" >/dev/null 2>&1 || return 1
    rm -f "${JARVIS_AUTHDB_BACKUP_POINTER}"
    return 0
}

# Does the live rule currently name our mechanism?
jarvis_rule_references_plugin() {
    jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" 2>/dev/null | grep -q "${JARVIS_PLUGIN_NAME}"
}

# Wait for a system service to be fully gone.
#
# `launchctl bootout` returns before teardown completes, so bootstrapping
# immediately afterwards races the unload and fails with EIO (5). Polling the
# actual condition is the fix; a fixed sleep would be a guess that is either too
# short on a loaded machine or wasted time on an idle one.
jarvis_wait_for_service_gone() {
    local label="$1"
    local timeout="${JARVIS_SERVICE_SETTLE_TIMEOUT_S:-10}"
    local interval="${JARVIS_SERVICE_POLL_INTERVAL_S:-0.2}"
    local waited=0

    while launchctl print "system/${label}" >/dev/null 2>&1; do
        # `bc` is not guaranteed; integer tenths keep this dependency-free so it
        # still works from a Recovery shell.
        if [ "${waited}" -ge $(( timeout * 10 )) ]; then
            return 1
        fi
        sleep "${interval}"
        waited=$(( waited + 2 ))
    done
    return 0
}
