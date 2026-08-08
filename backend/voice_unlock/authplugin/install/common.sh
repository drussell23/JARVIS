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
# system.login.console is deliberately NEVER touched: a defect there costs you
# login, not just unlock, and the difference is the difference between
# "annoying" and "boot from Recovery".
#
# NOT system.login.screensaver either, which is what this used to be. That right
# is class=rule delegating to use-login-window-ui -- it hands the whole lock
# screen to loginwindow, panel and password field and the session resume that
# re-attaches WindowServer. Converting it to a mechanism chain replaced a user
# interface with an authenticator and black-screened the machine.
#
# loginwindow, in turn, evaluates system.login.screensaver.unlock -- and THAT is
# a real evaluate-mechanisms chain, shipping CryptoTokenKit:login, which is how
# smartcard unlock already works. It is the supported host, one level below the
# delegator the previous design was destroying.
#
# TWO PROPERTIES OF THIS RIGHT MAKE IT UNFORGIVING, AND BOTH ARE LOAD-BEARING:
#
#   tries = 1        One attempt. Nothing retries on our behalf.
#
#   no builtin:authenticate in the chain
#                    The old design put the stock authenticator directly behind
#                    us, so "we yield and it prompts" was a property of the
#                    chain itself. Here it is not. Whatever password path exists
#                    belongs to loginwindow, OUTSIDE this chain, and whether it
#                    is reached is a MEASUREMENT -- see probe_screensaver_rule.sh
#                    and its fail-open battery. Do not assume it. It has to be
#                    proven on the machine, under the dead man's switch, before
#                    an install is allowed anywhere near it.
#
# Apple's own comment on this right is "Do not modify." That is not decoration:
# it means unsupported, and it means an OS update may rewrite it without notice.
#
# Selectable so the lifecycle probe can exercise a synthetic right of our own.
# That does NOT reopen the original defect: the installer's gate asks the schema
# what CLASS a right is, never what it is CALLED, so pointing this back at
# system.login.screensaver is still refused. A name-based check is the kind that
# rots; this one cannot.
JARVIS_AUTH_RIGHT="${JARVIS_AUTH_RIGHT:-system.login.screensaver.unlock}"

# Rights in our own reverse-DNS namespace are ones we created. Nothing in macOS
# consults them, so they are exempt from the login-rights guard below -- which
# otherwise fails closed on any right absent from Apple's schema.
JARVIS_RIGHT_NAMESPACE="com.jarvis."

