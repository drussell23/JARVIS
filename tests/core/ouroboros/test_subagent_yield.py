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
