"""Spend WAL — atomic append, replay, malformed-line tolerance."""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.aegis.spend_wal import (
    SPEND_WAL_SCHEMA_VERSION,
    SpendEntry,
    SpendEntryKind,
    admit_entry,
    append_entry,
    append_entry_sync,
    boot_entry,
    reconcile_entry,
    replay_wal,
)


def test_spend_entry_kind_closed():
    expected = {"boot", "admit", "reconcile"}
    actual = {k.value for k in SpendEntryKind}
    assert actual == expected


def test_spend_entry_dict_roundtrip_admit():
    e = admit_entry(
        ts=1000.0, lease_nonce="n1", op_id="op-1", route="STANDARD",
        estimated_cost_usd=0.10, reserve_cost_usd=0.15,
    )
    d = e.to_dict()
    recovered = SpendEntry.from_dict(d)
    assert recovered == e


def test_spend_entry_dict_roundtrip_reconcile():
    e = reconcile_entry(
        ts=1000.0, lease_nonce="n1", op_id="op-1", route="STANDARD",
        actual_cost_usd=0.08, reserve_cost_usd=0.15,
    )
    d = e.to_dict()
    recovered = SpendEntry.from_dict(d)
    assert recovered == e


def test_spend_entry_dict_roundtrip_boot():
    e = boot_entry(ts=1.0, detail="hello")
    d = e.to_dict()
    recovered = SpendEntry.from_dict(d)
    assert recovered == e


def test_append_sync_then_replay(tmp_path):
    wal = tmp_path / "spend.jsonl"
    e1 = boot_entry(ts=1.0, detail="boot1")
    e2 = admit_entry(
        ts=2.0, lease_nonce="n1", op_id="op-1", route="STANDARD",
        estimated_cost_usd=0.01, reserve_cost_usd=0.02,
    )
    e3 = reconcile_entry(
        ts=3.0, lease_nonce="n1", op_id="op-1", route="STANDARD",
        actual_cost_usd=0.015, reserve_cost_usd=0.02,
    )
    assert append_entry_sync(wal, e1) is True
    assert append_entry_sync(wal, e2) is True
    assert append_entry_sync(wal, e3) is True

    recovered = replay_wal(wal)
    assert len(recovered) == 3
    assert recovered[0] == e1
    assert recovered[1] == e2
    assert recovered[2] == e3


@pytest.mark.asyncio
async def test_append_async_then_replay(tmp_path):
    wal = tmp_path / "spend.jsonl"
    e = boot_entry(ts=1.0, detail="async-boot")
    ok = await append_entry(wal, e)
    assert ok is True
    recovered = replay_wal(wal)
    assert recovered == [e]


def test_replay_missing_file_returns_empty(tmp_path):
    assert replay_wal(tmp_path / "absent.jsonl") == []


def test_replay_skips_malformed_lines(tmp_path):
    wal = tmp_path / "spend.jsonl"
    good = boot_entry(ts=1.0, detail="good")
    append_entry_sync(wal, good)
    # Inject malformed lines.
    with wal.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write('{"kind": "unknown_kind", "ts": 2.0}\n')
        fh.write('[]\n')  # not a dict
    good2 = boot_entry(ts=2.0, detail="good2")
    append_entry_sync(wal, good2)

    recovered = replay_wal(wal)
    # Only the two valid entries survive.
    assert len(recovered) == 2
    assert recovered[0].detail == "good"
    assert recovered[1].detail == "good2"


def test_append_writes_compact_json(tmp_path):
    wal = tmp_path / "spend.jsonl"
    e = boot_entry(ts=1.0, detail="x")
    append_entry_sync(wal, e)
    raw = wal.read_text().strip()
    # Compact JSON: no spaces after delimiters, sorted keys.
    parsed = json.loads(raw)
    assert parsed["kind"] == "boot"
    assert parsed["ts"] == 1.0
    assert parsed["schema_version"] == SPEND_WAL_SCHEMA_VERSION
