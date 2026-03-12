# tests/test_ouroboros_governance/test_repair_sandbox.py
from __future__ import annotations
import subprocess
import pytest
from backend.core.ouroboros.governance.repair_sandbox import (
    RepairSandbox,
    SandboxValidationResult,
)


def _has_patch():
    try:
        subprocess.run(["patch", "--version"], check=True, capture_output=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


class TestRepairSandbox:
    @pytest.mark.asyncio
    async def test_context_manager_creates_and_cleans_up(self, tmp_path):
        sb = RepairSandbox(repo_root=tmp_path, test_timeout_s=5.0)
        sandbox_root = None
        async with sb:
            sandbox_root = sb.sandbox_root
            assert sandbox_root is not None
            assert sandbox_root.exists()
        assert not sandbox_root.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_exception(self, tmp_path):
        sb = RepairSandbox(repo_root=tmp_path, test_timeout_s=5.0)
        sandbox_root = None
        with pytest.raises(RuntimeError, match="intentional"):
            async with sb:
                sandbox_root = sb.sandbox_root
                raise RuntimeError("intentional")
        assert sandbox_root is not None
        assert not sandbox_root.exists()

    @pytest.mark.asyncio
    async def test_apply_patch_modifies_file(self, tmp_path):
        if not _has_patch():
            pytest.skip("patch binary not available")
        sb = RepairSandbox(repo_root=tmp_path, test_timeout_s=5.0)
        async with sb:
            dest = sb.sandbox_root / "foo.py"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("def foo():\n    return 1\n")
            diff = "@@ -1,2 +1,2 @@\n def foo():\n-    return 1\n+    return 2\n"
            await sb.apply_patch(diff, "foo.py")
            assert dest.read_text() == "def foo():\n    return 2\n"

    @pytest.mark.asyncio
    async def test_run_tests_returns_result(self, tmp_path):
        sb = RepairSandbox(repo_root=tmp_path, test_timeout_s=5.0)
        async with sb:
            result = await sb.run_tests(
                test_targets=("tests/nonexistent_test.py",),
                timeout_s=5.0,
            )
        assert isinstance(result, SandboxValidationResult)
        assert isinstance(result.passed, bool)
        assert isinstance(result.stdout, str)
        assert isinstance(result.duration_s, float)

    @pytest.mark.asyncio
    async def test_run_tests_timeout(self, tmp_path):
        sb = RepairSandbox(repo_root=tmp_path, test_timeout_s=0.001)
        async with sb:
            result = await sb.run_tests(test_targets=(), timeout_s=0.001)
        assert result.passed is False
