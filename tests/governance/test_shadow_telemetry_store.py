from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.shadow_telemetry_store import (
    ShadowTelemetryStore,
)


@pytest.mark.asyncio
async def test_start_and_close_idempotent(tmp_path):
    store = ShadowTelemetryStore(db_path=tmp_path / "t.db")
    await store.start()
    await store.start()  # idempotent
    await store.aclose()
    await store.aclose()  # idempotent


@pytest.mark.asyncio
async def test_plan_single_phase_write_computes_alignment(tmp_path):
    aligned_calls = []

    def fake_eval(agent, legacy, shadow):
        aligned_calls.append(agent)
        return (True, "")

    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db", evaluator=fake_eval,
    )
    await store.start()
    store.record_legacy_nowait(
        op_id="op1", agent="plan", ts=1.0, legacy_outcome={"flat": ["a.py"]},
    )
    store.record_shadow_nowait(
        op_id="op1", agent="plan", ts=1.0, shadow_outcome={"units": []},
    )
    await store.drain()  # test helper: await the queue empty
    rows = await store.last_n("plan", 5)
    assert len(rows) == 1
    assert rows[0]["aligned"] == 1
    assert aligned_calls == ["plan"]
    await store.aclose()


@pytest.mark.asyncio
async def test_fifo_cap_prunes_oldest(tmp_path):
    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db",
        evaluator=lambda a, l, s: (True, ""),
        cap_per_agent=5,
    )
    await store.start()
    for i in range(12):
        store.record_legacy_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            legacy_outcome={"i": i})
        store.record_shadow_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            shadow_outcome={"i": i})
    await store.drain()
    rows = await store.last_n("plan", 100)
    # cap=5 -> only the 5 highest seq survive
    assert len(rows) == 5
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs, reverse=True)
    assert min(seqs) >= 8  # oldest (op0..op6) pruned
    await store.aclose()


@pytest.mark.asyncio
async def test_streak_resets_on_divergence(tmp_path):
    # evaluator: align unless shadow says {"bad": True}
    def ev(agent, legacy, shadow):
        return (not shadow.get("bad", False), "div" if shadow.get("bad") else "")

    store = ShadowTelemetryStore(db_path=tmp_path / "t.db", evaluator=ev)
    await store.start()

    async def one(op, bad):
        store.record_legacy_nowait(
            op_id=op, agent="review", ts=0.0, legacy_outcome={})
        store.record_shadow_nowait(
            op_id=op, agent="review", ts=0.0, shadow_outcome={"bad": bad})

    for i in range(3):
        await one(f"a{i}", False)
    await one("bad1", True)
    for i in range(2):
        await one(f"b{i}", False)
    await store.drain()
    # newest-first: b1,b0 aligned (2), then bad1 breaks -> streak == 2
    assert await store.recent_aligned_streak("review") == 2
    await store.aclose()
