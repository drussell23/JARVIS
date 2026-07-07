"""Tests for the wake-sequence renderer (backend/core/ouroboros/ui/wake_sequence.py).

TDD: written before the implementation. Pins the spec 2026-07-06 §5 contract:

  * state reflects REAL phase transitions (in-flight -> done), coalesced
  * insertion order preserved; "live" only when every phase completes
  * debounce gate so a fast engine spin-up can't flood the terminal (#4)
  * themed frame with zero escape leakage at the NONE tier
"""
from __future__ import annotations

from backend.core.ouroboros.battle_test.boot_timing import BootTimer, PhaseRecord
from backend.core.ouroboros.ui import wake_sequence
from backend.core.ouroboros.ui.theme import ColorTier, build_console
from backend.core.ouroboros.ui.wake_sequence import WakeModel, WakeSequenceRenderer


def _active(name: str) -> PhaseRecord:
    return PhaseRecord(name=name, started_at=1.0, ended_at=0.0, parent="")


def _done(name: str) -> PhaseRecord:
    return PhaseRecord(name=name, started_at=1.0, ended_at=2.0, parent="")


class TestWakeModel:
    def test_begin_marks_phase_active(self) -> None:
        m = WakeModel()
        m.observe(_active("sensors"))
        assert ("sensors", True) in m.phases()

    def test_end_marks_phase_done(self) -> None:
        m = WakeModel()
        m.observe(_active("sensors"))
        m.observe(_done("sensors"))
        assert ("sensors", False) in m.phases()

    def test_coalesces_same_phase_to_single_entry(self) -> None:
        m = WakeModel()
        m.observe(_active("sensors"))
        m.observe(_done("sensors"))
        assert len(m.phases()) == 1

    def test_preserves_first_seen_order(self) -> None:
        m = WakeModel()
        for n in ("sensors", "loop", "venom"):
            m.observe(_active(n))
        assert [name for name, _ in m.phases()] == ["sensors", "loop", "venom"]

    def test_not_live_while_a_phase_is_active(self) -> None:
        m = WakeModel()
        m.observe(_active("sensors"))
        assert m.is_live is False

    def test_live_when_all_phases_done(self) -> None:
        m = WakeModel()
        m.observe(_active("sensors"))
        m.observe(_done("sensors"))
        assert m.is_live is True

    def test_not_live_when_empty(self) -> None:
        assert WakeModel().is_live is False

    def test_empty_name_ignored(self) -> None:
        m = WakeModel()
        m.observe(PhaseRecord(name="", started_at=1.0, ended_at=0.0, parent=""))
        assert m.phases() == []


class TestDebounce:
    def _renderer(self, interval: float) -> WakeSequenceRenderer:
        console = build_console(force_tier=ColorTier.NONE, force_terminal=True)
        return WakeSequenceRenderer(console, min_interval_s=interval)

    def test_first_flush_allowed_immediately(self) -> None:
        r = self._renderer(0.05)
        r.observe(_active("x"))
        assert r.should_flush(now=0.0) is True

    def test_flush_gated_within_interval(self) -> None:
        r = self._renderer(0.05)
        r.observe(_active("x"))
        r.flush(now=0.0)
        r.observe(_done("x"))
        assert r.should_flush(now=0.01) is False   # too soon
        assert r.should_flush(now=0.06) is True     # interval elapsed

    def test_no_flush_when_nothing_changed(self) -> None:
        r = self._renderer(0.0)
        r.observe(_active("x"))
        r.flush(now=0.0)
        assert r.should_flush(now=1.0) is False      # not dirty

    def test_rapid_observations_coalesced_into_one_render(self) -> None:
        """100 phase events while the clock is frozen -> a single render."""
        r = self._renderer(0.05)
        for i in range(100):
            r.observe(_active(f"p{i}"))
        assert r.flush(now=0.0) is True
        assert r.render_count == 1
        assert r.flush(now=0.001) is False           # gated + not dirty


class TestFrame:
    def _renderer(self):
        console = build_console(force_tier=ColorTier.NONE, force_terminal=True)
        return console, WakeSequenceRenderer(console)

    def test_frame_lists_every_phase(self) -> None:
        console, r = self._renderer()
        r.observe(_active("sensors"))
        r.observe(_done("loop"))
        with console.capture() as cap:
            console.print(r.render_frame())
        out = cap.get()
        assert "sensors" in out and "loop" in out

    def test_frame_no_escape_leakage_at_none_tier(self) -> None:
        console, r = self._renderer()
        r.observe(_done("venom"))
        with console.capture() as cap:
            console.print(r.render_frame())
        assert "\x1b[" not in cap.get()

    def test_frame_shows_live_when_complete(self) -> None:
        console, r = self._renderer()
        r.observe(_active("x"))
        r.observe(_done("x"))
        with console.capture() as cap:
            console.print(r.render_frame())
        assert "live" in cap.get().lower()

    def test_frame_shows_ouroboros_wordmark(self) -> None:
        console, r = self._renderer()
        r.observe(_active("x"))
        with console.capture() as cap:
            console.print(r.render_frame())
        assert "ouroboros" in cap.get().lower()


class TestAttach:
    def test_attach_subscribes_to_bootTimer(self) -> None:
        timer = BootTimer()
        console = build_console(force_tier=ColorTier.NONE)
        r = WakeSequenceRenderer(console)
        r.attach(timer)

        timer.begin("sensors")

        assert ("sensors", True) in r.model.phases()

    def test_attach_reflects_completion(self) -> None:
        timer = BootTimer()
        console = build_console(force_tier=ColorTier.NONE)
        r = WakeSequenceRenderer(console)
        r.attach(timer)

        timer.begin("sensors")
        timer.end("sensors")

        assert ("sensors", False) in r.model.phases()
        assert r.model.is_live is True
