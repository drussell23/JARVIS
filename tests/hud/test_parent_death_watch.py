"""The child must not outlive its launcher.

Xcode's Stop button SIGKILLs the HUD. SIGKILL is uncatchable, so nothing in
the parent runs on the way out and the Python backend is reparented to
launchd, holding ports, a microphone claim and a few hundred megabytes.
Measured 2026-08-04: two orphans, one at 46.8% CPU with no listening socket.

These tests spawn REAL processes and REALLY kill them. A mocked parent cannot
demonstrate the property under test, because the property is about what
happens when a process is destroyed without warning — and a fake parent is
destroyed politely, which is the case that already worked.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from brainstem.parent_watch import (
    ENV_ENABLED, ENV_PARENT_PID, ParentWatch, declared_parent_pid, install,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Declaration: supervision requires somebody to have asked for it out loud.
# ---------------------------------------------------------------------------


def test_no_declaration_means_no_supervision(monkeypatch):
    """A standalone `python3 -m brainstem` must never kill itself."""
    monkeypatch.delenv(ENV_PARENT_PID, raising=False)
    assert declared_parent_pid() is None
    assert install() is None


@pytest.mark.parametrize("value", ["", "   ", "not-a-pid", "0", "-5", "1"])
def test_implausible_declarations_are_refused(monkeypatch, value):
    """PID 1 is refused deliberately: launchd is everybody's eventual parent,
    so treating it as the parent would mean waiting for a death that has
    already happened."""
    monkeypatch.setenv(ENV_PARENT_PID, value)
    assert declared_parent_pid() is None


def test_our_own_pid_is_refused(monkeypatch):
    """Watching ourselves would fire the moment we exit."""
    monkeypatch.setenv(ENV_PARENT_PID, str(os.getpid()))
    assert declared_parent_pid() is None


def test_master_switch_disables(monkeypatch):
    monkeypatch.setenv(ENV_PARENT_PID, "424242")
    monkeypatch.setenv(ENV_ENABLED, "false")
    assert install() is None


# ---------------------------------------------------------------------------
# Detection, against processes that are really killed.
# ---------------------------------------------------------------------------


def _sleeper() -> subprocess.Popen:
    """A real process to play the launcher."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def test_a_dead_parent_is_detected_before_arming():
    """The race between spawn and arming is not an error, it is the answer.

    If the launcher dies in the window before the watch starts, no future
    event will ever be delivered — the exit already happened. Checking
    liveness at arm time is what stops that from becoming a permanent orphan.
    """
    p = _sleeper()
    pid = p.pid
    p.kill()
    p.wait()

    w = ParentWatch(pid)
    w.grace_s = 60.0          # keep escalation away from this assertion
    fired = []
    w._fire = lambda reason: fired.append(reason)  # type: ignore[method-assign]
    w.start()
    assert fired, "a parent that was already gone must be detected at arm time"


def test_sigkill_of_the_parent_is_detected():
    """The case that produced the bug: an uncatchable kill.

    The kernel reports NOTE_EXIT identically for SIGKILL and a clean exit —
    it does not care how the process died — which is exactly why detection
    lives in the child and not in the parent's shutdown path.
    """
    p = _sleeper()
    w = ParentWatch(p.pid)
    w.grace_s = 60.0
    fired = []
    w._fire = lambda reason: fired.append(reason)  # type: ignore[method-assign]
    w.start()

    time.sleep(0.3)
    assert not fired, "must not fire while the parent is alive"

    os.kill(p.pid, signal.SIGKILL)
    p.wait()

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not fired:
        time.sleep(0.1)
    w.stop()
    assert fired, "SIGKILL of the parent went undetected — this is the orphan bug"


def test_firing_is_idempotent():
    """Two mechanisms watch one parent; only one shutdown may start.

    Without this, both would raise SIGTERM and start an escalation timer,
    and the second timer would `os._exit` a process that was already
    shutting down cleanly.
    """
    w = ParentWatch(os.getppid() or 424242)
    sent = []
    w._escalate = lambda: sent.append("escalate")  # type: ignore[method-assign]
    killed = []
    real_kill = os.kill

    def _fake_kill(pid, sig):
        if pid == os.getpid():
            killed.append(sig)
            return
        return real_kill(pid, sig)

    os.kill = _fake_kill  # type: ignore[assignment]
    try:
        w._fire("first")
        w._fire("second")
        w._fire("third")
    finally:
        os.kill = real_kill  # type: ignore[assignment]

    assert killed == [signal.SIGTERM], f"expected exactly one SIGTERM, got {killed}"


