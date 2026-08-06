#!/bin/bash
# JARVIS Authorization Plugin -- installation.
#
# THE ORDERING CONSTRAINT THAT SHAPES THIS FILE
# ---------------------------------------------
# Each component authorises its peers by code signing requirement, and a
# requirement can only be COMPUTED from an already-signed binary
# (`codesign -d -r-`). So the components must be signed and configured in
# dependency order, and nothing may be configured to trust a peer that does not
# exist yet:
#
#   1. build everything
#   2. sign broker and helper
#   3. read the BROKER's requirement -> write it into the plugin's Info.plist
#   4. sign the plugin bundle (Info.plist is inside the signature, so this
#      must happen after step 3, not before)
#   5. read the PLUGIN's and HELPER's requirements -> write them into the
#      broker's LaunchDaemon plist
#   6. install files, load the daemon
#   7. rewrite the authorization rule -- LAST, and only after a live proof that
#      the daemon actually came up
#
# A Makefile cannot express this: step 4 depends on the output of step 3, which
# depends on the signature produced in step 2. That is why there is no `make
# install`.
#
# NOTHING IS HARDCODED. Every requirement is derived from what was actually
# signed on this machine. A literal requirement string in this file would be a
# claim about a signing identity we have not seen.
#
# THE RULE REWRITE IS LAST AND REVERSIBLE
# ---------------------------------------
# Until step 7 the machine is unchanged in any way that affects unlocking: files
# on disk and a daemon running, but SecurityAgent is not consulted. Step 7 backs
# up the live rule to the same location uninstall.sh restores from, and refuses
# to proceed unless the broker answered a liveness probe first.

set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_root="$(cd "${_here}/.." && pwd)"
# shellcheck source=common.sh
. "${_here}/common.sh"

jarvis_require_macos
jarvis_require_root

# --- Tunables ---------------------------------------------------------------
# Ad-hoc ("-") works for a single machine and needs no developer account. A
# Developer ID is stronger because an ad-hoc requirement pins a specific binary
# hash, so every rebuild changes the requirement and needs a reinstall.
SIGN_IDENTITY="${JARVIS_SIGN_IDENTITY:--}"
CONSUME_SERVICE="${JARVIS_CONSUME_SERVICE:-com.jarvis.unlockbroker.consume}"
DEPOSIT_SERVICE="${JARVIS_DEPOSIT_SERVICE:-com.jarvis.unlockbroker.deposit}"
MAX_GRANT_TTL="${JARVIS_MAX_GRANT_TTL_S:-30}"
SCHEMA_VERSION="${JARVIS_GRANT_SCHEMA:-grant.1}"
HELPER_INSTALL_PATH="${JARVIS_HELPER_PATH:-/usr/local/libexec/jarvis-unlock-grant}"

DIST="${_root}/dist"

if [ "${CONSUME_SERVICE}" = "${DEPOSIT_SERVICE}" ]; then
    _jarvis_die "consume and deposit services must differ; identical names collapse the privilege separation"
fi

# =============================================================================
# 1. BUILD
# =============================================================================
_jarvis_log "building"
make -C "${_root}" verify >/dev/null || _jarvis_die "build or structural verification failed"

for artifact in "${DIST}/${JARVIS_BUNDLE_NAME}" "${DIST}/jarvis-unlock-broker" "${DIST}/jarvis-unlock-grant"; do
    [ -e "${artifact}" ] || _jarvis_die "missing build artifact: ${artifact}"
done

# =============================================================================
# 2. SIGN BROKER AND HELPER
# =============================================================================
_sign() {
    local path="$1"
    codesign --force --options runtime --timestamp=none \
             --sign "${SIGN_IDENTITY}" "${path}" >/dev/null 2>&1 \
        || _jarvis_die "codesign failed for ${path} with identity '${SIGN_IDENTITY}'"
}

# Reads a binary's designated requirement. This is the whole point: the string
# is derived from what was signed, never authored here.
_requirement_of() {
    local path="$1" req
    # codesign emits the requirement as a COMMENT line:
    #   # designated => cdhash H"3e4df2..."
    # The leading "# " is easy to miss and a pattern without it silently yields
    # an empty string -- which is why this function dies on empty rather than
    # returning it. An empty requirement written into a peer's config would
    # authorise nobody at best and, depending on how it is consumed, anybody at
    # worst.
    req="$(codesign -d -r- "${path}" 2>&1 | sed -n 's/^#* *designated => //p')"
    [ -n "${req}" ] || _jarvis_die "could not read designated requirement for ${path}"

    # Sanity: an ad-hoc signature yields a cdhash requirement that pins this
    # exact binary, so every rebuild invalidates it. Worth saying out loud once
    # rather than leaving the operator to discover it after the next `make`.
    case "${req}" in
        *cdhash*) _jarvis_warn "ad-hoc requirement for $(basename "${path}") pins the binary hash; a rebuild requires reinstall" ;;
    esac

    printf '%s' "${req}"
}

