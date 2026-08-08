#!/bin/bash
# JARVIS -- Mechanism lifecycle probe.
#
# WHAT THIS ANSWERS
# -----------------
# "Does our mechanism survive being loaded, invoked, answered and destroyed
# inside authorizationhosthelper -- repeatedly?"
#
# That is the question 27 segfaults across two days says nobody may assume. The
# use-after-free lived precisely in that lifecycle: SecurityAgent frees the
# mechanism the instant the chain advances past it, and every escaping callback
# can still fire afterwards. A single successful invocation proves almost
# nothing about a race; a few dozen back-to-back invocations are what shake one
# out.
#
# WHY IT COSTS NOTHING TO RUN
# ---------------------------
# It does not touch the lock screen, the login window, or any right macOS
# consults. It creates a right of our OWN -- in our own reverse-DNS namespace,
# referenced by nothing, evaluated by nobody -- puts our mechanism in it alone,
# and invokes it directly with `security authorize`. Cleanup is
# `authorizationdb remove`: the right never existed, so there is nothing to
# restore and no backup to get wrong.
#
# Compare with the alternatives that were considered and rejected:
#
#   system.login.screensaver.unlock  tries=1, no password mechanism behind us,
#                                    and failure locks you out of your session.
#   system.restart                   forgiving, but invoking it runs
#                                    RestartAuthorization:restart, and whether
#                                    that mechanism merely CHECKS or actually
#                                    initiates a restart is not something to
#                                    find out by being wrong about it.
#
# WHAT IT DOES NOT ANSWER
# -----------------------
# Whether a yield reaches a password prompt. No synthetic right can tell you
# that -- it is a property of loginwindow, on the real right, and it needs
# probe_screensaver_rule.sh. Run this one first anyway: if the mechanism cannot
# survive its own lifecycle here, there is no point risking a lock screen to
# learn the same thing.
#
# USAGE
#   sudo ./probe_mechanism_lifecycle.sh
#   sudo JARVIS_LIFECYCLE_ITERATIONS=100 ./probe_mechanism_lifecycle.sh

set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${_here}/common.sh"

jarvis_require_macos
jarvis_require_root

# --- Tunables ----------------------------------------------------------------
PROBE_RIGHT="${JARVIS_LIFECYCLE_RIGHT:-${JARVIS_RIGHT_NAMESPACE}probe.lifecycle}"
ITERATIONS="${JARVIS_LIFECYCLE_ITERATIONS:-25}"
# The dead man's window has to outlast the whole run by a comfortable margin, so
# it is derived from the iteration count rather than fixed. A constant here would
# either fire mid-run on a slow machine or leave a synthetic right lying around
# for minutes on a fast one.
DEADMAN_S="${JARVIS_LIFECYCLE_DEADMAN_S:-$(( ITERATIONS * 4 + 60 ))}"
PROBE_LOG="${JARVIS_STATE_DIR}/probe.log"

case "${ITERATIONS}" in
    ''|*[!0-9]*) _jarvis_die "JARVIS_LIFECYCLE_ITERATIONS must be a positive integer" ;;
esac
[ "${ITERATIONS}" -ge 1 ] || _jarvis_die "iterations must be >= 1"
[ "${ITERATIONS}" -le 1000 ] || _jarvis_die "iterations must be <= 1000; this is a probe, not a soak host"

# The right must be ours. A synthetic right is only safe because nothing consults
# it, and that is guaranteed by the namespace and by nothing else.
case "${PROBE_RIGHT}" in
    "${JARVIS_RIGHT_NAMESPACE}"*) : ;;
    *) _jarvis_die "the lifecycle right must be under ${JARVIS_RIGHT_NAMESPACE}; ${PROBE_RIGHT} is not ours to invent" ;;
esac

# It must not already exist. If it does, something else is using this name and
# removing it afterwards would be destroying someone else's configuration.
if jarvis_authdb_read "${PROBE_RIGHT}" >/dev/null 2>&1; then
    _jarvis_die "${PROBE_RIGHT} already exists; refusing to reuse a name that is already in the database"
fi

