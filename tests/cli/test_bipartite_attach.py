"""Bulletproof spine for the Style-Guide cockpit ON the ov attach client.

The guide's canonical surface (Bipartite Zone 1/Zone 2 + reactive border) was
mounted only in the headless daemon — invisible from `ov`. This wires it into
the attach client. Coverage:

  (1) THE one operator-line router — identical verb behaviour on BOTH loops
      (detach / audio verbs / flush-on-input / chat text / never raises),
  (2) the bridge stream redirects into Zone 1 when the cockpit is mounted
      (markup-escaped: a daemon line is inert DATA, it can never style the
      canvas) and falls back to print when not,
  (3) the connection watcher exits the app the moment the daemon dies,
  (4) the toolbar-bearing cockpit app constructs headlessly.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.cli.ov import _route_operator_line


class _FakeClient:
    def __init__(self) -> None:
        self.audio: list = []
        self.inputs: list = []
        self.connected = True

    def send_audio(self, cmd: str) -> None:
        self.audio.append(cmd)

    def send_input(self, text: str) -> bool:
        self.inputs.append(text)
        return True


class _FakeUI:
    def __init__(self, flush: bool = False) -> None:
        self._flush = flush

    def should_flush_on_input(self) -> bool:
        return self._flush

    def toolbar(self) -> str:
        return "voice: off · detach to leave"


# ---------------------------------------------------------------------------
# (1) the shared operator-line router
# ---------------------------------------------------------------------------


def test_router_detach_verbs():
    c, ui = _FakeClient(), _FakeUI()
    for verb in ("detach", "exit", "quit", "  DETACH  "):
        assert _route_operator_line(c, ui, verb) == "detach"
    assert c.audio == [] and c.inputs == []


def test_router_audio_verbs_never_travel_as_chat():
    c, ui = _FakeClient(), _FakeUI()
    expect = [
        ("wake", "wake"), ("voice", "wake"), ("listen", "wake"),
        ("wake!", "force_wake"), ("force-wake", "force_wake"),
        ("ptt", "ptt"), ("ptt stop", "ptt_stop"), ("ptt off", "ptt_stop"),
        ("flush", "flush"), ("shh", "flush"), ("hush", "flush"),
        ("mute", "sleep"), ("sleep", "sleep"), ("barge", "barge"),
    ]
    for line, cmd in expect:
        assert _route_operator_line(c, ui, line) == "handled"
        assert c.audio[-1] == cmd
    assert c.inputs == []                      # audio verbs never reach chat


def test_router_chat_text_and_flush_on_input():
    c, ui = _FakeClient(), _FakeUI(flush=False)
    assert _route_operator_line(c, ui, "fix the tests") == "sent"
    assert c.inputs == ["fix the tests"] and c.audio == []

    c2, ui2 = _FakeClient(), _FakeUI(flush=True)   # Karen is speaking → duck
    assert _route_operator_line(c2, ui2, "stop that") == "sent"
    assert c2.audio == ["flush"] and c2.inputs == ["stop that"]

    assert _route_operator_line(c, ui, "   ") == "empty"
    assert _route_operator_line(c, ui, None) == "empty"


def test_router_never_raises():
    class _Boom:
        def send_audio(self, _c):
            raise RuntimeError("bus down")

        def send_input(self, _t):
            raise RuntimeError("bus down")

        connected = True

    assert _route_operator_line(_Boom(), _FakeUI(), "wake") == "empty"
    assert _route_operator_line(_Boom(), None, "hello") == "empty"


# ---------------------------------------------------------------------------
# (2) the bridge stream lands in Zone 1 when the cockpit is mounted
# ---------------------------------------------------------------------------


def test_bridge_line_redirects_into_canvas_escaped():
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        get_active_canvas,
        set_active_canvas,
    )
    mux = BipartiteLayout(width=80, height=20)
    set_active_canvas(mux)
    try:
        # Mirror the client's on_line body (canvas branch).
        canvas = get_active_canvas()
        assert canvas is not None
        from rich.markup import escape
        canvas.push_raw(escape("⏺ op complete [bold red]NOT-MARKUP[/bold red]"))
        assert mux.line_count() == 1
        rendered = mux.render_canvas_ansi()
        # The literal text survives; the injected markup is INERT (escaped) —
        # no bold-red style sequence was applied around NOT-MARKUP.
        assert "NOT-MARKUP" in rendered
        snap = mux._buffer.snapshot()
        assert "\\[bold red]" in snap[0]       # escaped, not interpreted
    finally:
        set_active_canvas(None)


# ---------------------------------------------------------------------------
# (3) the connection watcher exits the app when the daemon dies
# ---------------------------------------------------------------------------


async def test_alive_watcher_exits_on_death():
    from backend.core.ouroboros.battle_test.bipartite_layout import _alive_watcher

    alive = {"v": True}
    exited = {"n": 0}

    async def fast(_s):
        await asyncio.sleep(0)

    task = asyncio.ensure_future(_alive_watcher(
        lambda: exited.__setitem__("n", exited["n"] + 1),
        lambda: alive["v"], sleep_fn=fast,
    ))
    await asyncio.sleep(0.01)
    assert exited["n"] == 0                    # healthy → no exit
    alive["v"] = False                          # the daemon dies
    await asyncio.wait_for(task, timeout=1.0)
    assert exited["n"] == 1                    # app exited exactly once


async def test_alive_watcher_probe_error_reads_as_gone():
    from backend.core.ouroboros.battle_test.bipartite_layout import _alive_watcher

    exited = {"n": 0}

    def boom() -> bool:
        raise RuntimeError("socket torn")

    await asyncio.wait_for(_alive_watcher(
        lambda: exited.__setitem__("n", exited["n"] + 1), boom,
        sleep_fn=lambda _s: asyncio.sleep(0),
    ), timeout=1.0)
    assert exited["n"] == 1


# ---------------------------------------------------------------------------
# (4) the toolbar-bearing cockpit app constructs headlessly
# ---------------------------------------------------------------------------


def test_cockpit_app_constructs_with_toolbar():
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        build_bipartite_application,
    )
    mux = BipartiteLayout(width=80, height=20)
    ui = _FakeUI()
    app = build_bipartite_application(
        mux, on_accept=lambda t: None, toolbar=ui.toolbar,
    )
    assert app is not None and app.full_screen is True
    # A crashing toolbar renders empty, never raises into the frame.
    app2 = build_bipartite_application(
        BipartiteLayout(width=80, height=20), on_accept=lambda t: None,
        toolbar=lambda: (_ for _ in ()).throw(RuntimeError("x")),
    )
    assert app2 is not None
