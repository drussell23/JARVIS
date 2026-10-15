"""The supervisor's terminal is a view; the session log is the record.

2026-10-14 18:30 boot: 1,756 INFO lines from about a hundred modules
scrolled above the status region, so the user could not follow the boot or
see what JARVIS did with a command. And halfway through every boot the
backend's ``enable_ml_logging`` removed every root handler -- the kernel's
terminal handler and the IPC stream to an attached viewer -- so the terminal
switched to whatever format a leftover basicConfig installed.
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import time

import pytest

from backend.core import console_view as cv
from backend.core.boot_timeline import BootTimeline


@pytest.fixture(autouse=True)
def _fresh_view(monkeypatch):
    monkeypatch.delenv("JARVIS_UI_VERBOSITY", raising=False)
    monkeypatch.setattr(cv, "_override", None)
    yield


# ------------------------------------------------------------------ the view
def test_the_normal_view_shows_warnings_and_the_narrative_only():
    assert cv.verbosity() == "ops"
    assert not cv.shows(logging.INFO) and cv.shows(logging.WARNING)
    assert cv.shows(logging.DEBUG, tone="done")


def test_the_view_follows_the_existing_verbosity_setting(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_VERBOSITY", "summary")
    assert not cv.shows(logging.WARNING) and cv.shows(logging.ERROR)
    monkeypatch.setenv("JARVIS_UI_VERBOSITY", "debug")
    assert cv.shows(logging.DEBUG)


def test_the_view_cycles_at_runtime_and_wraps():
    assert [cv.cycle_verbosity() for _ in range(3)] == ["debug", "summary", "ops"]


def test_a_multi_line_reply_stays_under_its_glyph():
    line = cv.render("I can see your screen.\nVisible: two windows", "reply", color=False)
    assert line == "⏺ I can see your screen.\n  Visible: two windows"
    assert cv.phase_title("clean_slate") == "Clean slate"


# ------------------------------------------------ the terminal and the record
class _Terminal(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 1


@pytest.fixture
def wired(monkeypatch):
    """configure_context_logging on an interactive terminal with a session log.

    Returns a function that (re)wires: pytest replaces sys.stdout again
    between fixture setup and the test body, so the wiring happens in the
    body, where the test's stdout is the one in force."""
    from backend.core import session_log, supervisor_ipc
    terminal, log = _Terminal(), io.StringIO()
    tee = session_log._Tee(terminal, log)
    monkeypatch.setattr(supervisor_ipc.os, "isatty", lambda fd: True)
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    def wire():
        monkeypatch.setattr(sys, "stdout", tee)
        supervisor_ipc.configure_context_logging()
        return terminal, log
    try:
        yield wire
    finally:
        root.handlers, root.level = saved_handlers, saved_level


def test_the_terminal_is_quiet_while_the_session_log_keeps_everything(wired):
    terminal, log = wired()
    logging.getLogger("backend.some.module").info("a detail nobody needs to watch")
    logging.getLogger("backend.some.module").warning("something needs attention")
    cv.narrate("Preflight", "done", suffix="15.7s")
    shown, kept = terminal.getvalue(), log.getvalue()
    assert "a detail nobody needs" not in shown and "a detail nobody needs" in kept
    assert "something needs attention" in shown and "something needs attention" in kept
    assert "✓" in shown and "Preflight" in shown and "Preflight" in kept
    assert "\x1b[" not in kept                      # the record carries no escape codes
    assert shown.count("something needs attention") == 1 and kept.count("something needs") == 1


def test_switching_to_the_full_view_shows_everything_from_the_next_line(wired):
    terminal, _ = wired()
    cv.set_verbosity("debug")
    logging.getLogger("backend.some.module").info("now visible")
    assert "now visible" in terminal.getvalue()


def test_configuring_twice_does_not_double_any_line(wired):
    wired()
    terminal, log = wired()
    logging.getLogger("x").warning("once")
    assert terminal.getvalue().count("once") == 1 and log.getvalue().count("once") == 1


def test_enabling_ml_logging_leaves_the_hosts_handlers_alone(monkeypatch):
    sys.path.insert(0, "backend")
    try:
        import enable_ml_logging as eml
    finally:
        sys.path.remove("backend")
    sentinel = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(sentinel)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        first = eml.configure_ml_logging()
        assert eml.configure_ml_logging() is first          # once per process
        assert sentinel in root.handlers
        assert "REAL-TIME LOGGING ENABLED" not in out.getvalue()
        ml = logging.getLogger("ml_models")
        assert not any(isinstance(h, logging.StreamHandler) for h in ml.handlers)
    finally:
        root.removeHandler(sentinel)


def test_the_kernels_own_logger_follows_the_view(monkeypatch):
    """UnifiedLogger prints itself, outside the stdlib handlers."""
    from backend.core import session_log
    recorded = []
    monkeypatch.setattr(session_log, "log_only", recorded.append)

    class _Kernel:
        _json_mode = False
        _STDLIB_LEVELS = {"INFO": logging.INFO, "WARNING": logging.WARNING, "PHASE": logging.INFO}

    import importlib
    in_view = importlib.import_module("unified_supervisor").UnifiedLogger._in_view
    assert in_view(_Kernel(), "INFO", "plain info") is False and recorded == ["plain info"]
    assert in_view(_Kernel(), "WARNING", "plain warning") is True


