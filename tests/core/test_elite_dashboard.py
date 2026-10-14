"""EliteDashboard: measured health, the boot timeline, and the inline region.

The dashboard used to show defaults (J-Prime OFFLINE, JARVIS "BOOTING" on a
port that was a WSL relay) because its rows were written by whatever events
happened to arrive. Rows are now probe readings only; the boot comes from the
kernel's BootTimeline; and the inline layout pins a short region below the
scrollback instead of taking the whole screen.
"""
import io
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.core.boot_timeline import BootTimeline
from backend.core.supervisor import elite_dashboard as ed
from backend.core.supervisor.elite_dashboard import (
    DashboardState,
    EliteDashboard,
    HealthTarget,
    TickerEvent,
    _append_event,
    _build_layout,
    _build_status_region,
    _process_envelope,
    probe_health,
)


def _render(renderable, width=100) -> str:
    from rich.console import Console
    console = Console(file=io.StringIO(), width=width, color_system=None, force_terminal=False)
    console.print(renderable)
    return console.file.getvalue()


# -- measured health ------------------------------------------------------------
class _Health(BaseHTTPRequestHandler):
    replies = {}

    def do_GET(self):  # noqa: N802
        code, body = self.replies[self.path]
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


@pytest.fixture
def health_server():
    server = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.parametrize("path,code,body,status", [
    ("/healthy", 200, b'{"status": "healthy"}', "ONLINE"),
    ("/degraded", 200, b'{"status": "degraded"}', "DEGRADED"),
    ("/plain", 200, b"ok", "ONLINE"),
    ("/broken", 500, b"", "ERROR"),
])
def test_a_row_is_what_the_component_reports(health_server, path, code, body, status):
    _Health.replies[path] = (code, body)
    reading = probe_health(HealthTarget("x", "X", lambda: health_server + path), timeout=2)
    assert reading.status == status
    assert reading.port == int(health_server.rsplit(":", 1)[1])


def test_nothing_listening_is_offline_and_no_url_is_unconfigured():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe_health(HealthTarget("x", "X", lambda: f"http://127.0.0.1:{port}/h"), 1).status == "OFFLINE"
    assert probe_health(HealthTarget("x", "X", lambda: "")).status == "UNCONFIGURED"


def test_every_row_shows_at_once_and_a_dead_target_does_not_hold_back_the_others(health_server, monkeypatch):
    _Health.replies["/ok"] = (200, b'{"status": "ok"}')
    slow = threading.Event()

    def blocked():
        slow.wait(5)
        return ""

    d = EliteDashboard(enabled=False)
    d.watch_health([HealthTarget("fast", "Fast", lambda: health_server + "/ok"),
                    HealthTarget("slow", "Slow", blocked)])
    deadline = time.time() + 5
    while d._state.repos.get("fast", None) is None or d._state.repos["fast"].status != "ONLINE":
        assert time.time() < deadline
        time.sleep(0.05)
    assert d._state.repos["slow"].status == "MEASURING"
    slow.set()
    d._prober_stop.set()


def test_telemetry_never_writes_a_health_row():
    class E:
        event_schema = "lifecycle.transition@1.0.0"
        payload = {"from_state": "PROBING", "to_state": "READY"}
        source = "jprime_lifecycle_controller"

    state = DashboardState()
    _process_envelope(state, E())
    assert state.repos == {} and state.total_envelopes == 1
    assert "READY" in state.events[-1].message


# -- the ticker -------------------------------------------------------------------
def test_a_repeating_event_folds_into_one_row():
    state = DashboardState()
    for i in range(9):
        _append_event(state, TickerEvent(time.time(), "lifecycle", f"stall {i}", "warn", key="stall"))
    _append_event(state, TickerEvent(time.time(), "voice", "Morning, Derek."))
    assert len(state.events) == 2
    assert state.events[0].count == 9 and state.events[0].message == "stall 8"


# -- the boot comes from the timeline -----------------------------------------------
def test_boot_panel_reads_the_timeline(tmp_path):
    tl = BootTimeline(origin=time.time() - 30, store=tmp_path / "t.json")
    tl.phase_started("clean_slate", tl._origin + 1)
    tl.phase_ended("clean_slate", tl._origin + 13)
    tl.phase_started("agi_os", tl._origin + 13)
    tl.phase_started("agi_os", tl._origin + 14)            # announced twice: one row
    state = DashboardState(timeline=tl)
    text = _render(_build_layout(state), width=160)
    assert "clean_slate (12.0s)" in text and text.count("agi_os") == 1
    tl.completed(tl._origin + 125)
    assert "COMPLETE (125.0s)" in _render(_build_layout(state), width=160)


# -- the inline region ---------------------------------------------------------------
def test_region_is_a_few_lines_whatever_happens(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_STATUS_REGION_EVENTS", "3")
    tl = BootTimeline(origin=time.time(), store=tmp_path / "t.json")
    tl.phase_started("backend", time.time())
    state = DashboardState(timeline=tl)
    state.repos["jarvis"] = ed.RepoHealth("jarvis", "JARVIS (Body)", "DEGRADED", "http://127.0.0.1:8010/health", 188)
    for i in range(40):
        _append_event(state, TickerEvent(time.time(), "fault", f"problem {i}", "error", key=f"k{i}"))
    lines = [l for l in _render(_build_status_region(state), width=80).splitlines() if l.strip()]
    assert len(lines) == 1 + 1 + 1 + 3 + 1            # rule, boot, health, events, counters
    assert "DEGRADED :8010 188ms" in lines[2]
    assert all(len(l) <= 80 for l in lines)


def test_inline_routing_puts_lines_above_and_restores_everything():
    from rich.console import Console
    sink = io.StringIO()
    console = Console(file=sink, width=120, color_system=None, force_terminal=False)
    stream_before = io.StringIO()
    handler = logging.StreamHandler(sys.stderr)
    logger = logging.getLogger("test.routing")
    logger.addHandler(handler)
    out, err = sys.stdout, sys.stderr
    try:
        with ed._terminal_routed_above(console):
            assert handler.stream is not err
            sys.stdout.write("half ")
            sys.stdout.write("a line\n")
            logger.warning("from a handler bound before the region")
        assert sys.stdout is out and sys.stderr is err and handler.stream is err
    finally:
        logger.removeHandler(handler)
    printed = sink.getvalue()
    assert "half a line" in printed and "from a handler bound before the region" in printed
    assert stream_before.getvalue() == ""


def test_layout_defaults_to_inline(monkeypatch):
    monkeypatch.delenv("JARVIS_DASHBOARD_LAYOUT", raising=False)
    assert EliteDashboard(enabled=False).inline
    monkeypatch.setenv("JARVIS_DASHBOARD_LAYOUT", "fullscreen")
    assert not EliteDashboard(enabled=False).inline


def test_disabled_dashboard_starts_nothing():
    d = EliteDashboard(enabled=False)
    d.activate_display()
    assert d._thread is None and d.health()["enabled"] is False
