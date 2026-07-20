"""Hive Aggregator — read-only fan-in multiplexer (Phase 12, Hive Step 1)."""
from __future__ import annotations

import asyncio

import pytest

from backend.api.hive_aggregator import HiveAggregator
from backend.api.hive_envelope import (
    HiveTelemetryEnvelope, from_ide_sse_event, from_trinity_event,
)


# ---------------------------------------------------------------------------
# mocks mirroring the REAL source contracts (native subscribe methods)
# ---------------------------------------------------------------------------

class _TrinityEvent:
    def __init__(self, topic, payload, event_id):
        self.topic = topic; self.payload = payload; self.event_id = event_id


class _MockTrinityBus:
    """Mirrors TrinityEventBus.subscribe(pattern, handler) + wildcard routing."""
    def __init__(self):
        self._subs = {}; self._n = 0; self.unsubscribed = []

    async def subscribe(self, pattern, handler, **kw):
        self._n += 1; sid = f"t-{self._n}"; self._subs[sid] = (pattern, handler); return sid

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id); self._subs.pop(sub_id, None); return True

    @staticmethod
    def _matches(pattern, topic):
        if pattern.endswith(".#"):
            base = pattern[:-2]
            return topic == base or topic.startswith(base + ".")
        return pattern == topic

    async def fire(self, topic, payload, event_id):
        ev = _TrinityEvent(topic, payload, event_id)
        for pat, h in list(self._subs.values()):
            if self._matches(pat, topic):
                await h(ev)


class _SseEvent:
    def __init__(self, event_type, op_id, payload, event_id):
        self.event_type = event_type; self.op_id = op_id
        self.payload = payload; self.event_id = event_id


class _MockSseSub:
    def __init__(self): self.queue = asyncio.Queue(); self._closed = False


class _MockSseBroker:
    """Mirrors StreamEventBroker.subscribe / stream_iter / unsubscribe."""
    def __init__(self): self.subs = []

    def subscribe(self, op_id_filter=None, last_event_id=None):
        s = _MockSseSub(); self.subs.append(s); return s

    def unsubscribe(self, sub):
        sub._closed = True

    async def stream_iter(self, sub, heartbeat_s=0):
        while not sub._closed:
            try:
                ev = await asyncio.wait_for(sub.queue.get(), timeout=0.5)
                yield ev
            except asyncio.TimeoutError:
                if sub._closed:
                    return
                continue

    async def publish(self, ev):
        for s in self.subs:
            await s.queue.put(ev)


# ---------------------------------------------------------------------------
# MANDATE 4 — 5 Trinity + 5 SSE, published simultaneously → all 10 captured,
# cast to envelopes, yielded in perfect chronological order, zero drops.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiplexer_merges_both_streams_in_chronological_order():
    bus, broker = _MockTrinityBus(), _MockSseBroker()
    agg = HiveAggregator(bus=bus, sse_broker=broker, sort_window_s=0.05)
    await agg.start()
    await asyncio.sleep(0.01)   # let subscriptions settle

    # Interleave the two fabrics with strictly increasing source timestamps.
    async def blast():
        # ts 1,3,5,7,9 → Trinity ; ts 2,4,6,8,10 → SSE
        for i in range(5):
            await bus.fire("autonomy.work_unit_state_changed",
                           {"ts": (2 * i + 1), "state": "running", "op_id": f"op{i}"},
                           event_id=f"t{i}")
            await broker.publish(_SseEvent(
                "gate_evaluated", f"op{i}",
                {"ts": (2 * i + 2), "narration_text": f"gate {i} PASS"}, f"s{i}"))

    await blast()
    await asyncio.sleep(0.2)     # > sort window: everything coalesces into one sorted batch

    got = await agg.drain_available()

    # 1. All 10 captured — nothing dropped.
    assert len(got) == 10, f"expected 10, got {len(got)}: {[e.ts for e in got]}"
    assert agg.stats["dropped_raw"] == 0 and agg.stats["dropped_out"] == 0
    # 2. Every item is a HiveTelemetryEnvelope.
    assert all(isinstance(e, HiveTelemetryEnvelope) for e in got)
    # 3. Perfect chronological order by source timestamp.
    ts_order = [e.ts for e in got]
    assert ts_order == sorted(ts_order) == [float(i) for i in range(1, 11)], ts_order
    # 4. Both fabrics are represented + correctly typed.
    fabrics = {e.source_fabric for e in got}
    assert fabrics == {"trinity", "ide_sse"}
    assert any(e.subsystem == "swarm" for e in got)        # autonomy.* → swarm
    assert any(e.subsystem == "governance" for e in got)   # gate_evaluated → governance

    await agg.stop()
    assert len(bus.unsubscribed) == len(_TrinityPatterns := [
        "training.#", "tier.#", "autonomy.#", "workflow.#", "gap.#",
        "fs.#", "command.#", "intake.#", "reactor.#", "degradation.#"])


@pytest.mark.asyncio
async def test_read_only_never_publishes_back_to_sources():
    """Mandate 1: the aggregator must be a pure listener — it must not call any
    publish method on the source bus/broker."""
    bus, broker = _MockTrinityBus(), _MockSseBroker()
    # Trip-wire: if the aggregator ever tries to publish, fail loudly.
    bus.publish = lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote to bus!"))
    agg = HiveAggregator(bus=bus, sse_broker=broker, sort_window_s=0.02)
    await agg.start()
    await bus.fire("gap.detected", {"ts": 1.0}, "g1")
    await asyncio.sleep(0.1)
    got = await agg.drain_available()
    assert len(got) == 1 and got[0].subsystem == "sensor"
    await agg.stop()


@pytest.mark.asyncio
async def test_neither_stream_blocks_the_other_under_load():
    """Fan-in stays live even if one source floods: blast 200 Trinity events;
    a lone SSE event must still make it through the merge."""
    bus, broker = _MockTrinityBus(), _MockSseBroker()
    agg = HiveAggregator(bus=bus, sse_broker=broker, sort_window_s=0.02, raw_max=64, out_max=4096)
    await agg.start()
    await asyncio.sleep(0.01)
    for i in range(200):
        await bus.fire("fs.changed.modified", {"ts": float(i)}, f"f{i}")
    await broker.publish(_SseEvent("tool_confidence", "opX", {"ts": 999.0}, "sX"))
    await asyncio.sleep(0.2)
    got = await agg.drain_available()
    # The SSE event survived despite the Trinity flood (proves independent fan-in).
    assert any(e.source_fabric == "ide_sse" and e.ts == 999.0 for e in got)
    await agg.stop()


# ---------------------------------------------------------------------------
# envelope adapters cast correctly + stay bridge-compatible (mandate 3)
# ---------------------------------------------------------------------------

def test_envelope_to_bus_payload_is_governance_bridge_compatible():
    env = from_ide_sse_event(event_type="gate_evaluated", op_id="op7",
                             payload={"narration_text": "gate PASS", "phase": "GATE"})
    p = env.to_bus_payload()
    # the exact keys governance_sse_bridge._render reads:
    for k in ("type", "narration_text", "source_brain", "narration_priority"):
        assert k in p
    assert p["narration_text"] == "gate PASS"
    assert p["type"] == "governance"


def test_trinity_swarm_lifecycle_maps_to_swarm_envelope():
    env = from_trinity_event(topic="autonomy.work_unit_state_changed",
                             payload={"state": "disposed", "op_id": "op1", "ts": 5.0})
    assert env.subsystem == "swarm"
    assert env.trace_id == "op1"
    assert env.ts == 5.0
