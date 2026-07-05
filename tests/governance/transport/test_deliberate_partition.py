# -*- coding: utf-8 -*-
"""Stage 3 Task 5: THE DELIBERATE-PARTITION SUITE (load-bearing).

Operator mandate: prove MATHEMATICAL SET-EQUALITY on the replay
synchronization. Four legs, all against REAL localhost WS pairs, REAL
DurableOutbound WAL files, REAL brokers -- the ARMED-durable live
default path, no injected fakes. No sleeps-as-logic: every wait is a
condition poll with a bounded deadline; the only time-window primitive
is a *stability* settle (counts unchanged for a continuous window,
bounded) used to let would-be duplicates land before exactness is
asserted.

  Leg A -- kill mid-stream, exact replay: 30 events total. ~10 cross
      live, the server is HARD-KILLED (runner.cleanup() = socket
      death), 10 more are published during the partition (client
      connected=False, WAL pending >= 10, zero at-risk), the server
      is RESTARTED with a FRESH broker (fresh dedup state) on a NEW
      port found via the url_resolver, and the final 10 cross live.
      The destination broker must end with ALL 30 exactly once:
      multiset equality on the full ordered id set + per-id
      occurrence == 1 + count == 30. The client broker ring is
      env-clamped to 4 so broker-cursor replay physically cannot
      recover the severed span -- the WAL is the only full truth.

  Leg B -- Body process death mid-partition: with N=8 pending during
      a partition, every client-side object is destroyed; a NEW
      broker + client + DurableOutbound are built from the SAME
      wal_path; on reconnect the 8 cross exactly once. Durability is
      disk-truth, not object-lifetime. The 3 pre-partition events
      that were acked+trimmed must NOT resurrect.

  Leg C -- Brain service death, HMAC suspend/resume: two synthetic
      in-flight ops registered in the REAL in-flight registry, then
      capture_inflight(reason="partition_test") -- the module's real
      contract, unchanged. list_pending returns both HMAC-VERIFIED;
      a ONE-BYTE payload tamper (valid JSON at both levels -- only
      the crypto gate can catch it) is REJECTED fail-closed; the
      untampered twin hydrates EXACTLY once (mark_resumed second
      call False, second hydrate injects nothing).

  Leg D -- no duplicated terminal state: the SAME WAL pending set is
      delivered twice across two reconnects to the SAME server. The
      worst-case race (acks processed, trim NOT persisted) is made
      deterministic by snapshotting the pre-trim WAL bytes and
      restoring them after the first delivery+trim -- the second
      incarnation replays all 6 again. Server-side count for those
      ids stays exactly 1: qualified-id dedup is the single dedup
      authority. Receipt of the second delivery is positively proven
      by the server's acks re-trimming the restored WAL to 0.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from typing import Any, Callable, Dict, List, Optional

from aiohttp import web

import backend.core.ouroboros.governance.fsm_checkpoint as fc
import backend.core.ouroboros.governance.in_flight_registry as ifr
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(monkeypatch, port: int, role: str) -> TransportConfig:
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_BRAIN_WS_HOST", "127.0.0.1")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", str(port))
    monkeypatch.setenv("JARVIS_BRAIN_WS_HEARTBEAT_S", "1.0")
    # Fast deterministic reconnect cadence -- partition recovery must not
    # wait out production backoff.
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_BASE_S", "0.05")
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_MAX_S", "0.2")
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_JITTER", "0")
    return TransportConfig.from_env(role=role)


async def _wait_for(cond: Callable[[], bool], timeout: float = 10.0,
                    msg: str = "") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        "condition not met within %.1fs%s"
        % (timeout, (": " + msg) if msg else ""))


async def _await_connected(bus: DistributedEventBus,
                           timeout: float = 10.0) -> None:
    def _is_connected() -> bool:
        client = getattr(bus, "_client", None)
        return client is not None and getattr(client, "connected", False)
    await _wait_for(_is_connected, timeout=timeout, msg="client not connected")


async def _await_disconnected(bus: DistributedEventBus,
                              timeout: float = 10.0) -> None:
    def _is_disconnected() -> bool:
        client = getattr(bus, "_client", None)
        return client is not None and not getattr(client, "connected", True)
    await _wait_for(_is_disconnected, timeout=timeout,
                    msg="client did not observe the sever")


async def _start_server(
    broker: StreamEventBroker, cfg: TransportConfig, port: int,
) -> web.AppRunner:
    bus = DistributedEventBus(broker, cfg, role="server")
    app = web.Application()
    bus.register_server_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    # Small shutdown_timeout: these legs KILL servers while a live WS is
    # attached -- the default 60s drain would hang the suite.
    site = web.TCPSite(runner, host="127.0.0.1", port=port,
                       shutdown_timeout=0.5)
    await site.start()
    return runner


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


def _counts(broker: StreamEventBroker, ops: List[str]) -> Dict[str, int]:
    """Per-op-id occurrence count on a destination broker."""
    universe = set(ops)
    got = [ev.op_id for ev in broker.recent_history(limit=2000)
           if ev.op_id in universe]
    return {op: got.count(op) for op in ops}


def _arrivals(broker: StreamEventBroker, ops: List[str]) -> List[str]:
    """The full ordered arrival list of the tracked ids."""
    universe = set(ops)
    return [ev.op_id for ev in broker.recent_history(limit=2000)
            if ev.op_id in universe]


async def _stable_counts(
    counts_fn: Callable[[], Dict[str, int]],
    *, window_s: float = 0.5, timeout: float = 6.0,
) -> Dict[str, int]:
    """Duplicate-detection settle: poll until the counts are UNCHANGED
    for a continuous window (bounded). Condition-based -- not a bare
    sleep pretending to be synchronization."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last = counts_fn()
    stable_since = loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(0.05)
        now = counts_fn()
        t = loop.time()
        if now != last:
            last = now
            stable_since = t
        elif t - stable_since >= window_s:
            return last
    return last


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# Leg A -- kill mid-stream, exact replay onto a FRESH broker.
# --------------------------------------------------------------------------- #
def test_leg_a_kill_mid_stream_exact_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    # Client broker ring clamped to 4: broker-cursor replay physically
    # cannot recover the severed span -- WAL dependence is forced.
    monkeypatch.setenv("JARVIS_IDE_STREAM_HISTORY_MAXLEN", "4")
    # Ack cadence pushed out of reach: nothing trims pre-kill, so the WAL
    # still carries the FULL 30-event truth for the fresh broker. (A trim
    # would be correct -- delivery to broker A already happened -- but the
    # set-equality target here is the fresh broker B.)
    monkeypatch.setenv("JARVIS_BUS_ACK_EVERY_N", "1000")
    monkeypatch.setenv("JARVIS_BUS_ACK_INTERVAL_S", "600")
    wal_path = str(tmp_path / "leg_a_wal.jsonl")
    ops = ["trinity:legA-%02d" % i for i in range(30)]

    async def scenario():
        port_a, port_b, dead_port = _free_port(), _free_port(), _free_port()
        cfg_a = _cfg(monkeypatch, port_a, "server")
        cfg_b = _cfg(monkeypatch, port_b, "server")
        # Static client url dials a DEAD port -- only the resolver connects.
        client_cfg = _cfg(monkeypatch, dead_port, "client")
        path = client_cfg.path
        target: Dict[str, str] = {"url": "ws://127.0.0.1:%d%s" % (port_a, path)}

        async def resolver() -> Optional[str]:
            return target["url"]

        client_broker = StreamEventBroker()  # env-clamped ring: 4 slots
        broker_a = StreamEventBroker(history_maxlen=256)
        broker_b = StreamEventBroker(history_maxlen=256)
        durable = DurableOutbound(client_broker, wal_path=wal_path)
        await durable.start()
        runner_a = await _start_server(broker_a, cfg_a, port_a)
        bus = DistributedEventBus(
            client_broker, client_cfg, role="client",
            durable_outbound=durable, url_resolver=resolver)
        task = asyncio.ensure_future(bus.start_client())
        runner_b = None
        try:
            await _await_connected(bus)

            # Events 1..10 cross LIVE to server A.
            for op in ops[:10]:
                client_broker.publish("task_started", op, {"leg": "A"})
            await _wait_for(
                lambda: _counts(broker_a, ops[:10]) == {o: 1 for o in ops[:10]},
                msg="first 10 events did not land live on server A")
            await _wait_for(lambda: durable.pending_count() == 10,
                            msg="WAL journal lagged the first 10 publishes")

            # HARD KILL at event ~10: socket death, not a graceful close.
            await runner_a.cleanup()
            await _await_disconnected(bus)

            # Events 11..20 published DURING the partition.
            for op in ops[10:20]:
                client_broker.publish("task_started", op, {"leg": "A"})
            await _wait_for(lambda: durable.pending_count() == 20,
                            msg="partition-time publishes not journaled")
            assert bus._client.connected is False, (
                "partition invariant broken: client claims connected")
            assert durable.pending_count() >= 10, (
                "WAL must hold at least the partition-time span")
            assert durable.journal_failures == 0, (
                "durability honesty: an accepted publish is NOT on disk")

            # RESTART: fresh broker (fresh dedup state) on a NEW port; the
            # resolver's answer flips -- rediscovery lands the reconnect.
            runner_b = await _start_server(broker_b, cfg_b, port_b)
            target["url"] = "ws://127.0.0.1:%d%s" % (port_b, path)
            await _await_connected(bus)
            await _wait_for(
                lambda: sum(_counts(broker_b, ops[:20]).values()) >= 20,
                msg="WAL replay did not deliver the severed span to B")

            # Events 21..30 cross LIVE post-reconnect (pump proven running
            # by the 20 arrivals above -- no ring-gap race).
            for op in ops[20:]:
                client_broker.publish("task_started", op, {"leg": "A"})
            await _wait_for(
                lambda: all(c >= 1 for c in _counts(broker_b, ops).values()),
                msg="full 30-event universe never completed on B")
            counts = await _stable_counts(lambda: _counts(broker_b, ops))
            arrivals = _arrivals(broker_b, ops)
        finally:
            await _stop_client(bus, task)
            await durable.stop()
            if runner_b is not None:
                await runner_b.cleanup()
        return counts, arrivals

    counts, arrivals = _run(scenario())
    # THE MANDATE: mathematical set-equality on the replay synchronization.
    assert set(arrivals) == set(ops), (
        "id-set mismatch: missing=%r extra=%r"
        % (sorted(set(ops) - set(arrivals)), sorted(set(arrivals) - set(ops))))
    assert sorted(arrivals) == sorted(ops), (
        "full ordered id multiset mismatch: %r" % (sorted(arrivals),))
    assert all(counts[op] == 1 for op in ops), (
        "per-id occurrence != 1: %r"
        % ({op: c for op, c in counts.items() if c != 1},))
    assert len(arrivals) == 30, "count != 30: %d" % len(arrivals)


