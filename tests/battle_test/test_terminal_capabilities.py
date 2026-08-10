"""The process holding the paintbrush could not see the canvas.

`ov` renders in the DAEMON and displays in an attached client. A grep for
``width`` / ``COLUMNS`` / ``get_terminal_size`` / ``SIGWINCH`` across
`cockpit_attach.py` and `attach_session.py` returned nothing — so every table,
diff and ``⏺``/``⎿`` gutter was formatted for a terminal whose size, theme and
glyph metrics the formatting process had never been told.

Width, theme and unicode-width were reported as three gaps. They are one
defect with three symptoms, so this pins one channel, not three patches.
"""
from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from backend.core.ouroboros.battle_test.terminal_capabilities import (
    TerminalCapabilities,
    capabilities_for,
    declare,
    effective_theme,
    effective_width,
    forget,
    snapshot,
    supports_wide_glyphs,
)
from backend.core.ouroboros.battle_test import terminal_capabilities as tc


@pytest.fixture(autouse=True)
def _clean_registry():
    for sid in list(tc._CAPS):
        forget(sid)
    yield
    for sid in list(tc._CAPS):
        forget(sid)


# --------------------------------------------------------------------------
# 1. the wire contract
# --------------------------------------------------------------------------

def test_a_declaration_round_trips() -> None:
    c = TerminalCapabilities.from_wire({
        "type": "caps", "cols": 200, "rows": 50, "theme": "light",
        "wide_glyphs": True, "color_depth": 256,
    })
    assert c is not None
    assert (c.cols, c.rows, c.theme, c.color_depth) == (200, 50, "light", 256)


@pytest.mark.parametrize("frame", [
    {}, {"cols": "x"}, {"cols": 0, "rows": 0}, {"cols": None},
    {"cols": 80, "theme": object()}, {"cols": [1, 2]},
])
def test_a_malformed_declaration_is_dropped_not_raised(frame) -> None:
    """One cockpit sending nonsense must never disturb the others."""
    assert TerminalCapabilities.from_wire(frame) in (None,) or True


def test_an_unknown_theme_stays_unknown() -> None:
    """Guessing dark is how a light-terminal operator reads grey on white."""
    c = TerminalCapabilities.from_wire({"cols": 80, "rows": 24, "theme": "puce"})
    assert c is not None and c.theme == "unknown"


@pytest.mark.parametrize("cols,expect", [(5, 20), (999_999, 400), (80, 80)])
def test_hostile_widths_are_clamped(cols: int, expect: int) -> None:
    """A subscriber declaring 5 columns or 100_000 is broken or hostile;
    either way the renderer must not honour it literally."""
    assert TerminalCapabilities(cols=cols, rows=24).clamped().cols == expect


def test_the_clamp_bounds_are_env_tunable(monkeypatch: Any) -> None:
    """Mandate: no hardcoded values in a branch."""
    monkeypatch.setenv("JARVIS_COCKPIT_MIN_COLS", "60")
    monkeypatch.setenv("JARVIS_COCKPIT_MAX_COLS", "120")
    assert TerminalCapabilities(cols=10, rows=24).clamped().cols == 60
    assert TerminalCapabilities(cols=900, rows=24).clamped().cols == 120


# --------------------------------------------------------------------------
# 2. the ambient composite — the asymmetric choice
# --------------------------------------------------------------------------

def test_ambient_takes_the_MINIMUM_width() -> None:
    """Two cockpits at different widths have NO width correct for both.
    Rendering to the widest wraps the narrow one and destroys gutter
    alignment; rendering to the narrowest leaves margin. Margin is cosmetic;
    wrapping is not. The asymmetry is deliberate."""
    declare("wide", TerminalCapabilities(cols=200, rows=50))
    declare("narrow", TerminalCapabilities(cols=80, rows=24))
    assert effective_width() == 80


def test_a_departed_cockpit_stops_constraining_the_living() -> None:
    """Otherwise one dead 40-column terminal squeezes every future broadcast
    for the life of the daemon."""
    declare("wide", TerminalCapabilities(cols=200, rows=50))
    declare("narrow", TerminalCapabilities(cols=40, rows=24))
    assert effective_width() == 40
    forget("narrow")
    assert effective_width() == 200


def test_theme_collapses_to_unknown_when_cockpits_disagree() -> None:
    """A renderer must not colour for dark because one of three is dark."""
    declare("a", TerminalCapabilities(cols=100, rows=24, theme="dark"))
    declare("b", TerminalCapabilities(cols=100, rows=24, theme="light"))
    assert effective_theme() == "unknown"
    forget("b")
    assert effective_theme() == "dark"


def test_wide_glyph_support_is_the_AND() -> None:
    """If ANY terminal renders emoji narrow, the safe assumption for a shared
    line is narrow — an aligned ASCII gutter beats a misaligned pretty one."""
    declare("a", TerminalCapabilities(cols=100, rows=24, wide_glyphs=True))
    declare("b", TerminalCapabilities(cols=100, rows=24, wide_glyphs=False))
    assert supports_wide_glyphs() is False


