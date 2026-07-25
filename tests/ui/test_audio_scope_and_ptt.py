"""Braille oscilloscope + PTT spacebar intercept.

Three mandated assertions, plus the edge cases that would actually bite in a
live cockpit:

  (1) an EMPTY prompt_toolkit buffer intercepts space without touching the text
      buffer — and a NON-empty buffer must still type a normal space;
  (2) a stream of mock RMS floats maps to the expected Braille string;
  (3) the ring buffer wraps without index errors, at any width.

Real prompt_toolkit objects are used for (1) — a hand-rolled fake buffer would
prove nothing about how prompt_toolkit actually routes a key through a filter.
"""

from __future__ import annotations

import math
import time

import pytest

from backend.core.ouroboros.ui.audio_scope import (
    LEVELS,
    SAMPLES_PER_CELL,
    AdaptiveNormalizer,
    AudioPlane,
    BrailleScope,
    cell_for,
    rms,
)
from backend.core.ouroboros.ui.ptt_router import (
    MicState,
    PTTLatch,
    build_ptt_key_bindings,
    on_release_supported,
)

_BASE = 0x2800
#: A silent sub-column still draws its bottom dot, so quiet renders as a
#: visible flat line. U+2800 is BLANK — a scope made of it is invisible,
#: which is indistinguishable from the feature not being installed.
_BASELINE = chr(_BASE + 0x40 + 0x80)   # dots 7+8, both bottom dots


# ---------------------------------------------------------------------------
# (2) RMS floats -> Braille
# ---------------------------------------------------------------------------


def test_silence_renders_empty_braille_cells():
    sc = BrailleScope(width=8)
    sc.extend([0.0] * 16)
    out = sc.render()
    assert len(out) == 8
    assert out == _BASELINE * 8, "silence must be a visible flat line"
    assert sc.is_silent() is True


def test_full_scale_renders_all_eight_dots():
    sc = BrailleScope(width=4)
    sc.extend([1.0] * 8)
    assert sc.render() == "⣿" * 4, "full scale must light all 8 dots per cell"


def test_cell_encodes_two_samples_at_four_levels():
    """The resolution claim, asserted bit-exactly: one cell = 2 columns x 4 rows,
    bars filling from the BOTTOM."""
    assert cell_for(0.0, 0.0, baseline=False) == chr(_BASE)
    assert cell_for(0.0, 0.0) == _BASELINE          # baseline on by default
    # Left column only, one dot from the bottom -> dot 7 (0x40).
    assert cell_for(0.2, 0.0, baseline=False) == chr(_BASE + 0x40)
    # Right column only, one dot from the bottom -> dot 8 (0x80).
    assert cell_for(0.0, 0.2, baseline=False) == chr(_BASE + 0x80)
    # Both columns full -> all eight dots.
    assert cell_for(1.0, 1.0) == chr(_BASE + 0xFF)   # unchanged: full scale


