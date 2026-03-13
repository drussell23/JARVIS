"""tests/governance/autonomy/test_feedback_engine_attribution.py

TDD tests for AutonomyFeedbackEngine — Model Attribution Scoring (Task 5, C+ Autonomous Loop).

Covers:
- score_attribution emits ATTRIBUTION_SCORED events for each active brain
- Fault isolated: persistence errors don't propagate
- Brains with fewer than MIN_SAMPLE_SIZE records are skipped
- No event_emitter => silent return
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventEnvelope,
    EventType,
)


# ---------------------------------------------------------------------------
# Fake persistence for testing
# ---------------------------------------------------------------------------


class FakePersistence:
    """Duck-typed persistence with configurable brain data."""

    def __init__(
        self,
        brain_ids: Optional[List[str]] = None,
        records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        self._brain_ids = brain_ids or []
        self._records = records or {}

    async def get_active_brain_ids(self) -> List[str]:
        return list(self._brain_ids)

    async def get_records_by_model_and_task(
        self,
        brain_id: str,
        window_hours: float = 24.0,
    ) -> List[Dict[str, Any]]:
        return list(self._records.get(brain_id, []))


class FailingPersistence:
    """Persistence that always raises on any call."""

    async def get_active_brain_ids(self) -> List[str]:
        raise RuntimeError("database connection lost")

    async def get_records_by_model_and_task(
        self,
        brain_id: str,
        window_hours: float = 24.0,
    ) -> List[Dict[str, Any]]:
        raise RuntimeError("database connection lost")


class PartialFailPersistence:
    """Returns brain_ids fine, but raises on record fetch for specific brains."""

    def __init__(
        self,
        brain_ids: List[str],
        records: Dict[str, List[Dict[str, Any]]],
        fail_brains: Optional[List[str]] = None,
    ) -> None:
        self._brain_ids = brain_ids
        self._records = records
        self._fail_brains = set(fail_brains or [])

    async def get_active_brain_ids(self) -> List[str]:
        return list(self._brain_ids)

    async def get_records_by_model_and_task(
        self,
        brain_id: str,
        window_hours: float = 24.0,
    ) -> List[Dict[str, Any]]:
        if brain_id in self._fail_brains:
            raise RuntimeError(f"fetch failed for {brain_id}")
        return list(self._records.get(brain_id, []))


class LoadRecordsPersistence:
    """Duck-typed persistence matching the real PerformanceRecordPersistence API."""

    def __init__(self, records: Dict[str, List[Any]]) -> None:
        self._records = records
        self.calls: List[Dict[str, Any]] = []

    async def load_records(
        self,
        model_id: Optional[str] = None,
        limit: int = 100,
        since: Optional[datetime] = None,
    ) -> Dict[str, deque]:
        self.calls.append(
            {
                "model_id": model_id,
                "limit": limit,
                "since": since,
            }
        )
        return {
            brain_id: deque(records, maxlen=limit)
            for brain_id, records in self._records.items()
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(tmp_path, event_emitter=None):
    """Build a FeedbackEngine with minimal config."""
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.feedback_engine import (
        AutonomyFeedbackEngine,
        FeedbackEngineConfig,
    )

    event_dir = tmp_path / "events"
    state_dir = tmp_path / "state"
    event_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    bus = CommandBus(maxsize=64)
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    return AutonomyFeedbackEngine(
        command_bus=bus,
        config=config,
        event_emitter=event_emitter,
    )


def _make_records(successes: int, failures: int) -> List[Dict[str, Any]]:
    """Build a list of outcome records with given success/failure counts."""
    records: List[Dict[str, Any]] = []
    for _ in range(successes):
        records.append({"outcome": "success"})
    for _ in range(failures):
        records.append({"outcome": "failure"})
    return records


# ---------------------------------------------------------------------------
# score_attribution emits ATTRIBUTION_SCORED events
# ---------------------------------------------------------------------------


class TestScoreAttributionEmitsEvents:
    @pytest.mark.asyncio
    async def test_emits_attribution_scored_for_each_brain(self, tmp_path):
        """Each active brain with sufficient data gets an ATTRIBUTION_SCORED event."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = FakePersistence(
            brain_ids=["brain-alpha", "brain-beta"],
            records={
                "brain-alpha": _make_records(successes=7, failures=3),  # 70%
                "brain-beta": _make_records(successes=4, failures=1),   # 80%
            },
        )

        await engine.score_attribution_once(persistence)

        assert len(captured) == 2
        payloads_by_brain = {e.payload["brain_id"]: e.payload for e in captured}

        assert "brain-alpha" in payloads_by_brain
        alpha = payloads_by_brain["brain-alpha"]
        assert alpha["success_rate"] == pytest.approx(0.7)
        assert alpha["sample_size"] == 10

        assert "brain-beta" in payloads_by_brain
        beta = payloads_by_brain["brain-beta"]
        assert beta["success_rate"] == pytest.approx(0.8)
        assert beta["sample_size"] == 5

    @pytest.mark.asyncio
    async def test_event_envelope_shape(self, tmp_path):
        """Verify the EventEnvelope has the right source_layer, event_type, and payload keys."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = FakePersistence(
            brain_ids=["brain-x"],
            records={
                "brain-x": _make_records(successes=3, failures=0),
            },
        )

        await engine.score_attribution_once(persistence)

        assert len(captured) == 1
        event = captured[0]
        assert event.source_layer == "L2"
        assert event.event_type == EventType.ATTRIBUTION_SCORED
        assert "brain_id" in event.payload
        assert "success_rate" in event.payload
        assert "avg_quality_score" in event.payload
        assert "sample_size" in event.payload
        assert "window_hours" in event.payload

    @pytest.mark.asyncio
    async def test_all_failures_gives_zero_success_rate(self, tmp_path):
        """A brain with 100% failure rate should still emit with success_rate=0.0."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = FakePersistence(
            brain_ids=["brain-fail"],
            records={
                "brain-fail": _make_records(successes=0, failures=5),
            },
        )

        await engine.score_attribution_once(persistence)

        assert len(captured) == 1
        assert captured[0].payload["success_rate"] == pytest.approx(0.0)
        assert captured[0].payload["sample_size"] == 5


