"""Every re-admission meets the same identity gates a fresh signal meets.

Before:
  * WAL replay put entries straight on the queue -- past dedup, the goal
    claim and the file lock;
  * ``GovernedLoopService.submit`` callers that bypass intake (CLI, MCP,
    scheduled agents, the intent engine) checked a SEPARATE file-lock set
    that background ops never wrote, so they ran beside them;
  * the lock keyed paths raw (``a.py`` and ``/repo/a.py`` were two files);
  * signals queued behind a holder were re-admitted FIFO, human or not;
  * meta-goal pooling took ops BEFORE admission.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake import unified_intake_router as uir
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.wal import WALEntry


def _env(op_id, files=("backend/x.py",), source="backlog", evidence=None, urgency="normal"):
    ev = {"signature": op_id, **(evidence or {})}
    return make_envelope(source=source, description=f"work {op_id}", target_files=tuple(files),
                         repo="jarvis", confidence=0.9, urgency=urgency, evidence=ev,
                         requires_human_ack=False, causal_id=op_id)


def _router(tmp_path, live=lambda op: True, dedup=0.0):
    # Replay tombstones a signal whose targets are all absent (a ghost), so
    # the files the envelopes name must exist under the project root.
    for rel in ("backend/x.py", "backend/z.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n")
    gls = MagicMock()
    gls.submit = AsyncMock()
    gls.op_is_live = live
    return uir.UnifiedIntakeRouter(gls=gls, config=uir.IntakeRouterConfig(
        project_root=tmp_path, dedup_window_s=dedup))


def _wal_entry(env, lease):
    return WALEntry(lease_id=lease, envelope_dict=env.with_lease(lease).to_dict(),
                    status="pending", ts_monotonic=time.monotonic(), ts_utc="")


class TestOneLock:
    def test_absolute_and_relative_spellings_are_one_file(self, tmp_path):
        r = _router(tmp_path)
        assert r.claim_files("op-a", ["backend/x.py"]) is None
        assert r.claim_files("op-b", [str(tmp_path / "backend" / "x.py")]) == "op-a"
        assert r.claim_files("op-c", ["./backend/../backend/x.py"]) == "op-a"

    def test_claim_is_idempotent_for_the_holder(self, tmp_path):
        r = _router(tmp_path)
        r.claim_files("op-a", ["a.py"])
        assert r.claim_files("op-a", ["a.py", "b.py"]) is None
        assert r._active_file_ops["b.py"][0] == "op-a"

    async def test_direct_submit_meets_the_routers_lock(self, tmp_path):
        """A CLI/MCP op used to check only the service's own set."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, ServiceState,
        )
        from backend.core.ouroboros.governance.op_context import OperationContext
        r = _router(tmp_path)
        r.claim_files("bg-op", ["backend/x.py"])            # a background op holds it
        gls = GovernedLoopService.__new__(GovernedLoopService)
        gls._state = ServiceState.ACTIVE
        gls._config = SimpleNamespace(max_concurrent_ops=4)
        gls._active_ops, gls._completed_ops, gls._active_file_ops = set(), {}, set()
        gls._intake_router = r
        gls._emit_terminal_events = AsyncMock()
        ctx = OperationContext.create(target_files=("backend/x.py",), description="cli op")
        result = await gls.submit(ctx, trigger_source="cli_manual")
        assert result.reason_code == "file_in_flight"
        assert not gls._active_file_ops                       # its own claim rolled back

    async def test_a_mock_router_never_reads_as_a_holder(self, tmp_path):
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
        gls = GovernedLoopService.__new__(GovernedLoopService)
        gls._active_file_ops = set()
        locked, conflict = gls._try_claim_file_ops(("a.py",))
        assert locked and conflict is None


