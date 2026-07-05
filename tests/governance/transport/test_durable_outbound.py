# -*- coding: utf-8 -*-
"""Stage 3 Task 2: DurableOutbound -- the Body-side durable WAL journal.

Journals every bridgeable (op_prefix-matched) StreamEventBroker event at
PUBLISH time -- upstream of connection state, so a partition or a Body
crash can never lose a publish() the broker accepted. Trims on ack
(Task 1's BusBridgeClient on_ack hook target), with a dynamic
disk-fraction capacity guard (no hardcoded byte caps).

Suite map (per the task brief):
  (a) journal-at-publish: only op_prefix events land, ordered by event_id
  (b) crash-survival: a fresh instance on the same wal_path recovers the
      pending set from fsync'd disk truth, not memory
  (c) ack-trim: on_ack(<eid>) trims every pending id <= eid, in memory
      immediately and on disk durably (fresh instance agrees)
  (d) dynamic capacity: tiny disk free -> degraded_capacity flips True,
      oldest pending dropped (dead_letter), exactly ONE warning per
      episode, newest survives; restored disk clears the flag
  (e) zero hardcoded caps: module source contains no byte-size literals;
      JARVIS_BODY_WAL_DISK_FRACTION is the only capacity knob
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import types
from pathlib import Path
from typing import Any, Callable, List

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.durable_outbound import (
    DurableOutbound,
)

_EVENT_TYPE = "task_started"  # valid broker type (see trinity_bus_bridge)
_PREFIX = "trinity:"

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "backend" / "core" / "ouroboros" / "governance" / "transport"
    / "durable_outbound.py"
)


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within %.1fs" % timeout)


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _pending_ids(outbound: DurableOutbound) -> List[str]:
    return [d["event_id"] for d in outbound.pending()]


# --------------------------------------------------------------------------- #
# (a) journal-at-publish: 5 trinity events + 2 non-prefix events -> exactly
#     the 5 trinity events are journaled, ordered by event_id.
# --------------------------------------------------------------------------- #
def test_journal_at_publish_filters_and_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)
        await outbound.start()
        try:
            trinity_ids = []
            for i in range(5):
                eid = broker.publish(_EVENT_TYPE, _PREFIX + "topic-%d" % i, {"i": i})
                assert eid
                trinity_ids.append(eid)
                if i == 2:  # interleave the non-prefix noise mid-stream
                    assert broker.publish(_EVENT_TYPE, "other-op-a", {})
                    assert broker.publish(_EVENT_TYPE, "other-op-b", {})
            await _wait_for(lambda: outbound.pending_count() == 5)
            return trinity_ids, _pending_ids(outbound), outbound.pending_count()
        finally:
            await outbound.stop()

    trinity_ids, got_ids, count = _run(scenario())
    assert count == 5, "exactly the 5 trinity-prefixed events must be journaled"
    assert got_ids == sorted(trinity_ids), (
        "pending() must be ordered by event_id: got %r want %r"
        % (got_ids, sorted(trinity_ids)))


# --------------------------------------------------------------------------- #
# (b) crash-survival -- THE WAL point: tear down without acks; a brand-new
#     instance on the same wal_path recovers all 5 from disk.
# --------------------------------------------------------------------------- #
def test_crash_survival_fresh_instance_recovers_from_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)
        await outbound.start()
        try:
            ids = [broker.publish(_EVENT_TYPE, _PREFIX + "t-%d" % i, {"i": i})
                   for i in range(5)]
            await _wait_for(lambda: outbound.pending_count() == 5)
        finally:
            await outbound.stop()  # simulated crash boundary: instance gone
        return ids

    ids = _run(scenario())

    # A NEW instance -- new broker, no start(), no shared memory -- must see
    # the same 5 pending entries purely from the fsync'd WAL file.
    fresh = DurableOutbound(StreamEventBroker(history_maxlen=4), wal_path=wal_path)
    assert fresh.pending_count() == 5
    assert _pending_ids(fresh) == sorted(ids), (
        "recovered pending set must match the journaled publishes")


# --------------------------------------------------------------------------- #
# (c) ack-trim: on_ack(<3rd id>) trims ids 1..3 -> pending_count()==2,
#     immediately in memory and durably on disk (fresh instance agrees).
# --------------------------------------------------------------------------- #
def test_ack_trim_immediate_and_durable(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)
        await outbound.start()
        try:
            ids = [broker.publish(_EVENT_TYPE, _PREFIX + "t-%d" % i, {"i": i})
                   for i in range(5)]
            await _wait_for(lambda: outbound.pending_count() == 5)

            outbound.on_ack(ids[2])  # cumulative cursor: acks ids[0..2]
            # IMMEDIATE in-memory effect -- before any disk tombstone lands.
            assert outbound.pending_count() == 2
            assert _pending_ids(outbound) == sorted(ids)[3:]

            # Durable effect: poll fresh instances until the tombstones land.
            def _disk_agrees() -> bool:
                probe = DurableOutbound(
                    StreamEventBroker(history_maxlen=4), wal_path=wal_path)
                return probe.pending_count() == 2
            await _wait_for(_disk_agrees)
        finally:
            await outbound.stop()
        return ids

    ids = _run(scenario())
    fresh = DurableOutbound(StreamEventBroker(history_maxlen=4), wal_path=wal_path)
    assert _pending_ids(fresh) == sorted(ids)[3:], (
        "disk truth must agree with the in-memory trim after the ack flush")


# --------------------------------------------------------------------------- #
# (d) dynamic capacity: tiny free disk -> degraded_capacity True, oldest
#     pending dead-lettered with exactly ONE warning per episode, newest
#     survives; restoring the disk clears the flag on the next append.
# --------------------------------------------------------------------------- #
def test_dynamic_capacity_degrades_and_recovers(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")

    fake_free = {"v": None}  # None -> real shutil.disk_usage
    real_disk_usage = shutil.disk_usage

    def _fake_disk_usage(path):
        if fake_free["v"] is None:
            return real_disk_usage(path)
        return types.SimpleNamespace(total=1, used=1, free=fake_free["v"])

    monkeypatch.setattr(shutil, "disk_usage", _fake_disk_usage)

    logger_name = "backend.core.ouroboros.governance.transport.durable_outbound"

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)
        await outbound.start()
        try:
            ids = [broker.publish(_EVENT_TYPE, _PREFIX + "t-%d" % i, {"i": i})
                   for i in range(3)]
            await _wait_for(lambda: outbound.pending_count() == 3)
            assert outbound.degraded_capacity is False

            fake_free["v"] = 0  # budget = fraction * 0 -> any WAL size is over
            ids.append(broker.publish(_EVENT_TYPE, _PREFIX + "t-3", {"i": 3}))
            await _wait_for(
                lambda: _pending_ids(outbound) == sorted(ids)[1:])
            assert outbound.degraded_capacity is True

            # Second over-capacity append: drops next-oldest, NO second warning.
            ids.append(broker.publish(_EVENT_TYPE, _PREFIX + "t-4", {"i": 4}))
            await _wait_for(
                lambda: _pending_ids(outbound) == sorted(ids)[2:])
            assert outbound.degraded_capacity is True

            fake_free["v"] = None  # disk restored
            ids.append(broker.publish(_EVENT_TYPE, _PREFIX + "t-5", {"i": 5}))
            await _wait_for(
                lambda: _pending_ids(outbound) == sorted(ids)[2:])
            assert outbound.degraded_capacity is False, (
                "capacity clearing must reset the degraded episode")
            return ids, _pending_ids(outbound)
        finally:
            await outbound.stop()

    with caplog.at_level(logging.WARNING, logger=logger_name):
        ids, final_ids = _run(scenario())

    warnings = [r for r in caplog.records
                if r.name == logger_name and r.levelno == logging.WARNING
                and "degraded_capacity" in r.getMessage()]
    assert len(warnings) == 1, (
        "exactly ONE degraded_capacity warning per episode, got %d"
        % len(warnings))
    assert sorted(ids)[-1] in final_ids, "the newest publish must survive"
    assert sorted(ids)[0] not in final_ids, "the oldest pending must be dropped"


# --------------------------------------------------------------------------- #
# (e) zero hardcoded caps: the capacity logic must carry no byte-size
#     literals; JARVIS_BODY_WAL_DISK_FRACTION is the only capacity knob.
# --------------------------------------------------------------------------- #
def test_no_hardcoded_byte_caps_in_module_source():
    src = _MODULE_PATH.read_text(encoding="utf-8")

    byte_literals = re.findall(
        r"\b(?:1024|2048|4096|8192|65536|1048576|1073741824)\b"
        r"|<<\s*(?:10|20|30|40)\b"
        r"|\b\d+(?:\.\d+)?[eE]\+?\d+\b",
        src,
    )
    assert not byte_literals, (
        "capacity logic must not hardcode byte sizes, found: %r" % byte_literals)

    knobs = set(re.findall(r"JARVIS_BODY_WAL_[A-Z0-9_]+", src))
    assert knobs == {
        "JARVIS_BODY_WAL_PATH",
        "JARVIS_BODY_WAL_DISK_FRACTION",
        "JARVIS_BODY_WAL_MAX_AGE_DAYS",
        "JARVIS_BODY_WAL_COMPACT_EVERY_N",
        "JARVIS_BODY_WAL_PROBE_INTERVAL_S",
    }, "unexpected env knob set: %r" % knobs

    capacity_knobs = {k for k in knobs
                     if any(w in k for w in ("FRACTION", "SIZE", "BYTES", "CAP"))}
    assert capacity_knobs == {"JARVIS_BODY_WAL_DISK_FRACTION"}, (
        "JARVIS_BODY_WAL_DISK_FRACTION must be the ONLY capacity knob: %r"
        % capacity_knobs)
