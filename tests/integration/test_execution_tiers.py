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


# ---------------------------------------------------------------------------
# ExecutionTierRouter tests  (Task 2)
# ---------------------------------------------------------------------------

class TestExecutionTierRouter:
    """Tests for ExecutionTierRouter routing logic."""

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    @pytest.fixture
    def router(self):
        """Return an uninitialised ExecutionTierRouter (no Neural Mesh needed)."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTierRouter
        return ExecutionTierRouter()

    # ------------------------------------------------------------------
    # Core routing — the five required test cases
    # ------------------------------------------------------------------

    def test_gmail_routes_to_api_tier(self, router) -> None:
        """workspace_service='gmail' must resolve to the API tier."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier(
            "send an email to Derek",
            workspace_service="gmail",
        )
        assert tier == ExecutionTier.API

    def test_whatsapp_installed_routes_to_native(self, router) -> None:
        """target_app='WhatsApp' + app_installed=True -> NATIVE_APP."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier(
            "send a WhatsApp message",
            target_app="WhatsApp",
            app_installed=True,
        )
        assert tier == ExecutionTier.NATIVE_APP

    def test_whatsapp_not_installed_routes_to_browser(self, router) -> None:
        """target_app='WhatsApp' + app_installed=False -> BROWSER."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier(
            "send a WhatsApp message",
            target_app="WhatsApp",
            app_installed=False,
        )
        assert tier == ExecutionTier.BROWSER

    def test_visual_request_forces_browser(self, router) -> None:
        """force_visual=True overrides everything and returns BROWSER."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        # Even with a valid API service, force_visual wins.
        tier = router.decide_tier(
            "check my calendar visually",
            workspace_service="calendar",
            force_visual=True,
        )
        assert tier == ExecutionTier.BROWSER

    def test_linkedin_no_api_routes_to_browser(self, router) -> None:
        """No known API service, no target app -> falls through to BROWSER."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier("browse LinkedIn profiles")
        assert tier == ExecutionTier.BROWSER

    # ------------------------------------------------------------------
    # Additional coverage
    # ------------------------------------------------------------------

    def test_all_api_services_route_to_api(self, router) -> None:
        """Every service in _API_SERVICES must route to API."""
        from backend.neural_mesh.agents.execution_tier_router import (
            ExecutionTier,
            _API_SERVICES,
        )

        for svc in _API_SERVICES:
            tier = router.decide_tier(
                f"do something with {svc}", workspace_service=svc
            )
            assert tier == ExecutionTier.API, f"Expected API for service={svc!r}"

    def test_slack_installed_routes_to_native(self, router) -> None:
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier(
            "message the team on Slack",
            target_app="Slack",
            app_installed=True,
        )
        assert tier == ExecutionTier.NATIVE_APP

    def test_slack_not_installed_routes_to_browser(self, router) -> None:
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier(
            "message the team on Slack",
            target_app="Slack",
            app_installed=False,
        )
        assert tier == ExecutionTier.BROWSER

    def test_get_web_alternative_known_app(self, router) -> None:
        url = router.get_web_alternative("WhatsApp")
        assert url == "https://web.whatsapp.com"

    def test_get_web_alternative_unknown_app(self, router) -> None:
        url = router.get_web_alternative("NonExistentApp123")
        assert url is None

    def test_get_web_alternative_case_insensitive(self, router) -> None:
        """Lookup works regardless of input capitalisation."""
        url = router.get_web_alternative("spotify")
        assert url is not None

    def test_email_keyword_routes_to_api(self, router) -> None:
        """Goals containing email keyword should route to API without workspace_service."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier("send an email to my boss")
        assert tier == ExecutionTier.API

    def test_calendar_keyword_routes_to_api(self, router) -> None:
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier("schedule a calendar event tomorrow")
        assert tier == ExecutionTier.API

    def test_force_visual_overrides_native_app(self, router) -> None:
        """force_visual=True must win even when app is installed."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        tier = router.decide_tier(
            "show me Slack visually",
            target_app="Slack",
            app_installed=True,
            force_visual=True,
        )
        assert tier == ExecutionTier.BROWSER

    # ------------------------------------------------------------------
    # execute_task dispatch
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_task_decide_tier_gmail(self, router) -> None:
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        result = await router.execute_task(
            {
                "action": "decide_tier",
                "goal": "draft a reply email",
                "workspace_service": "gmail",
            }
        )
        assert result["tier"] == ExecutionTier.API.value
        assert result["goal"] == "draft a reply email"

    @pytest.mark.asyncio
    async def test_execute_task_decide_tier_browser(self, router) -> None:
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        result = await router.execute_task(
            {
                "action": "decide_tier",
                "goal": "open LinkedIn",
            }
        )
        assert result["tier"] == ExecutionTier.BROWSER.value

    @pytest.mark.asyncio
    async def test_execute_task_not_installed_includes_web_url(
        self, router
    ) -> None:
        """BROWSER tier for a known app should include web_url in result."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        result = await router.execute_task(
            {
                "action": "decide_tier",
                "goal": "open WhatsApp",
                "target_app": "WhatsApp",
                "app_installed": False,
            }
        )
        assert result["tier"] == ExecutionTier.BROWSER.value
        assert result["web_url"] == "https://web.whatsapp.com"

    @pytest.mark.asyncio
    async def test_execute_task_unknown_action_raises(self, router) -> None:
        with pytest.raises(ValueError, match="Unknown action"):
            await router.execute_task({"action": "fly_to_moon"})

    # ------------------------------------------------------------------
    # on_initialize — graceful degradation when AppInventoryService absent
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_initialize_without_app_inventory(self, router) -> None:
        """on_initialize must not raise even when AppInventoryService is missing."""
        with patch(
            "backend.neural_mesh.agents.execution_tier_router.importlib.import_module",
            side_effect=ImportError("no module"),
        ):
            await router.on_initialize()
        assert router._app_inventory_service is None

    @pytest.mark.asyncio
    async def test_on_initialize_with_mock_app_inventory(self, router) -> None:
        """When AppInventoryService is importable, it should be stored."""
        mock_svc_cls = MagicMock()
        mock_svc_instance = MagicMock()
        mock_svc_cls.return_value = mock_svc_instance

        mock_module = MagicMock()
        mock_module.AppInventoryService = mock_svc_cls

        with patch(
            "backend.neural_mesh.agents.execution_tier_router.importlib.import_module",
            return_value=mock_module,
        ):
            await router.on_initialize()

        assert router._app_inventory_service is not None

    # ------------------------------------------------------------------
    # Dynamic app check (app_installed=None + AppInventoryService present)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_dynamic_app_check_installed(self, router) -> None:
        """With app_installed=None and mock service saying installed -> NATIVE_APP."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        mock_svc = AsyncMock()
        mock_svc.execute_task = AsyncMock(return_value={"found": True, "app_name": "Discord"})
        router._app_inventory_service = mock_svc

        tier = await router.decide_tier_async(
            "open Discord",
            target_app="Discord",
            app_installed=None,
        )
        assert tier == ExecutionTier.NATIVE_APP

    @pytest.mark.asyncio
    async def test_dynamic_app_check_not_installed(self, router) -> None:
        """With app_installed=None and mock service saying not installed -> BROWSER."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        mock_svc = AsyncMock()
        mock_svc.execute_task = AsyncMock(return_value={"found": False})
        router._app_inventory_service = mock_svc

        tier = await router.decide_tier_async(
            "open Discord",
            target_app="Discord",
            app_installed=None,
        )
        assert tier == ExecutionTier.BROWSER

    @pytest.mark.asyncio
    async def test_dynamic_app_check_service_error_falls_back_to_browser(
        self, router
    ) -> None:
        """If AppInventoryService.execute_task raises, fall back to BROWSER."""
        from backend.neural_mesh.agents.execution_tier_router import ExecutionTier

        mock_svc = AsyncMock()
        mock_svc.execute_task = AsyncMock(side_effect=RuntimeError("service down"))
        router._app_inventory_service = mock_svc

        tier = await router.decide_tier_async(
            "open Telegram",
            target_app="Telegram",
            app_installed=None,
        )
        assert tier == ExecutionTier.BROWSER


# ---------------------------------------------------------------------------
# VisualBrowserAgent tests  (Task 4)
# ---------------------------------------------------------------------------

class TestVisualBrowserAgent:
    """Tests for VisualBrowserAgent — Playwright + J-Prime vision for Chrome."""

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    @pytest.fixture
    def agent(self):
        """Return an uninitialised VisualBrowserAgent instance."""
        from backend.neural_mesh.agents.visual_browser_agent import VisualBrowserAgent
        return VisualBrowserAgent()

    # ------------------------------------------------------------------
    # Structural / metadata tests
    # ------------------------------------------------------------------

    def test_agent_name_and_type(self, agent) -> None:
        """Agent must be named 'visual_browser_agent' with type 'autonomy'."""
        assert agent.agent_name == "visual_browser_agent"
        assert agent.agent_type == "autonomy"

    def test_agent_has_correct_capabilities(self, agent) -> None:
        """Agent capabilities must include visual_browser and browse_and_interact."""
        assert {"visual_browser", "browse_and_interact"} <= agent.capabilities

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_task_requires_url_or_goal(self, agent) -> None:
        """Calling browse_and_interact with neither url nor goal returns an error dict."""
        await agent.initialize()  # standalone mode — no browser launched yet

        result = await agent.execute_task(
            {"action": "browse_and_interact", "url": "", "goal": ""}
        )

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert result["steps_taken"] == 0
        assert isinstance(result["actions"], list)
        assert len(result["actions"]) == 0


# ---------------------------------------------------------------------------
# NativeAppControlAgent tests  (Task 3)
# ---------------------------------------------------------------------------


class TestNativeAppControlAgent:
    """Tests for NativeAppControlAgent vision-action loop agent."""

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    @pytest.fixture
    def agent(self):
        """Return an uninitialised NativeAppControlAgent instance."""
        from backend.neural_mesh.agents.native_app_control_agent import (
            NativeAppControlAgent,
        )
        return NativeAppControlAgent()

    # ------------------------------------------------------------------
    # Structural / metadata tests
    # ------------------------------------------------------------------

    def test_agent_has_correct_capabilities(self, agent) -> None:
        """Agent must advertise native_app_control and interact_with_app."""
        assert {"native_app_control", "interact_with_app"} <= agent.capabilities

    def test_agent_name(self, agent) -> None:
        assert agent.agent_name == "native_app_control_agent"

    def test_agent_type(self, agent) -> None:
        assert agent.agent_type == "autonomy"

    # ------------------------------------------------------------------
    # Validation: empty app_name
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_task_validates_app_name(self, agent) -> None:
        """Empty app_name must return an error dict (not raise)."""
        agent._initialized = True  # Skip full init for unit test
        result = await agent.execute_task(
            {"action": "interact_with_app", "app_name": "", "goal": "do something"}
        )
        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert result["steps_taken"] == 0

    # ------------------------------------------------------------------
    # Validation: empty goal
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_task_validates_goal(self, agent) -> None:
        """Empty goal must return an error dict (not raise)."""
        agent._initialized = True
        result = await agent.execute_task(
            {"action": "interact_with_app", "app_name": "Finder", "goal": ""}
        )
        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert result["steps_taken"] == 0

    # ------------------------------------------------------------------
    # App installed check
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_task_checks_app_installed(self, agent) -> None:
        """A definitely non-existent app must produce an error mentioning 'not installed'."""
        agent._initialized = True

        # Provide a real mock AppInventoryService that reports not-found
        mock_svc = MagicMock()
        mock_svc.execute_task = AsyncMock(
            return_value={
                "found": False,
                "app_name": "XJARVIS_GHOST_APP_XYZ",
                "bundle_id": None,
                "path": None,
                "is_running": False,
                "window_count": 0,
                "confidence": 0.0,
            }
        )
        agent._app_inventory_service = mock_svc

        result = await agent.execute_task(
            {
                "action": "interact_with_app",
                "app_name": "XJARVIS_GHOST_APP_XYZ",
                "goal": "open settings",
            }
        )

        assert isinstance(result, dict)
        assert result["success"] is False
        # Error or final_message must mention "not installed"
        combined = (result.get("error", "") + result.get("final_message", "")).lower()
        assert "not installed" in combined
        assert result["steps_taken"] == 0

    # ------------------------------------------------------------------
    # Post-action verification — Task 4
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_verify_action_returns_true_when_no_vision(self, agent) -> None:
        """_verify_action returns True when no vision model is reachable.

        With no ANTHROPIC_API_KEY and J-Prime unavailable (both raise exceptions),
        the method must fall back to True so the main loop is never blocked.
        """
        import os
        from unittest.mock import AsyncMock, patch

        agent._initialized = True

        # Patch _take_screenshot to return a minimal fake JPEG b64 string
        fake_b64 = "AAAA"  # not a real image, but enough to pass the None guard

        with patch.object(agent, "_take_screenshot", new=AsyncMock(return_value=fake_b64)):
            # Ensure no API key so Claude path is skipped
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
                # Make get_prime_client raise to skip J-Prime path
                with patch(
                    "backend.neural_mesh.agents.native_app_control_agent.asyncio.wait_for",
                    side_effect=Exception("prime unavailable"),
                ):
                    result = await agent._verify_action(
                        app_name="TestApp",
                        action_description="Click the save button",
                        expected_result="File saved",
                        max_retries=0,
                    )

        assert result is True

    def test_native_agent_has_verify_method(self, agent) -> None:
        """NativeAppControlAgent must expose a _verify_action coroutine method."""
        import inspect

        assert hasattr(agent, "_verify_action"), (
            "_verify_action method is missing from NativeAppControlAgent"
        )
        assert inspect.iscoroutinefunction(agent._verify_action), (
            "_verify_action must be an async def coroutine"
        )