# --------------------------------------------------------- the boot checklist
def test_the_region_shows_running_phases_and_what_comes_next(tmp_path):
    import json
    from backend.core.supervisor import elite_dashboard as ed
    store = tmp_path / "t.json"
    store.write_text(json.dumps({"boots": 3, "total": 100, "milestones": {
        "preflight:start": 0, "preflight:end": 15, "backend:start": 15, "backend:end": 50,
        "frontend:start": 90, "frontend:end": 95}}))
    now = time.time()
    tl = BootTimeline(origin=now - 20, store=store)
    tl.phase_started("preflight", now - 20)
    tl.phase_ended("preflight", now - 5)
    tl.phase_started("backend", now - 5)
    lines = [str(line) for line in ed._boot_checklist_lines(tl)]
    assert lines[0].endswith(("5.0s", "5.1s")) and "Backend" in lines[0]
    assert lines[-1] == "○ next: Frontend"


def test_a_finished_phase_becomes_one_checklist_line(monkeypatch):
    import importlib
    us = importlib.import_module("unified_supervisor")
    said = []
    monkeypatch.setattr(cv, "narrate", lambda text, tone="note", suffix="": said.append((text, tone, suffix)))
    renderer = us.RichCliRenderer(no_animation=True)
    renderer.start()
    t0 = time.time()
    renderer.handle_event(us.SupervisorEvent(event_type=us.SupervisorEventType.PHASE_START,
                                             timestamp=t0, message="", phase="clean_slate",
                                             correlation_id="c1"))
    renderer.handle_event(us.SupervisorEvent(event_type=us.SupervisorEventType.PHASE_END,
                                             timestamp=t0 + 18.4, message="", phase="clean_slate",
                                             correlation_id="c1", duration_ms=18400))
    assert said == [("Clean slate", "done", "18s")]


def test_the_view_key_cycles_and_other_keys_do_nothing():
    from backend.core.supervisor import elite_dashboard as ed
    pressed = []
    keys = ed._ViewKeys("v", lambda: pressed.append(1))
    for ch in "xVqv":
        keys._handle(ch)
    assert pressed == [1, 1]


# --------------------------------------------------------------- the transcript
def test_a_command_reads_as_a_transcript(monkeypatch):
    from backend.api import unified_command_processor as ucp
    said = []
    monkeypatch.setattr(cv, "narrate", lambda text, tone="note", suffix="": said.append((tone, text)))
    proc = ucp.UnifiedCommandProcessor.__new__(ucp.UnifiedCommandProcessor)
    proc._dedup_cache, proc._dedup_inflight, proc._dedup_ttl, proc._dedup_max = {}, {}, 60.0, 10

    async def pipeline(text, **_kw):
        await asyncio.sleep(0.01)
        return {"success": True, "response": "I searched Google for dogs.",
                "facts": ['Opened https://www.google.com/search?q=dogs; its window "dogs - Google" is in front']}

    monkeypatch.setattr(proc, "_execute_command_pipeline", pipeline)

    async def go():
        await asyncio.gather(proc.process_command("search for dogs", request_id="c1"),
                             proc.process_command("search for dogs", request_id="c1"))
    asyncio.run(go())
    assert [tone for tone, _ in said] == ["command", "reply", "detail"]     # once, not per resend
    assert said[0][1] == "search for dogs" and "dogs - Google" in said[2][1]


def test_a_failed_command_says_so(monkeypatch):
    from backend.api import unified_command_processor as ucp
    said = []
    monkeypatch.setattr(cv, "narrate", lambda text, tone="note", suffix="": said.append((tone, text)))
    ucp.UnifiedCommandProcessor._narrate_outcome(
        {"success": False, "response": "Started Firefox, but no window of it appeared"}, 12.0)
    assert said == [("failed", "Started Firefox, but no window of it appeared")]


# ------------------------------------------------------------------ the viewer
def test_an_attached_viewer_shows_the_same_view(capsys):
    from backend.core.supervisor_ipc import _render_event
    _render_event({"type": "log", "level": "INFO", "logger": "x", "msg": "hidden detail"}, color=True)
    _render_event({"type": "log", "level": "WARNING", "logger": "x", "msg": "a warning"}, color=True)
    _render_event({"type": "log", "level": "INFO", "logger": cv.NARRATIVE_LOGGER,
                   "msg": "search for dogs", "tone": "command"}, color=True)
    out = capsys.readouterr().out
    assert "hidden detail" not in out and "a warning" in out
    assert "›" in out and "search for dogs" in out
    _render_event({"type": "log", "level": "INFO", "logger": "x", "msg": "piped"}, color=False)
    assert "piped" in capsys.readouterr().out                # a pipe still gets every event