# The bundle has to be there. Composing a chain around a mechanism that cannot be
# loaded would measure SecurityAgent's failure to find a file, not our code.
jarvis_plugin_bundle_present || _jarvis_die "${JARVIS_PLUGIN_PATH} is not installed; run: sudo ${_here}/install.sh --skip-authdb"

mkdir -p "${JARVIS_STATE_DIR}"
chmod 700 "${JARVIS_STATE_DIR}" 2>/dev/null || true

# =============================================================================
# 1. ARM THE DEAD MAN'S SWITCH  (before the right exists, on purpose)
# =============================================================================
# Same discipline as the screensaver probe and the same function: there must be
# no window in which something has been created and nothing is scheduled to
# remove it. Removal, not restoration -- the right had no previous state.
REAPER_PID="$(jarvis_arm_deadman "${DEADMAN_S}" "${PROBE_LOG}" \
    "/usr/bin/security authorizationdb remove '${PROBE_RIGHT}'" \
    "sudo security authorizationdb remove ${PROBE_RIGHT}")"
_jarvis_log "dead man's switch armed (pid ${REAPER_PID}, removes the right in ${DEADMAN_S}s)"

_removed=0
_remove_now() {
    [ "${_removed}" -eq 1 ] && return 0
    _removed=1
    if security authorizationdb remove "${PROBE_RIGHT}" >/dev/null 2>&1; then
        _jarvis_log "removed ${PROBE_RIGHT}"
    else
        _jarvis_warn "could not remove ${PROBE_RIGHT}; the dead man's switch will retry"
    fi
}
trap _remove_now EXIT INT TERM

# =============================================================================
# 2. CREATE THE RIGHT
# =============================================================================
# Our mechanism ALONE. Nothing else in the chain, deliberately: a second
# mechanism could answer for us and mask a failure to answer at all, which is
# exactly the symptom a dead host produces.
RULE_TMP="$(mktemp -t jarvis-lifecycle-rule)"
cat > "${RULE_TMP}" <<RULE
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>class</key>
    <string>${JARVIS_MECHANISM_HOST_CLASS}</string>
    <key>comment</key>
    <string>JARVIS lifecycle probe. Ephemeral; nothing in macOS consults this right. Remove with: security authorizationdb remove ${PROBE_RIGHT}</string>
    <key>mechanisms</key>
    <array>
        <string>${JARVIS_MECHANISM}</string>
    </array>
    <key>shared</key>
    <false/>
    <key>tries</key>
    <integer>1</integer>
</dict>
</plist>
RULE
plutil -lint "${RULE_TMP}" >/dev/null 2>&1 || _jarvis_die "generated lifecycle rule is malformed; nothing created"

# Authored rather than derived, and that is correct HERE and nowhere else: there
# is no incumbent to derive from. The right does not exist until this line. The
# "never author a rule" discipline exists to stop us destroying a configuration
# we did not read -- there is nothing here to destroy.
if ! jarvis_authdb_write "${PROBE_RIGHT}" "${RULE_TMP}"; then
    rm -f "${RULE_TMP}"
    _jarvis_die "could not create ${PROBE_RIGHT}"
fi
rm -f "${RULE_TMP}"
_jarvis_log "created ${PROBE_RIGHT} -> ${JARVIS_MECHANISM}"

# =============================================================================
# 3. BASELINE, THEN HAMMER IT
# =============================================================================
T0_EPOCH="$(date +%s)"
T0_LOG="$(date '+%Y-%m-%d %H:%M:%S')"

_jarvis_log "invoking ${ITERATIONS}x -- each one is a full Create/Invoke/SetResult/Destroy"
_ok_count=0
_fail_count=0
_i=1
while [ "${_i}" -le "${ITERATIONS}" ]; do
    printf '\r[jarvis-authplugin] invocation %d/%d (ok=%d fail=%d) ' \
        "${_i}" "${ITERATIONS}" "${_ok_count}" "${_fail_count}" >&2
    # No -u: user interaction is exactly what we do not want. The mechanism
    # yields with no grant present and the chain completes without any UI.
    if security authorize "${PROBE_RIGHT}" >/dev/null 2>&1; then
        _ok_count=$(( _ok_count + 1 ))
    else
        _fail_count=$(( _fail_count + 1 ))
    fi
    _i=$(( _i + 1 ))
