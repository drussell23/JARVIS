"""Governance→SSE bridge spine — the O+V ↔ JARVIS-Apple wire.

Mandate 4: proves the forwarder end-to-end with a REAL fake of both bus
contracts (no seams injected into the module under test), plus the
failure/edge behaviors that keep 24/7 residency honest.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.api import governance_sse_bridge as gsb


# --- real-contract fakes (mirror the ACTUAL bus signatures) ---

class _FakeBus:
    """Mirrors TrinityEventBus.subscribe/unsubscribe used by the bridge."""
    def __init__(self):
        self.handlers = {}          # sub_id -> (pattern, handler)
        self._n = 0
        self.unsubscribed = []

    async def subscribe(self, pattern, handler, **kw):
        self._n += 1
        sid = f"sub-{self._n}"
        self.handlers[sid] = (pattern, handler)
        return sid

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)
        self.handlers.pop(sub_id, None)
        return True

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        """Faithful to TrinityEventBus: 'autonomy.#' matches any topic
        under the 'autonomy.' prefix; '*' matches one level."""
        if pattern.endswith(".#"):
            return topic == pattern[:-2] or topic.startswith(pattern[:-1])
        if pattern.endswith(".*"):
            head = pattern[:-2]
            return (topic.startswith(head + ".")
                    and "." not in topic[len(head) + 1:])
        return pattern == topic

    async def fire(self, event):
        """Deliver an event ONLY to handlers whose pattern matches the
        event topic — exactly as the real bus routes."""
        topic = str(getattr(event, "topic", "") or "")
        for pat, h in list(self.handlers.values()):
            if self._matches(pat, topic):
                await h(event)


class _FakeEventStream:
    """Mirrors EventStream.broadcast_event(channel, payload) -> int."""
    def __init__(self, *, sessions=1):
        self.sessions = sessions
        self.broadcasts = []        # (channel, payload)

    async def broadcast_event(self, channel, payload):
        self.broadcasts.append((channel, payload))
        return self.sessions        # 0 = nobody attached


class _Event:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


@pytest.fixture(autouse=True)
def _reset():
    asyncio.get_event_loop().run_until_complete(gsb.reset_governance_sse_bridge())
    yield
    asyncio.get_event_loop().run_until_complete(gsb.reset_governance_sse_bridge())


def _wire(monkeypatch, bus, es):
    monkeypatch.setattr(
        "backend.core.trinity_event_bus.get_event_bus_if_exists",
        lambda: bus, raising=False,
    )
    monkeypatch.setattr(
        "backend.core.event_stream.get_event_stream_if_initialized",
        lambda: es, raising=False,
    )


@pytest.mark.asyncio
async def test_ov_event_reaches_the_sse_governance_channel(monkeypatch):
    """The core claim: an O+V autonomy event published to TrinityEventBus
    lands on the EventStream governance channel as an ov_activity frame."""
    bus, es = _FakeBus(), _FakeEventStream(sessions=1)
    _wire(monkeypatch, bus, es)

    bridge = await gsb.install_governance_sse_bridge()
    assert bridge is not None and bridge.installed

    await bus.fire(_Event("autonomy.op_completed",
                          {"op_id": "op-42", "success": True}))

    assert len(es.broadcasts) == 1
    channel, payload = es.broadcasts[0]
    assert channel == "governance"                 # the native client's channel
    assert payload["type"] == "ov_activity"
    assert payload["event"] == "op_completed"
    assert payload["op_id"] == "op-42"
    assert payload["detail"]["success"] is True    # raw O+V payload rides along
    assert bridge.stats["forwarded"] == 1


@pytest.mark.asyncio
async def test_no_native_client_is_a_cheap_noop_not_an_error(monkeypatch):
    bus, es = _FakeBus(), _FakeEventStream(sessions=0)   # nobody attached
    _wire(monkeypatch, bus, es)
    bridge = await gsb.install_governance_sse_bridge()

    await bus.fire(_Event("autonomy.op_started", {"op_id": "x"}))

    assert bridge.stats["forwarded"] == 0
    assert bridge.stats["dropped_no_stream"] == 1
    assert bridge.stats["forward_errors"] == 0     # NOT an error


@pytest.mark.asyncio
async def test_forward_fault_is_isolated_never_propagates(monkeypatch):
    class _BoomStream:
        async def broadcast_event(self, *a):
            raise ConnectionResetError("phone dropped mid-send")
    bus = _FakeBus()
    _wire(monkeypatch, bus, _BoomStream())
    bridge = await gsb.install_governance_sse_bridge()

    # Must NOT raise back into the O+V loop that fired the event.
    await bus.fire(_Event("autonomy.op_completed", {"op_id": "y"}))
    assert bridge.stats["forward_errors"] == 1


@pytest.mark.asyncio
async def test_install_is_idempotent(monkeypatch):
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    b1 = await gsb.install_governance_sse_bridge()
    n_after_first = len(bus.handlers)
    b2 = await gsb.install_governance_sse_bridge()
    assert b1 is b2                                 # same singleton
    assert len(bus.handlers) == n_after_first       # no double subscription


@pytest.mark.asyncio
async def test_install_returns_none_when_bus_absent(monkeypatch):
    monkeypatch.setattr(
        "backend.core.trinity_event_bus.get_event_bus_if_exists",
        lambda: None, raising=False,
    )
    assert await gsb.install_governance_sse_bridge() is None


@pytest.mark.asyncio
async def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("JARVIS_GOVERNANCE_SSE_BRIDGE_ENABLED", "false")
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    assert await gsb.install_governance_sse_bridge() is None


@pytest.mark.asyncio
async def test_no_feedback_loop_never_writes_back_to_bus(monkeypatch):
    """We read the bus and write the stream; the bus must receive NO
    publish from the bridge (else events echo forever)."""
    bus, es = _FakeBus(), _FakeEventStream()
    bus.published = []
    async def _publish_raw(*a, **k): bus.published.append((a, k))
    bus.publish_raw = _publish_raw
    _wire(monkeypatch, bus, es)
    await gsb.install_governance_sse_bridge()
    await bus.fire(_Event("autonomy.op_completed", {"op_id": "z"}))
    assert bus.published == []                      # zero writes back


@pytest.mark.asyncio
async def test_uninstall_releases_every_subscription(monkeypatch):
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    await gsb.install_governance_sse_bridge()
    n = len(bus.handlers)
    assert n >= 1
    await gsb.reset_governance_sse_bridge()
    assert len(bus.unsubscribed) == n               # all released
    assert gsb.get_governance_sse_bridge() is None


@pytest.mark.asyncio
async def test_malformed_event_does_not_crash_render(monkeypatch):
    bus, es = _FakeBus(), _FakeEventStream()
    _wire(monkeypatch, bus, es)
    bridge = await gsb.install_governance_sse_bridge()
    # A malformed event that DOES reach the handler (no topic, no dict
    # payload) must degrade to a safe frame, never crash.
    await bridge._on_event(object())
    assert len(es.broadcasts) == 1
    _, payload = es.broadcasts[0]
    assert payload["type"] == "ov_activity"
    assert payload["event"] == "activity"           # safe fallback label
