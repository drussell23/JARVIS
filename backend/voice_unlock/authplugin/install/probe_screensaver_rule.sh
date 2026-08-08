#!/bin/bash
# JARVIS -- Ephemeral Probe for the system.login.screensaver authorization rule.
#
# WHAT THIS MEASURES -- AND, JUST AS IMPORTANTLY, WHAT IT DOES NOT
# ----------------------------------------------------------------
# Your lock screen currently uses:
#
#     class = rule
#     rule  = ( use-login-window-ui )
#
# which means loginwindow owns authentication. This script swaps in a different
# rule for a bounded window so you can test Touch ID, Watch unlock and your
# password under it yourself, then puts it back.
#
# IT MEASURES THE SHAPE IT APPLIES, AND NOTHING ELSE. That sentence is here
# because an earlier version of this file did not respect it. It probed
# authenticate-session-owner-or-admin -- class = user, a SecurityAgent-evaluated
# rule with no mechanisms array at all -- reported that Touch ID and the password
# survived, and closed by advising that the plugin was therefore viable "against
# evaluate-mechanisms". Those are two different configurations. The one that was
# measured worked; the one that was installed black-screened the machine, because
# converting a delegating rule into a mechanism chain removes the delegation, and
# loginwindow is what draws the lock screen and resumes the session.
#
# So this script now records the EXACT shape it applied, as a shape identity that
# install.sh matches on. A measurement of one shape cannot be spent on another --
# not by a comment, not by an argument about equivalence, and not by a flag.
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

# HOW THIS PROBE MUTATES IS DERIVED, NOT CONFIGURED.
#
#   mechanism host -> COMPOSE. Insert our mechanism at the head of the live
#                     chain, by the same function install.sh uses. This is the
#                     only mode that produces evidence install.sh can accept,
#                     because the shape applied IS the shape install.sh writes.
#                     Anything less is the substitution that caused the incident.
#
#   anything else  -> RULE SWAP. Point the right at a named SecurityAgent rule.
#                     Measures whether SecurityAgent evaluation works at all, and
#                     produces a shape that can never authorise a mechanism
#                     install -- which is correct, not a shortcoming.
#
# Deliberately not overridable. A flag that forced COMPOSE onto a delegating
# right would be a supported way to perform the exact conversion that
# black-screened this machine, and the evidence it produced could never be spent
# anyway: install.sh refuses that right at gate 7a regardless.
_probe_host_rc=0
jarvis_right_hosts_mechanisms "${JARVIS_AUTH_RIGHT}" || _probe_host_rc=$?
case "${_probe_host_rc}" in
    0) PROBE_MODE="compose" ;;
    2) PROBE_MODE="rule-swap" ;;
    *) _jarvis_die "cannot classify ${JARVIS_AUTH_RIGHT} against the system schema; refusing to probe blind" ;;
esac

# Only consulted in rule-swap mode. Named by the stock screensaver rule's own
# comment as the supported alternative to use-login-window-ui.
PROBE_RULE_NAME="${JARVIS_PROBE_RULE:-authenticate-session-owner-or-admin}"
[ "${PROBE_MODE}" = "compose" ] && PROBE_RULE_NAME="compose:${JARVIS_MECHANISM}"

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

# The baseline must be a rule we do not already appear in. If it names us, this
# is not a probe of a clean machine -- it is a measurement of an install, and
# reverting "to the backup" afterwards would restore our own mechanism while
# calling it stock.
if grep -q "${JARVIS_PLUGIN_NAME}" "${BACKUP}" 2>/dev/null; then
    _jarvis_warn "the live rule already names ${JARVIS_PLUGIN_NAME}; this is not a clean baseline"
    _jarvis_warn "  remove the install first:  sudo ${_here}/uninstall.sh"
    rm -f "${BACKUP}"
    _jarvis_die "aborting without touching the system"
fi

