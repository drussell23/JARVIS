"""Slice 101 Phase 1 — Cognitive Integration Bus transport adapter.

Proves the thin adapter over the real ``TrinityEventBus``:
  * inert when the master flag is off (publish no-op, register []),
  * inert on unknown kind / no running bus (never creates a bus from hot path),
  * end-to-end delivery of a lifecycle event to a real subscriber,
  * a raising subscriber is fault-isolated and never propagates.
"""

from __future__ import annotations

import asyncio

from backend.core.ouroboros.governance import cognitive_bus as CB
from backend.core.trinity_event_bus import (
    EventPriority,
    get_trinity_event_bus,
    shutdown_trinity_event_bus,
)


async def _poll_until(predicate, *, timeout_s: float = 3.0, step_s: float = 0.02) -> bool:
    waited = 0.0
    while waited < timeout_s:
        if predicate():
            return True
        await asyncio.sleep(step_s)
        waited += step_s
    return predicate()


# --- inert paths ------------------------------------------------------------

def test_master_off_publish_is_noop(monkeypatch):
    monkeypatch.delenv(CB._ENV_ENABLED, raising=False)
    assert CB.cognitive_bus_enabled() is False
    assert CB.publish_lifecycle_event(CB.LIFECYCLE_POST_FAILURE, {"op_id": "x"}) is False


def test_master_off_register_is_empty(monkeypatch):
    monkeypatch.delenv(CB._ENV_ENABLED, raising=False)

    async def _run():
        sub = CB.CognitiveSubscriber("noop", CB.lifecycle_pattern(), lambda e: asyncio.sleep(0))
        return await CB.register_cognitive_subscribers([sub])

    assert asyncio.run(_run()) == []


def test_unknown_kind_is_rejected(monkeypatch):
    monkeypatch.setenv(CB._ENV_ENABLED, "1")
    assert CB.cognitive_bus_enabled() is True
    # Unknown kind short-circuits before any bus interaction.
    assert CB.publish_lifecycle_event("not_a_real_kind", {"op_id": "x"}) is False


def test_master_on_but_no_bus_is_noop(monkeypatch):
    monkeypatch.setenv(CB._ENV_ENABLED, "1")

    async def _run():
        await shutdown_trinity_event_bus()  # ensure no global bus exists
        # Known kind, master on, running loop present — but no bus booted.
        return CB.publish_lifecycle_event(CB.LIFECYCLE_PRE_APPLY, {"op_id": "x"})

    assert asyncio.run(_run()) is False


# --- end-to-end delivery ----------------------------------------------------

def test_lifecycle_event_delivered_to_subscriber(monkeypatch):
    monkeypatch.setenv(CB._ENV_ENABLED, "1")

    received = []

    async def _handler(event):
        received.append(event)

    async def _run():
        await shutdown_trinity_event_bus()
        bus = await get_trinity_event_bus()
        try:
            sub = CB.CognitiveSubscriber("recorder", CB.lifecycle_pattern(), _handler)
            ids = await CB.register_cognitive_subscribers([sub], bus=bus)
            assert len(ids) == 1

            scheduled = CB.publish_lifecycle_event(
                CB.LIFECYCLE_POST_FAILURE,
                {"op_id": "op-123", "phase": "GENERATE"},
                priority=EventPriority.HIGH,
            )
            assert scheduled is True

            ok = await _poll_until(lambda: len(received) >= 1)
            assert ok, "lifecycle event was never delivered"
            evt = received[0]
            assert evt.topic == CB.lifecycle_topic(CB.LIFECYCLE_POST_FAILURE)
            assert evt.payload["op_id"] == "op-123"
            assert evt.payload["phase"] == "GENERATE"
            # publisher stamps the kind for subscriber convenience
            assert evt.payload["lifecycle_kind"] == CB.LIFECYCLE_POST_FAILURE
        finally:
            await shutdown_trinity_event_bus()

    asyncio.run(_run())


def test_raising_subscriber_is_fault_isolated(monkeypatch):
    monkeypatch.setenv(CB._ENV_ENABLED, "1")

    good_received = []

    async def _bad_handler(event):
        raise RuntimeError("subscriber boom")

    async def _good_handler(event):
        good_received.append(event)

    async def _run():
        await shutdown_trinity_event_bus()
        bus = await get_trinity_event_bus()
        try:
            subs = [
                CB.CognitiveSubscriber("bad", CB.lifecycle_pattern(), _bad_handler),
                CB.CognitiveSubscriber("good", CB.lifecycle_pattern(), _good_handler),
            ]
            ids = await CB.register_cognitive_subscribers(subs, bus=bus)
            assert len(ids) == 2

            # Unique payload avoids the bus dedup window.
            CB.publish_lifecycle_event(CB.LIFECYCLE_PRE_APPLY, {"op_id": "op-fault-iso"})
            ok = await _poll_until(lambda: len(good_received) >= 1)
            assert ok, "good subscriber starved by a raising sibling"
        finally:
            await shutdown_trinity_event_bus()

    asyncio.run(_run())


def test_topic_helpers_are_consistent():
    assert CB.lifecycle_topic(CB.LIFECYCLE_TOOL_EMIT) == "ouroboros.lifecycle.tool_emit"
    assert CB.lifecycle_pattern() == "ouroboros.lifecycle.*"
    assert CB.is_lifecycle_kind(CB.LIFECYCLE_SESSION_END) is True
    assert CB.is_lifecycle_kind("bogus") is False
