"""
Native git-hook entrypoint for the Autonomic Git-Mutex.

Turns the advisory lock in :mod:`backend.core.git_transaction_lock` into a
constraint git itself enforces. The shell hooks under ``scripts/hooks/`` are
pure bridges: they exec this module and propagate its exit status. All lock
verification lives here, reusing :func:`git_transaction_lock.probe_lock` —
nothing about lock semantics is restated in bash.

Exit contract
-------------
``0``  allow the operation
``1``  block it (git aborts the ref transaction / rebase / push)

Invocation
----------
    python3 -m backend.core.git_mutex_hook <hook-name> [hook args...]

``reference-transaction`` additionally passes a state word (``prepared`` /
``committed`` / ``aborted``) as its first argument. Only ``prepared`` is
actionable: it is the sole point at which a non-zero exit still aborts the
transaction. Refusing at ``committed`` would be theatre — the refs already moved.

Why this can fail open
----------------------
This hook runs on *every* ref transaction in the repository, including ones
issued by the operator's editor and by unrelated tooling. A crash here would
brick all git usage in the repo. So an *internal* failure (interpreter missing,
import error, unreadable lock) allows the operation and logs, unless
``JARVIS_GIT_HOOK_STRICT`` is set. A *definite* "held by another live process"
always blocks. That asymmetry is deliberate: the threat model is concurrent
autonomous agents colliding, not an adversary who edits their own venv.

Environment
-----------
``JARVIS_GIT_HOOK_ENABLED``  master switch (default ``true``)
``JARVIS_GIT_HOOK_STRICT``   block on internal error too (default ``false``)
``JARVIS_GIT_TXN_TOKEN``     set automatically inside a held transaction; makes
                             the holder's own git children pass
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence

ALLOW = 0

#: Distinct sentinel for a deliberate refusal — NOT 1.
#:
#: The bridge must be able to tell "the mutex refused" from "python could not
#: start". Both would exit 1 by default: a missing module, a broken venv, or a
#: syntax error all yield 1, and the bridge would propagate that as a block,
#: bricking every git operation in the repository. (Observed exactly this in
#: development, before installation: the module was absent from the resolved
#: root and every ref transaction would have been refused.) Only this sentinel
#: means "blocked"; the bridge treats every other non-zero status as an
#: infrastructure failure and allows.
BLOCK = 17

#: reference-transaction states. Only the first can still be vetoed.
_ACTIONABLE_REF_STATES = frozenset({"prepared"})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _emit(message: str) -> None:
    """Hook diagnostics go to stderr so they never pollute git's stdout."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _blocked_banner(hook: str, owner: Optional[str]) -> str:
    return (
        "\n"
        "# GIT-MUTEX -- operation blocked\n"
        f"  hook   : {hook}\n"
        f"  reason : the git transaction lock is held by another live agent\n"
        f"  owner  : {owner or 'unknown'}\n"
        "\n"
        "  Another process is mid-transaction in this repository. Retry when it\n"
        "  finishes. Autonomous callers should wrap their work in\n"
        "  backend.core.git_transaction_lock.git_transaction() so they queue\n"
        "  instead of colliding.\n"
        "\n"
        "  Override (last resort): JARVIS_GIT_HOOK_ENABLED=false git ...\n"
    )


def decide(hook: str, args: Sequence[str]) -> int:
    """Return ALLOW or BLOCK for ``hook`` invoked with ``args``."""
    if not _env_bool("JARVIS_GIT_HOOK_ENABLED", True):
        return ALLOW

    # reference-transaction fires three times per transaction; only the
    # 'prepared' phase can still be vetoed.
    if hook == "reference-transaction":
        state = args[0].strip() if args else ""
        if state not in _ACTIONABLE_REF_STATES:
            return ALLOW

    strict = _env_bool("JARVIS_GIT_HOOK_STRICT", False)

    try:
        from backend.core.git_transaction_lock import probe_lock

        probe = probe_lock()
    except Exception as exc:  # noqa: BLE001 — must not brick the repository
        _emit(f"[git-mutex] hook degraded ({type(exc).__name__}: {exc})")
        if strict:
            _emit("[git-mutex] JARVIS_GIT_HOOK_STRICT set — refusing")
            return BLOCK
        return ALLOW

    if probe.held_by_other:
        _emit(_blocked_banner(hook, probe.owner))
        return BLOCK
    return ALLOW


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        _emit("[git-mutex] hook invoked without a hook name — allowing")
        return ALLOW
    return decide(args[0], args[1:])


if __name__ == "__main__":  # pragma: no cover — process entrypoint
    raise SystemExit(main())
