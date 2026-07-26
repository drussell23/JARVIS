"""A test run must not act on the operator's desktop.

The fault
---------
The autonomous loop runs pytest continuously. Sixteen ``test_*`` functions in
this tree launch GUI applications through ``open -a``, so every sweep that
reached them opened apps on the operator's screen — the Weather app, four
times reported as "it keeps opening on its own". Confirmed live: pytest
spawned at 20:47:18, Weather.app launched at 20:47:30, pytest exited at
20:47:39.

Ignoring the archived scripts removes fourteen of them. This closes the class,
including the two live tests and any written later, because the real invariant
is not "those particular files are bad" — it is:

    A test run is an OBSERVATION of the system, not an ACTUATION of the
    operator's machine.

Why a guard and not a rule
--------------------------
"Don't launch apps in tests" is a convention, and a convention cannot survive
a self-modifying system that writes its own tests. The organism generates test
files; it will eventually generate one that drives the GUI, and nobody will be
watching that sweep. The guard makes the failure impossible rather than
discouraged.

Scope, deliberately narrow
--------------------------
Only the ``open -a <app>`` shape is intercepted — the one that raises a window
on a human's screen. Nothing else about subprocess is touched: tests that shell
out to git, pytest, ``say`` or anything else behave exactly as before. A test
that genuinely needs to launch an app opts in with
``JARVIS_TEST_ALLOW_APP_LAUNCH=1``, which is explicit, greppable, and absent by
default.

The interception returns a successful CompletedProcess rather than raising:
these tests assert on the launch succeeding, and turning a desktop side effect
into a test failure would trade one wrong behaviour for another.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, List, Sequence

import pytest

_ALLOW = ("1", "true", "yes", "on")


def _launch_allowed() -> bool:
    return os.getenv(
        "JARVIS_TEST_ALLOW_APP_LAUNCH", "",
    ).strip().lower() in _ALLOW


def _is_gui_launch(args: Any) -> bool:
    """Is this the ``open -a <app>`` shape? Nothing else qualifies."""
    if isinstance(args, (str, bytes)):
        text = args.decode() if isinstance(args, bytes) else args
        return "open" in text.split()[:1] and " -a" in text
    if not isinstance(args, Sequence):
        return False
    parts: List[str] = [str(a) for a in args]
    if not parts:
        return False
    return os.path.basename(parts[0]) == "open" and "-a" in parts[1:]


@pytest.fixture(autouse=True, scope="session")
def _no_desktop_actuation() -> Any:
    """Make GUI app launches inert for the whole session.

    Session-scoped and autouse: a per-test fixture would leave collection-time
    and fixture-time launches uncovered, and an opt-in fixture would have to be
    remembered by code that does not yet exist."""
    if _launch_allowed():
        yield
        return

    real_run = subprocess.run
    real_popen = subprocess.Popen
    blocked: List[List[str]] = []

    def guarded_run(args: Any, *a: Any, **kw: Any) -> Any:
        if _is_gui_launch(args):
            blocked.append([str(x) for x in args])
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return real_run(args, *a, **kw)

    class GuardedPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:
            if _is_gui_launch(args):
                blocked.append([str(x) for x in args])
                # Substitute a trivial command so the object still behaves like
                # a process: callers wait() on it and read returncode.
                args = ["true"]
                kw.pop("stdout", None)
                kw.pop("stderr", None)
            super().__init__(args, *a, **kw)

    subprocess.run = guarded_run                      # type: ignore[assignment]
    subprocess.Popen = GuardedPopen                   # type: ignore[misc]
    try:
        yield
    finally:
        subprocess.run = real_run                     # type: ignore[assignment]
        subprocess.Popen = real_popen                 # type: ignore[misc]
        if blocked:
            # Reported, not silent: a suppressed side effect the author did not
            # expect is worth knowing about, and this is the line that would
            # have told us months ago.
            names = sorted({" ".join(b[:3]) for b in blocked})
            print(
                f"\n[conftest] suppressed {len(blocked)} desktop app launch(es) "
                f"during this run: {names}. Set JARVIS_TEST_ALLOW_APP_LAUNCH=1 "
                f"to permit them.",
            )