# --- Sentinel ----------------------------------------------------------------
# The recovery scripts are installed to a fixed system path, not run from the
# repository. A machine that will not unlock is a bad time to discover that the
# checkout lives in a cloud-synced directory that has not mounted yet, and the
# sentinel launchd invokes must not hold a path into someone's home directory.
JARVIS_SYSTEM_TOOLS_DIR="/usr/local/libexec/jarvis-authplugin"
JARVIS_SENTINEL_LABEL="com.jarvis.unlocksentinel"
JARVIS_SENTINEL_BIN="${JARVIS_SYSTEM_TOOLS_DIR}/sentinel.sh"
JARVIS_SENTINEL_PLIST="/Library/LaunchDaemons/${JARVIS_SENTINEL_LABEL}.plist"
# Scripts copied to JARVIS_SYSTEM_TOOLS_DIR. sentinel needs common; uninstall and
# verify are there so recovery and diagnosis work from a bare SSH session.
JARVIS_SYSTEM_TOOLS="common.sh sentinel.sh uninstall.sh verify.sh"
# The authorization database itself. Watched so a rule edited BY HAND -- the way
# this machine got into trouble in the first place -- is noticed like any other
# drift. No installer gate can cover that path; only something watching can.
JARVIS_AUTHDB_FILE="${JARVIS_AUTHDB_FILE:-/var/db/auth.db}"

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
#
# THE MUTATION CHOKEPOINT. install, probe, uninstall-restore and uninstall-strip
# all pass through here, which is why the login-rights guard lives here and not
# in any of them. A guard replicated at four call sites is a guard with four
# chances to be forgotten by the fifth.
jarvis_authdb_write() {
    local right="$1" plist="$2"
    [ -f "$plist" ] || _jarvis_die "rule plist not found: $plist"

    # Refuse to place OUR MECHANISM into a right that performs login.
    #
    # Scoped to rules that name us, deliberately. Restoring a backup and writing
    # a stripped rule both pass, because both REMOVE us -- and a safety check
    # that blocks the recovery path is how someone ends up stranded at a lock
    # screen holding a script that will not help them.
    if grep -q "${JARVIS_PLUGIN_NAME}" "${plist}" 2>/dev/null \
       && jarvis_right_performs_login "${right}"; then
        _jarvis_warn "refusing to place ${JARVIS_PLUGIN_NAME} into ${right}"
        _jarvis_warn "  its stock chain runs a ${JARVIS_LOGIN_PLUGIN_PREFIX} mechanism, so this right"
        _jarvis_warn "  performs LOGIN. A defect there costs you the machine, not the lock screen."
        return 1
    fi

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

    # The backup has to be a backup OF THIS RIGHT.
    #
    # The pointer file records a path and nothing else, and the target right has
    # already changed once -- from system.login.screensaver to
    # system.login.screensaver.unlock. A pointer left behind by an install of the
    # old target would be restored, faithfully and with a success message, into
    # the NEW right: writing a class=rule delegating definition over a mechanism
    # chain. That is not a restore, it is a second conversion, performed by the
    # recovery path, on a machine already in trouble.
    #
    # Every backup this system writes embeds the right in its filename, so the
    # provenance is already recorded; it was simply never checked.
    # The timestamp is part of the pattern, not decoration. Matching on
    # "<right>.*" alone would let a backup of system.login.screensaver.unlock
    # satisfy a check for system.login.screensaver, because the longer name has
    # the shorter one as a prefix -- the wrong direction of the same mistake.
    case "$(basename "${backup}")" in
        "${JARVIS_AUTH_RIGHT}."[0-9]*) : ;;
        *)
            _jarvis_warn "restore point is not for ${JARVIS_AUTH_RIGHT}: ${backup}"
            return 1
            ;;
    esac

    # A backup that already names the plugin is not a restore point.
    #
    # Reinstalling over an existing install used to overwrite the pointer with a
    # capture of the ALREADY-MODIFIED rule. Uninstall then faithfully restored
    # that, logged "authorization rule restored from backup", and returned --
    # putting the mechanism straight back while the bundle it names had just been
    # deleted. That state is strictly worse than doing nothing: SecurityAgent
    # cannot load a missing mechanism, so the chain fails before reaching
    # builtin:authenticate and the screen has no authenticator at all.
    if grep -q "${JARVIS_PLUGIN_NAME}" "${backup}" 2>/dev/null; then
        _jarvis_warn "backup names ${JARVIS_PLUGIN_NAME}; not a restore point: ${backup}"
        return 1
    fi

    jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${backup}" >/dev/null 2>&1 || return 1

    # Prove the write achieved the GOAL, not merely that it succeeded. A restore
    # that returns 0 with our mechanism still live is precisely the failure this
    # function exists to prevent, and the caller has a working fallback -- but
    # only if we admit we did not do the job.
    if jarvis_rule_references_plugin; then
        _jarvis_warn "restored ${backup} but the live rule still names ${JARVIS_PLUGIN_NAME}"
        return 1
    fi

    rm -f "${JARVIS_AUTHDB_BACKUP_POINTER}"
    return 0
}

# Does the live rule currently name our mechanism?
jarvis_rule_references_plugin() {
    jarvis_authdb_read "${JARVIS_AUTH_RIGHT}" 2>/dev/null | grep -q "${JARVIS_PLUGIN_NAME}"
}

# =============================================================================
# AUTHORIZATION RULE SHAPE
# =============================================================================
# This section exists because of one specific incident, and every function in it
# is shaped by that incident.
#
# install.sh used to AUTHOR the rule it wrote, from a template literal in its own
# body: class = evaluate-mechanisms, mechanisms = ( ours, builtin:authenticate ).
# It wrote that over system.login.screensaver, whose stock definition is
#
#     class = rule
#     rule  = use-login-window-ui
#
# use-login-window-ui delegates the right to loginwindow, which owns the lock
# screen: the wallpaper, the clock, the user's name, the password field, and the
# session resume that re-attaches WindowServer after a successful authentication.
# An evaluate-mechanisms chain only AUTHENTICATES. Nothing in it paints a panel
# and nothing in it resumes a session -- so the machine authenticated into a
# black screen with a live cursor and no way back.
#
# The measurement that authorised that change had been taken against a DIFFERENT
# shape. probe_screensaver_rule.sh swapped the right to
# authenticate-session-owner-or-admin -- which is class = user, a SecurityAgent-
# evaluated rule with no mechanisms array at all -- confirmed Touch ID and the
# password still worked, and that result was spent to justify writing a mechanism
# chain nobody had ever run. Two different configurations; only one was measured.
#
# Hence three rules, each enforced by a function below:
#
#   1. NEVER AUTHOR A RULE. Derive what is written from the bytes of the rule
#      that is already live, changing only what must change. A template literal
#      cannot know what it is destroying.
#
#   2. ASK APPLE'S SCHEMA whether a right can host a mechanism, and ask it about
#      the STOCK definition rather than the live one. On a reinstall the live
#      rule is our own previous mutation, so a check that consulted it would
#      cheerfully ratify the damage it exists to detect.
#
#   3. A SHAPE MAY ONLY BE WRITTEN IF THAT EXACT SHAPE HAS BEEN MEASURED on this
#      machine. Not a similar one, and not one a comment claims is equivalent.
#
# Everything here uses plutil, which install.sh already depends on and which
# Recovery ships. No python: uninstall.sh must run from a Recovery shell.