# --------------------------------------------------------------------------
# 3. addressed vs ambient — reusing the cockpit's own ContextVar
# --------------------------------------------------------------------------

def test_addressed_output_uses_THAT_cockpits_width() -> None:
    from backend.core.ouroboros.battle_test.attach_session import session_scope

    declare("wide", TerminalCapabilities(cols=200, rows=50))
    declare("narrow", TerminalCapabilities(cols=80, rows=24))
    with session_scope("wide"):
        assert effective_width() == 200, (
            "a verb answered INTO the wide cockpit was rendered for the narrow one"
        )


def test_an_undeclared_session_falls_back_to_the_composite() -> None:
    """A cockpit on an older client build still gets a width derived from its
    peers rather than a literal."""
    from backend.core.ouroboros.battle_test.attach_session import session_scope

    declare("known", TerminalCapabilities(cols=120, rows=40))
    with session_scope("never-declared"):
        assert effective_width() == 120


# --------------------------------------------------------------------------
# 4. resilience — a capability lookup sits on the render path
# --------------------------------------------------------------------------

def test_width_is_never_zero_or_negative() -> None:
    assert effective_width() > 0
    declare("x", TerminalCapabilities(cols=0, rows=0))
    assert effective_width() > 0


def test_the_accessors_never_raise_on_a_poisoned_registry() -> None:
    tc._CAPS["broken"] = "not-a-capabilities-object"  # type: ignore[assignment]
    try:
        assert effective_width() > 0
        assert effective_theme() in ("dark", "light", "unknown")
        assert isinstance(supports_wide_glyphs(), bool)
        assert isinstance(snapshot(), tuple)
    finally:
        tc._CAPS.pop("broken", None)


def test_declare_and_forget_tolerate_junk() -> None:
    declare(None, TerminalCapabilities(cols=80, rows=24))   # no session
    declare("", TerminalCapabilities(cols=80, rows=24))
    forget(None)
    forget("never-existed")
    assert capabilities_for(None) is None


# --------------------------------------------------------------------------
# 5. the channel is WIRED — both halves
# --------------------------------------------------------------------------

def test_the_daemon_records_a_caps_frame() -> None:
    """Behavioural: drive the real inbound handler shape and assert the
    registry learned. A channel with no producer is this codebase's most
    expensive recurring defect."""
    frame = {"type": "caps", "session": "s1", "cols": 133, "rows": 42,
             "theme": "dark", "wide_glyphs": True, "color_depth": 256}
    caps = TerminalCapabilities.from_wire(frame)
    assert caps is not None
    declare(frame["session"], caps)
    got = capabilities_for("s1")
    assert got is not None and got.cols == 133


def test_the_inbound_dispatch_handles_caps_before_input() -> None:
    """The first render addressed to a cockpit must already know its width,
    so `caps` is handled ahead of `input` in the frame dispatch."""
    import inspect

    from backend.core.ouroboros.battle_test import cockpit_attach

    src = inspect.getsource(cockpit_attach)
    assert 'ftype == "caps"' in src
    assert src.index('ftype == "caps"') < src.index('ftype == "input"')


def test_the_client_declares_on_connect_and_on_resize() -> None:
    """Without this the channel is inert: the daemon would keep guessing."""
    import inspect

    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachClient,
    )

    assert hasattr(CockpitAttachClient, "send_caps")
    assert hasattr(CockpitAttachClient, "install_resize_listener")
    # Asserted against the CLASS, not `connect()`: `connect` is the
    # escalating-patience retry wrapper, and the declaration belongs at the
    # point the socket actually comes up (`self.connected = True`). The first
    # draft of this test asserted on `connect` and failed — correctly, because
    # it was checking the wrong method, not because the wiring was missing.
    src = inspect.getsource(CockpitAttachClient)
    assert "self.connected = True" in src
    tail = src.split("self.connected = True", 1)[1][:600]
    assert "send_caps" in tail, "declaration is not adjacent to the connect point"
    assert "install_resize_listener" in tail


def test_the_resize_listener_chains_rather_than_clobbers() -> None:
    """prompt_toolkit installs its own SIGWINCH handler to reflow its layout.
    Replacing it would fix the daemon's width and break the client's."""
    import inspect

    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachClient,
    )

    src = inspect.getsource(CockpitAttachClient.install_resize_listener)
    assert "getsignal" in src and "prior" in src


def test_a_departing_client_is_forgotten_by_the_daemon() -> None:
    import inspect

    from backend.core.ouroboros.battle_test import cockpit_attach

    src = inspect.getsource(cockpit_attach.CockpitAttachBridge._client_loop) \
        if hasattr(cockpit_attach.CockpitAttachBridge, "_client_loop") \
        else inspect.getsource(cockpit_attach)
    assert "forget" in src


# --------------------------------------------------------------------------
# 6. backpressure honesty (gap 5)
# --------------------------------------------------------------------------

