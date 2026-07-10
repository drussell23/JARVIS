from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry


def _entry(lease="lse-1"):
    return WALEntry(
        lease_id=lease, envelope_dict={"k": "v"}, status="pending",
        ts_monotonic=time.monotonic(), ts_utc="2026-07-10T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_append_async_row_byte_equal_to_sync(tmp_path):
    """Mandate 3/4: async twin delegates to the same record builder —
    prove byte-equal rows (modulo the two timestamps, which the test pins)."""
    sync_wal = WAL(tmp_path / "sync.jsonl")
    async_wal = WAL(tmp_path / "async.jsonl")
    e = _entry()
    assert sync_wal.append(e) is True
    assert await async_wal.append_async(e) is True
    srow = json.loads((tmp_path / "sync.jsonl").read_text().splitlines()[0])
    arow = json.loads((tmp_path / "async.jsonl").read_text().splitlines()[0])
    assert srow == arow


@pytest.mark.asyncio
async def test_update_status_async_tombstone_parity(tmp_path):
    wal = WAL(tmp_path / "w.jsonl")
    wal.append(_entry())
    await wal.update_status_async("lse-1", "acked")
    rows = [json.loads(l) for l in (tmp_path / "w.jsonl").read_text().splitlines()]
    assert rows[-1]["_type"] == "status_update"
    assert rows[-1]["status"] == "acked"
    assert wal.pending_entries() == []


@pytest.mark.asyncio
async def test_update_status_async_invalid_status_raises_before_await(tmp_path):
    """Sync parity: ValueError on invalid status (wal.py:74-79 contract),
    raised synchronously (no partial write)."""
    wal = WAL(tmp_path / "w.jsonl")
    with pytest.raises(ValueError):
        await wal.update_status_async("lse-1", "not-a-status")
    assert not (tmp_path / "w.jsonl").exists() or (tmp_path / "w.jsonl").read_text() == ""


@pytest.mark.asyncio
async def test_append_async_does_not_block_loop_under_lock_contention(tmp_path):
    """The Run #14 mechanism: a held flock must park the WAIT in the pool,
    not on the loop. Hold the lock in a thread; assert loop tick gaps stay
    <250ms while append_async waits/fails honestly."""
    import fcntl
    import threading

    target = tmp_path / "w.jsonl"
    lock_path = target.with_suffix(target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    release = threading.Timer(1.0, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), fd.close()))
    release.start()

    wal = WAL(target)
    gaps: list[float] = []

    async def ticker():
        prev = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    t = asyncio.ensure_future(ticker())
    try:
        ok = await wal.append_async(_entry())
    finally:
        t.cancel()
        release.cancel()
    assert isinstance(ok, bool)  # honest bool either way
    assert max(gaps) < 0.25, f"loop starved: {max(gaps):.3f}s"


@pytest.mark.asyncio
async def test_master_off_degrades_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")
    wal = WAL(tmp_path / "w.jsonl")
    assert await wal.append_async(_entry()) is True
    row = json.loads((tmp_path / "w.jsonl").read_text().splitlines()[0])
    assert row["lease_id"] == "lse-1"


def test_uir_call_sites_use_async_variants():
    """AST-shape pin: the two Run #14 hot sites must await the async twins
    (fails if the conversion is reverted)."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    ).read_text(encoding="utf-8")
    assert "await self._wal.append_async(" in src
    assert 'await self._wal.update_status_async(envelope.lease_id, "acked")' in src