def test_permission_denied_is_alive_not_dead():
    """A parent owned by another user exists. Reading EPERM as "gone" would
    kill the backend whenever the launcher runs as somebody else."""
    from brainstem import parent_watch
    assert parent_watch._parent_is_alive(1) is True


# ---------------------------------------------------------------------------
# End to end: a real child, a real SIGKILL, and no orphan left behind.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)
def test_a_real_child_does_not_survive_a_sigkilled_parent(tmp_path):
    """The whole point, with nothing faked.

    A parent spawns a child that installs the watch. The parent is SIGKILLed
    — no handler, no atexit, no chance to clean up, exactly what Xcode's Stop
    button does. The child must notice and leave on its own.
    """
    child_src = tmp_path / "child.py"
    child_src.write_text(textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {REPO_ROOT!r})
        from brainstem.parent_watch import install
        install()
        # Live long enough that surviving would be unambiguous.
        for _ in range(600):
            time.sleep(0.2)
    """))

    parent_src = tmp_path / "parent.py"
    parent_src.write_text(textwrap.dedent(f"""
        import os, subprocess, sys, time
        env = dict(os.environ)
        env["JARVIS_PARENT_PID"] = str(os.getpid())
        env["JARVIS_PARENT_WATCH_POLL_S"] = "0.5"
        env["JARVIS_PARENT_WATCH_GRACE_S"] = "2"
        p = subprocess.Popen([sys.executable, {str(child_src)!r}], env=env)
        print(p.pid, flush=True)
        time.sleep(300)
    """))

    parent = subprocess.Popen([sys.executable, str(parent_src)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        time.sleep(2.0)          # let the child arm its watch

        assert _alive(child_pid), "child should be running before the kill"

        os.kill(parent.pid, signal.SIGKILL)   # uncatchable, like Xcode's Stop
        parent.wait()

        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline and _alive(child_pid):
            time.sleep(0.25)

        survived = _alive(child_pid)
        if survived:                          # never leave one behind
            try:
                os.kill(child_pid, signal.SIGKILL)
            except Exception:
                pass
        assert not survived, (
            "the child outlived a SIGKILLed parent — it is an orphan holding "
            "ports and memory, which is the bug this exists to prevent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# The watchdog must not depend on anything it is watching.
# ---------------------------------------------------------------------------


def test_the_escalation_path_never_touches_logging():
    """A wedged shutdown is exactly the state where some thread holds the
    logging lock. If escalation called `logger.*` it would block on the system
    it exists to bound — a watchdog sharing a lock with its subject is not a
    watchdog. Same rule that forbids the battle harness's wall-clock watchdog
    from reading the op-ledger.
    """
    import ast
    import inspect
    from brainstem import parent_watch

    for name in ("_escalate", "_fire", "_record"):
        fn = getattr(parent_watch.ParentWatch, name, None) or getattr(parent_watch, name)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id != "logger", (
                    f"{name}() calls logger.{node.attr} — the escalation path "
                    f"must use only syscalls, or a wedged logging lock "
                    f"deadlocks the watchdog")


def test_the_record_sink_survives_an_unwritable_destination(monkeypatch):
    """A log line is never worth obstructing the exit it describes."""
    from brainstem import parent_watch
    monkeypatch.setenv(parent_watch.ENV_LOG_PATH, "/nonexistent-root/nope/x.log")
    parent_watch._record("this must not raise")   # would propagate if it did


def test_record_actually_writes_where_it_says(monkeypatch, tmp_path):
    """The destination must be real — the previous incarnation reported into a
    pipe held by the process that had just been SIGKILLed, and recorded
    nothing at all on a run that worked perfectly.
    """
    from brainstem import parent_watch
    target = tmp_path / "nested" / "parent-watch.log"
    monkeypatch.setenv(parent_watch.ENV_LOG_PATH, str(target))
    parent_watch._record("hello from the watch")
    assert target.exists(), "the sink must create its own directory"
    assert "hello from the watch" in target.read_text()