# --------------------------------------------------------------------------- #
# Leg B -- Body process death mid-partition: disk-truth durability.
# --------------------------------------------------------------------------- #
def test_leg_b_body_process_death_mid_partition(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    # Fast acks: the pre-partition events are trimmed (persisted tombstones)
    # BEFORE the partition, so exactly N=8 are pending at death.
    monkeypatch.setenv("JARVIS_BUS_ACK_EVERY_N", "1")
    monkeypatch.setenv("JARVIS_BUS_ACK_INTERVAL_S", "0.2")
    wal_path = str(tmp_path / "leg_b_wal.jsonl")
    pre_ops = ["trinity:legB-pre-%d" % i for i in range(3)]
    part_ops = ["trinity:legB-part-%d" % i for i in range(8)]

    async def scenario():
        port_a, port_b = _free_port(), _free_port()
        cfg_a = _cfg(monkeypatch, port_a, "server")
        cfg_b = _cfg(monkeypatch, port_b, "server")
        client_cfg = _cfg(monkeypatch, port_a, "client")
        path = client_cfg.path

        # ----- incarnation 1: deliver 3, then partition with 8 pending -----
        broker1 = StreamEventBroker(history_maxlen=64)
        broker_a = StreamEventBroker(history_maxlen=256)
        durable1 = DurableOutbound(broker1, wal_path=wal_path)
        await durable1.start()
        # Journal the pre-partition events BEFORE connecting so their WAL
        # entries exist before any ack can arrive. (A live publish's append
        # is offloaded; an ack that lands inside that window legitimately
        # strands the entry pending until the NEXT ack -- at-least-once by
        # design, absorbed by server dedup. Pre-journaling keeps THIS leg's
        # trim-persistence assertion deterministic.)
        for op in pre_ops:
            broker1.publish("task_started", op, {"leg": "B"})
        await _wait_for(lambda: durable1.pending_count() == 3,
                        msg="pre-partition publishes not journaled")
        runner_a = await _start_server(broker_a, cfg_a, port_a)
        bus1 = DistributedEventBus(
            broker1, client_cfg, role="client", durable_outbound=durable1)
        task1 = asyncio.ensure_future(
            bus1.start_client("ws://127.0.0.1:%d%s" % (port_a, path)))
        try:
            await _await_connected(bus1)
            await _wait_for(
                lambda: _counts(broker_a, pre_ops) == {o: 1 for o in pre_ops},
                msg="pre-partition events did not cross via WAL replay")
            # Ack lane trims them; _ack_inflight empty == tombstones LANDED
            # on disk (the flush pops only after update_status succeeds).
            await _wait_for(
                lambda: (durable1.pending_count() == 0
                         and not durable1._ack_inflight),
                msg="ack-driven trim did not persist pre-partition")

            # Partition: hard-kill the server, then publish N=8.
            await runner_a.cleanup()
            await _await_disconnected(bus1)
            for op in part_ops:
                broker1.publish("task_started", op, {"leg": "B"})
            await _wait_for(lambda: durable1.pending_count() == 8,
                            msg="partition publishes not journaled")
            assert durable1.journal_failures == 0, (
                "durability honesty: an accepted publish is NOT on disk")
        finally:
            # Body death: destroy EVERY client-side object. (An in-proc test
            # cannot SIGKILL itself; the durability property under test is
            # already sealed on disk -- the 8 appends landed at publish time,
            # upstream of anything stop() flushes.)
            await _stop_client(bus1, task1)
            await durable1.stop()
        del bus1, durable1, broker1

        # ----- incarnation 2: NEW objects, SAME wal_path -----------------
        broker2 = StreamEventBroker(history_maxlen=64)
        durable2 = DurableOutbound(broker2, wal_path=wal_path)
        await durable2.start()
        # Disk truth: exactly the 8 partition events are pending; the 3
        # acked ones stay dead (their tombstones persisted).
        assert durable2.pending_count() == 8, (
            "disk recovery expected exactly 8 pending, got %d"
            % durable2.pending_count())

        broker_b = StreamEventBroker(history_maxlen=256)
        runner_b = await _start_server(broker_b, cfg_b, port_b)
        bus2 = DistributedEventBus(
            broker2, client_cfg, role="client", durable_outbound=durable2)
        task2 = asyncio.ensure_future(
            bus2.start_client("ws://127.0.0.1:%d%s" % (port_b, path)))
        try:
            await _await_connected(bus2)
            await _wait_for(
                lambda: all(c >= 1 for c in _counts(broker_b, part_ops).values()),
                msg="recovered WAL did not deliver the 8 pending")
            counts = await _stable_counts(lambda: _counts(broker_b, part_ops))
            resurrected = _counts(broker_b, pre_ops)
        finally:
            await _stop_client(bus2, task2)
            await durable2.stop()
            await runner_b.cleanup()
        return counts, resurrected

    counts, resurrected = _run(scenario())
    assert counts == {op: 1 for op in part_ops}, (
        "the N pending must cross EXACTLY once, got %r" % (counts,))
    assert resurrected == {op: 0 for op in pre_ops}, (
        "acked+trimmed events resurrected across the object boundary: %r"
        % (resurrected,))


# --------------------------------------------------------------------------- #
# Leg C -- Brain service death: HMAC suspend/resume, fail-closed tamper gate.
# --------------------------------------------------------------------------- #
def test_leg_c_hmac_suspend_resume_fail_closed(tmp_path, monkeypatch):
    # Deterministic signing key (no dependence on a persisted host key).
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET",
                       "task5-deliberate-partition-secret")
    monkeypatch.delenv("JARVIS_CHECKPOINT_TTL_S", raising=False)
    base = str(tmp_path)

    class _Ctx:
        """Synthetic in-flight op context carrying exactly the attributes
        capture_from_context reads (module API used UNCHANGED)."""

        def __init__(self, op_id: str) -> None:
            self.op_id = op_id
            self.description = "deliberate-partition suspend/resume drill"
            self.target_files = ["backend/core/ouroboros/example.py"]
            self.intake_evidence_json = ""
            self.provider_route = "STANDARD"
            self.signal_source = "partition_test"

    # SUSPEND: drive capture_inflight through its REAL contract -- the
    # in-flight registry snapshot (registry data structure works master-off
    # by design; capture_inflight does not gate on the master flag).
    ifr.reset_default_registry()
    try:
        reg = ifr.get_default_registry()
        assert reg.register("op-clean", ctx_ref=_Ctx("op-clean"),
                            last_phase_name="GENERATE") is not None
        assert reg.register("op-tampered", ctx_ref=_Ctx("op-tampered"),
                            last_phase_name="VALIDATE") is not None
        n = fc.capture_inflight(base_dir=base, reason="partition_test")
        assert n == 2, "capture_inflight must checkpoint both in-flight ops"
    finally:
        ifr.reset_default_registry()

    # Both come back HMAC-VERIFIED.
    pending = fc.list_pending(base_dir=base)
    assert sorted(cp.op_id for cp in pending) == ["op-clean", "op-tampered"]
    by_id = {cp.op_id: cp for cp in pending}
    assert by_id["op-clean"].phase == "GENERATE"
    assert by_id["op-tampered"].phase == "VALIDATE"
    assert all(cp.resume_reason == "partition_test" for cp in pending)

    # TAMPER exactly ONE byte inside the signed payload -- the wrapper and
    # the payload both stay VALID JSON, so only the crypto gate can reject.
    path = os.path.join(fc.checkpoint_dir(base), "op-tampered.json")
    with open(path, "r", encoding="utf-8") as fh:
        wrapper = json.loads(fh.read())
    payload = wrapper["payload"]
    assert "partition_test" in payload
    tampered = payload.replace("partition_test", "partition_tesX", 1)
    assert len(tampered) == len(payload) and tampered != payload
    wrapper["payload"] = tampered
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(wrapper))

    # Fail-closed: the tampered checkpoint is REJECTED, the twin survives.
    pending_after = fc.list_pending(base_dir=base)
    assert [cp.op_id for cp in pending_after] == ["op-clean"], (
        "one-byte tamper must be rejected by the HMAC gate (zero torn "
        "ledger), got %r" % [cp.op_id for cp in pending_after])

    # RESUME: the untampered twin hydrates EXACTLY once.
    ingested: List[Dict[str, Any]] = []
    resumed = fc.hydrate_pending_checkpoints(ingested.append, base_dir=base)
    assert resumed == 1
    assert [env["op_id"] for env in ingested] == ["op-clean"]
    assert ingested[0]["resume"] is True
    assert ingested[0]["resume_phase"] == "GENERATE"
    assert ingested[0]["source"] == "fsm_resume"
    # Exactly-once proof: hydrate consumed the file, so a second
    # mark_resumed finds nothing to consume...
    assert fc.mark_resumed("op-clean", base_dir=base) is False
    # ...and a second hydrate injects nothing.
    assert fc.hydrate_pending_checkpoints(ingested.append, base_dir=base) == 0
    assert len(ingested) == 1
    # The tampered file stays on disk (rejected, never hydrated, never
    # silently deleted) -- auditable, fail-closed.
    assert os.path.isfile(path)
    assert fc.list_pending(base_dir=base) == []


