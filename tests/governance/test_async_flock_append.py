from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import cross_process_jsonl as cpj


@pytest.mark.asyncio
async def test_async_append_line_basic(tmp_path):
    target = tmp_path / "ledger.jsonl"
    ok = await cpj.async_flock_append_line(target, '{"a":1}')
    assert ok is True
    assert target.read_text() == '{"a":1}\n'


@pytest.mark.asyncio
async def test_async_append_creates_parent_dir(tmp_path):
    target = tmp_path / "deep" / "nested" / "ledger.jsonl"
    ok = await cpj.async_flock_append_line(target, '{"b":2}')
    assert ok is True and target.exists()


@pytest.mark.asyncio
async def test_async_append_runs_off_loop_thread(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = cpj.flock_append_lines

    def spy(path, lines, *, timeout_s=None):
        seen.append(threading.get_ident())
        return real(path, lines, timeout_s=timeout_s)

    monkeypatch.setattr(cpj, "flock_append_lines", spy)
    await cpj.async_flock_append_line(tmp_path / "x.jsonl", "{}")
    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_concurrent_appends_no_interleave_no_loss(tmp_path):
    """Mandate 4: 32 concurrent async appends → exactly 32 intact
    JSON lines, no partial/interleaved writes, no losses."""
    target = tmp_path / "concurrent.jsonl"
    payloads = [json.dumps({"i": i, "pad": "x" * 512}) for i in range(32)]
    results = await asyncio.gather(*[
        cpj.async_flock_append_line(target, p) for p in payloads
    ])
    assert all(results)
    lines = target.read_text().splitlines()
    assert len(lines) == 32
    parsed = sorted(json.loads(ln)["i"] for ln in lines)  # every line intact JSON
    assert parsed == list(range(32))


@pytest.mark.asyncio
async def test_concurrent_batch_appends_stay_contiguous(tmp_path):
    """Mandate 4 (batch atomicity under async concurrency):
    flock_append_lines writes a whole batch inside ONE flock scope —
    prove that survives the async path. 8 concurrent tasks each
    append a distinct 5-line batch via async_flock_append_lines →
    exactly 8×5 intact JSON lines AND each task's 5 lines are
    CONTIGUOUS in file order (no interleaving within any batch)."""
    target = tmp_path / "batches.jsonl"
    n_tasks, batch_len = 8, 5
    batches = [
        [json.dumps({"task": t, "seq": s}) for s in range(batch_len)]
        for t in range(n_tasks)
    ]
    results = await asyncio.gather(*[
        cpj.async_flock_append_lines(target, batch) for batch in batches
    ])
    assert all(results)
    rows = [json.loads(ln) for ln in target.read_text().splitlines()]
    assert len(rows) == n_tasks * batch_len
    # Each batch must appear as one contiguous, in-order run of 5.
    for i in range(0, len(rows), batch_len):
        chunk = rows[i:i + batch_len]
        task_ids = {r["task"] for r in chunk}
        assert len(task_ids) == 1, (
            f"interleaved batch at rows {i}..{i + batch_len - 1}: {chunk}"
        )
        assert [r["seq"] for r in chunk] == list(range(batch_len))
    # Every task's batch landed exactly once.
    assert sorted(
        rows[i]["task"] for i in range(0, len(rows), batch_len)
    ) == list(range(n_tasks))


@pytest.mark.asyncio
async def test_sequential_awaited_appends_preserve_order(tmp_path):
    target = tmp_path / "ordered.jsonl"
    for i in range(10):
        assert await cpj.async_flock_append_line(target, json.dumps({"seq": i}))
    seqs = [json.loads(ln)["seq"] for ln in target.read_text().splitlines()]
    assert seqs == list(range(10))


@pytest.mark.asyncio
async def test_contended_lock_does_not_block_loop(tmp_path):
    """Mandate 4 + 2: another thread holds the flock; the async append
    waits in the POOL, the loop keeps ticking, append succeeds after
    release."""
    target = tmp_path / "contended.jsonl"
    lock_path = target.with_suffix(target.suffix + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)

    # hold the actual lock file the append uses
    import fcntl, os
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)

    ticks: list[float] = []

    async def heartbeat():
        t0 = asyncio.get_event_loop().time()
        for _ in range(10):
            await asyncio.sleep(0.03)
            ticks.append(asyncio.get_event_loop().time() - t0)

    async def delayed_release():
        await asyncio.sleep(0.35)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    hb = asyncio.ensure_future(heartbeat())
    rel = asyncio.ensure_future(delayed_release())
    ok = await cpj.async_flock_append_line(target, '{"c":3}', timeout_s=3.0)
    await asyncio.gather(hb, rel)
    assert ok is True
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.25, f"loop starved during lock wait: {gaps}"


@pytest.mark.asyncio
async def test_lock_timeout_returns_false_never_deadlocks(tmp_path):
    """Non-reentrancy / stuck-holder hazard is BOUNDED: timeout → False."""
    import fcntl, os
    target = tmp_path / "stuck.jsonl"
    lock_path = target.with_suffix(target.suffix + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        ok = await cpj.async_flock_append_line(target, "{}", timeout_s=0.3)
        elapsed = time.monotonic() - t0
        assert ok is False
        assert elapsed < 2.0  # bounded, no deadlock
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.asyncio
async def test_master_off_inline_still_appends(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")
    target = tmp_path / "off.jsonl"
    assert await cpj.async_flock_append_line(target, '{"d":4}') is True
    assert target.read_text() == '{"d":4}\n'


# --- decision-trace async path -------------------------------------------

@pytest.mark.asyncio
async def test_ledger_record_async(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.observability.decision_trace_ledger import (
        DecisionTraceLedger,
    )
    ledger = DecisionTraceLedger(path=tmp_path / "decision_trace.jsonl")
    ok, detail = await ledger.record_async(
        op_id="op-test1", phase="ROUTE", decision="standard",
        factors={"k": "v"}, weights={}, rationale="unit",
    )
    assert ok is True and detail == "ok"
    row = json.loads((tmp_path / "decision_trace.jsonl").read_text().splitlines()[0])
    assert row["op_id"] == "op-test1" and row["decision"] == "standard"


@pytest.mark.asyncio
async def test_record_decision_async_never_raises(monkeypatch):
    from backend.core.ouroboros.governance.observability import phase8_producers as p8

    class _Boom:
        async def record_async(self, **kw):
            raise RuntimeError("synthetic")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.observability."
        "decision_trace_ledger.get_default_ledger",
        lambda: _Boom(),
    )
    ok = await p8.record_decision_async(
        op_id="op-x", phase="ROUTE", decision="standard",
    )
    assert ok is False  # swallowed, logged, never raised
