"""tests/governance/autonomy/test_feedback_engine_brain_hint.py

TDD tests for AutonomyFeedbackEngine — Canary -> Brain Feedback (Task 7, C+ Autonomous Loop).

Covers:
- Brain hint emitted after enough failures (>= threshold)
- No hint below threshold
- Success decays rollback count
- Different brains tracked independently
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)


def _make_engine(tmp_path, *, threshold: int = 3):
    """Build a FeedbackEngine wired to an EventEmitter and return components."""
    event_dir = tmp_path / "events"
    state_dir = tmp_path / "state"
    event_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    bus = CommandBus(maxsize=64)
    emitter = EventEmitter()
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(
        command_bus=bus, config=config, event_emitter=emitter,
    )
    # Override threshold if needed
    engine._brain_hint_threshold = threshold

    # Wire up event handlers
    engine.register_event_handlers(emitter)

    return engine, bus, emitter


def _rollback_event(brain_id: str = "j-prime") -> EventEnvelope:
    """Create an OP_ROLLED_BACK event with the given brain_id."""
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_ROLLED_BACK,
        payload={"brain_id": brain_id},
    )


def _completed_event(brain_id: str = "j-prime") -> EventEnvelope:
    """Create an OP_COMPLETED event with the given brain_id."""
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_COMPLETED,
        payload={"brain_id": brain_id},
    )


# ---------------------------------------------------------------------------
# Brain hint emitted after enough failures (>= threshold)
# ---------------------------------------------------------------------------


class TestBrainHintEmittedAfterThreshold:
    @pytest.mark.asyncio
    async def test_hint_emitted_at_threshold(self, tmp_path):
        """After exactly *threshold* rollbacks for a brain, an ADJUST_BRAIN_HINT
        command should appear on the bus."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        for _ in range(3):
            await emitter.emit(_rollback_event("j-prime"))

        assert bus.qsize() == 1
        cmd = await bus.get()
        assert cmd.command_type == CommandType.ADJUST_BRAIN_HINT
        assert cmd.source_layer == "L2"
        assert cmd.target_layer == "L1"
        assert cmd.payload["brain_id"] == "j-prime"
        assert cmd.payload["weight_delta"] == -0.1

    @pytest.mark.asyncio
    async def test_hint_payload_shape(self, tmp_path):
        """The ADJUST_BRAIN_HINT payload should contain all required fields."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        for _ in range(3):
            await emitter.emit(_rollback_event("j-prime"))

        cmd = await bus.get()
        p = cmd.payload
        assert "brain_id" in p
        assert "weight_delta" in p
        assert "evidence_window_ops" in p
        assert "canary_slice" in p
        assert "reason" in p
        assert p["canary_slice"] == "tests/"

    @pytest.mark.asyncio
    async def test_hint_emitted_again_after_more_failures(self, tmp_path):
        """If rollbacks continue past the threshold, additional hints are emitted
        at every subsequent threshold crossing."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        # First threshold crossing
        for _ in range(3):
            await emitter.emit(_rollback_event("j-prime"))

        assert bus.qsize() == 1

        # Three more rollbacks -> second crossing
        for _ in range(3):
            await emitter.emit(_rollback_event("j-prime"))

        assert bus.qsize() == 2


# ---------------------------------------------------------------------------
# No hint below threshold
# ---------------------------------------------------------------------------


class TestNoHintBelowThreshold:
    @pytest.mark.asyncio
    async def test_no_hint_at_one_rollback(self, tmp_path):
        """A single rollback should not produce any command."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        await emitter.emit(_rollback_event("j-prime"))

        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_no_hint_at_two_rollbacks(self, tmp_path):
        """Two rollbacks (below threshold=3) should not produce any command."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        await emitter.emit(_rollback_event("j-prime"))
        await emitter.emit(_rollback_event("j-prime"))

        assert bus.qsize() == 0


# ---------------------------------------------------------------------------
# Success decays rollback count
# ---------------------------------------------------------------------------


class TestSuccessDecaysRollbackCount:
    @pytest.mark.asyncio
    async def test_success_decays_count(self, tmp_path):
        """An OP_COMPLETED event should decrement the rollback count,
        preventing threshold crossing."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        # Two rollbacks -> count = 2
        await emitter.emit(_rollback_event("j-prime"))
        await emitter.emit(_rollback_event("j-prime"))

        # One success -> count = 1
        await emitter.emit(_completed_event("j-prime"))

        # One more rollback -> count = 2 (still below threshold)
        await emitter.emit(_rollback_event("j-prime"))

        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_success_does_not_go_below_zero(self, tmp_path):
        """Decay should floor at zero — success on a brain with no rollbacks
        should not cause negative counts."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        # Success with no prior rollbacks
        await emitter.emit(_completed_event("j-prime"))

        # Now do exactly threshold rollbacks -> should still emit at threshold
        for _ in range(3):
            await emitter.emit(_rollback_event("j-prime"))

        assert bus.qsize() == 1

    @pytest.mark.asyncio
    async def test_success_for_unknown_brain_is_harmless(self, tmp_path):
        """A completion event for a brain with no rollback history should
        not raise or produce side effects."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        # Should not raise
        await emitter.emit(_completed_event("unknown-brain"))

        assert bus.qsize() == 0


# ---------------------------------------------------------------------------
# Different brains tracked independently
# ---------------------------------------------------------------------------


class TestDifferentBrainsTrackedIndependently:
    @pytest.mark.asyncio
    async def test_separate_brain_counts(self, tmp_path):
        """Rollbacks for brain-A should not affect the count for brain-B."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        # Two rollbacks for brain-A, one for brain-B
        await emitter.emit(_rollback_event("brain-A"))
        await emitter.emit(_rollback_event("brain-A"))
        await emitter.emit(_rollback_event("brain-B"))

        # Neither has reached threshold
        assert bus.qsize() == 0

        # One more for brain-A -> crosses threshold
        await emitter.emit(_rollback_event("brain-A"))
        assert bus.qsize() == 1

        cmd = await bus.get()
        assert cmd.payload["brain_id"] == "brain-A"

        # brain-B still at 1 — no hint for it
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_success_decays_only_target_brain(self, tmp_path):
        """A success for brain-A should not decay brain-B's count."""
        engine, bus, emitter = _make_engine(tmp_path, threshold=3)

        # Two rollbacks for each brain
        await emitter.emit(_rollback_event("brain-A"))
        await emitter.emit(_rollback_event("brain-A"))
        await emitter.emit(_rollback_event("brain-B"))
        await emitter.emit(_rollback_event("brain-B"))

        # Success for brain-A -> brain-A count = 1, brain-B still = 2
        await emitter.emit(_completed_event("brain-A"))

        # One more rollback for brain-B -> count = 3, should trigger
        await emitter.emit(_rollback_event("brain-B"))

        assert bus.qsize() == 1
        cmd = await bus.get()
        assert cmd.payload["brain_id"] == "brain-B"
