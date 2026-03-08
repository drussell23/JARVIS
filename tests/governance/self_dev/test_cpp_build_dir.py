"""Tests for per-op CppAdapter build directory isolation."""
from pathlib import Path
from backend.core.ouroboros.governance.test_runner import CppAdapter


def test_build_dir_uses_op_id():
    adapter = CppAdapter(repo_root=Path("/repo"), scratch_root=Path("/scratch"))
    assert adapter._build_dir("op-abc", sandbox_dir=None) == Path("/scratch/op-abc/cpp-build")


def test_build_dir_uses_sandbox_when_provided():
    adapter = CppAdapter(repo_root=Path("/repo"), scratch_root=Path("/scratch"))
    assert adapter._build_dir("op-xyz", sandbox_dir=Path("/sandbox")) == Path("/sandbox/op-xyz/cpp-build")


def test_two_op_ids_have_different_build_dirs():
    adapter = CppAdapter(repo_root=Path("/repo"), scratch_root=Path("/scratch"))
    assert adapter._build_dir("op-1", None) != adapter._build_dir("op-2", None)


def test_resolve_always_returns_empty_tuple():
    """ctest is label-driven; resolve() always returns empty tuple."""
    import asyncio
    adapter = CppAdapter(repo_root=Path("/repo"), scratch_root=Path("/scratch"))
    result = asyncio.get_event_loop().run_until_complete(
        adapter.resolve((Path("/repo/mlforge/x.cpp"),), Path("/repo"))
    )
    assert result == ()
