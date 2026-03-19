"""
JARVIS Neural Mesh - Execution Tier Router

Decides whether a task should be executed via:
  - API  : direct Google Workspace / service API call
  - NATIVE_APP : launch / control the installed macOS application
  - BROWSER    : drive Chrome/Playwright for web-based interaction

Decision priority (highest to lowest):
  1. force_visual=True  →  BROWSER (unconditional override)
  2. workspace_service in _API_SERVICES  →  API
  3. target_app set + app_installed=True  →  NATIVE_APP
  4. target_app set + app_installed=False →  BROWSER
  5. goal text contains API-service keywords  →  API
  6. Default fallback  →  BROWSER

When app_installed is None and an AppInventoryService is available the router
performs a live async check via decide_tier_async().  The synchronous
decide_tier() falls back to BROWSER in that scenario.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from enum import Enum
from typing import Any, Dict, Optional, Set

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

class ExecutionTier(str, Enum):
    """Execution tier a task will be routed to."""

    API = "api"
    NATIVE_APP = "native_app"
    BROWSER = "browser"


# Services that have a direct Google Workspace / first-party API integration.
# Loaded once at module import; nothing is hard-coded in logic methods.
_API_SERVICES: frozenset[str] = frozenset(
    {
        "gmail",
        "calendar",
        "drive",
        "docs",
        "sheets",
        "contacts",
    }
)

# Keyword fragments that indicate an API-routable goal when no explicit
# workspace_service is provided.  Populated dynamically from _API_SERVICES so
# the keyword list always stays in sync.
_API_KEYWORDS: frozenset[str] = frozenset(
    {
        "email",
        "e-mail",
        "calendar",
        "event",
        "schedule",
        "appointment",
        "drive",
        "document",
        "spreadsheet",
        "sheet",
        "contact",
        *_API_SERVICES,
    }
)

# Web fall-back URLs for native apps that may not be installed.
# Key: canonical app name (title-cased); value: full HTTPS URL.
_WEB_ALTERNATIVES: dict[str, str] = {
    "WhatsApp": "https://web.whatsapp.com",
    "Slack": "https://app.slack.com",
    "Discord": "https://discord.com/app",
    "Telegram": "https://web.telegram.org",
    "Spotify": "https://open.spotify.com",
}


# ---------------------------------------------------------------------------
# Router agent
# ---------------------------------------------------------------------------

class ExecutionTierRouter(BaseNeuralMeshAgent):
    """
    Execution Tier Router — decides how to execute a task.

    Capabilities:
    - decide_tier   : synchronous tier decision (returns ExecutionTier)
    - tier_routing  : full async execution via execute_task()
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="execution_tier_router",
            agent_type="intelligence",
            capabilities={"decide_tier", "tier_routing"},
            version="1.0.0",
            description=(
                "Routes tasks to API, native-app control, or browser automation "
                "based on service availability and installed apps."
            ),
        )
        # Optional: injected by on_initialize() if AppInventoryService is present.
        self._app_inventory_service: Optional[Any] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_initialize(self, **kwargs) -> None:
        """Try to wire up AppInventoryService for dynamic app checks."""
        try:
            module = importlib.import_module(
                "backend.neural_mesh.agents.app_inventory_service"
            )
            svc_cls = getattr(module, "AppInventoryService", None)
            if svc_cls is not None:
                self._app_inventory_service = svc_cls()
                logger.info(
                    "ExecutionTierRouter: AppInventoryService wired for dynamic app checks"
                )
            else:
                logger.debug(
                    "ExecutionTierRouter: AppInventoryService class not found in module"
                )
        except (ImportError, Exception) as exc:
            logger.debug(
                "ExecutionTierRouter: AppInventoryService unavailable (%s) — "
                "dynamic app checks disabled",
                exc,
            )
            self._app_inventory_service = None

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def execute_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tasks routed to this agent.

        Supported actions:
          - ``decide_tier``: returns ``{"tier": str, "web_url": Optional[str], "goal": str}``
        """
        action = payload.get("action", "")

        if action == "decide_tier":
            return await self._handle_decide_tier(payload)

        raise ValueError(f"Unknown action: {action!r}")

    # ------------------------------------------------------------------
    # Core routing logic
    # ------------------------------------------------------------------

    def decide_tier(
        self,
        goal: str,
        *,
        workspace_service: Optional[str] = None,
        target_app: Optional[str] = None,
        app_installed: Optional[bool] = None,
        force_visual: bool = False,
    ) -> ExecutionTier:
        """Synchronously decide the execution tier.

        When ``app_installed`` is None and ``target_app`` is set, this method
        cannot perform an async lookup; it defaults to BROWSER.  Use
        ``decide_tier_async()`` for the live-check path.

        Args:
            goal: Natural-language description of the task.
            workspace_service: Normalised service slug (e.g. "gmail").
            target_app: Canonical app name (e.g. "WhatsApp").
            app_installed: Explicit install status; None = unknown.
            force_visual: When True, always return BROWSER.

        Returns:
            The selected ExecutionTier.
        """
        # 1. Visual override — unconditional
        if force_visual:
            logger.debug("ExecutionTierRouter: force_visual → BROWSER")
            return ExecutionTier.BROWSER

        # 2. Explicit workspace API service
        if workspace_service and workspace_service.lower() in _API_SERVICES:
            logger.debug(
                "ExecutionTierRouter: workspace_service=%r → API", workspace_service
            )
            return ExecutionTier.API

        # 3 & 4. Known target app + explicit install status
        if target_app is not None:
            if app_installed is True:
                logger.debug(
                    "ExecutionTierRouter: target_app=%r installed=True → NATIVE_APP",
                    target_app,
                )
                return ExecutionTier.NATIVE_APP
            if app_installed is False:
                logger.debug(
                    "ExecutionTierRouter: target_app=%r installed=False → BROWSER",
                    target_app,
                )
                return ExecutionTier.BROWSER
            # app_installed is None and no async available → fall through to
            # keyword check, then default BROWSER

        # 5. Keyword match on goal text
        goal_lower = goal.lower()
        if any(kw in goal_lower for kw in _API_KEYWORDS):
            logger.debug(
                "ExecutionTierRouter: goal keyword match → API (goal=%r)", goal[:80]
            )
            return ExecutionTier.API

        # 6. Default
        logger.debug("ExecutionTierRouter: default → BROWSER (goal=%r)", goal[:80])
        return ExecutionTier.BROWSER

    async def decide_tier_async(
        self,
        goal: str,
        *,
        workspace_service: Optional[str] = None,
        target_app: Optional[str] = None,
        app_installed: Optional[bool] = None,
        force_visual: bool = False,
    ) -> ExecutionTier:
        """Async version of decide_tier that performs live app-install checks.

        When ``app_installed`` is None and ``target_app`` is set, queries
        AppInventoryService (if available) before making the tier decision.

        Falls back to BROWSER on any error from AppInventoryService.
        """
        resolved_installed = app_installed

        if (
            target_app is not None
            and app_installed is None
            and self._app_inventory_service is not None
        ):
            try:
                check_result = await self._app_inventory_service.execute_task({
                    "action": "check_app",
                    "app_name": target_app,
                })
                resolved_installed = check_result.get("found", False)
                logger.debug(
                    "ExecutionTierRouter: dynamic check app=%r → installed=%s",
                    target_app,
                    resolved_installed,
                )
            except Exception as exc:
                logger.warning(
                    "ExecutionTierRouter: app check for %r failed: %s — "
                    "falling back to BROWSER",
                    target_app,
                    exc,
                )
                # Emit learning experience for the error-driven fallback
                try:
                    from core.trinity_event_bus import get_event_bus_if_exists
                    bus = get_event_bus_if_exists()
                    if bus:
                        await bus.publish_raw(
                            topic="tier.fallback",
                            data={
                                "goal": goal,
                                "target_app": target_app,
                                "original_tier": "native_app",
                                "fallback_tier": "browser",
                                "reason": "app_check_error",
                                "web_alternative": self.get_web_alternative(target_app),
                                "timestamp": time.time(),
                            },
                        )
                except Exception:
                    pass  # Learning is best-effort
                return ExecutionTier.BROWSER

        # Emit a learning experience when the app is confirmed NOT installed
        # and the tier will fall back to BROWSER.
        if target_app is not None and resolved_installed is False:
            try:
                from core.trinity_event_bus import get_event_bus_if_exists
                bus = get_event_bus_if_exists()
                if bus:
                    await bus.publish_raw(
                        topic="tier.fallback",
                        data={
                            "goal": goal,
                            "target_app": target_app,
                            "original_tier": "native_app",
                            "fallback_tier": "browser",
                            "reason": "app_not_installed",
                            "web_alternative": self.get_web_alternative(target_app),
                            "timestamp": time.time(),
                        },
                    )
            except Exception:
                pass  # Learning is best-effort

        return self.decide_tier(
            goal,
            workspace_service=workspace_service,
            target_app=target_app,
            app_installed=resolved_installed,
            force_visual=force_visual,
        )

    # ------------------------------------------------------------------
    # Web alternative lookup
    # ------------------------------------------------------------------

    def get_web_alternative(self, app_name: str) -> Optional[str]:
        """Return the web URL alternative for a native app, or None.

        The lookup is case-insensitive against the canonical keys in
        ``_WEB_ALTERNATIVES``.

        Args:
            app_name: Name of the native application.

        Returns:
            HTTPS URL string if a web alternative exists, otherwise None.
        """
        # Direct lookup first (preserves exact match performance)
        if app_name in _WEB_ALTERNATIVES:
            return _WEB_ALTERNATIVES[app_name]

        # Case-insensitive fallback
        app_name_lower = app_name.lower()
        for key, url in _WEB_ALTERNATIVES.items():
            if key.lower() == app_name_lower:
                return url

        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle_decide_tier(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute the decide_tier action from a task payload.

        Payload keys (all optional except ``goal``):
          - ``goal``              : str  — task description
          - ``workspace_service`` : str  — service slug
          - ``target_app``        : str  — native app name
          - ``app_installed``     : bool — explicit install flag
          - ``force_visual``      : bool — force browser tier
        """
        goal: str = payload.get("goal", "")
        workspace_service: Optional[str] = payload.get("workspace_service")
        target_app: Optional[str] = payload.get("target_app")
        app_installed: Optional[bool] = payload.get("app_installed")
        force_visual: bool = bool(payload.get("force_visual", False))

        tier = await self.decide_tier_async(
            goal,
            workspace_service=workspace_service,
            target_app=target_app,
            app_installed=app_installed,
            force_visual=force_visual,
        )

        # Resolve web URL: only meaningful when BROWSER tier was chosen for a
        # known target app.
        web_url: Optional[str] = None
        if tier == ExecutionTier.BROWSER and target_app:
            web_url = self.get_web_alternative(target_app)

        return {
            "tier": tier.value,
            "web_url": web_url,
            "goal": goal,
        }
