"""E2E test: LanguageRouter routing logic with mocked adapters.

These tests validate LanguageRouter's routing decisions (_ADAPTER_RULES) by
mocking both adapters so execution is fast and deterministic. The real
LanguageRouter (including _route(), _normalize(), BlockedPathError) is exercised.
"""
import pytest
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.test_runner import (
    AdapterResult,
    CppAdapter,
    LanguageRouter,
    MultiAdapterResult,
    PythonAdapter,
    TestResult,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_adapter_result(
    name: str,
    passed: bool,
    failure_class: Literal["none", "test", "build", "infra"] = "none",
) -> AdapterResult:
    return AdapterResult(
        adapter=name,
        passed=passed,
        failure_class=failure_class,
        test_result=TestResult(
            passed=passed,
            total=2 if passed else 0,
            failed=0 if passed else 1,
            failed_tests=(),
            duration_seconds=0.1,
            stdout="2 passed" if passed else "cmake: command not found",
            flake_suspected=False,
        ),
        duration_s=0.1,
    )


def make_passing_python() -> MagicMock:
    py = MagicMock(spec=PythonAdapter)
    py.name = "python"
    py.resolve = AsyncMock(return_value=())
    py.run = AsyncMock(return_value=_make_adapter_result("python", passed=True))
    return py


def make_failing_cpp(failure_class: Literal["none", "test", "build", "infra"] = "infra") -> MagicMock:
    cpp = MagicMock(spec=CppAdapter)
    cpp.name = "cpp"
    cpp.resolve = AsyncMock(return_value=())
    cpp.run = AsyncMock(return_value=_make_adapter_result("cpp", passed=False, failure_class=failure_class))
    return cpp


@pytest.mark.asyncio
async def test_mlforge_path_triggers_both_adapters_and_fails_on_cpp_infra():
    """Op touching mlforge/ -> both Python+C++ run; C++ infra fail -> FAILED(infra)."""
    python_adapter = make_passing_python()
    cpp_adapter = make_failing_cpp("infra")

    # CORRECT constructor: (repo_root, adapters dict)
    router = LanguageRouter(
        repo_root=REPO_ROOT,
        adapters={"python": python_adapter, "cpp": cpp_adapter},
    )

    changed_files = (REPO_ROOT / "mlforge" / "fake_module.cpp",)

    # CORRECT run() — no repo_root param
    result = await router.run(
        changed_files=changed_files,
        sandbox_dir=None,
        timeout_budget_s=30.0,
        op_id="op-e2e-mlforge",
    )

    assert isinstance(result, MultiAdapterResult)
    adapter_names = {r.adapter for r in result.adapter_results}
    assert "cpp" in adapter_names
    assert "python" in adapter_names
    assert result.failure_class == "infra"
    assert result.passed is False


@pytest.mark.asyncio
async def test_reactor_core_path_uses_python_adapter_only():
    """Op touching reactor_core/ -> only Python adapter runs."""
    python_adapter = make_passing_python()
    cpp_adapter = make_failing_cpp("test")

    router = LanguageRouter(
        repo_root=REPO_ROOT,
        adapters={"python": python_adapter, "cpp": cpp_adapter},
    )

    changed_files = (REPO_ROOT / "reactor_core" / "model.py",)

    result = await router.run(
        changed_files=changed_files,
        sandbox_dir=None,
        timeout_budget_s=30.0,
        op_id="op-e2e-reactor",
    )

    adapter_names = {r.adapter for r in result.adapter_results}
    assert "python" in adapter_names
    assert "cpp" not in adapter_names
    cpp_adapter.run.assert_not_called()


@pytest.mark.asyncio
async def test_catchall_path_uses_python_adapter_only():
    """A file outside mlforge/bindings/reactor_core/tests/ uses the catch-all -> python only."""
    python_adapter = make_passing_python()
    cpp_adapter = make_failing_cpp("test")

    router = LanguageRouter(
        repo_root=REPO_ROOT,
        adapters={"python": python_adapter, "cpp": cpp_adapter},
    )

    changed_files = (REPO_ROOT / "backend" / "core" / "some_module.py",)

    result = await router.run(
        changed_files=changed_files,
        sandbox_dir=None,
        timeout_budget_s=30.0,
        op_id="op-e2e-catchall",
    )

    adapter_names = {r.adapter for r in result.adapter_results}
    assert "python" in adapter_names
    assert "cpp" not in adapter_names
    cpp_adapter.run.assert_not_called()
    assert result.passed is True


@pytest.mark.asyncio
async def test_bindings_path_triggers_both_adapters():
    """Op touching bindings/ -> both adapters run (same rule as mlforge/)."""
    python_adapter = make_passing_python()
    cpp_adapter = MagicMock(spec=CppAdapter)
    cpp_adapter.name = "cpp"
    cpp_adapter.resolve = AsyncMock(return_value=())
    cpp_adapter.run = AsyncMock(return_value=_make_adapter_result("cpp", passed=True))

    router = LanguageRouter(
        repo_root=REPO_ROOT,
        adapters={"python": python_adapter, "cpp": cpp_adapter},
    )

    changed_files = (REPO_ROOT / "bindings" / "py_wrapper.cpp",)

    result = await router.run(
        changed_files=changed_files,
        sandbox_dir=None,
        timeout_budget_s=30.0,
        op_id="op-e2e-bindings",
    )

    adapter_names = {r.adapter for r in result.adapter_results}
    assert "python" in adapter_names
    assert "cpp" in adapter_names
    assert result.passed is True