# ---------------------------------------------------------------------------
# Fault isolated: persistence errors don't propagate
# ---------------------------------------------------------------------------


class TestScoreAttributionFaultIsolated:
    @pytest.mark.asyncio
    async def test_persistence_error_on_get_brain_ids_does_not_raise(self, tmp_path):
        """If persistence.get_active_brain_ids() raises, score_attribution_once returns silently."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = FailingPersistence()

        # Should NOT raise
        await engine.score_attribution_once(persistence)

    @pytest.mark.asyncio
    async def test_persistence_error_on_get_records_does_not_raise(self, tmp_path):
        """If persistence.get_records_by_model_and_task() raises for one brain,
        other brains still get scored."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = PartialFailPersistence(
            brain_ids=["brain-ok", "brain-fail"],
            records={
                "brain-ok": _make_records(successes=4, failures=1),
            },
            fail_brains=["brain-fail"],
        )

        # Should NOT raise
        await engine.score_attribution_once(persistence)

        # brain-ok should still have been scored
        assert len(captured) == 1
        assert captured[0].payload["brain_id"] == "brain-ok"


# ---------------------------------------------------------------------------
# Skip brain with insufficient data
# ---------------------------------------------------------------------------


class TestScoreAttributionSkipsInsufficientData:
    @pytest.mark.asyncio
    async def test_brain_with_fewer_than_3_records_skipped(self, tmp_path):
        """Brains with < 3 records should not emit an ATTRIBUTION_SCORED event."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = FakePersistence(
            brain_ids=["brain-small", "brain-enough"],
            records={
                "brain-small": _make_records(successes=1, failures=1),   # 2 records
                "brain-enough": _make_records(successes=2, failures=1),  # 3 records
            },
        )

        await engine.score_attribution_once(persistence)

        assert len(captured) == 1
        assert captured[0].payload["brain_id"] == "brain-enough"

    @pytest.mark.asyncio
    async def test_brain_with_zero_records_skipped(self, tmp_path):
        """Brains with 0 records should be skipped."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = FakePersistence(
            brain_ids=["brain-empty"],
            records={},  # no records at all
        )

        await engine.score_attribution_once(persistence)

        assert len(captured) == 0

    @pytest.mark.asyncio
    async def test_exactly_3_records_is_scored(self, tmp_path):
        """Exactly 3 records (the minimum) should be scored, not skipped."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = FakePersistence(
            brain_ids=["brain-min"],
            records={
                "brain-min": _make_records(successes=2, failures=1),
            },
        )

        await engine.score_attribution_once(persistence)

        assert len(captured) == 1
        assert captured[0].payload["sample_size"] == 3
        assert captured[0].payload["success_rate"] == pytest.approx(2.0 / 3.0)

    @pytest.mark.asyncio
    async def test_real_load_records_shape_is_supported(self, tmp_path):
        """The real load_records persistence shape should emit attribution events."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter

        emitter = EventEmitter()
        captured: List[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            captured.append(event)

        emitter.subscribe(EventType.ATTRIBUTION_SCORED, capture)

        engine = _make_engine(tmp_path, event_emitter=emitter)

        persistence = LoadRecordsPersistence(
            records={
                "brain-live": [
                    SimpleNamespace(success=True, code_quality_score=0.9),
                    SimpleNamespace(success=True, code_quality_score=0.8),
                    SimpleNamespace(success=False, code_quality_score=0.3),
                ]
            }
        )

        await engine.score_attribution_once(persistence)

        assert len(captured) == 1
        payload = captured[0].payload
        assert payload["brain_id"] == "brain-live"
        assert payload["success_rate"] == pytest.approx(2.0 / 3.0)
        assert payload["avg_quality_score"] == pytest.approx((0.9 + 0.8 + 0.3) / 3.0)
        assert persistence.calls[0]["since"] is not None
        assert persistence.calls[0]["since"].tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# No event_emitter => silent return
# ---------------------------------------------------------------------------


class TestScoreAttributionNoEmitter:
    @pytest.mark.asyncio
    async def test_no_emitter_returns_silently(self, tmp_path):
        """If no event_emitter was provided, score_attribution_once is a no-op."""
        engine = _make_engine(tmp_path, event_emitter=None)

        persistence = FakePersistence(
            brain_ids=["brain-a"],
            records={
                "brain-a": _make_records(successes=5, failures=0),
            },
        )

        # Should NOT raise
        await engine.score_attribution_once(persistence)
