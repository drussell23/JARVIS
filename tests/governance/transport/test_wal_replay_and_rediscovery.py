# -*- coding: utf-8 -*-
"""Stage 3 Task 3: WAL-seeded reconnect replay + per-attempt discovery
re-race, proven against a REAL localhost client<->server WS pair.

  (a) REDISCOVERY: the client re-races discovery on EVERY reconnect
      attempt (spec line 69). A client whose STATIC url is dead but whose
      ``url_resolver`` answers a live URL connects; after the server is
      killed and restarted on a DIFFERENT port, flipping the resolver's
      answer lands the reconnect on the new port. Redialing a static url
      forever (the pre-Task-3 behavior) can never pass this.
  (a2) RESOLVER FAIL-SOFT: a resolver that raises must not kill the
      reconnect loop -- the attempt falls back to the static url.
  (b) WAL-SEEDED REPLAY BEYOND BROKER HISTORY: 6 events published with NO
      server up and the broker history ring clamped to 2
      (JARVIS_IDE_STREAM_HISTORY_MAXLEN=2 -- broker replay alone CANNOT
      recover more than 2). The DurableOutbound WAL carried all 6; on
      connect the client seeds the link from ``durable.pending()`` and the
      server broker ends holding ALL 6 exactly once (qualified-id dedup
      absorbs any overlap -- no client-side dedup added).
  (c) LEGACY SHAPE: no ``durable``, no ``url_resolver`` -> Stage-2
      behavior byte-identical (existing suites stay authoritative; this
      pins the default construction path still mirrors events).
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any, Callable, Dict, List, Optional

from aiohttp import web

import backend.core.ouroboros.governance.transport.exchange_protocol as xp
from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.transport_config import (
    TransportConfig,
)
from backend.core.ouroboros.governance.transport.distributed_event_bus import (
    DistributedEventBus,
)
from backend.core.ouroboros.governance.transport.durable_outbound import (
    DurableOutbound,
)

OP = xp.EXCHANGE_OP_PREFIX + "task3"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(monkeypatch, port: int, role: str) -> TransportConfig:
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_BRAIN_WS_HOST", "127.0.0.1")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", str(port))
    monkeypatch.setenv("JARVIS_BRAIN_WS_HEARTBEAT_S", "1.0")
    # Fast, deterministic reconnect cadence so re-race tests finish quickly.
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_BASE_S", "0.05")
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_MAX_S", "0.2")
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_JITTER", "0")
    return TransportConfig.from_env(role=role)


async def _wait_for(cond: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within %.1fs" % timeout)


async def _await_connected(bus: DistributedEventBus, timeout: float = 8.0) -> None:
    def _is_connected() -> bool:
        client = getattr(bus, "_client", None)
        return client is not None and getattr(client, "connected", False)
    await _wait_for(_is_connected, timeout=timeout)


async def _start_server(
    broker: StreamEventBroker, cfg: TransportConfig, port: int,
) -> web.AppRunner:
    bus = DistributedEventBus(broker, cfg, role="server")
    app = web.Application()
    bus.register_server_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    # Small shutdown_timeout: the rediscovery test KILLS a server while a
    # live WS is attached -- the default 60s drain would hang the test.
    site = web.TCPSite(runner, host="127.0.0.1", port=port, shutdown_timeout=0.5)
    await site.start()
    return runner


def _op_ids(broker: StreamEventBroker) -> List[str]:
    return [ev.op_id for ev in broker.recent_history(limit=1000)
            if xp.is_exchange_op(ev.op_id) or ev.op_id.startswith("trinity:")]


async def _stop_client(bus: DistributedEventBus, task: asyncio.Task) -> None:
    try:
        await bus.stop()
    except Exception:  # noqa: BLE001
        pass
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# (a) rediscovery: the resolver is consulted on EVERY attempt; a kill +
#     restart on a DIFFERENT port is recovered by flipping the resolver's
#     answer. The static url is a dead port throughout -- only re-raced
#     discovery can connect at all.
# --------------------------------------------------------------------------- #
def test_rediscovery_reraces_resolver_each_attempt(monkeypatch):
    async def scenario():
        port_a, port_b, dead_port = _free_port(), _free_port(), _free_port()
        server_cfg_a = _cfg(monkeypatch, port_a, "server")
        server_cfg_b = _cfg(monkeypatch, port_b, "server")
        # Static client cfg dials a DEAD port -- the resolver must win.
        client_cfg = _cfg(monkeypatch, dead_port, "client")
        path = client_cfg.path

        target: Dict[str, str] = {"url": "ws://127.0.0.1:%d%s" % (port_a, path)}
        calls = {"n": 0}

        async def resolver() -> Optional[str]:
            calls["n"] += 1
            return target["url"]

        client_broker = StreamEventBroker(history_maxlen=64)
        broker_a = StreamEventBroker(history_maxlen=64)
        broker_b = StreamEventBroker(history_maxlen=64)

        runner_a = await _start_server(broker_a, server_cfg_a, port_a)
        client_bus = DistributedEventBus(
            client_broker, client_cfg, role="client", url_resolver=resolver)
        task = asyncio.ensure_future(client_bus.start_client())
        try:
            await _await_connected(client_bus)
            client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-a", {"p": 1})
            await _wait_for(lambda: (OP + "-a") in _op_ids(broker_a))

            # Kill A. The client's retries keep hitting the (now dead)
            # resolver answer -- each attempt must RE-ASK the resolver.
            await runner_a.cleanup()
            calls_at_kill = calls["n"]
            await asyncio.sleep(0.4)

            # Restart on a DIFFERENT port and flip the resolver's answer.
            runner_b = await _start_server(broker_b, server_cfg_b, port_b)
            target["url"] = "ws://127.0.0.1:%d%s" % (port_b, path)
            await _await_connected(client_bus)
            assert calls["n"] > calls_at_kill, (
                "resolver must be re-raced per reconnect attempt, not "
                "consulted once: %d calls before kill, %d after"
                % (calls_at_kill, calls["n"]))

            client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-b", {"p": 2})
            await _wait_for(lambda: (OP + "-b") in _op_ids(broker_b))
            landed_on_b = (OP + "-b") in _op_ids(broker_b)
            await runner_b.cleanup()
            return landed_on_b
        finally:
            await _stop_client(client_bus, task)

    assert _run(scenario()) is True, (
        "reconnect must land on the resolver's NEW url (per-attempt re-race)")


# --------------------------------------------------------------------------- #
# (a2) resolver fail-soft: a raising resolver falls back to the static url.
# --------------------------------------------------------------------------- #
def test_resolver_failure_falls_back_to_static_url(monkeypatch):
    async def scenario():
        port = _free_port()
        server_cfg = _cfg(monkeypatch, port, "server")
        client_cfg = _cfg(monkeypatch, port, "client")

        async def bad_resolver() -> Optional[str]:
            raise RuntimeError("discovery service down")

        client_broker = StreamEventBroker(history_maxlen=64)
        server_broker = StreamEventBroker(history_maxlen=64)
        runner = await _start_server(server_broker, server_cfg, port)
        client_bus = DistributedEventBus(
            client_broker, client_cfg, role="client", url_resolver=bad_resolver)
        url = "ws://127.0.0.1:%d%s" % (port, client_cfg.path)
        task = asyncio.ensure_future(client_bus.start_client(url))
        try:
            await _await_connected(client_bus)
            client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-fs", {"p": 1})
            await _wait_for(lambda: (OP + "-fs") in _op_ids(server_broker))
            return True
        finally:
            await _stop_client(client_bus, task)
            await runner.cleanup()

    assert _run(scenario()) is True, (
        "a raising resolver must fail-soft to the static url, not kill run()")


# --------------------------------------------------------------------------- #
# (b) WAL-seeded replay beyond broker history: 6 events published with NO
#     server and the broker ring clamped to 2 -- broker replay alone CANNOT
#     recover them. The WAL can. Server ends with ALL 6, exactly once each.
# --------------------------------------------------------------------------- #
def test_wal_seeded_replay_beyond_broker_history(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    monkeypatch.setenv("JARVIS_IDE_STREAM_HISTORY_MAXLEN", "2")
    wal_path = str(tmp_path / "body_wal.jsonl")
    ops = ["trinity:wal-%d" % i for i in range(6)]

    async def scenario():
        port = _free_port()
        server_cfg = _cfg(monkeypatch, port, "server")
        client_cfg = _cfg(monkeypatch, port, "client")

        client_broker = StreamEventBroker()  # env-clamped ring: 2 slots
        server_broker = StreamEventBroker(history_maxlen=64)
        durable = DurableOutbound(client_broker, wal_path=wal_path)
        await durable.start()

        # Publish ALL 6 while no server exists.
        ids = [client_broker.publish("task_started", op, {"i": i})
               for i, op in enumerate(ops)]
        assert all(ids)
        await _wait_for(lambda: durable.pending_count() == 6)
        # Proof the ring CANNOT recover alone: only the last 2 survive it.
        ring = [ev.op_id for ev in client_broker.recent_history(limit=100)]
        assert len(ring) == 2, (
            "precondition broken: broker ring must hold only 2, got %r" % ring)

        # NOW the server comes up; the client connects with the WAL wired.
        runner = await _start_server(server_broker, server_cfg, port)
        client_bus = DistributedEventBus(
            client_broker, client_cfg, role="client", durable_outbound=durable)
        url = "ws://127.0.0.1:%d%s" % (port, client_cfg.path)
        task = asyncio.ensure_future(client_bus.start_client(url))
        try:
            await _await_connected(client_bus)

            def _counts() -> Dict[str, int]:
                got = [ev.op_id for ev in server_broker.recent_history(limit=200)
                       if ev.op_id in set(ops)]
                return {op: got.count(op) for op in ops}

            await _wait_for(lambda: all(c >= 1 for c in _counts().values()))
            await asyncio.sleep(0.6)  # let any duplicate land before exactness
            counts = _counts()
        finally:
            await _stop_client(client_bus, task)
            await durable.stop()
            await runner.cleanup()
        return counts

    counts = _run(scenario())
    assert counts == {op: 1 for op in ops}, (
        "server must hold ALL 6 WAL-carried events exactly once, got %r"
        % counts)


# --------------------------------------------------------------------------- #
# (c) legacy shape: no durable, no resolver -> Stage-2 behavior intact
#     (default construction path still connects + mirrors an event).
# --------------------------------------------------------------------------- #
def test_legacy_shape_no_durable_no_resolver(monkeypatch):
    async def scenario():
        port = _free_port()
        server_cfg = _cfg(monkeypatch, port, "server")
        client_cfg = _cfg(monkeypatch, port, "client")
        client_broker = StreamEventBroker(history_maxlen=64)
        server_broker = StreamEventBroker(history_maxlen=64)
        runner = await _start_server(server_broker, server_cfg, port)
        client_bus = DistributedEventBus(client_broker, client_cfg, role="client")
        url = "ws://127.0.0.1:%d%s" % (port, client_cfg.path)
        task = asyncio.ensure_future(client_bus.start_client(url))
        try:
            await _await_connected(client_bus)
            client = client_bus._client
            assert getattr(client, "_durable", "MISSING") is None
            assert getattr(client, "_url_resolver", "MISSING") is None
            client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-legacy", {"p": 1})
            await _wait_for(lambda: (OP + "-legacy") in _op_ids(server_broker))
            return True
        finally:
            await _stop_client(client_bus, task)
            await runner.cleanup()

    assert _run(scenario()) is True
