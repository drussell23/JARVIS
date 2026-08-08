#!/bin/bash
# JARVIS Authorization Plugin -- self-tests for the rule-shape layer.
#
# WHY THIS FILE EXISTS
# --------------------
# install.sh used to author the authorization rule it wrote from a template
# literal, and wrote it over system.login.screensaver -- a right whose stock
# class is `rule`, delegating the entire lock screen to loginwindow. The machine
# then authenticated into a black screen with a live cursor. Every other check in
# this directory stayed green throughout, because every other check asks whether
# the configuration is COHERENT and none of them asked whether it was ALLOWED.
#
# So the code that decides that is exercised here, against fixtures, on every
# build -- `make verify` runs this, and install.sh runs `make verify` as step 1.
# The gate cannot rot silently while the thing it guards is a lock screen.
#
# Runs as any user. Touches no system state: the authorization database is never
# read or written, and the schema oracle is pointed at a fixture. The two tests
# that DO consult the real /System/Library/Security/authorization.plist read it
# and nothing else -- they are the pins that keep the classifier honest about
# this machine.

set -uo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${_here}/common.sh"
# common.sh sets -e for the installers. The tests deliberately drive failure
# paths, so exit-on-error is turned back off here and every expected non-zero is
# captured explicitly.
set +e

# An explicit template rather than `mktemp -t`: on macOS the -t form resolves
# through confstr(_CS_DARWIN_USER_TEMP_DIR) and ignores TMPDIR, which makes the
# scratch location unoverridable in sandboxed and CI environments. The installers
# keep -t because they run as root on a real machine; a test suite has to be
# runnable anywhere.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/jarvis-ruleshape-test.XXXXXX")" \
    || { echo "FATAL: could not create a scratch directory under ${TMPDIR:-/tmp}"; exit 1; }
trap 'rm -rf "${WORK}"' EXIT

