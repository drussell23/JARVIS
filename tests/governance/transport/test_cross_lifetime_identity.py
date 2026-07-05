# -*- coding: utf-8 -*-
"""Stage 3 Task 7, live-fire finding A: CROSS-LIFETIME EVENT-ID COLLISION.

StreamEventBroker mints event ids from an in-memory sequence that
restarts at 0 every process lifetime, while the durable outbound WAL
(and the far side's qualified-id dedup) remember ids ACROSS lifetimes.
Live fire proved the failure: Body-mode process 1 journaled events
000000000001..03 and exited; process 2 on the same WAL path recovered
those 3 as pending, then its fresh broker minted the SAME ids for 3
brand-new events -- ``DurableOutbound._journal_event`` skipped them as
already-pending, so they were published-but-unjournaled: silent loss
during a partition.

The fix is monotonic identity across lifetimes:
  * ``durable_outbound.wal_high_water(path)`` -- the max id EVER
    journaled (any status, tombstones included), base-16 parse,
    corrupt-line tolerant, 0 for missing/empty (fail-soft).
  * ``StreamEventBroker(initial_event_seq=...)`` -- additive seed.
  * scripts/run_body_mode.py live default seeds the broker with the
    WAL high-water BEFORE constructing the durable outbound.

Suite map:
  (a) wal_high_water: max-ever incl. tombstoned; missing file -> 0;
      corrupt / non-hex lines skipped
  (b) THE COLLISION REPRO (RED proof): an UNSEEDED lifetime-2 broker
      re-mints lifetime-1's exact ids and the journal silently skips
      the new events (pending stays 3)
  (c) THE FIX (GREEN): a lifetime-2 broker seeded via wal_high_water
      journals 3 NEW events -> pending_count()==6, all 6 lease ids
      distinct
  (d) a seeded broker mints strictly-greater ids, 012x format preserved

Real broker + real DurableOutbound + real WAL file, no mocks -- the
in-proc fakes masked exactly this class of bug earlier this stage.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry
from backend.core.ouroboros.governance.transport.durable_outbound import (
    DurableOutbound,
    wal_high_water,
)

_EVENT_TYPE = "task_started"  # valid broker type (see trinity_bus_bridge)
_PREFIX = "trinity:"


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


def _append(wal: WAL, event_id: str) -> None:
    wal.append(WALEntry(
        lease_id=event_id,
        envelope_dict={"event_id": event_id},
        status="pending",
        ts_monotonic=time.monotonic(),
        ts_utc=datetime.now(timezone.utc).isoformat(),
    ))


async def _lifetime_publish(
    wal_path: str, n: int, *, initial_event_seq: int = 0,
) -> List[str]:
    """One full process lifetime: fresh broker (optionally seeded) +
    fresh DurableOutbound on ``wal_path``; publish ``n`` bridgeable
    events; wait for the journal consumer to settle; tear down."""
    broker = StreamEventBroker(
        history_maxlen=64, initial_event_seq=initial_event_seq)
    outbound = DurableOutbound(broker, wal_path=wal_path)
    await outbound.start()
    try:
        ids = []
        for i in range(n):
            eid = broker.publish(_EVENT_TYPE, _PREFIX + "t-%d" % i, {"i": i})
            assert eid, "publish must accept a valid bridgeable event"
            ids.append(eid)
        # Settle: drain the journal consumer past the last publish. A
        # count-based wait cannot be used here -- the COLLISION case is
        # precisely the one where the count does NOT advance.
        await _wait_for(lambda: outbound._sub.queue.qsize() == 0)
        await asyncio.sleep(0.1)
        return ids
    finally:
        await outbound.stop()  # simulated crash/exit boundary


# --------------------------------------------------------------------------- #
# (a) wal_high_water: the max id EVER journaled -- ANY status. Tombstoned
#     (acked / dead_letter) ids count: an id that was ever minted must never
#     be minted again, trimmed or not.
# --------------------------------------------------------------------------- #
def test_wal_high_water_is_max_ever_including_tombstoned(tmp_path):
    wal_path = tmp_path / "body_wal.jsonl"
    wal = WAL(wal_path)
    _append(wal, "000000000001")
    _append(wal, "00000000000a")  # 10 -- hex parse, not lexical/decimal
    _append(wal, "000000000003")
    wal.update_status("00000000000a", "acked")       # tombstone the max
    wal.update_status("000000000003", "dead_letter")  # and another

    assert wal_high_water(wal_path) == 10, (
        "high-water must be the max id EVER seen (base-16), tombstoned "
        "entries included")
    # Sanity: the tombstoned max is invisible to pending-based recovery --
    # exactly why the seed must scan ALL records, not the pending set.
    assert "00000000000a" not in [e.lease_id for e in wal.pending_entries()]


def test_wal_high_water_missing_and_empty_file_return_zero(tmp_path):
    assert wal_high_water(tmp_path / "does_not_exist.jsonl") == 0
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert wal_high_water(empty) == 0
    assert wal_high_water(str(empty)) == 0, "str paths must work too"


def test_wal_high_water_skips_corrupt_and_non_hex_lines(tmp_path):
    wal_path = tmp_path / "body_wal.jsonl"
    good = {
        "v": 1, "lease_id": "000000000005", "envelope": {},
        "status": "pending", "ts_monotonic": 0.0, "ts_utc": "",
    }
    lines = [
        "{not json at all",                                   # corrupt
        json.dumps({"v": 1, "status": "pending"}),            # no lease_id
        json.dumps(dict(good, lease_id="hb:not-hex")),        # non-hex id
        json.dumps(dict(good, lease_id="ZZZZ")),              # non-hex id
        json.dumps(good),                                     # the real max
        "",                                                   # blank
        json.dumps(dict(good, lease_id="000000000002")),
        json.dumps([1, 2, 3]),                                # non-dict record
    ]
    wal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert wal_high_water(wal_path) == 5, (
        "corrupt / non-hex / malformed lines must be skipped, valid max kept")


# --------------------------------------------------------------------------- #
# (b) THE COLLISION REPRO (RED proof -- the bug exists without the seed):
#     lifetime 1 journals 3; lifetime 2 with an UNSEEDED broker re-mints the
#     SAME ids 01..03 for 3 brand-new events -> the journal skips them as
#     already-pending. pending stays 3 = published-but-unjournaled during a
#     partition = SILENT LOSS.
# --------------------------------------------------------------------------- #
def test_unseeded_second_lifetime_collides_and_silently_drops(
        tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")

    async def scenario():
        first_ids = await _lifetime_publish(wal_path, 3)
        # Lifetime 2: same WAL path, fresh UNSEEDED broker (seq back at 0).
        second_ids = await _lifetime_publish(wal_path, 3)
        probe = DurableOutbound(
            StreamEventBroker(history_maxlen=4), wal_path=wal_path)
        return first_ids, second_ids, probe.pending_count()

    first_ids, second_ids, count = _run(scenario())
    assert second_ids == first_ids, (
        "RED proof: the unseeded lifetime-2 broker re-mints lifetime-1's "
        "exact ids -- the cross-lifetime collision is real")
    assert count == 3, (
        "RED proof: 6 accepted publishes but only 3 journaled -- the 3 "
        "colliding NEW events were silently skipped as already-pending "
        "(the live-fire silent-loss failure)")


# --------------------------------------------------------------------------- #
# (c) THE FIX (GREEN): lifetime 2 seeds its broker at wal_high_water(path)
#     -> 3 NEW events journal alongside the 3 recovered ones:
#     pending_count()==6 and all 6 lease ids DISTINCT.
# --------------------------------------------------------------------------- #
def test_seeded_second_lifetime_journals_six_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")

    async def scenario():
        first_ids = await _lifetime_publish(wal_path, 3)
        seed = wal_high_water(Path(wal_path))
        second_ids = await _lifetime_publish(
            wal_path, 3, initial_event_seq=seed)
        probe = DurableOutbound(
            StreamEventBroker(history_maxlen=4), wal_path=wal_path)
        return first_ids, seed, second_ids, _pending_ids(probe)

    first_ids, seed, second_ids, pending = _run(scenario())
    assert seed == max(int(eid, 16) for eid in first_ids)
    assert len(pending) == 6, (
        "both lifetimes' publishes must be journaled: got %d" % len(pending))
    assert len(set(pending)) == 6, (
        "all 6 journaled lease ids must be DISTINCT: %r" % (pending,))
    assert sorted(pending) == sorted(first_ids + second_ids)
    assert min(int(eid, 16) for eid in second_ids) > seed, (
        "every lifetime-2 id must be strictly above the WAL high-water")


# --------------------------------------------------------------------------- #
# (d) seeded broker mints strictly-greater ids; 012x wire format preserved
#     (zero-padded 12-hex -- plain string comparison stays correct for the
#     cumulative ack cursor and the WAL trim).
# --------------------------------------------------------------------------- #
def test_seeded_broker_mints_strictly_greater_012x_ids():
    seed = 0x2A
    broker = StreamEventBroker(history_maxlen=16, initial_event_seq=seed)
    ids = [broker.publish(_EVENT_TYPE, _PREFIX + "fmt-%d" % i, {})
           for i in range(3)]
    assert ids == [format(seed + 1 + i, "012x") for i in range(3)], (
        "seeded broker must continue the 012x sequence strictly above the "
        "seed: %r" % (ids,))
    for eid in ids:
        assert len(eid) == 12 and int(eid, 16) > seed
    assert ids == sorted(ids), "zero-padded hex must stay string-sortable"


def test_initial_event_seq_default_and_garbage_fail_soft():
    # Default: byte-identical legacy behavior (first id is 000000000001).
    legacy = StreamEventBroker(history_maxlen=16)
    assert legacy.publish(_EVENT_TYPE, _PREFIX + "d", {}) == format(1, "012x")
    # Garbage seeds degrade to the legacy 0, never raise (fail-soft).
    for bad in ("not-an-int", None, -7):
        broker = StreamEventBroker(history_maxlen=16, initial_event_seq=bad)
        assert broker.publish(_EVENT_TYPE, _PREFIX + "g", {}) == format(1, "012x"), (
            "seed %r must fail soft to the legacy sequence" % (bad,))
