#!/bin/bash
# JARVIS Authorization Plugin -- the sentinel.
#
# WHAT THIS INVERTS
# -----------------
# Every other guard in this directory stops the INSTALLER from doing the wrong
# thing. None of them can help once the rule is written, and four ways to a black
# lock screen remain open after a correct install:
#
#   1. the mechanism starts crashing its host   (27 times, over two days, unseen)
#   2. an OS update rewrites the right          ("Do not modify" is about this)
#   3. the bundle goes away, rule still naming it  (rebuild, make clean, rm)
#   4. someone edits the rule by hand           (how this machine got here)
#
# All four end the same way: the machine sits broken until a human notices. This
# exists so it does not.
#
# The default state is now safe and JARVIS has to keep earning its place: the
# moment the configuration stops being the one the installer proved, our
# mechanism comes out. Nothing prompts, nothing waits for a person.
#
# WHY THERE IS NO RESIDENT DAEMON
# -------------------------------
# launchd's WatchPaths is kqueue-backed, so this script is invoked on a change
# rather than polling for one. Idle cost is not "near zero", it is zero: between
# events there is no process. That also answers "what if the sentinel crashes"
# at the root rather than with KeepAlive restarts -- a thing that is not running
# cannot crash, and launchd is the one process on the system whose liveness is
# not our problem.
#
# A long StartInterval remains as a backstop. It is not the mechanism; it is
# insurance against a coalesced or missed kqueue event, and against the machine
# having been asleep. Deliberately long enough that it is never the thing that
# notices first.
#
# WHAT IT WILL NOT DO
# -------------------
# It never installs, never composes, never grants. Its only write is a REVERT,
# through the same audited function uninstall.sh uses. A sentinel that could put
# the mechanism back would be a second installer with no gates in front of it.

set -uo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${_here}/common.sh"
set +e

SENTINEL_LOG="${JARVIS_SENTINEL_LOG:-${JARVIS_STATE_DIR}/sentinel.log}"
SENTINEL_LOG_MAX="${JARVIS_SENTINEL_LOG_MAX_BYTES:-262144}"
LOCK_DIR="${JARVIS_STATE_DIR}/sentinel.lock"
DEFER_FILE="${JARVIS_STATE_DIR}/sentinel.deferrals"
DEFER_MAX="${JARVIS_SENTINEL_MAX_DEFERRALS:-3}"

# Root, or nothing. launchd invokes this as root; a human running it by hand
# without sudo is not an error, it simply cannot read the crash reports or write
# the database. Exit 0 rather than failing: a non-zero exit here would be a
# launchd-visible failure that says something is wrong with the machine when the
# only thing wrong is the invocation.
if [ "$(id -u)" -ne 0 ]; then
    printf '[sentinel] needs root; nothing checked (try: sudo %s)\n' "$0" >&2
    exit 0
fi

mkdir -p "${JARVIS_STATE_DIR}" 2>/dev/null || true

# Decide once whether the log is writable. A redirection to an unwritable path is
# reported by the shell BEFORE the command runs, so `2>/dev/null` on the printf
# does not suppress it -- every _say would emit a permission error alongside the
# message it was trying to record.
_LOG_OK=0
if : >> "${SENTINEL_LOG}" 2>/dev/null; then _LOG_OK=1; fi

_say() {
    [ "${_LOG_OK}" -eq 1 ] \
        && printf '%s [sentinel] %s\n' "$(date -u +%FT%TZ)" "$*" >> "${SENTINEL_LOG}" 2>/dev/null
    printf '[sentinel] %s\n' "$*" >&2
}

# Bounded log. A watchdog that fills the disk it is watching has become the
# problem it exists to prevent.
if [ -f "${SENTINEL_LOG}" ]; then
    _size="$(stat -f %z "${SENTINEL_LOG}" 2>/dev/null || echo 0)"
    if [ "${_size}" -gt "${SENTINEL_LOG_MAX}" ]; then
        tail -c $(( SENTINEL_LOG_MAX / 2 )) "${SENTINEL_LOG}" > "${SENTINEL_LOG}.tmp" 2>/dev/null \
            && mv -f "${SENTINEL_LOG}.tmp" "${SENTINEL_LOG}" 2>/dev/null
    fi
