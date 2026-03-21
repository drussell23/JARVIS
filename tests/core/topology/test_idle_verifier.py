"""Tests for Little's Law idle verifier and ProactiveDrive state machine."""
import time

import pytest

from backend.core.topology.idle_verifier import (
    LittlesLawVerifier,
    ProactiveDrive,
    QueueSample,
)


class TestQueueSample:
    def test_fields(self):
        s = QueueSample(timestamp=1.0, depth=5, processing_latency_ms=100.0)
        assert s.depth == 5
        assert s.processing_latency_ms == 100.0


class TestLittlesLawVerifier:
    def test_insufficient_samples_returns_none(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        assert v.compute_L() is None

    def test_insufficient_samples_not_idle(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        idle, reason = v.is_idle()
        assert idle is False
        assert "insufficient" in reason

    def test_idle_with_low_load(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        now = time.monotonic()
        for i in range(15):
            v._samples.append(QueueSample(
                timestamp=now + i * 1.0,
                depth=2,
                processing_latency_ms=10.0,
            ))
        L = v.compute_L()
        assert L is not None
        assert L < 0.30 * 100
        idle, reason = v.is_idle()
        assert idle is True

    def test_busy_with_high_load(self):
        v = LittlesLawVerifier("prime", max_queue_depth=100)
        now = time.monotonic()
        for i in range(15):
            v._samples.append(QueueSample(
                timestamp=now + i * 0.1,
                depth=80,
                processing_latency_ms=5000.0,
            ))
        L = v.compute_L()
        assert L is not None
        assert L >= 0.30 * 100
        idle, reason = v.is_idle()
        assert idle is False

    def test_record_prunes_old_samples(self):
        v = LittlesLawVerifier("reactor", max_queue_depth=100)
        old_time = time.monotonic() - 200.0
        v._samples.append(QueueSample(timestamp=old_time, depth=1, processing_latency_ms=10.0))
        v.record(depth=1, processing_latency_ms=10.0)
        assert all(s.timestamp > old_time for s in v._samples)

    def test_zero_window_returns_none(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        now = time.monotonic()
        for _ in range(15):
            v._samples.append(QueueSample(timestamp=now, depth=1, processing_latency_ms=10.0))
        assert v.compute_L() is None


class TestProactiveDrive:
    def _make_idle_verifiers(self, idle=True):
        verifiers = {}
        for name in ("jarvis", "prime", "reactor"):
            v = LittlesLawVerifier(name, max_queue_depth=100)
            now = time.monotonic()
            if idle:
                for i in range(15):
                    v._samples.append(QueueSample(
                        timestamp=now + i * 1.0, depth=1, processing_latency_ms=5.0,
                    ))
            verifiers[name] = v
        return verifiers

    def test_initial_state_is_reactive(self):
        vs = self._make_idle_verifiers(idle=False)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        assert drive.state == "REACTIVE"

    def test_tick_not_idle_goes_to_measuring(self):
        vs = self._make_idle_verifiers(idle=False)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        state, reason = drive.tick()
        assert state == "MEASURING"
        assert "insufficient" in reason or "Not idle" in reason

    def test_tick_all_idle_starts_eligibility(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        state, reason = drive.tick()
        assert state == "MEASURING"
        assert "eligibility" in reason.lower() or "idle" in reason.lower()

    def test_becomes_eligible_after_min_seconds(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive.tick()
        drive._eligible_since = time.monotonic() - drive.MIN_ELIGIBLE_SECONDS - 1
        state, reason = drive.tick()
        assert state == "ELIGIBLE"

    def test_begin_exploration_from_eligible(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "ELIGIBLE"
        drive.begin_exploration()
        assert drive.state == "EXPLORING"

    def test_begin_exploration_from_wrong_state_raises(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        with pytest.raises(AssertionError):
            drive.begin_exploration()

    def test_end_exploration_enters_cooldown(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "EXPLORING"
        drive.end_exploration()
        assert drive.state == "COOLDOWN"

    def test_cooldown_expires_to_reactive(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "COOLDOWN"
        drive._last_exploration_end = time.monotonic() - drive.COOLDOWN_SECONDS - 1
        state, reason = drive.tick()
        assert state == "REACTIVE"

    def test_exploring_state_waits_for_sentinel(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "EXPLORING"
        state, reason = drive.tick()
        assert state == "EXPLORING"
        assert "Sentinel active" in reason

    def test_idle_interrupted_resets_eligibility(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive.tick()
        assert drive._eligible_since is not None
        vs["prime"]._samples.clear()
        state, reason = drive.tick()
        assert state == "MEASURING"
        assert drive._eligible_since is None