# The class an authorization right must have in order to evaluate a mechanism
# chain. Deliberately NOT environment-overridable -- it is a constant of the
# macOS authorization schema, not a tunable, and an env-overridable version would
# be a one-variable bypass of the only check standing between a template literal
# and the lock screen.
JARVIS_MECHANISM_HOST_CLASS="evaluate-mechanisms"

# The plugin that performs login. A right whose STOCK chain runs one of its
# mechanisms is a login right, whatever it happens to be named.
#
# The colon is part of the prefix and is load-bearing. Matching the substring
# "login" would classify CryptoTokenKit:login as a login mechanism -- and that is
# the sole entry in system.login.screensaver.unlock, the right we actually want.
# A guard that bans its own target is worse than no guard: it gets removed.
# (Same shape as `"lock" in "unlock my screen"` being true.)
JARVIS_LOGIN_PLUGIN_PREFIX="loginwindow:"

# The authoritative schema for what every right's stock definition is. Overridable
# ONLY so the self-tests can point the classifier at a fixture; install.sh pins it
# back to the system path and refuses to run against anything else, so this is
# testability without a production bypass.
JARVIS_SYSTEM_AUTH_TEMPLATE_DEFAULT="/System/Library/Security/authorization.plist"
JARVIS_SYSTEM_AUTH_TEMPLATE="${JARVIS_SYSTEM_AUTH_TEMPLATE:-${JARVIS_SYSTEM_AUTH_TEMPLATE_DEFAULT}}"

# Where probe_screensaver_rule.sh records what it measured, and where install.sh
# looks for permission to write a shape. One definition, because a producer and a
# consumer that disagree about a path is a gate that silently never fires.
JARVIS_PROBE_RESULTS_LOG="${JARVIS_STATE_DIR}/probe-results.log"

# plutil key paths are dot-separated, so a right named "system.login.screensaver"
# has to have its own dots escaped or it reads as a four-level nesting.
_jarvis_plist_escape_key() { printf '%s' "$1" | sed 's/\./\\./g'; }

# Read one value out of a plist. The single place that shells out to plutil for
# reads, so there is one definition of "how do we ask a plist a question".
jarvis_plist_value() {
    local file="$1" keypath="$2"
    [ -f "${file}" ] || return 1
    plutil -extract "${keypath}" raw -o - "${file}" 2>/dev/null
}

jarvis_rule_class() { jarvis_plist_value "$1" class; }

# Emit a rule's mechanisms, one per line, in order.
#
# Bounded by the array's OWN element count rather than by walking until an
# extraction fails: a count is an exact answer the file already contains, and a
# sentinel loop over an unfamiliar plist shape is a guess with no upper bound.
# Returns 1 if an element the count promised cannot be read -- a malformed rule
# must not read as a short one.
jarvis_rule_mechanisms() {
    local file="$1" count idx value
    count="$(jarvis_plist_value "${file}" mechanisms 2>/dev/null || true)"
    # Absent key, or a non-array whose raw form is not a count: no mechanisms.
    case "${count}" in ''|*[!0-9]*) return 0 ;; esac

    idx=0
    while [ "${idx}" -lt "${count}" ]; do
        value="$(jarvis_plist_value "${file}" "mechanisms.${idx}")" || return 1
        printf '%s\n' "${value}"
        idx=$(( idx + 1 ))
    done
}

# Exact-line membership test over a newline-delimited list.
#
# Exists because `producer | grep -qxF needle` is wrong here and looks right.
# These scripts run under `set -o pipefail`, and grep -q exits the instant it
# matches -- which SIGPIPEs the producer, fails the pipeline, and inverts the
# test: the chain that DID contain the mechanism reported that it did not. The
# same trap applies to `| head -1`. Consumers of jarvis_rule_mechanisms must
# either read all of their input or, as here, not be a pipeline at all.
_jarvis_lines_contain() {
    local list="$1" needle="$2" line
    [ -n "${list}" ] || return 1
    while IFS= read -r line; do
        [ "${line}" = "${needle}" ] && return 0
    done <<EOF
${list}
EOF
    return 1
}

