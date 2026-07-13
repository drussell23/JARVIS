"""Slice 11 Task 4 — WorkspacePromoter governance composer (RED first).

Thin async policy layer over Task 3's git primitives, integrated into the
existing AutoCommit 8b terminal sequence (both the live Slice4bRunner and
the inline orchestrator twin — the T5 lesson). Mandate 3 (DRY): the
LiveWork consult reuses ``Orchestrator._live_work_apply_gate``; the drift
re-check reuses ``state_drift.should_block_apply`` pointed at the TARGET
tree; failures ride the existing POSTMORTEM fail path and leave the
workspace branch as the quarantine artifact. Master
``JARVIS_WORKSPACE_PROMOTION_ENABLED`` default FALSE — production posture
stays quarantine + Orange PR; the A1 driver opts in.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.worktree_manager import PromotionError
from backend.core.ouroboros.governance.workspace_promoter import (
    run_workspace_promotion,
)

MASTER = "JARVIS_WORKSPACE_PROMOTION_ENABLED"
CONSULT = "JARVIS_PROMOTION_LIVE_WORK_CONSULT"
ENV_WS = "JARVIS_AUTO_COMMIT_WORKSPACE"


class _FakeComm:
    def __init__(self):
        self.decisions = []
        self.heartbeats = []

    async def emit_decision(self, op_id, outcome, reason_code, **kw):
        self.decisions.append((op_id, outcome, reason_code))

    async def emit_heartbeat(self, op_id, phase, **kw):
        self.heartbeats.append((op_id, phase))


class _FakeManager:
    def __init__(self, branch="ouroboros/auto/s-x", fail_with=None):
        self.branch = branch
        self.fail_with = fail_with
        self.promote_calls = []

    async def _run_git_rc(self, root, args):
        if "rev-parse" in args and "--abbrev-ref" in args:
            return 0, self.branch + "\n", ""
        return 0, "", ""

    async def promote_commits(self, target_root, branch, shas, **kw):
        self.promote_calls.append((Path(target_root), branch, tuple(shas)))
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(
            promoted_shas=tuple(shas), mode="cherry-pick",
            target_root=str(target_root),
        )


class _QuietGate:
    """Mimics _live_work_apply_gate's quiet result."""

    def __init__(self, active=False):
        self.active = active
        self.calls = 0

    async def __call__(self, ctx, best_candidate):
        self.calls += 1
        return SimpleNamespace(
            active_hit=("file.py", "dirty") if self.active else None,
        )


def _orch(tmp_path, monkeypatch, *, gate=None, comm=None):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    monkeypatch.setenv(ENV_WS, str(ws))
    from backend.core.ouroboros.governance.orchestrator import (
        OrchestratorConfig,
    )
    orch = SimpleNamespace(
        _config=OrchestratorConfig(project_root=repo),
        _live_work_apply_gate=gate or _QuietGate(),
        _stack=SimpleNamespace(comm=comm or _FakeComm()),
    )
    return orch


def _ctx(hashes=None):
    return SimpleNamespace(
        op_id="op-test-1234",
        target_files=("backend/mod.py",),
        generate_file_hashes=hashes or {},
    )