fi

# =============================================================================
# SERIALISE
# =============================================================================
# Three WatchPaths and a RunAtLoad can fire together -- a bundle removal touches
# the plugin directory AND, moments later, the authorization database when we
# repair it, which re-triggers the watch. Two concurrent reverts racing on one
# rule is how a half-written chain happens.
#
# mkdir because macOS ships no flock(1) and this has to work from a Recovery
# shell. It is atomic on every filesystem that matters. The PID inside is what
# makes a lock left behind by a SIGKILL recoverable rather than permanent.
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    # mkdir fails for more than one reason, and they are not interchangeable.
    # Only "it already exists" is contention. A missing parent or a permission
    # error is an environment problem, and treating it as a stale lock would
    # rm -rf a path we could not create -- and, if the directory did exist but
    # its pid file was unreadable, would break a LIVE lock and let two reverts
    # race on one rule.
    if [ ! -d "${LOCK_DIR}" ]; then
        _say "cannot create ${LOCK_DIR} (missing state directory or no permission); nothing checked"
        exit 0
    fi

    _owner="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
    if [ -n "${_owner}" ] && kill -0 "${_owner}" 2>/dev/null; then
        _say "another sentinel is running (pid ${_owner}); this invocation defers to it"
        exit 0
    fi

    # An existing lock with no readable pid is ambiguous, but it is also exactly
    # what a SIGKILLed predecessor leaves behind -- and a lock that can never be
    # broken would disable the sentinel permanently. Break it, and say so.
    _say "breaking a stale lock (owner ${_owner:-unknown} is gone)"
    rm -rf "${LOCK_DIR}" 2>/dev/null
    mkdir "${LOCK_DIR}" 2>/dev/null || { _say "could not take the lock; giving up this pass"; exit 0; }
fi
printf '%s' "$$" > "${LOCK_DIR}/pid" 2>/dev/null
trap 'rm -rf "${LOCK_DIR}" 2>/dev/null' EXIT

# =============================================================================
# IS THERE ANYTHING TO GUARD?
# =============================================================================
# If the rule does not name us there is nothing to revert, and this is the
# overwhelmingly common case -- every boot on a machine that never installed,
# every event after a successful uninstall. It has to be the cheapest path.
if ! jarvis_rule_references_plugin; then
    rm -f "${DEFER_FILE}" 2>/dev/null
    exit 0
fi

# =============================================================================
# THE HEALTH PREDICATE
# =============================================================================
# Findings are collected, not acted on one at a time, so the log records
# everything that was wrong rather than only the first thing noticed.
_findings=""
_urgent=0
_note()  { _findings="${_findings}${_findings:+; }$1"; }
_note_urgent() { _note "$1"; _urgent=1; }

# 1. The bundle. URGENT: a chain naming a mechanism that cannot be loaded fails
#    before anything in it answers, and on tries=1 that is a lockout happening
#    right now.
jarvis_plugin_bundle_present \
    || _note_urgent "bundle absent at ${JARVIS_PLUGIN_PATH}"

# 2. Crashes since the bundle was installed. URGENT for the same reason: a host
#    that dies never calls SetResult. Scoped to reports that NAME us, so an
#    unrelated authorizationhosthelper fault does not uninstall a healthy plugin.
_since=0
[ -e "${JARVIS_PLUGIN_PATH}" ] && _since="$(stat -f %m "${JARVIS_PLUGIN_PATH}" 2>/dev/null || echo 0)"
if _reports="$(jarvis_crash_reports_since "${_since}")" && [ -n "${_reports}" ]; then
    _ours=0
    while IFS= read -r _rep; do
        [ -n "${_rep}" ] || continue
        grep -q "${JARVIS_PLUGIN_NAME}" "${_rep}" 2>/dev/null && _ours=$(( _ours + 1 ))
    done <<EOF
${_reports}
EOF
    if [ "${_ours}" -gt 0 ]; then
        _newest="$(printf '%s\n' "${_reports}" | sed -n '$p')"
        _note_urgent "${_ours} crash(es) naming ${JARVIS_PLUGIN_NAME} (frame: $(jarvis_crash_faulting_symbol "${_newest}" 2>/dev/null || echo unknown))"
    fi
