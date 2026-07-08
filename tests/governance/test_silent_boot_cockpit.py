"""silent_boot cockpit-silence regression suite (ov cockpit silence, Slice 2).

A live COCKPIT battle-test run proved Slice 1's WARNING-threshold gate
insufficient: this codebase uses ``logger.warning(...)`` for routine boot
chatter (``[DiscordBridge]``, ``[asyncio leak]``, etc.), so the terminal
still flooded. This suite pins the fix:

  §A  COCKPIT console handler threshold == ERROR; file handler stays DEBUG.
  §B  Injected WARNING reaches the file handler but NOT the console handler
      in COCKPIT.
  §C  Injected ERROR/CRITICAL reach BOTH handlers in COCKPIT (Mandate 1 --
      the gate lowers verbosity, it never filters fatal-adjacent events).
  §D  SOAK thresholds are byte-identical to pre-Slice-2 (golden).
  §E  Explicit kwarg / explicit operator env override precedence.
  §F  HeartbeatConsoleHandler.emit() no-ops under COCKPIT; unchanged (TTY
      gated) behavior under SOAK.
"""
from __future__ import annotations

import logging
import pathlib
import time

import pytest

from backend.core.ouroboros.governance import silent_boot as sb
from backend.core.ouroboros.governance import headless_telemetry as ht
from backend.core.ouroboros.ui.presentation_mode import PresentationMode


def _wait_for_log_content(
    path: pathlib.Path, needle: str, timeout: float = 5.0,
) -> str:
    deadline = time.time() + timeout
    content = ""
    while time.time() < deadline:
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — best-effort poll
                content = ""
            if needle in content:
                return content
        time.sleep(0.02)
    return content


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_SILENT_BOOT_ENABLED",
        "JARVIS_SILENT_BOOT_TERMINAL_LEVEL",
        "JARVIS_SILENT_BOOT_LOG_FILENAME",
        "JARVIS_OV_PRESENTATION",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    sb.restore_legacy_terminal_logging()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


@pytest.fixture
def session_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "session-test"


def _terminal_handler(root: logging.Logger):
    handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, sb._HANDLER_MARKER, False)
    ]
    assert len(handlers) == 1
    return handlers[0]


# ---------------------------------------------------------------------------
# §A — COCKPIT console handler == ERROR, file handler == DEBUG
# ---------------------------------------------------------------------------


class TestCockpitConsoleThreshold:
    def test_cockpit_console_handler_is_error(
        self, fresh_registry, session_dir,
    ):
        handler = sb.configure_silent_boot(
            session_dir, mode=PresentationMode.COCKPIT,
        )
        assert handler is not None
        assert handler.level == logging.DEBUG  # file handler unaffected
        root = logging.getLogger()
        term = _terminal_handler(root)
        assert term.level == logging.ERROR

    def test_cockpit_via_env_var_matches_explicit_mode(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry, session_dir,
    ):
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
        sb.configure_silent_boot(session_dir)
        root = logging.getLogger()
        term = _terminal_handler(root)
        assert term.level == logging.ERROR


# ---------------------------------------------------------------------------
# §B/§C — end-to-end record routing under COCKPIT
# ---------------------------------------------------------------------------


class TestCockpitRecordRouting:
    def test_warning_reaches_file_not_console(
        self, fresh_registry, session_dir, capsys,
    ):
        sb.configure_silent_boot(session_dir, mode=PresentationMode.COCKPIT)
        test_logger = logging.getLogger("test.cockpit.warn_only_file")
        test_logger.warning("cockpit_warning_should_stay_in_file_only")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        captured = capsys.readouterr()
        assert "cockpit_warning_should_stay_in_file_only" not in captured.err
        log_path = session_dir / "debug.log"
        content = _wait_for_log_content(
            log_path, "cockpit_warning_should_stay_in_file_only",
        )
        assert "cockpit_warning_should_stay_in_file_only" in content

    def test_error_reaches_both_console_and_file(
        self, fresh_registry, session_dir, capsys,
    ):
        sb.configure_silent_boot(session_dir, mode=PresentationMode.COCKPIT)
        test_logger = logging.getLogger("test.cockpit.error_both")
        test_logger.error("cockpit_error_should_reach_console_and_file")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        captured = capsys.readouterr()
        assert "cockpit_error_should_reach_console_and_file" in captured.err
        log_path = session_dir / "debug.log"
        content = _wait_for_log_content(
            log_path, "cockpit_error_should_reach_console_and_file",
        )
        assert "cockpit_error_should_reach_console_and_file" in content

    def test_critical_reaches_both_console_and_file(
        self, fresh_registry, session_dir, capsys,
    ):
        sb.configure_silent_boot(session_dir, mode=PresentationMode.COCKPIT)
        test_logger = logging.getLogger("test.cockpit.critical_both")
        test_logger.critical("cockpit_critical_should_reach_console_and_file")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        captured = capsys.readouterr()
        assert "cockpit_critical_should_reach_console_and_file" in captured.err


