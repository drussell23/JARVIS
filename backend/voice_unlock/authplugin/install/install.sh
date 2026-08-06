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
# NOTE: the root check deliberately happens AFTER argument parsing, below.
# `--help` that requires sudo is a help message nobody reads, and a `--dry-run`
# that demands privileges it does not need is a safety feature people skip.

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
# MODES
# =============================================================================
# There are two useful places to stop short of a full install, and they answer
# different questions:
#
#   --dry-run     Build, sign, derive every requirement, generate both plists,
#                 validate all of it -- and mutate NOTHING. Answers "would this
#                 install produce a coherent configuration?" Safe to run at any
#                 time on any machine.
#
#   --skip-authdb Everything except the authorization rule. Files installed,
#                 daemon loaded, and the deposit path exercised against the LIVE
#                 broker. Answers "does the XPC channel actually work, and does
#                 the code signing validation actually accept the real peer?"
#                 The lock screen is untouched, so a failure here costs nothing.
#
# The second is the one that matters. Until it passes, no signature in this
# system has ever been checked by a live peer, and a full install would make its
# first integration test the run that rewrites the authorization database.
DRY_RUN=0
SKIP_AUTHDB=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)     DRY_RUN=1; SKIP_AUTHDB=1 ;;
        --skip-authdb) SKIP_AUTHDB=1 ;;
        -h|--help)
            cat <<USAGE
usage: install.sh [--dry-run | --skip-authdb]

  (no flags)      full install, including the authorization rule
  --skip-authdb   install and load, exercise the deposit path, leave the
                  lock screen completely untouched
  --dry-run       mutate nothing; prove the configuration would be coherent

Environment: JARVIS_SIGN_IDENTITY, JARVIS_CONSUME_SERVICE,
             JARVIS_DEPOSIT_SERVICE, JARVIS_MAX_GRANT_TTL_S,
             JARVIS_GRANT_SCHEMA, JARVIS_HELPER_PATH
USAGE
            exit 0 ;;
        *) _jarvis_die "unknown argument: $1 (try --help)" ;;
    esac
    shift
done

if [ "${DRY_RUN}" -eq 1 ]; then
    # Signing is NOT skipped, and cannot be: a requirement is derived from a
    # signature, so refusing to sign would mean refusing to check the one thing
    # a dry run exists to check. Signatures land on build output under dist/,
    # which is scratch. Nothing outside the repo is touched.
    _jarvis_log "DRY RUN -- nothing outside ${_root}/dist will be modified"
    # Deliberately NOT requiring root: a dry run writes only to build output, and
    # making it need sudo would be asking for privilege to do nothing with it.
else
    jarvis_require_root
fi

# Narrates an action that a dry run declines to take. Returns 1 when skipped, so
# callers read as `_would "..." || <do it>` and the skip is impossible to forget.
_would() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[jarvis-authplugin][dry-run] would %s\n' "$*" >&2
        return 0
    fi
    return 1
}

# =============================================================================
# 1. BUILD
# =============================================================================
_jarvis_log "building"
make -C "${_root}" verify >/dev/null || _jarvis_die "build or structural verification failed"

for artifact in "${DIST}/${JARVIS_BUNDLE_NAME}" "${DIST}/jarvis-unlock-broker" "${DIST}/jarvis-unlock-grant"; do
    [ -e "${artifact}" ] || _jarvis_die "missing build artifact: ${artifact}"
done

# =============================================================================
# 1b. STAGE OUTSIDE THE REPOSITORY BEFORE SIGNING
# =============================================================================
# codesign refuses to sign anything carrying "resource fork, Finder information,
# or similar detritus". This repository lives inside a fileprovider-managed tree,
# so directories acquire com.apple.FinderInfo and com.apple.fileprovider.fpfs#P
# merely by existing -- and stripping them does not stick: the daemon re-adds
# them asynchronously, so a clear-then-sign in place is a race that codesign
# usually loses.
#
# Clearing harder is the wrong answer; there is no number of `xattr -cr` calls
# that wins against a daemon whose job is to put them back. The signature-
# sensitive work moves out of its domain instead. Everything downstream operates
# on the staging copy, so the reassignment below is the only change needed.
STAGE="$(mktemp -d -t jarvis-authplugin-stage)" || _jarvis_die "could not create staging directory"
trap 'rm -rf "${STAGE}"' EXIT

