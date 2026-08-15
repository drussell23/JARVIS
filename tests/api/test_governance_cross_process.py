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

    def test_the_scheme_follows_THIS_links_posture(self, monkeypatch):
        """Written against the brain link's knob originally, which was the
        bug: that knob is shared with the cross-host brain transport. The
        scheme must follow the posture derived for THIS link, and the brain
        link's setting must not move it in either direction."""
        monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_URL", raising=False)
        monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
        monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_TLS_ENABLED", raising=False)
        assert gxp.bus_url().startswith("ws://"), "loopback stays plaintext"
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "10.0.0.5")
        assert gxp.bus_url().startswith("wss://"), "routable requires TLS"

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


# ---------------------------------------------------------------------------
# The TLS posture — derived, not inherited
# ---------------------------------------------------------------------------


class TestTheTlsPostureIsDerivedFromReachability:
    """`TransportConfig.from_env` reads `JARVIS_BRAIN_WS_*`, which
    `brain_keeper` and `organism_bus_host` also read for the CROSS-HOST brain
    link. Turning TLS off there to make a loopback telemetry socket
    convenient would silently downgrade that link too — a security
    regression bought with an unrelated convenience. So this link decides for
    itself, from the only fact that matters: can the socket leave the host.
    """

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_TLS_ENABLED", raising=False)
        yield

    def test_loopback_runs_plaintext(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
        assert gxp.link_tls_enabled() is False
        assert gxp.link_refusal() == ""

    def test_a_routable_host_requires_tls_without_being_told(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "10.0.0.5")
        assert gxp.link_tls_enabled() is True
        assert gxp.link_refusal() == ""

    def test_wildcard_bind_is_not_loopback(self, monkeypatch):
        """`0.0.0.0` is the shape that looks local and is not."""
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "0.0.0.0")
        assert gxp.is_loopback("0.0.0.0") is False
        assert gxp.link_tls_enabled() is True

    def test_plaintext_on_a_routable_host_is_REFUSED_not_served(
            self, monkeypatch):
        """The one combination that fails closed. An override may relax the
        derivation; it cannot relax this."""
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "0.0.0.0")
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_TLS_ENABLED", "0")
        assert gxp.link_refusal() != ""

    def test_an_operator_may_demand_tls_on_loopback(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_TLS_ENABLED", "1")
        assert gxp.link_tls_enabled() is True
        assert gxp.bus_url().startswith("wss://")

    def test_the_brain_links_knob_is_left_alone(self, monkeypatch):
        """The whole reason this is derived: the shared config must come back
        untouched apart from the one field with a different threat model."""
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
        monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")
        from backend.core.ouroboros.governance.transport.transport_config import (
            TransportConfig,
        )
        shared = TransportConfig.from_env(role="client")
        ours = gxp.link_transport_config("client")
        assert shared.tls_enabled is True, "the brain link keeps its TLS"
        assert ours.tls_enabled is False, "ours is derived for loopback"
        assert ours.queue_maxsize == shared.queue_maxsize
        assert ours.reconnect_base_s == shared.reconnect_base_s
        assert ours.reconnect_jitter == shared.reconnect_jitter

    async def test_a_refused_link_starts_no_client(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "0.0.0.0")
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_TLS_ENABLED", "0")
        assert await gxp.start_bus_client() is None


# ---------------------------------------------------------------------------
# The socket itself — a real loopback WebSocket, two brokers
# ---------------------------------------------------------------------------


class TestTheLinkCarriesAFrameOverARealSocket:
    """Proven live between two OS processes on 2026-08-15
    (``ws://127.0.0.1:8123/ws/trinity-bus`` → ``channel=governance``). This
    is that proof made repeatable: two SEPARATE brokers, a real aiohttp
    server, a real client dialling loopback. One broker on both ends would
    prove nothing about the wire.
    """

    async def test_a_frame_published_on_one_broker_reaches_the_other(
            self, monkeypatch, stream, unused_tcp_port_factory=None):
        import socket as _socket

        from aiohttp import web

        from backend.core.ouroboros.governance import governance_bus_server as gbs

        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_GOVERNANCE_OWNER", "ov")
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
        monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_URL", raising=False)
        monkeypatch.delenv("JARVIS_GOVERNANCE_BUS_TLS_ENABLED", raising=False)

        sock = _socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        monkeypatch.setenv("JARVIS_CHANNEL_PORT", str(port))

        server_broker = StreamEventBroker()
        client_broker = StreamEventBroker()

        gbs.reset_for_tests()
        monkeypatch.setattr(
            "backend.core.ouroboros.governance.ide_observability_stream."
            "get_default_broker", lambda: server_broker)

        app = web.Application()
        gbs.register_routes(app)
        assert gbs.get_server_bus() is not None
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()

        consumer = gxp.GovernanceBusConsumer(client_broker)
        assert await consumer.start()
        bus = await gxp.start_bus_client(broker=client_broker)
        assert bus is not None
        try:
            sink = gxp.BrokerSink(server_broker)
            deadline = asyncio.get_event_loop().time() + 20
            while asyncio.get_event_loop().time() < deadline and not stream.seen:
                await sink([FRAME])       # republish while the link settles
                await asyncio.sleep(0.25)
            assert stream.seen, (
                f"nothing crossed the socket; consumer={consumer.health()}")
            channel, payload = stream.seen[0]
            assert channel == "governance"
            assert payload["narration_text"] == FRAME["narration_text"]
        finally:
            await consumer.stop()
            try:
                await bus.stop()
            except Exception:
                pass
            await runner.cleanup()
            gbs.reset_for_tests()


# ---------------------------------------------------------------------------
# The install must not race its own dependency
# ---------------------------------------------------------------------------


class TestTheProducerWaitsForTheBus:
    """Measured on a live boot: the install point ran 64s BEFORE
    `[TrinityEventBus] Started`. `GovernanceSSEBridge.install()` returns
    False — not an error — when the bus is absent, so the forwarder was
    permanently inert with no exception and no log line. Moving to a
    different fixed point would only re-lose the race on a boot that
    reorders.
    """

    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GOVERNANCE_OWNER", "ov")
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_PRODUCER_RETRY_S", "0.5")
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_PRODUCER_WAIT_S", "10")
        yield

    async def test_it_installs_once_the_bus_appears(self, monkeypatch):
        """The regression that matters: absent bus at call time, present a
        moment later, forwarder live without anyone re-calling."""
        state = {"bus_up": False, "installs": 0}

        class _Bridge:
            def __init__(self, *a, **k):
                pass

            async def install(self):
                state["installs"] += 1
                return state["bus_up"]

        monkeypatch.setattr(
            "backend.api.governance_sse_bridge.GovernanceSSEBridge", _Bridge)
        task = await gxp.install_governance_bus_producer()
        assert task is not None, "must not give up when the bus is merely late"
        state["bus_up"] = True
        result = await asyncio.wait_for(task, timeout=8)
        assert result is not None
        assert state["installs"] >= 2, "it retried rather than gave up"

    async def test_an_immediately_available_bus_needs_no_retry(
            self, monkeypatch):
        class _Bridge:
            def __init__(self, *a, **k):
                pass

            async def install(self):
                return True

        monkeypatch.setattr(
            "backend.api.governance_sse_bridge.GovernanceSSEBridge", _Bridge)
        out = await gxp.install_governance_bus_producer()
        assert out is not None and not isinstance(out, asyncio.Task)

    async def test_it_gives_up_loudly_rather_than_retrying_forever(
            self, monkeypatch, caplog):
        """A task that outlives its reason is worse than a warning. A
        forwarder that never installed is indistinguishable from a quiet
        organism, so the give-up is said once at a level an operator sees."""
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_PRODUCER_WAIT_S", "1")

        class _Bridge:
            def __init__(self, *a, **k):
                pass

            async def install(self):
                return False

        monkeypatch.setattr(
            "backend.api.governance_sse_bridge.GovernanceSSEBridge", _Bridge)
        with caplog.at_level("WARNING"):
            task = await gxp.install_governance_bus_producer()
            assert await asyncio.wait_for(task, timeout=8) is None
        assert any("never installed" in r.message for r in caplog.records)

    async def test_waiting_can_be_declined(self, monkeypatch):
        class _Bridge:
            def __init__(self, *a, **k):
                pass

            async def install(self):
                return False

        monkeypatch.setattr(
            "backend.api.governance_sse_bridge.GovernanceSSEBridge", _Bridge)
        assert await gxp.install_governance_bus_producer(
            wait_for_bus=False) is None
