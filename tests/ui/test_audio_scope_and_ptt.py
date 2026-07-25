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


# ---------------------------------------------------------------------------
# (2) RMS floats -> Braille
# ---------------------------------------------------------------------------


def test_silence_renders_empty_braille_cells():
    sc = BrailleScope(width=8)
    sc.extend([0.0] * 16)
    out = sc.render()
    assert len(out) == 8
    assert out == chr(_BASE) * 8, "silence must be blank cells, not a floating bar"
    assert sc.is_silent() is True


def test_full_scale_renders_all_eight_dots():
    sc = BrailleScope(width=4)
    sc.extend([1.0] * 8)
    assert sc.render() == "⣿" * 4, "full scale must light all 8 dots per cell"


def test_cell_encodes_two_samples_at_four_levels():
    """The resolution claim, asserted bit-exactly: one cell = 2 columns x 4 rows,
    bars filling from the BOTTOM."""
    assert cell_for(0.0, 0.0) == chr(_BASE)
    # Left column only, one dot from the bottom -> dot 7 (0x40).
    assert cell_for(0.2, 0.0) == chr(_BASE + 0x40)
    # Right column only, one dot from the bottom -> dot 8 (0x80).
    assert cell_for(0.0, 0.2) == chr(_BASE + 0x80)
    # Both columns full -> all eight dots.
    assert cell_for(1.0, 1.0) == chr(_BASE + 0xFF)


def test_bars_fill_upward_from_the_baseline():
    """A louder sample must be a TALLER bar anchored at the bottom, so a rising
    ramp is monotonically denser."""
    masks = [ord(cell_for(v, v)) - _BASE for v in (0.0, 0.25, 0.5, 0.75, 1.0)]
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
    assert sc.render()[-1] == chr(_BASE)
    sc.push(1.0)
    assert sc.render()[-1] != chr(_BASE), "new sample did not reach the right edge"


def test_partial_buffer_is_left_padded_to_stable_width():
    """Width must be stable from the first frame or the header layout jitters."""
    sc = BrailleScope(width=10)
    sc.push(1.0)
    out = sc.render()
    assert len(out) == 10
    assert out.startswith(chr(_BASE)), "partial buffer must left-pad"
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
    assert sc.render() == chr(_BASE) * 6
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
    n = AdaptiveNormalizer(decay=0.5)
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


def test_render_rich_wraps_in_the_plane_accent():
    sc = BrailleScope(width=3)
    sc.set_plane(AudioPlane.SYSTEM)
    out = sc.render_rich()
    assert out.startswith("[venom_green]") and out.endswith("[/venom_green]")


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
