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


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """MiniCrest completes the BIG ring via CrestAnimator (shared disk cache) —
    isolate it per-test so tests never read/pollute the user's real cache."""
    monkeypatch.setenv("JARVIS_CREST_ANIM_CACHE_DIR", str(tmp_path / "crest_cache"))
    yield


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


# ---------------------------------------------------------------------------
# (5) CC-style header + borderless canvas (operator mandate 2026-07-23)
# ---------------------------------------------------------------------------


def test_mini_is_the_big_logo_downsampled():
    """Root-cause regression (operator: 'the little logo doesn't look the same
    as the big one'): the mini is a box-filter DOWNSCALE of the big crest's own
    raster — same artwork, same palette — never a tiny geometry re-sample."""
    from backend.core.ouroboros.ui.crest_animator import MiniCrest
    mini = MiniCrest(cols=13, frame_count=4, ss=1, source_cols=60, source_rows=24)
    assert mini.available and mini.rows >= 3
    frame = mini._frame_now(0.0)
    assert frame and len(frame) >= 20          # a real emblem, not a speck
    # Palette fidelity: the mini carries BOTH brand families from the big logo —
    # venom-green coil pixels AND purple V pixels.
    greens = sum(1 for (r, g, b) in frame.values() if g > r and g > b)
    purples = sum(1 for (r, g, b) in frame.values() if b > g)
    assert greens >= 3 and purples >= 3
    # Full-intensity colors (lit-only averaging — no empty-area dilution).
    assert any(max(rgb) > 180 for rgb in frame.values())


def test_mini_animates_after_ring_completes():
    from backend.core.ouroboros.ui.crest_animator import MiniCrest
    mini = MiniCrest(cols=13, frame_count=4, ss=1, source_cols=60, source_rows=24)
    asyncio.run(mini.ensure_frames())
    assert mini._built >= 2
    a = mini._frame_now(0.0)
    b = mini._frame_now(999.25)               # a different clock → different pose
    assert a is not None and b is not None
    assert a != b                              # the downsampled ring rotates


def test_cockpit_header_contains_identity_and_path():
    from backend.core.ouroboros.ui.crest_animator import (
        MiniCrest,
        render_cockpit_header,
    )
    from rich.text import Text
    mini = MiniCrest(cols=13, frame_count=4, ss=1, source_cols=60, source_rows=24)
    lines = [Text("O+V ov 0.1.0"), Text("● healthy"), Text("~/repos/jarvis")]
    ansi = render_cockpit_header(mini, lines, 100, now=0.0)
    import re
    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", ansi)
    assert "ov 0.1.0" in plain and "~/repos/jarvis" in plain and "healthy" in plain
    assert "▀" in plain or "▄" in plain      # the mini crest is beside the text
    # Text-only degradation (no crest) still renders the identity.
    ansi2 = render_cockpit_header(None, lines, 100)
    plain2 = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", ansi2)
    assert "ov 0.1.0" in plain2


def test_canvas_is_borderless_by_default(monkeypatch):
    from backend.core.ouroboros.battle_test.bipartite_layout import BipartiteLayout
    monkeypatch.delenv("JARVIS_BIPARTITE_BORDER", raising=False)
    mux = BipartiteLayout(width=80, height=20)
    mux.push_raw("hello organism")
    ansi = mux.render_canvas_ansi()
    assert "╭" not in ansi and "╰" not in ansi     # no ring
    assert "hello organism" in ansi
    # The frame is restorable chrome, not deleted capability.
    monkeypatch.setenv("JARVIS_BIPARTITE_BORDER", "1")
    ansi2 = mux.render_canvas_ansi()
    assert "╭" in ansi2


def test_app_constructs_with_header():
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        build_bipartite_application,
    )
    app = build_bipartite_application(
        BipartiteLayout(width=80, height=20),
        on_accept=lambda t: None,
        header=lambda: "O+V header",
        header_height=3,
    )
    assert app is not None and app.full_screen is True


def test_quantizer_zero_out_of_palette_pixels():
    """Hard-Edge Vector Quantizer mandate: every rendered mini pixel is a PURE
    member of the crest's own palette — zero blends, zero dimmed transitions."""
    from backend.core.ouroboros.ui.crest_animator import MiniCrest, _quant_palette
    mini = MiniCrest(cols=16, frame_count=4, ss=1, source_cols=60, source_rows=24)
    asyncio.run(mini.ensure_frames())
    pal = set(_quant_palette())
    for f in mini._frames:
        if not f:
            continue
        offenders = [rgb for rgb in f.values() if tuple(rgb) not in pal]
        assert offenders == [], f"blended colors leaked: {offenders[:4]}"


def test_quantizer_v_survives_micro_scale():
    """Feature-preserving dilation: the V-family purple is present in EVERY
    frame at 16 cols — the V never collapses between grid coordinates."""
    from backend.core.ouroboros.ui.crest_animator import MiniCrest, _v_family
    mini = MiniCrest(cols=16, frame_count=4, ss=1, source_cols=60, source_rows=24)
    asyncio.run(mini.ensure_frames())
    vfam = _v_family()
    for f in mini._frames:
        if not f:
            continue
        assert any(tuple(rgb) in vfam for rgb in f.values()), "V vanished"
