#!/bin/bash
# JARVIS Authorization Plugin -- verify an installed system is coherent.
#
# WHY THIS EXISTS
# ---------------
# Three components authorise each other by code signing requirement, and each
# requirement is a hash of a specific binary. Any rebuild, partial install, or
# manual edit can leave two of them disagreeing -- and that disagreement is
# INVISIBLE until SecurityAgent invokes the mechanism at a locked screen, where
# it manifests as "voice unlock silently does nothing" and looks identical to a
# dead daemon.
#
# The checks below were originally typed by hand after each install. Anything
# worth typing twice during an incident is worth being a script: hand-run checks
# get shortened under pressure, and the one that gets skipped is the one that
# would have found the problem.
#
# READ-ONLY. Needs no root, changes nothing, and is safe at any time.
#
# Exit codes:
#   0  coherent (or coherently absent -- nothing installed)
#   1  installed but INCOHERENT: something would fail at the lock screen
#   2  partially installed: some components present, others missing

set -uo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${_here}/common.sh"

jarvis_require_macos

HELPER_PATH="${JARVIS_HELPER_PATH:-/usr/local/libexec/jarvis-unlock-grant}"
PLUGIN_PLIST="${JARVIS_PLUGIN_PATH}/Contents/Info.plist"

_problems=0
_missing=0

_ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
_bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; _problems=$(( _problems + 1 )); }
_gone() { printf '  \033[33m-\033[0m %s\n' "$*"; _missing=$(( _missing + 1 )); }

# Reads a binary or bundle's designated requirement. Same parse as install.sh,
# including the leading "# " that codesign puts on the line -- omitting it yields
# an empty string that compares equal to another empty string, which would make
# this script report agreement between two components it never actually read.
_requirement_of() {
    codesign -d -r- "$1" 2>&1 | /usr/bin/sed -n 's/^#* *designated => //p'
}

_compare() {
    local label="$1" expected="$2" actual="$3"
    if [ -z "${expected}" ] || [ -z "${actual}" ]; then
        _bad "${label}: could not read one side (expected='${expected}' actual='${actual}')"
        return
    fi
    if [ "${expected}" = "${actual}" ]; then
        _ok "${label}"
    else
        _bad "${label}"
        printf '      expects: %s\n' "${expected}"
        printf '      actual : %s\n' "${actual}"
    fi
}

echo
echo "JARVIS unlock plugin -- installed state"
echo "======================================"

# --- Presence ---------------------------------------------------------------
echo
echo "components"
for path in "${JARVIS_PLUGIN_PATH}" "${JARVIS_BROKER_BIN}" "${HELPER_PATH}" "${JARVIS_BROKER_PLIST}"; do
    if [ -e "${path}" ]; then _ok "${path}"; else _gone "${path} (absent)"; fi
done

if [ "${_missing}" -eq 4 ]; then
    echo
    echo "nothing installed. system.login.screensaver:"
    if jarvis_rule_references_plugin; then
        _bad "rule names ${JARVIS_PLUGIN_NAME} but no plugin is installed -- run uninstall.sh"
        echo; exit 1
    fi
    _ok "stock (does not reference ${JARVIS_PLUGIN_NAME})"
    echo; exit 0
fi

# --- Signatures -------------------------------------------------------------
echo
echo "signatures"
for path in "${JARVIS_PLUGIN_PATH}" "${JARVIS_BROKER_BIN}" "${HELPER_PATH}"; do
    [ -e "${path}" ] || continue
    if codesign --verify --strict "${path}" >/dev/null 2>&1; then
        _ok "$(basename "${path}") signature valid"
    else
        _bad "$(basename "${path}") FAILS its own signature check"
    fi
done

# --- Mutual requirements ----------------------------------------------------
# The heart of it. Each component names its peers by hash; a rebuild of any one
# invalidates two references.
echo
echo "mutual code signing requirements"
plugin_req="$(_requirement_of "${JARVIS_PLUGIN_PATH}")"
broker_req="$(_requirement_of "${JARVIS_BROKER_BIN}")"
helper_req="$(_requirement_of "${HELPER_PATH}")"

_compare "plugin -> broker  (plugin dials a broker it trusts)" \
         "$(plutil -extract JARVISBrokerCodeRequirement raw "${PLUGIN_PLIST}" 2>/dev/null)" \
         "${broker_req}"
_compare "broker -> plugin  (consume service accepts the plugin)" \
         "$(plutil -extract EnvironmentVariables.JARVIS_BROKER_CONSUMER_REQUIREMENT raw "${JARVIS_BROKER_PLIST}" 2>/dev/null)" \
         "${plugin_req}"