# The canonical identity of a rule's SHAPE: its class and its ordered mechanism
# list, and nothing else.
#
# This is the key the evidence gate is stated in, so what it deliberately omits
# matters as much as what it includes. comment and version are documentation and
# bookkeeping; two rules differing only there behave identically, and letting
# them read as different shapes would invalidate a real measurement over a
# cosmetic edit. Contains no spaces by construction, so it survives being one
# field in a whitespace-delimited log line.
jarvis_rule_shape() {
    local file="$1" class mechs
    class="$(jarvis_rule_class "${file}" 2>/dev/null || true)"
    [ -n "${class}" ] || return 1
    mechs="$(jarvis_rule_mechanisms "${file}" | tr '\n' '|' | sed 's/|$//')" || return 1
    printf 'class=%s;mechanisms=%s' "${class}" "${mechs}"
}

# The class a right has in Apple's schema, before anyone on this machine touched
# it. Rights and rules live in separate buckets of the same file; a right can be
# in either, so both are consulted.
jarvis_stock_rule_class() {
    local right="$1" key bucket value
    [ -r "${JARVIS_SYSTEM_AUTH_TEMPLATE}" ] || return 1
    key="$(_jarvis_plist_escape_key "${right}")"
    for bucket in rights rules; do
        value="$(jarvis_plist_value "${JARVIS_SYSTEM_AUTH_TEMPLATE}" "${bucket}.${key}.class" || true)"
        if [ -n "${value}" ]; then
            printf '%s' "${value}"
            return 0
        fi
    done
    return 1
}

# Human-readable stock definition, for the refusal message. An operator being
# told "no" is owed the evidence, and Apple's own comment on the right is usually
# the most useful sentence available -- on system.login.screensaver it names the
# supported alternative outright.
jarvis_stock_rule_summary() {
    local right="$1" key bucket found=0 value
    [ -r "${JARVIS_SYSTEM_AUTH_TEMPLATE}" ] || return 1
    key="$(_jarvis_plist_escape_key "${right}")"
    for bucket in rights rules; do
        jarvis_plist_value "${JARVIS_SYSTEM_AUTH_TEMPLATE}" "${bucket}.${key}.class" >/dev/null 2>&1 \
            || continue
        found=1
        for field in class rule comment; do
            value="$(jarvis_plist_value "${JARVIS_SYSTEM_AUTH_TEMPLATE}" "${bucket}.${key}.${field}" || true)"
            [ -n "${value}" ] && printf '%s: %s\n' "${field}" "${value}"
        done
        value="$(jarvis_plist_value "${JARVIS_SYSTEM_AUTH_TEMPLATE}" "${bucket}.${key}.mechanisms" || true)"
        [ -n "${value}" ] && printf 'mechanisms: %s entr(y/ies)\n' "${value}"
        break
    done
    [ "${found}" -eq 1 ]
}

# THE GATE. Can this right host a mechanism chain at all?
#
#   0  yes -- stock class is the mechanism-host class
#   2  no  -- stock class is something else; converting it destroys a delegation
#   1  unknown -- schema unreadable or the right is not in it
#
# 1 and 2 are distinguished because they need different messages, not because
# they need different outcomes. Both fail closed.
jarvis_right_hosts_mechanisms() {
    local stock
    stock="$(jarvis_stock_rule_class "$1")" || return 1
    [ "${stock}" = "${JARVIS_MECHANISM_HOST_CLASS}" ] || return 2
    return 0
}