_pass=0
_fail=0
_ok()   { _pass=$(( _pass + 1 )); printf '  ok   %s\n' "$1"; }
_no()   { _fail=$(( _fail + 1 )); printf '  FAIL %s\n' "$1"; [ $# -gt 1 ] && printf '         %s\n' "$2"; }
_is()   { # _is <label> <expected> <actual>
    if [ "$2" = "$3" ]; then _ok "$1"; else _no "$1" "expected [$2] got [$3]"; fi
}
_rc()   { # _rc <label> <expected-rc> <actual-rc>
    if [ "$2" = "$3" ]; then _ok "$1"; else _no "$1" "expected rc $2 got rc $3"; fi
}

# --- Fixtures ----------------------------------------------------------------
# Written as XML and normalised through plutil, so a malformed fixture fails here
# rather than being mistaken for a malformed implementation.
_fixture() { # _fixture <name> <inner-xml>
    local path="${WORK}/$1.plist"
    {
        printf '<?xml version="1.0" encoding="UTF-8"?>\n'
        printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        printf '<plist version="1.0"><dict>%s</dict></plist>\n' "$2"
    } > "${path}"
    plutil -lint "${path}" >/dev/null 2>&1 || { echo "FATAL: fixture $1 is malformed"; exit 1; }
    printf '%s' "${path}"
}

DELEGATING="$(_fixture delegating '
    <key>class</key><string>rule</string>
    <key>comment</key><string>The owner or any administrator can unlock.</string>
    <key>rule</key><string>use-login-window-ui</string>
    <key>version</key><integer>1</integer>')"

USERCLASS="$(_fixture userclass '
    <key>class</key><string>user</string>
    <key>group</key><string>admin</string>
    <key>session-owner</key><true/>
    <key>shared</key><false/>')"

# Carries keys the old template literal silently discarded. If composition ever
# regresses to authoring, tries/shared/allow-root vanish and these tests say so.
HOSTED="$(_fixture hosted '
    <key>class</key><string>evaluate-mechanisms</string>
    <key>comment</key><string>Login mechanism based rule.</string>
    <key>mechanisms</key><array>
        <string>builtin:prelogin</string>
        <string>loginwindow:login</string>
        <string>builtin:authenticate,privileged</string>
    </array>
    <key>tries</key><integer>10000</integer>
    <key>shared</key><false/>
    <key>allow-root</key><false/>
    <key>version</key><integer>11</integer>')"

EMPTYCHAIN="$(_fixture emptychain '
    <key>class</key><string>evaluate-mechanisms</string>
    <key>mechanisms</key><array/>')"

NOTAPLIST="${WORK}/garbage.plist"
printf 'this is not a plist\n' > "${NOTAPLIST}"

MECH="JARVISUnlock:grant,privileged"

# A fixture standing in for Apple's schema, so the classifier's behaviour is
# pinned independently of whatever macOS this happens to run on.
TEMPLATE="$(_fixture template '
    <key>rights</key><dict>
        <key>system.login.screensaver</key><dict>
            <key>class</key><string>rule</string>
            <key>rule</key><string>use-login-window-ui</string>
            <key>comment</key><string>set rule to authenticate-session-owner-or-admin to enable SecurityAgent.</string>
        </dict>
        <key>system.login.console</key><dict>
            <key>class</key><string>evaluate-mechanisms</string>
            <key>mechanisms</key><array><string>builtin:authenticate,privileged</string></array>
        </dict>
    </dict>
    <key>rules</key><dict>
        <key>authenticate-session-owner-or-admin</key><dict>
            <key>class</key><string>user</string>
            <key>group</key><string>admin</string>
        </dict>
    </dict>')"

echo
echo "rule shape self-tests"
echo "====================="

# =============================================================================
echo
echo "reading"
# =============================================================================
_is "class of a delegating rule"      "rule"                "$(jarvis_rule_class "${DELEGATING}")"
_is "class of a mechanism chain"      "evaluate-mechanisms" "$(jarvis_rule_class "${HOSTED}")"

_is "mechanisms are returned in order" \
    "builtin:prelogin|loginwindow:login|builtin:authenticate,privileged" \
    "$(jarvis_rule_mechanisms "${HOSTED}" | tr '\n' '|' | sed 's/|$//')"

_is "a rule with no mechanisms key yields nothing" "" "$(jarvis_rule_mechanisms "${DELEGATING}")"
_is "an empty chain yields nothing"                "" "$(jarvis_rule_mechanisms "${EMPTYCHAIN}")"

_is "shape of a delegating rule" \
    "class=rule;mechanisms=" \
    "$(jarvis_rule_shape "${DELEGATING}")"

_is "shape of a mechanism chain" \
    "class=evaluate-mechanisms;mechanisms=builtin:prelogin|loginwindow:login|builtin:authenticate,privileged" \
    "$(jarvis_rule_shape "${HOSTED}")"

# The shape is the evidence key, so it has to be stable against edits that do not
# change behaviour -- otherwise a comment tweak silently invalidates a real
# measurement and the gate starts refusing for no reason.
_cosmetic="${WORK}/cosmetic.plist"
cp "${HOSTED}" "${_cosmetic}"
plutil -replace comment -string 'a completely different comment' "${_cosmetic}" >/dev/null 2>&1
plutil -replace version -integer 99 "${_cosmetic}" >/dev/null 2>&1
_is "shape ignores comment and version" \
    "$(jarvis_rule_shape "${HOSTED}")" "$(jarvis_rule_shape "${_cosmetic}")"

_is "shape contains no whitespace (survives one log field)" \
    "" "$(jarvis_rule_shape "${HOSTED}" | tr -dc '[:space:]')"

_shape_rc=0; jarvis_rule_shape "${NOTAPLIST}" >/dev/null 2>&1 || _shape_rc=$?
_rc "shape of an unparseable file fails" 1 "${_shape_rc}"

# =============================================================================
echo
echo "classifying against the schema"
# =============================================================================
JARVIS_SYSTEM_AUTH_TEMPLATE="${TEMPLATE}"

_is "stock class from the rights bucket" "rule" "$(jarvis_stock_rule_class system.login.screensaver)"
_is "stock class from the rules bucket"  "user" "$(jarvis_stock_rule_class authenticate-session-owner-or-admin)"

_stock_rc=0; jarvis_stock_rule_class no.such.right >/dev/null 2>&1 || _stock_rc=$?
_rc "unknown right is not classified" 1 "${_stock_rc}"

# The dotted name must be escaped or plutil reads it as four levels of nesting
# and every right in the system reads as absent -- which fails closed, but for
# the wrong reason, and would make the gate refuse everything forever.
_is "dotted right names are escaped for plutil" \
    'system\.login\.screensaver' "$(_jarvis_plist_escape_key system.login.screensaver)"

_h=0; jarvis_right_hosts_mechanisms system.login.console >/dev/null 2>&1 || _h=$?
_rc "a mechanism-host right is accepted" 0 "${_h}"

_h=0; jarvis_right_hosts_mechanisms system.login.screensaver >/dev/null 2>&1 || _h=$?
_rc "a delegating right is refused" 2 "${_h}"

_h=0; jarvis_right_hosts_mechanisms authenticate-session-owner-or-admin >/dev/null 2>&1 || _h=$?
_rc "a class=user rule is refused" 2 "${_h}"

_h=0; jarvis_right_hosts_mechanisms no.such.right >/dev/null 2>&1 || _h=$?
_rc "an unknown right is refused as unknown" 1 "${_h}"

_h=0; JARVIS_SYSTEM_AUTH_TEMPLATE="${WORK}/absent.plist" \
    jarvis_right_hosts_mechanisms system.login.console >/dev/null 2>&1 || _h=$?
_rc "an unreadable schema refuses rather than assumes" 1 "${_h}"

# =============================================================================
echo
echo "composing"
# =============================================================================
OUT="${WORK}/composed.plist"

_c=0; jarvis_compose_mechanism_rule "${HOSTED}" "${OUT}" "${MECH}" >/dev/null 2>&1 || _c=$?
_rc "composes onto a mechanism chain" 0 "${_c}"
# sed rather than head/tail on purpose: under pipefail an early-exiting consumer
# SIGPIPEs the producer, which is the exact bug these tests caught in the
# composer. A test that reproduces the trap it is testing for proves nothing.
_is "our mechanism is at the head"  "${MECH}" "$(jarvis_rule_mechanisms "${OUT}" | sed -n '1p')"
_is "the incumbent chain is preserved beneath it" \
    "$(jarvis_rule_mechanisms "${HOSTED}")" "$(jarvis_rule_mechanisms "${OUT}" | sed '1d')"
_is "class is untouched" "evaluate-mechanisms" "$(jarvis_rule_class "${OUT}")"
plutil -lint "${OUT}" >/dev/null 2>&1 && _ok "the composed rule is a valid plist" \
    || _no "the composed rule is a valid plist"

# The specific regression: the template literal wrote four keys and destroyed
# every other one the incumbent had.
for _key in tries shared allow-root version; do
    _is "key '${_key}' survives composition" \
        "$(jarvis_plist_value "${HOSTED}" "${_key}")" "$(jarvis_plist_value "${OUT}" "${_key}")"
done

_c=0; jarvis_compose_mechanism_rule "${OUT}" "${WORK}/again.plist" "${MECH}" >/dev/null 2>&1 || _c=$?
_rc "composing twice is a no-op, not a duplicate" 2 "${_c}"

_c=0; jarvis_compose_mechanism_rule "${EMPTYCHAIN}" "${WORK}/onempty.plist" "${MECH}" >/dev/null 2>&1 || _c=$?
_rc "composes onto an empty chain" 0 "${_c}"
_is "empty chain gains exactly our mechanism" "${MECH}" "$(jarvis_rule_mechanisms "${WORK}/onempty.plist")"

# THE regression. This is the write that black-screened the machine.
_c=0; jarvis_compose_mechanism_rule "${DELEGATING}" "${WORK}/nope.plist" "${MECH}" >/dev/null 2>&1 || _c=$?
_rc "REFUSES to compose over a delegating rule" 3 "${_c}"

_c=0; jarvis_compose_mechanism_rule "${USERCLASS}" "${WORK}/nope2.plist" "${MECH}" >/dev/null 2>&1 || _c=$?
_rc "REFUSES to compose over a class=user rule" 3 "${_c}"

_c=0; jarvis_compose_mechanism_rule "${NOTAPLIST}" "${WORK}/nope3.plist" "${MECH}" >/dev/null 2>&1 || _c=$?
_rc "refuses to compose from an unparseable rule" 1 "${_c}"

_c=0; jarvis_compose_mechanism_rule "${WORK}/absent.plist" "${WORK}/nope4.plist" "${MECH}" >/dev/null 2>&1 || _c=$?
_rc "refuses to compose from a missing file" 1 "${_c}"

_c=0; jarvis_compose_mechanism_rule "${HOSTED}" "${WORK}/nope5.plist" "" >/dev/null 2>&1 || _c=$?
_rc "refuses to compose an empty mechanism" 1 "${_c}"

# =============================================================================
echo
echo "stripping (the inverse -- uninstall depends on this)"
# =============================================================================
ROUND="${WORK}/roundtrip.plist"
cp "${OUT}" "${ROUND}"
_removed="$(jarvis_rule_strip_mechanism "${ROUND}" "${JARVIS_PLUGIN_NAME}")"
_is "strip removes exactly one entry" "1" "${_removed}"
_is "compose then strip returns the original chain" \
    "$(jarvis_rule_mechanisms "${HOSTED}")" "$(jarvis_rule_mechanisms "${ROUND}")"
_is "strip leaves other keys alone" \
    "$(jarvis_plist_value "${HOSTED}" tries)" "$(jarvis_plist_value "${ROUND}" tries)"

# A half-finished install can leave two. Removing one of them would report
# success while the lock screen still runs a mechanism whose bundle is gone.
DUPES="$(_fixture dupes '
    <key>class</key><string>evaluate-mechanisms</string>
    <key>mechanisms</key><array>
        <string>JARVISUnlock:grant,privileged</string>
        <string>builtin:authenticate,privileged</string>
        <string>JARVISUnlock:grant,privileged</string>
    </array>')"
_removed="$(jarvis_rule_strip_mechanism "${DUPES}" "${JARVIS_PLUGIN_NAME}")"
_is "strip removes every matching entry"  "2" "${_removed}"
_is "strip keeps the stock authenticator" "builtin:authenticate,privileged" \
    "$(jarvis_rule_mechanisms "${DUPES}")"

_removed="$(jarvis_rule_strip_mechanism "${DELEGATING}" "${JARVIS_PLUGIN_NAME}")"
_is "strip reports 0 on a rule with no mechanisms" "0" "${_removed}"

_untouched="$(_fixture untouched '
    <key>class</key><string>evaluate-mechanisms</string>
    <key>mechanisms</key><array><string>builtin:authenticate,privileged</string></array>')"
_removed="$(jarvis_rule_strip_mechanism "${_untouched}" "${JARVIS_PLUGIN_NAME}")"
_is "strip reports 0 when we are not in the chain" "0" "${_removed}"
_is "and changes nothing" "builtin:authenticate,privileged" \
    "$(jarvis_rule_mechanisms "${_untouched}")"

# =============================================================================
echo
echo "evidence gate"
# =============================================================================
JARVIS_PROBE_RESULTS_LOG="${WORK}/probe-results.log"
SHAPE_A="class=evaluate-mechanisms;mechanisms=A:x|B:y"
SHAPE_PREFIX="class=evaluate-mechanisms;mechanisms=A:x"

_e=0; jarvis_probe_evidence_for_shape "${SHAPE_A}" >/dev/null 2>&1 || _e=$?
_rc "no log at all is not permission" 1 "${_e}"

{
    printf 'T1 rule=r shape=%s window=180s locked=yes touchid=yes watch=no password=no\n'        "${SHAPE_A}"
    printf 'T2 rule=r shape=%s window=180s locked=no  touchid=yes watch=no password=yes\n'      "${SHAPE_A}"
    printf 'T3 rule=r shape=%s window=180s locked=yes touchid=yes watch=no password=yes\n'      "${SHAPE_PREFIX}"
    printf 'T4 rule=r shape=%s window=180s locked=yes touchid=not-tested watch=no password=yes\n' "${SHAPE_A}"
} > "${JARVIS_PROBE_RESULTS_LOG}"

_e=0; _rec="$(jarvis_probe_evidence_for_shape "${SHAPE_A}")" || _e=$?
_rc "an exact match with locked=yes and password=yes is permission" 0 "${_e}"
case "${_rec}" in T4*) _ok "the qualifying record is the one returned" ;;
                  *)   _no "the qualifying record is the one returned" "got [${_rec}]" ;; esac

