# -*- coding: utf-8 -*-
"""Stage 3 Task 1: arm the dormant ack lane (bus_frame.py FRAME_ACK, minted
at Stage 0, was a documented no-op through Stage 2 -- see
bus_bridge_server.py's former L148 comment).

Server side: ``BusBridgeServer._handle_ws`` now tracks a per-connection
ingest cursor (the event_id of the last EVENT frame ingested on THIS
connection) and emits an ``ack_frame`` back to the peer on a cadence --
every ``JARVIS_BUS_ACK_EVERY_N`` events OR every ``JARVIS_BUS_ACK_INTERVAL_S``
seconds, whichever trips first.

Client side: ``BusBridgeClient._pump_inbound`` routes FRAME_ACK to the new
``_apply_ack`` method, which monotonically advances ``last_acked_id``
(zero-padded hex event ids compare correctly as strings) and fires an
optional ``on_ack`` callback, fail-soft.

Real localhost WS pair, no mocks -- the in-proc fake bridge masked three
live bugs earlier this stage (see test_bridge_reflection_and_cursor.py),
so this suite proves the ack lane against an actual aiohttp server +
aiohttp client socket pair.
"""
from __future__ import annotations

import asyncio
import socket
from typing import List, Optional

from aiohttp import web

import backend.core.ouroboros.governance.transport.exchange_protocol as xp
from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport import bus_frame as bf
from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig

OP = xp.EXCHANGE_OP_PREFIX + "ack-lane"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(**over) -> TransportConfig:
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.05, reconnect_max_s=0.5, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain",
    )
    base.update(over)
    return TransportConfig(**base)


class _ServerHarness:
    """A REAL BusBridgeServer bound to localhost -- constructed directly
    (not via DistributedEventBus) so the test can hand BusBridgeClient the
    new ``on_ack`` kwarg, which DistributedEventBus does not forward yet."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.broker = StreamEventBroker(history_maxlen=1024)
        self.server = BusBridgeServer(self.broker, _cfg(port=self.port))
        self._runner: Optional[web.AppRunner] = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/trinity-bus"

    async def start(self) -> None:
        app = web.Application()
        self.server.register_routes(app)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="127.0.0.1", port=self.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


async def _await_connected(client: BusBridgeClient, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if client.connected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("client never reported connected")


async def _stop_client(client: BusBridgeClient, run_task: "asyncio.Task") -> None:
    await client.stop()
    run_task.cancel()
    try:
        await run_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# (a) N=20 real client-side events -> within the ack cadence, the client's
#     last_acked_id reaches the 20th event's id and on_ack fired >=1 time
#     with monotonically increasing ids.
# --------------------------------------------------------------------------- #
def test_ack_cursor_advances_to_last_ingested_event(monkeypatch):
    monkeypatch.setenv("JARVIS_BUS_ACK_EVERY_N", "5")
    monkeypatch.setenv("JARVIS_BUS_ACK_INTERVAL_S", "0.2")

    async def scenario():
        harness = _ServerHarness()
        await harness.start()

        client_broker = StreamEventBroker(history_maxlen=1024)
        acked: List[str] = []
        client = BusBridgeClient(
            client_broker, _cfg(port=harness.port), url=harness.url,
            on_ack=acked.append,
        )
        run_task = asyncio.ensure_future(client.run())
        try:
            await _await_connected(client)

            event_ids = []
            for i in range(20):
                eid = client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + f"-{i}", {"i": i})
                assert eid, "publish must succeed for a valid exchange event"
                event_ids.append(eid)

            deadline = asyncio.get_event_loop().time() + 3.0
            while (asyncio.get_event_loop().time() < deadline
                   and client.last_acked_id != event_ids[-1]):
                await asyncio.sleep(0.05)

            last_acked = client.last_acked_id
        finally:
            await _stop_client(client, run_task)
            await harness.stop()
        return last_acked, list(acked), event_ids

    last_acked, acked, event_ids = asyncio.get_event_loop().run_until_complete(scenario())
    assert last_acked == event_ids[-1], (
        f"client last_acked_id must reach the 20th event's id: "
        f"got {last_acked!r}, want {event_ids[-1]!r}")
    assert len(acked) >= 1, "on_ack callback must fire at least once"
    assert acked == sorted(acked), f"on_ack ids must be monotonically increasing: {acked!r}"


# --------------------------------------------------------------------------- #
# (b) monotonic guard: a regressive/duplicate ack must not move the cursor
#     or re-fire on_ack. Unit-level -- hand-deliver ack frames straight into
#     _apply_ack, no network needed for this half of the contract.
# --------------------------------------------------------------------------- #
def test_apply_ack_ignores_regressive_and_duplicate():
    broker = StreamEventBroker(history_maxlen=16)
    acked: List[str] = []
    client = BusBridgeClient(broker, _cfg(), on_ack=acked.append)

    client._apply_ack(bf.ack_frame("brain", "000000000005"))
    assert client.last_acked_id == "000000000005"
    assert acked == ["000000000005"]

    client._apply_ack(bf.ack_frame("brain", "000000000003"))  # regressive
    assert client.last_acked_id == "000000000005", "regressive ack must not rewind the cursor"

    client._apply_ack(bf.ack_frame("brain", "000000000005"))  # duplicate
    assert client.last_acked_id == "000000000005"
    assert acked == ["000000000005"], "on_ack must not re-fire for a regressive/duplicate ack"

    client._apply_ack(bf.ack_frame("brain", "000000000009"))  # forward
    assert client.last_acked_id == "000000000009"
    assert acked == ["000000000005", "000000000009"]


def test_apply_ack_on_ack_exception_is_fail_soft():
    def _boom(_eid: str) -> None:
        raise RuntimeError("boom")

    broker = StreamEventBroker(history_maxlen=16)
    client = BusBridgeClient(broker, _cfg(), on_ack=_boom)

    client._apply_ack(bf.ack_frame("brain", "000000000001"))  # must not raise
    assert client.last_acked_id == "000000000001"


# --------------------------------------------------------------------------- #
# (c) legacy shape: a client with NO on_ack keeps working (acks silently
#     advance the cursor with nothing to invoke). Stage-2 behavior (event
#     flow, reflection suppression, cursor-across-reconnect) is asserted
#     unmodified by the existing suites in this directory -- see
#     test_bridge_reflection_and_cursor.py, test_bus_bridge_client.py,
#     test_two_process_loopback.py, run alongside this file.
# --------------------------------------------------------------------------- #
def test_client_without_on_ack_still_ingests_and_gets_acked(monkeypatch):
    monkeypatch.setenv("JARVIS_BUS_ACK_EVERY_N", "1")
    monkeypatch.setenv("JARVIS_BUS_ACK_INTERVAL_S", "0.2")

    async def scenario():
        harness = _ServerHarness()
        await harness.start()
        client_broker = StreamEventBroker(history_maxlen=1024)
        client = BusBridgeClient(client_broker, _cfg(port=harness.port), url=harness.url)
        run_task = asyncio.ensure_future(client.run())
        try:
            await _await_connected(client)

            eid = client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-legacy", {"i": 0})
            assert eid

            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline and client.last_acked_id != eid:
                await asyncio.sleep(0.05)

            last_acked = client.last_acked_id
        finally:
            await _stop_client(client, run_task)
            await harness.stop()
        return last_acked, eid

    last_acked, eid = asyncio.get_event_loop().run_until_complete(scenario())
    assert last_acked == eid, "a client with no on_ack must still consume acks (cursor advances)"