cp -R "${DIST}/${JARVIS_BUNDLE_NAME}" "${DIST}/jarvis-unlock-broker" \
      "${DIST}/jarvis-unlock-grant" "${STAGE}/" \
    || _jarvis_die "could not stage build artifacts into ${STAGE}"
xattr -cr "${STAGE}" 2>/dev/null || true

# Prove the staging area is actually free of the attributes that block signing,
# rather than assuming a different filesystem behaves differently.
_detritus="$(xattr -r "${STAGE}" 2>/dev/null | grep -cE 'FinderInfo|fileprovider' || true)"
if [ "${_detritus}" -ne 0 ]; then
    _jarvis_die "staging area ${STAGE} still carries signing-hostile xattrs; set TMPDIR to a plain filesystem"
fi

_jarvis_log "staged for signing in ${STAGE}"
DIST="${STAGE}"

# =============================================================================
# 2. SIGN BROKER AND HELPER
# =============================================================================
_sign() {
    local path="$1" err

    # Strip extended attributes before every signature.
    #
    # codesign refuses to sign anything carrying "resource fork, Finder
    # information, or similar detritus", and a bundle built inside this repo
    # carries exactly that: the tree lives on an iCloud-adjacent filesystem, so
    # directories pick up com.apple.FinderInfo and com.apple.fileprovider.fpfs#P
    # merely by existing. PlistBuddy re-adds them when it edits Info.plist, so
    # clearing once at build time is not enough -- it has to happen here, at the
    # one seam every artifact passes through, immediately before each signature.
    xattr -cr "${path}" 2>/dev/null || true

    if ! err="$(codesign --force --options runtime --timestamp=none \
                         --sign "${SIGN_IDENTITY}" "${path}" 2>&1)"; then
        # Report what codesign actually said. Swallowing it into a generic
        # "codesign failed" is what made this defect take a diagnosis rather
        # than a read.
        _jarvis_warn "codesign: ${err}"
        _jarvis_die "codesign failed for ${path} with identity '${SIGN_IDENTITY}'"
    fi
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

# plutil, NOT PlistBuddy.
#
# PlistBuddy takes its whole operation as ONE shell word:
#     PlistBuddy -c "Add :K string ${REQ}"
# and a requirement contains double quotes -- `cdhash H"732e..."`. The shell
# consumes them while assembling that word, so the value that lands is
# `cdhash H732e...`: syntactically present, semantically empty, and unparseable
# as a requirement. It fails at the lock screen and looks like an unreachable
# broker.
#
# plutil takes the value as its own argv element, so nothing re-parses it.
plutil -replace JARVISBrokerMachServiceName -string "${CONSUME_SERVICE}" "${PLUGIN_PLIST}" \
    || _jarvis_die "could not write JARVISBrokerMachServiceName"
plutil -replace JARVISBrokerCodeRequirement -string "${BROKER_REQ}" "${PLUGIN_PLIST}" \
    || _jarvis_die "could not write JARVISBrokerCodeRequirement"

# Read it back and compare byte-for-byte. Writing a value is not the same as the
# value landing, and this specific field is unverifiable at runtime -- it is only
# exercised inside SecurityAgent at a locked screen, which is the worst possible
# place to discover it was mangled.
_written_req="$(plutil -extract JARVISBrokerCodeRequirement raw "${PLUGIN_PLIST}" 2>/dev/null)"
if [ "${_written_req}" != "${BROKER_REQ}" ]; then
    _jarvis_warn "wrote:    ${BROKER_REQ}"
    _jarvis_warn "read back: ${_written_req}"
    _jarvis_die "the broker requirement did not survive being written to the plugin Info.plist"
fi
_jarvis_log "verified: plugin Info.plist carries the broker requirement intact"

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
if _would "install plugin -> ${JARVIS_PLUGIN_PATH}, broker -> ${JARVIS_BROKER_BIN}, helper -> ${HELPER_INSTALL_PATH}, daemon plist -> ${JARVIS_BROKER_PLIST}"; then
    _would "load ${JARVIS_BROKER_LABEL} and exercise the deposit path"
    rm -f "${DAEMON_TMP}"
else
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

    # A failure from here on can leave the rule pointing at a mechanism whose
    # broker is down. That is fail-open safe -- the mechanism yields and
    # builtin:authenticate prompts -- but it is a half-installed system, which is
    # exactly the wired-but-inert state this project keeps having to dig out of.
    # So: if we cannot bring the broker up and the rule already names us from an
    # earlier install, put the stock rule back before dying.
    _abort_incoherent() {
        if jarvis_rule_references_plugin; then
            _jarvis_warn "${JARVIS_AUTH_RIGHT} still names ${JARVIS_PLUGIN_NAME} from an earlier install"
            if jarvis_restore_auth_rule_from_pointer; then
                _jarvis_warn "restored the stock authorization rule -- your lock screen is back to default"
            else
                _jarvis_warn "COULD NOT restore the rule automatically. Run:"
                _jarvis_warn "  sudo ${_here}/uninstall.sh"
            fi
        fi
        _jarvis_die "$1"
    }

    _jarvis_log "loading ${JARVIS_BROKER_LABEL}"
    launchctl bootout "system/${JARVIS_BROKER_LABEL}" >/dev/null 2>&1 || true

    # bootout is asynchronous: it returns before teardown finishes, and
    # bootstrapping into a label that is still unloading fails with
    # "Bootstrap failed: 5: Input/output error". Wait for the actual condition.
    if ! jarvis_wait_for_service_gone "${JARVIS_BROKER_LABEL}"; then
        _abort_incoherent "${JARVIS_BROKER_LABEL} did not unload; refusing to bootstrap over it"
    fi

    # Bounded retries. launchctl can still transiently refuse while the previous
    # instance's Mach ports are reclaimed, and one more attempt is cheaper than
    # sending the operator back to the shell.
    _bootstrap_attempts="${JARVIS_BOOTSTRAP_ATTEMPTS:-3}"
    _attempt=1
    _bootstrap_err=""
    while : ; do
        if _bootstrap_err="$(launchctl bootstrap system "${JARVIS_BROKER_PLIST}" 2>&1)"; then
            break
        fi
        if [ "${_attempt}" -ge "${_bootstrap_attempts}" ]; then
            _jarvis_warn "launchctl: ${_bootstrap_err}"
            _abort_incoherent "launchctl bootstrap failed after ${_attempt} attempt(s)"
        fi
        _jarvis_log "bootstrap attempt ${_attempt} failed (${_bootstrap_err}); retrying"
        _attempt=$(( _attempt + 1 ))
        sleep 1
    done

    # The daemon refuses to start on bad config and exits non-zero, so "is it
    # running" is a real answer about whether its configuration is coherent.
    #
    # `launchctl print` succeeding only means the job is REGISTERED. A job that
    # execs and immediately exits 1 is still registered, so the process itself
    # has to be confirmed -- otherwise "broker is running" would be a claim about
    # launchd's bookkeeping rather than about a broker.
    if ! launchctl print "system/${JARVIS_BROKER_LABEL}" >/dev/null 2>&1; then
        _abort_incoherent "broker job did not register; see ${JARVIS_STATE_DIR}/broker.err.log"
    fi
    if ! pgrep -qf "${JARVIS_BROKER_BIN}" 2>/dev/null; then
        _jarvis_warn "the job registered but no broker process is alive -- it started and exited"
        _jarvis_warn "see ${JARVIS_STATE_DIR}/broker.err.log"
        _abort_incoherent "broker is not running"
    fi
    _jarvis_log "broker is running (pid $(pgrep -f "${JARVIS_BROKER_BIN}" | head -1))"

    # =========================================================================
    # 6b. THE FIRST INTEGRATION TEST THIS SYSTEM HAS EVER HAD
    # =========================================================================
    # Deposits a real grant through the real helper into the real broker. This
    # is the first time a code signing requirement is checked by a live peer,
    # and it happens BEFORE the authorization rule is touched -- so a failure
    # costs a log line, not a lock screen.
    #
    # It exercises the deposit half only. The consume half runs inside
    # SecurityAgent and cannot be invoked without the rule change, which is
    # exactly why the rule change comes after everything testable has been
    # tested.
    _jarvis_log "self-test: depositing a grant through the installed helper"
    _selftest_out=""
    if _selftest_out="$(JARVIS_BROKER_DEPOSIT_SERVICE="${DEPOSIT_SERVICE}" \
                        JARVIS_BROKER_CODE_REQUIREMENT="${BROKER_REQ}" \
                        JARVIS_GRANT_DEPOSIT_TIMEOUT_S=5 \
                        "${HELPER_INSTALL_PATH}" "install self-test" 2>&1)"; then
        _jarvis_log "self-test PASSED -- grant ${_selftest_out}"
        _jarvis_log "  the broker accepted the helper's signature over a live XPC connection"
    else
        _selftest_rc=$?
        _jarvis_warn "self-test FAILED (exit ${_selftest_rc}): ${_selftest_out}"
        case "${_selftest_rc}" in
            69) _jarvis_warn "  broker unreachable, or it refused the helper's signature" ;;
            77) _jarvis_warn "  broker answered and refused -- signature or schema mismatch" ;;
            75) _jarvis_warn "  no answer in time" ;;
            78) _jarvis_warn "  helper could not read its own configuration" ;;
        esac
        _jarvis_warn "see ${JARVIS_STATE_DIR}/broker.err.log and"
        _jarvis_warn "  log show --predicate 'subsystem == \"com.jarvis.unlockbroker\"' --last 5m"
        # Fail closed. The rule change is the only irreversible step, and taking
        # it after the channel it depends on demonstrably does not work would be
        # betting the lock screen on a component that just failed in front of us.
        _jarvis_die "refusing to rewrite ${JARVIS_AUTH_RIGHT}; nothing about unlocking has changed"
    fi
