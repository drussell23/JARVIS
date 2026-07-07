"""Tests for the BootTimer observer hook (wake-sequence enabler).

TDD: written before the implementation. The wake sequence subscribes to real
phase transitions via ``add_observer`` so the boot animation reflects true
system readiness, never a scripted timer (spec 2026-07-06 §5.1).
"""
from __future__ import annotations

from backend.core.ouroboros.battle_test.boot_timing import BootTimer, PhaseRecord


class TestBootTimerObserver:
    def test_observer_notified_in_flight_on_begin(self) -> None:
        timer = BootTimer()
        seen: list[tuple] = []
        timer.add_observer(lambda rec: seen.append((rec.name, rec.is_in_flight)))

        timer.begin("harness_boot")

        assert ("harness_boot", True) in seen

    def test_observer_notified_completed_on_end(self) -> None:
        timer = BootTimer()
        seen: list[tuple] = []
        timer.add_observer(lambda rec: seen.append((rec.name, rec.is_in_flight)))

        timer.begin("harness_boot")
        timer.end("harness_boot")

        assert ("harness_boot", False) in seen

    def test_observer_notified_on_mark(self) -> None:
        timer = BootTimer()
        seen: list[str] = []
        timer.add_observer(lambda rec: seen.append(rec.name))

        timer.mark("repl_prompt_rendered")

        assert "repl_prompt_rendered" in seen

    def test_observer_receives_phase_records(self) -> None:
        timer = BootTimer()
        seen: list[PhaseRecord] = []
        timer.add_observer(seen.append)

        timer.begin("x")
        timer.end("x")

        assert all(isinstance(r, PhaseRecord) for r in seen)

    def test_raising_observer_never_breaks_timing(self) -> None:
        """A broken observer must not propagate into the boot hot path."""
        timer = BootTimer()

        def boom(_rec: PhaseRecord) -> None:
            raise RuntimeError("observer exploded")

        timer.add_observer(boom)

        # Must not raise despite the exploding observer.
        timer.begin("phase")
        timer.end("phase")
        timer.mark("milestone")

        # And timing was still recorded faithfully.
        names = {r.name for r in timer.records()}
        assert {"phase", "milestone"} <= names

    def test_multiple_observers_all_notified(self) -> None:
        timer = BootTimer()
        a: list[str] = []
        b: list[str] = []
        timer.add_observer(lambda rec: a.append(rec.name))
        timer.add_observer(lambda rec: b.append(rec.name))

        timer.mark("boot")

        assert a == ["boot"] and b == ["boot"]
