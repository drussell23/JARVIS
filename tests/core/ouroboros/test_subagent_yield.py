"""Tests for ExplorationSubagent cooperative yield mechanism."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.exploration_subagent import (
    ExplorationSubagent,
    ExplorationReport,
)
import backend.core.ouroboros.governance.cooperative_fs_io as fsio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(tmp_path: Path) -> ExplorationSubagent:
    return ExplorationSubagent(project_root=tmp_path)


# ---------------------------------------------------------------------------
# Unit tests — request_yield / should_yield
# ---------------------------------------------------------------------------

class TestRequestYield:
    def test_yield_not_requested_by_default(self, tmp_path):
        """_yield_requested starts as False."""
        agent = _make_agent(tmp_path)
        assert agent._yield_requested is False

    def test_request_yield_sets_flag(self, tmp_path):
        """request_yield() sets the internal flag to True."""
        agent = _make_agent(tmp_path)
        agent.request_yield()
        assert agent._yield_requested is True

    def test_should_yield_returns_false_before_request(self, tmp_path):
        """should_yield() returns False until request_yield() is called."""
        agent = _make_agent(tmp_path)
        assert agent.should_yield() is False

    def test_should_yield_returns_flag(self, tmp_path):
        """should_yield() reflects _yield_requested accurately."""
        agent = _make_agent(tmp_path)
        assert agent.should_yield() is False
        agent.request_yield()
        assert agent.should_yield() is True

    def test_request_yield_idempotent(self, tmp_path):
        """Calling request_yield() multiple times is safe."""
        agent = _make_agent(tmp_path)
        agent.request_yield()
        agent.request_yield()
        assert agent.should_yield() is True


# ---------------------------------------------------------------------------
# Integration test — yield breaks out of explore() loop
# ---------------------------------------------------------------------------

class TestYieldBreaksExploreLoop:
    @pytest.mark.asyncio
    async def test_explore_respects_yield_request(self, tmp_path):
        """When request_yield() is called before explore(), the agent exits the loop early."""
        # Create a few Python files so there's something to iterate
        for i in range(5):
            (tmp_path / f"module_{i}.py").write_text(f"# module {i}\ndef func_{i}(): pass\n")

        agent = _make_agent(tmp_path)

        # Signal yield BEFORE the explore call — the inner loop should break immediately
        agent.request_yield()

        report = await agent.explore(
            goal="test yield mechanism",
            entry_files=tuple(f"module_{i}.py" for i in range(5)),
            max_files=20,
            max_depth=1,
        )

        # With yield requested from the start, no files should be read (loop breaks on first check)
        assert isinstance(report, ExplorationReport)
        assert len(report.files_read) == 0

    @pytest.mark.asyncio
    async def test_explore_without_yield_reads_files(self, tmp_path):
        """Without yield, explore() reads available files normally."""
        (tmp_path / "module_a.py").write_text("def foo(): pass\n")

        agent = _make_agent(tmp_path)
        # No yield requested

        report = await agent.explore(
            goal="test normal flow",
            entry_files=("module_a.py",),
            max_files=5,
            max_depth=1,
        )

        assert isinstance(report, ExplorationReport)
        assert "module_a.py" in report.files_read


# ---------------------------------------------------------------------------
# Tier-2 batch 2 row 12 — _search_codebase routes through
# cooperative_fs_io.offload instead of a raw synchronous rglob on the loop.
# ---------------------------------------------------------------------------

class TestSearchCodebaseOffload:
    @pytest.mark.asyncio
    async def test_search_codebase_routes_through_offload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(a) Spy: explore()'s Phase 3 keyword search must dispatch
        _search_codebase via cooperative_fs_io.offload — not a bare
        synchronous call."""
        (tmp_path / "needle.py").write_text("def needle_fn(): pass\n")

        calls = []
        real_offload = fsio.offload

        async def _spy_offload(fn, *a, **k):
            calls.append(fn.__name__ if hasattr(fn, "__name__") else fn)
            return await real_offload(fn, *a, **k)

        monkeypatch.setattr(fsio, "offload", _spy_offload)

        agent = _make_agent(tmp_path)
        report = await agent.explore(
            goal="find the needle function",
            entry_files=("needle.py",),
            max_files=5,
            max_depth=1,
        )

        assert calls, "_search_codebase did not route through cooperative_fs_io.offload"
        assert isinstance(report, ExplorationReport)

    @pytest.mark.asyncio
    async def test_search_codebase_offload_parity_with_sync(
        self, tmp_path: Path,
    ) -> None:
        """(b) Parity: offloaded search finds the same matches the
        synchronous _search_codebase would find directly."""
        (tmp_path / "target.py").write_text(
            "def widget_handler(): pass\n"
        )
        (tmp_path / "other.py").write_text("def unrelated(): pass\n")

        agent = _make_agent(tmp_path)
        direct = agent._search_codebase("widget_handler")

        report = await agent.explore(
            goal="find widget_handler usage",
            entry_files=("target.py",),
            max_files=5,
            max_depth=1,
        )
        pattern_findings = [
            f for f in report.findings if f.category == "pattern"
        ]
        assert len(direct) >= 1
        assert len(pattern_findings) >= 1
        assert any("target.py" in f.file_path for f in pattern_findings)

    @pytest.mark.asyncio
    async def test_search_codebase_fail_soft_on_offload_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(c) Fail-soft: an OffloadError degrades to the same empty
        result the sync path produces on error — explore() never raises."""
        (tmp_path / "target.py").write_text("def widget_handler(): pass\n")

        async def _boom_offload(fn, *a, **k):
            return fsio.OffloadError(
                fn_name="_search_codebase", exc_type="OSError",
                message="simulated", cpu_bound=False,
            )

        monkeypatch.setattr(fsio, "offload", _boom_offload)

        agent = _make_agent(tmp_path)
        report = await agent.explore(
            goal="find widget_handler usage",
            entry_files=("target.py",),
            max_files=5,
            max_depth=1,
        )
        # No pattern findings should have been produced — the offload
        # failed soft to an empty search result, no exception raised.
        pattern_findings = [
            f for f in report.findings if f.category == "pattern"
        ]
        assert pattern_findings == []
        assert isinstance(report, ExplorationReport)
