"""Tests for the Ouroboros Battle Test Harness."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)


# ---------------------------------------------------------------------------
# HarnessConfig tests
# ---------------------------------------------------------------------------


class TestHarnessConfigDefaults:
    """Verify HarnessConfig default values."""

    def test_default_repo_path(self):
        cfg = HarnessConfig()
        assert cfg.repo_path == Path(".")

    def test_default_cost_cap(self):
        cfg = HarnessConfig()
        assert cfg.cost_cap_usd == 0.50

    def test_default_idle_timeout(self):
        cfg = HarnessConfig()
        assert cfg.idle_timeout_s == 600.0

    def test_default_branch_prefix(self):
        cfg = HarnessConfig()
        assert cfg.branch_prefix == "ouroboros/battle-test"

    def test_default_session_dir_is_none(self):
        cfg = HarnessConfig()
        assert cfg.session_dir is None

    def test_default_notebook_output_dir_is_none(self):
        cfg = HarnessConfig()
        assert cfg.notebook_output_dir is None


class TestHarnessConfigFromEnv:
    """Verify HarnessConfig.from_env reads environment variables."""

    def test_from_env_reads_cost_cap(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "1.25")
        cfg = HarnessConfig.from_env()
        assert cfg.cost_cap_usd == 1.25

    def test_from_env_reads_idle_timeout(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_BATTLE_IDLE_TIMEOUT", "300")
        cfg = HarnessConfig.from_env()
        assert cfg.idle_timeout_s == 300.0

    def test_from_env_reads_branch_prefix(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_BATTLE_BRANCH_PREFIX", "test/prefix")
        cfg = HarnessConfig.from_env()
        assert cfg.branch_prefix == "test/prefix"

    def test_from_env_reads_repo_path(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPO_PATH", "/tmp/my-repo")
        cfg = HarnessConfig.from_env()
        assert cfg.repo_path == Path("/tmp/my-repo")

    def test_from_env_uses_defaults_when_unset(self, monkeypatch):
        # Clear all relevant env vars
        for var in [
            "OUROBOROS_BATTLE_COST_CAP",
            "OUROBOROS_BATTLE_IDLE_TIMEOUT",
            "OUROBOROS_BATTLE_BRANCH_PREFIX",
            "JARVIS_REPO_PATH",
        ]:
            monkeypatch.delenv(var, raising=False)
        cfg = HarnessConfig.from_env()
        assert cfg.cost_cap_usd == 0.50
        assert cfg.idle_timeout_s == 600.0
        assert cfg.branch_prefix == "ouroboros/battle-test"
        assert cfg.repo_path == Path(".")


# ---------------------------------------------------------------------------
# BattleTestHarness tests
# ---------------------------------------------------------------------------


class TestHarnessSessionId:
    """Verify session ID format."""

    def test_session_id_format(self):
        cfg = HarnessConfig()
        harness = BattleTestHarness(cfg)
        # Format: bt-YYYY-MM-DD-HHMMSS
        assert re.match(r"^bt-\d{4}-\d{2}-\d{2}-\d{6}$", harness.session_id)


class TestHarnessLifecycle:
    """Test harness lifecycle with all boot methods mocked."""

    @pytest.mark.asyncio
    async def test_harness_lifecycle(self, tmp_path):
        """Create harness with mocked boots, set shutdown_event immediately,
        verify boots called and report generated."""
        cfg = HarnessConfig(
            repo_path=tmp_path,
            session_dir=tmp_path / "session",
            notebook_output_dir=tmp_path / "notebooks",
        )
        harness = BattleTestHarness(cfg)

        # Mock all boot methods so they don't import real Ouroboros components
        harness.boot_oracle = AsyncMock()
        harness.boot_governance_stack = AsyncMock()
        harness.boot_governed_loop_service = AsyncMock()
        harness.boot_jarvis_tiers = AsyncMock()
        harness.create_branch = AsyncMock(return_value="ouroboros/battle-test/20260406-120000")
        harness.boot_intake = AsyncMock()
        harness.boot_graduation = AsyncMock()

        # Mock _shutdown_components and _generate_report to avoid real teardown
        harness._shutdown_components = AsyncMock()
        harness._generate_report = AsyncMock()

        # Set shutdown event immediately so the run loop exits
        async def set_shutdown():
            await asyncio.sleep(0.01)
            harness._shutdown_event.set()

        asyncio.ensure_future(set_shutdown())

        await harness.run()

        # Verify all boot methods were called
        harness.boot_oracle.assert_awaited_once()
        harness.boot_governance_stack.assert_awaited_once()
        harness.boot_governed_loop_service.assert_awaited_once()
        harness.boot_jarvis_tiers.assert_awaited_once()
        harness.create_branch.assert_awaited_once()
        harness.boot_intake.assert_awaited_once()
        harness.boot_graduation.assert_awaited_once()

        # Verify shutdown and report were called
        harness._shutdown_components.assert_awaited_once()
        harness._generate_report.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_harness_stop_reason_shutdown(self, tmp_path):
        """Verify stop_reason is 'shutdown_signal' when shutdown_event fires."""
        cfg = HarnessConfig(
            repo_path=tmp_path,
            session_dir=tmp_path / "session",
            notebook_output_dir=tmp_path / "notebooks",
        )
        harness = BattleTestHarness(cfg)

        harness.boot_oracle = AsyncMock()
        harness.boot_governance_stack = AsyncMock()
        harness.boot_governed_loop_service = AsyncMock()
        harness.boot_jarvis_tiers = AsyncMock()
        harness.create_branch = AsyncMock(return_value="ouroboros/battle-test/test")
        harness.boot_intake = AsyncMock()
        harness.boot_graduation = AsyncMock()
        harness._shutdown_components = AsyncMock()
        harness._generate_report = AsyncMock()

        # Fire shutdown immediately
        async def fire_shutdown():
            await asyncio.sleep(0.01)
            harness._shutdown_event.set()

        asyncio.ensure_future(fire_shutdown())
        await harness.run()

        # Check that the stop_reason captured is related to shutdown
        assert harness._stop_reason == "shutdown_signal"

    @pytest.mark.asyncio
    async def test_harness_stop_reason_budget(self, tmp_path):
        """Verify stop_reason is 'budget_exhausted' when budget_event fires."""
        cfg = HarnessConfig(
            repo_path=tmp_path,
            session_dir=tmp_path / "session",
            notebook_output_dir=tmp_path / "notebooks",
        )
        harness = BattleTestHarness(cfg)

        harness.boot_oracle = AsyncMock()
        harness.boot_governance_stack = AsyncMock()
        harness.boot_governed_loop_service = AsyncMock()
        harness.boot_jarvis_tiers = AsyncMock()
        harness.create_branch = AsyncMock(return_value="ouroboros/battle-test/test")
        harness.boot_intake = AsyncMock()
        harness.boot_graduation = AsyncMock()
        harness._shutdown_components = AsyncMock()
        harness._generate_report = AsyncMock()

        # Fire budget event immediately
        async def fire_budget():
            await asyncio.sleep(0.01)
            harness._cost_tracker.budget_event.set()

        asyncio.ensure_future(fire_budget())
        await harness.run()

        assert harness._stop_reason == "budget_exhausted"

    @pytest.mark.asyncio
    async def test_harness_stop_reason_idle(self, tmp_path):
        """Verify stop_reason is 'idle_timeout' when idle_event fires."""
        cfg = HarnessConfig(
            repo_path=tmp_path,
            session_dir=tmp_path / "session",
            notebook_output_dir=tmp_path / "notebooks",
            idle_timeout_s=0.01,  # Very short timeout
        )
        harness = BattleTestHarness(cfg)

        harness.boot_oracle = AsyncMock()
        harness.boot_governance_stack = AsyncMock()
        harness.boot_governed_loop_service = AsyncMock()
        harness.boot_jarvis_tiers = AsyncMock()
        harness.create_branch = AsyncMock(return_value="ouroboros/battle-test/test")
        harness.boot_intake = AsyncMock()
        harness.boot_graduation = AsyncMock()
        harness._shutdown_components = AsyncMock()
        harness._generate_report = AsyncMock()

        # The idle watchdog should fire almost immediately with 0.01s timeout
        await harness.run()

        assert harness._stop_reason == "idle_timeout"


class TestHarnessCostPollInterval:
    """Regression for Task #95 (budget cap overshoot).

    ``_monitor_provider_costs()`` must use an env-driven poll interval.
    The default dropped from 5.0s → 1.0s so the budget_event fires soon
    enough after spend crosses --cost-cap that the in-flight shutdown
    catches the next paid call. Env: ``JARVIS_COST_POLL_INTERVAL_S``.
    """

    @pytest.mark.asyncio
    async def test_cost_monitor_default_interval_is_tight(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_COST_POLL_INTERVAL_S", raising=False)
        cfg = HarnessConfig(
            repo_path=tmp_path,
            session_dir=tmp_path / "session",
            notebook_output_dir=tmp_path / "notebooks",
        )
        harness = BattleTestHarness(cfg)
        harness._governed_loop_service = None

        observed: list = []

        async def fake_sleep(seconds: float) -> None:
            observed.append(seconds)
            raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fake_sleep):
            task = asyncio.create_task(harness._monitor_provider_costs())
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert observed, "cost monitor never slept"
        assert observed[0] == 1.0, f"default should be 1.0s, got {observed[0]}"

    @pytest.mark.asyncio
    async def test_cost_monitor_respects_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_COST_POLL_INTERVAL_S", "0.25")
        cfg = HarnessConfig(
            repo_path=tmp_path,
            session_dir=tmp_path / "session",
            notebook_output_dir=tmp_path / "notebooks",
        )
        harness = BattleTestHarness(cfg)
        harness._governed_loop_service = None

        observed: list = []

        async def fake_sleep(seconds: float) -> None:
            observed.append(seconds)
            raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fake_sleep):
            task = asyncio.create_task(harness._monitor_provider_costs())
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert observed[0] == 0.25, f"env var override ignored, got {observed[0]}"

    @pytest.mark.asyncio
    async def test_cost_monitor_rejects_non_positive_interval(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_COST_POLL_INTERVAL_S", "0")
        cfg = HarnessConfig(repo_path=tmp_path, session_dir=tmp_path / "session")
        harness = BattleTestHarness(cfg)
        harness._governed_loop_service = None

        observed: list = []

        async def fake_sleep(seconds: float) -> None:
            observed.append(seconds)
            raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fake_sleep):
            task = asyncio.create_task(harness._monitor_provider_costs())
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert observed[0] == 1.0, f"non-positive should fall back to 1.0, got {observed[0]}"

    @pytest.mark.asyncio
    async def test_cost_monitor_rejects_malformed_interval(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_COST_POLL_INTERVAL_S", "not-a-number")
        cfg = HarnessConfig(repo_path=tmp_path, session_dir=tmp_path / "session")
        harness = BattleTestHarness(cfg)
        harness._governed_loop_service = None

        observed: list = []

        async def fake_sleep(seconds: float) -> None:
            observed.append(seconds)
            raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fake_sleep):
            task = asyncio.create_task(harness._monitor_provider_costs())
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert observed[0] == 1.0, f"malformed should fall back to 1.0, got {observed[0]}"


class TestPlanReviewToggle:
    """REPL plan toggle wiring for pre-execution plan review."""

    @pytest.mark.asyncio
    async def test_plan_on_updates_env_and_flow(self, tmp_path):
        cfg = HarnessConfig(repo_path=tmp_path, session_dir=tmp_path / "session")
        harness = BattleTestHarness(cfg)
        harness._serpent_flow = MagicMock()
        printed: list[str] = []
        harness._repl_print = printed.append  # type: ignore[method-assign]

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_SHOW_PLAN_BEFORE_EXECUTE", None)
            await harness._handle_repl_command("/plan on")

            assert harness._plan_before_execute is True
            assert os.environ["JARVIS_SHOW_PLAN_BEFORE_EXECUTE"] == "1"

        harness._serpent_flow.set_plan_review_mode.assert_called_once_with(True)
        assert any("Plan review enabled" in line for line in printed)

    @pytest.mark.asyncio
    async def test_plan_off_updates_env_and_flow(self, tmp_path):
        cfg = HarnessConfig(repo_path=tmp_path, session_dir=tmp_path / "session")
        harness = BattleTestHarness(cfg)
        harness._serpent_flow = MagicMock()
        printed: list[str] = []
        harness._repl_print = printed.append  # type: ignore[method-assign]
        harness._plan_before_execute = True

        with patch.dict(os.environ, {"JARVIS_SHOW_PLAN_BEFORE_EXECUTE": "1"}, clear=False):
            await harness._handle_repl_command("/plan off")

            assert harness._plan_before_execute is False
            assert os.environ["JARVIS_SHOW_PLAN_BEFORE_EXECUTE"] == "0"

        harness._serpent_flow.set_plan_review_mode.assert_called_once_with(False)
        assert any("Plan review disabled" in line for line in printed)
