#!/usr/bin/env bash
# Guard: the committed Xcode project must equal what XcodeGen produces from
# `project.yml`. Nothing in `.xcodeproj` may exist that the spec cannot
# recreate, and nothing in the spec may be missing from `.xcodeproj`.
#
# WHY THIS EXISTS
# ---------------
# Two failures, one root cause — the generated artefact and its spec drifted
# apart, and the artefact is what Xcode actually reads:
#
#   1. DARK CODE. `UtteranceRecorder.swift` was written, committed, and
#      referenced by `WakeWordListener` — and never compiled, because a new
#      file is invisible to Xcode until `xcodegen` puts it in the build
#      sources. Code that exists in git and does not exist in the binary is
#      worse than missing code: it reads as done.
#
#   2. A SETTING THAT LIVED ONLY IN THE OUTPUT. `OS_ACTIVITY_MODE=disable`
#      had been hand-added to the generated `.xcscheme`. The next `xcodegen`
#      run — the one that fixed failure 1 — deleted it, because regeneration
#      restores the spec's truth and the spec had never been told. A setting
#      that lives only in a generated artefact is a countdown.
#
# Both are the same class, so one check covers both: regenerate from the spec
# and demand the result match byte for byte. A symptom check ("is every
# .swift in the pbxproj?") would have caught 1 and not 2.
#
# HOW
# ---
# The spec's directory is mirrored — from the git INDEX, not the working tree,
# so this sees exactly what is being committed — into a temp dir, minus the
# `.xcodeproj` itself. XcodeGen runs there, in place, so every emitted path is
# relative in the same way the committed one is; generating into a different
# directory makes XcodeGen write absolute `../../..` paths and every line
# reports as drift. The mirror is small (~500KB: sources and the spec, no
# build products) and the run takes about a second.
#
# Usage:
#   ./scripts/check_xcodegen_parity.sh
#
# Exit codes:
#   0 - every generated project matches its spec
#   1 - drift found; the offending diff is printed to stderr
#   2 - xcodegen is not installed and no exemption was declared
#
# XcodeGen missing is a FAILURE, not a skip. A guard that quietly passes when
# its tool is absent is indistinguishable from a guard that passes, which is
# how this class of defect survives CI. To run without it, say so out loud:
#
#   JARVIS_XCODEGEN_PARITY_SKIP_IF_MISSING=1 ./scripts/check_xcodegen_parity.sh
#
# Silence means enforce; only a positive declaration exempts.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

if ! command -v xcodegen > /dev/null 2>&1; then
    if [ "${JARVIS_XCODEGEN_PARITY_SKIP_IF_MISSING:-0}" = "1" ]; then
        echo "WARNING: xcodegen not installed; parity NOT checked (exemption declared)." >&2
        exit 0
    fi
    echo "ERROR: xcodegen is not installed, so Xcode project parity cannot be checked." >&2
    echo "       Install it:  brew install xcodegen" >&2
    echo "       Or declare the exemption explicitly:" >&2
    echo "         JARVIS_XCODEGEN_PARITY_SKIP_IF_MISSING=1 $0" >&2
    exit 2
fi

# An XcodeGen spec is a tracked `project.yml` that declares targets. Other
# `project.yml` files (there are unrelated ones in this repo) are ignored.
SPECS=()
while IFS= read -r spec; do
    grep -qE '^targets:' "${spec}" && SPECS+=("${spec}")
done < <(git ls-files '*project.yml')

if [ ${#SPECS[@]} -eq 0 ]; then
    echo "OK: no XcodeGen specs found."
    exit 0
fi

MIRROR="$(mktemp -d)"
trap 'rm -rf "${MIRROR}"' EXIT

FAILED=0

for spec in "${SPECS[@]}"; do
    spec_dir="$(dirname "${spec}")"
    proj_name="$(basename "${spec_dir}")"

    # Mirror the tracked/staged content of the spec directory, excluding the
    # generated project — that is the thing under test, and copying it in
    # would let XcodeGen preserve parts of it instead of deriving them.
    git ls-files -z -- "${spec_dir}" \
        | grep -zv '\.xcodeproj/' \
        | tar -cf - --null -T - 2>/dev/null \
        | (cd "${MIRROR}" && tar -xf -)

    if ! (cd "${MIRROR}/${spec_dir}" && xcodegen generate --spec project.yml --quiet); then
        echo "ERROR: xcodegen failed on ${spec}" >&2
        FAILED=1
        continue
    fi

    # Find the generated project by name rather than assuming it matches the
    # directory - `name:` in the spec decides, and the two need not agree.
    generated="$(find "${MIRROR}/${spec_dir}" -maxdepth 1 -name '*.xcodeproj' | head -1)"
    if [ -z "${generated}" ]; then
        echo "ERROR: xcodegen produced no .xcodeproj for ${spec}" >&2
        FAILED=1
        continue
    fi
    committed="${spec_dir}/$(basename "${generated}")"

    if [ ! -d "${committed}" ]; then
        echo "ERROR: ${committed} is not committed, but ${spec} generates it." >&2
        FAILED=1
        continue
    fi

    # Compare only what XcodeGen owns. `xcuserdata` is per-developer window
    # state and `project.xcworkspace` carries local settings Xcode writes on
    # its own; neither is generated, and demanding parity on them would fail
    # for every developer who has ever opened the project.
    DRIFT=""
    for artefact in project.pbxproj xcshareddata/xcschemes; do
        if [ -e "${generated}/${artefact}" ] || [ -e "${committed}/${artefact}" ]; then
            if ! d=$(diff -r "${generated}/${artefact}" "${committed}/${artefact}" 2>&1); then
                DRIFT="${DRIFT}${d}"$'\n'
            fi
        fi
    done

    if [ -n "${DRIFT}" ]; then
        FAILED=1
        echo "DRIFT: ${committed} does not match what ${spec} generates." >&2
        echo "" >&2
        printf '%s\n' "${DRIFT}" >&2

        # Name the dark-code case explicitly. A developer reading a raw
        # pbxproj diff will not necessarily recognise that the missing lines
        # are the reason their new file never ran.
        # `sourcecode.swift` is the value of `lastKnownFileType`, not a
        # filename; it appears on every Swift line and would otherwise be
        # reported as a dark file on every run.
        DARK=$(printf '%s\n' "${DRIFT}" \
            | grep -oE '[A-Za-z0-9_+-]+\.swift' \
            | grep -vx 'sourcecode.swift' \
            | sort -u || true)
        if [ -n "${DARK}" ]; then
            echo "" >&2
            echo "  Swift files involved in the drift:" >&2
            printf '%s\n' "${DARK}" | sed 's/^/    /' >&2
            echo "  If these are new, they are NOT being compiled - the code is dark." >&2
        fi

        echo "" >&2
        echo "  Fix by regenerating, then commit the result:" >&2
        echo "    (cd ${spec_dir} && xcodegen generate)" >&2
        echo "" >&2
        echo "  If the change you want is in the .xcodeproj, put it in ${spec}" >&2
        echo "  instead - the next regeneration will delete anything the spec" >&2
        echo "  does not know about." >&2
    else
        echo "OK: ${committed} matches ${spec}."
    fi
done

exit "${FAILED}"
