"""Governance→SSE bridge (Phase 1 + Phase 10 backpressure) spine.

The bridge now enqueues onto a bounded backpressure queue drained by a
single pump that CONFLATES floods. Tests drive the pump and assert the
DaemonEvent contract + the buffer-bloat guard under a 200-event storm.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.api import governance_sse_bridge as gsb


class _FakeBus:
    """Mirrors TrinityEventBus.subscribe with real wildcard routing."""
    def __init__(self):
        self.handlers = {}; self._n = 0; self.unsubscribed = []

    async def subscribe(self, pattern, handler, **kw):
        self._n += 1; sid = f"sub-{self._n}"
        self.handlers[sid] = (pattern, handler); return sid

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id); self.handlers.pop(sub_id, None); return True

    @staticmethod
    def _matches(pattern, topic):
        if pattern.endswith(".#"):
            return topic == pattern[:-2] or topic.startswith(pattern[:-1])
        return pattern == topic

    async def fire(self, event):
        topic = str(getattr(event, "topic", "") or "")
        for pat, h in list(self.handlers.values()):
            if self._matches(pat, topic):
                await h(event)


class _FakeEventStream:
    def __init__(self, sessions=1):
        self.sessions = sessions; self.broadcasts = []
    async def broadcast_event(self, channel, payload):
        self.broadcasts.append((channel, payload)); return self.sessions


class _Event:
    def __init__(self, topic, payload): self.topic = topic; self.payload = payload


@pytest.fixture(autouse=True)
def _fast_drain(monkeypatch):
    # Tiny drain window + low conflate threshold for fast, deterministic tests.
    monkeypatch.setenv("JARVIS_GOVERNANCE_SSE_DRAIN_WINDOW_S", "0.02")
    monkeypatch.setenv("JARVIS_GOVERNANCE_SSE_CONFLATE_THRESHOLD", "10")
    yield
    asyncio.get_event_loop().run_until_complete(gsb.reset_governance_sse_bridge())


def _wire(monkeypatch, bus, es):
    monkeypatch.setattr("backend.core.trinity_event_bus.get_event_bus_if_exists",
                        lambda: bus, raising=False)
    monkeypatch.setattr("backend.core.event_stream.get_event_stream_if_initialized",
                        lambda: es, raising=False)


async def _drain(seconds=0.15):
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# MANDATE 1/2 — a real autonomy event yields the DaemonEvent contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_op_completed_forwarded_as_daemon_contract(monkeypatch):
    bus, es = _FakeBus(), _FakeEventStream(sessions=1)
    _wire(monkeypatch, bus, es)
    bridge = await gsb.install_governance_sse_bridge()
    assert bridge is not None and bridge.installed

    await bus.fire(_Event("autonomy.op_completed",
                          {"op_id": "op-42", "success": True}))
    await _drain()

    assert len(es.broadcasts) == 1
    channel, payload = es.broadcasts[0]
    assert channel == "governance"
    # The EXACT Swift DaemonEvent Codable keys (all four, all strings).
    for k in ("command_id", "narration_text", "narration_priority",
              "source_brain"):
        assert k in payload and isinstance(payload[k], str)
    assert payload["command_id"] == "op-42"
    assert payload["source_brain"] == "ouroboros"
    assert payload["type"] == "ov_activity"        # extra keys flattened top-level
    # detail rides along for the HUD's expand view
    assert payload["detail"]["success"] is True


@pytest.mark.asyncio
async def test_failure_escalates_priority(monkeypatch):
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    await gsb.install_governance_sse_bridge()
    await bus.fire(_Event("autonomy.op_failed", {"op_id": "x", "success": False}))
    await _drain()
    assert es.broadcasts[0][1]["narration_priority"] == "high"


# ---------------------------------------------------------------------------
# MANDATE 4 — 200-event flood → conflation catches it (buffer-bloat guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_event_flood_is_conflated_not_1to1(monkeypatch):
    """Blast 200 autonomy.log events. The pump must conflate the flood into
    a FEW summary frames — NOT 200 individual broadcasts (buffer bloat)."""
    bus, es = _FakeBus(), _FakeEventStream(sessions=1)
    _wire(monkeypatch, bus, es)
    bridge = await gsb.install_governance_sse_bridge()

    # Fire 200 events as fast as possible (simulated storm).
    for i in range(200):
        await bus.fire(_Event("autonomy.log", {"op_id": f"op-{i}", "seq": i}))
    await _drain(0.3)   # let the pump drain + conflate

    # The buffer-bloat guard: far fewer broadcasts than events.
    assert len(es.broadcasts) < 50, f"no conflation: {len(es.broadcasts)} broadcasts"
    assert bridge.stats["conflated"] > 0
    assert bridge.stats["enqueued"] >= 200
    # At least one broadcast is a conflated summary carrying the count.
    conflated = [p for _, p in es.broadcasts
                 if p.get("type") == "ov_activity_batch"]
    assert conflated, "expected a conflated batch frame"
    assert conflated[0]["conflated"] >= 10
    # Every conflated frame is STILL a valid DaemonEvent (all 4 keys).
    for _, p in es.broadcasts:
        for k in ("command_id", "narration_text", "narration_priority",
                  "source_brain"):
            assert k in p


@pytest.mark.asyncio
async def test_flood_does_not_block_the_event_loop(monkeypatch):
    """The bus handler must return immediately (enqueue), never awaiting a
    broadcast — so a storm can't block the event loop / the bus."""
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    await gsb.install_governance_sse_bridge()

    t0 = asyncio.get_event_loop().time()
    for i in range(200):
        await bus.fire(_Event("autonomy.log", {"op_id": str(i)}))
    enqueue_elapsed = asyncio.get_event_loop().time() - t0
    # Enqueuing 200 events is near-instant (no per-event broadcast await).
    assert enqueue_elapsed < 0.5


