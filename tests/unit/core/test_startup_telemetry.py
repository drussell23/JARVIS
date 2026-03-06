"""Tests for backend.core.startup_telemetry — event-sourced startup telemetry."""

from __future__ import annotations

import json
import pathlib
from typing import List

import pytest

from backend.core.startup_telemetry import (
    EventConsumer,
    MetricsCollector,
    StartupEvent,
    StartupEventBus,
    StructuredLogger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RecordingConsumer(EventConsumer):
    """Test consumer that records every event it receives."""

    def __init__(self) -> None:
        self.events: List[StartupEvent] = []

    async def consume(self, event: StartupEvent) -> None:
        self.events.append(event)


class _BadConsumer(EventConsumer):
    """Consumer that always raises."""

    async def consume(self, event: StartupEvent) -> None:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# TestStartupEvent
# ---------------------------------------------------------------------------

class TestStartupEvent:
    def test_event_is_frozen(self) -> None:
        event = StartupEvent(
            trace_id="abc",
            event_type="phase_gate",
            timestamp=1.0,
            wall_clock="2026-01-01T00:00:00+00:00",
            authority_state="IDLE",
            phase="init",
            detail={"key": "value"},
        )
        with pytest.raises(AttributeError):
            event.trace_id = "new"  # type: ignore[misc]

    def test_event_has_required_fields(self) -> None:
        event = StartupEvent(
            trace_id="t1",
            event_type="lease_probe",
            timestamp=42.5,
            wall_clock="2026-03-06T12:00:00+00:00",
            authority_state="BOOTING",
            phase=None,
            detail={"probe": True},
        )
        assert event.trace_id == "t1"
        assert event.event_type == "lease_probe"
        assert event.timestamp == 42.5
        assert event.wall_clock == "2026-03-06T12:00:00+00:00"
        assert event.authority_state == "BOOTING"
        assert event.phase is None
        assert event.detail == {"probe": True}


# ---------------------------------------------------------------------------
# TestStartupEventBus
# ---------------------------------------------------------------------------

class TestStartupEventBus:
    @pytest.fixture()
    def bus(self) -> StartupEventBus:
        return StartupEventBus(trace_id="test-trace-001")

    @pytest.mark.asyncio
    async def test_emit_delivers_to_all_consumers(self, bus: StartupEventBus) -> None:
        c1 = _RecordingConsumer()
        c2 = _RecordingConsumer()
        bus.subscribe(c1)
        bus.subscribe(c2)

        event = bus.create_event("phase_gate", {"step": 1}, phase="init")
        await bus.emit(event)

        assert len(c1.events) == 1
        assert len(c2.events) == 1
        assert c1.events[0] is event
        assert c2.events[0] is event

    @pytest.mark.asyncio
    async def test_emit_with_no_consumers_does_not_error(self, bus: StartupEventBus) -> None:
        event = bus.create_event("phase_gate", {"x": 1})
        await bus.emit(event)  # should not raise

    @pytest.mark.asyncio
    async def test_consumer_error_does_not_block_others(self, bus: StartupEventBus) -> None:
        bad = _BadConsumer()
        good = _RecordingConsumer()
        bus.subscribe(bad)
        bus.subscribe(good)

        event = bus.create_event("lease_probe", {"ok": True})
        await bus.emit(event)

        assert len(good.events) == 1
        assert good.events[0] is event

    def test_create_event_sets_trace_id(self, bus: StartupEventBus) -> None:
        event = bus.create_event("authority_transition", {"from": "A", "to": "B"})
        assert event.trace_id == "test-trace-001"

    def test_event_history_returns_copy(self, bus: StartupEventBus) -> None:
        history = bus.event_history
        history.append(None)  # type: ignore[arg-type]
        assert len(bus.event_history) == 0  # internal list not affected

    @pytest.mark.asyncio
    async def test_emit_appends_to_history(self, bus: StartupEventBus) -> None:
        e1 = bus.create_event("phase_gate", {"n": 1})
        e2 = bus.create_event("lease_probe", {"n": 2})
        await bus.emit(e1)
        await bus.emit(e2)

        history = bus.event_history
        assert len(history) == 2
        assert history[0] is e1
        assert history[1] is e2


# ---------------------------------------------------------------------------
# TestStructuredLogger
# ---------------------------------------------------------------------------

class TestStructuredLogger:
    @pytest.fixture()
    def _log_dir(self) -> pathlib.Path:
        """Create a temp dir under the sandbox-writable private tmp."""
        import shutil
        import tempfile

        d = pathlib.Path(tempfile.mkdtemp(prefix="tel_", dir="/private/tmp/claude-501"))
        yield d
        shutil.rmtree(str(d), ignore_errors=True)

    @pytest.mark.asyncio
    async def test_logs_event_as_json(self, _log_dir: pathlib.Path) -> None:
        log_file = _log_dir / "events.jsonl"
        logger = StructuredLogger(str(log_file))

        event = StartupEvent(
            trace_id="t-log",
            event_type="invariant_check",
            timestamp=100.0,
            wall_clock="2026-03-06T08:00:00+00:00",
            authority_state="RUNNING",
            phase="ready",
            detail={"invariant": "budget_nonzero", "passed": True},
        )
        await logger.consume(event)

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["trace_id"] == "t-log"
        assert data["event_type"] == "invariant_check"
        assert data["timestamp"] == 100.0
        assert data["wall_clock"] == "2026-03-06T08:00:00+00:00"
        assert data["authority_state"] == "RUNNING"
        assert data["phase"] == "ready"
        assert data["detail"]["invariant"] == "budget_nonzero"
        assert data["detail"]["passed"] is True


# ---------------------------------------------------------------------------
# TestMetricsCollector
# ---------------------------------------------------------------------------

class TestMetricsCollector:
    @pytest.mark.asyncio
    async def test_counts_events_by_type(self) -> None:
        mc = MetricsCollector()
        bus = StartupEventBus(trace_id="m1")
        bus.subscribe(mc)

        for _ in range(3):
            await bus.emit(bus.create_event("phase_gate", {}))
        await bus.emit(bus.create_event("lease_probe", {}))

        snap = mc.snapshot()
        assert snap["counts"]["phase_gate"] == 3
        assert snap["counts"]["lease_probe"] == 1

    @pytest.mark.asyncio
    async def test_tracks_phase_durations(self) -> None:
        mc = MetricsCollector()
        event = StartupEvent(
            trace_id="m2",
            event_type="phase_gate",
            timestamp=1.0,
            wall_clock="2026-01-01T00:00:00+00:00",
            authority_state="BOOTING",
            phase="model_load",
            detail={"duration_s": 3.45},
        )
        await mc.consume(event)

        snap = mc.snapshot()
        assert snap["phase_durations"]["model_load"] == 3.45

    def test_snapshot_returns_copy(self) -> None:
        mc = MetricsCollector()
        snap = mc.snapshot()
        assert "counts" in snap
        assert "phase_durations" in snap
        assert "budget_wait_times" in snap
        # Mutating the snapshot must not affect internals.
        snap["counts"]["fake"] = 999
        assert "fake" not in mc.snapshot()["counts"]