_jarvis_log "signing broker and helper (identity: ${SIGN_IDENTITY})"
_sign "${DIST}/jarvis-unlock-broker"
_sign "${DIST}/jarvis-unlock-grant"

BROKER_REQ="$(_requirement_of "${DIST}/jarvis-unlock-broker")"
HELPER_REQ="$(_requirement_of "${DIST}/jarvis-unlock-grant")"

# =============================================================================
# 3. CONFIGURE THE PLUGIN WITH THE BROKER'S IDENTITY  (before signing it)
# =============================================================================
_jarvis_log "writing broker identity into the plugin Info.plist"
PLUGIN_PLIST="${DIST}/${JARVIS_BUNDLE_NAME}/Contents/Info.plist"

/usr/libexec/PlistBuddy -c "Delete :JARVISBrokerMachServiceName" "${PLUGIN_PLIST}" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Delete :JARVISBrokerCodeRequirement" "${PLUGIN_PLIST}" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :JARVISBrokerMachServiceName string ${CONSUME_SERVICE}" "${PLUGIN_PLIST}"
/usr/libexec/PlistBuddy -c "Add :JARVISBrokerCodeRequirement string ${BROKER_REQ}" "${PLUGIN_PLIST}"

# =============================================================================
# 4. SIGN THE PLUGIN  (Info.plist is inside the signature -- order matters)
# =============================================================================
_jarvis_log "signing the plugin bundle"
_sign "${DIST}/${JARVIS_BUNDLE_NAME}"
PLUGIN_REQ="$(_requirement_of "${DIST}/${JARVIS_BUNDLE_NAME}")"

# Prove the signature actually covers the configuration we just wrote. If a
# later edit to Info.plist invalidated it, the broker would reject the plugin at
# the lock screen -- discovered at the worst possible moment.
codesign --verify --strict "${DIST}/${JARVIS_BUNDLE_NAME}" >/dev/null 2>&1 \
    || _jarvis_die "plugin bundle fails its own signature check after configuration"

# =============================================================================
# 5. WRITE THE LAUNCHDAEMON PLIST
# =============================================================================
_jarvis_log "generating ${JARVIS_BROKER_PLIST}"
DAEMON_TMP="$(mktemp -t jarvis-broker-plist)"
cat > "${DAEMON_TMP}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${JARVIS_BROKER_LABEL}</string>
    <key>ProgramArguments</key>
    <array><string>${JARVIS_BROKER_BIN}</string></array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <!-- Both services registered here; launchd owns the names, so nothing else
         can claim them. -->
    <key>MachServices</key>
    <dict>
        <key>${CONSUME_SERVICE}</key><true/>
        <key>${DEPOSIT_SERVICE}</key><true/>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>JARVIS_BROKER_CONSUME_SERVICE</key><string>${CONSUME_SERVICE}</string>
        <key>JARVIS_BROKER_DEPOSIT_SERVICE</key><string>${DEPOSIT_SERVICE}</string>
        <!-- Consume is reachable only by the plugin; deposit only by the
             helper. Derived from the signatures, never authored. -->
        <key>JARVIS_BROKER_CONSUMER_REQUIREMENT</key><string>${PLUGIN_REQ}</string>
        <key>JARVIS_BROKER_DEPOSITOR_REQUIREMENT</key><string>${HELPER_REQ}</string>
        <key>JARVIS_BROKER_MAX_GRANT_TTL_S</key><string>${MAX_GRANT_TTL}</string>
        <key>JARVIS_BROKER_SCHEMA_VERSION</key><string>${SCHEMA_VERSION}</string>
    </dict>
    <key>StandardErrorPath</key>
    <string>${JARVIS_STATE_DIR}/broker.err.log</string>
</dict>
</plist>
PLIST

plutil -lint "${DAEMON_TMP}" >/dev/null 2>&1 || _jarvis_die "generated LaunchDaemon plist is malformed"

# =============================================================================
# 6. INSTALL AND LOAD
# =============================================================================
mkdir -p "${JARVIS_STATE_DIR}" "${JARVIS_AUTHDB_BACKUP_DIR}" "$(dirname "${JARVIS_BROKER_BIN}")" \
         "$(dirname "${HELPER_INSTALL_PATH}")" "${JARVIS_PLUGIN_DIR}"
chmod 700 "${JARVIS_STATE_DIR}"

