"""O+V's activity reaches the HUD after the two processes were split.

WHAT BROKE, AND WHY IT WAS INVISIBLE
------------------------------------
`ov` now owns the governed loop and `unified_supervisor` owns the body — the
mic, the vision plane, and the EventStream the Swift HUD reads. That split is
correct, and it silently cut one wire: ``governance_sse_bridge`` subscribes to
TrinityEventBus ``autonomy.# / governance.# / ouroboros.#`` in the SUPERVISOR,
and O+V now publishes those in the `ov` process.

The bus does cross processes — a live daemon logs ``[Transport] Multicast
enabled for jarvis`` — but its receive loop drops::

    if event.source == self.local_repo:      # trinity_event_bus
        continue

Both processes are ``RepoType.JARVIS``. The transport is cross-REPO
(jarvis↔prime↔reactor) and defines "self" by repo, not by PID, so the
supervisor discards every frame `ov` publishes as its own echo. The wire is
fine; the filter is the wall. Nothing errors, nothing warns, and the phone
simply stops seeing the organism work.

WHY NOT WIDEN THE ECHO FILTER
------------------------------
Making "self" mean the PROCESS instead of the repo is a one-line change that
would fix this and quietly hand two JARVIS processes every one of each
other's topics — ``fs.changed.*`` handled twice, sensors double-firing,
dedup windows keyed per-process. Unbounded blast radius for a need that is
one channel wide. The filter stays; one channel crosses deliberately.

WHAT THIS REUSES, AND WHAT IT ADDS
-----------------------------------
Nothing here implements transport, backpressure or reconnection. All three
already exist and are already load-bearing:

* ``DistributedEventBus`` — "the publish()-here-subscribers-there seam",
  server/client roles over ``BusBridgeServer``/``BusBridgeClient``.
* ``TransportConfig.from_env`` — ``queue_maxsize`` (256), heartbeat,
  ``reconnect_base_s`` / ``reconnect_max_s`` / ``reconnect_jitter``, TLS.
  The client's ``_next_backoff`` is exponential-with-jitter already.
* ``GovernanceSSEBridge`` — the subscription, the bounded queue with
  drop-oldest, the drain pump, and the conflation guard that collapses a
  flood into one frame. The producer here is that bridge with a different
  SINK, so the backpressure policy has exactly one implementation.

What is new is small on purpose: a strict envelope
(:mod:`governance.governance_envelope`), a sink that publishes it, and a
consumer that turns it back into an EventStream broadcast.

DIRECTION OF TRUST
------------------
Frames are rendered ONCE, in the process that owns the loop, and the
consumer never feeds cross-process data to ``GovernanceSSEBridge._render``.
"the renderer never raises" is a weaker guarantee than "the renderer is
never called on it".

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Jarvis.GovernanceCrossProcess")

GOVERNANCE_CROSS_PROCESS_SCHEMA_VERSION: str = "governance_cross_process.1"

__all__ = [
    "GOVERNANCE_CROSS_PROCESS_SCHEMA_VERSION",
    "BrokerSink",
    "GovernanceBusConsumer",
    "bridge_enabled",
    "install_governance_bus_consumer",
    "install_governance_bus_producer",
    "bus_url",
    "link_transport_config",
    "link_refusal",
    "link_tls_enabled",
    "is_loopback",
    "link_host",
    "reset_for_tests",
    "start_bus_client",
]


def bridge_enabled() -> bool:
    """``JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED`` (default false).

    Off by default like every cross-process surface here: it opens a socket
    between two daemons, and that is an operator's decision to make once they
    have both running.
    """
    return (os.environ.get("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "0")
            or "").strip().lower() not in ("0", "false", "no", "off", "")


def _loop_is_remote() -> bool:
    """True when the governed loop runs in a DIFFERENT process than the HUD.

    Both ends check this, so a single-process deployment
    (``JARVIS_GOVERNANCE_OWNER=supervisor``) never spins a client dialling
    its own broker or double-broadcasts frames the local bridge already
    forwarded. The cross-process path exists because the loop moved; if it
    did not move, there is nothing to cross.
    """
    try:
        from backend.core.ouroboros.governance.lifecycle_ownership import (
            supervisor_owns_governance,
        )
        return not supervisor_owns_governance()
    except Exception:  # noqa: BLE001
        return False


def _source_id() -> str:
    """Provenance for an operator staring at two daemons. Never routing."""
    return (os.environ.get("JARVIS_BRAIN_WS_SOURCE_ID") or "ov").strip() or "ov"


# ---------------------------------------------------------------------------
# Producer — `ov` side
# ---------------------------------------------------------------------------


class BrokerSink:
    """Publishes rendered frames onto the local broker as envelopes.

    Handed to ``GovernanceSSEBridge`` as its sink, so the flood control in
    front of it is the same queue, the same drop-oldest, and the same
    conflation the local path has always used. ``publish`` on the broker
    never blocks and never raises, so a disconnected peer cannot apply
    backpressure INTO the governed loop — it can only cost frames, which is
    the correct trade for telemetry.
    """

    def __init__(self, broker: Any = None, *, source_id: Optional[str] = None) -> None:
        self._broker = broker
        self._source_id = source_id if source_id is not None else _source_id()
        self.stats: Dict[str, int] = {"published": 0, "rejected": 0}

    def _resolve_broker(self) -> Any:
        if self._broker is not None:
            return self._broker
        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_default_broker,
        )
        self._broker = get_default_broker()
        return self._broker

    async def __call__(self, frames: List[Dict[str, Any]]) -> int:
        """Deliver a drained batch. Returns how many were published.

        NEVER raises: this runs inside the bridge's pump, and a sink that
        can throw turns a telemetry hiccup into a dead forwarder.
        """
        from backend.core.ouroboros.governance.governance_envelope import (
            GOVERNANCE_FORWARD_EVENT_TYPE, GovernanceEnvelope,
        )
        delivered = 0
        try:
            broker = self._resolve_broker()
        except Exception:  # noqa: BLE001
            logger.debug("[GovBus] broker unavailable", exc_info=True)
            self.stats["rejected"] += len(frames)
            return 0
        for frame in frames or ():
            try:
                op_id = ""
                if isinstance(frame, dict):
                    op_id = str(frame.get("command_id", "")
                                or frame.get("op_id", "") or "")
                # `topic` is empty by construction on this path: the bridge
                # renders BEFORE its queue, so by the time a batch reaches a
                # sink the source topic is already folded into the payload.
                # The field stays on the envelope for producers that do hold
                # it, and the consumer surfaces whatever arrives — an empty
                # provenance is honest, a fabricated one would not be.
                env = GovernanceEnvelope.of(
                    "", frame if isinstance(frame, dict) else {},
                    op_id=op_id, source_id=self._source_id)
                if env is None:
                    self.stats["rejected"] += 1
                    continue
                if broker.publish(GOVERNANCE_FORWARD_EVENT_TYPE, op_id,
                                  env.to_payload()) is None:
                    # Only reachable if the event type ever leaves
                    # `_VALID_EVENT_TYPES` — the silent-drop trap this
                    # registration exists to close. Counted, never guessed at.
                    self.stats["rejected"] += 1
                    continue
                self.stats["published"] += 1
                delivered += 1
            except Exception:  # noqa: BLE001
                self.stats["rejected"] += 1
                logger.debug("[GovBus] publish degraded", exc_info=True)
        return delivered


async def install_governance_bus_producer(
    *, broker: Any = None,
) -> Optional[Any]:
    """`ov` side: forward O+V activity onto the bus. NEVER raises.

    Returns the installed bridge, or None when disabled or when the bus is
    not up yet (the caller may retry — ``install`` is idempotent).
    """
    if not bridge_enabled() or not _loop_is_remote():
        return None
    try:
        from backend.api.governance_sse_bridge import GovernanceSSEBridge
        bridge = GovernanceSSEBridge(sink=BrokerSink(broker))
        if not await bridge.install():
            return None
        logger.info(
            "[GovBus] producer installed — O+V activity forwards onto the "
            "bus as %r frames",
            "governance_forward")
        return bridge
    except Exception:  # noqa: BLE001
        logger.debug("[GovBus] producer install degraded", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Consumer — supervisor side
# ---------------------------------------------------------------------------


class GovernanceBusConsumer:
    """Turns arriving envelopes back into EventStream broadcasts.

    Subscribes to the LOCAL broker: ``BusBridgeClient._apply_inbound``
    republishes every peer frame into it, so this end needs no socket
    knowledge at all. That is the property that keeps the transport
    swappable — the consumer would not notice if the link became a UDS.
    """

    def __init__(self, broker: Any = None, *, channel: Optional[str] = None) -> None:
        self._broker = broker
        self._channel = channel
        self._task: Optional[asyncio.Task] = None
        self._sub: Any = None
        self._running = False
        self.stats: Dict[str, int] = {
            "received": 0, "broadcast": 0, "malformed": 0, "no_stream": 0,
        }

    def _resolve_channel(self) -> str:
        if self._channel:
            return self._channel
        from backend.api.governance_sse_bridge import _SSE_CHANNEL
        return _SSE_CHANNEL

    def _resolve_broker(self) -> Any:
        if self._broker is None:
            from backend.core.ouroboros.governance.ide_observability_stream import (
                get_default_broker,
            )
            self._broker = get_default_broker()
        return self._broker

    async def start(self) -> bool:
        """Begin draining. Idempotent. NEVER raises."""
        if self._running:
            return True
        try:
            broker = self._resolve_broker()
            self._sub = broker.subscribe()
            if self._sub is None:
                logger.debug("[GovBus] consumer refused: subscriber cap")
                return False
            self._running = True
            self._task = asyncio.get_event_loop().create_task(
                self._drain(), name="governance-bus-consumer")
            logger.info("[GovBus] consumer started — peer O+V frames will "
                        "reach the %s channel", self._resolve_channel())
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[GovBus] consumer start degraded", exc_info=True)
            self._running = False
            return False

    async def stop(self) -> None:
        """NEVER raises."""
        self._running = False
        try:
            if self._task is not None and not self._task.done():
                self._task.cancel()
            if self._sub is not None and self._broker is not None:
                self._broker.unsubscribe(self._sub)
        except Exception:  # noqa: BLE001
            pass
        self._task = None
        self._sub = None

    async def _drain(self) -> None:
        """One event at a time, forever. NEVER raises out."""
        from backend.core.ouroboros.governance.governance_envelope import (
            GOVERNANCE_FORWARD_EVENT_TYPE, GovernanceEnvelope,
        )
        try:
            broker = self._resolve_broker()
            async for event in broker.stream_iter(self._sub):
                if not self._running:
                    return
                try:
                    if getattr(event, "event_type", "") != GOVERNANCE_FORWARD_EVENT_TYPE:
                        continue          # heartbeats and every other type
                    self.stats["received"] += 1
                    env = GovernanceEnvelope.from_payload(
                        getattr(event, "payload", None))
                    if env is None:
                        # A frame this process cannot decode is dropped at
                        # the boundary, not passed inward half-understood.
                        self.stats["malformed"] += 1
                        continue
                    await self._broadcast(env.payload)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self.stats["malformed"] += 1
                    logger.debug("[GovBus] frame degraded", exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.debug("[GovBus] consumer drain ended", exc_info=True)

    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        """Hand a decoded payload to the EventStream. NEVER raises."""
        try:
            from backend.core.event_stream import (
                get_event_stream_if_initialized,
            )
            es = get_event_stream_if_initialized()
            if es is None:
                self.stats["no_stream"] += 1
                return
            sent = await es.broadcast_event(self._resolve_channel(), payload)
            if sent > 0:
                self.stats["broadcast"] += 1
            else:
                self.stats["no_stream"] += 1
        except Exception:  # noqa: BLE001
            self.stats["no_stream"] += 1
            logger.debug("[GovBus] broadcast degraded", exc_info=True)

    def health(self) -> Dict[str, Any]:
        return {
            "schema_version": GOVERNANCE_CROSS_PROCESS_SCHEMA_VERSION,
            "enabled": bridge_enabled(),
            "running": self._running,
            **self.stats,
        }


#: Addresses the kernel will not route off this host. A link that cannot
#: leave the machine has a different threat model from one that can, and that
#: difference is the ONLY thing this module lets decide the TLS posture.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "127.0.1.1"})


def link_host() -> str:
    """Where the `ov` end is reachable. The server binds it, the client
    dials it, and the TLS posture is derived from it — one value, so the two
    ends cannot hold different beliefs about how exposed the link is."""
    return (os.environ.get("JARVIS_CHANNEL_HOST") or "127.0.0.1").strip()


def is_loopback(host: str) -> bool:
    """NEVER raises. Unknown shapes are treated as ROUTABLE — the safe
    direction, since guessing 'probably local' is what turns a development
    convenience into an exposed plaintext socket."""
    try:
        return (host or "").strip().lower() in _LOOPBACK_HOSTS
    except Exception:  # noqa: BLE001
        return False


def link_tls_enabled() -> bool:
    """The posture for THIS link. NEVER raises.

    DERIVED from reachability rather than inherited, because the inherited
    knob is shared: ``TransportConfig.from_env`` reads ``JARVIS_BRAIN_WS_*``,
    which ``brain_keeper`` and ``organism_bus_host`` also read for the
    CROSS-HOST brain link. Setting ``JARVIS_BRAIN_WS_TLS_ENABLED=false`` to
    make a loopback telemetry socket convenient would have silently
    downgraded that one too — a security regression bought with an
    unrelated convenience.

    So: a socket the kernel will not route off this host runs plaintext; a
    socket that can leave the machine requires TLS. Same rule the
    ``ide_observability`` surface already lives by, applied to a transport
    instead of a router. ``JARVIS_GOVERNANCE_BUS_TLS_ENABLED`` overrides in
    either direction — an operator may demand TLS on loopback, and
    :func:`link_refusal` still refuses the unsafe combination.
    """
    raw = (os.environ.get("JARVIS_GOVERNANCE_BUS_TLS_ENABLED") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return not is_loopback(link_host())


def link_refusal() -> str:
    """Why this link must NOT be established, or "". NEVER raises.

    Fails CLOSED on exactly one combination: reachable off-host AND
    plaintext. Both ends call it, so neither can be talked into the unsafe
    posture by the other's configuration.
    """
    host = link_host()
    if not is_loopback(host) and not link_tls_enabled():
        return (f"refusing a plaintext governance link on non-loopback host "
                f"{host!r} — set JARVIS_GOVERNANCE_BUS_TLS_ENABLED=1 (and "
                f"provision certs) or bind JARVIS_CHANNEL_HOST to loopback")
    return ""


def link_transport_config(role: str) -> Any:
    """``TransportConfig`` for this link, with the posture applied.

    Everything else — ``queue_maxsize``, heartbeat, the reconnect
    base/max/jitter — is inherited untouched from the shared env, because
    those are transport tuning and are correct for any link. Only the TLS
    decision is overridden, and only because sharing it would couple two
    links with different threat models.
    """
    from dataclasses import replace

    from backend.core.ouroboros.governance.transport.transport_config import (
        TransportConfig,
    )
    cfg = TransportConfig.from_env(role=role)
    return replace(cfg, tls_enabled=link_tls_enabled())


def bus_url() -> str:
    """Where the supervisor dials `ov`. NEVER raises.

    ``JARVIS_GOVERNANCE_BUS_URL`` when set. Otherwise DERIVED from the same
    knobs the `ov` end binds with — ``JARVIS_CHANNEL_HOST`` /
    ``JARVIS_CHANNEL_PORT`` for the address and ``TransportConfig.path`` for
    the route. Deriving rather than restating means an operator who moves the
    event channel does not silently leave a client dialling the old port: the
    two ends cannot disagree because they read the same values.

    The scheme follows ``tls_enabled``, so a link is never quietly downgraded
    by this function.
    """
    explicit = (os.environ.get("JARVIS_GOVERNANCE_BUS_URL") or "").strip()
    if explicit:
        return explicit
    try:
        cfg = link_transport_config("client")
        path = cfg.path or "/ws/trinity-bus"
        scheme = "wss" if getattr(cfg, "tls_enabled", True) else "ws"
    except Exception:  # noqa: BLE001
        path, scheme = "/ws/trinity-bus", "wss"
    port = (os.environ.get("JARVIS_CHANNEL_PORT") or "8099").strip()
    return f"{scheme}://{link_host()}:{port}{path}"


async def start_bus_client(*, broker: Any = None,
                           url: Optional[str] = None) -> Optional[Any]:
    """Supervisor side: dial `ov` and fill the local broker. NEVER raises.

    ``BusBridgeClient.run()`` is an infinite reconnect loop — exponential
    backoff with jitter, straight from ``TransportConfig`` — so it is
    launched as a task rather than awaited. Its inbound frames republish into
    the local broker, which is the only thing :class:`GovernanceBusConsumer`
    knows about; the consumer would not notice if this link became a UDS.
    """
    if not bridge_enabled():
        return None
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_default_broker,
        )
        from backend.core.ouroboros.governance.transport.distributed_event_bus import (  # noqa: E501
            DistributedEventBus,
        )
        refusal = link_refusal()
        if refusal:
            logger.warning("[GovBus] %s", refusal)
            return None
        cfg = link_transport_config("client")
        bus = DistributedEventBus(broker or get_default_broker(), cfg,
                                  role="client")
        target = url or bus_url()
        asyncio.get_event_loop().create_task(
            bus.start_client(target), name="governance-bus-client")
        logger.info("[GovBus] client dialling %s (tls=%s, reconnect "
                    "base=%.1fs max=%.1fs jitter=%.2f)", target,
                    cfg.tls_enabled, cfg.reconnect_base_s,
                    cfg.reconnect_max_s, cfg.reconnect_jitter)
        return bus
    except Exception:  # noqa: BLE001
        logger.debug("[GovBus] client start degraded", exc_info=True)
        return None


_consumer: Optional[GovernanceBusConsumer] = None
_client_bus: Optional[Any] = None


async def install_governance_bus_consumer(
    *, broker: Any = None,
) -> Optional[GovernanceBusConsumer]:
    """Supervisor side. Idempotent; NEVER raises."""
    global _consumer  # noqa: PLW0603
    if not bridge_enabled() or not _loop_is_remote():
        return None
    if _consumer is not None and _consumer._running:
        return _consumer
    consumer = GovernanceBusConsumer(broker)
    if not await consumer.start():
        return None
    _consumer = consumer
    # The consumer reads the LOCAL broker; the client is what puts peer
    # frames into it. Starting one without the other is a subscriber to an
    # empty queue — live by every surface, and permanently silent.
    global _client_bus  # noqa: PLW0603
    if _client_bus is None:
        _client_bus = await start_bus_client(broker=broker)
    return consumer


async def reset_for_tests() -> None:
    """Test-only."""
    global _client_bus, _consumer  # noqa: PLW0603
    if _consumer is not None:
        await _consumer.stop()
    if _client_bus is not None:
        try:
            await _client_bus.stop()
        except Exception:  # noqa: BLE001
            pass
    _consumer = None
    _client_bus = None