# Does this right perform LOGIN, as opposed to unlock or anything else?
#
# Derived from the stock chain rather than from a list of names, so a login right
# introduced by a future macOS is protected the day it ships and nobody has to
# remember to add it. Verified against the live schema in both directions:
#
#   protected  system.login.console / .filevault / .fus   -- all run loginwindow:
#   allowed    system.login.screensaver.unlock            -- CryptoTokenKit:login
#   allowed    system.restart / system.disk.unlock        -- no loginwindow:
#
# Returns 0 (protected) for a right that is not in the schema at all. An unknown
# right could be anything, and this is the one question where a guess costs you
# the machine. Rights in our own namespace are exempt -- we created them, so
# their provenance is not in doubt.
jarvis_right_performs_login() {
    local right="$1" key bucket count idx mech

    case "${right}" in "${JARVIS_RIGHT_NAMESPACE}"*) return 1 ;; esac
    [ -r "${JARVIS_SYSTEM_AUTH_TEMPLATE}" ] || return 0

    key="$(_jarvis_plist_escape_key "${right}")"
    for bucket in rights rules; do
        jarvis_plist_value "${JARVIS_SYSTEM_AUTH_TEMPLATE}" "${bucket}.${key}.class" >/dev/null 2>&1 \
            || continue

        count="$(jarvis_plist_value "${JARVIS_SYSTEM_AUTH_TEMPLATE}" "${bucket}.${key}.mechanisms" || true)"
        case "${count}" in ''|*[!0-9]*) return 1 ;; esac

        idx=0
        while [ "${idx}" -lt "${count}" ]; do
            mech="$(jarvis_plist_value "${JARVIS_SYSTEM_AUTH_TEMPLATE}" "${bucket}.${key}.mechanisms.${idx}" || true)"
            case "${mech}" in "${JARVIS_LOGIN_PLUGIN_PREFIX}"*) return 0 ;; esac
            idx=$(( idx + 1 ))
        done
        return 1
    done
    return 0
}

# Derive the rule to write from the rule that is live.
#
#   0  composed -- out is the incumbent plus our mechanism at the head
#   2  no-op    -- the incumbent already names it; out is a copy, unchanged
#   3  refused  -- the incumbent is not a mechanism chain
#   1  error
#
# The output starts as a byte copy of the incumbent, so every key we have no
# opinion about -- tries, shared, allow-root, version, anything a future macOS
# adds -- survives untouched. The previous template literal silently dropped all
# of them and invented values of its own.
jarvis_compose_mechanism_rule() {
    local incumbent="$1" out="$2" mechanism="$3"
    local class composed head rest original comment

    [ -f "${incumbent}" ] || return 1
    [ -n "${mechanism}" ] || return 1
    class="$(jarvis_rule_class "${incumbent}" 2>/dev/null || true)"
    [ -n "${class}" ] || return 1
    [ "${class}" = "${JARVIS_MECHANISM_HOST_CLASS}" ] || return 3

    original="$(jarvis_rule_mechanisms "${incumbent}")" || return 1

    cp "${incumbent}" "${out}" 2>/dev/null || return 1

    # Idempotent. A reinstall must not stack a second copy of our mechanism onto
    # a chain that already runs it.
    if _jarvis_lines_contain "${original}" "${mechanism}"; then
        return 2
    fi

    # Head position, not appended. Ours either grants or yields, and a yield has
    # to still reach whatever the right already had. Appended, it would run after
    # the stock authenticator -- where a grant is worthless because the user has
    # already typed their password.
    plutil -insert "mechanisms.0" -string "${mechanism}" "${out}" >/dev/null 2>&1 || return 1

    # Prove the file on disk is what was intended, rather than trusting that
    # plutil returned 0. The composed chain must differ from the incumbent by
    # exactly one entry, in exactly one position.
    composed="$(jarvis_rule_mechanisms "${out}")" || return 1
    # Split by parameter expansion rather than head/tail: no subprocess, and
    # nothing that can early-exit into the SIGPIPE trap described above.
    head="${composed%%$'\n'*}"
    if [ "${composed}" = "${head}" ]; then rest=""; else rest="${composed#*$'\n'}"; fi
    [ "${head}" = "${mechanism}" ] || return 1
    [ "${rest}" = "${original}" ] || return 1
    [ "$(jarvis_rule_class "${out}")" = "${class}" ] || return 1

    # Provenance, best-effort and deliberately last: the rule is what a stranded
    # operator reads with `security authorizationdb read`, and it should name its
    # own undo. A failure here is not a failure of the composition -- comment is
    # documentation, the authorization engine never evaluates it, and it is
    # excluded from the shape identity above -- so it must not fail the write.
    comment="$(jarvis_plist_value "${out}" comment || true)"
    if [ -n "${comment}" ]; then
        plutil -replace comment -string \
            "${comment} [${JARVIS_PLUGIN_NAME}: added ${mechanism}; remove with install/uninstall.sh]" \
            "${out}" >/dev/null 2>&1 || true
    else
        plutil -insert comment -string \
            "[${JARVIS_PLUGIN_NAME}: added ${mechanism}; remove with install/uninstall.sh]" \
            "${out}" >/dev/null 2>&1 || true
    fi
    return 0
}

