"""The 2026-10-15 12:05 restart: a stale-kernel attach, an unlogged --restart,
a fault dump that killed the kernel, an exit code that overflowed, and a
guard that outlived its kernel."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


# --- fault reporting -----------------------------------------------------------

def test_windows_dumps_only_the_raising_thread_by_default(monkeypatch):
    from backend.core import fault_reporting as fr

    monkeypatch.delenv("JARVIS_FAULTHANDLER_ALL_THREADS", raising=False)
    monkeypatch.setattr(fr.sys, "platform", "win32")
    assert fr.dump_all_threads() is False
    monkeypatch.setattr(fr.sys, "platform", "linux")
    assert fr.dump_all_threads() is True


def test_the_setting_can_be_declared(monkeypatch):
    from backend.core import fault_reporting as fr

    monkeypatch.setattr(fr.sys, "platform", "win32")
    monkeypatch.setenv("JARVIS_FAULTHANDLER_ALL_THREADS", "true")
    assert fr.dump_all_threads() is True


def test_every_caller_arms_it_through_the_one_place(monkeypatch):
    from backend.core import fault_reporting as fr

    seen = {}
    monkeypatch.setattr(fr.faulthandler, "enable", lambda **kw: seen.update(kw))
    monkeypatch.setattr(fr, "dump_all_threads", lambda: False)
    assert fr.enable_fault_reporting() is True
    assert seen == {"all_threads": False}
    for rel in ("unified_supervisor.py", "backend/core/memory_fault_guard.py"):
        src = Path(rel).read_text(encoding="utf-8")
        assert "faulthandler.enable(" not in src, rel


@pytest.mark.skipif(sys.platform != "win32", reason="the vectored handler exists on Windows")
def test_a_handled_com_exception_does_not_walk_other_threads():
    code = (
        "import sys, threading, ctypes; sys.path.insert(0, '.');"
        "from backend.core.fault_reporting import enable_fault_reporting;"
        "enable_fault_reporting();"
        "stop = []\n"
        "def spin():\n"
        "    while not stop: sum(range(50))\n"
        "ts = [threading.Thread(target=spin, daemon=True) for _ in range(3)]\n"
        "[t.start() for t in ts]\n"
        "try:\n"
        "    ctypes.windll.kernel32.RaiseException(0x80010108, 0, 0, None)\n"
        "except OSError:\n"
        "    pass\n"
        "stop.append(1); print('survived')\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=os.getcwd(), timeout=60,
                          env={**os.environ, "JARVIS_FAULTHANDLER_ALL_THREADS": ""})
    assert "survived" in done.stdout
    assert not re.search(r"^Thread 0x", done.stderr, re.M), done.stderr


# --- the launcher's status query -------------------------------------------------

def _serve(behaviour):
    """A one-connection stand-in daemon on a unix socket; returns its path."""
    path = os.path.join(tempfile.mkdtemp(), "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(4096)
        try:
            behaviour(conn)
        finally:
            time.sleep(0.2)
            conn.close()
            srv.close()
    threading.Thread(target=run, daemon=True).start()
    return Path(path)


def _event(conn, kind, **fields):
    conn.sendall((json.dumps({"type": kind, **fields}) + "\n").encode())


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32",
                    reason="exercised over a POSIX unix socket")
class TestTheStatusQuery:
    @pytest.fixture(autouse=True)
    def _limits(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DAEMON_QUERY_TIMEOUT_S", "0.5")
        monkeypatch.setenv("JARVIS_DAEMON_HANDOVER_SILENCE_S", "3")

    def test_a_busy_daemon_that_keeps_talking_is_waited_for(self):
        """Past the quiet limit, but talking all along: the answer is waited for."""
        from backend.core import daemon_handover as dh

        def busy(conn):
            for _ in range(6):  # ~1.2 s of log lines, more than the 0.5 s quiet limit
                _event(conn, "log", msg="booting")
                time.sleep(0.2)
            _event(conn, "status_response", pid=4242)

        assert dh._query(_serve(busy), {"cmd": "status"}, "status_response")["pid"] == 4242

    def test_a_silent_daemon_is_given_up_on(self):
        from backend.core import daemon_handover as dh

        started = time.monotonic()
        assert dh._query(_serve(lambda conn: time.sleep(2)), {"cmd": "status"}, "status_response") is None
        assert time.monotonic() - started < 1.5

    def test_talking_without_answering_ends_at_the_silence_window(self):
        from backend.core import daemon_handover as dh

        def chatty(conn):
            end = time.monotonic() + 5
            while time.monotonic() < end:
                _event(conn, "log", msg="noise")
                time.sleep(0.1)

        started = time.monotonic()
        assert dh._query(_serve(chatty), {"cmd": "status"}, "status_response") is None
        assert time.monotonic() - started < 4.5


# --- the --restart runner ---------------------------------------------------------

def _runner(session_log, argv, result_path):
    src = Path("unified_supervisor.py").read_text(encoding="utf-8")
    template = re.search(r"_script_content = (f'''.*?''')\n", src, re.S).group(1)
    fake_sys = type("S", (), {"executable": sys.executable, "argv": argv})
    return eval(template, {"_reexec_marker": "_JARVIS_RESTART_REEXEC", "_early_sys": fake_sys,
                           "__file__": str(Path("unified_supervisor.py").resolve()),
                           "_early_os": os, "_result_path": str(result_path),
                           "_session_log": None if session_log is None else str(session_log)})


def test_a_restarted_kernel_writes_its_session_log_as_it_runs(tmp_path):
    log, result = tmp_path / "supervisor-x.log", tmp_path / "r.result"
    script = tmp_path / "runner.py"
    script.write_bytes(_runner(log, ["-c", "print('kernel line'); import sys; sys.exit(7)"], result).encode())
    subprocess.run([sys.executable, str(script)], timeout=60, check=False)
    assert "kernel line" in log.read_text(encoding="utf-8")
    first, second = result.read_text(encoding="utf-8").splitlines()[:2]
    assert first == "7" and second == f"log: {log}"


def test_a_shutdown_still_returns_its_output_whole(tmp_path):
    result = tmp_path / "r.result"
    script = tmp_path / "runner.py"
    script.write_bytes(_runner(None, ["-c", "print('stopped')"], result).encode())
    subprocess.run([sys.executable, str(script)], timeout=60, check=False)
    assert result.read_text(encoding="utf-8").splitlines()[:2] == ["0", "stopped"]


def test_session_logs_share_one_naming_and_keep_the_declared_count(tmp_path, monkeypatch):
    from backend.core import host_platform, session_log

    monkeypatch.setattr(host_platform, "log_dir", lambda: tmp_path)
    monkeypatch.setenv("JARVIS_SESSION_LOG_KEEP", "3")
    for i in range(5):
        old = tmp_path / f"supervisor-2026010{i}-000000.log"
        old.write_text("x")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    path = session_log.new_session_log("supervisor")
    path.write_text("new")
    assert path.name.startswith("supervisor-") and path.suffix == ".log"
    assert len(list(tmp_path.glob("supervisor-*.log"))) == 3


# --- the launcher's exit code ------------------------------------------------------

def test_a_windows_crash_status_passes_through_the_launcher():
    src = Path("unified_supervisor.py").read_text(encoding="utf-8")
    assert "_os._exit(_code if isinstance(_code, int) and _code >= 0 else 1)" not in src
    width = 1 << 32
    for status, expected in ((0xC0000005, -1073741819), (3, 3), (0, 0)):
        code = int(status) % width
        assert (code - width if code >= width >> 1 else code) == expected


# --- the guard pinned to its kernel ------------------------------------------------

def test_a_pinned_guard_lets_go_when_its_kernel_exits(tmp_path):
    from backend.core.memory_guard import MemoryGuard

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        guard = MemoryGuard(tmp_path / "j.jsonl", pid=proc.pid, siblings=lambda: set())
        assert guard._holder() == proc.pid
        proc.kill()
        proc.wait(timeout=10)
        assert guard._holder() is None
    finally:
        if proc.poll() is None:
            proc.kill()


# --- the resource snapshot comes off the loop ---------------------------------------

def test_the_timeout_calculation_judges_the_snapshot_it_is_given():
    from backend.core import pytorch_executor as pe

    obj = pe.IntelligentTimeoutCalculator(base_timeout=10.0)

    class _Monitor:
        def get_snapshot(self, *a, **k):
            raise AssertionError("sampled on the caller's thread")

    obj._resource_monitor = _Monitor()
    snapshot = type("Snap", (), {"is_under_pressure": False})()
    timeout, _ = obj.calculate(op_type="other", snapshot=snapshot)
    assert timeout > 0  # judged without sampling on the calling thread