# T3 is a strict prefix of SHAPE_A. Substring matching would hand SHAPE_A's
# permission to a chain that is missing a mechanism -- exactly the class of
# "close enough" substitution that caused the incident.
_e=0; jarvis_probe_evidence_for_shape "${SHAPE_PREFIX}${SHAPE_PREFIX}" >/dev/null 2>&1 || _e=$?
_rc "an unmeasured shape gets nothing" 1 "${_e}"

{
    printf 'T5 rule=r shape=%s window=180s locked=yes touchid=yes watch=no password=no\n'  "${SHAPE_A}"
    printf 'T6 rule=r shape=%s window=180s locked=no  touchid=yes watch=no password=yes\n' "${SHAPE_A}"
} > "${JARVIS_PROBE_RESULTS_LOG}"
_e=0; jarvis_probe_evidence_for_shape "${SHAPE_A}" >/dev/null 2>&1 || _e=$?
_rc "password=no and locked=no are both disqualifying" 1 "${_e}"

# =============================================================================
echo
echo "this machine (against the real schema)"
# =============================================================================
# Two pins, in opposite directions. Without the second, a classifier broken to
# always refuse would pass every test above.
JARVIS_SYSTEM_AUTH_TEMPLATE="${JARVIS_SYSTEM_AUTH_TEMPLATE_DEFAULT}"
if [ -r "${JARVIS_SYSTEM_AUTH_TEMPLATE}" ]; then
    # The right the previous design targeted. Pinned as still refused, so a
    # future edit cannot quietly point the installer back at the delegator.
    _h=0; jarvis_right_hosts_mechanisms system.login.screensaver >/dev/null 2>&1 || _h=$?
    _rc "system.login.screensaver stays refused (class $(jarvis_stock_rule_class system.login.screensaver 2>/dev/null || echo '?'))" 2 "${_h}"

    # The right we now target: the chain loginwindow evaluates one level below it.
    _h=0; jarvis_right_hosts_mechanisms "${JARVIS_AUTH_RIGHT}" >/dev/null 2>&1 || _h=$?
    _rc "${JARVIS_AUTH_RIGHT} is a mechanism host (class $(jarvis_stock_rule_class "${JARVIS_AUTH_RIGHT}" 2>/dev/null || echo '?'))" 0 "${_h}"

    _h=0; jarvis_right_hosts_mechanisms system.login.console >/dev/null 2>&1 || _h=$?
    _rc "system.login.console is accepted (the classifier is not just refusing)" 0 "${_h}"

    # The installer must never be pointed at the login right, whatever else
    # changes. A defect there costs login, not unlock.
    if [ "${JARVIS_AUTH_RIGHT}" = "system.login.console" ]; then
        _no "the target right is system.login.console -- a defect there costs LOGIN"
    else
        _ok "the target right is not system.login.console"
    fi
