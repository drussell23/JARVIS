# -*- coding: utf-8 -*-
"""Stage 3 Task 2: DurableOutbound -- the Body-side durable WAL journal.

Journals every bridgeable (op_prefix-matched) StreamEventBroker event at
PUBLISH time -- upstream of connection state, so a partition or a Body
crash can never lose a publish() the broker accepted. Trims on ack
(Task 1's BusBridgeClient on_ack hook target), with a dynamic
disk-fraction capacity guard (no hardcoded byte caps).

Suite map (task brief + review round):
  (a) journal-at-publish: only op_prefix events land, ordered by event_id
  (b) crash-survival: a fresh instance on the same wal_path recovers the
      pending set from journaled disk truth, not memory
  (c) ack-trim: on_ack(<eid>) trims every pending id <= eid, in memory
      immediately and on disk durably (fresh instance agrees)
  (d) dynamic capacity: tiny disk free -> degraded_capacity flips True,
      oldest pending dropped (dead_letter), exactly ONE warning per
      episode, newest survives; restored disk clears the flag; compact
      fires ONLY at episode onset (review IMPORTANT-2)
  (e) zero hardcoded caps: module source contains no byte-size literals;
      JARVIS_BODY_WAL_DISK_FRACTION is the only capacity knob
  (f) review CRITICAL: a failed WAL append is NEVER reported as
      pending-durable -- surfaced via journal_failures, ONE warning per
      episode, retried on later journal cycles, lands when fault clears
  (g) review IMPORTANT-1: a failed ack tombstone keeps the lease parked
      for retry (no live-acked/disk-pending split-brain loss), ONE
      warning per episode, drains once the fault clears
  (h) Task-4 carry-in (Task-3 review): ``journal_filter`` -- when
      provided, only events the filter accepts are journaled.
      Peer-republished events (payload origin != local source_id)
      carry client-local event ids the server has never seen;
      replaying them defeats qualified-id dedup and duplicates events
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
    # the same 5 pending entries purely from the flock-journaled WAL file.
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

    # Review IMPORTANT-2: compact must fire ONLY at episode onset, not on
    # every over-budget append while already degraded.
    compact_calls = {"n": 0}

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)

        real_compact = outbound._wal.compact

        def _counting_compact():
            compact_calls["n"] += 1
            return real_compact()

        monkeypatch.setattr(outbound._wal, "compact", _counting_compact)

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

    assert compact_calls["n"] == 1, (
        "compact must run ONLY at degraded-episode onset, ran %d times"
        % compact_calls["n"])

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


# --------------------------------------------------------------------------- #
# (f) review CRITICAL: WAL.append failure (the ENOSPC class this module
#     exists to survive) must NOT be reported as pending-durable. The event
#     is surfaced as at-risk (journal_failures), warned ONCE per episode,
#     retried on subsequent journal cycles, and lands when the fault clears.
# --------------------------------------------------------------------------- #
def test_journal_append_failure_not_falsely_durable_then_retried(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")
    logger_name = "backend.core.ouroboros.governance.transport.durable_outbound"

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)
        await outbound.start()
        try:
            real_append = outbound._wal.append
            fault = {"on": True}

            def _flaky_append(entry):
                if fault["on"]:
                    raise OSError(28, "No space left on device")
                return real_append(entry)

            monkeypatch.setattr(outbound._wal, "append", _flaky_append)

            e1 = broker.publish(_EVENT_TYPE, _PREFIX + "t-0", {"i": 0})
            e2 = broker.publish(_EVENT_TYPE, _PREFIX + "t-1", {"i": 1})
            await _wait_for(lambda: outbound.journal_failures == 2)

            # The durability claim must be HONEST: nothing landed on disk,
            # so nothing may be reported as pending-durable.
            assert outbound.pending_count() == 0, (
                "a failed append must NOT surface as pending-durable")
            probe = DurableOutbound(
                StreamEventBroker(history_maxlen=4), wal_path=wal_path)
            assert probe.pending_count() == 0, (
                "disk must agree: nothing was journaled")

            # Fault clears -> the next journal cycle retries the at-risk
            # entries, then journals the new event. All three land.
            fault["on"] = False
            e3 = broker.publish(_EVENT_TYPE, _PREFIX + "t-2", {"i": 2})
            await _wait_for(lambda: (outbound.pending_count() == 3
                                     and outbound.journal_failures == 0))
            assert _pending_ids(outbound) == sorted([e1, e2, e3])
            return [e1, e2, e3]
        finally:
            await outbound.stop()

    with caplog.at_level(logging.WARNING, logger=logger_name):
        ids = _run(scenario())

    fresh = DurableOutbound(StreamEventBroker(history_maxlen=4), wal_path=wal_path)
    assert _pending_ids(fresh) == sorted(ids), (
        "retried at-risk events must be durably journaled once the fault clears")

    warnings = [r for r in caplog.records
                if r.name == logger_name and r.levelno == logging.WARNING
                and "journal append FAILED" in r.getMessage()]
    assert len(warnings) == 1, (
        "exactly ONE journal-failure warning per episode, got %d"
        % len(warnings))


# --------------------------------------------------------------------------- #
# (g) review IMPORTANT-1: ack tombstone write failure must not produce the
#     live-says-acked / disk-says-pending split silently -- the lease stays
#     parked for retry, ONE distinguishing warning per episode, and the
#     tombstones land once the fault clears.
# --------------------------------------------------------------------------- #
def test_ack_tombstone_failure_parks_lease_for_retry(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")
    logger_name = "backend.core.ouroboros.governance.transport.durable_outbound"

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)
        await outbound.start()
        try:
            ids = [broker.publish(_EVENT_TYPE, _PREFIX + "t-%d" % i, {"i": i})
                   for i in range(3)]
            await _wait_for(lambda: outbound.pending_count() == 3)

            real_update = outbound._wal.update_status
            fault = {"on": True}

            def _flaky_update(lease_id, status):
                if fault["on"] and status == "acked":
                    raise OSError(5, "injected tombstone write failure")
                return real_update(lease_id, status)

            monkeypatch.setattr(outbound._wal, "update_status", _flaky_update)

            outbound.on_ack(ids[1])  # acks ids[0..1]
            # Immediate in-memory ack semantics still hold (redelivery is
            # SAFE downstream -- server dedup)...
            assert outbound.pending_count() == 1

            # ...but the failed tombstones park the leases for retry.
            await _wait_for(lambda: len(outbound._ack_retry) == 2)
            probe = DurableOutbound(
                StreamEventBroker(history_maxlen=4), wal_path=wal_path)
            assert probe.pending_count() == 3, (
                "disk truth must still show 3 pending -- no tombstone landed")

            # Fault clears -> a later flush retries the parked leases.
            fault["on"] = False
            outbound.on_ack(ids[1])  # no new ids; drains the retry park
            await _wait_for(lambda: not outbound._ack_retry)

            def _disk_agrees() -> bool:
                p = DurableOutbound(
                    StreamEventBroker(history_maxlen=4), wal_path=wal_path)
                return p.pending_count() == 1
            await _wait_for(_disk_agrees)
            assert outbound.pending_count() == 1
            return ids
        finally:
            await outbound.stop()

    with caplog.at_level(logging.WARNING, logger=logger_name):
        ids = _run(scenario())

    fresh = DurableOutbound(StreamEventBroker(history_maxlen=4), wal_path=wal_path)
    assert _pending_ids(fresh) == [sorted(ids)[2]], (
        "acked leases must be durably tombstoned once the fault clears")

    warnings = [r for r in caplog.records
                if r.name == logger_name and r.levelno == logging.WARNING
                and "ack tombstone" in r.getMessage()]
    assert len(warnings) == 1, (
        "exactly ONE ack-tombstone-failure warning per episode, got %d"
        % len(warnings))


# --------------------------------------------------------------------------- #
# (h) Task-3 carry-forward (re-review): a cumulative ack for Y must NOT
#     purge an at-risk entry X <= Y -- the ack proves the server's ingest
#     cursor reached Y, not that it ever RECEIVED the gap X (X was never
#     durably journaled; if the process dies now, X is lost forever).
#     Retention costs only a duplicate send, which server-side qualified-id
#     dedup makes safe. The entry must keep retrying until it lands in the
#     WAL, and then surface via pending() for replay.
# --------------------------------------------------------------------------- #
def test_ack_past_at_risk_entry_retains_it_until_journaled(
        tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(broker, wal_path=wal_path)
        await outbound.start()
        try:
            real_append = outbound._wal.append
            fault = {"on": True}

            def _flaky_append(entry):
                if fault["on"]:
                    raise OSError(28, "No space left on device")
                return real_append(entry)

            monkeypatch.setattr(outbound._wal, "append", _flaky_append)

            e1 = broker.publish(_EVENT_TYPE, _PREFIX + "risk-0", {"i": 0})
            await _wait_for(lambda: outbound.journal_failures == 1)

            # A cumulative ack that passes e1 arrives while e1 is at risk.
            outbound.on_ack(e1)
            await asyncio.sleep(0.1)
            assert outbound.journal_failures == 1, (
                "an ack for Y must NOT purge the at-risk gap X<=Y -- the "
                "ack cannot prove X was received")

            # Fault clears -> the next journal cycle lands e1 durably.
            fault["on"] = False
            e2 = broker.publish(_EVENT_TYPE, _PREFIX + "risk-1", {"i": 1})
            await _wait_for(lambda: (outbound.journal_failures == 0
                                     and outbound.pending_count() == 2))
            assert _pending_ids(outbound) == sorted([e1, e2]), (
                "the retained entry must land in the WAL and surface via "
                "pending() for replay")
            return [e1, e2]
        finally:
            await outbound.stop()

    ids = _run(scenario())
    fresh = DurableOutbound(StreamEventBroker(history_maxlen=4), wal_path=wal_path)
    assert _pending_ids(fresh) == sorted(ids), (
        "disk truth must retain the once-at-risk entry after the fault clears")


# --------------------------------------------------------------------------- #
# (h) journal_filter: peer-origin events are NOT journaled; locally-
#     originated events (and origin-less events -- fail-open durability
#     bias) are. The driver's live default excludes peer republications:
#     they carry client-local event ids the server has never seen, so
#     replaying them at reconnect defeats qualified-id dedup.
# --------------------------------------------------------------------------- #
def test_journal_filter_excludes_peer_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")
    local_source = "mac-body"

    def _local_only(event: Any) -> bool:
        return (getattr(event, "payload", None) or {}).get(
            "origin") in (None, "", local_source)

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(
            broker, wal_path=wal_path, journal_filter=_local_only)
        await outbound.start()
        try:
            local_id = broker.publish(
                _EVENT_TYPE, _PREFIX + "topic-a",
                {"origin": local_source, "i": 0})
            peer_id = broker.publish(
                _EVENT_TYPE, _PREFIX + "topic-b",
                {"origin": "gcp-brain", "i": 1})
            bare_id = broker.publish(
                _EVENT_TYPE, _PREFIX + "topic-c", {"i": 2})
            assert local_id and peer_id and bare_id
            await _wait_for(lambda: outbound.pending_count() >= 2)
            await asyncio.sleep(0.1)  # settle: catch a late (wrong) journal
            return local_id, peer_id, bare_id, _pending_ids(outbound)
        finally:
            await outbound.stop()

    local_id, peer_id, bare_id, got = _run(scenario())
    assert got == sorted([local_id, bare_id]), (
        "local-origin + origin-less events must journal; got %r" % (got,))
    assert peer_id not in got, (
        "peer-republished (origin=gcp-brain) event must NOT be journaled")

    # Disk truth agrees: a fresh instance recovers exactly the two.
    fresh = DurableOutbound(
        StreamEventBroker(history_maxlen=4), wal_path=wal_path)
    assert _pending_ids(fresh) == sorted([local_id, bare_id])


# --------------------------------------------------------------------------- #
# (h2) review round: a RAISING journal_filter fails OPEN (journals anyway --
#      durability over dedup) but must NOT be silent: one warning per
#      episode, episode reset when the filter succeeds again (the file's
#      established warn-once pattern: WAL append / ack tombstone / capacity).
# --------------------------------------------------------------------------- #
def test_journal_filter_failure_warns_once_and_fails_open(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_BODY_WAL_PROBE_INTERVAL_S", "0")
    wal_path = str(tmp_path / "body_wal.jsonl")
    fault = {"on": True}

    def _flaky_filter(event: Any) -> bool:
        if fault["on"]:
            raise RuntimeError("filter boom")
        return (getattr(event, "payload", None) or {}).get(
            "origin") != "peer"

    def _filter_warnings() -> List[Any]:
        return [r for r in caplog.records
                if r.levelno == logging.WARNING
                and "journal_filter" in r.getMessage()]

    async def scenario():
        broker = StreamEventBroker(history_maxlen=64)
        outbound = DurableOutbound(
            broker, wal_path=wal_path, journal_filter=_flaky_filter)
        await outbound.start()
        try:
            with caplog.at_level(logging.INFO):
                # Episode 1: two raising evaluations -> BOTH events still
                # journal (fail open) but exactly ONE warning fires.
                e1 = broker.publish(
                    _EVENT_TYPE, _PREFIX + "f-0", {"origin": "peer"})
                e2 = broker.publish(
                    _EVENT_TYPE, _PREFIX + "f-1", {"origin": "peer"})
                await _wait_for(lambda: outbound.pending_count() == 2)
                assert _pending_ids(outbound) == sorted([e1, e2]), (
                    "fail-open: a raising filter must journal anyway")
                assert len(_filter_warnings()) == 1, (
                    "exactly one warning per failure episode")

                # Recovery: the filter works again -> episode resets and
                # peer exclusion is back in force.
                fault["on"] = False
                e3 = broker.publish(
                    _EVENT_TYPE, _PREFIX + "f-2", {"origin": "peer"})
                e4 = broker.publish(
                    _EVENT_TYPE, _PREFIX + "f-3", {"origin": "local"})
                await _wait_for(lambda: outbound.pending_count() == 3)
                got = _pending_ids(outbound)
                assert e3 not in got, "recovered filter excludes peer again"
                assert e4 in got

                # Episode 2: a NEW fault warns again (episode was reset).
                fault["on"] = True
                broker.publish(
                    _EVENT_TYPE, _PREFIX + "f-4", {"origin": "peer"})
                await _wait_for(lambda: outbound.pending_count() == 4)
                assert len(_filter_warnings()) == 2, (
                    "a fresh failure episode must warn again")
        finally:
            await outbound.stop()

    _run(scenario())
