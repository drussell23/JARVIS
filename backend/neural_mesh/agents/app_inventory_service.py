"""
JARVIS Neural Mesh — AppInventoryService

A thin Neural Mesh agent wrapper around the AppLibrary singleton.
Provides dynamic, Spotlight-backed app discovery as a first-class agent
capability without hardcoding any application state.

Capabilities exposed:
  • check_app      — resolve a single app by name
  • scan_installed — bulk-check a dynamic set of productivity apps
  • app_inventory  — alias capability tag (used for discovery)
  • is_running     — thin delegate to check_app

Fallback strategy when AppLibrary is unavailable:
  Searches common macOS application directories directly via the
  filesystem so the agent degrades gracefully instead of failing hard.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default scan list — kept minimal, dynamic, and env-overridable.
# Override via JARVIS_APP_SCAN_LIST (comma-separated app names).
# ---------------------------------------------------------------------------

_DEFAULT_SCAN_APPS: List[str] = [
    "Safari",
    "Google Chrome",
    "Firefox",
    "Visual Studio Code",
    "Xcode",
    "Terminal",
    "iTerm2",
    "Slack",
    "Discord",
    "Zoom",
    "Spotify",
    "Finder",
    "Mail",
    "Calendar",
    "Notes",
    "Pages",
    "Numbers",
    "Keynote",
    "Preview",
    "Photos",
]

# Standard macOS application directories searched by the filesystem fallback.
_APP_DIRS: List[Path] = [
    Path("/Applications"),
    Path("/System/Applications"),
    Path.home() / "Applications",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_scan_list() -> List[str]:
    """Return the list of app names to scan, honouring env override."""
    raw = os.getenv("JARVIS_APP_SCAN_LIST", "").strip()
    if raw:
        return [name.strip() for name in raw.split(",") if name.strip()]
    return list(_DEFAULT_SCAN_APPS)


async def _filesystem_check(app_name: str) -> Dict[str, Any]:
    """
    Fallback resolution: scan common macOS app directories for *app_name*.

    Searches for ``{app_name}.app`` (case-insensitive) in each of the
    standard app directories.  Returns a dict shaped identically to the
    AppLibrary path so callers do not need to branch.
    """
    name_lower = app_name.lower().strip()

    def _scan() -> Optional[Path]:
        for directory in _APP_DIRS:
            try:
                for entry in directory.iterdir():
                    if entry.suffix == ".app" and entry.stem.lower() == name_lower:
                        return entry
            except (OSError, PermissionError):
                continue
        return None

    # Run blocking I/O in the default executor so we never block the loop.
    found_path: Optional[Path] = await asyncio.get_event_loop().run_in_executor(
        None, _scan
    )

    if found_path is not None:
        return {
            "found": True,
            "app_name": found_path.stem,
            "bundle_id": None,
            "path": str(found_path),
            "is_running": False,
            "window_count": 0,
            "confidence": 0.8,
        }

    return {
        "found": False,
        "app_name": app_name,
        "bundle_id": None,
        "path": None,
        "is_running": False,
        "window_count": 0,
        "confidence": 0.0,
    }


def _resolution_to_dict(result: Any) -> Dict[str, Any]:
    """Convert an AppResolutionResult (or mock) to a plain dict."""
    return {
        "found": bool(result.found),
        "app_name": result.app_name or "",
        "bundle_id": result.bundle_id,
        "path": result.path,
        "is_running": bool(result.is_running),
        "window_count": int(result.window_count),
        "confidence": float(result.confidence),
    }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AppInventoryService(BaseNeuralMeshAgent):
    """
    Neural Mesh wrapper around AppLibrary for dynamic app discovery.

    In normal operation the agent delegates to AppLibrary which uses
    macOS Spotlight (mdfind) for O(1) lookups plus intelligent caching.
    When AppLibrary is unavailable (e.g. during isolated tests or on
    non-macOS platforms) the agent falls back to a direct filesystem scan
    of the standard application directories.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="app_inventory_service",
            agent_type="system",
            capabilities={"app_inventory", "check_app", "scan_installed"},
            version="1.0.0",
            description=(
                "Dynamic macOS app discovery via Spotlight (AppLibrary) "
                "with filesystem fallback."
            ),
        )
        self._app_library: Optional[Any] = None
        self._use_fallback: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_initialize(self, **kwargs) -> None:  # noqa: D102
        """Wire up AppLibrary or note that the filesystem fallback will be used."""
        try:
            from backend.system.app_library import AppLibrary

            self._app_library = AppLibrary()
            self._use_fallback = False
            logger.info(
                "[AppInventoryService] Initialised with AppLibrary (Spotlight-backed)"
            )
        except Exception as exc:  # ImportError, AttributeError, etc.
            logger.warning(
                "[AppInventoryService] AppLibrary unavailable (%s). "
                "Falling back to filesystem scan.",
                exc,
            )
            self._app_library = None
            self._use_fallback = True

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Route to the appropriate action handler.

        Supported actions:
          • ``check_app``      — resolve one app by name
          • ``scan_installed`` — bulk-check common productivity apps
          • ``is_running``     — same as check_app (convenience alias)

        Args:
            payload: Must contain ``action`` (str).  ``check_app`` and
                     ``is_running`` also require ``app_name`` (str).

        Returns:
            Action-dependent dict (see individual handlers for schemas).

        Raises:
            ValueError: If ``action`` is missing or unrecognised.
        """
        action = str(payload.get("action", "")).strip().lower()

        if not action:
            raise ValueError("execute_task requires an 'action' key in the payload.")

        if action == "check_app":
            return await self._check_app(payload)
        if action == "scan_installed":
            return await self._scan_installed(payload)
        if action == "is_running":
            # Convenience alias — delegates entirely to check_app.
            return await self._check_app(payload)

        raise ValueError(
            f"Unknown action '{action}'. "
            "Supported: 'check_app', 'scan_installed', 'is_running'."
        )

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    async def _check_app(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve a single app name.

        Args:
            payload: Must contain ``app_name`` (str).

        Returns:
            Dict with keys: found, app_name, bundle_id, path,
            is_running, window_count, confidence.
        """
        app_name: str = str(payload.get("app_name", "")).strip()
        if not app_name:
            raise ValueError("'check_app' action requires 'app_name' in payload.")

        if self._app_library is not None:
            try:
                result = await self._app_library.resolve_app_name_async(app_name)
                return _resolution_to_dict(result)
            except Exception as exc:
                logger.warning(
                    "[AppInventoryService] AppLibrary resolution failed for '%s': %s. "
                    "Falling back to filesystem.",
                    app_name,
                    exc,
                )

        # Filesystem fallback
        return await _filesystem_check(app_name)

    async def _scan_installed(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Check a set of apps concurrently and return only the found ones.

        The app list is determined by the ``JARVIS_APP_SCAN_LIST`` env var
        (comma-separated names) or the built-in default list; there is no
        hardcoding in the agent code itself.

        Args:
            payload: Optional ``apps`` key (list[str]) overrides the scan list
                     for this single invocation.

        Returns:
            Dict with keys:
              • ``apps``          — list of result dicts for *found* apps
              • ``total_scanned`` — total number of names checked
        """
        override: Optional[List[str]] = payload.get("apps")
        scan_list: List[str] = (
            [str(a).strip() for a in override if str(a).strip()]
            if override
            else _build_scan_list()
        )

        # Run all checks concurrently for speed.
        tasks = [
            asyncio.create_task(
                self._check_app({"action": "check_app", "app_name": name}),
                name=f"scan_{name}",
            )
            for name in scan_list
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        found_apps: List[Dict[str, Any]] = []
        for name, res in zip(scan_list, results):
            if isinstance(res, Exception):
                logger.debug(
                    "[AppInventoryService] Scan error for '%s': %s", name, res
                )
                continue
            if res.get("found"):
                found_apps.append(res)

        return {
            "apps": found_apps,
            "total_scanned": len(scan_list),
        }