else
    printf '  skip system schema unreadable at %s\n' "${JARVIS_SYSTEM_AUTH_TEMPLATE}"
fi

# =============================================================================
echo
echo "incumbent chain preservation"
# =============================================================================
# What verify.sh asks instead of grepping for a mechanism name it expects. On
# system.login.screensaver.unlock the expected name would have been wrong: there
# is no builtin:authenticate, there is CryptoTokenKit:login.
_c=0; jarvis_compose_mechanism_rule "${HOSTED}" "${WORK}/pres.plist" "${MECH}" >/dev/null 2>&1 || _c=$?
jarvis_chain_preserves_backup "${WORK}/pres.plist" "${HOSTED}" \
    && _ok "a composed chain preserves the incumbent" \
    || _no "a composed chain preserves the incumbent"

# Drop one of the incumbent's mechanisms and it must be caught. This is the
# regression that would mean smartcard unlock had been silently removed.
cp "${WORK}/pres.plist" "${WORK}/lossy.plist"
_dropped="$(jarvis_rule_strip_mechanism "${WORK}/lossy.plist" "loginwindow:login")"
_is "the fixture actually lost a mechanism" "1" "${_dropped}"
jarvis_chain_preserves_backup "${WORK}/lossy.plist" "${HOSTED}" \
    && _no "a chain that dropped an incumbent mechanism is caught" \
    || _ok "a chain that dropped an incumbent mechanism is caught"

