"""A restart is followed, not lost (2026-10-16 00:17-00:18).

Two --restart runs 30 s apart and a plain launch: the second restart threw the
first one's boot away, the plain launch attached to the kernel being handed
over and dropped to the prompt, and the "background" kernel shared the
launching console (start_new_session is POSIX-only), so that window's Ctrl+C
and closing reached it."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runner():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


# --- the record -------------------------------------------------------------------

def test_a_live_runner_s_record_is_pending(tmp_path, runner):
    from backend.core import daemon_handover as dh

    path = tmp_path / "restart.json"
    dh.record_restart(runner.pid, "C:/logs/supervisor-x.log", path=path)
    record = dh.pending_restart(path)
    assert record["pid"] == runner.pid and record["log"] == "C:/logs/supervisor-x.log"


def test_a_dead_runner_s_record_is_not(tmp_path, runner):
    from backend.core import daemon_handover as dh

    path = tmp_path / "restart.json"
    dh.record_restart(runner.pid, None, path=path)
    runner.kill()
    runner.wait(timeout=10)
    assert dh.pending_restart(path) is None


def test_only_its_own_runner_withdraws_a_record(tmp_path, runner):
    from backend.core import daemon_handover as dh

    path = tmp_path / "restart.json"
    dh.record_restart(runner.pid, None, path=path)
    dh.clear_restart(runner.pid + 1, path=path)     # an earlier, superseded runner
    assert path.exists()
    dh.clear_restart(runner.pid, path=path)
    assert not path.exists()


# --- the runner settles ------------------------------------------------------------

def test_the_record_goes_once_the_kernel_holds_the_lock(tmp_path, monkeypatch, runner):
    import os
    from backend.core import daemon_handover as dh

    path = tmp_path / "restart.json"
    dh.record_restart(os.getpid(), None, path=path)   # this process plays the runner
    holders = iter([os.getppid(), os.getppid(), runner.pid])   # the old kernel, then the new
    monkeypatch.setattr(dh, "kernel_holder", lambda *a, **k: next(holders))
    dh.settle_restart(runner, poll_s=0.05, path=path)
    assert runner.poll() is None and not path.exists()


def test_with_no_kernel_before_it_the_first_holder_settles_it(tmp_path, monkeypatch, runner):
    import os
    from backend.core import daemon_handover as dh

    path = tmp_path / "restart.json"
    dh.record_restart(os.getpid(), None, path=path)
    holders = iter([None, None, runner.pid])
    monkeypatch.setattr(dh, "kernel_holder", lambda *a, **k: next(holders))
    dh.settle_restart(runner, poll_s=0.05, path=path)
    assert runner.poll() is None and not path.exists()


def test_the_kernel_being_replaced_does_not_settle_it(tmp_path, monkeypatch, runner):
    import os
    from backend.core import daemon_handover as dh

    path = tmp_path / "restart.json"
    dh.record_restart(os.getpid(), None, path=path)
    calls = []

    def holder(*a, **k):
        calls.append(1)
        if len(calls) == 3:
            runner.kill()         # the kernel exits without ever taking the lock
        return os.getppid()       # the old kernel, not the runner's child
    monkeypatch.setattr(dh, "kernel_holder", holder)
    dh.settle_restart(runner, poll_s=0.05, path=path)
    assert len(calls) >= 3 and not path.exists()


# --- waiting for the successor -------------------------------------------------------

def test_a_viewer_waits_while_a_successor_is_expected(monkeypatch):
    from backend.core import daemon_handover as dh

    answers = iter([False, False, True])
    monkeypatch.setattr(dh, "daemon_live", lambda sock: next(answers))
    monkeypatch.setattr(dh, "successor_expected", lambda: True)
    monkeypatch.setattr(dh, "settle_poll_s", lambda: 0.01)
    said = []
    assert asyncio.run(dh.await_daemon(Path("unused"), say=said.append)) is True
    assert len(said) == 1 and "Waiting for the restarted kernel" in said[0]


def test_a_viewer_stops_when_nothing_is_coming(monkeypatch):
    from backend.core import daemon_handover as dh

    monkeypatch.setattr(dh, "daemon_live", lambda sock: False)
    monkeypatch.setattr(dh, "successor_expected", lambda: False)
    assert asyncio.run(dh.await_daemon(Path("unused"), say=lambda s: None)) is False


def test_the_viewer_follows_the_new_kernel(monkeypatch):
    from backend.core import daemon_handover as dh
    from backend.core.supervisor_ipc import SupervisorIPC

    outcomes = iter(["shutdown", "stopped"])         # old kernel handed over, then Ctrl+C
    expected = iter([True])
    monkeypatch.setattr(dh, "successor_expected", lambda: next(expected))
    monkeypatch.setattr(dh, "settle_poll_s", lambda: 0.01)

    async def live(sock, say=print):
        return True
    monkeypatch.setattr(dh, "await_daemon", live)
    ipc = SupervisorIPC()
    watched = []

    async def attach_as_client():
        watched.append(1)
        return next(outcomes)
    monkeypatch.setattr(ipc, "attach_as_client", attach_as_client)
    assert asyncio.run(ipc.follow()) == "stopped" and len(watched) == 2


# --- detached from the terminal --------------------------------------------------------

def test_windows_detaches_from_the_console_and_its_job(monkeypatch):
    from backend.core import process_identity as pi

    seen = []

    def popen(argv, **kw):
        seen.append(kw.get("creationflags"))
        if len(seen) == 1:
            raise PermissionError(5, "Access is denied")  # the job forbids breakaway
        return "proc"
    monkeypatch.setattr(pi.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", popen)
    for name, value in (("CREATE_NEW_PROCESS_GROUP", 0x200), ("CREATE_NO_WINDOW", 0x08000000)):
        monkeypatch.setattr(subprocess, name, value, raising=False)
    assert pi.spawn_detached(["x"], start_new_session=True) == "proc"
    assert seen[0] == 0x200 | 0x08000000 | 0x01000000
    assert seen[1] == 0x200 | 0x08000000


def test_the_restart_runner_and_the_guard_spawn_detached():
    for rel in ("unified_supervisor.py", "backend/core/memory_guard.py"):
        assert "spawn_detached(" in Path(rel).read_text(encoding="utf-8"), rel


# --- identity without psutil ------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="the native reader is Windows'")
def test_the_native_start_time_is_psutil_s(runner):
    psutil = pytest.importorskip("psutil")
    from backend.core import process_identity as pi

    native = pi._native_started_at(runner.pid)
    assert pi._same_start(native, psutil.Process(runner.pid).create_time())


def test_one_start_time_read_two_ways_is_one_process(monkeypatch):
    from backend.core import process_identity as pi

    monkeypatch.delenv("JARVIS_PROCESS_START_TOLERANCE_S", raising=False)
    assert pi._same_start(1792135086.0664573, 1792135086.0664575)
    assert not pi._same_start(1792135086.06, 1792135086.08)
