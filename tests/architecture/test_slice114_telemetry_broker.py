"""Slice 114 — cross-process telemetry broker (gateway decoupling).

Proves the non-blocking engine→gateway transport: publish never blocks the FSM
(drop-oldest on full), the gateway-side drain fans frames to its WS manager and
stops cleanly on the sentinel, and the decoupled bridge subscriber routes
lifecycle events to the queue instead of an in-process manager. No real
subprocess — the IPC mechanics are exercised with stdlib queues.
"""

from __future__ import annotations

import asyncio
import queue as _queue

import pytest

from backend.core.ouroboros.governance import telemetry_broker as TB
from backend.core.ouroboros.governance.telemetry_broker import (
    drain_queue_to_manager,
    gateway_decoupled_enabled,
    publish_frame,
    stop_gateway_queue,
)


class _BoundedFakeQueue:
    """Deterministic bounded queue with the put_nowait/get_nowait + queue.Full
    contract publish_frame relies on (mp.Queue's Full timing is racy in tests)."""

    def __init__(self, maxsize):
        self._items = []
        self._max = maxsize

    def put_nowait(self, item):
        if len(self._items) >= self._max:
            raise _queue.Full()
        self._items.append(item)

    def get_nowait(self):
        if not self._items:
            raise _queue.Empty()
        return self._items.pop(0)


class _FakeManager:
    def __init__(self):
        self.frames = []
    async def broadcast(self, frame):
        self.frames.append(frame)


class _FakeEvent:
    def __init__(self, payload):
        self.payload = payload


# ===========================================================================
# Master flag
# ===========================================================================


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_GATEWAY_DECOUPLED_ENABLED", raising=False)
    assert gateway_decoupled_enabled() is False
    monkeypatch.setenv("JARVIS_GATEWAY_DECOUPLED_ENABLED", "1")
    assert gateway_decoupled_enabled() is True


# ===========================================================================
# publish_frame — non-blocking, drop-oldest, never raises
# ===========================================================================


class TestPublishFrame:
    def test_roundtrip(self):
        q = _BoundedFakeQueue(4)
        assert publish_frame(q, {"kind": "telemetry", "n": 1}) is True
        assert q.get_nowait() == {"kind": "telemetry", "n": 1}

    def test_drop_oldest_when_full(self):
        q = _BoundedFakeQueue(2)
        publish_frame(q, {"n": 1})
        publish_frame(q, {"n": 2})
        # Queue full → publish_frame drops the oldest (n=1) to make room for n=3.
        assert publish_frame(q, {"n": 3}) is True
        remaining = [q.get_nowait(), q.get_nowait()]
        assert {"n": 1} not in remaining          # oldest dropped
        assert {"n": 3} in remaining               # newest kept

    def test_none_queue_is_safe(self):
        assert publish_frame(None, {"n": 1}) is False  # never raises


# ===========================================================================
# drain_queue_to_manager — gateway-side fan-out + clean stop
# ===========================================================================


class TestDrain:
    @pytest.mark.asyncio
    async def test_drains_frames_to_manager_then_stops_on_sentinel(self):
        q = _queue.Queue()
        mgr = _FakeManager()
        q.put({"kind": "why_snapshot", "op_id": "a"})
        q.put({"kind": "telemetry", "op_id": "a"})
        stop_gateway_queue(q)  # pushes the _STOP sentinel
        n = await asyncio.wait_for(drain_queue_to_manager(q, mgr), timeout=5)
        assert n == 2
        assert [f["kind"] for f in mgr.frames] == ["why_snapshot", "telemetry"]

    @pytest.mark.asyncio
    async def test_bad_frame_never_kills_drain(self):
        class _BoomOnce:
            def __init__(self):
                self.frames = []
                self._first = True
            async def broadcast(self, frame):
                if self._first:
                    self._first = False
                    raise RuntimeError("boom")
                self.frames.append(frame)
        q = _queue.Queue()
        mgr = _BoomOnce()
        q.put({"n": 1})  # raises in broadcast → swallowed
        q.put({"n": 2})  # still delivered
        q.put(TB._STOP)
        await asyncio.wait_for(drain_queue_to_manager(q, mgr), timeout=5)
        assert mgr.frames == [{"n": 2}]


# ===========================================================================
# Decoupled bridge subscriber — routes lifecycle events to the queue
# ===========================================================================


class TestQueueBridge:
    @pytest.mark.asyncio
    async def test_subscriber_publishes_frames_to_queue(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COGNITIVE_OBSERVABILITY_ENABLED", "1")
        q = _BoundedFakeQueue(32)
        sub = TB.build_queue_publishing_subscriber(q)
        assert sub is not None
        assert sub.label == "telemetry_broker_queue"
        ev = _FakeEvent({"lifecycle_kind": "post_apply", "op_id": "op-7",
                         "phase": "APPLY", "confidence": 0.9})
        await sub.handler(ev)
        # Frames (why_snapshot + telemetry) were routed to the cross-process
        # queue — NOT to any in-process manager.
        kinds = [q.get_nowait()["kind"] for _ in range(len(q._items))]
        assert "why_snapshot" in kinds
        assert "telemetry" in kinds