jarvis_chain_preserves_backup "${HOSTED}" "${DELEGATING}" \
    && _ok "a backup with no mechanisms demands nothing" \
    || _no "a backup with no mechanisms demands nothing"

# =============================================================================
echo
echo "fail-open evidence (the tries=1 bar)"
# =============================================================================
# A shape naming OUR mechanism must clear failopen=proven. A shape that does not
# keeps the old bar, because it has no mechanism of ours to fail open from.
JARVIS_PROBE_RESULTS_LOG="${WORK}/failopen.log"
MECH_SHAPE="class=evaluate-mechanisms;mechanisms=${MECH}|CryptoTokenKit:login"

{
    printf 'T1 shape=%s locked=yes prompted=yes touchid=yes watch=no password=yes failopen=yielded-unconfirmed\n' "${MECH_SHAPE}"
    printf 'T2 shape=%s locked=yes prompted=no  touchid=no  watch=no password=yes failopen=crashed\n'             "${MECH_SHAPE}"
    printf 'T3 shape=%s locked=yes prompted=no  touchid=no  watch=no password=yes failopen=not-reached\n'         "${MECH_SHAPE}"
} > "${JARVIS_PROBE_RESULTS_LOG}"
_e=0; jarvis_probe_evidence_for_shape "${MECH_SHAPE}" >/dev/null 2>&1 || _e=$?
_rc "password=yes alone does NOT authorise a mechanism shape" 1 "${_e}"

