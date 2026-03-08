"""Tests for declarative adapter routing table."""
from pathlib import Path
import pytest
from backend.core.ouroboros.governance.test_runner import (
    AdapterResult,
    BlockedPathError,
    MultiAdapterResult,
    TestResult,
    _ADAPTER_RULES,
    _normalize,
    _route,
)

REPO = Path("/fake/repo")


def test_mlforge_path_routes_both_adapters():
    """mlforge/** always yields {python, cpp}."""
    files = (REPO / "mlforge" / "core.cpp",)
    result = _route(files, REPO)
    assert result == frozenset({"python", "cpp"})


def test_bindings_path_routes_both_adapters():
    """bindings/** always yields {python, cpp}."""
    files = (REPO / "bindings" / "wrapper.cpp",)
    result = _route(files, REPO)
    assert result == frozenset({"python", "cpp"})


def test_reactor_core_path_routes_python_only():
    files = (REPO / "reactor_core" / "model.py",)
    result = _route(files, REPO)
    assert result == frozenset({"python"})


def test_unknown_path_routes_python_fallback():
    files = (REPO / "some_random_dir" / "foo.py",)
    result = _route(files, REPO)
    assert result == frozenset({"python"})


def test_union_across_files_includes_cpp():
    """One mlforge file + one reactor_core file -> union includes cpp."""
    files = (REPO / "mlforge" / "a.cpp", REPO / "reactor_core" / "b.py")
    result = _route(files, REPO)
    assert result == frozenset({"python", "cpp"})


def test_normalize_inside_repo_returns_posix():
    p = REPO / "src" / "foo.py"
    assert _normalize(p, REPO) == "src/foo.py"


def test_normalize_outside_repo_raises_blocked():
    p = Path("/etc/passwd")
    with pytest.raises(BlockedPathError):
        _normalize(p, REPO)


def test_adapter_rules_has_catch_all():
    """Last rule must match any path."""
    catch_all = _ADAPTER_RULES[-1]
    assert catch_all.pattern.match("anything/at/all")


def test_multi_adapter_result_failure_class_from_dominant():
    fail = AdapterResult(
        adapter="cpp", passed=False, failure_class="test",
        test_result=TestResult(
            passed=False, total=1, failed=1, failed_tests=(),
            duration_seconds=0.1, stdout="", flake_suspected=False,
        ),
        duration_s=0.1,
    )
    ok = AdapterResult(
        adapter="python", passed=True, failure_class="none",
        test_result=TestResult(
            passed=True, total=1, failed=0, failed_tests=(),
            duration_seconds=0.1, stdout="", flake_suspected=False,
        ),
        duration_s=0.1,
    )
    mar = MultiAdapterResult(
        passed=False,
        adapter_results=(ok, fail),
        dominant_failure=fail,
        total_duration_s=0.2,
    )
    assert mar.passed is False
    assert mar.failure_class == "test"


def test_empty_changed_files_returns_empty_frozenset():
    """No files changed -> no adapters required."""
    assert _route((), REPO) == frozenset()
