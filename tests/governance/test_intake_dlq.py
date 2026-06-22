"""Tests for intake_dlq — Sovereign Dead-Letter Queue (A1-T1)."""
from __future__ import annotations
import asyncio
from backend.core.ouroboros.governance import intake_dlq as dlq


def test_append_and_read(tmp_path):
    p = tmp_path / "intake_dlq.jsonl"
    dlq.append_dlq({"goal_id": "g1", "x": 1}, reason="no_router", path=str(p))
    rows = dlq.read_dlq(str(p))
    assert len(rows) == 1 and rows[0]["envelope"]["goal_id"] == "g1"
    assert rows[0]["reason"] == "no_router"


def test_append_failsoft_bad_path():
    dlq.append_dlq({"goal_id": "g"}, reason="x", path="/nonexistent_dir_xyz/dlq.jsonl")  # must not raise


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_DLQ_ENABLED", "false")
    p = tmp_path / "dlq.jsonl"
    dlq.append_dlq({"goal_id": "g1"}, reason="r", path=str(p))
    assert dlq.read_dlq(str(p)) == []   # nothing written when disabled


def test_replay_dedups_by_goal_id(tmp_path):
    p = tmp_path / "dlq.jsonl"
    dlq.append_dlq({"goal_id": "g1"}, reason="r", path=str(p))
    dlq.append_dlq({"goal_id": "g1"}, reason="r", path=str(p))  # dup
    dlq.append_dlq({"goal_id": "g2"}, reason="r", path=str(p))
    seen = []
    async def fake_ingest(env):
        seen.append(env["goal_id"]); return "ok"
    drained = asyncio.run(dlq.replay_dlq(str(p), fake_ingest))
    assert set(seen) == {"g1", "g2"}
    assert drained == 2
    assert dlq.read_dlq(str(p)) == []   # all drained -> file emptied


def test_replay_keeps_failed_entries(tmp_path):
    p = tmp_path / "dlq.jsonl"
    dlq.append_dlq({"goal_id": "g1"}, reason="r", path=str(p))
    async def boom(env): raise RuntimeError("down")
    drained = asyncio.run(dlq.replay_dlq(str(p), boom))
    assert drained == 0
    assert len(dlq.read_dlq(str(p))) == 1   # failed re-ingest stays


# ---------------------------------------------------------------------------
# Part B: _TeeRouter wiring
# ---------------------------------------------------------------------------

def test_tee_router_none_upstream_writes_dlq(tmp_path, monkeypatch):
    """_TeeRouter(upstream=None).ingest(env) must persist a DLQ entry."""
    p = tmp_path / "dlq.jsonl"
    # Point the DLQ at a tmp file so the test is hermetic
    monkeypatch.setenv("JARVIS_INTAKE_DLQ_ENABLED", "true")

    from backend.core.ouroboros.governance.roadmap_orchestrator import _TeeRouter
    import backend.core.ouroboros.governance.intake_dlq as _dlq_mod

    # Patch _default_path so append_dlq (called with path=None) lands in tmp
    monkeypatch.setattr(_dlq_mod, "_default_path", lambda: str(p))

    tee = _TeeRouter(upstream=None)
    result = asyncio.run(tee.ingest({"goal_id": "tee-g1"}))

    assert result == "captured"
    rows = dlq.read_dlq(str(p))
    assert len(rows) == 1
    assert rows[0]["reason"] == "no_router"
    assert rows[0]["envelope"].get("goal_id") == "tee-g1"