printf 'T4 shape=%s locked=yes prompted=yes touchid=yes watch=no password=yes failopen=proven\n' \
    "${MECH_SHAPE}" >> "${JARVIS_PROBE_RESULTS_LOG}"
_e=0; _rec="$(jarvis_probe_evidence_for_shape "${MECH_SHAPE}")" || _e=$?
_rc "failopen=proven authorises a mechanism shape" 0 "${_e}"
case "${_rec}" in T4*) _ok "the proven record is the one returned" ;;
                  *)   _no "the proven record is the one returned" "got [${_rec}]" ;; esac

# A crashed run must never become permission, however many times it is repeated.
{
    printf 'C1 shape=%s locked=yes prompted=yes touchid=yes watch=no password=yes failopen=crashed\n' "${MECH_SHAPE}"
    printf 'C2 shape=%s locked=yes prompted=yes touchid=yes watch=no password=yes failopen=crashed\n' "${MECH_SHAPE}"
} > "${JARVIS_PROBE_RESULTS_LOG}"
_e=0; jarvis_probe_evidence_for_shape "${MECH_SHAPE}" >/dev/null 2>&1 || _e=$?
_rc "a crashed run is never permission" 1 "${_e}"

# Non-mechanism shapes keep the old bar: there is nothing of ours to fail open.
NONMECH_SHAPE="class=user;mechanisms="
printf 'R1 shape=%s locked=yes prompted=yes touchid=yes watch=no password=yes\n' \
    "${NONMECH_SHAPE}" > "${JARVIS_PROBE_RESULTS_LOG}"
_e=0; jarvis_probe_evidence_for_shape "${NONMECH_SHAPE}" >/dev/null 2>&1 || _e=$?
_rc "a shape without our mechanism does not need failopen" 0 "${_e}"

# =============================================================================
echo
echo "crash-report walk"
# =============================================================================
JARVIS_CRASH_REPORTS_DIR="${WORK}/reports"
JARVIS_AUTH_HOST_PROCESS="fakehost"
mkdir -p "${JARVIS_CRASH_REPORTS_DIR}"
_is "an empty report directory yields nothing" "" "$(jarvis_crash_reports_since 0)"

