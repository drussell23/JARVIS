"""Tests for CrossRepoVerifier three-tier verification."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.saga.cross_repo_verifier import (
    CrossRepoVerifier,
    VerifyResult,
    VerifyFailureClass,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)


def _make_patch_map(repos=("jarvis",)):
    return {
        r: RepoPatch(
            repo=r,
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"old"),),
            new_content=(("backend/x.py", b"new"),),
        )
        for r in repos
    }


async def test_happy_path_all_tiers_pass(tmp_path):
    """All tiers pass → VerifyResult.passed=True."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    patch_map = _make_patch_map(["jarvis"])

    with patch.object(verifier, "_tier1_per_repo", return_value=None), \
         patch.object(verifier, "_tier2_cross_repo_contracts", return_value=None), \
         patch.object(verifier, "_tier3_integration_tests", return_value=None):
        result = await verifier.verify(
            repo_scope=("jarvis",),
            patch_map=patch_map,
            dependency_edges=(),
        )

    assert result.passed is True
    assert result.failure_class is None


async def test_tier1_failure_returns_result(tmp_path):
    """Tier 1 typecheck failure → passed=False, class=VERIFY_FAILED_PER_REPO."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    patch_map = _make_patch_map(["jarvis"])

    with patch.object(verifier, "_tier1_per_repo", return_value=VerifyResult(
        passed=False,
        failure_class=VerifyFailureClass.PER_REPO,
        reason_code="verify_typecheck_failed",
        details="jarvis: pyright error",
    )):
        result = await verifier.verify(
            repo_scope=("jarvis",),
            patch_map=patch_map,
            dependency_edges=(),
        )

    assert result.passed is False
    assert result.failure_class == VerifyFailureClass.PER_REPO
    assert "pyright" in result.details


async def test_tier2_skipped_when_single_repo(tmp_path):
    """Tier 2 is skipped for single-repo operations (no edges)."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    patch_map = _make_patch_map(["jarvis"])
    called = []

    with patch.object(verifier, "_tier1_per_repo", return_value=None), \
         patch.object(verifier, "_tier2_cross_repo_contracts", side_effect=lambda **kw: called.append(True) or None), \
         patch.object(verifier, "_tier3_integration_tests", return_value=None):
        result = await verifier.verify(
            repo_scope=("jarvis",),
            patch_map=patch_map,
            dependency_edges=(),
        )

    assert not called  # Tier 2 never called for single-repo
    assert result.passed is True


async def test_tier3_noop_when_no_cross_repo_tests(tmp_path):
    """Tier 3 passes silently when no @cross_repo tests exist."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    result = await verifier._tier3_integration_tests(
        repo_scope=("jarvis",),
        repo_roots={"jarvis": tmp_path},
    )
    assert result is None  # no-op → None means pass
