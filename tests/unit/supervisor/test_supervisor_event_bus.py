"""
Tests for SupervisorEventBus, SupervisorEvent, and get_event_bus.

Source under test: unified_supervisor.py starting at line 5940.

These tests cover the event dataclass (frozen immutability, serialization),
the event bus (singleton, pub/sub, async delivery, backpressure, fault
isolation), and the module-level get_event_bus() accessor.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(**overrides):
    """Create a SupervisorEvent with sensible defaults, applying overrides."""
    from unified_supervisor import SupervisorEvent, SupervisorEventType, SupervisorEventSeverity

    defaults = dict(
        event_type=SupervisorEventType.LOG,
        timestamp=time.time(),
        message="test event",
    )
    defaults.update(overrides)
    return SupervisorEvent(**defaults)


def _reset_singletons():
    """Reset both the class-level and module-level singletons."""
    from unified_supervisor import SupervisorEventBus
    import unified_supervisor as _us

    SupervisorEventBus._instance = None
    _us._supervisor_event_bus = None


# =========================================================================
# TestSupervisorEvent
# =========================================================================


class TestSupervisorEvent:
    """Tests for the frozen SupervisorEvent dataclass."""

    def test_event_is_frozen(self):
        """Cannot mutate fields after creation (raises FrozenInstanceError)."""
        event = _make_event()
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.message = "changed"

    def test_event_default_values(self):
        """Unspecified fields use documented defaults."""
        from unified_supervisor import SupervisorEventSeverity

        event = _make_event()
        assert event.severity == SupervisorEventSeverity.INFO
        assert event.phase == ""
        assert event.component == ""
        assert event.duration_ms == 0.0
        assert event.progress_pct == -1
        assert event.metadata == ()
        assert event.correlation_id == ""

    def test_metadata_dict_from_tuple(self):
        """metadata_dict property converts tuple-of-pairs to dict."""
        event = _make_event(metadata=(("key1", "val1"), ("key2", 42)))
        assert event.metadata_dict == {"key1": "val1", "key2": 42}

    def test_metadata_dict_empty(self):
        """Empty metadata tuple yields empty dict."""
        event = _make_event(metadata=())
        assert event.metadata_dict == {}

    def test_to_json_dict_minimal(self):
        """Only always-present fields emitted when optionals are at defaults."""
        from unified_supervisor import SupervisorEventType, SupervisorEventSeverity

        ts = 1700000000.0
        event = _make_event(
            event_type=SupervisorEventType.LOG,
            timestamp=ts,
            message="hello",
        )
        d = event.to_json_dict()
        assert d == {
            "event_type": "log",
            "timestamp": ts,
            "message": "hello",
            "severity": "info",
        }
        # Optional fields must not appear
        assert "phase" not in d
        assert "component" not in d
        assert "duration_ms" not in d
        assert "progress_pct" not in d
        assert "metadata" not in d
        assert "correlation_id" not in d

    def test_to_json_dict_full(self):
        """All fields populated and serialized correctly."""
        from unified_supervisor import (
            SupervisorEventType,
            SupervisorEventSeverity,
        )

        ts = 1700000000.0
        event = _make_event(
            event_type=SupervisorEventType.PHASE_END,
            timestamp=ts,
            message="phase done",
            severity=SupervisorEventSeverity.SUCCESS,
            phase="backend",
            component="jarvis-body",
            duration_ms=1234.567,
            progress_pct=100.0,
            metadata=(("retries", 2),),
            correlation_id="abc-123",
        )
        d = event.to_json_dict()
        assert d["event_type"] == "phase_end"
        assert d["timestamp"] == ts
        assert d["message"] == "phase done"
        assert d["severity"] == "success"
        assert d["phase"] == "backend"
        assert d["component"] == "jarvis-body"
        assert d["duration_ms"] == 1234.6  # rounded to 1 decimal
        assert d["progress_pct"] == 100.0
        assert d["metadata"] == {"retries": 2}
        assert d["correlation_id"] == "abc-123"

    def test_to_json_dict_omits_negative_progress(self):
        """progress_pct=-1 (default/not applicable) is omitted from JSON."""
        event = _make_event(progress_pct=-1)
        d = event.to_json_dict()
        assert "progress_pct" not in d


# =========================================================================
# TestSupervisorEventBus
# =========================================================================


class TestSupervisorEventBus:
    """Tests for the SupervisorEventBus singleton, pub/sub, and async delivery."""

    def test_singleton_pattern(self, monkeypatch):
        """Two instantiations return the same object."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")
        from unified_supervisor import SupervisorEventBus
        _reset_singletons()

        try:
            a = SupervisorEventBus()
            b = SupervisorEventBus()
            assert a is b
        finally:
            _reset_singletons()

    def test_subscribe_increments_handler_count(self, event_bus_sync):
        """subscribe() increases handler_count."""
        assert event_bus_sync.handler_count == 0
        event_bus_sync.subscribe(lambda e: None)
        assert event_bus_sync.handler_count == 1
        event_bus_sync.subscribe(lambda e: None)
        assert event_bus_sync.handler_count == 2

    def test_unsubscribe_decrements(self, event_bus_sync):
        """unsubscribe() decreases handler_count."""
        handler = lambda e: None
        event_bus_sync.subscribe(handler)
        assert event_bus_sync.handler_count == 1
        event_bus_sync.unsubscribe(handler)
        assert event_bus_sync.handler_count == 0

    def test_unsubscribe_nonexistent_no_error(self, event_bus_sync):
        """Unsubscribing a handler that was never registered is a silent no-op."""
        event_bus_sync.unsubscribe(lambda e: None)  # should not raise

    def test_emit_sync_before_start(self, event_bus_sync):
        """Before start(), emit delivers synchronously (queue is None)."""
        received = []
        event_bus_sync.subscribe(lambda e: received.append(e))

        event = _make_event(message="sync delivery")
        event_bus_sync.emit(event)

        assert len(received) == 1
        assert received[0].message == "sync delivery"

    def test_emit_sync_closes_coroutines(self, event_bus_sync):
        """In sync mode, async handler coroutines are closed (not awaited)."""
        close_called = False

        async def async_handler(e):
            pass  # pragma: no cover

        # Wrap to detect .close()
        original_subscribe = event_bus_sync.subscribe

        closed_events = []

        async def tracking_async_handler(e):
            # This coroutine should be .close()'d
            closed_events.append(e)  # pragma: no cover
            await asyncio.sleep(10)  # pragma: no cover

        event_bus_sync.subscribe(tracking_async_handler)

        event = _make_event(message="sync with async handler")
        # Should not raise - coroutine is created and closed
        event_bus_sync.emit(event)

        # The event should NOT have been delivered asynchronously
        assert len(closed_events) == 0

    async def test_emit_async_after_start(self, event_bus):
        """After start(), events are delivered asynchronously via the queue."""
        received = []
        event_bus.subscribe(lambda e: received.append(e))

        event = _make_event(message="async delivery")
        event_bus.emit(event)

        # Give consumer loop time to process
        await asyncio.sleep(0.2)

        assert len(received) == 1
        assert received[0].message == "async delivery"

    def test_emit_disabled_no_delivery(self, monkeypatch):
        """When JARVIS_EVENT_BUS_ENABLED=false, emit() is a no-op."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "false")
        from unified_supervisor import SupervisorEventBus
        _reset_singletons()

        try:
            bus = SupervisorEventBus()
            received = []
            bus.subscribe(lambda e: received.append(e))

            bus.emit(_make_event(message="should be dropped"))
            assert len(received) == 0
        finally:
            _reset_singletons()

    async def test_backpressure_drops_oldest(self, monkeypatch):
        """When the queue is full, the oldest event is dropped to make room."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "3")
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")

        import unified_supervisor
        monkeypatch.setattr(unified_supervisor, "create_safe_task", asyncio.create_task)
        _reset_singletons()

        from unified_supervisor import SupervisorEventBus

        try:
            bus = SupervisorEventBus()
            await bus.start()

            # Pause the consumer so events pile up
            # We do this by stopping the consumer task but keeping the queue
            bus._started = False
            if bus._consumer_task:
                bus._consumer_task.cancel()
                try:
                    await asyncio.wait_for(bus._consumer_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            # Now emit 4 events into a queue of size 3
            for i in range(4):
                bus.emit(_make_event(message=f"event-{i}"))

            # Queue should contain events 1, 2, 3 (event-0 was dropped)
            assert bus._queue.qsize() == 3
            assert bus.dropped_count >= 1

            # Verify oldest was dropped: first in queue should be event-1
            first = bus._queue.get_nowait()
            assert first.message == "event-1"

            await bus.stop()
        finally:
            _reset_singletons()

    async def test_dropped_count_accurate(self, monkeypatch):
        """dropped_count reflects the exact number of overflow drops."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "2")
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")

        import unified_supervisor
        monkeypatch.setattr(unified_supervisor, "create_safe_task", asyncio.create_task)
        _reset_singletons()

        from unified_supervisor import SupervisorEventBus

        try:
            bus = SupervisorEventBus()
            await bus.start()

            # Pause consumer
            bus._started = False
            if bus._consumer_task:
                bus._consumer_task.cancel()
                try:
                    await asyncio.wait_for(bus._consumer_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            initial_dropped = bus.dropped_count

            # Emit 5 events into queue of size 2 -> 3 drops
            for i in range(5):
                bus.emit(_make_event(message=f"evt-{i}"))

            assert bus.dropped_count - initial_dropped == 3

            await bus.stop()
        finally:
            _reset_singletons()

    async def test_handler_crash_isolated(self, event_bus):
        """A handler that raises does not prevent other handlers from running."""
        received = []

        def crashing_handler(e):
            raise RuntimeError("boom")

        def good_handler(e):
            received.append(e)

        event_bus.subscribe(crashing_handler)
        event_bus.subscribe(good_handler)

        event_bus.emit(_make_event(message="survive crash"))
        await asyncio.sleep(0.2)

        assert len(received) == 1
        assert received[0].message == "survive crash"

    async def test_handler_timeout_isolated(self, event_bus):
        """A handler that takes >2s is timed out; other handlers still receive."""
        received = []

        async def slow_handler(e):
            await asyncio.sleep(10)  # Way beyond 2s timeout

        def fast_handler(e):
            received.append(e)

        event_bus.subscribe(slow_handler)
        event_bus.subscribe(fast_handler)

        event_bus.emit(_make_event(message="timeout test"))

        # Give enough time for delivery + timeout (2s) + margin
        await asyncio.sleep(2.5)

        assert len(received) == 1
        assert received[0].message == "timeout test"

    async def test_start_idempotent(self, monkeypatch):
        """Calling start() twice does not create a second consumer task."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "10")
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")

        import unified_supervisor
        monkeypatch.setattr(unified_supervisor, "create_safe_task", asyncio.create_task)
        _reset_singletons()

        from unified_supervisor import SupervisorEventBus

        try:
            bus = SupervisorEventBus()
            await bus.start()
            first_task = bus._consumer_task

            await bus.start()  # second call
            assert bus._consumer_task is first_task

            await bus.stop()
        finally:
            _reset_singletons()

    async def test_stop_drains_queue(self, monkeypatch):
        """stop() delivers remaining queued events before returning."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "10")
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")

        import unified_supervisor
        monkeypatch.setattr(unified_supervisor, "create_safe_task", asyncio.create_task)
        _reset_singletons()

        from unified_supervisor import SupervisorEventBus

        try:
            bus = SupervisorEventBus()
            await bus.start()

            # Pause consumer so events accumulate
            bus._started = False
            if bus._consumer_task:
                bus._consumer_task.cancel()
                try:
                    await asyncio.wait_for(bus._consumer_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            received = []
            bus.subscribe(lambda e: received.append(e))

            # Enqueue events with consumer paused
            for i in range(5):
                bus.emit(_make_event(message=f"drain-{i}"))

            assert bus._queue.qsize() == 5

            # stop() should drain the queue
            await bus.stop()

            assert len(received) == 5
            messages = [e.message for e in received]
            assert messages == [f"drain-{i}" for i in range(5)]
        finally:
            _reset_singletons()

    async def test_concurrent_emitters(self, event_bus):
        """10 concurrent tasks emitting simultaneously are all received."""
        received = []
        event_bus.subscribe(lambda e: received.append(e))

        async def emitter(idx):
            event_bus.emit(_make_event(message=f"concurrent-{idx}"))

        tasks = [asyncio.create_task(emitter(i)) for i in range(10)]
        await asyncio.gather(*tasks)

        # Give consumer time to process all events
        await asyncio.sleep(0.5)

        assert len(received) == 10
        messages = sorted(e.message for e in received)
        expected = sorted(f"concurrent-{i}" for i in range(10))
        assert messages == expected


# =========================================================================
# TestGetEventBus
# =========================================================================


class TestGetEventBus:
    """Tests for the module-level get_event_bus() accessor."""

    def test_returns_singleton(self, monkeypatch):
        """get_event_bus() returns the same instance on repeated calls."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")
        _reset_singletons()

        try:
            from unified_supervisor import get_event_bus

            a = get_event_bus()
            b = get_event_bus()
            assert a is b
        finally:
            _reset_singletons()

    def test_module_cache_reset(self, monkeypatch):
        """After resetting _supervisor_event_bus, get_event_bus creates new."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")
        _reset_singletons()

        try:
            import unified_supervisor
            from unified_supervisor import get_event_bus, SupervisorEventBus

            first = get_event_bus()

            # Reset only the module-level cache, AND the class singleton
            unified_supervisor._supervisor_event_bus = None
            SupervisorEventBus._instance = None

            second = get_event_bus()
            assert second is not first
        finally:
            _reset_singletons()

    def test_get_event_bus_returns_event_bus_type(self, monkeypatch):
        """get_event_bus() returns a SupervisorEventBus instance."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")
        _reset_singletons()

        try:
            from unified_supervisor import get_event_bus, SupervisorEventBus

            bus = get_event_bus()
            assert isinstance(bus, SupervisorEventBus)
        finally:
            _reset_singletons()