printf '{"triggered":true,"frames":[{"symbol":"JARVISDeliver"}]}\n' \
    > "${JARVIS_CRASH_REPORTS_DIR}/fakehost.arm64-old.ips"
touch -t 200001010000 "${JARVIS_CRASH_REPORTS_DIR}/fakehost.arm64-old.ips"
_is "a report older than the cutoff is excluded" "" "$(jarvis_crash_reports_since "$(date +%s)")"
_is "and included when the cutoff precedes it" \
    "${JARVIS_CRASH_REPORTS_DIR}/fakehost.arm64-old.ips" "$(jarvis_crash_reports_since 0)"

# The parse that must not read a register annotation as a stack frame.
_is "the faulting symbol comes from the triggered thread" \
    "JARVISDeliver" "$(jarvis_crash_faulting_symbol "${JARVIS_CRASH_REPORTS_DIR}/fakehost.arm64-old.ips")"

printf '{"threadState":{"x14":{"symbol":"OBJC_CLASS_$___NSTaggedDate"}},"triggered":true,"frames":[{"symbol":"realFrame"}]}\n' \
    > "${JARVIS_CRASH_REPORTS_DIR}/fakehost.arm64-reg.ips"
_is "a register annotation is not mistaken for a frame" \
    "realFrame" "$(jarvis_crash_faulting_symbol "${JARVIS_CRASH_REPORTS_DIR}/fakehost.arm64-reg.ips")"

_e=0; JARVIS_CRASH_REPORTS_DIR="${WORK}/nonexistent" jarvis_crash_reports_since 0 >/dev/null 2>&1 || _e=$?
_rc "an unreadable report directory is distinguished from no crashes" 1 "${_e}"

# =============================================================================
echo
echo "restore-point provenance"
# =============================================================================
# The pointer file records a path and nothing else, and the target right has
# already changed once. A pointer left by an install of the OLD right would
# otherwise be restored into the NEW one -- a class=rule delegating definition
# written over a mechanism chain, by the recovery path, on a machine already in
# trouble. Only the refusal is exercised here: the accepting path writes to the
# authorization database and needs root, which a test suite must not require.
JARVIS_AUTHDB_BACKUP_POINTER="${WORK}/pointer"

_wrong="${WORK}/system.login.screensaver.20260807-000000.install.plist"
cp "${DELEGATING}" "${_wrong}"
printf '%s' "${_wrong}" > "${JARVIS_AUTHDB_BACKUP_POINTER}"
_e=0; jarvis_restore_auth_rule_from_pointer >/dev/null 2>&1 || _e=$?
_rc "a backup taken for a DIFFERENT right is refused" 1 "${_e}"

# The prefix trap in the other direction: system.login.screensaver.unlock has
# system.login.screensaver as a prefix, so a bare "<right>.*" match would accept
# the longer name for the shorter right.
_longer="${WORK}/system.login.screensaver.unlock.20260807-000000.install.plist"
cp "${DELEGATING}" "${_longer}"
printf '%s' "${_longer}" > "${JARVIS_AUTHDB_BACKUP_POINTER}"
_e=0; JARVIS_AUTH_RIGHT="system.login.screensaver" \
    jarvis_restore_auth_rule_from_pointer >/dev/null 2>&1 || _e=$?
_rc "a longer right name cannot satisfy a shorter one" 1 "${_e}"

printf '%s' "${WORK}/absent-backup.plist" > "${JARVIS_AUTHDB_BACKUP_POINTER}"
_e=0; jarvis_restore_auth_rule_from_pointer >/dev/null 2>&1 || _e=$?
_rc "a pointer naming a missing file is refused" 1 "${_e}"

rm -f "${JARVIS_AUTHDB_BACKUP_POINTER}"
_e=0; jarvis_restore_auth_rule_from_pointer >/dev/null 2>&1 || _e=$?
_rc "no pointer at all is refused" 1 "${_e}"

# =============================================================================
echo
if [ "${_fail}" -ne 0 ]; then
    printf 'rule shape: %d passed, %d FAILED\n\n' "${_pass}" "${_fail}"
    exit 1
fi
printf 'rule shape: %d passed\n\n' "${_pass}"
