#!/usr/bin/env bash
# Harness Epic Slice 3 — CI guard against the banned `tail -f /dev/null | python`
# stdin-guard idiom in `docs/` and `scripts/`.
#
# Why banned: the stdin-guard pattern keeps the Python child's stdout
# pipe open after the parent shell dies, so the child becomes an orphan
# instead of receiving SIGPIPE. The 7 orphan PIDs in the S4 mass-cleanup
# (2026-04-23) all had this pattern. Slice 3 enforces the ban so the
# pattern can't sneak back in via runbook drift or copy-paste.
#
# Replacement: `python3 scripts/ouroboros_battle_test.py --headless ...`
# (or env `OUROBOROS_BATTLE_HEADLESS=true`). See
# `docs/operations/battle_test_runbook.md`.
#
# Usage:
#   ./scripts/check_no_stdin_guard.sh
#
# Exit codes:
#   0 — no banned patterns found in `docs/` or `scripts/`
#   1 — banned pattern found; offending lines printed to stderr
#
# Wire into CI as a fast pre-merge check (subsecond runtime).

set -euo pipefail

# Use git grep so the check honors .gitignore and only inspects tracked
# content. Falls back to grep -r when git is unavailable (defensive — the
# repo always has git, but tests construct synthetic scenarios).
PATTERN='tail -f /dev/null \| python'
SCOPE=("docs/" "scripts/")

# Self-references that legitimately quote the pattern (the guard script
# itself contains the regex; the runbook documents the ban). These are
# excluded from the violation set — they're meta-references, not the
# pattern actually being USED.
EXEMPT=(
    "scripts/check_no_stdin_guard.sh"
    "docs/operations/battle_test_runbook.md"
)

# Build a `git grep` exclude list from the EXEMPT array.
EXCLUDE_ARGS=()
for f in "${EXEMPT[@]}"; do
    EXCLUDE_ARGS+=(":(exclude)${f}")
done

if command -v git > /dev/null 2>&1 && git rev-parse --git-dir > /dev/null 2>&1; then
    if git grep -E "${PATTERN}" -- "${SCOPE[@]}" "${EXCLUDE_ARGS[@]}" 2>/dev/null; then
        echo "" >&2
        echo "ERROR: banned 'tail -f /dev/null | python' stdin-guard pattern" \
             "found in docs/ or scripts/." >&2
        echo "       Use --headless instead. See docs/operations/battle_test_runbook.md." >&2
        exit 1
    fi
else
    # grep -r fallback: build --exclude= args from EXEMPT basenames
    EXCLUDE_GREP=()
    for f in "${EXEMPT[@]}"; do
        EXCLUDE_GREP+=("--exclude=$(basename "${f}")")
    done
    if grep -rE "${PATTERN}" "${EXCLUDE_GREP[@]}" "${SCOPE[@]}" 2>/dev/null; then
        echo "" >&2
        echo "ERROR: banned 'tail -f /dev/null | python' stdin-guard pattern" \
             "found in docs/ or scripts/." >&2
        exit 1
    fi
fi

echo "OK: no banned stdin-guard patterns in docs/ or scripts/."
exit 0
