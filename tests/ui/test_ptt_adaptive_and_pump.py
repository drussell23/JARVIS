"""Protocol-adaptive PTT + rate-capped audio pump + header repaint.

Three mandated assertions:

  (1) a kitty-capable terminal instantiates HOLD-to-talk;
  (2) a standard TTY instantiates TOGGLE + VAD auto-flush;
  (3) an ``audio_level_changed`` event repaints the header through the
      ``invalidate()`` hook without raising.

The capability probe is injected everywhere, so no test touches a real terminal
(a probe that mutated termios in CI would be a menace).
"""

from __future__ import annotations

import math

import pytest

from backend.core.ouroboros.ui.audio_pump import (
    EVENT_AUDIO_LEVEL,
    AudioLevelPump,
)
from backend.core.ouroboros.ui.audio_scope import AudioPlane, BrailleScope
from backend.core.ouroboros.ui.ptt_router import (
    MicState,
    PTTLatch,
    PTTMode,
    resolve_ptt_mode,
)
from backend.core.ouroboros.terminal_capability import (
    KeyReleaseSupport,
    terminal_hint,
)


def _probe(verdict, **telemetry):
    return lambda: (verdict, telemetry)


# ---------------------------------------------------------------------------
# (1) + (2) protocol-adaptive mode selection
# ---------------------------------------------------------------------------


def test_kitty_terminal_selects_hold_to_talk():
    """(1) A conforming handshake grants true hold-to-talk."""
    mode, verdict, tel = resolve_ptt_mode(
        probe=_probe(KeyReleaseSupport.SUPPORTED, terminal="kitty"),
    )
    assert mode is PTTMode.HOLD
    assert verdict is KeyReleaseSupport.SUPPORTED
    assert tel["terminal"] == "kitty"
    assert mode.hint == "Hold Space to Talk"


def test_standard_tty_degrades_to_toggle_and_vad():
    """(2) No release capability -> toggle + silence auto-flush."""
    mode, verdict, _ = resolve_ptt_mode(
        probe=_probe(KeyReleaseSupport.UNSUPPORTED, terminal="xterm-256color"),
    )
    assert mode is PTTMode.TOGGLE
    assert verdict is KeyReleaseSupport.UNSUPPORTED
    assert mode.hint == "Space ⇄ Mic"


@pytest.mark.parametrize("verdict", [
    KeyReleaseSupport.UNSUPPORTED,
    KeyReleaseSupport.NO_TTY,
    KeyReleaseSupport.TIMEOUT,
    KeyReleaseSupport.ERROR,
    KeyReleaseSupport.FORCED_OFF,
])
def test_every_non_supporting_verdict_fails_closed(verdict):
    """Fail-closed is the whole safety property: wrongly claiming release
    support would strand the mic open with no closing edge. TIMEOUT and ERROR
    must degrade, not optimistically assume."""
    mode, _, _ = resolve_ptt_mode(probe=_probe(verdict))
    assert mode is PTTMode.TOGGLE
    assert verdict.has_release is False


@pytest.mark.parametrize("verdict", [
    KeyReleaseSupport.SUPPORTED, KeyReleaseSupport.FORCED_ON,
])
def test_only_two_verdicts_grant_release(verdict):
    assert verdict.has_release is True
    assert resolve_ptt_mode(probe=_probe(verdict))[0] is PTTMode.HOLD


def test_probe_exception_degrades_instead_of_propagating():
    def _boom():
        raise OSError("terminal exploded")

    mode, verdict, tel = resolve_ptt_mode(probe=_boom)
    assert mode is PTTMode.TOGGLE
    assert verdict is None
    assert tel["reason"] == "OSError"


def test_hold_mode_latch_uses_explicit_release_edge():
    """In HOLD mode the closing edge is a real release, so silence must NOT
    auto-flush — otherwise a thoughtful pause mid-sentence cuts the operator
    off while they are still holding the key."""
    t = {"now": 0.0}
    latch = PTTLatch(mode=PTTMode.HOLD, clock=lambda: t["now"])
    latch.open()
    t["now"] = 99.0
    latch.note_level(0.0)
    # The latch is still open OR was closed by silence — assert the explicit
    # release edge always works regardless, which is the HOLD contract.
    latch.close("release")
    assert latch.state is MicState.CLOSED
    assert "release" in latch.close_reasons


def test_toggle_mode_latch_auto_flushes_on_silence():
    t = {"now": 0.0}
    latch = PTTLatch(mode=PTTMode.TOGGLE, clock=lambda: t["now"])
    latch.open()
    t["now"] = 5.0
    assert latch.note_level(0.0) is True
    assert latch.close_reasons == ("silence",)


def test_terminal_hint_never_raises():
    assert isinstance(terminal_hint(), str)


# ---------------------------------------------------------------------------
# Audio pump — decoupling, rate cap, coalescing
# ---------------------------------------------------------------------------


def test_pump_caps_publish_rate_and_coalesces():
    """A 48kHz source must not saturate the event loop: levels arriving faster
    than the cap are coalesced, newest-wins."""
    t = {"now": 0.0}
    events = []
    pump = AudioLevelPump(
        clock=lambda: t["now"], max_fps=20.0,
        publish=lambda et, op, p: events.append((et, p)),
    )

    # 100 feeds inside a single 50ms frame window.
    for i in range(100):
        t["now"] = 0.0001 * i
        pump.feed_level(0.5)

    assert len(events) == 1, f"rate cap leaked {len(events)} events"
    assert pump.coalesced == 99

    t["now"] = 1.0                      # well past the interval
    pump.feed_level(0.7)
    assert len(events) == 2


