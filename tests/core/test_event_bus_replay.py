"""Slice H — Temporal Replay Buffer (state reconciliation) in TrinityEventBus.

A client (ov panel / HUD) that attaches long after DAG hydration must be
reconciled from an in-memory ring buffer BEFORE it starts receiving live
events — no disk, no polling.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core import trinity_event_bus as teb


async def _make_bus():
    bus = teb.TrinityEventBus(teb.RepoType.JARVIS)
    await bus.start()
    return bus


async def _drain(seconds=0.05):
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# MANDATE 4 — a DELAYED subscriber receives the history before any live event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delayed_subscriber_reconciles_history_before_live():
    bus = await _make_bus()
    try:
        # 1. Publish three critical lifecycle events (no subscriber yet).
        await bus.publish_raw("ouroboros.hydration",
                              {"type": "SYSTEM_HYDRATING", "seq": 1}, target=teb.RepoType.JARVIS, persist=False)
        await bus.publish_raw("ouroboros.hydration",
                              {"type": "SYSTEM_HYDRATING", "seq": 2}, target=teb.RepoType.JARVIS, persist=False)
        await bus.publish_raw("ouroboros.system",
                              {"type": "SYSTEM_READY", "seq": 3}, target=teb.RepoType.JARVIS, persist=False)

        # 2. Wait 100ms so the live queue has fully drained — these events now
        #    exist ONLY in the replay buffer.
        await asyncio.sleep(0.1)

        # 3. A delayed subscriber attaches.
        received = []
        async def handler(event):
            received.append((event.topic, event.payload.get("seq"),
                             event.payload.get("type")))

        await bus.subscribe("ouroboros.#", handler)

        # 4. It IMMEDIATELY received the three historical events, in order,
        #    from the buffer — before any live event.
        assert [r[1] for r in received] == [1, 2, 3], received
        assert received[0][2] == "SYSTEM_HYDRATING"
        assert received[2][2] == "SYSTEM_READY"

        # 5. A subsequent LIVE event arrives AFTER the reconciled history.
        await bus.publish_raw("ouroboros.fault",
                              {"type": "OUROBOROS_FAULT", "seq": 4}, target=teb.RepoType.JARVIS, persist=False)
        await _drain()
        assert [r[1] for r in received] == [1, 2, 3, 4]
        assert received[3][2] == "OUROBOROS_FAULT"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_history_precedes_live_even_when_event_races_the_flush():
    """No duplication + strict ordering when a live event arrives DURING the
    reconciliation flush (the hand-off window)."""
    bus = await _make_bus()
    try:
        await bus.publish_raw("ouroboros.hydration", {"seq": 1}, target=teb.RepoType.JARVIS, persist=False)
        await bus.publish_raw("ouroboros.hydration", {"seq": 2}, target=teb.RepoType.JARVIS, persist=False)
        await asyncio.sleep(0.05)

        order = []
        slow_gate = asyncio.Event()

        async def handler(event):
            order.append(event.payload.get("seq"))
            # Stall on the FIRST replayed event so a live event can race in.
            if event.payload.get("seq") == 1:
                await slow_gate.wait()

        sub_task = asyncio.create_task(bus.subscribe("ouroboros.#", handler))
        await asyncio.sleep(0.02)                       # handler is mid-flush
        # A live event arrives while the subscriber is still priming.
        await bus.publish_raw("ouroboros.system", {"seq": 99}, target=teb.RepoType.JARVIS, persist=False)
        await asyncio.sleep(0.02)
        slow_gate.set()
        await sub_task
        await _drain()

        # Historical (1,2) delivered before the live (99); no dupes.
        assert order == [1, 2, 99], order
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_non_critical_topics_are_not_replayed():
    bus = await _make_bus()
    try:
        await bus.publish_raw("autonomy.op_completed", {"op": "x"}, target=teb.RepoType.JARVIS, persist=False)
        await bus.publish_raw("some.other.topic", {"a": 1}, target=teb.RepoType.JARVIS, persist=False)
        await asyncio.sleep(0.05)

        received = []
        async def handler(event):
            received.append(event.topic)
        await bus.subscribe("#", handler)     # matches everything
        await _drain()
        # Neither non-critical topic was buffered → no historical replay.
        assert received == []
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_replay_buffer_is_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_EVENT_REPLAY_MAXLEN", "5")
    bus = await _make_bus()
    try:
        for i in range(20):
            await bus.publish_raw("ouroboros.hydration", {"seq": i}, target=teb.RepoType.JARVIS, persist=False)
        await asyncio.sleep(0.05)

        received = []
        async def handler(event):
            received.append(event.payload.get("seq"))
        await bus.subscribe("ouroboros.#", handler)
        # Only the last 5 survive the ring (bounded, newest kept, in order).
        assert received == [15, 16, 17, 18, 19]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_only_matching_pattern_is_reconciled():
    bus = await _make_bus()
    try:
        await bus.publish_raw("ouroboros.hydration", {"seq": 1}, target=teb.RepoType.JARVIS, persist=False)
        await bus.publish_raw("ouroboros.fault", {"seq": 2}, target=teb.RepoType.JARVIS, persist=False)
        await asyncio.sleep(0.05)

        received = []
        async def handler(event):
            received.append(event.topic)
        # Subscribe ONLY to faults — hydration history must NOT be replayed.
        await bus.subscribe("ouroboros.fault", handler)
        assert received == ["ouroboros.fault"]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_every_subsystem_inherits_replay_via_publish():
    """DRY proof: replay is embedded in publish(), so a bare publish() (not
    publish_raw) is cached too — every existing publisher inherits it."""
    bus = await _make_bus()
    try:
        ev = teb.TrinityEvent(topic="ouroboros.selftest",
                              source=teb.RepoType.JARVIS,
                              payload={"type": "FAILOVER_PROVEN"})
        await bus.publish(ev, persist=False)
        await asyncio.sleep(0.05)

        received = []
        async def handler(event):
            received.append(event.payload.get("type"))
        await bus.subscribe("ouroboros.selftest", handler)
        assert received == ["FAILOVER_PROVEN"]
    finally:
        await bus.stop()