def test_bars_fill_upward_from_the_baseline():
    """A louder sample must be a TALLER bar anchored at the bottom, so a rising
    ramp is monotonically denser."""
    masks = [ord(cell_for(v, v, baseline=False)) - _BASE
             for v in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert masks == sorted(masks), f"not monotonic: {masks}"
    assert masks[0] == 0 and masks[-1] == 0xFF


def test_known_rms_stream_maps_to_expected_string():
    """(2) A deterministic ramp produces a deterministic, reproducible scope."""
    sc = BrailleScope(width=4)
    sc.extend([0.0, 0.25, 0.5, 0.75, 1.0, 1.0, 0.5, 0.0])
    expected = "".join((
        cell_for(0.0, 0.25),
        cell_for(0.5, 0.75),
        cell_for(1.0, 1.0),
        cell_for(0.5, 0.0),
    ))
    assert sc.render() == expected


def test_newest_sample_lands_at_the_right_edge():
    """Right-to-left scroll: the freshest data is always rightmost."""
    sc = BrailleScope(width=3)
    sc.extend([0.0] * 6)
    assert sc.render()[-1] == _BASELINE
    sc.push(1.0)
    assert sc.render()[-1] != _BASELINE, "new sample did not reach the right edge"


def test_partial_buffer_is_left_padded_to_stable_width():
    """Width must be stable from the first frame or the header layout jitters."""
    sc = BrailleScope(width=10)
    sc.push(1.0)
    out = sc.render()
    assert len(out) == 10
    assert out.startswith(_BASELINE), "partial buffer must left-pad with baseline"
    assert out[-1] != chr(_BASE)


# ---------------------------------------------------------------------------
# (3) ring buffer wrap — no index errors
# ---------------------------------------------------------------------------


def test_ring_wrap_never_raises_and_holds_width():
    sc = BrailleScope(width=20)
    for i in range(5000):                      # far past capacity
        sc.push(abs(math.sin(i / 3.0)))
        out = sc.render()
        assert len(out) == 20
    assert len(sc.render()) == 20


@pytest.mark.parametrize("width", [1, 2, 3, 7, 20, 64])
def test_wrap_is_safe_at_every_width(width):
    sc = BrailleScope(width=width)
    for i in range(width * SAMPLES_PER_CELL * 3 + 5):
        sc.push((i % 5) / 4.0)
        assert len(sc.render()) == width


def test_odd_sample_count_does_not_index_past_the_end():
    """An odd buffer length must not read a nonexistent right-hand sample."""
    sc = BrailleScope(width=5)
    sc.extend([0.5] * 9)                       # odd
    assert len(sc.render()) == 5


def test_clear_resets_to_silence():
    sc = BrailleScope(width=6)
    sc.extend([1.0] * 12)
    sc.clear()
    assert sc.render() == _BASELINE * 6
    assert sc.is_silent() is True


def test_out_of_range_and_garbage_samples_are_clamped():
    sc = BrailleScope(width=4)
    for bad in (-5.0, 42.0, float("inf"), None, "loud", float("nan")):
        sc.push(bad)                            # type: ignore[arg-type]
    out = sc.render()
    assert len(out) == 4
    for ch in out:
        assert _BASE <= ord(ch) <= _BASE + 0xFF, "emitted a non-Braille codepoint"


# ---------------------------------------------------------------------------
# RMS + adaptive normalization
# ---------------------------------------------------------------------------


def test_rms_matches_the_closed_form():
    assert rms([]) == 0.0
    assert rms([0.0, 0.0]) == 0.0
    assert rms([1.0, -1.0]) == pytest.approx(1.0)
    assert rms([0.5, -0.5]) == pytest.approx(0.5)
    assert rms([3.0, 4.0]) == pytest.approx(math.sqrt(12.5))


def test_rms_never_raises_on_garbage():
    assert rms(None) == 0.0                     # type: ignore[arg-type]
    assert rms(["x", None, 1.0]) >= 0.0


def test_normalizer_uses_full_range_for_quiet_and_loud_sources():
    """A fixed divisor would flatline a quiet mic or clip a hot one; the
    decaying peak must give BOTH sources full deflection."""
    quiet = AdaptiveNormalizer()
    assert quiet.normalize(0.004) == pytest.approx(1.0)

    loud = AdaptiveNormalizer()
    assert loud.normalize(0.95) == pytest.approx(1.0)


def test_normalizer_peak_decays_so_a_quieter_source_recovers():
    """Squelch OFF on purpose: this isolates the PEAK-DECAY mechanism. With the
    squelch armed a sustained 0.01 is correctly identified as room tone and
    clamped to zero, so the peak would never see it — a true behaviour, but a
    different one, covered by the squelch tests below."""
    n = AdaptiveNormalizer(decay=0.5, squelch=False)
    n.normalize(1.0)
    for _ in range(40):
        n.normalize(0.01)
    assert n.normalize(0.01) > 0.5, "peak never decayed — quiet source stays flat"


def test_normalizer_floor_prevents_amplifying_room_noise():
    n = AdaptiveNormalizer(floor=0.1)
    assert n.normalize(0.0001) < 0.01


# ---------------------------------------------------------------------------
# plane-reactive colour
# ---------------------------------------------------------------------------


def test_plane_colours_are_semantic_names_not_hex():
    sc = BrailleScope(width=4)
    assert sc.accent == "muted"
    sc.set_plane(AudioPlane.USER)
    assert sc.accent == "cyan"
    sc.set_plane(AudioPlane.SYSTEM)
    assert sc.accent == "venom_green"
    # Never a raw colour — the theme owns the palette.
    assert not sc.accent.startswith("#")


def test_set_plane_reports_only_real_transitions():
    """Zero-flicker: the caller must invalidate only when something changed."""
    sc = BrailleScope(width=4)
    assert sc.set_plane(AudioPlane.USER) is True
    assert sc.set_plane(AudioPlane.USER) is False


def test_render_rich_resolves_the_accent_through_the_theme():
    """This assertion USED to check for the literal "[venom_green]" markup —
    and that is precisely how the bug survived. Rich does not know that name;
    an unknown style is dropped SILENTLY, so the scope rendered with no colour
    at all while this test stayed green. A PTY integration test caught it by
    reading the terminal bytes.

    The contract is now: whatever markup is emitted must be a style Rich can
    actually resolve for the active tier — or none at all on a NONE tier,
    which is the honest answer for a non-colour terminal."""
    from backend.core.ouroboros.ui.theme import semantic

    sc = BrailleScope(width=3)
    sc.set_plane(AudioPlane.SYSTEM)
    out = sc.render_rich()

    style = semantic("venom_green")
    if style:
        assert out.startswith(f"[{style}]") and out.endswith(f"[/{style}]")
        assert "[venom_green]" not in out, "emitted a name Rich cannot resolve"
    else:
        # NONE tier: bare glyphs, no markup at all.
        assert "[" not in out


def test_render_rich_never_emits_an_unresolvable_style_name():
    """Guard the whole class, not just venom_green: no plane may emit a raw
    semantic name into markup."""
    for plane in (AudioPlane.IDLE, AudioPlane.USER, AudioPlane.SYSTEM):
        sc = BrailleScope(width=3)
        sc.set_plane(plane)
        out = sc.render_rich()
        for raw in ("venom_green", "cyan", "muted"):
            assert f"[{raw}]" not in out, f"{plane.value} emitted raw name {raw}"


# ---------------------------------------------------------------------------
# (1) spacebar intercept — REAL prompt_toolkit objects
# ---------------------------------------------------------------------------


def _press_space(kb, buffer):
    """Route a space through the binding exactly as prompt_toolkit would:
    resolve the handler, honour its filter, else insert the character."""
    from prompt_toolkit.keys import Keys  # noqa: F401

    class _Ev:
        current_buffer = buffer

    for binding in kb.bindings:
        if " " in [str(k) for k in binding.keys]:
            if binding.filter():
                binding.handler(_Ev())
                return True
            break
    buffer.insert_text(" ")
    return False


def test_empty_buffer_intercepts_space_without_modifying_text():
    """(1) THE CORE ASSERTION: on an empty buffer, space arms the mic and the
    text buffer stays untouched."""
    from prompt_toolkit.buffer import Buffer

    buf = Buffer()
    latch = PTTLatch()
    kb = build_ptt_key_bindings(latch, buffer_getter=lambda: buf.text)

    assert buf.text == ""
    intercepted = _press_space(kb, buf)

    assert intercepted is True, "space was not intercepted on an empty buffer"
    assert buf.text == "", "intercept leaked a space into the buffer"
    assert latch.state is MicState.OPEN
    assert latch.open_count == 1


def test_non_empty_buffer_types_a_normal_space():
    """The other half — swallowing word separators mid-sentence is intolerable."""
    from prompt_toolkit.buffer import Buffer

    buf = Buffer()
    buf.insert_text("deploy")
    latch = PTTLatch()
    kb = build_ptt_key_bindings(latch, buffer_getter=lambda: buf.text)

    intercepted = _press_space(kb, buf)

    assert intercepted is False, "space was hijacked while the buffer had text"
    assert buf.text == "deploy ", "normal space insertion was lost"
    assert latch.state is MicState.CLOSED


def test_whitespace_only_buffer_still_counts_as_empty():
    from prompt_toolkit.buffer import Buffer

    buf = Buffer()
    buf.insert_text("   ")
    latch = PTTLatch()
    kb = build_ptt_key_bindings(latch, buffer_getter=lambda: buf.text)
    assert _press_space(kb, buf) is True
    assert latch.is_open is True


def test_second_space_closes_the_latch():
    """Toggle is the reliable closing edge — terminals give no key-release."""
    from prompt_toolkit.buffer import Buffer

    buf = Buffer()
    latch = PTTLatch()
    kb = build_ptt_key_bindings(latch, buffer_getter=lambda: buf.text)

    _press_space(kb, buf)
    assert latch.is_open is True
    _press_space(kb, buf)
    assert latch.state is MicState.CLOSED
    assert latch.close_reasons == ("toggle",)
    assert buf.text == "", "toggling must never write to the buffer"


def test_intercept_triggers_the_invalidate_hook():
    """Zero-flicker redraw: the same invalidate discipline as the theme."""
    from prompt_toolkit.buffer import Buffer

    buf = Buffer()
    hits = []
    kb = build_ptt_key_bindings(
        PTTLatch(), buffer_getter=lambda: buf.text, invalidate=lambda: hits.append(1),
    )
    _press_space(kb, buf)
    assert hits == [1]


def test_disabled_ptt_leaves_space_alone(monkeypatch):
    from prompt_toolkit.buffer import Buffer

    monkeypatch.setenv("JARVIS_PTT_ENABLED", "false")
    buf = Buffer()
    latch = PTTLatch()
    kb = build_ptt_key_bindings(latch, buffer_getter=lambda: buf.text)

    assert _press_space(kb, buf) is False
    assert buf.text == " "
    assert latch.state is MicState.CLOSED


# ---------------------------------------------------------------------------
# latch semantics
# ---------------------------------------------------------------------------


def test_open_is_idempotent_under_key_repeat():
    """Terminal auto-repeat must not emit a burst of mic_active intents."""
    opens = []
    latch = PTTLatch(on_open=lambda: opens.append(1))
    assert latch.open() is True
    for _ in range(20):
        assert latch.open() is False
    assert opens == [1]


def test_close_is_idempotent():
    closes = []
    latch = PTTLatch(on_close=lambda r: closes.append(r))
    latch.open()
    assert latch.close("toggle") is True
    assert latch.close("toggle") is False
    assert closes == ["toggle"]


def test_silence_auto_flush_substitutes_for_key_release():
    """The honest stand-in for an unobservable release edge."""
    t = {"now": 0.0}
    closes = []
    latch = PTTLatch(on_close=lambda r: closes.append(r), clock=lambda: t["now"])
    latch.open()

    t["now"] = 0.5
    assert latch.note_level(0.9) is False, "voice must hold the latch open"
    t["now"] = 1.0
    assert latch.note_level(0.0) is False, "brief pause must not flush"
    t["now"] = 2.5
    assert latch.note_level(0.0) is True, "sustained silence must flush"
    assert latch.state is MicState.CLOSED
    assert closes == ["silence"]


def test_levels_ignored_while_closed():
    """A quiet idle cockpit must never emit spurious flushes."""
    latch = PTTLatch()
    for _ in range(50):
        assert latch.note_level(0.0) is False
    assert latch.state is MicState.CLOSED


def test_consumer_exception_never_wedges_the_latch():
    def _boom():
        raise RuntimeError("audio backend died")

    latch = PTTLatch(on_open=_boom, on_close=lambda r: (_ for _ in ()).throw(OSError()))
    assert latch.open() is True and latch.is_open is True
    assert latch.close("toggle") is True
    assert latch.state is MicState.CLOSED


def test_key_release_limitation_is_declared_in_code():
    """The constraint must be discoverable as a predicate, not folklore."""
    assert on_release_supported() is False


def test_broker_event_types_are_registered():
    """Unregistered types are silently dropped by publish() — a listener on an
    unregistered name is dead wiring."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    assert "audio_level_changed" in _VALID_EVENT_TYPES
    assert "mic_state_changed" in _VALID_EVENT_TYPES


def test_idle_scope_is_visible_not_blank():
    """THE BUG A LIVE COCKPIT EXPOSED: U+2800 is the BLANK braille pattern, so
    a silent scope rendered as pure whitespace — the operator saw nothing and
    could not tell it from an uninstalled feature. A real oscilloscope shows a
    flat line at rest."""
    sc = BrailleScope(width=20)
    out = sc.render()
    assert all(ord(c) != _BASE for c in out), "idle scope is invisible"
    assert out == _BASELINE * 20
    assert sc.is_silent() is True, "baseline must not be mistaken for signal"


def test_baseline_is_below_every_signal_level():
    """The flat line must sit UNDER the trace, never overlap it — otherwise a
    quiet passage would look louder than it is."""
    quiet = ord(cell_for(0.0, 0.0)) - _BASE
    loud = ord(cell_for(1.0, 1.0)) - _BASE
    assert quiet & loud == quiet, "baseline dots are not a subset of full scale"


def test_ascii_fallback_also_shows_a_baseline(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_SCOPE_ASCII", "true")
    sc = BrailleScope(width=6)
    out = sc.render()
    assert out.strip(), "ASCII fallback rendered invisible whitespace"
    assert out == "_" * 6


# ---------------------------------------------------------------------------
# dynamic noise-floor squelch
# ---------------------------------------------------------------------------


def _noise(n, lo, hi, seed=11):
    import random
    r = random.Random(seed)
    return [r.uniform(lo, hi) for _ in range(n)]


def test_ambient_noise_clamps_to_absolute_zero_after_the_window_adapts():
    """(1) A live mic never reaches digital zero — fans and HVAC keep RMS
    hovering, which renders as a permanently twitching baseline. Once the
    profile is learned, ambient must clamp to a HARD 0.0."""
    n = AdaptiveNormalizer()
    out = [n.normalize(v) for v in _noise(200, 0.002, 0.006)]

    assert all(v == 0.0 for v in out[-100:]), (
        f"ambient leaked through: max={max(out[-100:]):.4f}"
    )
    assert n.squelch_ready is True


def test_speech_bypasses_the_clamp_and_registers_high():
    """(2) The gate must open instantly for real signal — a meter that swallows
    the first word is worse than no meter."""
    n = AdaptiveNormalizer()
    for v in _noise(120, 0.002, 0.006):
        n.normalize(v)

    loud = n.normalize(0.7)
    assert loud > 0.5, f"speech was squelched (got {loud})"


def test_squelch_adapts_across_wildly_different_rooms():
    """The reason a constant cannot work: the correct gate in a quiet study is
    20x too low for a cafe. Both must end up silent."""
    for lo, hi in ((0.002, 0.006), (0.02, 0.05), (0.06, 0.10)):
        n = AdaptiveNormalizer()
        out = [n.normalize(v) for v in _noise(200, lo, hi)]
        assert all(v == 0.0 for v in out[-80:]), (
            f"room {lo}-{hi} still jittering: max={max(out[-80:]):.4f}"
        )
        assert n.normalize(0.7) > 0.5, f"speech blocked in room {lo}-{hi}"


def test_gate_spans_the_ambient_distribution_not_just_its_trough():
    """A gate pinned to the MINIMUM is sailed over by the noise's own peaks —
    the first implementation failed exactly here in a quiet room."""
    n = AdaptiveNormalizer()
    for v in _noise(200, 0.002, 0.006):
        n.normalize(v)
    assert n.noise_gate > 0.006, (
        f"gate {n.noise_gate:.4f} sits below the ambient ceiling 0.006"
    )
    assert n.noise_floor < 0.006, "floor should track the trough, not the peak"


def test_speech_does_not_drag_the_gate_up():
    """Speech must not widen the profile, or a talker would progressively
    squelch themselves."""
    n = AdaptiveNormalizer()
    for v in _noise(150, 0.02, 0.05):
        n.normalize(v)
    before = n.noise_gate
    for _ in range(30):
        n.normalize(0.8)
    after = n.noise_gate
    assert after < before * 1.5, f"gate inflated under speech: {before:.4f}->{after:.4f}"
    assert n.normalize(0.8) > 0.5, "speaker squelched themselves"


def test_no_clamping_before_the_profile_exists():
    """One sample cannot characterise a room. Clamping during warmup would
    swallow the first words after launch."""
    n = AdaptiveNormalizer(squelch_warmup_frames=20)
    assert n.squelch_ready is False
    assert n.normalize(0.5) > 0.0, "clamped before learning the room"


def test_squelch_can_be_disabled():
    n = AdaptiveNormalizer(squelch=False)
    out = [n.normalize(v) for v in _noise(200, 0.002, 0.006)]
    assert any(v > 0.0 for v in out[-50:]), "squelch ran while disabled"


def test_a_quieter_room_is_adopted_quickly():
    """Down-fast: moving somewhere quieter must not leave a stale high gate
    that swallows speech."""
    n = AdaptiveNormalizer()
    for v in _noise(150, 0.06, 0.10):
        n.normalize(v)
    loud_gate = n.noise_gate
    for v in _noise(300, 0.001, 0.003, seed=5):
        n.normalize(v)
    assert n.noise_gate < loud_gate, "gate never fell after the room quietened"


def test_squelch_is_allocation_free_scalar_math():
    """Structural: the profiler must stay two float updates. A deque scan per
    frame would put real work on the path that feeds the STT thread."""
    import inspect

    src = inspect.getsource(AdaptiveNormalizer._update_noise_floor)
    # Strip the docstring first: it *describes* the costs being avoided, and a
    # naive substring scan matched its own prose rather than the code.
    body = src.split('"""')[-1]
    for banned in ("deque", "sorted", "np.", "numpy", "for ", "while "):
        assert banned not in body, f"profiler is doing {banned!r} work per frame"


def test_squelched_output_renders_a_stable_baseline():
    """End-to-end into the visual: a noisy room must draw the flat line, not a
    twitching trace."""
    sc = BrailleScope(width=20)
    n = AdaptiveNormalizer()
    for v in _noise(40, 0.002, 0.006):
        n.normalize(v)
    for v in _noise(40, 0.002, 0.006, seed=3):
        sc.push(n.normalize(v))
    out = sc.render()
    assert len(set(out)) == 1, f"baseline is jittering: {out}"
    assert sc.is_silent() is True


# ===========================================================================
# Kinetic Decay Interpolator — the wave must FALL when telemetry starves
# ===========================================================================
#
# The failure this defends against: heavy STT inference spikes the CPU, the
# telemetry stream stalls mid-utterance, and the scope keeps painting the last
# frame it received. The operator sees a full-height trace and reads it as
# "loud right now". A monitor that freezes on its last reading is worse than a
# blank one, because it is confidently wrong.
#
# Every test drives an INJECTED clock. Using wall time would make these both
# slow and flaky, and would test the machine's scheduler rather than the decay.


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += float(dt)
        return self.t


def _loud_scope(width: int = 3, **kw):
    """A scope pinned at full deflection, with an injected clock."""
    clk = _Clock()
    sc = BrailleScope(width=width, clock=clk, **kw)
    sc.extend([0.9] * (width * SAMPLES_PER_CELL))
    return sc, clk


# --- (1) the mandated pair --------------------------------------------------


def test_high_amplitude_frame_renders_at_full_deflection():
    """(1) A 0.9 frame must render loud. Without this control, a decay test
    passes trivially on a scope that never drew anything."""
    sc, _clk = _loud_scope(width=3)
    assert sc.render() == "⣿⣿⣿"


async def test_starved_stream_decays_to_the_squelched_baseline():
    """(2) THE REGRESSION. Advance the clock 200ms with no new frames — the
    trace must have fallen to the flat baseline, not held its spike."""
    sc, clk = _loud_scope(width=3)
    assert sc.render() == "⣿⣿⣿"

    clk.advance(0.200)
    sc.tick()

    assert sc.render() == "⣀⣀⣀", "the wave froze instead of falling"


# --- gravity mechanics ------------------------------------------------------


def test_no_decay_inside_the_starvation_window():
    """A frame arriving on schedule must not be dragged down. Decaying between
    normal 50ms frames would flatten live audio into a permanent murmur."""
    sc, clk = _loud_scope(width=3)
    clk.advance(0.03)                     # < starve_after
    assert sc.tick() is False
    assert sc.render() == "⣿⣿⣿"


def test_decay_is_monotonic_and_never_rebounds():
    """Gravity only ever pulls down. A rebound would render as phantom sound."""
    sc, clk = _loud_scope(width=4)
    seen = []
    for _ in range(12):
        clk.advance(0.02)
        sc.tick()
        seen.append(max(sc.samples()) if sc.samples() else 0.0)
    assert all(b <= a + 1e-12 for a, b in zip(seen, seen[1:])), seen


def test_decay_reaches_exact_zero_not_an_asymptote():
    """Exponential decay never mathematically reaches 0. Without the snap the
    ring would hold residue forever and is_silent would answer False for a
    scope that has been visually flat for minutes."""
    sc, clk = _loud_scope(width=4)
    clk.advance(2.0)
    sc.tick()
    assert sc.samples() == [0.0] * len(sc.samples())
    assert sc.is_silent() is True


def test_decay_is_frame_rate_independent():
    """The same elapsed time must produce the same amplitude whether the UI
    repainted once or fifty times. A per-tick constant would fall faster on an
    idle machine than a busy one — i.e. fastest exactly when it is least
    needed."""
    coarse, c1 = _loud_scope(width=3)
    c1.advance(0.30)
    coarse.tick()

    fine, c2 = _loud_scope(width=3)
    for _ in range(30):
        c2.advance(0.01)
        fine.tick()

    assert max(coarse.samples()) == pytest.approx(max(fine.samples()), abs=1e-9)


def test_the_whole_ring_falls_together_not_just_new_samples():
    """A spike that merely scrolled off would still be painted at full height
    for the width of the buffer. Every column descends."""
    sc, clk = _loud_scope(width=6)
    clk.advance(0.12)
    sc.tick()
    vals = sc.samples()
    assert all(v < 0.9 for v in vals), vals
    assert len(set(round(v, 9) for v in vals)) == 1, "columns fell unevenly"


def test_a_new_frame_rearms_the_stream_and_stops_gravity():
    """Recovery: when telemetry resumes, the trace must snap back to what is
    actually being heard rather than continuing to sink."""
    sc, clk = _loud_scope(width=3)
    clk.advance(0.15)
    sc.tick()
    assert sc.render() != "⣿⣿⣿"

    sc.extend([1.0] * 6)                  # stream recovers
    assert sc.tick() is False, "kept decaying through live telemetry"
    assert sc.render() == "⣿⣿⣿"


def test_starved_predicate_tracks_the_stream_not_the_samples():
    sc, clk = _loud_scope(width=3)
    assert sc.starved is False
    clk.advance(0.5)
    assert sc.starved is True
    sc.push(0.4)
    assert sc.starved is False


# --- isolation --------------------------------------------------------------


def test_gravity_never_touches_the_daemon_side_normalizer():
    """Kinetic decay is a CLIENT-side visual affordance. If it moved the
    adaptive peak, a starved stream would silently rescale the meter and the
    next real frame would render at the wrong height — corrupting measurement
    to fix a repaint."""
    norm = AdaptiveNormalizer()
    sc, clk = _loud_scope(width=3, normalizer=norm)
    before = norm.peak
    clk.advance(1.0)
    sc.tick()
    assert norm.peak == before


def test_tick_on_an_empty_scope_is_a_no_op():
    sc = BrailleScope(width=3, clock=_Clock())
    assert sc.tick() is False
    assert sc.render() == "⣀⣀⣀"


def test_tick_never_raises_on_a_hostile_clock():
    """A clock that explodes must cost a frame of animation, never the cockpit."""
    def _boom() -> float:
        raise RuntimeError("monotonic went backwards")

    sc = BrailleScope(width=3)
    sc.extend([0.8] * 6)
    sc._clock = _boom                     # noqa: SLF001 — fault injection
    assert sc.tick() is False


def test_clear_rearms_the_starvation_clock():
    """A cleared scope has no stale amplitude to fall from; it must not report
    itself starved from the moment of the clear."""
    sc, clk = _loud_scope(width=3)
    clk.advance(5.0)
    assert sc.starved is True
    sc.clear()
    assert sc.starved is False


def test_concurrent_ticks_and_pushes_do_not_corrupt_the_ring():
    """The render thread ticks while the audio thread pushes. Both mutate the
    deque, so both must hold the lock."""
    import threading as _t

    sc = BrailleScope(width=8)
    stop = _t.Event()

    def _push():
        while not stop.is_set():
            sc.push(0.5)

    def _tick():
        while not stop.is_set():
            sc.tick()

    threads = [_t.Thread(target=_push), _t.Thread(target=_tick)]
    for th in threads:
        th.daemon = True
        th.start()
    time.sleep(0.15)
    stop.set()
    for th in threads:
        th.join(timeout=2.0)

    vals = sc.samples()
    assert len(vals) <= 16
    assert all(0.0 <= v <= 1.0 for v in vals)


# --- tunability -------------------------------------------------------------


def test_gravity_timings_are_environment_tunable(monkeypatch):
    """None of these constants are taste-free: a laggy SSH session wants a
    slower fall than a local 60Hz repaint."""
    monkeypatch.setenv("JARVIS_AUDIO_SCOPE_STARVE_S", "0.5")
    monkeypatch.setenv("JARVIS_AUDIO_SCOPE_GRAVITY_TAU_S", "1.5")
    sc = BrailleScope(width=3, clock=_Clock())
    assert sc._starve_after == pytest.approx(0.5)    # noqa: SLF001
    assert sc._gravity_tau == pytest.approx(1.5)     # noqa: SLF001


def test_garbage_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_SCOPE_GRAVITY_TAU_S", "molasses")
    sc = BrailleScope(width=3, clock=_Clock())
    assert sc._gravity_tau > 0.0                      # noqa: SLF001


def test_the_render_loop_ticks_before_it_paints():
    """Structural pin. tick() is only reachable from the UI repaint seam; if a
    refactor drops that call the decay becomes dead code that every unit test
    here still passes."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/cli/ov.py",
    ).read_text(encoding="utf-8", errors="replace")
    gut = src[src.index("def _gutter():"):][:900]
    assert ".tick()" in gut, "the header no longer applies gravity"
    assert gut.index(".tick()") < gut.index("render_rich()"), (
        "gravity applied AFTER the paint — one frame stale, every frame"
    )
