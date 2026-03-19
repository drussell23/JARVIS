"""
Integration tests for Execution Tier Agents.

Task 1: AppInventoryService — Neural Mesh wrapper for app discovery
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_resolution(
    found: bool,
    app_name: str = "",
    bundle_id: str | None = None,
    path: str | None = None,
    is_running: bool = False,
    window_count: int = 0,
    confidence: float = 0.0,
):
    """Build a minimal mock AppResolutionResult."""
    result = MagicMock()
    result.found = found
    result.app_name = app_name
    result.bundle_id = bundle_id
    result.path = path
    result.is_running = is_running
    result.window_count = window_count
    result.confidence = confidence
    return result


# ---------------------------------------------------------------------------
# AppInventoryService tests
# ---------------------------------------------------------------------------

class TestAppInventoryService:
    """Tests for AppInventoryService Neural Mesh agent."""

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    @pytest.fixture
    def agent(self):
        """Return an uninitialised AppInventoryService instance."""
        from backend.neural_mesh.agents.app_inventory_service import (
            AppInventoryService,
        )
        return AppInventoryService()

    @pytest.fixture
    async def initialised_agent(self, agent):
        """Return an agent that has gone through on_initialize() in standalone mode."""
        await agent.initialize()
        return agent

    # ------------------------------------------------------------------
    # Structural / metadata tests
    # ------------------------------------------------------------------

    def test_agent_name(self, agent):
        assert agent.agent_name == "app_inventory_service"

    def test_agent_type(self, agent):
        assert agent.agent_type == "system"

    def test_capabilities(self, agent):
        assert {"app_inventory", "check_app", "scan_installed"} <= agent.capabilities

    # ------------------------------------------------------------------
    # test_check_app_installed_vscode
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_check_app_installed_vscode(self, initialised_agent):
        """check_app for VS Code returns a dict with the expected shape.

        We accept found=True OR found=False because VS Code may or may not
        be installed on the CI machine, but we always want a well-formed dict.
        """
        result = await initialised_agent.execute_task(
            {"action": "check_app", "app_name": "Visual Studio Code"}
        )

        assert isinstance(result, dict)
        assert "found" in result
        assert "app_name" in result
        assert "bundle_id" in result
        assert "path" in result
        assert "is_running" in result
        assert "window_count" in result
        assert "confidence" in result
        assert isinstance(result["found"], bool)
        assert isinstance(result["is_running"], bool)
        assert isinstance(result["window_count"], int)
        assert isinstance(result["confidence"], float)

    # ------------------------------------------------------------------
    # test_check_app_not_installed
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_check_app_not_installed(self, initialised_agent):
        """check_app for a definitely non-existent app returns found=False."""
        result = await initialised_agent.execute_task(
            {"action": "check_app", "app_name": "XJARVIS_DEFINITELY_NOT_INSTALLED_APP_XYZ"}
        )

        assert isinstance(result, dict)
        assert result["found"] is False
        assert "app_name" in result

    # ------------------------------------------------------------------
    # test_scan_all_returns_list
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_scan_all_returns_list(self, initialised_agent):
        """scan_installed returns a dict with an 'apps' list and total_scanned count."""
        result = await initialised_agent.execute_task({"action": "scan_installed"})

        assert isinstance(result, dict)
        assert "apps" in result
        assert "total_scanned" in result
        assert isinstance(result["apps"], list)
        assert isinstance(result["total_scanned"], int)
        assert result["total_scanned"] >= 0
        # Every item in the list must have at least 'app_name' and 'found'
        for app in result["apps"]:
            assert "app_name" in app
            assert "found" in app

    # ------------------------------------------------------------------
    # test_is_running action (delegates to check_app)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_is_running_action_returns_dict(self, initialised_agent):
        """is_running action returns the same shaped dict as check_app."""
        result = await initialised_agent.execute_task(
            {"action": "is_running", "app_name": "Finder"}
        )

        assert isinstance(result, dict)
        assert "found" in result
        assert "is_running" in result

    # ------------------------------------------------------------------
    # test_unknown_action raises ValueError
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self, initialised_agent):
        """execute_task raises ValueError for unrecognised action strings."""
        with pytest.raises(ValueError, match="Unknown action"):
            await initialised_agent.execute_task({"action": "explode_everything"})

    # ------------------------------------------------------------------
    # Fallback path: AppLibrary unavailable
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_fallback_when_app_library_unavailable(self):
        """Agent falls back to filesystem checks when AppLibrary cannot be imported.

        We simulate the unavailable-library state by directly placing the agent
        into fallback mode (as on_initialize() does when the import fails) and
        then verifying the filesystem fallback handles a real check_app call.
        """
        from backend.neural_mesh.agents.app_inventory_service import (
            AppInventoryService,
        )

        agent = AppInventoryService()
        # Simulate what on_initialize does when AppLibrary import fails:
        # leave _app_library as None and flag _use_fallback.
        agent._app_library = None
        agent._use_fallback = True
        agent._initialized = True  # Skip the full initialize() call

        # Even without AppLibrary the agent should handle check_app gracefully
        result = await agent.execute_task(
            {"action": "check_app", "app_name": "Finder"}
        )
        assert isinstance(result, dict)
        assert "found" in result
        # Finder always exists on macOS; if the test runs on macOS it will be True,
        # on other platforms it might be False — either is fine as long as the
        # response is well-formed.
        assert isinstance(result["found"], bool)

    # ------------------------------------------------------------------
    # AppLibrary mocked — verify delegation
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_check_app_delegates_to_app_library(self):
        """check_app correctly maps AppResolutionResult fields to the output dict."""
        from backend.neural_mesh.agents.app_inventory_service import (
            AppInventoryService,
        )

        mock_result = _make_mock_resolution(
            found=True,
            app_name="Google Chrome",
            bundle_id="com.google.Chrome",
            path="/Applications/Google Chrome.app",
            is_running=True,
            window_count=3,
            confidence=0.97,
        )

        mock_library = MagicMock()
        mock_library.resolve_app_name_async = AsyncMock(return_value=mock_result)

        agent = AppInventoryService()
        agent._app_library = mock_library
        agent._initialized = True  # Skip re-init

        result = await agent.execute_task(
            {"action": "check_app", "app_name": "Chrome"}
        )

        assert result["found"] is True
        assert result["app_name"] == "Google Chrome"
        assert result["bundle_id"] == "com.google.Chrome"
        assert result["path"] == "/Applications/Google Chrome.app"
        assert result["is_running"] is True
        assert result["window_count"] == 3
        assert result["confidence"] == pytest.approx(0.97)

        mock_library.resolve_app_name_async.assert_awaited_once_with("Chrome")
