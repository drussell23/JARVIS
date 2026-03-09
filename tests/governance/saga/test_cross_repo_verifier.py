"""Tests for CrossRepoVerifier three-tier verification."""
from unittest.mock import patch, MagicMock

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
         patch.object(verifier, "_tier2_cross_repo_contracts", side_effect=lambda **_kw: called.append(True) or None), \
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


async def test_tier2_import_boundary_failure(tmp_path):
    """Tier 2 catches a broken cross-repo import edge."""
    src_root = tmp_path / "prime"
    src_root.mkdir()
    dst_root = tmp_path / "jarvis"
    dst_root.mkdir()

    # Create contract manifest with a nonexistent module
    jarvis_jarvis = dst_root / ".jarvis"
    jarvis_jarvis.mkdir()
    (jarvis_jarvis / "contract_manifest.json").write_text(
        '{"boundary_modules": ["_nonexistent_module_xyz_for_testing"]}'
    )

    verifier = CrossRepoVerifier(
        repo_roots={"prime": src_root, "jarvis": dst_root},
        dependency_edges=(("prime", "jarvis"),),
    )
    result = await verifier._tier2_cross_repo_contracts(
        _repo_scope=("prime", "jarvis"),
        dependency_edges=(("prime", "jarvis"),),
    )
    assert result is not None
    assert result.passed is False
    assert result.reason_code == "verify_import_edge_broken"
    assert "prime" in result.details or "jarvis" in result.details


async def test_tier3_failure_when_cross_repo_tests_fail(tmp_path):
    """Tier 3 returns failure when @cross_repo tests exist and pytest exits non-zero."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_cross_repo_example.py").write_text("# @cross_repo\n")

    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = "FAILED test_cross_repo_example.py::test_failing\n1 failed"

    with patch(
        "backend.core.ouroboros.governance.saga.cross_repo_verifier.subprocess.run",
        return_value=mock_proc,
    ):
        result = await verifier._tier3_integration_tests(
            repo_scope=("jarvis",),
            repo_roots={"jarvis": tmp_path},
        )

    assert result is not None
    assert result.passed is False
    assert result.failure_class == VerifyFailureClass.INTEGRATION
    assert result.reason_code == "verify_integration_failed"