_compare "broker -> helper  (deposit service accepts the helper)" \
         "$(plutil -extract EnvironmentVariables.JARVIS_BROKER_DEPOSITOR_REQUIREMENT raw "${JARVIS_BROKER_PLIST}" 2>/dev/null)" \
         "${helper_req}"

# --- Mach service agreement -------------------------------------------------
echo
echo "mach services"
_compare "plugin dials the consume service the broker vends" \
         "$(plutil -extract JARVISBrokerMachServiceName raw "${PLUGIN_PLIST}" 2>/dev/null)" \
         "$(plutil -extract EnvironmentVariables.JARVIS_BROKER_CONSUME_SERVICE raw "${JARVIS_BROKER_PLIST}" 2>/dev/null)"

consume="$(plutil -extract EnvironmentVariables.JARVIS_BROKER_CONSUME_SERVICE raw "${JARVIS_BROKER_PLIST}" 2>/dev/null)"
deposit="$(plutil -extract EnvironmentVariables.JARVIS_BROKER_DEPOSIT_SERVICE raw "${JARVIS_BROKER_PLIST}" 2>/dev/null)"
if [ -n "${consume}" ] && [ "${consume}" = "${deposit}" ]; then
    _bad "consume and deposit share a service name -- privilege separation collapsed"
else
    _ok "consume and deposit are distinct services"
fi

# --- Runtime ----------------------------------------------------------------
echo
echo "runtime"
if pgrep -qf "${JARVIS_BROKER_BIN}" 2>/dev/null; then
    _ok "broker running (pid $(pgrep -f "${JARVIS_BROKER_BIN}" | head -1))"
else
    _bad "broker NOT running -- voice unlock cannot work (password still will)"
fi

# --- Authorization rule -----------------------------------------------------
echo
echo "authorization rule (${JARVIS_AUTH_RIGHT})"
if jarvis_rule_references_plugin; then
    _ok "references ${JARVIS_PLUGIN_NAME}"
    # The stock authenticator must remain in the chain. Without it a failure in
    # our mechanism has nothing to fall through to, and the fail-open guarantee
    # -- the entire reason this is safe to install -- would be a comment rather
    # than a property.
    if jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" 2>/dev/null | grep -q "builtin:authenticate"; then
        _ok "builtin:authenticate still present (fail-open path intact)"
    else
        _bad "builtin:authenticate is MISSING -- nothing to fall through to. Run uninstall.sh"
    fi
else
    _gone "stock rule; the plugin is installed but not wired in"
fi

# --- Rule shape vs Apple's schema -------------------------------------------
# The check that would have caught the black screen, and the reason every other
# check in this section was not enough.
#
# Those above ask whether the rule names the right things. This one asks whether
# the rule is a KIND the right is allowed to be. system.login.screensaver ships
# as class=rule pointing at use-login-window-ui, which delegates the whole lock
# screen -- panel, password field, and the session resume that re-attaches
# WindowServer -- to loginwindow. Converting it to a mechanism chain replaces an
# authenticator AND a user interface with an authenticator alone. Every check
# above stays green through that, because the chain it finds is coherent. It just
# has nothing to draw with.
#
# Consulted against the stock schema, never the live rule: after the conversion
# the live rule IS a mechanism chain, so a live-rule check would confirm its own
# damage.
echo
echo "rule shape"
_live_rule="$(mktemp -t jarvis-verify-rule)"
if jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" > "${_live_rule}" 2>/dev/null; then
    _live_shape="$(jarvis_rule_shape "${_live_rule}" 2>/dev/null || true)"
    _live_class="$(jarvis_rule_class "${_live_rule}" 2>/dev/null || true)"
    printf '      live: %s\n' "${_live_shape:-<unreadable>}"

    _shape_rc=0
    jarvis_right_hosts_mechanisms "${JARVIS_AUTH_RIGHT}" || _shape_rc=$?

    if [ "${_shape_rc}" -eq 0 ]; then
        _ok "${JARVIS_AUTH_RIGHT} is a mechanism host in Apple's schema"
    elif [ "${_live_class}" = "${JARVIS_MECHANISM_HOST_CLASS}" ]; then
        _bad "${JARVIS_AUTH_RIGHT} has been CONVERTED into a mechanism chain"
        jarvis_stock_rule_summary "${JARVIS_AUTH_RIGHT}" 2>/dev/null | sed 's/^/      stock /' || true
        printf '      This is the black-screen configuration: authentication succeeds\n'
        printf '      and nothing paints the lock screen or resumes the session.\n'
        printf '      Recover now:  sudo %s/uninstall.sh\n' "${_here}"
    else
        _ok "${JARVIS_AUTH_RIGHT} is not a mechanism host, and has not been converted"
    fi
else
    _bad "cannot read ${JARVIS_AUTH_RIGHT}"
fi
rm -f "${_live_rule}"

