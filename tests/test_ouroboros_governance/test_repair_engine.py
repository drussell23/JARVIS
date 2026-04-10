"""Tests for the L2 Iterative Self-Repair Loop engine.

Covers RepairBudget configuration, RepairEngine FSM, and repair workflows.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.repair_engine import RepairBudget


class TestRepairBudget:
    """Tests for RepairBudget dataclass and from_env() configuration."""

    def test_defaults(self):
        """Verify RepairBudget default values.

        L2 is enabled by default as of the Iron Gate push — the self-repair
        loop is load-bearing for the Ouroboros cycle (Manifesto §6).
        """
        b = RepairBudget()
        assert b.enabled is True
        assert b.max_iterations == 5
        assert b.timebox_s == 120.0
        assert b.min_deadline_remaining_s == 10.0
        assert b.per_iteration_test_timeout_s == 60.0
        assert b.max_diff_lines == 150
        assert b.max_files_changed == 3
        assert b.max_total_validation_runs == 8
        assert b.no_progress_streak_kill == 2
        assert b.max_class_retries == {"syntax": 2, "test": 3, "flake": 2, "env": 1}
        assert b.flake_confirm_reruns == 1

    def test_from_env_defaults(self, monkeypatch):
        """Verify from_env() returns defaults when no env vars are set."""
        for k in (
            "JARVIS_L2_ENABLED",
            "JARVIS_L2_MAX_ITERS",
            "JARVIS_L2_TIMEBOX_S",
            "JARVIS_L2_MIN_DEADLINE_S",
            "JARVIS_L2_ITER_TEST_TIMEOUT_S",
            "JARVIS_L2_MAX_DIFF_LINES",
            "JARVIS_L2_MAX_FILES_CHANGED",
            "JARVIS_L2_MAX_VALIDATION_RUNS",
            "JARVIS_L2_NO_PROGRESS_KILL",
            "JARVIS_L2_CLASS_RETRIES_JSON",
            "JARVIS_L2_FLAKE_RERUNS",
        ):
            monkeypatch.delenv(k, raising=False)
        b = RepairBudget.from_env()
        # Default flipped to True (Manifesto §6 — L2 closes the Ouroboros
        # self-repair loop). Opt-out via JARVIS_L2_ENABLED=false.
        assert b.enabled is True
        assert b.max_iterations == 5

    def test_from_env_reads_values(self, monkeypatch):
        """Verify from_env() reads and parses environment variables."""
        monkeypatch.setenv("JARVIS_L2_ENABLED", "true")
        monkeypatch.setenv("JARVIS_L2_MAX_ITERS", "3")
        monkeypatch.setenv("JARVIS_L2_TIMEBOX_S", "90.0")
        monkeypatch.setenv(
            "JARVIS_L2_CLASS_RETRIES_JSON",
            '{"syntax":1,"test":2,"flake":1,"env":0}',
        )
        b = RepairBudget.from_env()
        assert b.enabled is True
        assert b.max_iterations == 3
        assert b.timebox_s == 90.0
        assert b.max_class_retries["syntax"] == 1

    def test_frozen(self):
        """Verify RepairBudget is immutable (frozen)."""
        b = RepairBudget()
        with pytest.raises(Exception):
            b.enabled = True  # type: ignore[misc]


# ── append after TestRepairBudget ────────────────────────────────────────────
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.repair_engine import (
    RepairBudget, RepairEngine,
)
from backend.core.ouroboros.governance.repair_sandbox import SandboxValidationResult


def _deadline(seconds: float = 300.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _mock_ctx(op_id="test-op-1"):
    candidate = {
        "candidate_id": "c1", "file_path": "src/foo.py",
        "unified_diff": "@@ -1 +1 @@\n-x = 1\n+x = 2",
        "full_content": "x = 2\n",
    }
    gen = MagicMock()
    gen.candidates = (candidate,)
    gen.model_id = "test-model"
    gen.provider_name = "gcp-jprime"
    ctx = MagicMock()
    ctx.op_id = op_id
    ctx.generation = gen
    ctx.target_files = ("src/foo.py",)
    return ctx


def _mock_sandbox_factory(svr):
    class _Mock:
        def __init__(self, repo_root, test_timeout_s):
            self.sandbox_root = MagicMock()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def apply_patch(self, diff, fp): pass
        async def run_tests(self, targets, timeout_s): return svr
    return _Mock


class TestRepairEngine:
    def _engine(self, budget, svr):
        gen = MagicMock()
        gen.candidates = (_mock_ctx().generation.candidates[0],)
        gen.model_id = "test-model"
        gen.provider_name = "gcp-jprime"
        prime = MagicMock()
        prime.generate = AsyncMock(return_value=gen)
        return RepairEngine(
            budget=budget, prime_provider=prime,
            repo_root=MagicMock(),
            sandbox_factory=_mock_sandbox_factory(svr),
        )

    def _fail_val(self):
        bv = MagicMock()
        bv.best_candidate = {
            "candidate_id": "c1", "file_path": "src/foo.py",
            "unified_diff": "@@ -1 +1 @@\n-x=1\n+x=2",
        }
        bv.short_summary = "FAILED tests/test_foo.py::test_bar\n1 failed"
        return bv

    @pytest.mark.asyncio
    async def test_l2_converged_on_passing_first_iteration(self):
        svr = SandboxValidationResult(True, "1 passed", "", 0, 0.1)
        engine = self._engine(RepairBudget(enabled=True, max_iterations=3), svr)
        result = await engine.run(_mock_ctx(), self._fail_val(), _deadline())
        assert result.terminal == "L2_CONVERGED"
        assert result.candidate is not None

    @pytest.mark.asyncio
    async def test_l2_stopped_budget_exhausted(self):
        svr = SandboxValidationResult(False, "FAILED tests/t.py::x\n1 failed", "", 1, 0.1)
        engine = self._engine(RepairBudget(enabled=True, max_iterations=2), svr)
        result = await engine.run(_mock_ctx(), self._fail_val(), _deadline())
        assert result.terminal == "L2_STOPPED"

    @pytest.mark.asyncio
    async def test_l2_aborted_on_cancel(self):
        # Iteration 1 uses ctx.generation.candidates[0] directly (no generate call).
        # After iteration 1 fails with empty stdout/stderr, repair_context is set
        # and iteration 2 calls prime.generate(), which raises CancelledError here.
        prime = MagicMock()
        prime.generate = AsyncMock(side_effect=asyncio.CancelledError())
        svr = SandboxValidationResult(False, "", "", 1, 0.1)
        engine = RepairEngine(
            budget=RepairBudget(enabled=True, max_iterations=5),
            prime_provider=prime, repo_root=MagicMock(),
            sandbox_factory=_mock_sandbox_factory(svr),
        )
        with pytest.raises(asyncio.CancelledError):
            await engine.run(_mock_ctx(), self._fail_val(), _deadline())

    @pytest.mark.asyncio
    async def test_emits_iteration_records(self):
        svr = SandboxValidationResult(False, "FAILED tests/t.py::x\n1 failed", "", 1, 0.1)
        engine = self._engine(RepairBudget(enabled=True, max_iterations=1), svr)
        result = await engine.run(_mock_ctx(), self._fail_val(), _deadline())
        assert len(result.iterations) >= 1
        assert result.iterations[0].schema_version == "repair.iter.v1"

    @pytest.mark.asyncio
    async def test_l2_stopped_deadline_expired(self):
        svr = SandboxValidationResult(False, "", "", 1, 0.1)
        engine = self._engine(
            RepairBudget(enabled=True, max_iterations=5, min_deadline_remaining_s=300.0),
            svr,
        )
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        result = await engine.run(_mock_ctx(), self._fail_val(), past)
        assert result.terminal == "L2_STOPPED"
        assert result.stop_reason is not None

    @pytest.mark.asyncio
    async def test_l2_stopped_diff_files_rejected(self):
        """Patch touching more files than max_files_changed is rejected immediately."""
        # Build a candidate whose unified_diff touches 5 files (> max_files_changed=3)
        multi_file_diff = (
            "+++ b/src/a.py\n@@ -1 +1 @@\n+x=1\n"
            "+++ b/src/b.py\n@@ -1 +1 @@\n+x=1\n"
            "+++ b/src/c.py\n@@ -1 +1 @@\n+x=1\n"
            "+++ b/src/d.py\n@@ -1 +1 @@\n+x=1\n"
            "+++ b/src/e.py\n@@ -1 +1 @@\n+x=1\n"
        )
        ctx = _mock_ctx()
        ctx.generation.candidates[0]["unified_diff"] = multi_file_diff
        svr = SandboxValidationResult(True, "1 passed", "", 0, 0.1)
        engine = self._engine(RepairBudget(enabled=True, max_iterations=3, max_files_changed=3), svr)
        result = await engine.run(ctx, self._fail_val(), _deadline())
        assert result.terminal == "L2_STOPPED"
        assert result.stop_reason == "diff_files_rejected"
