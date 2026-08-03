#!/usr/bin/env bash
# Git-Mutex hook bridge.
#
# Pure transport. Resolves the repo root and an interpreter, then hands the
# decision to backend.core.git_mutex_hook. No lock logic lives in bash — the
# hook must agree with the acquire path by construction, so there is exactly
# one implementation of "is this lock held".
#
# Sourced by the per-hook stubs, which set GIT_MUTEX_HOOK_NAME first.
set -uo pipefail

if [ -z "${GIT_MUTEX_HOOK_NAME:-}" ]; then
  echo "[git-mutex] bridge invoked without GIT_MUTEX_HOOK_NAME" >&2
  exit 0
fi

# Fast path: skip the interpreter spawn entirely when disabled.
case "${JARVIS_GIT_HOOK_ENABLED:-true}" in
  0|false|False|FALSE|no|off) exit 0 ;;
esac

# reference-transaction fires 3x per transaction; only 'prepared' is vetoable.
# Short-circuit the other two in shell so we do not pay Python startup for them.
if [ "$GIT_MUTEX_HOOK_NAME" = "reference-transaction" ] && [ "${1:-}" != "prepared" ]; then
  exit 0
fi

# Where the JARVIS package lives. Normally the repo being operated on, but a
# repo guarded from outside (a scratch clone, or the regression suite's temp
# repos) can point at the installation explicitly.
if [ -n "${JARVIS_GIT_MUTEX_HOOK_ROOT:-}" ]; then
  _root="${JARVIS_GIT_MUTEX_HOOK_ROOT}"
else
  # --git-common-dir so linked worktrees resolve to the shared repository.
  _common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || exit 0
  _root="$(cd "${_common}/.." 2>/dev/null && pwd)" || exit 0
fi
[ -d "${_root}/backend" ] || exit 0

# Prefer the project venv, else whatever python3 is on PATH.
if [ -x "${_root}/.venv/bin/python3" ]; then
  _py="${_root}/.venv/bin/python3"
else
  _py="$(command -v python3 2>/dev/null)" || exit 0
fi
[ -n "${_py}" ] || exit 0

# Explicit sys.path insert rather than `-m` + PYTHONPATH: for `-m`, Python puts
# the CURRENT DIRECTORY at sys.path[0], which outranks PYTHONPATH. The hook runs
# with cwd set to the repo being operated on, so whenever that differs from the
# resolved root (a guarded scratch clone, or the regression suite's temp repos)
# `-m` silently resolves `backend` in the wrong tree and reports "No module
# named ...". Inserting the root ourselves makes resolution deterministic.
"${_py}" -c 'import sys
sys.path.insert(0, sys.argv[1])
from backend.core.git_mutex_hook import main
sys.exit(main(sys.argv[2:]))' "${_root}" "$GIT_MUTEX_HOOK_NAME" "$@"
_rc=$?

# 17 is the ONLY status that means "the mutex refused". Anything else non-zero
# means python itself could not deliver a verdict — missing module, broken venv,
# syntax error — and must NOT block, or a broken interpreter would brick every
# git operation in the repository. Fail open, loudly.
case "$_rc" in
  0)  exit 0 ;;
  17) exit 1 ;;
  *)
    echo "[git-mutex] hook could not evaluate (exit ${_rc}) — allowing operation" >&2
    case "${JARVIS_GIT_HOOK_STRICT:-false}" in
      1|true|True|TRUE|yes|on)
        echo "[git-mutex] JARVIS_GIT_HOOK_STRICT set — refusing instead" >&2
        exit 1 ;;
    esac
    exit 0 ;;
esac
