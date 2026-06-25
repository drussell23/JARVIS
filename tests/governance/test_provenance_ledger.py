"""Unified Provenance Ledger -- tamper-evident hash-chained origin graph.

Behavioural spine for ``governance.provenance_ledger``:

* ingestion stamps a hash-chained ProvenanceRecord per envelope;
* a tampered / dropped chain link is DETECTED (hash mismatch / broken prev);
* ``classify_origin`` maps TestFailure -> SENSOR, RoadmapOrchestrator ->
  ROADMAP from the SignalSource ENUM (no hardcoded op-id list);
* an unknown source -> UNKNOWN (honest, never silently SENSOR);
* fail-soft: a ledger error NEVER blocks ingestion;
* gated: OFF flag -> stamp is a byte-identical no-op.
"""
from __future__ import annotations

import logging

import pytest

from backend.core.ouroboros.governance import provenance_ledger as pl
from backend.core.ouroboros.governance.intent.signals import SignalSource


# --- classify_origin: from the SignalSource ENUM, not a hardcoded op-id list -


def test_classify_test_failure_is_sensor():
    assert pl.classify_origin("test_failure") is pl.OriginClass.SENSOR
    assert pl.classify_origin(SignalSource.TEST_FAILURE) is pl.OriginClass.SENSOR


def test_classify_roadmap_is_roadmap():
    assert pl.classify_origin("roadmap") is pl.OriginClass.ROADMAP
    assert pl.classify_origin(SignalSource.ROADMAP) is pl.OriginClass.ROADMAP


def test_classify_other_known_sensors_are_sensor():
    # Every recognized SignalSource that is NOT roadmap is a sensor (ingest
    # without emit). Derived from the enum -- no hardcoded op-id map.
    for member in SignalSource:
        if member is SignalSource.ROADMAP:
            continue
        assert pl.classify_origin(member) is pl.OriginClass.SENSOR, member


def test_classify_unknown_source_is_unknown():
    assert pl.classify_origin("not_a_real_source") is pl.OriginClass.UNKNOWN
    assert pl.classify_origin("") is pl.OriginClass.UNKNOWN
    assert pl.classify_origin(None) is pl.OriginClass.UNKNOWN


def test_classify_never_raises_on_exotic_input():
    class _Boom:
        def __str__(self):  # noqa: D401
            raise RuntimeError("boom")

    assert pl.classify_origin(_Boom()) is pl.OriginClass.UNKNOWN


# --- hash-chained ledger: stamp + tamper detection -------------------------


def test_append_chains_records_from_genesis():
    led = pl.ProvenanceLedger()
    r1 = led.append(op_id="op-1", origin="test_failure", ingested_ts=1.0)
    r2 = led.append(op_id="op-2", origin="roadmap", ingested_ts=2.0)
    assert r1 is not None and r2 is not None
    assert r1.prev_hash == pl.GENESIS_HASH
    assert r2.prev_hash == r1.record_hash
    assert led.verify_chain() is True


def test_record_carries_origin_and_class():
    led = pl.ProvenanceLedger()
    r = led.append(op_id="op-1", origin="test_failure", ingested_ts=1.0)
    assert r.origin == "test_failure"
    assert r.origin_class == "sensor"
    r2 = led.append(op_id="op-2", origin=SignalSource.ROADMAP, ingested_ts=2.0)
    assert r2.origin == "roadmap"
    assert r2.origin_class == "roadmap"


def test_tampered_record_hash_is_detected():
    led = pl.ProvenanceLedger()
    led.append(op_id="op-1", origin="test_failure", ingested_ts=1.0)
    led.append(op_id="op-2", origin="roadmap", ingested_ts=2.0)
    assert led.verify_chain() is True
    # Forge the first record's origin in place (frozen dataclass -> rebuild +
    # splice into the internal deque to simulate on-the-wire tampering).
    recs = led.records()
    forged = pl.ProvenanceRecord(
        op_id=recs[0].op_id,
        origin="roadmap",  # tampered: was test_failure
        origin_class=recs[0].origin_class,
        ingested_ts=recs[0].ingested_ts,
        prev_hash=recs[0].prev_hash,
        record_hash=recs[0].record_hash,  # stale hash -> mismatch
    )
    led._records[0] = forged
    assert led.verify_chain() is False


