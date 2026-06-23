"""tests/governance/test_outage_ledger.py -- Unit tests for OutageLedger.

All tests use tmp_path and a direct OutageLedger(path=..., max_records=...)
instantiation to avoid touching the global singleton or any real .jarvis dir.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.outage_ledger import (
    OutageLedger,
    OutageRecord,
    _INFLIGHT_TASKS,
    emit_outage_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ledger(tmp_path, max_records: int = 50) -> OutageLedger:
    return OutageLedger(
        path=str(tmp_path / "test_ledger.jsonl"),
        max_records=max_records,
    )


# ---------------------------------------------------------------------------
# Test 1: open/close round-trip
# ---------------------------------------------------------------------------

def test_open_close_round_trip(tmp_path):
    ledger = _make_ledger(tmp_path)
    oid = ledger.open_outage(
        failure_mode="TIMEOUT",
        error_codes=["err-1"],
        lane="batch",
        model_ids=["model-a"],
        dilation_hops=3,
    )
    assert oid, "open_outage should return a non-empty id"

    records_before = ledger.recent(10)
    assert len(records_before) == 1
    assert records_before[0].ended_ts is None
    assert records_before[0].duration_s is None

    ledger.close_outage(oid, served_by_jprime=True, jprime_uptime_s=42.5)

    records_after = ledger.recent(10)
    assert len(records_after) == 1
    rec = records_after[0]
    assert rec.ended_ts is not None, "ended_ts should be stamped after close"
    assert rec.duration_s is not None, "duration_s should be stamped after close"
    assert rec.duration_s >= 0.0
    assert rec.served_by_jprime is True
    assert rec.jprime_uptime_s == pytest.approx(42.5)


# ---------------------------------------------------------------------------
# Test 2: bounded ring
# ---------------------------------------------------------------------------

def test_bounded_ring(tmp_path):
    max_n = 5
    ledger = _make_ledger(tmp_path, max_records=max_n)
    # Open and close max_n + 5 outages
    for i in range(max_n + 5):
        oid = ledger.open_outage(lane=f"lane-{i}")
        ledger.close_outage(oid)

    records = ledger.recent(100)
    assert len(records) <= max_n, (
        f"ring should be bounded to {max_n}, got {len(records)}"
    )


# ---------------------------------------------------------------------------
# Test 3: recent(n) limit
# ---------------------------------------------------------------------------

def test_recent_n(tmp_path):
    ledger = _make_ledger(tmp_path, max_records=50)
    for i in range(10):
        oid = ledger.open_outage(lane=f"lane-{i}")
        ledger.close_outage(oid)

    result = ledger.recent(3)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Test 4: has_open_outage true and false
# ---------------------------------------------------------------------------

def test_has_open_outage_true_and_false(tmp_path):
    ledger = _make_ledger(tmp_path)
    assert not ledger.has_open_outage(), "should be False when empty"

    oid = ledger.open_outage(lane="realtime")
    assert ledger.has_open_outage(), "should be True after open"

    ledger.close_outage(oid)
    assert not ledger.has_open_outage(), "should be False after close"


# ---------------------------------------------------------------------------
# Test 5: dedup -- double open returns same id
# ---------------------------------------------------------------------------

def test_dedup_double_open(tmp_path):
    ledger = _make_ledger(tmp_path)
    id1 = ledger.open_outage(lane="batch+realtime", failure_mode="TIMEOUT")
    id2 = ledger.open_outage(lane="batch+realtime", failure_mode="TIMEOUT")
    assert id1 == id2, "second open on same lane should return the existing id"

    # Only one record should exist
    records = ledger.recent(50)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Test 6: fail-soft on corrupt file
# ---------------------------------------------------------------------------

def test_fail_soft_corrupt_file(tmp_path):
    p = tmp_path / "test_ledger.jsonl"
    p.write_text("NOT-JSON-AT-ALL\n{{{broken}}}\n", encoding="utf-8")
    ledger = OutageLedger(path=str(p), max_records=50)
    result = ledger.recent(10)
    assert result == [], "corrupt file should return empty list, not raise"
    assert not ledger.has_open_outage()


# ---------------------------------------------------------------------------
# Test 7: emit with no running loop -- no raise
# ---------------------------------------------------------------------------

def test_emit_no_running_loop_no_raise(tmp_path):
    # Make sure we are outside an event loop
    ledger = _make_ledger(tmp_path)
    oid = ledger.open_outage(lane="batch")
    records = ledger.recent(1)
    assert records
    rec = records[0]
    # Should not raise even with no running loop
    emit_outage_event("DW_OUTAGE_DETECTED", rec)


# ---------------------------------------------------------------------------
# Test 8: emit fire-and-forget with fake bus
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_fire_and_forget_with_fake_bus(tmp_path, monkeypatch):
    publish_calls = []

    class FakeBus:
        _running = True

        async def publish(self, event, persist=True):  # noqa: ARG002
            publish_calls.append(event)
            return "fake-event-id"

    import backend.core.ouroboros.governance.outage_ledger as _mod

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.outage_ledger._export_enabled",
        lambda: True,
    )

    async def fake_get_bus_if_exists():
        return FakeBus()

    # Patch _publish_outage_event to use the fake bus directly
    async def _fake_publish(kind: str, record: OutageRecord) -> None:
        bus = FakeBus()
        from backend.core.trinity_event_bus import TrinityEvent, EventPriority, RepoType
        event = TrinityEvent(
            topic=kind,
            source=RepoType.JARVIS,
            priority=EventPriority.HIGH,
            payload=record.to_dict(),
        )
        await bus.publish(event, persist=True)

    monkeypatch.setattr(_mod, "_publish_outage_event", _fake_publish)

    ledger = _make_ledger(tmp_path)
    oid = ledger.open_outage(lane="batch")
    records = ledger.recent(1)
    assert records
    rec = records[0]

    emit_outage_event("DW_OUTAGE_DETECTED", rec)
    # Let the task run
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(publish_calls) == 1, f"expected 1 publish call, got {len(publish_calls)}"
    assert publish_calls[0].topic == "DW_OUTAGE_DETECTED"


# ---------------------------------------------------------------------------
# Test 9: export disabled gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_disabled_gate(tmp_path, monkeypatch):
    publish_calls = []

    monkeypatch.setenv("JARVIS_TRINITY_OUTAGE_EXPORT_ENABLED", "false")

    import backend.core.ouroboros.governance.outage_ledger as _mod

    async def _fake_publish(kind: str, record: OutageRecord) -> None:
        publish_calls.append(kind)

    monkeypatch.setattr(_mod, "_publish_outage_event", _fake_publish)

    ledger = _make_ledger(tmp_path)
    oid = ledger.open_outage(lane="batch")
    records = ledger.recent(1)
    assert records
    rec = records[0]

    emit_outage_event("DW_OUTAGE_DETECTED", rec)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(publish_calls) == 0, "publish should NOT be called when export is disabled"


# ---------------------------------------------------------------------------
# Test 10: master off -- all noop
# ---------------------------------------------------------------------------

def test_master_off_all_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_OUTAGE_LEDGER_ENABLED", "false")
    p = tmp_path / "test_ledger.jsonl"
    ledger = OutageLedger(path=str(p), max_records=50)

    oid = ledger.open_outage(lane="batch")
    assert oid == "", "open_outage should return '' when disabled"
    ledger.close_outage(oid)
    result = ledger.recent(10)
    assert result == []
    assert not ledger.has_open_outage()
    # No file should be written
    assert not p.exists(), "no file should be written when master is off"