@pytest.mark.asyncio
async def test_small_batch_forwarded_1to1(monkeypatch):
    """Below the threshold, events are NOT conflated — each forwarded."""
    bus, es = _FakeEventStream, None  # placeholder to avoid lint
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    await gsb.install_governance_sse_bridge()
    for i in range(3):
        await bus.fire(_Event("autonomy.op_completed", {"op_id": str(i)}))
    await _drain()
    # 3 < threshold(10) → 3 individual daemon frames, no conflation.
    assert len(es.broadcasts) == 3
    assert all(p.get("type") == "ov_activity" for _, p in es.broadcasts)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uninstall_stops_pump_and_releases_subs(monkeypatch):
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    bridge = await gsb.install_governance_sse_bridge()
    n = len(bus.handlers)
    await gsb.reset_governance_sse_bridge()
    assert len(bus.unsubscribed) == n
    assert bridge._pump_task is None or bridge._pump_task.cancelled() or bridge._pump_task.done()


@pytest.mark.asyncio
async def test_no_stream_no_crash(monkeypatch):
    bus = _FakeBus()
    monkeypatch.setattr("backend.core.trinity_event_bus.get_event_bus_if_exists",
                        lambda: bus, raising=False)
    monkeypatch.setattr("backend.core.event_stream.get_event_stream_if_initialized",
                        lambda: None, raising=False)
    await gsb.install_governance_sse_bridge()
    await bus.fire(_Event("autonomy.op_completed", {"op_id": "x"}))
    await _drain()  # pump runs, no stream → no crash


# ---------------------------------------------------------------------------
# Slice F — lifecycle telemetry forwards the discriminator + rich narration
# ---------------------------------------------------------------------------

def test_render_forwards_lifecycle_discriminator_and_narration():
    """A SYSTEM_HYDRATING event must reach the HUD as a daemon frame carrying
    the ``lifecycle`` discriminator + its own rich narration — NOT overwritten
    with a generic "O+V: hydration". This is what the native Adaptive UI State
    Machine reacts to."""
    ev = _Event("ouroboros.hydration", {
        "type": "SYSTEM_HYDRATING", "op_id": "hydration",
        "narration_text": "Waking Ouroboros…", "narration_priority": "normal",
        "source_brain": "supervisor", "state": "hydrating"})
    payload = gsb.GovernanceSSEBridge._render(ev)
    assert payload["lifecycle"] == "SYSTEM_HYDRATING"
    assert payload["narration_text"] == "Waking Ouroboros…"   # NOT "O+V: hydration"
    assert payload["source_brain"] == "supervisor"
    assert payload["state"] == "hydrating"
    # Swift DaemonEvent required keys still present + string-typed.
    for k in ("command_id", "narration_text", "narration_priority", "source_brain"):
        assert isinstance(payload[k], str)


def test_render_forwards_degraded_and_fault_lifecycles():
    for raw in ("SYSTEM_DEGRADED", "OUROBOROS_FAULT", "SYSTEM_READY"):
        ev = _Event("ouroboros.system", {
            "type": raw, "narration_text": f"n:{raw}",
            "narration_priority": "high", "source_brain": "supervisor"})
        p = gsb.GovernanceSSEBridge._render(ev)
        assert p["lifecycle"] == raw
        assert p["narration_text"] == f"n:{raw}"


def test_render_generic_activity_has_no_lifecycle_key():
    """A plain governance op (no narration_text) keeps the legacy generic
    rendering and carries NO lifecycle discriminator."""
    ev = _Event("autonomy.op_completed", {"op_id": "op-9", "success": True})
    p = gsb.GovernanceSSEBridge._render(ev)
    assert "lifecycle" not in p
    assert p["type"] == "ov_activity"
    assert p["narration_text"].startswith("O+V:")