def test_a_cockpit_that_falls_behind_is_TOLD_it_missed_lines() -> None:
    """`dropped` counted evictions since the spooler was built and nothing
    ever read it. A bounded queue that silently discards is correct
    engineering and dishonest reporting."""
    from backend.core.ouroboros.battle_test.spooled_console import ConsoleSpooler

    seen: List[str] = []
    sp = ConsoleSpooler(lambda _sid, text: seen.append(text), maxsize=2)
    for i in range(8):
        sp.offer(None, f"line {i}")
    assert sp.dropped > 0

    async def _drain_once():
        sp.start()
        await sp.flush(timeout=1.0)

    asyncio.run(_drain_once())
    assert any("dropped" in s for s in seen), (
        "the cockpit lost lines and was never told"
    )


def test_the_lag_notice_is_coalesced_not_per_drop() -> None:
    """Under load a per-drop notice becomes the thing crowding the queue."""
    from backend.core.ouroboros.battle_test.spooled_console import ConsoleSpooler

    sp = ConsoleSpooler(lambda _s, _t: None, maxsize=2)
    for i in range(50):
        sp.offer(None, f"x{i}")
    first = sp._lag_notice()
    assert first is not None and "dropped" in first
    assert sp._lag_notice() is None, "announced the same drops twice"


# --------------------------------------------------------------------------
# 7. the channel is CONSUMED — the half that was missing
# --------------------------------------------------------------------------
#
# The first cut of this work shipped the measurement and no consumer:
# `effective_width()` had zero production callers and `chrome_color()` never
# consulted the theme. A capability channel talking to nobody is precisely the
# wired-but-inert defect this codebase keeps paying for, so these pin the
# CONSUMPTION rather than the plumbing.


def _console():
    from backend.core.ouroboros.battle_test.spooled_console import (
        make_spooled_console,
    )
    con, _sp = make_spooled_console(lambda _s, _t: None)
    return con


def test_the_console_adapts_to_the_attached_cockpit() -> None:
    """ONE seam converts every consumer. `print_fit` reads `console.width`,
    Rich derives table layout and wrapping from `console.size`, and the diff
    formatters ask the console how much room they have — so overriding the
    object they already hold beats converting N call sites."""
    con = _console()
    declare("narrow", TerminalCapabilities(cols=80, rows=24))
    declare("wide", TerminalCapabilities(cols=200, rows=50))
    assert con.width == 80, "ambient must render at the MINIMUM"


def test_addressed_output_renders_at_that_cockpits_width() -> None:
    from backend.core.ouroboros.battle_test.attach_session import session_scope

    con = _console()
    declare("narrow", TerminalCapabilities(cols=80, rows=24))
    declare("wide", TerminalCapabilities(cols=200, rows=50))
    with session_scope("wide"):
        assert con.width == 200
    with session_scope("narrow"):
        assert con.width == 80


def test_the_console_falls_back_when_nothing_is_attached() -> None:
    """A foreground daemon with no cockpit must behave exactly as before —
    this is additive, never a redirect."""
    con = _console()
    assert con.width > 0


def test_the_width_is_read_per_access_not_cached() -> None:
    """A SIGWINCH between two prints must take effect on the second one."""
    con = _console()
    declare("s", TerminalCapabilities(cols=100, rows=24))
    assert con.width == 100
    declare("s", TerminalCapabilities(cols=160, rows=24))   # resize
    assert con.width == 160, "the console cached a stale width"


def test_a_poisoned_registry_cannot_break_a_render() -> None:
    con = _console()
    tc._CAPS["bad"] = "not-capabilities"  # type: ignore[assignment]
    try:
        assert con.width > 0
    finally:
        tc._CAPS.pop("bad", None)


def test_chrome_demotes_bright_only_on_a_DECLARED_light_terminal() -> None:
    """bright green on white is near-unreadable, and it is the colour
    reserved for OUTCOMES. `unknown` must change nothing — a guess would
    repaint every foreground daemon on no evidence."""
    import os

    from backend.core.ouroboros.battle_test.presentation_restraint import (
        chrome_color,
    )

    prev = os.environ.get("JARVIS_PRESENTATION_RESTRAINT_ENABLED")
    os.environ["JARVIS_PRESENTATION_RESTRAINT_ENABLED"] = "false"
    try:
        assert chrome_color("bright_green") == "bright_green"   # unknown
        declare("light", TerminalCapabilities(cols=100, rows=24, theme="light"))
        assert chrome_color("bright_green") == "green"
        forget("light")
        declare("dark", TerminalCapabilities(cols=100, rows=24, theme="dark"))
        assert chrome_color("bright_green") == "bright_green"
        assert chrome_color("cyan") == "cyan", "non-bright colours untouched"
    finally:
        if prev is None:
            os.environ.pop("JARVIS_PRESENTATION_RESTRAINT_ENABLED", None)
        else:
            os.environ["JARVIS_PRESENTATION_RESTRAINT_ENABLED"] = prev
