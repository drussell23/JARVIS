from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.shadow_telemetry_store import (
    ShadowTelemetryStore,
)
from backend.core.ouroboros.governance.shadow_graduation_gate import (
    ShadowGraduationGate, build_rail_evaluator,
)


@pytest.mark.asyncio
async def test_fifty_aligned_ops_graduate_plan(tmp_path, monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.setenv("JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")
    monkeypatch.delenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", raising=False)

    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db", evaluator=build_rail_evaluator())
    await store.start()
    gate = ShadowGraduationGate(store=store)

    for i in range(50):
        store.record_legacy_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            legacy_outcome={"flat": ["a.py"]})
        store.record_shadow_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            shadow_outcome={"units": [
                {"id": "u1", "owned_paths": ["a.py"], "deps": []}]})
    await store.drain()

    promoted = await gate.maybe_promote("plan")
    assert promoted is True
    assert ("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true") in persisted
    await store.aclose()


@pytest.mark.asyncio
async def test_one_divergence_blocks_graduation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: True,
    )
    monkeypatch.setenv("JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")
    monkeypatch.delenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", raising=False)

    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db", evaluator=build_rail_evaluator())
    await store.start()
    gate = ShadowGraduationGate(store=store)

    for i in range(49):
        store.record_legacy_nowait(
            op_id=f"ok{i}", agent="plan", ts=float(i),
            legacy_outcome={"flat": ["a.py"]})
        store.record_shadow_nowait(
            op_id=f"ok{i}", agent="plan", ts=float(i),
            shadow_outcome={"units": [
                {"id": "u1", "owned_paths": ["a.py"], "deps": []}]})
    # one cyclical (misaligned) op as the newest
    store.record_legacy_nowait(
        op_id="bad", agent="plan", ts=99.0, legacy_outcome={"flat": ["a.py"]})
    store.record_shadow_nowait(
        op_id="bad", agent="plan", ts=99.0, shadow_outcome={"units": [
            {"id": "u1", "owned_paths": ["a.py"], "deps": ["u2"]},
            {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]}]})
    await store.drain()

    assert await store.recent_aligned_streak("plan") == 0
    assert await gate.maybe_promote("plan") is False
    await store.aclose()