fi

# =============================================================================
# 7. REWRITE THE AUTHORIZATION RULE  (last, backed up, proof-gated)
# =============================================================================
if [ "${SKIP_AUTHDB}" -eq 1 ]; then
    cat <<STOPPED

stopped before the authorization rule.

  ${JARVIS_AUTH_RIGHT} is unchanged. Your lock screen behaves exactly as it did
  before this script ran, and nothing here can affect your ability to unlock.

  derived requirements (each pins its own binary):
    plugin   ${PLUGIN_REQ}
    broker   ${BROKER_REQ}
    helper   ${HELPER_REQ}

STOPPED
    if [ "${DRY_RUN}" -eq 1 ]; then
        # ${DIST} has been reassigned to the staging directory by now, and
        # naming it here would under-report: the build also wrote ${_root}/dist.
        # Both are scratch, but saying "nothing outside <temp dir>" when a second
        # directory changed is the kind of small inaccuracy that erodes trust in
        # everything else the script claims.
        echo "  This was a dry run. Build output under ${_root}/dist was rebuilt,"
        echo "  and a temporary staging copy was signed. No system state changed."
        echo "  Next: sudo $0 --skip-authdb   (installs, and proves the XPC channel works)"
    else
        echo "  The deposit path is installed and self-tested."
        echo "  Next: sudo $0                 (rewrites the rule; reversible via uninstall.sh)"
    fi
    echo
    exit 0
fi

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
