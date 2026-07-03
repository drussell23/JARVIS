"""Tests for AgenticExploreSubagent — Tier-2 batch 2 row 11.

``_resolve_entry_files`` did two synchronous filesystem crawls
(``scope_dir.rglob("*.py")`` and ``self._root.glob("*.py")``) inline on
the asyncio main loop, reachable per subagent dispatch via
``AgenticExploreSubagent.run`` → ``_run_deterministic`` →
``_resolve_entry_files``. This spine pins:

  (a) the crawl routes through ``cooperative_fs_io.offload``,
  (b) parity vs. the pre-offload synchronous behavior on a planted tree,
  (c) fail-soft: an ``OffloadError`` degrades to the same empty-list
      fallback the synchronous path produced on error — never raises.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import backend.core.ouroboros.governance.cooperative_fs_io as fsio
from backend.core.ouroboros.governance.agentic_subagent import (
    AgenticExploreSubagent,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    SubagentContext,
    SubagentRequest,
    SubagentStatus,
    SubagentType,
)


def _make_ctx(
    *,
    tmp_path: Path,
    target_files: tuple = (),
    scope_path: str = "",
    timeout_s: float = 30.0,
) -> SubagentContext:
    req = SubagentRequest(
        subagent_type=SubagentType.EXPLORE,
        goal="explore the widget module",
        target_files=target_files,
        max_files=5,
        max_depth=1,
        timeout_s=timeout_s,
    )
    parent_ctx = MagicMock()
    parent_ctx.op_id = "op-explore-test"
    return SubagentContext(
        parent_op_id="op-explore-test",
        parent_ctx=parent_ctx,
        subagent_id="op-explore-test::sub-01",
        subagent_type=SubagentType.EXPLORE,
        request=req,
        deadline=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=60),
        scope_path=scope_path,
        primary_provider_name="deterministic",
        fallback_provider_name="claude-api",
    )


# ---------------------------------------------------------------------------
# (a) Spy — the crawl routes through cooperative_fs_io.offload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_entry_files_scope_crawl_routes_through_offload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    (scope_dir / "a.py").write_text("def a(): pass\n")
    (scope_dir / "b.py").write_text("def b(): pass\n")

    calls = []
    real_offload = fsio.offload

    async def _spy_offload(fn, *a, **k):
        calls.append(1)
        return await real_offload(fn, *a, **k)

    monkeypatch.setattr(fsio, "offload", _spy_offload)

    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path, scope_path="scope")
    entry_files = await subagent._resolve_entry_files(ctx)

    assert calls, "scope_dir.rglob crawl did not route through offload"
    assert entry_files
    assert all(f.startswith("scope/") for f in entry_files)


@pytest.mark.asyncio
async def test_resolve_entry_files_root_fallback_crawl_routes_through_offload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No target_files, no scope_path, no README — falls through to the
    repo-root glob, which must also route through offload."""
    (tmp_path / "onlyfile.py").write_text("def x(): pass\n")

    calls = []
    real_offload = fsio.offload

    async def _spy_offload(fn, *a, **k):
        calls.append(1)
        return await real_offload(fn, *a, **k)

    monkeypatch.setattr(fsio, "offload", _spy_offload)

    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path)
    entry_files = await subagent._resolve_entry_files(ctx)

    assert calls, "root fallback glob crawl did not route through offload"
    assert entry_files == ("onlyfile.py",)


# ---------------------------------------------------------------------------
# (b) Parity vs. the pre-offload synchronous behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_entry_files_parity_target_files_shortcut(
    tmp_path: Path,
) -> None:
    """target_files present → returned verbatim, no crawl at all."""
    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path, target_files=("explicit.py",))
    entry_files = await subagent._resolve_entry_files(ctx)
    assert entry_files == ("explicit.py",)


@pytest.mark.asyncio
async def test_resolve_entry_files_parity_scope_dir_caps_at_five(
    tmp_path: Path,
) -> None:
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    for i in range(8):
        (scope_dir / f"m{i}.py").write_text(f"def f{i}(): pass\n")

    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path, scope_path="scope")
    entry_files = await subagent._resolve_entry_files(ctx)

    # Mirrors the original sorted(...)[:5] cap.
    expected = tuple(
        str(p.relative_to(tmp_path))
        for p in sorted(scope_dir.rglob("*.py"))[:5]
    )
    assert entry_files == expected
    assert len(entry_files) == 5


@pytest.mark.asyncio
async def test_resolve_entry_files_parity_readme_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# hi\n")
    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path, scope_path="nonexistent-scope")
    entry_files = await subagent._resolve_entry_files(ctx)
    assert entry_files == ("README.md",)


@pytest.mark.asyncio
async def test_resolve_entry_files_parity_empty_when_nothing_found(
    tmp_path: Path,
) -> None:
    """No target_files, no scope match, no README, no root *.py → ()."""
    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path)
    entry_files = await subagent._resolve_entry_files(ctx)
    assert entry_files == ()


# ---------------------------------------------------------------------------
# (c) Fail-soft — OffloadError degrades to the sync-path empty fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_entry_files_fail_soft_scope_crawl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OffloadError on the scope crawl → falls through to README/root,
    never raises."""
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    (scope_dir / "a.py").write_text("def a(): pass\n")
    (tmp_path / "README.md").write_text("# hi\n")

    async def _boom_offload(fn, *a, **k):
        return fsio.OffloadError(
            fn_name="scope_crawl", exc_type="OSError",
            message="simulated", cpu_bound=False,
        )

    monkeypatch.setattr(fsio, "offload", _boom_offload)

    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path, scope_path="scope")
    entry_files = await subagent._resolve_entry_files(ctx)

    # Scope crawl failed soft → empty → falls through to README.
    assert entry_files == ("README.md",)


@pytest.mark.asyncio
async def test_resolve_entry_files_fail_soft_root_crawl_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OffloadError on the last-ditch root glob → empty tuple, no raise."""
    (tmp_path / "onlyfile.py").write_text("def x(): pass\n")

    async def _boom_offload(fn, *a, **k):
        return fsio.OffloadError(
            fn_name="root_crawl", exc_type="OSError",
            message="simulated", cpu_bound=False,
        )

    monkeypatch.setattr(fsio, "offload", _boom_offload)

    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path)
    entry_files = await subagent._resolve_entry_files(ctx)
    assert entry_files == ()


# ---------------------------------------------------------------------------
# explore() run() contract unchanged — end-to-end shape pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explore_end_to_end_return_shape_unchanged(
    tmp_path: Path,
) -> None:
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    (scope_dir / "a.py").write_text("def a(): pass\n")

    subagent = AgenticExploreSubagent(tmp_path)
    ctx = _make_ctx(tmp_path=tmp_path, scope_path="scope")
    result = await subagent.explore(ctx)

    assert result.status in (
        SubagentStatus.COMPLETED, SubagentStatus.DIVERSITY_REJECTED,
    )
    assert result.subagent_id == ctx.subagent_id