# --------------------------------------------------------------------------- #
# Leg D -- double-replay of the SAME WAL pending: no duplicated terminal state.
# --------------------------------------------------------------------------- #
def test_leg_d_double_replay_single_terminal_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    monkeypatch.setenv("JARVIS_BUS_ACK_EVERY_N", "1")
    monkeypatch.setenv("JARVIS_BUS_ACK_INTERVAL_S", "0.2")
    wal_path = tmp_path / "leg_d_wal.jsonl"
    snapshot_path = tmp_path / "leg_d_wal.pre_trim_snapshot"
    ops = ["trinity:legD-%d" % i for i in range(6)]

    async def scenario():
        port = _free_port()
        server_cfg = _cfg(monkeypatch, port, "server")
        client_cfg = _cfg(monkeypatch, port, "client")
        url = "ws://127.0.0.1:%d%s" % (port, client_cfg.path)

        # ONE server instance across both reconnects: its qualified-id
        # dedup memory is the single authority under test.
        server_broker = StreamEventBroker(history_maxlen=256)
        runner = await _start_server(server_broker, server_cfg, port)

        # ----- delivery 1 -------------------------------------------------
        broker1 = StreamEventBroker(history_maxlen=64)
        durable1 = DurableOutbound(broker1, wal_path=str(wal_path))
        await durable1.start()
        for op in ops:
            broker1.publish("task_started", op, {"leg": "D"})
        await _wait_for(lambda: durable1.pending_count() == 6,
                        msg="publishes not journaled")
        # Snapshot the PRE-TRIM disk bytes: this IS the worst-case race
        # frozen deterministically -- "acks processed, trim never persisted".
        shutil.copyfile(wal_path, snapshot_path)

        bus1 = DistributedEventBus(
            broker1, client_cfg, role="client", durable_outbound=durable1)
        task1 = asyncio.ensure_future(bus1.start_client(url))
        try:
            await _await_connected(bus1)
            await _wait_for(
                lambda: all(c >= 1 for c in _counts(server_broker, ops).values()),
                msg="first delivery incomplete")
            # Acks processed + trim persisted in THIS incarnation.
            await _wait_for(
                lambda: (durable1.pending_count() == 0
                         and not durable1._ack_inflight),
                msg="first-delivery acks never trimmed the WAL")
        finally:
            await _stop_client(bus1, task1)
            await durable1.stop()

        # Crash-with-torn-trim: the persisted trim is LOST -- restore the
        # pre-trim WAL bytes, so the next incarnation re-sends everything.
        shutil.copyfile(snapshot_path, wal_path)

        # ----- delivery 2 (same ids, second reconnect) --------------------
        broker2 = StreamEventBroker(history_maxlen=64)
        durable2 = DurableOutbound(broker2, wal_path=str(wal_path))
        await durable2.start()
        assert durable2.pending_count() == 6, (
            "restored WAL must re-arm the SAME 6 for a second delivery")
        bus2 = DistributedEventBus(
            broker2, client_cfg, role="client", durable_outbound=durable2)
        task2 = asyncio.ensure_future(bus2.start_client(url))
        try:
            await _await_connected(bus2)
            # Positive receipt proof: the server acks the re-sent frames
            # (its ack cadence counts every EVENT frame, deduped or not),
            # so the restored WAL trims to 0 ONLY if the double-replay was
            # actually received on the wire. No vacuous pass possible.
            await _wait_for(lambda: durable2.pending_count() == 0,
                            msg="second delivery never reached the server")
            counts = await _stable_counts(lambda: _counts(server_broker, ops))
        finally:
            await _stop_client(bus2, task2)
            await durable2.stop()
            await runner.cleanup()
        return counts

    counts = _run(scenario())
    assert counts == {op: 1 for op in ops}, (
        "double-replay must NOT duplicate terminal state -- qualified-id "
        "dedup is the single authority; got %r" % (counts,))