# COMPOSE MODE HARD GUARD: the bundle has to be on disk.
#
# Writing a chain that names a mechanism whose bundle is absent is the one state
# uninstall.sh calls strictly worse than either the installed or the stock
# configuration: SecurityAgent cannot load what is not there, so the chain fails
# before anything in it can answer. On a tries=1 right with no in-chain password
# fallback, that is a lockout with a 3-minute dead man's switch as the only way
# back. The probe must not create it.
if [ "${PROBE_MODE}" = "compose" ] && ! jarvis_plugin_bundle_present; then
    _jarvis_warn "${JARVIS_PLUGIN_PATH} is not installed."
    _jarvis_warn "  A composed chain would name a mechanism that cannot be loaded, and this"
    _jarvis_warn "  right has tries=1 with no password mechanism behind us."
    _jarvis_warn ""
    _jarvis_warn "  Install everything EXCEPT the rule first -- it leaves the lock screen"
    _jarvis_warn "  completely untouched and proves the XPC channel works:"
    _jarvis_warn "    sudo ${_here}/install.sh --skip-authdb"
    rm -f "${BACKUP}"
    _jarvis_die "aborting without touching the system"
fi

# The shape we must be able to get back to. Everything downstream compares
# against this rather than against a marker string, so the probe stays correct on
# any right, on any macOS, whatever the stock rule happens to be called.
STOCK_SHAPE="$(jarvis_rule_shape "${BACKUP}" 2>/dev/null || true)"
[ -n "${STOCK_SHAPE}" ] || { rm -f "${BACKUP}"; _jarvis_die "cannot compute the shape of the live rule; aborting without mutating"; }

# Prove the restore path works on this exact file BEFORE relying on it: write the
# backup back over the identical live value. A no-op if it succeeds, and if it
# fails we learn that now rather than 180 seconds from now.
_jarvis_log "pre-flight: verifying the restore path with a no-op write"
if ! jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${BACKUP}" >/dev/null 2>&1; then
    rm -f "${BACKUP}"
    _jarvis_die "restore path does not work on this machine; aborting without mutating"
fi

_noop_check="$(mktemp -t jarvis-probe-noop)"
jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" > "${_noop_check}" 2>/dev/null || true
_noop_shape="$(jarvis_rule_shape "${_noop_check}" 2>/dev/null || true)"
rm -f "${_noop_check}"
if [ "${_noop_shape}" != "${STOCK_SHAPE}" ]; then
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
REVERTER_PID="$(jarvis_arm_deadman "${PROBE_WINDOW_S}" "${PROBE_LOG}" \
    "/usr/bin/security authorizationdb write '${JARVIS_AUTH_RIGHT}' < '${BACKUP}'" \
    "sudo security authorizationdb write ${JARVIS_AUTH_RIGHT} < ${BACKUP}")"
_jarvis_log "dead man's switch armed (pid ${REVERTER_PID}, fires in ${PROBE_WINDOW_S}s)"

# Layer 2: instant revert on Ctrl-C or normal exit. The timer remains the backstop.
#
# Idempotent. Ctrl-C previously fired the handler once for INT and again for
# EXIT, reverting twice and printing the whole thing twice -- harmless against
# the authorization database, but a script that reports two reverts for one
# revert is lying about what it did.
_reverted=0
_revert_now() {
    [ "${_reverted}" -eq 1 ] && return 0
    _reverted=1
    _jarvis_log "reverting ${JARVIS_AUTH_RIGHT} now"
    if jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${BACKUP}" >/dev/null 2>&1; then
        _jarvis_log "reverted"
    else
        _jarvis_warn "REVERT FAILED. Run by hand:"
        _jarvis_warn "  sudo security authorizationdb write ${JARVIS_AUTH_RIGHT} < ${BACKUP}"
        _jarvis_warn "or: sudo ${_here}/uninstall.sh"
    fi
}

# Ctrl-C means "I am done testing early", NOT "discard what I measured".
# Previously INT exited straight through the trap and skipped the capture, so
# the documented escape hatch destroyed the very data the probe exists to
# collect. It now ends the countdown and falls through to revert + capture.
_interrupted=0
_on_interrupt() { _interrupted=1; }
trap _on_interrupt INT TERM
trap _revert_now EXIT