# Remove every mechanism entry mentioning a needle. Echoes how many were removed.
#
# Walks BACKWARDS. A forward walk has to deliberately not advance its index after
# a delete, because the entries shift down -- correct, but the kind of correct
# that breaks the first time someone tidies the loop. Iterating from the end
# means no delete can ever move an entry that has not been visited yet.
jarvis_rule_strip_mechanism() {
    local file="$1" needle="$2" count idx value removed=0
    count="$(jarvis_plist_value "${file}" mechanisms 2>/dev/null || true)"
    case "${count}" in ''|*[!0-9]*) printf '0'; return 0 ;; esac

    idx=$(( count - 1 ))
    while [ "${idx}" -ge 0 ]; do
        value="$(jarvis_plist_value "${file}" "mechanisms.${idx}" || true)"
        case "${value}" in
            *"${needle}"*)
                if plutil -remove "mechanisms.${idx}" "${file}" >/dev/null 2>&1; then
                    removed=$(( removed + 1 ))
                fi
                ;;
        esac
        idx=$(( idx - 1 ))
    done
    printf '%s' "${removed}"
}

# Has THIS shape been measured on THIS machine, with the password still working?
#
# Matches shape as a whole whitespace-delimited field rather than by substring,
# so a shape that merely starts with another shape's text cannot borrow its
# evidence -- which is the entire class of mistake this gate exists to stop.
# locked=yes is required because a probe run in which the operator never locked
# the screen measured nothing, and the probe records that fact honestly.
# A shape that actually contains OUR mechanism has to clear a higher bar:
# failopen=proven. Derived from the shape itself rather than passed in, so a
# caller cannot ask the easier question by accident.
#
# The reason is specific to system.login.screensaver.unlock. The old right had
# builtin:authenticate directly behind us, so "we yield and the stock
# authenticator prompts" was a property of the chain. This one has no password
# mechanism in it and tries=1 -- whatever prompt exists belongs to loginwindow,
# outside the chain, and whether a yield reaches it is not derivable from the
# configuration. Only a run that yielded, survived, and still got a password
# prompt establishes it. A shape that merely swaps in a named rule keeps the old
# bar, because it has no mechanism of ours to fail open FROM.
jarvis_probe_evidence_for_shape() {
    local shape="$1" need_failopen=0
    [ -n "${shape}" ] || return 1
    [ -r "${JARVIS_PROBE_RESULTS_LOG}" ] || return 1
    case "${shape}" in *"${JARVIS_MECHANISM}"*) need_failopen=1 ;; esac
    awk -v want="shape=${shape}" -v needfo="${need_failopen}" '
        {
            s = 0; p = 0; l = 0; f = 0
            for (i = 1; i <= NF; i++) {
                if ($i == want)             s = 1
                if ($i == "password=yes")   p = 1
                if ($i == "locked=yes")     l = 1
                if ($i == "failopen=proven") f = 1
            }
            if (s && p && l && (needfo != "1" || f)) last = $0
        }
        END { if (last != "") { print last; exit 0 } exit 1 }
    ' "${JARVIS_PROBE_RESULTS_LOG}"
}

# =============================================================================
# RUNTIME EVIDENCE
# =============================================================================
# Everything above this line inspects CONFIGURATION. None of it can see a
# mechanism that loads correctly and then dies, which is precisely what happened:
# 27 segfaults across two days while every static check reported the install
# coherent, because the mechanism runs inside a process we do not own and its
# corpses land in DiagnosticReports rather than anywhere anyone was looking.
#
# These are the primitives for asking what actually HAPPENED. Defined here, not
# in verify.sh, because the probe needs the identical questions -- and a probe
# that measured "did it crash" differently from the verifier would be two
# opinions about one machine.

JARVIS_LOG_SUBSYSTEM_PLUGIN="com.jarvis.unlockplugin"
JARVIS_CRASH_REPORTS_DIR="${JARVIS_CRASH_REPORTS_DIR:-/Library/Logs/DiagnosticReports}"
# The process that hosts a SecurityAgent mechanism. Ours dying means THIS dying.
JARVIS_AUTH_HOST_PROCESS="${JARVIS_AUTH_HOST_PROCESS:-authorizationhosthelper}"

jarvis_plugin_bundle_present() { [ -d "${JARVIS_PLUGIN_PATH}" ]; }

# Paths of host crash reports modified at or after <epoch>, one per line.
# Emits nothing (rc 0) when there are none; rc 1 only if the directory is
# unreadable, which needs membership in _analyticsusers and is a different fact
# from "there were no crashes".
jarvis_crash_reports_since() {
    local since="$1" rep mtime
    [ -r "${JARVIS_CRASH_REPORTS_DIR}" ] || return 1
    for rep in "${JARVIS_CRASH_REPORTS_DIR}/${JARVIS_AUTH_HOST_PROCESS}"*.ips; do
        [ -e "${rep}" ] || continue
        mtime="$(stat -f %m "${rep}" 2>/dev/null || echo 0)"
        [ "${mtime}" -ge "${since}" ] || continue
        printf '%s\n' "${rep}"
    done
    return 0
}

