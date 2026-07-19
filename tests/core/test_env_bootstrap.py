"""Phase 0 — centralized .env + frontend lifecycle + zombie prevention.

Mandate 4 verbatim (2026-07-19): a mock frontend subprocess running at
teardown → terminate() called AND its closure awaited before the loop
exits.
"""
from __future__ import annotations

import os

import pytest

from backend.core import env_bootstrap as eb


@pytest.fixture(autouse=True)
def _reset_loaded(monkeypatch):
    monkeypatch.setattr(eb, "_LOADED", False)
    yield


class TestConfigPrecedence:
    def test_env_file_loaded_once(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("JARVIS_TEST_ONLY_KEY=from_file\n")
        monkeypatch.delenv("JARVIS_TEST_ONLY_KEY", raising=False)
        assert eb.load_env_once(f) is True
        assert os.environ.get("JARVIS_TEST_ONLY_KEY") == "from_file"
        # Second call is a no-op (idempotent):
        f.write_text("JARVIS_TEST_ONLY_KEY=changed\n")
        assert eb.load_env_once(f) is True
        assert os.environ.get("JARVIS_TEST_ONLY_KEY") == "from_file"  # unchanged

    def test_real_env_wins_over_file(self, tmp_path, monkeypatch):
        """MANDATE 2: override=False — explicit env / launchd ALWAYS
        beats the .env file."""
        f = tmp_path / ".env"
        f.write_text("JARVIS_PRECEDENCE_KEY=from_file\n")
        monkeypatch.setenv("JARVIS_PRECEDENCE_KEY", "from_launchd")
        eb.load_env_once(f)
        assert os.environ.get("JARVIS_PRECEDENCE_KEY") == "from_launchd"

    def test_missing_file_degrades_no_crash(self, tmp_path):
        assert eb.load_env_once(tmp_path / "nope.env") is False

    def test_env_file_override_path(self, monkeypatch, tmp_path):
        f = tmp_path / "custom.env"
        f.write_text("x=1\n")
        monkeypatch.setenv("JARVIS_ENV_FILE", str(f))
        assert eb.env_file_path() == f


class TestFrontendAutolaunchGate:
    def test_default_off_no_browser(self, monkeypatch):
        monkeypatch.delenv("JARVIS_FRONTEND_AUTOLAUNCH", raising=False)
        assert eb.frontend_autolaunch_enabled() is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FRONTEND_AUTOLAUNCH", "true")
        assert eb.frontend_autolaunch_enabled() is True

    def test_supervisor_gates_start_task_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "unified_supervisor.py").read_text()
        body = src[src.index("async def _ensure_frontend_start_task"):][:900]
        assert "frontend_autolaunch_enabled" in body
        assert "_frontend_autolaunch_disabled_noop" in body


class _MockProc:
    """A mock frontend subprocess with the asyncio.Process contract."""
    def __init__(self, *, exit_after_terminate=True):
        self.returncode = None
        self.pid = 424242
        self._terminated = False
        self._killed = False
        self._waited = False
        self._exit_after_terminate = exit_after_terminate

    def terminate(self):
        self._terminated = True
        if self._exit_after_terminate:
            self.returncode = 0

    def kill(self):
        self._killed = True
        self.returncode = -9

    async def wait(self):
        self._waited = True
        return self.returncode


class TestZombieProcessPrevention:
    async def test_teardown_terminates_and_awaits_closure(self, monkeypatch):
        """MANDATE 4 VERBATIM: mock frontend running at teardown →
        terminate() called + wait() awaited before returning."""
        # Force the no-killpg path (test PID isn't a real group):
        import signal as _sig
        monkeypatch.setattr(
            "os.killpg",
            lambda *a: (_ for _ in ()).throw(ProcessLookupError()),
        )
        proc = _MockProc(exit_after_terminate=True)
        outcome = await eb.terminate_frontend_subprocess(
            proc, graceful_timeout_s=2.0,
        )
        assert proc._terminated is True          # terminate() called
        assert proc._waited is True              # closure AWAITED
        assert outcome == "terminated"
        assert proc.returncode == 0              # process released

    async def test_hung_process_escalates_to_kill(self, monkeypatch):
        monkeypatch.setattr(
            "os.killpg",
            lambda *a: (_ for _ in ()).throw(ProcessLookupError()),
        )
        proc = _MockProc(exit_after_terminate=False)  # ignores SIGTERM

        # First wait times out (still running), kill() then exits it.
        real_wait = proc.wait
        async def _wait_then_exit():
            if not proc._killed:
                import asyncio
                await asyncio.sleep(5)            # forces the graceful timeout
            return proc.returncode
        proc.wait = _wait_then_exit
        outcome = await eb.terminate_frontend_subprocess(
            proc, graceful_timeout_s=0.1, kill_timeout_s=2.0,
        )
        assert proc._killed is True              # escalated
        assert outcome == "killed"

    async def test_already_exited_and_none_safe(self):
        exited = _MockProc()
        exited.returncode = 0
        assert await eb.terminate_frontend_subprocess(exited) == "already_exited"
        assert await eb.terminate_frontend_subprocess(None) == "none"

    def test_stop_frontend_in_teardown_sequence_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "unified_supervisor.py").read_text()
        # _stop_frontend is invoked in the cleanup path (deterministic
        # teardown), and it awaits the process wait().
        assert "await self._stop_frontend()" in src
        body = src[src.index("async def _stop_frontend"):][:1500]
        assert "terminate()" in body and "wait()" in body