# =============================================================================
# 4. MUTATE
# =============================================================================
# Everything below this point runs with the dead man's switch already armed.
#
# The measurement baseline is taken FIRST, before the mutation, so the crash and
# log windows cannot miss an event that happens the instant the rule lands.
PROBE_T0_EPOCH="$(date +%s)"
PROBE_T0_LOG="$(date '+%Y-%m-%d %H:%M:%S')"
PROBE_CRASHES_BEFORE="$(jarvis_crash_reports_since 0 2>/dev/null | grep -c . || true)"

case "${PROBE_MODE}" in
    compose)
        # The SAME function install.sh calls. Not a reimplementation of it, and
        # not an approximation of it -- if these two ever composed differently,
        # the probe would be measuring a configuration the installer never
        # writes, which is the failure this whole design exists to prevent.
        _jarvis_log "composing ${JARVIS_MECHANISM} into ${JARVIS_AUTH_RIGHT}"
        PROBE_RULE_TMP="$(mktemp -t jarvis-probe-rule)"
        _probe_compose_rc=0
        jarvis_compose_mechanism_rule "${BACKUP}" "${PROBE_RULE_TMP}" "${JARVIS_MECHANISM}" \
            || _probe_compose_rc=$?
        case "${_probe_compose_rc}" in
            0) : ;;
            2) rm -f "${PROBE_RULE_TMP}"; _jarvis_die "the live chain already names ${JARVIS_MECHANISM}; nothing to measure" ;;
            3) rm -f "${PROBE_RULE_TMP}"; _jarvis_die "${JARVIS_AUTH_RIGHT} is not a mechanism chain live; refusing to author one" ;;
            *) rm -f "${PROBE_RULE_TMP}"; _jarvis_die "could not compose a probe rule; nothing mutated" ;;
        esac
        plutil -lint "${PROBE_RULE_TMP}" >/dev/null 2>&1 \
            || { rm -f "${PROBE_RULE_TMP}"; _jarvis_die "composed probe rule is malformed; nothing mutated"; }

        _jarvis_log "chain to be measured: $(jarvis_rule_mechanisms "${PROBE_RULE_TMP}" | tr '\n' ' ')"
        if ! jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${PROBE_RULE_TMP}"; then
            rm -f "${PROBE_RULE_TMP}"
            _jarvis_die "mutation failed; trap and dead-man switch will restore the stock rule"
        fi
        rm -f "${PROBE_RULE_TMP}"
        ;;
    rule-swap)
        _jarvis_log "switching ${JARVIS_AUTH_RIGHT} -> ${PROBE_RULE_NAME}"
        if ! security authorizationdb write "${JARVIS_AUTH_RIGHT}" "${PROBE_RULE_NAME}"; then
            _jarvis_die "mutation failed; trap and dead-man switch will restore the stock rule"
        fi
        ;;
esac

# Capture the shape that is actually live, from the database rather than from
# what we asked for. A write is a request; the rule the engine will evaluate is
# the answer, and the answer is what the measurement has to be labelled with.
LIVE_RULE="$(mktemp -t jarvis-probe-live)"
jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" > "${LIVE_RULE}" 2>/dev/null \
    || _jarvis_die "could not read back ${JARVIS_AUTH_RIGHT} after the write; trap and dead-man switch will restore the stock rule"

PROBE_SHAPE="$(jarvis_rule_shape "${LIVE_RULE}" 2>/dev/null || true)"
[ -n "${PROBE_SHAPE}" ] || _jarvis_die "could not compute the shape of the live rule; nothing is being measured"

# Structural rather than a marker match. "Did the rule change" is answered by
# comparing the live shape to the shape we backed up -- which stays true on any
# macOS, for any right, whatever the stock rule happens to be called.
if [ "${PROBE_SHAPE}" = "${STOCK_SHAPE}" ]; then
    _jarvis_die "the live rule is unchanged after the write; probe is not measuring anything"