class TestReplayThroughIngest:
    async def test_replay_takes_file_claims_and_the_second_queues(self, tmp_path):
        r = _router(tmp_path)
        await r._replay_wal([_wal_entry(_env("op-a"), "lse-a"), _wal_entry(_env("op-b"), "lse-b")])
        assert r._active_file_ops["backend/x.py"][0] == "op-a"
        assert [e.causal_id for e in r._queued_behind["op-a"]] == ["op-b"]

    async def test_replay_keeps_the_lease_and_writes_no_second_row(self, tmp_path):
        r = _router(tmp_path)
        before = len(r._wal.pending_entries())
        await r._replay_wal([_wal_entry(_env("op-a"), "lse-a")])
        assert len(r._wal.pending_entries()) == before
        _, _, _, queued = r._queue.get_nowait()
        assert queued.lease_id == "lse-a"

    async def test_human_work_is_replayed_first(self, tmp_path):
        r = _router(tmp_path)
        auto = _wal_entry(_env("op-auto"), "lse-1")
        human = _wal_entry(_env("op-human", source="roadmap",
                                evidence={"operator_origin": True}), "lse-2")
        await r._replay_wal([auto, human])
        assert r._active_file_ops["backend/x.py"][0] == "op-human"

    async def test_a_lease_is_not_a_duplicate_of_itself(self, tmp_path):
        """A capacity-parked signal re-drained in the same process."""
        r = _router(tmp_path, dedup=600.0)
        env = _env("op-a").with_lease("lse-a")
        r._register_dedup(env)
        assert not r._is_replay_duplicate(env, "lse-a")
        assert r._is_replay_duplicate(env, "lse-other")

    async def test_identity_refusal_resolves_the_wal_row(self, tmp_path, monkeypatch):
        r = _router(tmp_path)
        acked = []
        monkeypatch.setattr(r._wal, "update_status", lambda lease, st: acked.append((lease, st)))
        monkeypatch.setattr(r, "ingest", AsyncMock(return_value="deduplicated"))
        await r._replay_wal([_wal_entry(_env("op-a"), "lse-a")])
        assert acked == [("lse-a", "acked")]

    async def test_an_admitted_replay_is_not_acked(self, tmp_path, monkeypatch):
        r = _router(tmp_path)
        acked = []
        monkeypatch.setattr(r._wal, "update_status", lambda lease, st: acked.append((lease, st)))
        monkeypatch.setattr(r, "ingest", AsyncMock(return_value="queued_behind"))
        await r._replay_wal([_wal_entry(_env("op-a"), "lse-a")])
        assert acked == []

    async def test_replay_skips_the_load_gates(self, tmp_path, monkeypatch):
        r = _router(tmp_path)
        monkeypatch.setattr(r, "intake_queue_depth", lambda: 10 ** 6)   # saturated
        assert await r.ingest(_env("op-a"), replayed_lease="lse-a") == "enqueued"
        assert await r.ingest(_env("op-b", files=("backend/z.py",))) == "backpressure"


class TestArbitration:
    async def test_waiters_are_readmitted_human_first(self, tmp_path):
        r = _router(tmp_path)
        r.claim_files("holder", ["backend/x.py"])
        r._queued_behind["holder"] = [
            _env("op-low", urgency="low"),
            _env("op-human", source="roadmap", evidence={"operator_origin": True}),
            _env("op-high", urgency="high"),
        ]
        await r.release_op("holder")
        assert r._active_file_ops["backend/x.py"][0] == "op-human"
        assert [e.causal_id for e in r._queued_behind["op-human"]] == ["op-high", "op-low"]

    def test_key_orders_sovereign_then_tier_then_age(self):
        k = uir._rearbitration_key
        assert k(_env("h", source="roadmap", evidence={"operator_origin": True})) < k(_env("a"))
        assert k(_env("a", urgency="critical")) < k(_env("b", urgency="low"))


class TestMetaGoalAdmission:
    async def test_ops_are_admitted_before_they_are_pooled(self, monkeypatch):
        from backend.core.ouroboros.governance import meta_goal_wiring
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
        order = []
        gls = GovernedLoopService.__new__(GovernedLoopService)
        gls._bg_pool = SimpleNamespace()

        async def admit(ctx, trigger):
            order.append("admit")
            return ctx
        gls._admit_routing = admit
        monkeypatch.setattr(meta_goal_wiring, "offer_ctx",
                            lambda host, ctx: order.append("pool") or True)
        ctx = SimpleNamespace(op_id="op-1")
        assert await gls.submit_background(ctx, "backlog") == "op-1"
        assert order == ["admit", "pool"]

    def test_a_pooled_op_is_live(self):
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
        gls = SimpleNamespace(_active_ops=set(), _bg_pool=None,
                              _meta_goal_pending_ctx={"op-pooled": object()})
        assert GovernedLoopService.op_is_live(gls, "op-pooled")
