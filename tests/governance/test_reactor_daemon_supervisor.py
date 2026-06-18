"""Tests for the Sovereign Lifecycle Daemon Supervisor (reactor-core Soul process management).

Covers `backend/core/reactor_daemon_supervisor.py`:
1. Pressure level → nice mapping (Phase 3).
2. Adaptive re-nice: memory pressure + busy-signal compose (strictest wins); no-op when unchanged.
3. setpriority is invoked with the right (PRIO_PROCESS, pid, nice); fail-soft on errors.
4. Log rotation logger is configured (size-rotated).
5. Signal handlers register on the loop (Phase 2).
6. Gating flag (default OFF).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.core.reactor_daemon_supervisor import (
    ReactorDaemonSupervisor,
    daemon_enabled,
    nice_for_level,
)


class _Level:
    def __init__(self, v: str) -> None:
        self.value = v


class _Gate:
    def __init__(self, level: str) -> None:
        self._level = _Level(level)

    def pressure(self):  # noqa: ANN201
        return self._level


class _Proc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: Optional[int] = None


def _sup(tmp_path: Path, gate=None, busy=None, setpri=None) -> ReactorDaemonSupervisor:
    return ReactorDaemonSupervisor(
        repo_path=tmp_path, port=8090, log_dir=str(tmp_path / "logs"),
        mem_gate=gate, busy_signal=busy, setpriority=setpri,
    )


# --------------------------------------------------------------------------- nice mapping
class TestNiceMapping:
    def test_levels(self) -> None:
        assert nice_for_level(_Level("ok")) == 0
        assert nice_for_level(_Level("warn")) == 5
        assert nice_for_level(_Level("high")) == 10
        assert nice_for_level(_Level("critical")) == 15

    def test_str_level(self) -> None:
        assert nice_for_level("critical") == 15

    def test_unknown_level_zero(self) -> None:
        assert nice_for_level(_Level("bogus")) == 0

    def test_custom_map(self) -> None:
        assert nice_for_level("high", {"high": 7}) == 7


# --------------------------------------------------------------------------- adaptive nice
class TestAdaptiveNice:
    def test_renice_under_pressure(self, tmp_path: Path) -> None:
        calls: List[Tuple[int, int, int]] = []
        sup = _sup(tmp_path, gate=_Gate("high"),
                   setpri=lambda which, who, prio: calls.append((which, who, prio)))
        sup._proc = _Proc(pid=999)
        applied = sup.apply_adaptive_nice()
        assert applied == 10
        assert calls == [(os.PRIO_PROCESS, 999, 10)]

    def test_ok_pressure_no_renice_change(self, tmp_path: Path) -> None:
        calls: List = []
        sup = _sup(tmp_path, gate=_Gate("ok"),
                   setpri=lambda *a: calls.append(a))
        sup._proc = _Proc()
        applied = sup.apply_adaptive_nice()
        assert applied == 0
        assert calls == []  # already at nice 0 → no-op

    def test_busy_signal_forces_deprioritize(self, tmp_path: Path) -> None:
        calls: List = []
        sup = _sup(tmp_path, gate=_Gate("ok"), busy=lambda: True,
                   setpri=lambda *a: calls.append(a))
        sup._proc = _Proc(pid=7)
        applied = sup.apply_adaptive_nice()
        assert applied == 10  # busy → at least 'high' nice even though memory OK
        assert calls and calls[0][2] == 10

    def test_no_proc_returns_none(self, tmp_path: Path) -> None:
        sup = _sup(tmp_path, gate=_Gate("critical"), setpri=lambda *a: None)
        assert sup.apply_adaptive_nice() is None

    def test_dead_proc_returns_none(self, tmp_path: Path) -> None:
        sup = _sup(tmp_path, gate=_Gate("critical"), setpri=lambda *a: None)
        p = _Proc(); p.returncode = 0
        sup._proc = p
        assert sup.apply_adaptive_nice() is None

    def test_setpriority_failure_is_failsoft(self, tmp_path: Path) -> None:
        def _boom(*a):
            raise PermissionError("nope")
        sup = _sup(tmp_path, gate=_Gate("critical"), setpri=_boom)
        sup._proc = _Proc()
        # must not raise; nice stays unchanged
        assert sup.apply_adaptive_nice() == 0

    def test_target_nice_strictest_wins(self, tmp_path: Path) -> None:
        sup = _sup(tmp_path, gate=_Gate("warn"), busy=lambda: True)
        # warn=5 vs busy→high=10 → strictest (10)
        assert sup._target_nice() == 10


# --------------------------------------------------------------------------- logging
class TestLogRotation:
    def test_rotating_logger_configured(self, tmp_path: Path) -> None:
        from logging.handlers import RotatingFileHandler
        sup = ReactorDaemonSupervisor(repo_path=tmp_path, log_dir=str(tmp_path / "logs"),
                                      log_max_bytes=1024, log_backups=3)
        lg = sup._make_file_logger()
        handlers = [h for h in lg.handlers if isinstance(h, RotatingFileHandler)]
        assert handlers, "expected a RotatingFileHandler"
        h = handlers[0]
        assert h.maxBytes == 1024 and h.backupCount == 3
        assert (tmp_path / "logs").is_dir()


# --------------------------------------------------------------------------- signals
class TestSignalHandlers:
    def test_install_registers_three_signals(self, tmp_path: Path) -> None:
        import signal as _signal
        registered: List[int] = []

        class _Loop:
            def add_signal_handler(self, sig, cb):  # noqa: ANN001
                registered.append(sig)

        sup = _sup(tmp_path)
        sup.install_signal_handlers(_Loop())  # type: ignore[arg-type]
        assert set(registered) == {_signal.SIGTERM, _signal.SIGHUP, _signal.SIGINT}

    def test_install_failsoft_when_not_supported(self, tmp_path: Path) -> None:
        class _Loop:
            def add_signal_handler(self, sig, cb):  # noqa: ANN001
                raise NotImplementedError
        sup = _sup(tmp_path)
        sup.install_signal_handlers(_Loop())  # must not raise


# --------------------------------------------------------------------------- gating
class TestGating:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_REACTOR_DAEMON_ENABLED", raising=False)
        assert daemon_enabled() is False

    def test_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_REACTOR_DAEMON_ENABLED", "true")
        assert daemon_enabled() is True
