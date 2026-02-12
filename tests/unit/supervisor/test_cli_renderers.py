# tests/unit/supervisor/test_cli_renderers.py
"""
Unit tests for CLI renderer classes in unified_supervisor.py (lines 6181-6379).

Covers: CliRenderer (base), PlainCliRenderer, JsonCliRenderer, RichCliRenderer,
and the _create_cli_renderer factory function.
"""
import json
import re
import sys
import time

import pytest
from unittest.mock import MagicMock, patch


def _make_event(
    event_type_name="LOG",
    message="test message",
    severity_name="INFO",
    phase="",
    component="",
    duration_ms=0.0,
    progress_pct=-1,
    metadata=(),
    correlation_id="",
    timestamp=None,
):
    """
    Convenience helper to build a SupervisorEvent without importing at module level.

    Parameters use string names for event_type and severity to avoid importing
    the enums at module scope.
    """
    import unified_supervisor as us

    etype = getattr(us.SupervisorEventType, event_type_name)
    sev = getattr(us.SupervisorEventSeverity, severity_name)
    ts = timestamp if timestamp is not None else time.time()
    return us.SupervisorEvent(
        event_type=etype,
        timestamp=ts,
        message=message,
        severity=sev,
        phase=phase,
        component=component,
        duration_ms=duration_ms,
        progress_pct=progress_pct,
        metadata=metadata,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# TestCliRendererBase
# ---------------------------------------------------------------------------
class TestCliRendererBase:
    """Tests for the abstract CliRenderer base class (should_display, start, stop)."""

    def test_verbosity_debug_shows_all(self):
        """Debug verbosity shows every event, including DEBUG-severity ones."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="debug")
        debug_event = _make_event(severity_name="DEBUG", event_type_name="LOG")
        info_event = _make_event(severity_name="INFO", event_type_name="LOG")
        critical_event = _make_event(severity_name="CRITICAL", event_type_name="ERROR")

        assert renderer.should_display(debug_event) is True
        assert renderer.should_display(info_event) is True
        assert renderer.should_display(critical_event) is True

    def test_verbosity_ops_hides_debug(self):
        """Ops verbosity hides DEBUG-severity events but shows everything else."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="ops")
        debug_event = _make_event(severity_name="DEBUG", event_type_name="LOG")
        info_event = _make_event(severity_name="INFO", event_type_name="LOG")
        warning_event = _make_event(severity_name="WARNING", event_type_name="WARNING")
        error_event = _make_event(severity_name="ERROR", event_type_name="ERROR")

        assert renderer.should_display(debug_event) is False
        assert renderer.should_display(info_event) is True
        assert renderer.should_display(warning_event) is True
        assert renderer.should_display(error_event) is True

    def test_verbosity_summary_shows_only_phases_and_critical(self):
        """Summary verbosity only shows phase transitions, startup/shutdown, errors, and CRITICAL."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="summary")

        # These should be shown (allowed event types for summary)
        phase_start = _make_event(event_type_name="PHASE_START", severity_name="INFO")
        phase_end = _make_event(event_type_name="PHASE_END", severity_name="INFO")
        startup_complete = _make_event(event_type_name="STARTUP_COMPLETE", severity_name="SUCCESS")
        shutdown_start = _make_event(event_type_name="SHUTDOWN_START", severity_name="INFO")
        shutdown_end = _make_event(event_type_name="SHUTDOWN_END", severity_name="INFO")
        error_event = _make_event(event_type_name="ERROR", severity_name="ERROR")
        critical_log = _make_event(event_type_name="LOG", severity_name="CRITICAL")

        assert renderer.should_display(phase_start) is True
        assert renderer.should_display(phase_end) is True
        assert renderer.should_display(startup_complete) is True
        assert renderer.should_display(shutdown_start) is True
        assert renderer.should_display(shutdown_end) is True
        assert renderer.should_display(error_event) is True
        assert renderer.should_display(critical_log) is True

        # These should be hidden (non-phase, non-critical)
        plain_log = _make_event(event_type_name="LOG", severity_name="INFO")
        metric = _make_event(event_type_name="METRIC", severity_name="INFO")
        health = _make_event(event_type_name="HEALTH_CHECK", severity_name="INFO")
        component = _make_event(event_type_name="COMPONENT_STATUS", severity_name="INFO")

        assert renderer.should_display(plain_log) is False
        assert renderer.should_display(metric) is False
        assert renderer.should_display(health) is False
        assert renderer.should_display(component) is False

    def test_start_stop_toggles_running(self):
        """start() sets _running to True, stop() sets it back to False."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="ops")
        assert renderer._running is False

        renderer.start()
        assert renderer._running is True

        renderer.stop()
        assert renderer._running is False


# ---------------------------------------------------------------------------
# TestPlainCliRenderer
# ---------------------------------------------------------------------------
class TestPlainCliRenderer:
    """Tests for PlainCliRenderer text output formatting."""

    def test_plain_format_output(self, capsys):
        """Correct format: [+NNNms] [TYPE] [phase] [component] message (duration) [pct%]."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="ops", no_ansi=True)
        base_time = time.time()
        renderer._start_time = base_time

        event = _make_event(
            event_type_name="PHASE_START",
            message="Starting backend",
            severity_name="INFO",
            phase="backend",
            component="jarvis-body",
            duration_ms=123.4,
            progress_pct=50.0,
            timestamp=base_time + 1.5,  # 1500ms after start
        )

        renderer.start()
        renderer.handle_event(event)
        captured = capsys.readouterr()

        line = captured.out.strip()
        # Verify all expected parts are present
        assert "[+   1500ms]" in line
        assert "[PHASE_START]" in line
        assert "[backend]" in line
        assert "[jarvis-body]" in line
        assert "Starting backend" in line
        assert "(123.4ms)" in line
        assert "[50%]" in line

    def test_plain_not_running_suppressed(self, capsys):
        """No output when the renderer is not running (_running=False)."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="ops")
        # Do NOT call start(), so _running stays False
        event = _make_event(message="should not appear")

        renderer.handle_event(event)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_plain_never_raises_on_handler_error(self):
        """A corrupted event with missing attributes does not raise."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="debug")
        renderer.start()

        # Create a corrupted "event" that will cause AttributeError
        corrupted = MagicMock()
        corrupted.timestamp = None  # Will fail arithmetic
        corrupted.event_type = MagicMock()
        corrupted.event_type.value = "log"
        corrupted.severity = MagicMock()
        corrupted.severity.value = "info"
        # Make should_display return True
        corrupted.severity.__eq__ = lambda self, other: False
        corrupted.severity.__ne__ = lambda self, other: True

        # This should not raise — the broad except catches everything
        renderer.handle_event(corrupted)

    def test_plain_no_ansi_produces_clean_text(self, capsys):
        """When no_ansi=True, output contains no ANSI escape sequences."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="ops", no_ansi=True)
        renderer.start()
        renderer._start_time = time.time()

        event = _make_event(
            event_type_name="LOG",
            message="Clean output",
            severity_name="INFO",
        )
        renderer.handle_event(event)
        captured = capsys.readouterr()

        # ANSI escape codes start with ESC (0x1b / \033)
        ansi_pattern = re.compile(r"\x1b\[")
        assert not ansi_pattern.search(captured.out), (
            f"Found ANSI escape codes in output: {captured.out!r}"
        )

    def test_plain_optional_fields_omitted(self, capsys):
        """When phase, component, duration, and progress are empty/default, they are omitted."""
        import unified_supervisor as us

        renderer = us.PlainCliRenderer(verbosity="ops", no_ansi=True)
        base_time = time.time()
        renderer._start_time = base_time
        renderer.start()

        event = _make_event(
            event_type_name="LOG",
            message="Minimal event",
            severity_name="INFO",
            phase="",
            component="",
            duration_ms=0.0,
            progress_pct=-1,
            timestamp=base_time + 0.1,
        )
        renderer.handle_event(event)
        captured = capsys.readouterr()
        line = captured.out.strip()

        # Should have elapsed and type, but no phase/component brackets beyond those
        assert "[LOG]" in line
        assert "Minimal event" in line

        # Should NOT contain duration or progress markers
        assert "ms)" not in line, "Duration should be omitted when 0"
        assert "%" not in line, "Progress should be omitted when -1"

        # Count bracket groups: should only have [+NNNms] and [LOG]
        bracket_groups = re.findall(r"\[[^\]]+\]", line)
        assert len(bracket_groups) == 2, (
            f"Expected exactly 2 bracket groups (elapsed + type), got {bracket_groups}"
        )


# ---------------------------------------------------------------------------
# TestJsonCliRenderer
# ---------------------------------------------------------------------------
class TestJsonCliRenderer:
    """Tests for JsonCliRenderer JSON-lines output."""

    def test_json_outputs_valid_json(self, capsys):
        """Each event produces a single line of valid JSON."""
        import unified_supervisor as us

        renderer = us.JsonCliRenderer(verbosity="ops")
        renderer.start()

        event = _make_event(
            event_type_name="PHASE_START",
            message="Starting phase",
            severity_name="INFO",
            phase="backend",
        )
        renderer.handle_event(event)
        captured = capsys.readouterr()

        lines = captured.out.strip().split("\n")
        assert len(lines) == 1

        parsed = json.loads(lines[0])
        assert isinstance(parsed, dict)
        assert parsed["message"] == "Starting phase"

    def test_json_compact_format(self, capsys):
        """JSON output uses compact separators (no whitespace after : or ,)."""
        import unified_supervisor as us

        renderer = us.JsonCliRenderer(verbosity="ops")
        renderer.start()

        event = _make_event(
            event_type_name="LOG",
            message="compact test",
            severity_name="INFO",
        )
        renderer.handle_event(event)
        captured = capsys.readouterr()
        raw = captured.out.strip()

        # Compact JSON: no ", " or ": " patterns (spaces after separators)
        # json.dumps with separators=(",", ":") produces no space after , or :
        # Re-serialize with compact separators and compare
        parsed = json.loads(raw)
        expected = json.dumps(parsed, separators=(",", ":"))
        assert raw == expected, (
            f"Output is not compact.\nGot:      {raw!r}\nExpected: {expected!r}"
        )

    def test_json_all_event_fields_present(self, capsys):
        """The always-present fields (event_type, timestamp, message, severity) appear in output."""
        import unified_supervisor as us

        renderer = us.JsonCliRenderer(verbosity="ops")
        renderer.start()

        event = _make_event(
            event_type_name="HEALTH_CHECK",
            message="heartbeat",
            severity_name="INFO",
        )
        renderer.handle_event(event)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())

        assert "event_type" in parsed
        assert "timestamp" in parsed
        assert "message" in parsed
        assert "severity" in parsed

        # Verify values are correct types
        assert isinstance(parsed["event_type"], str)
        assert isinstance(parsed["timestamp"], (int, float))
        assert isinstance(parsed["message"], str)
        assert isinstance(parsed["severity"], str)

    def test_json_never_raises(self):
        """A corrupted event that cannot be serialized does not crash the renderer."""
        import unified_supervisor as us

        renderer = us.JsonCliRenderer(verbosity="debug")
        renderer.start()

        # Create an event-like object whose to_json_dict raises
        corrupted = MagicMock()
        corrupted.event_type = MagicMock()
        corrupted.event_type.value = "log"
        corrupted.severity = MagicMock()
        corrupted.severity.value = "info"
        # Make should_display return True for debug verbosity
        corrupted.to_json_dict = MagicMock(side_effect=RuntimeError("serialize fail"))

        # Should not raise
        renderer.handle_event(corrupted)