# ---------------------------------------------------------------------------
# §D — SOAK golden (byte-identical to pre-Slice-2)
# ---------------------------------------------------------------------------


class TestSoakGoldenUnchanged:
    def test_soak_console_handler_is_warning(
        self, fresh_registry, session_dir,
    ):
        sb.configure_silent_boot(session_dir, mode=PresentationMode.SOAK)
        root = logging.getLogger()
        term = _terminal_handler(root)
        assert term.level == logging.WARNING

    def test_default_mode_no_env_matches_soak(
        self, fresh_registry, session_dir,
    ):
        # No JARVIS_OV_PRESENTATION set -> resolve_presentation_mode()
        # fails safe to SOAK -- must be byte-identical to explicit SOAK.
        sb.configure_silent_boot(session_dir)
        root = logging.getLogger()
        term = _terminal_handler(root)
        assert term.level == logging.WARNING

    def test_soak_warning_reaches_both(
        self, fresh_registry, session_dir, capsys,
    ):
        sb.configure_silent_boot(session_dir, mode=PresentationMode.SOAK)
        test_logger = logging.getLogger("test.soak.warn_both")
        test_logger.warning("soak_warning_should_reach_console_and_file")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        captured = capsys.readouterr()
        assert "soak_warning_should_reach_console_and_file" in captured.err


# ---------------------------------------------------------------------------
# §E — Precedence: explicit kwarg / explicit env override
# ---------------------------------------------------------------------------


class TestThresholdPrecedence:
    def test_explicit_kwarg_wins_over_cockpit_default(
        self, fresh_registry, session_dir,
    ):
        sb.configure_silent_boot(
            session_dir,
            mode=PresentationMode.COCKPIT,
            terminal_threshold=logging.DEBUG,
        )
        root = logging.getLogger()
        term = _terminal_handler(root)
        assert term.level == logging.DEBUG

    def test_explicit_env_override_wins_over_cockpit_default(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry, session_dir,
    ):
        monkeypatch.setenv("JARVIS_SILENT_BOOT_TERMINAL_LEVEL", "INFO")
        sb.configure_silent_boot(session_dir, mode=PresentationMode.COCKPIT)
        root = logging.getLogger()
        term = _terminal_handler(root)
        assert term.level == logging.INFO

    def test_resolve_terminal_threshold_direct(self, fresh_registry):
        assert sb._resolve_terminal_threshold(
            PresentationMode.COCKPIT, None,
        ) == logging.ERROR
        assert sb._resolve_terminal_threshold(
            PresentationMode.SOAK, None,
        ) == logging.WARNING
        assert sb._resolve_terminal_threshold(
            PresentationMode.COCKPIT, logging.DEBUG,
        ) == logging.DEBUG


# ---------------------------------------------------------------------------
# §F — HeartbeatConsoleHandler gating
# ---------------------------------------------------------------------------


class _FakeTTYStream:
    def __init__(self) -> None:
        self.written: list = []

    def isatty(self) -> bool:
        return True

    def write(self, data: str) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass


class TestHeartbeatCockpitGate:
    def test_emit_no_ops_under_cockpit(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
        stream = _FakeTTYStream()
        hb = ht.HeartbeatConsoleHandler(stream=stream)
        record = logging.LogRecord(
            name="test.hb", level=logging.INFO, pathname=__file__,
            lineno=1, msg="hello", args=(), exc_info=None,
        )
        hb.emit(record)
        assert stream.written == []

    def test_emit_unchanged_under_soak(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
        stream = _FakeTTYStream()
        hb = ht.HeartbeatConsoleHandler(stream=stream)
        record = logging.LogRecord(
            name="test.hb", level=logging.INFO, pathname=__file__,
            lineno=1, msg="hello", args=(), exc_info=None,
        )
        hb.emit(record)
        assert len(stream.written) == 1

    def test_emit_unchanged_when_no_mode_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("JARVIS_OV_PRESENTATION", raising=False)
        stream = _FakeTTYStream()
        hb = ht.HeartbeatConsoleHandler(stream=stream)
        record = logging.LogRecord(
            name="test.hb", level=logging.INFO, pathname=__file__,
            lineno=1, msg="hello", args=(), exc_info=None,
        )
        hb.emit(record)
        assert len(stream.written) == 1
