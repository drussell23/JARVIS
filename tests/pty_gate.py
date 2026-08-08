"""The single gate for tests that need a real pseudo-terminal.

WHY THIS EXISTS
---------------
Thirty-one tests across three files — the entire proof that the cockpit
responds to a keystroke — sat behind two independently written
``_require_pty`` helpers that both called ``pytest.skip("out of pty devices")``.

In every sandboxed or containerised runner ``pty.openpty()`` raises, so all
thirty-one vanished and the run reported **green**. That is how the cockpit's
interactive behaviour came to be described as "never run under a real TTY"
while a 650-line suite proving it sat in the repository, passing in 80 seconds
the moment anyone gave it a terminal.

A skip is a legitimate answer to "this environment has no pty". Reporting it as
success is not. This module makes the distinction impossible to lose:

  * ONE gate, so the two copies cannot drift;
  * every skip is RECORDED, and the terminal summary says loudly how much of
    the suite did not run and what that means;
  * ``JARVIS_PTY_TESTS_REQUIRED`` turns the skip into a FAILURE, so a runner
    that is supposed to have a terminal cannot quietly stop having one.

The env var is the adaptive part. A developer laptop legitimately varies; a CI
job that claims to exercise the cockpit does not, and it should fail loudly the
day its base image drops ``/dev/ptmx``.
"""

from __future__ import annotations

import os
from typing import List, Tuple

import pytest

#: Set to a truthy value where a pty is a REQUIREMENT rather than a nicety.
#: Unset (the default) preserves the skip, so a laptop without one still runs
#: the rest of the suite.
REQUIRED_ENV_VAR = "JARVIS_PTY_TESTS_REQUIRED"

#: Every skip taken this session, for the terminal summary. Module scope
#: because the summary hook runs in a different fixture context entirely and a
#: pytest stash would tie this to one plugin's lifetime.
SKIPPED: List[Tuple[str, str]] = []


def pty_required() -> bool:
    """Is a pty mandatory in this environment?"""
    raw = str(os.environ.get(REQUIRED_ENV_VAR, "")).strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def open_pty(nodeid: str = "") -> Tuple[int, int]:
    """Allocate a master/slave pair, or skip — recording that we did.

    Returns the pair so callers that need the fds get them from the same call
    that decides whether they may proceed. A caller that only needs the
    decision closes them immediately; that is cheaper than a second syscall
    path with its own error handling, and it keeps ONE definition of "can this
    machine give us a terminal".
    """
    import pty

    try:
        return pty.openpty()
    except OSError as exc:
        reason = f"pty allocation unavailable in this environment: {exc}"
        SKIPPED.append((nodeid or "<unknown>", str(exc)))
        if pty_required():
            pytest.fail(
                f"{reason}\n"
                f"{REQUIRED_ENV_VAR} is set, so this runner is expected to "
                f"provide a pseudo-terminal. The cockpit's interactive "
                f"behaviour is UNPROVEN without one."
            )
        pytest.skip(reason)
        raise AssertionError("unreachable")  # pragma: no cover


def require_pty(nodeid: str = "") -> None:
    """Skip unless this machine can give us a terminal. Allocates nothing."""
    master, slave = open_pty(nodeid)
    os.close(master)
    os.close(slave)
