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
jarvis_probe_evidence_for_shape() {
    local shape="$1"
    [ -n "${shape}" ] || return 1
    [ -r "${JARVIS_PROBE_RESULTS_LOG}" ] || return 1
    awk -v want="shape=${shape}" '
        {
            s = 0; p = 0; l = 0
            for (i = 1; i <= NF; i++) {
                if ($i == want)           s = 1
                if ($i == "password=yes") p = 1
                if ($i == "locked=yes")   l = 1
            }
            if (s && p && l) last = $0
        }
        END { if (last != "") { print last; exit 0 } exit 1 }
    ' "${JARVIS_PROBE_RESULTS_LOG}"
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