# --- Crash history ----------------------------------------------------------
# The check that was missing when it mattered.
#
# A use-after-free in the mechanism segfaulted authorizationhosthelper 27 times
# across two days while every check above reported "coherent" -- because every
# check above is STATIC. It compares hashes and plist keys, none of which change
# when the mechanism dies at runtime. The mechanism runs inside a process we do
# not own, so its deaths land in DiagnosticReports rather than anywhere this
# script was looking, and the operator saw only a black lock screen.
#
# A crash here is strictly worse than a hash mismatch. A mismatch fails CLOSED
# to a password prompt, which is the designed-for outcome. A host that dies
# before calling SetResult means nothing in the chain ever answers -- including
# the builtin:authenticate that the section above just confirmed was present, and
# which never gets reached. That is the black screen, and it is invisible to
# every other check in this file.
echo
echo "crash history (authorizationhosthelper)"
_reports_dir="/Library/Logs/DiagnosticReports"
if [ ! -r "${_reports_dir}" ]; then
    _gone "cannot read ${_reports_dir} (needs membership in _analyticsusers)"
else
    # Only reports newer than the installed bundle. Older ones belong to a build
    # that is no longer on disk, and counting them would make a fixed install
    # look permanently broken -- a check that cannot go green is a check the
    # operator learns to ignore.
    _since=0
    if [ -e "${JARVIS_PLUGIN_PATH}" ]; then
        _since="$(stat -f %m "${JARVIS_PLUGIN_PATH}" 2>/dev/null || echo 0)"
    fi

    _crashes=0
    _newest=""
    for _rep in "${_reports_dir}"/authorizationhosthelper*.ips; do
        [ -e "${_rep}" ] || continue
        _mtime="$(stat -f %m "${_rep}" 2>/dev/null || echo 0)"
        [ "${_mtime}" -ge "${_since}" ] || continue
        _crashes=$(( _crashes + 1 ))
        _newest="${_rep}"
    done

    if [ "${_crashes}" -eq 0 ]; then
        _ok "no crashes since this bundle was installed"
    else
        _bad "${_crashes} crash(es) since install -- the lock screen's plugin host is DYING"

        # Whether it is OUR bug is decidable from the report, so decide it here
        # rather than leaving the operator to interpret a stack trace at a
        # machine that will not unlock.
        if /usr/bin/grep -q "${JARVIS_PLUGIN_NAME}" "${_newest}" 2>/dev/null; then
            printf '      the newest report NAMES %s -- this is our defect\n' "${JARVIS_PLUGIN_NAME}"
        else
            printf '      the newest report does not name %s\n' "${JARVIS_PLUGIN_NAME}"
        fi

        # Faulting symbol: the top frame of the TRIGGERED thread.
        #
        # Both narrowing steps are load-bearing. Without "triggered":true you
        # get thread 0's idle runloop frame, which every report has and which
        # means nothing. Without "frames":[ you get a symbol out of the
        # preceding threadState, where the crash reporter annotates register
        # VALUES that happen to land on known addresses -- the first draft of
        # this reported OBJC_CLASS_$___NSTaggedDate, which was the contents of
        # x14, not a stack frame at all.
        _sym="$(tr -d '\n' < "${_newest}" 2>/dev/null \
                | /usr/bin/sed -e 's/.*"triggered":true//' -e 's/.*"frames":\[//' \
                | /usr/bin/grep -o '"symbol":"[^"]*"' \
                | head -1 | cut -d'"' -f4)"
        [ -n "${_sym}" ] && printf '      faulting frame: %s\n' "${_sym}"
        printf '      report: %s\n' "${_newest}"
        printf '      Remove now, diagnose after:  sudo %s/uninstall.sh\n' "${_here}"
    fi
fi

# --- Verdict ----------------------------------------------------------------
echo
if [ "${_problems}" -gt 0 ]; then
    printf '\033[31m%d problem(s).\033[0m Voice unlock will not work.\n' "${_problems}"
    echo "Your password still will -- the mechanism yields on every failure."
    echo "Repair:  sudo ${_here}/install.sh      (re-derives every requirement)"
    echo "Remove:  sudo ${_here}/uninstall.sh"
    echo
    exit 1
fi

if [ "${_missing}" -gt 0 ]; then
    printf '\033[33mpartially installed.\033[0m %d component(s) absent.\n' "${_missing}"
    echo "Repair:  sudo ${_here}/install.sh"
    echo
    exit 2
fi

printf '\033[32mcoherent.\033[0m Every component agrees about every peer.\n'
echo
echo "Not proven here: that SecurityAgent loads the mechanism. It loads only when"
echo "the screensaver right is evaluated, so lock the screen once, then:"
echo "  log show --predicate 'subsystem == \"com.jarvis.unlockplugin\"' --last 15m"
echo
exit 0