def test_dropped_record_breaks_chain():
    led = pl.ProvenanceLedger()
    led.append(op_id="op-1", origin="test_failure", ingested_ts=1.0)
    led.append(op_id="op-2", origin="roadmap", ingested_ts=2.0)
    led.append(op_id="op-3", origin="ai_miner", ingested_ts=3.0)
    # Drop the MIDDLE record -> op-3.prev_hash no longer matches op-1's hash.
    del led._records[1]
    assert led.verify_chain() is False


def test_empty_ledger_is_vacuously_valid():
    led = pl.ProvenanceLedger()
    assert led.verify_chain() is True


def test_bounded_ring_evicts_oldest_but_chain_head_persists():
    led = pl.ProvenanceLedger(max_records=3)
    for i in range(6):
        led.append(op_id=f"op-{i}", origin="test_failure", ingested_ts=float(i))
    recs = led.records()
    assert len(recs) == 3  # bounded
    assert recs[0].op_id == "op-3"  # oldest evicted
    # The retained window still verifies (head_hash continuity preserved).
    assert led.verify_chain() is True


def test_latest_for_op_returns_record():
    led = pl.ProvenanceLedger()
    led.append(op_id="op-1", origin="test_failure", ingested_ts=1.0)
    assert led.latest_for_op("op-1").origin == "test_failure"
    assert led.latest_for_op("nope") is None


# --- stamp_provenance: structured line, gating, fail-soft ------------------


def test_stamp_emits_structured_line(caplog):
    led = pl.ProvenanceLedger()
    with caplog.at_level(logging.WARNING, logger=pl.logger.name):
        pl.stamp_provenance("op-abc", "test_failure", ledger=led)
    line = [r.getMessage() for r in caplog.records if "[Provenance]" in r.getMessage()]
    assert line, "no [Provenance] line emitted"
    msg = line[-1]
    assert "op=op-abc" in msg
    assert "origin=test_failure" in msg
    assert "origin_class=sensor" in msg
    assert "chain_ok=True" in msg


def test_stamp_roadmap_origin_class(caplog):
    led = pl.ProvenanceLedger()
    with caplog.at_level(logging.WARNING, logger=pl.logger.name):
        pl.stamp_provenance("op-r", SignalSource.ROADMAP, ledger=led)
    msg = [r.getMessage() for r in caplog.records if "[Provenance]" in r.getMessage()][-1]
    assert "origin_class=roadmap" in msg


def test_stamp_off_flag_is_byte_identical_noop(caplog, monkeypatch):
    monkeypatch.setenv("JARVIS_PROVENANCE_LEDGER_ENABLED", "false")
    led = pl.ProvenanceLedger()
    with caplog.at_level(logging.WARNING, logger=pl.logger.name):
        result = pl.stamp_provenance("op-x", "test_failure", ledger=led)
    assert result is None
    assert not [r for r in caplog.records if "[Provenance]" in r.getMessage()]
    assert led.records() == []  # nothing appended when OFF


def test_stamp_never_raises_into_caller():
    # A broken ledger must not propagate -- ingestion is the hot path.
    class _ExplodingLedger:
        def append(self, **_kw):
            raise RuntimeError("disk full")

        def verify_chain(self):
            return True

    # stamp_provenance swallows everything internally.
    out = pl.stamp_provenance("op-x", "test_failure", ledger=_ExplodingLedger())
    assert out is None


def test_default_ledger_singleton_and_reset():
    pl.reset_default_ledger()
    a = pl.get_default_ledger()
    b = pl.get_default_ledger()
    assert a is b
    pl.reset_default_ledger()
    assert pl.get_default_ledger() is not a
