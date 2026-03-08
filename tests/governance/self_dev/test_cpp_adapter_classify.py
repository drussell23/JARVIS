"""Tests for CppAdapter failure_class taxonomy."""
from pathlib import Path
from backend.core.ouroboros.governance.test_runner import CppAdapter


def make_adapter():
    return CppAdapter(repo_root=Path("/fake/repo"), scratch_root=Path("/tmp/cpp_scratch"))


def test_executable_not_found_is_infra():
    assert make_adapter()._classify_build_failure("some output", "executable_not_found") == "infra"


def test_configure_stage_is_infra():
    assert make_adapter()._classify_build_failure("some output", "configure_stage") == "infra"


def test_cmake_not_found_marker_is_infra():
    assert make_adapter()._classify_build_failure("cmake: command not found", "exit_1") == "infra"


def test_ninja_not_found_marker_is_infra():
    assert make_adapter()._classify_build_failure("ninja: command not found", "exit_1") == "infra"


def test_compile_error_is_build():
    assert make_adapter()._classify_build_failure("error: 'foo' was not declared", "exit_1") == "build"


def test_ctest_failure_class_constant_is_test():
    assert make_adapter()._ctest_failure_class == "test"
