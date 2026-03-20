"""Tests for preflight_check() invariant guards — T19, T28.

TDD step: write tests FIRST, then implement.

Coverage:
    T19 — stale repo snapshot (HEAD mismatch)
    T28 — policy hash mismatch

All git subprocess calls are mocked so no real git repo is required.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import FrozenSet
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    BlastRadiusPolicy,
    PlanningContext,
)
from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMMIT = "abc123def456"
_POLICY = "cafebabe" * 8  # 64-char hex-like string


def _make_context(
    *,
    repo_commit: str = _COMMIT,
    policy_hash: str = _POLICY,
    trust_tier: AutonomyTier = AutonomyTier.GOVERNED,
    budget_remaining_usd: float = 2.00,
) -> PlanningContext:
    return PlanningContext(
        repo_commit=repo_commit,
        oracle_snapshot_id="snap-1",
        policy_hash=policy_hash,
        schema_version="1.0",
        trust_tier=trust_tier,
        budget_remaining_usd=budget_remaining_usd,
    )


def _make_graph(owned_paths: tuple = ("backend/core/foo.py",)) -> MagicMock:
    """Return a minimal ExecutionGraph mock with deterministic owned_paths."""
    unit = MagicMock()
    unit.effective_owned_paths = owned_paths

    graph = MagicMock()
    graph.graph_id = "g-test"
    graph.units = (unit,)
    return graph


def _make_blast(*, max_files: int = 50) -> BlastRadiusPolicy:
    return BlastRadiusPolicy(max_files_changed=max_files)


def _patch_git(stdout: str, returncode: int = 0):
    """Context manager that replaces asyncio.create_subprocess_exec for git."""

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = returncode

        async def _communicate():
            return (stdout.encode(), b"")

        proc.communicate = _communicate
        return proc

    return patch(
        "asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPreflightStaleSnapshot:
    """T19 — HEAD has moved since planning."""

    async def test_stale_commit_returns_error(self, tmp_path: Path):
        """preflight_check returns an error string when HEAD != context.repo_commit."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context(repo_commit=_COMMIT)
        blast = _make_blast()

        with _patch_git("different_commit_sha"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is not None
        assert (
            "commit" in result.lower()
            or "stale" in result.lower()
            or "head" in result.lower()
        )

    async def test_matching_commit_passes(self, tmp_path: Path):
        """preflight_check returns None when HEAD matches context.repo_commit."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context(repo_commit=_COMMIT)
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):  # git appends newline
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is None


class TestPreflightTrustDemotion:
    """Trust tier must not have been demoted since planning."""

    async def test_tier_demotion_returns_error(self, tmp_path: Path):
        """Planning at GOVERNED but current tier is SUGGEST → error."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        # planned at GOVERNED, now demoted to SUGGEST
        ctx = _make_context(trust_tier=AutonomyTier.GOVERNED)
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.SUGGEST,  # lower than planned
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is not None
        assert (
            "tier" in result.lower()
            or "trust" in result.lower()
            or "demot" in result.lower()
        )

    async def test_same_tier_passes(self, tmp_path: Path):
        """Same trust tier → no error from tier check."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context(trust_tier=AutonomyTier.GOVERNED)
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is None

    async def test_tier_promotion_passes(self, tmp_path: Path):
        """Promotion (GOVERNED→AUTONOMOUS) is acceptable, not a demotion."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context(trust_tier=AutonomyTier.GOVERNED)
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.AUTONOMOUS,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is None


class TestPreflightBudget:
    """Budget headroom must remain above zero."""

    async def test_zero_budget_returns_error(self, tmp_path: Path):
        """budget_remaining_usd == 0.0 → error."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context(budget_remaining_usd=0.50)
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=0.0,  # exhausted
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is not None
        assert "budget" in result.lower()

    async def test_negative_budget_returns_error(self, tmp_path: Path):
        """budget_remaining_usd < 0 → error."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context()
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=-0.01,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is not None
        assert "budget" in result.lower()


class TestPreflightBlastRadius:
    """Graph file count must stay within blast radius policy."""

    async def test_blast_radius_violation_returns_error(self, tmp_path: Path):
        """More owned paths than max_files_changed → blast radius error."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        # 3 units each owning 1 file → 3 total; policy max = 2
        unit_a = MagicMock()
        unit_a.effective_owned_paths = ("a.py",)
        unit_b = MagicMock()
        unit_b.effective_owned_paths = ("b.py",)
        unit_c = MagicMock()
        unit_c.effective_owned_paths = ("c.py",)

        graph = MagicMock()
        graph.graph_id = "g-blast"
        graph.units = (unit_a, unit_b, unit_c)

        ctx = _make_context()
        blast = BlastRadiusPolicy(max_files_changed=2)

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is not None
        assert "blast" in result.lower() or "file" in result.lower()

    async def test_blast_radius_within_limit_passes(self, tmp_path: Path):
        """Owned paths within limit → no blast radius error."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph(owned_paths=("backend/core/foo.py",))
        ctx = _make_context()
        blast = BlastRadiusPolicy(max_files_changed=50)

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is None


class TestPreflightPolicyHash:
    """T28 — policy hash must not have changed since planning."""

    async def test_policy_hash_mismatch_returns_error(self, tmp_path: Path):
        """current_policy_hash != context.policy_hash → error."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context(policy_hash="original_hash_aabbcc")
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash="changed_hash_112233",  # different
            )

        assert result is not None
        assert "policy" in result.lower() or "hash" in result.lower()

    async def test_policy_hash_match_passes(self, tmp_path: Path):
        """Matching policy hashes → no error from policy check."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context(policy_hash=_POLICY)
        blast = _make_blast()

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is None


class TestPreflightPathConflicts:
    """In-flight graphs must not own overlapping paths."""

    async def test_path_conflict_returns_error(self, tmp_path: Path):
        """owned_paths intersection with inflight_owned_paths → conflict error."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph(owned_paths=("shared/module.py",))
        ctx = _make_context()
        blast = _make_blast()
        inflight: FrozenSet[str] = frozenset({"shared/module.py", "other/file.py"})

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
                inflight_owned_paths=inflight,
            )

        assert result is not None
        assert (
            "conflict" in result.lower()
            or "inflight" in result.lower()
            or "path" in result.lower()
        )

    async def test_no_path_conflict_passes(self, tmp_path: Path):
        """Disjoint owned paths → no conflict."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph(owned_paths=("backend/core/new_file.py",))
        ctx = _make_context()
        blast = _make_blast()
        inflight: FrozenSet[str] = frozenset({"backend/core/other.py"})

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
                inflight_owned_paths=inflight,
            )

        assert result is None


