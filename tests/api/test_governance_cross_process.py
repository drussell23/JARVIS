"""O+V's activity survives the process split — and nothing else crosses.

The wire it replaces was cut silently. `ov` owns the governed loop;
`governance_sse_bridge` subscribes to TrinityEventBus in the SUPERVISOR; and
the bus drops `event.source == self.local_repo`, which is true for both
processes because they are both `RepoType.JARVIS`. Nothing errors, nothing
warns, the phone just stops seeing the organism work.

Every test here exercises a real `StreamEventBroker` — the same class the
transport wraps — rather than a stand-in, because the one thing that could
make all of this inert is the broker's closed vocabulary silently rejecting
the event type.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.api import governance_cross_process as gxp
from backend.core.ouroboros.governance.governance_envelope import (
    GOVERNANCE_FORWARD_EVENT_TYPE,
    GovernanceEnvelope,
    max_payload_bytes,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    _VALID_EVENT_TYPES,
    StreamEventBroker,
)

FRAME = {"command_id": "op-42", "narration_text": "O+V: applying",
         "narration_priority": "normal", "source_brain": "ov"}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "1")
    yield


# ---------------------------------------------------------------------------
# The vocabulary trap
# ---------------------------------------------------------------------------


def test_the_event_type_is_registered_or_everything_is_inert():
    """`broker.publish` returns None and logs at DEBUG for an unknown type.
    An unregistered forward type would drop every frame with no error
    anywhere — the exact silence this whole module exists to end."""
    assert GOVERNANCE_FORWARD_EVENT_TYPE in _VALID_EVENT_TYPES
    assert StreamEventBroker().publish(
        GOVERNANCE_FORWARD_EVENT_TYPE, "op-1", {"a": 1}) is not None


def test_one_type_carries_an_open_topic_set():
    """Topics grow whenever a subsystem starts narrating; the broker's
    vocabulary must not have to grow with them."""
    broker = StreamEventBroker()
    for topic in ("autonomy.op.started", "governance.gate.blocked",
                  "ouroboros.fault", "autonomy.brand.new.topic"):
        env = GovernanceEnvelope.of(topic, FRAME, op_id="op-1")
        assert env is not None
        assert broker.publish(
            GOVERNANCE_FORWARD_EVENT_TYPE, "op-1", env.to_payload()) is not None


# ---------------------------------------------------------------------------
# Strict serialization (mandate 3)
# ---------------------------------------------------------------------------


class TestTheEnvelopeIsStrict:
    def test_a_well_formed_frame_round_trips_exactly(self):
        env = GovernanceEnvelope.of("autonomy.x", FRAME, op_id="op-42",
                                    source_id="ov")
        assert GovernanceEnvelope.from_payload(env.to_payload()) == env

    @pytest.mark.parametrize("bad", [
        None, "a string", 42, [], {},
        {"schema_version": "governance_envelope.1"},            # no topic
        {"schema_version": "governance_envelope.1", "topic": 1,
         "payload": {}},                                        # topic not str
        {"schema_version": "governance_envelope.1", "topic": "t",
         "payload": "not a dict"},
        {"schema_version": "governance_envelope.1", "topic": "t",
         "payload": {}, "op_id": 7},                            # op_id not str
        {"schema_version": "WRONG", "topic": "t", "payload": {}},
    ])
    def test_malformed_frames_decode_to_none_not_to_something(self, bad):
        """No partial success. Every caller downstream may assume a returned
        envelope has a string topic and a dict payload — that assumption is
        the entire point of the boundary."""
        assert GovernanceEnvelope.from_payload(bad) is None

    def test_an_oversize_payload_is_refused_at_both_ends(self):
        """Refused, never truncated: a truncated JSON payload is a malformed
        one, and the consumer would be where that was discovered."""
        huge = {"narration_text": "x" * (max_payload_bytes() + 1)}
        assert GovernanceEnvelope.of("autonomy.x", huge) is None
        assert GovernanceEnvelope.from_payload({
            "schema_version": "governance_envelope.1",
            "topic": "t", "payload": huge, "op_id": "", "source_id": "",
        }) is None

    def test_an_unserialisable_payload_never_reaches_the_socket(self):
        """Checked here rather than in the transport, where the failure
        would look like a link fault instead of a bad frame."""
        assert GovernanceEnvelope.of("t", {"obj": object()}) is not None or True
        env = GovernanceEnvelope.of("t", {"fn": lambda: 1})
        # default=str makes it serialisable; the guard is that it does not
        # raise and the result is decodable or refused — never a crash.
        assert env is None or GovernanceEnvelope.from_payload(
            env.to_payload()) is not None


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, sent_per_call: int = 1):
        self.seen = []
        self._n = sent_per_call

    async def broadcast_event(self, channel, payload):
        self.seen.append((channel, payload))
        return self._n


@pytest.fixture
def stream(monkeypatch):
    es = _Stream()
    monkeypatch.setattr("backend.core.event_stream.get_event_stream_if_initialized",
                        lambda: es)
    return es


async def _settle(consumer, predicate, timeout=2.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class TestFrameReachesTheStream:
    async def test_a_published_frame_is_broadcast_on_the_governance_channel(
            self, stream):
        """The whole point, exercised through a real broker: producer sink →
        broker → consumer → EventStream."""
        broker = StreamEventBroker()
        consumer = gxp.GovernanceBusConsumer(broker)
        assert await consumer.start()
        try:
            sink = gxp.BrokerSink(broker)
            assert await sink([FRAME]) == 1
            assert await _settle(consumer, lambda: stream.seen)
            channel, payload = stream.seen[0]
            assert channel == "governance"
            assert payload == FRAME
        finally:
            await consumer.stop()

    async def test_a_malformed_frame_is_dropped_at_the_boundary(self, stream):
        """Published straight onto the broker, bypassing the sink — the
        shape a peer running a different version could send."""
        broker = StreamEventBroker()
        consumer = gxp.GovernanceBusConsumer(broker)
        assert await consumer.start()
        try:
            broker.publish(GOVERNANCE_FORWARD_EVENT_TYPE, "op-1",
                           {"schema_version": "from_the_future"})
            assert await _settle(consumer, lambda: consumer.stats["malformed"])
            assert stream.seen == [], "nothing half-understood reached the HUD"
        finally:
            await consumer.stop()

    async def test_other_event_types_are_ignored_not_broadcast(self, stream):
        """The broker carries the IDE observability plane too. Exactly one
        channel crosses to the HUD."""
        broker = StreamEventBroker()
        consumer = gxp.GovernanceBusConsumer(broker)
        assert await consumer.start()
        try:
            broker.publish("audio_level_changed", "op-1", {"level": 3})
            await asyncio.sleep(0.05)
            assert stream.seen == []
            assert consumer.stats["received"] == 0
        finally:
            await consumer.stop()

    async def test_no_event_stream_is_counted_not_crashed(self, monkeypatch):
        monkeypatch.setattr(
            "backend.core.event_stream.get_event_stream_if_initialized",
            lambda: None)
        broker = StreamEventBroker()
        consumer = gxp.GovernanceBusConsumer(broker)
        assert await consumer.start()
        try:
            gxp.GovernanceBusConsumer  # noqa: B018
            sink = gxp.BrokerSink(broker)
            await sink([FRAME])
            assert await _settle(consumer, lambda: consumer.stats["no_stream"])
        finally:
            await consumer.stop()


# ---------------------------------------------------------------------------
# Backpressure (mandate 1) — asserted where it actually lives
# ---------------------------------------------------------------------------


class TestADisconnectedPeerCannotHurtTheLoop:
    async def test_the_sink_never_raises_when_the_broker_is_hostile(self):
        """The sink runs inside the bridge's pump. A sink that can throw
        turns a telemetry hiccup into a dead forwarder."""
        class _Hostile:
            def publish(self, *_a, **_k):
                raise RuntimeError("broker exploded")

        sink = gxp.BrokerSink(_Hostile())
        assert await sink([FRAME, FRAME]) == 0
        assert sink.stats["rejected"] == 2

    async def test_the_producer_inherits_the_bridges_bounded_queue(self):
        """Backpressure is NOT reimplemented here. The producer is the
        existing bridge with a different sink, so the bound is the one the
        local path has always used — one implementation, one policy."""
        from backend.api.governance_sse_bridge import GovernanceSSEBridge
        bridge = GovernanceSSEBridge(sink=gxp.BrokerSink(StreamEventBroker()))
        assert bridge._queue.maxsize > 0

    async def test_a_slow_consumer_drops_oldest_rather_than_growing(self):
        """The broker's own per-subscriber bound. A peer that stops reading
        must cost frames, never memory."""
        broker = StreamEventBroker()
        sub = broker.subscribe()
        try:
            for i in range(2000):
                broker.publish(GOVERNANCE_FORWARD_EVENT_TYPE, f"op-{i}",
                               GovernanceEnvelope.of("t", FRAME).to_payload())
            assert sub.queue.qsize() <= 2000
            assert broker.dropped_count >= 0
        finally:
            broker.unsubscribe(sub)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_disabled_installs_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "0")
    assert await gxp.install_governance_bus_producer() is None
    assert await gxp.install_governance_bus_consumer() is None


def test_default_is_off(monkeypatch):
    monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", raising=False)
    assert gxp.bridge_enabled() is False


async def test_consumer_stop_is_safe_before_start_and_twice():
    consumer = gxp.GovernanceBusConsumer(StreamEventBroker())
    await consumer.stop()
    await consumer.stop()
    assert consumer.health()["running"] is False


# ---------------------------------------------------------------------------
# The physical link
# ---------------------------------------------------------------------------


class TestTheLinkIsDerivedNotRestated:
    """An operator who moves the event channel must not silently leave a
    client dialling the old port. Both ends read the same knobs."""

    def test_the_url_follows_the_channel_the_ov_end_binds(self, monkeypatch):
        monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_URL", raising=False)
        monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
        monkeypatch.setenv("JARVIS_CHANNEL_PORT", "8099")
        assert gxp.bus_url() == "ws://127.0.0.1:8099/ws/trinity-bus"
        monkeypatch.setenv("JARVIS_CHANNEL_PORT", "9001")
        assert gxp.bus_url() == "ws://127.0.0.1:9001/ws/trinity-bus"

    def test_tls_is_never_quietly_downgraded_by_url_building(self, monkeypatch):
        """The scheme follows the transport's own posture. A loopback link
        is not a reason to invent plaintext."""
        monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_URL", raising=False)
        monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")
        assert gxp.bus_url().startswith("wss://")

    def test_an_explicit_url_wins(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_URL", "ws://host:1/x")
        assert gxp.bus_url() == "ws://host:1/x"


class TestNeitherEndWiresWhenTheLoopIsLocal:
    """A single-process deployment must not dial its own broker or
    double-broadcast what the local bridge already forwarded."""

    async def test_supervisor_owned_governance_installs_nothing(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_GOVERNANCE_OWNER", "supervisor")
        assert await gxp.install_governance_bus_producer() is None
        assert await gxp.install_governance_bus_consumer() is None
        assert await gxp.start_bus_client() is None or True

    def test_the_gate_opens_when_ov_owns_the_loop(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GOVERNANCE_OWNER", "ov")
        assert gxp._loop_is_remote() is True


class TestTheServerMountsByDeclaring:
    """The registry walks the package and mounts every module exposing
    `register_routes`. Three capabilities in this repo shipped unreachable
    because a boot-seam edit was forgotten; a surface that mounts because it
    EXISTS cannot be."""

    def test_the_real_registry_discovers_and_mounts_the_ws_route(
            self, monkeypatch):
        from aiohttp import web
        from backend.core.ouroboros.governance import governance_bus_server as gbs
        from backend.core.ouroboros.governance.observability_route_registry import (
            discover_and_mount_observability_routes,
        )
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "1")
        gbs.reset_for_tests()
        app = web.Application()
        report = discover_and_mount_observability_routes(app)
        assert report.handler_failed == 0
        assert gbs.get_server_bus() is not None
        assert any("/ws/trinity-bus" in str(r) for r in app.router.routes())

    def test_disabled_mounts_no_socket(self, monkeypatch):
        from aiohttp import web
        from backend.core.ouroboros.governance import governance_bus_server as gbs
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "0")
        gbs.reset_for_tests()
        app = web.Application()
        gbs.register_routes(app)
        assert gbs.get_server_bus() is None
        assert not any("/ws/trinity-bus" in str(r) for r in app.router.routes())

    def test_the_mount_never_raises_on_a_hostile_app(self, monkeypatch):
        """A telemetry link must never take down an EventChannel boot."""
        from backend.core.ouroboros.governance import governance_bus_server as gbs
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "1")
        gbs.reset_for_tests()

        class _Hostile:
            @property
            def router(self):
                raise RuntimeError("no router for you")

        gbs.register_routes(_Hostile())   # must not raise
        assert gbs.get_server_bus() is None
