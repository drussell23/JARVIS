"""The loading server outlives the completion broadcast until its page has left.

Live 2026-10-14 08:13 and 08:53: the frontend phase stopped the loading server
three seconds into "finalizing", ~20 s before the kernel's completion, and
_stop_loading_server's own "100%" was dropped by the retired gate -- the page
stayed at 95% FINALIZING while the logs said JARVIS was ready.
"""
from __future__ import annotations

import asyncio
import types


def _kernel(presence, alive=lambda: True):
    import unified_supervisor as us

    calls = {"stopped": 0, "asked": 0, "info": []}

    async def _presence(self):
        calls["asked"] += 1
        return presence(calls["asked"])

    async def _stop(self):
        calls["stopped"] += 1

    k = types.SimpleNamespace(
        config=types.SimpleNamespace(loading_server_port=8081),
        logger=types.SimpleNamespace(info=calls["info"].append, debug=lambda *_: None),
        _is_loading_server_alive=alive,
    )
    k._loading_page_presence = types.MethodType(_presence, k)
    k._stop_loading_server = types.MethodType(_stop, k)
    run = types.MethodType(us.JarvisSystemKernel._retire_loading_server_after_handoff, k)
    return run, calls


def test_waits_for_the_page_to_leave_then_retires(monkeypatch):
    monkeypatch.setenv("JARVIS_LOADING_HANDOFF_POLL_S", "0.01")
    run, calls = _kernel(lambda n: {"websocket_clients": 1 if n < 3 else 0})
    asyncio.run(run())
    assert calls["asked"] == 3 and calls["stopped"] == 1


def test_an_unreadable_server_is_retired_without_waiting(monkeypatch):
    def boom(_n):
        raise RuntimeError("gone")
    run, calls = _kernel(boom)
    asyncio.run(run())
    assert calls["asked"] == 1 and calls["stopped"] == 1


def test_a_page_that_never_leaves_is_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_LOADING_HANDOFF_BUDGET_S", "0.1")
    monkeypatch.setenv("JARVIS_LOADING_HANDOFF_POLL_S", "0.01")
    run, calls = _kernel(lambda _n: {"websocket_clients": 2})
    asyncio.run(run())
    assert calls["stopped"] == 1 and any("still open" in m for m in calls["info"])


def test_the_session_log_keeps_lines_the_status_region_prints(tmp_path):
    """The inline region draws on the bare terminal; scrollback lines it
    prints must still reach the session log (they did not, live 08:51)."""
    import io
    from rich.console import Console
    from backend.core import session_log
    from backend.core.supervisor import elite_dashboard as ed

    log = open(tmp_path / "s.log", "w", encoding="utf-8")
    terminal = io.StringIO()
    tee = session_log._Tee(terminal, log)
    assert session_log.console_of(tee) is terminal
    console = Console(file=session_log.console_of(tee), width=100, color_system=None)
    ed._Scrollback(console, tee).write("kernel says hello\n")
    log.close()
    assert "kernel says hello" in terminal.getvalue()
    assert (tmp_path / "s.log").read_text(encoding="utf-8") == "kernel says hello\n"