class TestPreflightGitSubprocessFailure:
    """Git subprocess errors must be handled gracefully."""

    async def test_git_subprocess_os_error_returns_error_string(self, tmp_path: Path):
        """If git subprocess raises OSError, return error string (don't crash)."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context()
        blast = _make_blast()

        async def _raise(*args, **kwargs):
            raise OSError("git not found")

        with patch("asyncio.create_subprocess_exec", side_effect=_raise):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is not None
        assert isinstance(result, str)

    async def test_git_nonzero_returncode_returns_error(self, tmp_path: Path):
        """Non-zero git exit code → error string."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph()
        ctx = _make_context()
        blast = _make_blast()

        with _patch_git("", returncode=128):  # git error exit code
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=2.00,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
            )

        assert result is not None
        assert isinstance(result, str)


class TestPreflightAllChecksPass:
    """Happy path: all 6 invariants pass → returns None."""

    async def test_all_checks_pass_returns_none(self, tmp_path: Path):
        """T19 (matching commit) + T28 (matching policy) + all other checks → None."""
        from backend.core.ouroboros.governance.autonomy.preflight import preflight_check

        graph = _make_graph(owned_paths=("backend/core/safe.py",))
        ctx = _make_context(
            repo_commit=_COMMIT,
            policy_hash=_POLICY,
            trust_tier=AutonomyTier.GOVERNED,
            budget_remaining_usd=3.50,
        )
        blast = BlastRadiusPolicy(max_files_changed=50)

        with _patch_git(_COMMIT + "\n"):
            result = await preflight_check(
                graph=graph,
                context=ctx,
                current_trust_tier=AutonomyTier.GOVERNED,
                budget_remaining_usd=3.50,
                blast_radius=blast,
                repo_root=tmp_path,
                current_policy_hash=_POLICY,
                inflight_owned_paths=frozenset(),
            )

        assert result is None