# ---------------------------------------------------------------------------
# TestRichCliRenderer
# ---------------------------------------------------------------------------
class TestRichCliRenderer:
    """Minimal tests for RichCliRenderer (avoid requiring Rich at test time)."""

    def test_rich_no_animation_skips_dashboard(self):
        """When no_animation=True, start() never creates a dashboard."""
        import unified_supervisor as us

        renderer = us.RichCliRenderer(verbosity="ops", no_animation=True)
        renderer.start()

        assert renderer._dashboard is None
        assert renderer._running is True

    def test_rich_handle_event_no_crash_when_dashboard_none(self):
        """handle_event works without crashing when _dashboard is None (fault tolerance)."""
        import unified_supervisor as us

        renderer = us.RichCliRenderer(verbosity="ops", no_animation=True)
        renderer.start()
        assert renderer._dashboard is None

        # Send a PHASE_START event — should add to timeline without error
        event = _make_event(
            event_type_name="PHASE_START",
            message="Starting backend",
            severity_name="INFO",
            phase="backend",
            correlation_id="cid-123",
        )
        renderer.handle_event(event)

        # Verify the event was recorded in the phase timeline
        timeline = renderer.phase_timeline
        assert len(timeline) == 1
        assert timeline[0]["phase"] == "backend"
        assert timeline[0]["status"] == "running"
        assert timeline[0]["cid"] == "cid-123"

        # Send a PHASE_END event to close it
        end_event = _make_event(
            event_type_name="PHASE_END",
            message="Backend ready",
            severity_name="SUCCESS",
            phase="backend",
            duration_ms=2500.0,
            correlation_id="cid-123",
        )
        renderer.handle_event(end_event)

        timeline = renderer.phase_timeline
        assert len(timeline) == 1
        assert timeline[0]["status"] == "complete"
        assert timeline[0]["duration_ms"] == 2500.0


