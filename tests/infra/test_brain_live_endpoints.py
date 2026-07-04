# -*- coding: utf-8 -*-
"""Task-9 wiring: client outbound replay cursor + live endpoint adapters +
un-loopbacked exchange over two REAL brokers with the REAL Brain responder.

The loopback design masked a Stage-0 gap: the WS client's outbound pump
re-subscribed with ``last_event_id=None`` on every reconnect, so events
published while severed never crossed client->server (server->client replay
worked via the hello). The cursor closes it; the adapters make the
cross-host bidirectional proof real (ValidationExchange itself UNCHANGED).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import backend.core.ouroboros.governance.transport.exchange_protocol as xp
from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ign():
    return _load("ignite_brain_vm")


@pytest.fixture()
def echo_mod():
    return _load("brain_bus_echo_server")


# --------------------------------------------------------------------------- #
# (a) client outbound pump resumes from its last-SENT cursor.
# --------------------------------------------------------------------------- #
def test_client_outbound_pump_resumes_from_last_sent_cursor(monkeypatch):
    from backend.core.ouroboros.governance.transport.bus_bridge_client import (
        BusBridgeClient,
    )
    from backend.core.ouroboros.governance.transport.transport_config import (
        TransportConfig,
    )

    for key in ("JARVIS_BRAIN_WS_TLS_ENABLED",):
        monkeypatch.setenv(key, "false")
    cfg = TransportConfig.from_env(role="client")
    broker = StreamEventBroker()
    client = BusBridgeClient(broker, cfg, url="ws://localhost:1/ws")

    captured: List[Optional[str]] = []
    real_subscribe = broker.subscribe

    def _spy(op_id_filter=None, last_event_id=None):
        captured.append(last_event_id)
        return real_subscribe(op_id_filter=op_id_filter, last_event_id=last_event_id)

    monkeypatch.setattr(broker, "subscribe", _spy)

    class _FakeWS:
        closed = False
        sent: List[bytes] = []

        async def send_bytes(self, b: bytes) -> None:
            self.sent.append(b)
            self.closed = True  # one send then hang up

    async def scenario():
        ws = _FakeWS()
        pump = asyncio.ensure_future(client._pump_outbound(ws))
        await asyncio.sleep(0.1)  # live-only subscription armed
        eid1 = broker.publish(xp.EXCHANGE_EVENT_TYPE, "op-1", {"n": 1})
        await asyncio.sleep(0.2)
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass
        return eid1

    eid1 = asyncio.get_event_loop().run_until_complete(scenario())
    # First connect: no cursor (None). After sending event 1, the cursor is set.
    assert captured[0] is None
    assert client._last_sent_id == eid1

    async def reconnect():
        ws = _FakeWS()
        pump = asyncio.ensure_future(client._pump_outbound(ws))
        await asyncio.sleep(0.1)
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass

    asyncio.get_event_loop().run_until_complete(reconnect())
    assert captured[1] == eid1, "reconnect must resume from the last-SENT cursor"


# --------------------------------------------------------------------------- #
# (b)+(c) adapters + REAL responder over two real brokers, bridged in-proc.
# --------------------------------------------------------------------------- #
class _InProcBridge:
    """Mirror publishes between two real brokers (the WS bridge, in-proc), with
    a severable link + buffered replay on reconnect -- op-prefixed exchange
    traffic only, mimicking the wire's dedup-by-id."""

    def __init__(self, mac: StreamEventBroker, brain: StreamEventBroker) -> None:
        self.mac, self.brain = mac, brain
        self.connected = True
        self._seen: set = set()
        self._buffered: List[Tuple[str, str, str, Dict[str, Any]]] = []
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.ensure_future(self._pump(self.mac, self.brain, "m2b")),
            asyncio.ensure_future(self._pump(self.brain, self.mac, "b2m")),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def sever(self) -> None:
        self.connected = False

    def reconnect(self) -> None:
        self.connected = True
        for etype, op, eid, payload in self._buffered:
            if eid not in self._seen:
                self._seen.add(eid)
                self._dst_for_buffer.publish(etype, op, payload)
        self._buffered.clear()

    async def _pump(self, src: StreamEventBroker, dst: StreamEventBroker,
                    tag: str) -> None:
        sub = src.subscribe()
        assert sub is not None
        if tag == "m2b":
            self._dst_for_buffer = dst  # mac->brain is the severable direction
        while True:
            ev = await sub.queue.get()
            if not xp.is_exchange_op(ev.op_id):
                continue
            if ev.event_id in self._seen:
                continue
            if not self.connected and tag == "m2b":
                self._buffered.append(
                    (ev.event_type, ev.op_id, ev.event_id, dict(ev.payload)))
                continue
            self._seen.add(ev.event_id)
            dst.publish(ev.event_type, ev.op_id, dict(ev.payload))


def test_exchange_cross_host_proven_with_real_responder(ign, echo_mod):
    async def scenario():
        mac_broker = StreamEventBroker()
        brain_broker = StreamEventBroker()
        bridge = _InProcBridge(mac_broker, brain_broker)
        await bridge.start()
        responder = asyncio.ensure_future(
            echo_mod.run_responder(brain_broker, touch_fn=lambda: None))
        await asyncio.sleep(0.05)

        class _Bus:  # publish seam the mac endpoint uses
            def publish(self, etype, op, payload):
                return mac_broker.publish(etype, op, payload)

        mac = ign._LiveMacEndpoint(_Bus(), mac_broker, lambda: None)
        await mac.start()
        brain = ign._LiveBrainEndpoint(mac)

        async def _reconnect():
            bridge.reconnect()
            return mac

        async def _sever():
            bridge.sever()

        exchange = ign.ValidationExchange(
            mac=mac, brain=brain, reconnect=_reconnect, sever=_sever,
            settle_s=0.15, observe_timeout_s=5.0,
        )
        result = await exchange.run_all(3)
        responder.cancel()
        await mac.stop()
        await bridge.stop()
        return result

    result = asyncio.get_event_loop().run_until_complete(scenario())
    assert result.get("loopback_only") in (False, None), (
        "distinct endpoints must clear the loopback guard: %r" % result)
    assert result["bidirectional"]["ok"], result
    assert result["reconnect_replay"]["ok"], result
    assert result["proven"] is True, result