fi

# In compose mode we ADDED to a chain. If anything the chain already had is now
# missing, we have not probed our mechanism -- we have probed a machine with
# smartcard unlock removed, and any result would be about the wrong system.
if [ "${PROBE_MODE}" = "compose" ] && ! jarvis_chain_preserves_backup "${LIVE_RULE}" "${BACKUP}"; then
    _jarvis_die "the composed chain dropped a mechanism the rule already had; reverting"
fi
rm -f "${LIVE_RULE}"

_jarvis_log "measuring shape: ${PROBE_SHAPE}"

# =============================================================================
# 5. THE MEASUREMENT WINDOW
# =============================================================================
cat <<BANNER

================================================================================
  PROBE ACTIVE -- ${PROBE_WINDOW_S} SECONDS
================================================================================
  ${JARVIS_AUTH_RIGHT}
  mode:  ${PROBE_MODE}
  shape: ${PROBE_SHAPE}

  THE FAIL-OPEN TEST IS THE POINT OF THIS RUN.

  No grant has been deposited, so JARVIS will find nothing and YIELD -- which is
  kAuthorizationResultAllow, "I am satisfied, continue", NOT Deny. Deny would
  fail the whole right and lock you out, and there is deliberately no code path
  in the mechanism that can produce it. So the yield you are about to trigger is
  the mechanism's ordinary behaviour, not a special test mode, and what it
  proves is whether a yield on THIS right still reaches a password prompt.

  That matters here and did not before: this chain has no builtin:authenticate
  behind us, and tries=1. Nothing retries.

  GO TEST NOW, in this order:

    1. Lock the screen            (Ctrl-Cmd-Q)
    2. Wait for the prompt to appear at all -> yes / no   <- the fail-open test
    3. Does TOUCH ID prompt/work?          -> yes / no
    4. Does APPLE WATCH auto-unlock work?  -> yes / no
    5. Does your PASSWORD still unlock?    -> yes / no   <- must be yes
    6. Unlock and come back here

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
while [ "${_remaining}" -gt 0 ] && [ "${_interrupted}" -eq 0 ]; do
    printf '\r[jarvis-authplugin] reverting in %3ds (Ctrl-C to finish early) ' "${_remaining}" >&2
    # `|| true`: an interrupted sleep exits non-zero, and `set -e` would tear
    # the script down before the capture that Ctrl-C is supposed to reach.
    sleep 1 || true
    _remaining=$((_remaining - 1))
done
printf '\r%*s\r' 60 '' >&2

if [ "${_interrupted}" -eq 1 ]; then
    _jarvis_log "interrupted with ${_remaining}s left -- reverting, then recording what you saw"
fi

_revert_now

_jarvis_log "final state of ${JARVIS_AUTH_RIGHT}:"
# Compared by shape against the file we backed up, not by a marker string. The
# question is "is the machine back to how it started", and the only thing that
# answers it exactly is the state it started in.
_after_revert="$(mktemp -t jarvis-probe-after)"
jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" > "${_after_revert}" 2>/dev/null || true
_after_shape="$(jarvis_rule_shape "${_after_revert}" 2>/dev/null || true)"
rm -f "${_after_revert}"

if [ -n "${_after_shape}" ] && [ "${_after_shape}" = "${STOCK_SHAPE}" ]; then
    _jarvis_log "  back to the shape it started with: ${STOCK_SHAPE}"
    rm -f "${JARVIS_AUTHDB_BACKUP_POINTER}"
else
    _jarvis_warn "  NOT STOCK. Restore by hand:"
    _jarvis_warn "    sudo security authorizationdb write ${JARVIS_AUTH_RIGHT} < ${BACKUP}"
    exit 1
fi