# ---------------------------------------------------------------------------
# TestCliRendererFactory
# ---------------------------------------------------------------------------
class TestCliRendererFactory:
    """Tests for _create_cli_renderer factory function."""

    def test_factory_json_mode(self, monkeypatch):
        """When JARVIS_LOG_JSON=1 and ui_mode='auto', factory returns JsonCliRenderer."""
        import unified_supervisor as us

        monkeypatch.setenv("JARVIS_LOG_JSON", "1")

        renderer = us._create_cli_renderer(
            ui_mode="auto",
            verbosity="ops",
            no_ansi=False,
            no_animation=False,
        )
        assert isinstance(renderer, us.JsonCliRenderer)

    def test_factory_plain_when_not_tty(self, monkeypatch):
        """When stdout is not a TTY and no JSON override, factory returns PlainCliRenderer."""
        import unified_supervisor as us

        monkeypatch.delenv("JARVIS_LOG_JSON", raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        renderer = us._create_cli_renderer(
            ui_mode="auto",
            verbosity="ops",
            no_ansi=True,
            no_animation=False,
        )
        assert isinstance(renderer, us.PlainCliRenderer)

    def test_factory_explicit_plain(self):
        """When ui_mode='plain' is specified explicitly, factory returns PlainCliRenderer."""
        import unified_supervisor as us

        renderer = us._create_cli_renderer(
            ui_mode="plain",
            verbosity="summary",
            no_ansi=True,
            no_animation=False,
        )
        assert isinstance(renderer, us.PlainCliRenderer)
        assert renderer._verbosity == "summary"
