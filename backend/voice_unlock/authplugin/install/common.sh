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