# =============================================================================
# 6b. THE FAIL-OPEN BATTERY  -- decided from evidence, not from the operator
# =============================================================================
# Run AFTER the revert, on purpose. Reading the unified log takes seconds and
# parsing crash reports takes longer; doing either while the machine is still
# mutated would extend the exposure window for no reason. The evidence is
# already on disk by then -- it does not expire.
#
# Three questions, none of which a human at a lock screen can answer reliably:
#
#   Did the mechanism host DIE?   The 27-crash failure mode. A dead host never
#                                 calls SetResult, so nothing in the chain ever
#                                 answers -- which on tries=1 is the black screen
#                                 and is invisible to every static check.
#   Did our mechanism RUN?        Distinguishes "fail-open works" from "the
#                                 screen was never locked", which otherwise look
#                                 identical from the operator's side.
#   Did it YIELD rather than grant?  A yield is the fail-open path. A grant would
#                                 mean a stale grant was sitting in the broker,
#                                 and the run measured the wrong thing.
PROBE_FAILOPEN="inconclusive"
PROBE_EVIDENCE_LOG="${JARVIS_STATE_DIR}/probe-window-${_stamp}.log"

_crashes_now="$(jarvis_crash_reports_since "${PROBE_T0_EPOCH}" 2>/dev/null || true)"
_crash_count="$(printf '%s' "${_crashes_now}" | grep -c . || true)"

_window_log=""
if _window_log="$(jarvis_unlock_log_since "${PROBE_T0_LOG}" 2>/dev/null)"; then :; fi
printf '%s\n' "${_window_log}" > "${PROBE_EVIDENCE_LOG}" 2>/dev/null || true
chmod 600 "${PROBE_EVIDENCE_LOG}" 2>/dev/null || true

_mech_lines="$(printf '%s\n' "${_window_log}" | grep -c "${JARVIS_LOG_SUBSYSTEM_PLUGIN}" || true)"
_yielded="$(printf '%s\n' "${_window_log}" | grep -ci 'yield' || true)"
_granted="$(printf '%s\n' "${_window_log}" | grep -ci 'grant ' || true)"

echo
echo "fail-open battery"
if [ "${_crash_count}" -gt 0 ]; then
    PROBE_FAILOPEN="crashed"
    _jarvis_warn "  the mechanism host CRASHED ${_crash_count} time(s) during the window"
    _newest_crash="$(printf '%s\n' "${_crashes_now}" | sed -n '$p')"
    if grep -q "${JARVIS_PLUGIN_NAME}" "${_newest_crash}" 2>/dev/null; then
        _jarvis_warn "  the newest report NAMES ${JARVIS_PLUGIN_NAME} -- this is our defect"
    fi
    _sym="$(jarvis_crash_faulting_symbol "${_newest_crash}" 2>/dev/null || true)"
    [ -n "${_sym}" ] && _jarvis_warn "  faulting frame: ${_sym}"
    _jarvis_warn "  DO NOT INSTALL. A host that dies on a tries=1 right is a lockout."
elif [ "${_mech_lines}" -eq 0 ]; then
    # Zero lines is genuinely ambiguous and must not be read as either answer:
    # the screen may never have been locked, or the log may not have been
    # readable. The probe says so rather than picking the convenient reading.
    PROBE_FAILOPEN="not-reached"
    _jarvis_warn "  our mechanism produced no log output during the window"
    _jarvis_warn "  either the screen was never locked, or the chain never reached us"
elif [ "${_granted}" -gt 0 ]; then
    PROBE_FAILOPEN="granted-not-yield"
    _jarvis_warn "  the mechanism GRANTED -- a stale grant was in the broker"
    _jarvis_warn "  this run measured the unlock path, not the fail-open path; re-run"
elif [ "${_yielded}" -gt 0 ]; then
    PROBE_FAILOPEN="yielded"
    _jarvis_log "  our mechanism ran and YIELDED, and the host survived"
    _jarvis_log "  whether a prompt followed is yours to confirm below"
else
    _jarvis_warn "  our mechanism ran but neither granted nor yielded in the log"
fi
_jarvis_log "  window log: ${PROBE_EVIDENCE_LOG}"

