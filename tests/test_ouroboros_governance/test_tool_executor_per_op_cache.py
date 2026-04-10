"""Tests for the per-op ToolExecutor cache in AsyncProcessToolBackend.

The cache exists so instance-scoped ToolExecutor state (``_files_read``,
``_edit_history``) survives across ``execute_async`` calls inside a single
op. Without it, the must-have-read invariant on ``edit_file`` / ``write_file``
/ ``delete_file`` collapses to a no-op in production — each call would
build a fresh ToolExecutor with an empty ``_files_read`` set.

These tests lock in:
  1. Calls within the same op reuse the same ToolExecutor instance.
  2. Calls with different op_ids get isolated instances.
  3. ``release_op`` drops the cached instance and returns it.
  4. The LRU-ish cap evicts oldest entries when ``JARVIS_TOOL_EXECUTOR_CACHE_MAX``
     is exceeded.
  5. Tests that inject ``_executor_instance`` keep the old single-instance
     behavior unchanged.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.tool_executor import (
    AsyncProcessToolBackend,
    PolicyContext,
    ToolCall,
    ToolExecStatus,
    ToolExecutor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text(
        "print('original')\n", encoding="utf-8"
    )
    return tmp_path


def _ctx(repo_root: Path, op_id: str) -> PolicyContext:
    return PolicyContext(
        repo="jarvis",
        repo_root=repo_root,
        op_id=op_id,
        call_id=f"{op_id}:r0:read_file",
        round_index=0,
    )


async def _read_file_via_backend(
    backend: AsyncProcessToolBackend, ctx: PolicyContext, rel: str
) -> None:
    call = ToolCall(name="read_file", arguments={"path": rel})
    result = await backend.execute_async(call, ctx, time.monotonic() + 10)
    assert result.status == ToolExecStatus.SUCCESS, f"read_file failed: {result.error}"


# ---------------------------------------------------------------------------
# Per-op cache persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_op_reuses_executor_instance(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))
    ctx = _ctx(repo, "op-alpha")

    await _read_file_via_backend(backend, ctx, "backend/main.py")

    cached = backend._executors_by_op.get("op-alpha")
    assert cached is not None, "executor should be cached under op_id"
    assert "backend/main.py" in cached._files_read

    # Second call on the SAME op should hit the cached executor.
    await _read_file_via_backend(backend, ctx, "backend/main.py")
    assert backend._executors_by_op.get("op-alpha") is cached, (
        "second call must reuse the same ToolExecutor instance"
    )
    assert "backend/main.py" in cached._files_read


@pytest.mark.asyncio
async def test_different_ops_get_isolated_executors(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))

    ctx_a = _ctx(repo, "op-alpha")
    ctx_b = _ctx(repo, "op-bravo")

    await _read_file_via_backend(backend, ctx_a, "backend/main.py")
    await _read_file_via_backend(backend, ctx_b, "backend/main.py")

    cached_a = backend._executors_by_op.get("op-alpha")
    cached_b = backend._executors_by_op.get("op-bravo")
    assert cached_a is not None and cached_b is not None
    assert cached_a is not cached_b, "distinct ops must get distinct executors"


@pytest.mark.asyncio
async def test_release_op_returns_and_drops_cached_executor(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))
    ctx = _ctx(repo, "op-alpha")

    await _read_file_via_backend(backend, ctx, "backend/main.py")
    assert "op-alpha" in backend._executors_by_op

    released = backend.release_op("op-alpha")
    assert released is not None
    assert isinstance(released, ToolExecutor)
    assert "backend/main.py" in released._files_read
    assert "op-alpha" not in backend._executors_by_op


@pytest.mark.asyncio
async def test_release_op_nonexistent_is_noop(tmp_path: Path) -> None:
    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))
    assert backend.release_op("never-seen") is None
    assert backend.release_op("") is None


@pytest.mark.asyncio
async def test_injected_executor_instance_bypasses_per_op_cache(
    tmp_path: Path,
) -> None:
    """When a test injects ``_executor_instance``, every call returns it
    unchanged — preserving existing test fixtures."""
    repo = _make_repo(tmp_path)
    fixed = ToolExecutor(repo_root=repo)
    backend = AsyncProcessToolBackend(
        semaphore=asyncio.Semaphore(1), _executor_instance=fixed,
    )
    ctx_a = _ctx(repo, "op-alpha")
    ctx_b = _ctx(repo, "op-bravo")

    await _read_file_via_backend(backend, ctx_a, "backend/main.py")
    await _read_file_via_backend(backend, ctx_b, "backend/main.py")

    # Both ops resolve to the same injected instance — no per-op cache entries.
    assert backend._executors_by_op == {}
    assert "backend/main.py" in fixed._files_read


# ---------------------------------------------------------------------------
# Must-have-read invariant survives across calls (the regression this fixes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_file_must_have_read_survives_backend_roundtrip(
    tmp_path: Path, monkeypatch,
) -> None:
    """The critical regression: a ``read_file`` call followed by an
    ``edit_file`` call through AsyncProcessToolBackend must see the
    read in its ``_files_read`` set. Before the per-op cache this
    failed because a fresh ToolExecutor was built for every call."""
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("JARVIS_TOOL_EDIT_ALLOWED", "true")

    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))
    ctx = _ctx(repo, "op-edit")

    # Step 1: read_file
    await _read_file_via_backend(backend, ctx, "backend/main.py")

    # Step 2: edit_file — must succeed because the prior read is tracked.
    edit_call = ToolCall(
        name="edit_file",
        arguments={
            "path": "backend/main.py",
            "old_text": "print('original')",
            "new_text": "print('edited')",
        },
    )
    result = await backend.execute_async(edit_call, ctx, time.monotonic() + 10)
    assert result.status == ToolExecStatus.SUCCESS, (
        f"edit_file must succeed after read_file in same op; error={result.error!r}"
    )
    assert (repo / "backend" / "main.py").read_text() == "print('edited')\n"


@pytest.mark.asyncio
async def test_edit_file_rejected_without_prior_read_in_same_op(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("JARVIS_TOOL_EDIT_ALLOWED", "true")

    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))
    ctx = _ctx(repo, "op-no-read")

    edit_call = ToolCall(
        name="edit_file",
        arguments={
            "path": "backend/main.py",
            "old_text": "print('original')",
            "new_text": "print('edited')",
        },
    )
    result = await backend.execute_async(edit_call, ctx, time.monotonic() + 10)
    # Handler returns a descriptive error string via ``output`` (not via
    # ``error``) — the ToolResult still has SUCCESS status because the
    # dispatch didn't raise. The must-have-read failure is in the text.
    assert "must-have-read" in result.output.lower()
    # And crucially, the file was NOT mutated.
    assert (repo / "backend" / "main.py").read_text() == "print('original')\n"


# ---------------------------------------------------------------------------
# Cache cap / eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_cache_respects_max_size(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("JARVIS_TOOL_EXECUTOR_CACHE_MAX", "3")
    # Build AFTER setting env so __init__ picks it up.
    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))

    ops: List[str] = [f"op-{i}" for i in range(5)]
    for op in ops:
        await _read_file_via_backend(backend, _ctx(repo, op), "backend/main.py")

    assert len(backend._executors_by_op) <= 3
    # The two oldest should have been evicted.
    assert "op-0" not in backend._executors_by_op
    assert "op-1" not in backend._executors_by_op
    # The newest entries must still be there.
    assert "op-4" in backend._executors_by_op