# The faulting symbol of a crash report: the top frame of the TRIGGERED thread.
#
# Both narrowing steps are load-bearing. Without "triggered":true you get thread
# 0's idle runloop frame, which every report has and which means nothing.
# Without "frames":[ you get a symbol out of the preceding threadState, where the
# crash reporter annotates register VALUES that happen to land on known
# addresses -- the first draft of this reported OBJC_CLASS_$___NSTaggedDate,
# which was the contents of x14 and not a stack frame at all.
jarvis_crash_faulting_symbol() {
    local report="$1"
    [ -r "${report}" ] || return 1
    tr -d '\n' < "${report}" 2>/dev/null \
        | sed -e 's/.*"triggered":true//' -e 's/.*"frames":\[//' \
        | grep -o '"symbol":"[^"]*"' \
        | head -1 | cut -d'"' -f4
}

# Unified-log lines from our subsystems and from the mechanism host, since a
# `log show`-formatted start time ("YYYY-MM-DD HH:MM:SS").
#
# Needs root. A non-root caller gets zero lines with no error, which reads
# exactly like "nothing happened" -- so callers must decide whether they are
# entitled to the answer before they interpret an empty one.
jarvis_unlock_log_since() {
    local since="$1"
    log show --style compact --start "${since}" --predicate \
        "subsystem == \"${JARVIS_LOG_SUBSYSTEM_PLUGIN}\" OR process == \"${JARVIS_AUTH_HOST_PROCESS}\"" \
        2>/dev/null
}

# Does the live chain still contain every mechanism the backup had?
#
# The question verify.sh used to ask as `grep -q builtin:authenticate`, which was
# a literal describing one particular chain. On system.login.screensaver.unlock
# there is no builtin:authenticate -- there is CryptoTokenKit:login -- so the
# named check would fail a CORRECT install and pass a chain that had silently
# lost smartcard unlock. What matters is not which mechanisms are there but that
# we took nothing away, and the only thing that knows what was there is the
# backup.
jarvis_chain_preserves_backup() {
    local live="$1" backup="$2" want livelist backlist
    livelist="$(jarvis_rule_mechanisms "${live}")" || return 1
    backlist="$(jarvis_rule_mechanisms "${backup}")" || return 1
    [ -n "${backlist}" ] || return 0
    while IFS= read -r want; do
        [ -n "${want}" ] || continue
        _jarvis_lines_contain "${livelist}" "${want}" || return 1
    done <<EOF
${backlist}
EOF
    return 0
}

# =============================================================================
# REVERT -- the one way out, used by uninstall AND the sentinel
# =============================================================================
# uninstall.sh owned this. The sentinel needs the identical operation, and a
# second implementation of "how do we get our mechanism out of the lock screen"
# would be discovered by whoever the drifted copy failed, at a machine that will
# not unlock. So it moved here, unchanged in behaviour.
#
# Every path is a REPAIR, so it degrades rather than aborting: a failed restore
# falls through to stripping our mechanism out of whatever is live.

# Strip our mechanism from the live rule, leaving every other entry untouched.
# Never invents a rule. Echoes nothing; returns 0 if the rule no longer names us.
jarvis_strip_mechanism_in_place() {
    local current scratch removed

    if ! current="$(jarvis_authdb_read "${JARVIS_AUTH_RIGHT}")"; then
        _jarvis_warn "cannot read ${JARVIS_AUTH_RIGHT}; leaving it alone"
        return 1
    fi

    if ! printf '%s' "${current}" | grep -q "${JARVIS_PLUGIN_NAME}"; then
        _jarvis_log "${JARVIS_AUTH_RIGHT} does not reference ${JARVIS_PLUGIN_NAME}; nothing to strip"
        return 0
    fi

    scratch="$(mktemp -t jarvis-authdb)" || { _jarvis_warn "mktemp failed"; return 1; }
    printf '%s' "${current}" > "${scratch}"

    removed="$(jarvis_rule_strip_mechanism "${scratch}" "${JARVIS_PLUGIN_NAME}")"
    if [ "${removed}" -eq 0 ]; then
        # The rule mentions us somewhere plutil found nothing to delete: a
        # different key, or a shape this function does not understand. Writing it
        # back unchanged would be a no-op reported as a repair.
        _jarvis_warn "${JARVIS_AUTH_RIGHT} names ${JARVIS_PLUGIN_NAME} but no mechanism entry matched"
        _jarvis_warn "  inspect: security authorizationdb read ${JARVIS_AUTH_RIGHT}"
        rm -f "${scratch}"
        return 1
    fi

    if jarvis_authdb_write "${JARVIS_AUTH_RIGHT}" "${scratch}"; then
        _jarvis_log "stripped ${removed} ${JARVIS_PLUGIN_NAME} mechanism(s) from ${JARVIS_AUTH_RIGHT}"
        rm -f "${scratch}"
        return 0
    fi
    _jarvis_warn "could not write stripped rule for ${JARVIS_AUTH_RIGHT}"
    rm -f "${scratch}"
    return 1
}