fi

# 3-5 need the live rule on disk.
_live="$(mktemp -t jarvis-sentinel-live)"
if jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" > "${_live}" 2>/dev/null; then

    # 3. Drift from the shape the installer proved. NOT urgent: the chain is
    #    still coherent and still has whatever it had, it is simply no longer the
    #    configuration that was measured -- which is exactly what an OS update
    #    rewriting the right looks like.
    if _sanctioned="$(jarvis_sanctioned_shape)" && [ -n "${_sanctioned}" ]; then
        _now="$(jarvis_rule_shape "${_live}" 2>/dev/null || true)"
        [ "${_now}" = "${_sanctioned}" ] \
            || _note "shape drifted from the sanctioned one (now: ${_now:-unreadable})"
    else
        # We are in the chain with no record of anyone having sanctioned it.
        # That is finding 4 -- a hand-written rule -- and it is the case no
        # installer gate can ever cover.
        _note_urgent "no sanctioned shape on record, but the rule names us"
    fi

    # 4. Did we take something away? The backup is the only thing that knows what
    #    the chain had before us.
    _ptr=""
    [ -f "${JARVIS_AUTHDB_BACKUP_POINTER}" ] && _ptr="$(cat "${JARVIS_AUTHDB_BACKUP_POINTER}" 2>/dev/null || true)"
    if [ -n "${_ptr}" ] && [ -f "${_ptr}" ]; then
        jarvis_chain_preserves_backup "${_live}" "${_ptr}" \
            || _note "the chain has lost a mechanism it had before the install"
    fi

    # 5. Is the right still a mechanism host at all? An OS update can change what
    #    a right IS, and being in the chain of a right that no longer hosts
    #    mechanisms is the original defect, arrived at by a different road.
    jarvis_right_hosts_mechanisms "${JARVIS_AUTH_RIGHT}" >/dev/null 2>&1 \
        || _note "${JARVIS_AUTH_RIGHT} is no longer a mechanism host in the system schema"
fi
rm -f "${_live}"

if [ -z "${_findings}" ]; then
    rm -f "${DEFER_FILE}" 2>/dev/null
    exit 0
fi

# =============================================================================
# ACT
# =============================================================================
_say "UNHEALTHY: ${_findings}"

# Defer only what is safe to defer, and only for a bounded number of passes.
#
# A non-urgent finding during an active authentication can wait for the user to
# finish; rewriting the rule underneath them achieves nothing they need. An
# URGENT finding cannot wait, because an active authentication is precisely when
# a stuck user is staring at the failure. And the deferral is counted, so a
# console that stays locked forever cannot postpone the repair forever -- a guard
# that waits on the guarded system is the deadlock this project already retired
# once.
if [ "${_urgent}" -eq 0 ] && jarvis_authentication_in_flight; then
    _count="$(cat "${DEFER_FILE}" 2>/dev/null || echo 0)"
    case "${_count}" in ''|*[!0-9]*) _count=0 ;; esac
    if [ "${_count}" -lt "${DEFER_MAX}" ]; then
        printf '%s' "$(( _count + 1 ))" > "${DEFER_FILE}" 2>/dev/null
        _say "authentication in flight; deferring non-urgent repair ($(( _count + 1 ))/${DEFER_MAX})"
        exit 0
    fi
    _say "deferral budget spent; repairing despite the active session"
fi

[ "${_urgent}" -eq 1 ] && _say "finding is URGENT; repairing without waiting for the session"

if jarvis_revert_auth_rule; then
    _say "REPAIRED: ${JARVIS_PLUGIN_NAME} is out of ${JARVIS_AUTH_RIGHT}"
    _say "  your lock screen is back to the configuration it had before the install"
    _say "  diagnose with: sudo ${_here}/verify.sh"
    rm -f "${DEFER_FILE}" 2>/dev/null
    exit 0
fi

# Could not repair. Say so loudly and keep saying it: the next event fires this
# again, and an unrepaired machine that stopped complaining is the worst of both.
_say "REPAIR FAILED -- ${JARVIS_AUTH_RIGHT} still names ${JARVIS_PLUGIN_NAME}"
_say "  recover by hand: sudo ${_here}/uninstall.sh"
exit 1