class TestMasterGating:
    async def test_master_off_is_inert(self, tmp_path, monkeypatch):
        monkeypatch.delenv(MASTER, raising=False)
        mgr = _FakeManager()
        orch = _orch(tmp_path, monkeypatch)
        out = await run_workspace_promotion(
            orch, _ctx(), "abc123", None, manager=mgr,
        )
        assert out.attempted is False
        assert out.state == "disabled"
        assert mgr.promote_calls == []

    async def test_same_root_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv(MASTER, "true")
        monkeypatch.delenv(ENV_WS, raising=False)
        from backend.core.ouroboros.governance.orchestrator import (
            OrchestratorConfig,
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        orch = SimpleNamespace(
            _config=OrchestratorConfig(project_root=repo),
            _live_work_apply_gate=_QuietGate(),
            _stack=SimpleNamespace(comm=_FakeComm()),
        )
        mgr = _FakeManager()
        out = await run_workspace_promotion(
            orch, _ctx(), "abc123", None, manager=mgr,
        )
        assert out.attempted is False
        assert out.state == "noop_same_root"
        assert mgr.promote_calls == []


class TestHappyPath:
    async def test_promotes_committed_sha_onto_project_root(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(MASTER, "true")
        gate = _QuietGate()
        comm = _FakeComm()
        mgr = _FakeManager(branch="ouroboros/auto/bt-1-abc")
        orch = _orch(tmp_path, monkeypatch, gate=gate, comm=comm)
        out = await run_workspace_promotion(
            orch, _ctx(), "deadbeef", {"candidate": 1}, manager=mgr,
        )
        assert out.attempted and out.promoted
        assert out.state == "promoted"
        assert mgr.promote_calls == [
            (Path(tmp_path / "repo").resolve(),
             "ouroboros/auto/bt-1-abc", ("deadbeef",)),
        ]
        assert gate.calls == 1, "LiveWork consult must run at promotion time"
        assert ("op-test-1234", "promoted", "workspace_promotion") \
            in comm.decisions

    async def test_consult_knob_off_skips_gate(self, tmp_path, monkeypatch):
        monkeypatch.setenv(MASTER, "true")
        monkeypatch.setenv(CONSULT, "false")
        gate = _QuietGate()
        mgr = _FakeManager()
        orch = _orch(tmp_path, monkeypatch, gate=gate)
        out = await run_workspace_promotion(
            orch, _ctx(), "deadbeef", None, manager=mgr,
        )
        assert out.promoted
        assert gate.calls == 0


class TestFailClosed:
    async def test_live_work_active_blocks_before_git(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(MASTER, "true")
        monkeypatch.delenv(CONSULT, raising=False)
        gate = _QuietGate(active=True)
        comm = _FakeComm()
        mgr = _FakeManager()
        orch = _orch(tmp_path, monkeypatch, gate=gate, comm=comm)
        out = await run_workspace_promotion(
            orch, _ctx(), "deadbeef", None, manager=mgr,
        )
        assert out.attempted and not out.promoted
        assert out.state == "live_work_active"
        assert mgr.promote_calls == []
        assert ("op-test-1234", "promotion_failed",
                "live_work_active") in comm.decisions

    async def test_generate_hash_drift_blocks_before_git(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(MASTER, "true")
        mgr = _FakeManager()
        orch = _orch(tmp_path, monkeypatch)
        # Real drift per state_drift's contract: prior_hashes is a SEQUENCE
        # of (relpath, sha256) pairs, and the file must EXIST with different
        # content (a missing file is a deletion — a different class, not
        # drift).
        mod = tmp_path / "repo" / "backend" / "mod.py"
        mod.parent.mkdir(parents=True, exist_ok=True)
        mod.write_text("operator changed this since GENERATE\n")
        ctx = _ctx(hashes=(("backend/mod.py", "0" * 64),))
        out = await run_workspace_promotion(
            orch, ctx, "deadbeef", None, manager=mgr,
        )
        assert out.state == "target_drift"
        assert not out.promoted
        assert mgr.promote_calls == []

    async def test_promotion_error_state_is_surfaced(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(MASTER, "true")
        comm = _FakeComm()
        mgr = _FakeManager(
            fail_with=PromotionError("conflict_aborted", "boom"),
        )
        orch = _orch(tmp_path, monkeypatch, comm=comm)
        out = await run_workspace_promotion(
            orch, _ctx(), "deadbeef", None, manager=mgr,
        )
        assert out.attempted and not out.promoted
        assert out.state == "conflict_aborted"
        assert ("op-test-1234", "promotion_failed",
                "conflict_aborted") in comm.decisions

    async def test_no_commit_hash_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(MASTER, "true")
        mgr = _FakeManager()
        orch = _orch(tmp_path, monkeypatch)
        out = await run_workspace_promotion(
            orch, _ctx(), None, None, manager=mgr,
        )
        assert out.attempted and not out.promoted
        assert out.state == "no_commit"
        assert mgr.promote_calls == []


class TestTerminalSequenceWiring:
    RUNNER = "backend/core/ouroboros/governance/phase_runners/slice4b_runner.py"
    ORCH = "backend/core/ouroboros/governance/orchestrator.py"

    def _between_8b_and_8b2(self, path: str) -> str:
        src = Path(path).read_text()
        i = src.index("Phase 8b: Auto-commit")
        # The inline orchestrator has no 8b2 marker at the same spot; bound
        # by the next phase marker that exists in each file.
        for end in ("Phase 8b2", "Phase 8c"):
            j = src.find(end, i)
            if j != -1:
                return src[i:j]
        raise AssertionError(f"{path}: no terminal-sequence end marker")

    def test_promotion_hook_wired_on_live_runner(self):
        block = self._between_8b_and_8b2(self.RUNNER)
        assert "run_workspace_promotion" in block, (
            "Slice4bRunner 8b terminal sequence must invoke the promoter "
            "between auto-commit and hot-reload (hot-reload re-imports from "
            "the REAL tree — it may only run after the fix lands there)"
        )

    def test_promotion_hook_wired_on_inline_twin(self):
        block = self._between_8b_and_8b2(self.ORCH)
        assert "run_workspace_promotion" in block, (
            "inline orchestrator 8b twin must carry the same hook — "
            "the T5 wired-but-inert lesson"
        )

    def test_promoter_is_async_and_forceless(self):
        assert inspect.iscoroutinefunction(run_workspace_promotion)
        import backend.core.ouroboros.governance.workspace_promoter as wp
        src = inspect.getsource(wp)
        for forbidden in ("--force", "reset --hard", "subprocess", "shell="):
            assert forbidden not in src, (
                f"promoter must not compose {forbidden!r} — all git flows "
                "through WorktreeManager (mandate 2)"
            )