# Get our mechanism out, by whichever route works. 0 if the rule no longer names
# us when this returns.
jarvis_revert_auth_rule() {
    if jarvis_restore_auth_rule_from_pointer; then
        _jarvis_log "authorization rule restored from backup"
        return 0
    fi
    _jarvis_warn "no usable backup (absent pointer, wrong right, missing file, or unparseable plist)"
    jarvis_strip_mechanism_in_place
}

# =============================================================================
# DEAD MAN'S SWITCH
# =============================================================================
# A detached timer holding nothing but a duration and a command. It observes
# NOTHING about whether its caller is alive, healthy, or finished, which is the
# entire point: a watchdog that shares state with the thing it guards is not a
# watchdog. It survives the caller crashing, the terminal closing, the SSH
# session dropping, and the parent being SIGKILLed -- none of those are
# conditions it can see.
#
# Extracted here when a second probe needed one. Two implementations of "put the
# machine back if I die" would differ in the details that matter at 3am.
#
# Echoes the detached pid.
jarvis_arm_deadman() {   # <window_s> <logfile> <recovery-command> <manual-hint>
    local window="$1" logfile="$2" cmd="$3" hint="$4" pid
    mkdir -p "$(dirname "${logfile}")" 2>/dev/null || true
    nohup bash -c "
        sleep ${window}
        for attempt in 1 2 3; do
            if ${cmd} 2>/dev/null; then
                printf '%s dead-man recovery OK (attempt %s)\n' \"\$(date -u +%FT%TZ)\" \"\$attempt\" >> '${logfile}'
                exit 0
            fi
            sleep 2
        done
        printf '%s DEAD-MAN RECOVERY FAILED -- by hand: %s\n' \"\$(date -u +%FT%TZ)\" '${hint}' >> '${logfile}'
    " >/dev/null 2>&1 &
    pid=$!
    disown "${pid}" 2>/dev/null || true
    printf '%s' "${pid}"
}

# =============================================================================
# SANCTIONED STATE -- what the installer proved, for the sentinel to check
# =============================================================================
# The installer measured a shape, gated it on evidence, and wrote it. The
# sentinel's job is to notice when the live rule stops being that. Without a
# record of what was sanctioned, "has it drifted" is not answerable, and a
# sentinel that cannot answer it would have to guess.
JARVIS_SANCTIONED_SHAPE_FILE="${JARVIS_STATE_DIR}/sanctioned-shape"

jarvis_record_sanctioned_shape() {
    printf '%s' "$1" > "${JARVIS_SANCTIONED_SHAPE_FILE}" || return 1
    chmod 600 "${JARVIS_SANCTIONED_SHAPE_FILE}" 2>/dev/null || true
}

jarvis_sanctioned_shape() {
    [ -r "${JARVIS_SANCTIONED_SHAPE_FILE}" ] || return 1
    cat "${JARVIS_SANCTIONED_SHAPE_FILE}" 2>/dev/null
}

# Is an authorization currently being evaluated, or the console locked?
#
# Two independent signals because they answer slightly different questions and
# either alone has a blind spot: SecurityAgent is the process that evaluates the
# chain, and IOConsoleLocked is the console's own state. Used to DEFER a
# non-urgent repair, never to defer an urgent one -- a lock screen is exactly
# when a stuck user needs the repair to happen, and a guard that waits for the
# guarded system to become idle is the deadlock Slice 47 retired.
jarvis_authentication_in_flight() {
    pgrep -qx SecurityAgent 2>/dev/null && return 0

    local tmp rc=1
    tmp="$(mktemp -t jarvis-console)" || return 1
    if ioreg -n Root -d1 -a > "${tmp}" 2>/dev/null; then
        case "$(jarvis_plist_value "${tmp}" IOConsoleLocked || true)" in
            true|1|YES) rc=0 ;;
        esac
    fi
    rm -f "${tmp}"
    return "${rc}"
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