done
printf '\r%*s\r' 70 '' >&2

_remove_now

# =============================================================================
# 4. THE VERDICT  -- from crash reports and the log, not from the exit codes
# =============================================================================
# `security authorize` returning non-zero is not the failure that matters. A
# mechanism that yields cleanly and a mechanism that never answers can both end
# with a non-zero authorization; only the host's corpses distinguish them.
_crashes="$(jarvis_crash_reports_since "${T0_EPOCH}" 2>/dev/null || true)"
_crash_count="$(printf '%s' "${_crashes}" | grep -c . || true)"

EVIDENCE_LOG="${JARVIS_STATE_DIR}/lifecycle-$(date +%Y%m%d-%H%M%S).log"
jarvis_unlock_log_since "${T0_LOG}" > "${EVIDENCE_LOG}" 2>/dev/null || true
chmod 600 "${EVIDENCE_LOG}" 2>/dev/null || true

_mech_lines="$(grep -c "${JARVIS_LOG_SUBSYSTEM_PLUGIN}" "${EVIDENCE_LOG}" 2>/dev/null || true)"
_yields="$(grep -ci 'yield' "${EVIDENCE_LOG}" 2>/dev/null || true)"

echo
echo "================================================================================"
echo "  MECHANISM LIFECYCLE -- ${ITERATIONS} invocation(s) of ${PROBE_RIGHT}"
echo "================================================================================"
printf '  authorize returned ok : %s\n' "${_ok_count}"
printf '  authorize returned no : %s\n' "${_fail_count}"
printf '  mechanism log lines   : %s\n' "${_mech_lines}"
printf '  yields observed       : %s\n' "${_yields}"
printf '  host crashes          : %s\n' "${_crash_count}"
printf '  window log            : %s\n' "${EVIDENCE_LOG}"
echo

_verdict_rc=0
if [ "${_crash_count}" -gt 0 ]; then
    _newest="$(printf '%s\n' "${_crashes}" | sed -n '$p')"
    _jarvis_warn "FAILED -- the mechanism host died ${_crash_count} time(s)"
    if grep -q "${JARVIS_PLUGIN_NAME}" "${_newest}" 2>/dev/null; then
        _jarvis_warn "  the newest report NAMES ${JARVIS_PLUGIN_NAME} -- our defect"
    fi
    _sym="$(jarvis_crash_faulting_symbol "${_newest}" 2>/dev/null || true)"
    [ -n "${_sym}" ] && _jarvis_warn "  faulting frame: ${_sym}"
    _jarvis_warn "  DO NOT go near the lock screen. Fix this first."
    _verdict_rc=1
elif [ "${_mech_lines}" -eq 0 ]; then
    # Honest ambiguity. Either the chain never reached us, or the unified log was
    # not readable. Neither is a pass, and calling it one is how an unproven fix
    # ends up on a tries=1 right.
    _jarvis_warn "INCONCLUSIVE -- our mechanism produced no log output"
    _jarvis_warn "  the chain may never have reached it, or the log was unreadable"
    _verdict_rc=2
else
    _jarvis_log "PASSED -- ${ITERATIONS} full lifecycles, ${_yields} yield(s), zero host crashes"
    _jarvis_log "  the use-after-free class is not reproducible at this iteration count"
    _jarvis_log "  next: sudo ${_here}/probe_screensaver_rule.sh  (the fail-open question)"
fi

# Prove we left nothing behind. A probe that creates a right and cannot
# demonstrate its removal has not finished.
if jarvis_authdb_read "${PROBE_RIGHT}" >/dev/null 2>&1; then
    _jarvis_warn "${PROBE_RIGHT} still exists -- remove by hand:"
    _jarvis_warn "  sudo security authorizationdb remove ${PROBE_RIGHT}"
    _verdict_rc=1
else
    _jarvis_log "verified: ${PROBE_RIGHT} is gone; the database is as it was"
fi

exit "${_verdict_rc}"
