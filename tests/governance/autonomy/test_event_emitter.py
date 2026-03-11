"""tests/governance/autonomy/test_event_emitter.py

TDD tests for EventEmitter (Task 3: L1 Outcome Events).

Covers:
- subscribe and receive a single event
- multiple subscribers both receive
- unrelated event not delivered
- subscriber error isolated (bad handler doesn't block good handler)
- cursor tracking (last_event_id updates)
"""
from __future__ import annotations

import asyncio

import pytest


def _make_event(
    event_type=None,
    payload=None,
    source_layer: str = "L1",
    op_id: str | None = None,
):
    """Helper to build EventEnvelope with minimal boilerplate."""
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (
        EventEnvelope,
        EventType,
    )

    return EventEnvelope(
        source_layer=source_layer,
        event_type=event_type or EventType.OP_COMPLETED,
        payload=payload or {},
        op_id=op_id,
    )


# ---------------------------------------------------------------------------
# subscribe and receive a single event
# ---------------------------------------------------------------------------


class TestSubscribeAndReceiveSingle:
    @pytest.mark.asyncio
    async def test_single_subscriber_receives_event(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(EventType.OP_COMPLETED, handler)

        event = _make_event(event_type=EventType.OP_COMPLETED, payload={"ok": True})
        await emitter.emit(event)

        assert len(received) == 1
        assert received[0] is event
        assert received[0].payload == {"ok": True}

    @pytest.mark.asyncio
    async def test_sync_handler_also_supported(self):
        """A synchronous (non-async) handler should also work."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        received = []

        def handler(event):
            received.append(event)

        emitter.subscribe(EventType.OP_COMPLETED, handler)

        event = _make_event(event_type=EventType.OP_COMPLETED)
        await emitter.emit(event)

        assert len(received) == 1
        assert received[0] is event


# ---------------------------------------------------------------------------
# multiple subscribers both receive
# ---------------------------------------------------------------------------


class TestMultipleSubscribers:
    @pytest.mark.asyncio
    async def test_both_subscribers_receive_same_event(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        received_a = []
        received_b = []

        async def handler_a(event):
            received_a.append(event)

        async def handler_b(event):
            received_b.append(event)

        emitter.subscribe(EventType.OP_COMPLETED, handler_a)
        emitter.subscribe(EventType.OP_COMPLETED, handler_b)

        event = _make_event(event_type=EventType.OP_COMPLETED, payload={"n": 42})
        await emitter.emit(event)

        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0] is event
        assert received_b[0] is event

    @pytest.mark.asyncio
    async def test_subscribers_on_different_event_types(self):
        """Subscribers for different event types each receive only their events."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        completed_events = []
        rollback_events = []

        async def on_completed(event):
            completed_events.append(event)

        async def on_rollback(event):
            rollback_events.append(event)

        emitter.subscribe(EventType.OP_COMPLETED, on_completed)
        emitter.subscribe(EventType.OP_ROLLED_BACK, on_rollback)

        evt_c = _make_event(event_type=EventType.OP_COMPLETED)
        evt_r = _make_event(event_type=EventType.OP_ROLLED_BACK)
        await emitter.emit(evt_c)
        await emitter.emit(evt_r)

        assert len(completed_events) == 1
        assert completed_events[0] is evt_c
        assert len(rollback_events) == 1
        assert rollback_events[0] is evt_r


# ---------------------------------------------------------------------------
# unrelated event not delivered
# ---------------------------------------------------------------------------


class TestUnrelatedEventNotDelivered:
    @pytest.mark.asyncio
    async def test_subscriber_not_called_for_unrelated_event_type(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        received = []

        async def handler(event):
            received.append(event)

        # Subscribe only to OP_COMPLETED
        emitter.subscribe(EventType.OP_COMPLETED, handler)

        # Emit a different event type
        unrelated = _make_event(event_type=EventType.HEALTH_PROBE_RESULT)
        await emitter.emit(unrelated)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_no_subscribers_emit_succeeds_silently(self):
        """emit() should not error even when no subscribers exist for the event type."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        event = _make_event()
        # Should not raise
        await emitter.emit(event)


# ---------------------------------------------------------------------------
# subscriber error isolated
# ---------------------------------------------------------------------------


class TestSubscriberErrorIsolation:
    @pytest.mark.asyncio
    async def test_bad_handler_does_not_block_good_handler(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        received = []

        async def bad_handler(event):
            raise RuntimeError("subscriber exploded")

        async def good_handler(event):
            received.append(event)

        # Subscribe bad handler first, good handler second
        emitter.subscribe(EventType.OP_COMPLETED, bad_handler)
        emitter.subscribe(EventType.OP_COMPLETED, good_handler)

        event = _make_event(event_type=EventType.OP_COMPLETED, payload={"x": 1})
        # Should not raise despite bad_handler failure
        await emitter.emit(event)

        # Good handler still received the event
        assert len(received) == 1
        assert received[0] is event

    @pytest.mark.asyncio
    async def test_sync_bad_handler_isolated(self):
        """A synchronous handler that raises should also be isolated."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        received = []

        def bad_sync_handler(event):
            raise ValueError("sync explosion")

        async def good_handler(event):
            received.append(event)

        emitter.subscribe(EventType.OP_COMPLETED, bad_sync_handler)
        emitter.subscribe(EventType.OP_COMPLETED, good_handler)

        event = _make_event(event_type=EventType.OP_COMPLETED)
        await emitter.emit(event)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_multiple_bad_handlers_all_isolated(self):
        """Multiple failing handlers should all be isolated; emit still succeeds."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        received = []

        async def bad_1(event):
            raise RuntimeError("fail 1")

        async def bad_2(event):
            raise RuntimeError("fail 2")

        async def good(event):
            received.append(event)

        emitter.subscribe(EventType.OP_COMPLETED, bad_1)
        emitter.subscribe(EventType.OP_COMPLETED, bad_2)
        emitter.subscribe(EventType.OP_COMPLETED, good)

        event = _make_event(event_type=EventType.OP_COMPLETED)
        await emitter.emit(event)

        assert len(received) == 1


# ---------------------------------------------------------------------------
# cursor tracking (last_event_id updates)
# ---------------------------------------------------------------------------


class TestCursorTracking:
    @pytest.mark.asyncio
    async def test_last_event_id_starts_none(self):
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        assert emitter.last_event_id is None

    @pytest.mark.asyncio
    async def test_last_event_id_updates_after_emit(self):
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        event = _make_event(payload={"seq": 1})
        await emitter.emit(event)
        assert emitter.last_event_id == event.event_id

    @pytest.mark.asyncio
    async def test_last_event_id_tracks_latest_event(self):
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        event_1 = _make_event(payload={"seq": 1})
        event_2 = _make_event(payload={"seq": 2})
        event_3 = _make_event(payload={"seq": 3})

        await emitter.emit(event_1)
        assert emitter.last_event_id == event_1.event_id

        await emitter.emit(event_2)
        assert emitter.last_event_id == event_2.event_id

        await emitter.emit(event_3)
        assert emitter.last_event_id == event_3.event_id

    @pytest.mark.asyncio
    async def test_last_event_id_updates_even_with_no_subscribers(self):
        """Cursor should update even if nobody is listening."""
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()
        event = _make_event()
        await emitter.emit(event)
        assert emitter.last_event_id == event.event_id

    @pytest.mark.asyncio
    async def test_last_event_id_updates_despite_subscriber_error(self):
        """Cursor should update even when a subscriber raises."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventType,
        )
        from backend.core.ouroboros.governance.autonomy.event_emitter import (
            EventEmitter,
        )

        emitter = EventEmitter()

        async def bad_handler(event):
            raise RuntimeError("kaboom")

        emitter.subscribe(EventType.OP_COMPLETED, bad_handler)

        event = _make_event(event_type=EventType.OP_COMPLETED)
        await emitter.emit(event)
        assert emitter.last_event_id == event.event_id
