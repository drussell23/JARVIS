"""A retired loading server must actually leave: its shutdown cannot wedge,
and the supervisor must recognise a launcher's child as the server."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from backend.core.process_identity import descends_from
from backend.loading_server.eta_prediction import PredictiveETACalculator


def test_finishing_a_session_does_not_deadlock_on_its_own_lock(tmp_path):
    calc = PredictiveETACalculator(db_path=tmp_path / "eta.db",
                                   json_history_path=tmp_path / "eta.json")
    calc.start_session("s-1")
    done = threading.Event()
    worker = threading.Thread(target=lambda: (calc.finish_session(), done.set()), daemon=True)
    worker.start()
    assert done.wait(10.0), "finish_session deadlocked re-entering its own lock"


def test_a_process_is_its_own_lineage():
    assert descends_from(os.getpid(), os.getpid())


def _launch(tmp_path, *, launcher_waits):
    """A launcher that starts a long-lived child and records its pid -- the
    shape of a venv's python.exe on Windows."""
    pidfile = tmp_path / "child.pid"
    tail = "p.wait()" if launcher_waits else "pass"
    # The base interpreter: inside a Windows venv sys.executable is itself a
    # launcher, which would add a second (dead) link to the chain under test.
    python = getattr(sys, "_base_executable", None) or sys.executable
    launcher = subprocess.Popen([python, "-c", (
        "import subprocess, sys, pathlib;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']);"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));"
        f"{tail}"
    )])
    deadline = time.monotonic() + 15
    while not (pidfile.exists() and pidfile.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    return launcher, int(pidfile.read_text())


def _kill(*pids):
    import psutil
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except Exception:  # noqa: BLE001
            pass


def test_the_server_a_launcher_runs_is_the_launchers(tmp_path):
    launcher, child = _launch(tmp_path, launcher_waits=True)
    try:
        assert descends_from(child, launcher.pid)
        assert not descends_from(os.getpid(), child)
        assert not descends_from(child, os.getpid() + 1_000_000)
    finally:
        _kill(child, launcher.pid)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="POSIX re-parents an orphan; Windows keeps its parent's pid number")
def test_on_windows_it_stays_the_launchers_after_the_launcher_exits(tmp_path):
    launcher, child = _launch(tmp_path, launcher_waits=False)
    launcher.wait(30)
    try:
        assert descends_from(child, launcher.pid)
    finally:
        _kill(child)


@pytest.mark.parametrize("pid,ancestor", [(None, 1), (1, None), (0, 0)])
def test_no_pid_has_no_lineage(pid, ancestor):
    assert not descends_from(pid, ancestor)