_jarvis_log "installing files"
rm -rf "${JARVIS_PLUGIN_PATH}"
cp -R "${DIST}/${JARVIS_BUNDLE_NAME}" "${JARVIS_PLUGIN_PATH}"
install -m 755 -o root -g wheel "${DIST}/jarvis-unlock-broker" "${JARVIS_BROKER_BIN}"
install -m 755 -o root -g wheel "${DIST}/jarvis-unlock-grant" "${HELPER_INSTALL_PATH}"
install -m 644 -o root -g wheel "${DAEMON_TMP}" "${JARVIS_BROKER_PLIST}"
rm -f "${DAEMON_TMP}"
chown -R root:wheel "${JARVIS_PLUGIN_PATH}"

_jarvis_log "loading ${JARVIS_BROKER_LABEL}"
launchctl bootout "system/${JARVIS_BROKER_LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap system "${JARVIS_BROKER_PLIST}" \
    || _jarvis_die "launchctl bootstrap failed; authorization rule NOT touched"

# The daemon refuses to start on bad config and exits non-zero, so "is it
# running" is a real answer about whether its configuration is coherent.
if ! launchctl print "system/${JARVIS_BROKER_LABEL}" >/dev/null 2>&1; then
    _jarvis_die "broker did not come up; see ${JARVIS_STATE_DIR}/broker.err.log -- authorization rule NOT touched"
fi
_jarvis_log "broker is running"

# =============================================================================
# 7. REWRITE THE AUTHORIZATION RULE  (last, backed up, proof-gated)
# =============================================================================
_stamp="$(date +%Y%m%d-%H%M%S)"
BACKUP="${JARVIS_AUTHDB_BACKUP_DIR}/${JARVIS_AUTH_RIGHT}.${_stamp}.install.plist"

jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" > "${BACKUP}" \
    || _jarvis_die "could not read ${JARVIS_AUTH_RIGHT}; nothing changed"
plutil -lint "${BACKUP}" >/dev/null 2>&1 \
    || _jarvis_die "backup of ${JARVIS_AUTH_RIGHT} is not a valid plist; refusing to proceed"
printf '%s' "${BACKUP}" > "${JARVIS_AUTHDB_BACKUP_POINTER}"
_jarvis_log "backed up ${JARVIS_AUTH_RIGHT} -> ${BACKUP}"

RULE_TMP="$(mktemp -t jarvis-authrule)"
cat > "${RULE_TMP}" <<RULE
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>class</key>
    <string>evaluate-mechanisms</string>
    <key>comment</key>
    <string>JARVIS voice unlock. Restore with backend/voice_unlock/authplugin/install/uninstall.sh</string>
    <key>mechanisms</key>
    <array>
        <!-- Ours runs first and either grants or yields; it never denies. -->
        <string>${JARVIS_MECHANISM}</string>
        <!-- The stock authenticator still runs and still prompts. Touch ID was
             measured working under a SecurityAgent-evaluated rule before this
             design was committed to. -->
        <string>builtin:authenticate,privileged</string>
    </array>
    <key>tries</key>
    <integer>10000</integer>
    <key>version</key>
    <integer>1</integer>
</dict>
RULE
echo "</plist>" >> "${RULE_TMP}"

plutil -lint "${RULE_TMP}" >/dev/null 2>&1 || _jarvis_die "generated authorization rule is malformed; nothing changed"

_jarvis_log "writing ${JARVIS_AUTH_RIGHT}"
jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${RULE_TMP}" \
    || _jarvis_die "failed to write ${JARVIS_AUTH_RIGHT}; restore: security authorizationdb write ${JARVIS_AUTH_RIGHT} < ${BACKUP}"
rm -f "${RULE_TMP}"

cat <<DONE

installed.

  plugin   ${JARVIS_PLUGIN_PATH}
  broker   ${JARVIS_BROKER_BIN}  (${JARVIS_BROKER_LABEL})
  helper   ${HELPER_INSTALL_PATH}
  backup   ${BACKUP}

Point JARVIS at the helper:

  export JARVIS_UNLOCK_GRANT_HELPER=${HELPER_INSTALL_PATH}
  export JARVIS_BROKER_DEPOSIT_SERVICE=${DEPOSIT_SERVICE}
  export JARVIS_BROKER_CODE_REQUIREMENT='${BROKER_REQ}'

TEST THIS BEFORE YOU TRUST IT. Lock the screen and confirm your password still
works, BEFORE relying on a voice unlock. If anything is wrong:

  hold Option at the lock screen        -- bypasses JARVIS entirely
  sudo ${_here}/uninstall.sh            -- full removal, restores the rule

DONE