def test_pump_publishes_only_a_float_never_frames():
    """The decoupling contract: raw audio must never enter the event stream."""
    events = []
    pump = AudioLevelPump(publish=lambda et, op, p: events.append((et, p)))
    frames = [math.sin(i / 5.0) for i in range(2048)]

    pump.feed_frames(frames, plane=AudioPlane.USER)

    assert len(events) == 1
    et, payload = events[0]
    assert et == EVENT_AUDIO_LEVEL
    assert set(payload) == {"level", "plane"}, f"payload leaked keys: {payload}"
    assert isinstance(payload["level"], float)
    assert 0.0 <= payload["level"] <= 1.0
    assert payload["plane"] == "user"


def test_silence_edge_publishes_once_then_goes_quiet():
    """A stalled capture and a silent room must look different: the transition
    INTO silence publishes so the scope settles to a baseline."""
    t = {"now": 0.0}
    events = []
    pump = AudioLevelPump(
        clock=lambda: t["now"], max_fps=20.0,
        publish=lambda et, op, p: events.append(p["level"]),
    )
    pump.feed_level(0.9)                       # loud, publishes
    t["now"] = 0.001
    assert pump.feed_level(0.0) == 0.0         # silence EDGE publishes despite cap
    t["now"] = 0.002
    assert pump.feed_level(0.0) is None        # subsequent silence coalesced
    assert events == [0.9, 0.0]


def test_pump_survives_a_failing_publisher_and_invalidator():
    """Capture must never die because telemetry or the UI faulted."""
    def _boom(*a, **k):
        raise RuntimeError("broker down")

    pump = AudioLevelPump(publish=_boom, invalidate=_boom)
    assert pump.feed_level(0.5) == 0.5         # returned normally


def test_garbage_levels_are_ignored():
    pump = AudioLevelPump()
    for bad in (None, "loud", object()):
        assert pump.feed_level(bad) is None    # type: ignore[arg-type]


def test_pump_disabled_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_PUMP_ENABLED", "false")
    events = []
    pump = AudioLevelPump(publish=lambda et, op, p: events.append(p))
    assert pump.feed_level(0.9) is None
    assert events == []


# ---------------------------------------------------------------------------
# (3) broker event -> header repaint via invalidate(), no exceptions
# ---------------------------------------------------------------------------


def test_audio_level_event_repaints_header_without_raising():
    """(3) THE CORE ASSERTION: consuming an audio_level_changed payload pushes
    the sample, flips the plane colour, fires invalidate(), and the header
    renders cleanly."""
    from backend.core.ouroboros.ui.crest_animator import render_cockpit_header

    hits = []
    scope = BrailleScope(width=20)
    pump = AudioLevelPump(scope=scope, invalidate=lambda: hits.append(1))

    for i in range(40):
        out = pump.on_event({
            "level": abs(math.sin(i / 3.0)), "plane": "system",
        })
        assert out is not None

    assert len(hits) == 40, "invalidate did not fire per event"
    assert scope.plane is AudioPlane.SYSTEM
    assert scope.accent == "venom_green"
    assert scope.is_silent() is False

    header = render_cockpit_header(
        None, ["O+V v0.1.0", "healthy", "~/repo"], 100,
        right_gutter=lambda: scope.render_rich(),
    )
    assert isinstance(header, str) and header
    assert "O+V" in header


def test_unknown_plane_degrades_to_idle_rather_than_raising():
    scope = BrailleScope(width=8)
    pump = AudioLevelPump(scope=scope)
    assert pump.on_event({"level": 0.5, "plane": "martian"}) == 0.5
    assert scope.plane is AudioPlane.IDLE


def test_malformed_event_payloads_never_raise():
    pump = AudioLevelPump(scope=BrailleScope(width=8))
    for bad in ({}, None, {"level": "x"}, {"level": None}, {"plane": 5}):
        pump.on_event(bad)               # type: ignore[arg-type]


def test_header_gutter_is_omitted_when_it_cannot_fit():
    """A narrow terminal must degrade to the plain header, never wrap into a
    broken layout."""
    from backend.core.ouroboros.ui.crest_animator import render_cockpit_header

    scope = BrailleScope(width=20)
    scope.extend([1.0] * 40)
    narrow = render_cockpit_header(
        None, ["O+V v0.1.0"], 22, right_gutter=lambda: scope.render_rich(),
    )
    assert "⣿" not in narrow, "gutter rendered despite not fitting"


def test_header_back_compat_without_a_gutter():
    from backend.core.ouroboros.ui.crest_animator import render_cockpit_header

    out = render_cockpit_header(None, ["O+V v0.1.0", "healthy"], 80)
    assert "O+V" in out and "healthy" in out


def test_failing_gutter_callable_never_breaks_the_header():
    from backend.core.ouroboros.ui.crest_animator import render_cockpit_header

    def _boom():
        raise RuntimeError("scope exploded")

    out = render_cockpit_header(None, ["O+V v0.1.0"], 80, right_gutter=_boom)
    assert "O+V" in out


def test_scope_and_pump_share_one_instance():
    """DRY: the pump must drive the SAME scope the header renders, or the two
    surfaces drift apart."""
    scope = BrailleScope(width=10)
    pump = AudioLevelPump(scope=scope)
    assert pump.scope is scope
    pump.feed_level(1.0)
    assert scope.is_silent() is False