# =============================================================================
# 7. CAPTURE THE MEASUREMENT  (a result that lives only in a terminal is not a
#    result -- the first run reverted cleanly and recorded nothing)
# =============================================================================
# The path is defined in common.sh, because install.sh reads this file to decide
# whether a shape may be written. A producer and a consumer holding two literals
# for one path is a gate that silently never fires.
PROBE_RESULTS="${JARVIS_PROBE_RESULTS_LOG}"

# Every answer defaults to the honest one for a run that did not ask.
_locked="not-tested"; _touchid="not-tested"; _watch="not-tested"; _password="not-tested"
_prompted="not-tested"

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

    _prompted="$(_ask 'Did an authentication PROMPT appear at all (not a black screen)?')"
    _touchid="$(_ask 'Did TOUCH ID work?')"
    _watch="$(_ask 'Did APPLE WATCH auto-unlock work?')"
    _password="$(_ask 'Did your PASSWORD unlock?')"
else
    _jarvis_log "non-interactive; no answers collected"
fi

# PROVEN requires both halves and neither is sufficient alone.
#
# The log proves our mechanism ran, yielded, and did not take the host down with
# it. Only the operator can say a prompt actually appeared and accepted a
# password -- the 27 crashes produced a black screen that no amount of log
# reading would have called a success, and a clean yield into a chain that then
# draws nothing is exactly the failure this right's tries=1 makes unrecoverable.
if [ "${PROBE_FAILOPEN}" = "yielded" ] \
   && [ "${_locked}" = "yes" ] && [ "${_prompted}" = "yes" ] && [ "${_password}" = "yes" ]; then
    PROBE_FAILOPEN="proven"
    _jarvis_log "fail-open PROVEN: mechanism yielded, host survived, prompt appeared, password worked"
elif [ "${PROBE_FAILOPEN}" = "yielded" ]; then
    PROBE_FAILOPEN="yielded-unconfirmed"
fi

# Written on BOTH paths. A run that asked nothing still proves the shape was
# applied on this machine and at what time, and a log that only contains
# successful interactive runs is a log that cannot show you a gap.
#
# shape= is the field install.sh matches on. rule= is retained as the human
# label, and is deliberately NOT what the gate reads: it is a request, whereas
# shape is what the authorization engine was actually left holding.
{
    printf '%s rule=%s shape=%s window=%ss locked=%s prompted=%s touchid=%s watch=%s password=%s failopen=%s\n' \
        "$(date -u +%FT%TZ)" "${PROBE_RULE_NAME}" "${PROBE_SHAPE}" "${PROBE_WINDOW_S}" \
        "${_locked}" "${_prompted}" "${_touchid}" "${_watch}" "${_password}" "${PROBE_FAILOPEN}"
} >> "${PROBE_RESULTS}"

_jarvis_log "recorded to ${PROBE_RESULTS}"

if [ "${_password}" = "no" ]; then
    _jarvis_warn "PASSWORD FAILED under this shape. It must never be installed."
fi

cat <<NEXT

WHAT THIS RUN DOES AND DOES NOT LICENSE

  measured shape:  ${PROBE_SHAPE}

  Those answers describe THAT shape. They describe no other. install.sh matches
  on the shape string above, so this measurement can only ever authorise the
  configuration it was taken under -- which is the point, and is the guarantee
  that was missing when a class=user probe was used to justify installing a
  mechanism chain.

  Touch ID + Watch SURVIVE  -> this shape is usable, at no biometric cost.
  Touch ID or Watch BREAK   -> this shape costs you native biometrics. A real
                               price, and your call whether it is worth paying.
  PASSWORD fails            -> this shape must never be installed. install.sh
                               will not accept this record as permission.

  Note that a usable shape is still not an installable one: install.sh also
  requires that the right can host a mechanism in Apple's schema at all. A right
  whose stock class delegates -- system.login.screensaver is class=rule pointing
  at use-login-window-ui -- is refused no matter how well it probes, because
  probing measures authentication and what conversion destroys is the lock
  screen's UI and its session resume.

NEXT
