"""
Atomic application management tools -- launch, activate, discover, navigate.

These tools provide the Executor and Communicator contexts with macOS
application lifecycle control.  All functions delegate to the existing
SpatialAwarenessAgent, AppInventoryService, and computer_use_bridge
infrastructure.

Async-first.  No blocking I/O.  No hardcoded app lists.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_APP_TIMEOUT_S = float(os.environ.get("TOOL_APP_TIMEOUT_S", "10.0"))
_ACTIVATE_TIMEOUT_S = float(os.environ.get("TOOL_ACTIVATE_TIMEOUT_S", "15.0"))


@dataclass(frozen=True)
class AppInfo:
    """Information about a macOS application.

    Attributes:
        name: Display name (e.g., "Safari", "WhatsApp").
        bundle_id: macOS bundle identifier (e.g., "com.apple.Safari").
        path: Filesystem path to the .app bundle.
        is_running: Whether the app is currently in memory.
        window_count: Number of open windows (0 if not running).
    """
    name: str
    bundle_id: str = ""
    path: str = ""
    is_running: bool = False
    window_count: int = 0


@dataclass(frozen=True)
class SwitchResult:
    """Outcome of switching to an application.

    Attributes:
        success: True if the app is now focused.
        result_type: One of "already_focused", "switched_space", "launched_app", "success".
        app_name: Name of the target app.
        from_space: macOS Space ID where the user was before switching.
        to_space: macOS Space ID where the app was found.
        space_changed: True if a Space switch was required.
        duration_ms: Time taken for the switch operation.
    """
    success: bool
    result_type: str
    app_name: str
    from_space: int = 0
    to_space: int = 0
    space_changed: bool = False
    duration_ms: float = 0.0


@dataclass
class SpatialContext:
    """Current spatial state of the macOS desktop.

    Attributes:
        focused_app: Name of the currently focused application.
        current_space: Active Space ID.
        total_spaces: Number of configured Spaces.
        app_locations: Map of app names to the Space IDs they appear on.
        window_count: Total number of visible windows.
    """
    focused_app: str = ""
    current_space: int = 0
    total_spaces: int = 0
    app_locations: Dict[str, List[int]] = field(default_factory=dict)
    window_count: int = 0


async def open_app(app_name: str) -> bool:
    """Launch a macOS application by name.

    Uses the native ``open -a`` command which resolves app names via
    Spotlight and the Applications directory.  If the app is already
    running, it is brought to the foreground.

    Args:
        app_name: Application name (e.g., "WhatsApp", "Safari", "Terminal").
            Case-insensitive.  Partial matches work ("chrome" finds Google Chrome).

    Returns:
        True if the app launched or was already running.

    Use when:
        The Executor needs to start an app that is not yet running,
        or bring a running app to the foreground as the first step
        of a UI automation task.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "open", "-a", app_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        returncode = await asyncio.wait_for(proc.wait(), timeout=_APP_TIMEOUT_S)
        success = returncode == 0
        if success:
            logger.info("[tool:apps] Opened app: %s", app_name)
        else:
            logger.error("[tool:apps] Failed to open %s (exit %d)", app_name, returncode)
        return success
    except asyncio.TimeoutError:
        logger.error("[tool:apps] open -a %s timed out", app_name)
        return False
    except Exception as exc:
        logger.error("[tool:apps] open_app error: %s", exc)
        return False


async def activate_app(app_name: str) -> bool:
    """Bring a running application to the foreground via AppleScript.

    Unlike open_app(), this does not launch the app if it is not running.
    It activates (focuses) an already-running app.

    Args:
        app_name: Application name to activate.

    Returns:
        True if the app was activated.  False if it is not running or
        the activation failed.

    Use when:
        The Executor needs to switch focus to a running app without
        opening a new instance, before performing actions on its window.
    """
    safe_name = app_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "{safe_name}" to activate'

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_ACTIVATE_TIMEOUT_S)
        success = proc.returncode == 0
        if success:
            logger.info("[tool:apps] Activated: %s", app_name)
        else:
            logger.warning("[tool:apps] Activate failed: %s (%s)",
                         app_name, stderr.decode(errors="replace").strip()[:100])
        return success
    except asyncio.TimeoutError:
        logger.error("[tool:apps] activate_app timed out for %s", app_name)
        return False
    except Exception as exc:
        logger.error("[tool:apps] activate_app error: %s", exc)
        return False


async def list_running_apps() -> List[str]:
    """List all currently running foreground applications.

    Queries macOS System Events for processes whose ``background only``
    property is false.  Returns application names sorted alphabetically.

    Returns:
        List of app names (e.g., ["Chrome", "Finder", "Safari", "Terminal"]).
        Empty list if the query fails.

    Use when:
        The Architect needs to know which apps are running to plan a task
        (e.g., "is WhatsApp open?" or "what apps can I interact with?").
    """
    script = (
        'tell application "System Events" to get name of '
        'every process whose background only is false'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            return []

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return []

        apps = sorted(set(a.strip() for a in raw.split(", ") if a.strip()))
        logger.info("[tool:apps] %d running apps", len(apps))
        return apps

    except asyncio.TimeoutError:
        logger.error("[tool:apps] list_running_apps timed out")
        return []
    except Exception as exc:
        logger.error("[tool:apps] list_running_apps error: %s", exc)
        return []


async def check_app_installed(app_name: str) -> Optional[AppInfo]:
    """Check if a macOS application is installed and optionally running.

    Delegates to AppInventoryService which uses Spotlight (AppLibrary)
    for fast O(1) lookups with filesystem fallback.

    Args:
        app_name: Application name to check (case-insensitive).

    Returns:
        AppInfo with install path, bundle ID, and running status.
        None if the app is not installed.

    Use when:
        The Architect needs to verify an app is available before planning
        a task that requires it (e.g., "is Slack installed?").
    """
    for import_path in ("backend.neural_mesh.agents.app_inventory_service",
                        "neural_mesh.agents.app_inventory_service"):
        try:
            import importlib
            mod = importlib.import_module(import_path)
            service_cls = getattr(mod, "AppInventoryService", None)
            if service_cls:
                service = service_cls()
                result = await service._check_app({"action": "check_app", "app_name": app_name})
                if result.get("found"):
                    return AppInfo(
                        name=result.get("app_name", app_name),
                        bundle_id=result.get("bundle_id", ""),
                        path=result.get("path", ""),
                        is_running=result.get("is_running", False),
                        window_count=result.get("window_count", 0),
                    )
                return None
        except (ImportError, Exception):
            continue

    return await _check_app_filesystem(app_name)


async def get_spatial_context() -> SpatialContext:
    """Get the current macOS desktop spatial state.

    Returns which app is focused, which Space is active, how many Spaces
    exist, and which apps are on which Spaces.  Delegates to the existing
    computer_use_bridge / SpatialAwarenessManager.

    Returns:
        SpatialContext with current focus, spaces, and app locations.

    Use when:
        The Architect needs to understand the desktop layout before
        planning multi-app or multi-space workflows.
    """
    for import_path in ("backend.core.computer_use_bridge",
                        "core.computer_use_bridge"):
        try:
            import importlib
            mod = importlib.import_module(import_path)
            get_ctx = getattr(mod, "get_current_context", None)
            if get_ctx:
                ctx = await get_ctx(force_refresh=True)
                if ctx:
                    return SpatialContext(
                        focused_app=getattr(ctx, "focused_app", ""),
                        current_space=getattr(ctx, "current_space_id", 0),
                        total_spaces=getattr(ctx, "total_spaces", 0),
                        app_locations=getattr(ctx, "app_locations", {}),
                        window_count=getattr(ctx, "total_windows", 0),
                    )
        except (ImportError, Exception) as exc:
            logger.debug("[tool:apps] computer_use_bridge unavailable: %s", exc)
            continue

    return await _basic_spatial_context()


async def switch_to_app(app_name: str) -> SwitchResult:
    """Switch to an application, crossing macOS Spaces if needed.

    Delegates to computer_use_bridge.switch_to_app_smart() which uses
    Yabai for Space-aware window targeting when available, falling back
    to ``open -a`` for basic app activation.

    Args:
        app_name: Application name to switch to.

    Returns:
        SwitchResult with success status, whether a Space switch occurred,
        and the source/target Space IDs.

    Use when:
        The Executor needs to navigate to an app that may be on a different
        macOS Space, without losing track of the current workspace state.
    """
    for import_path in ("backend.core.computer_use_bridge",
                        "core.computer_use_bridge"):
        try:
            import importlib
            mod = importlib.import_module(import_path)
            switch_fn = getattr(mod, "switch_to_app_smart", None)
            if switch_fn:
                op = await asyncio.wait_for(
                    switch_fn(app_name, narrate=False),
                    timeout=_APP_TIMEOUT_S,
                )
                return SwitchResult(
                    success=op.result.value in ("success", "already_focused",
                                                 "switched_space", "launched_app"),
                    result_type=op.result.value,
                    app_name=op.app_name,
                    from_space=op.from_space,
                    to_space=op.to_space,
                    space_changed=op.from_space != op.to_space,
                    duration_ms=op.execution_time_ms,
                )
        except (ImportError, asyncio.TimeoutError, Exception) as exc:
            logger.debug("[tool:apps] switch_to_app_smart unavailable: %s", exc)
            continue

    success = await open_app(app_name)
    return SwitchResult(
        success=success,
        result_type="launched_app" if success else "failed",
        app_name=app_name,
    )


async def find_app_window(app_name: str) -> Dict[str, Any]:
    """Find which macOS Space(s) an application's windows are on.

    Checks the spatial context to locate the app across all Spaces.

    Args:
        app_name: Application name to search for (case-insensitive).

    Returns:
        Dict with:
          - "found": bool
          - "app_name": str
          - "spaces": list of Space IDs where the app has windows
          - "is_focused": bool (whether the app currently has focus)
          - "current_space": int (the active Space ID)

    Use when:
        The Architect needs to know if an app is running and where its
        windows are before deciding whether to switch Spaces.
    """
    ctx = await get_spatial_context()
    name_lower = app_name.lower()

    for registered_name, spaces in ctx.app_locations.items():
        if name_lower in registered_name.lower():
            return {
                "found": True,
                "app_name": registered_name,
                "spaces": spaces,
                "is_focused": ctx.focused_app.lower() == name_lower,
                "current_space": ctx.current_space,
            }

    return {
        "found": False,
        "app_name": app_name,
        "spaces": [],
        "is_focused": False,
        "current_space": ctx.current_space,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _check_app_filesystem(app_name: str) -> Optional[AppInfo]:
    """Check standard macOS app directories for an installed app."""
    import pathlib

    search_dirs = [
        pathlib.Path("/Applications"),
        pathlib.Path("/System/Applications"),
        pathlib.Path.home() / "Applications",
    ]
    name_lower = app_name.lower()

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        try:
            for entry in search_dir.iterdir():
                if entry.suffix == ".app" and name_lower in entry.stem.lower():
                    return AppInfo(name=entry.stem, path=str(entry))
        except PermissionError:
            continue

    return None


async def _basic_spatial_context() -> SpatialContext:
    """Minimal spatial context from System Events (no Yabai required)."""
    focused_app = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to get name of '
            'first process whose frontmost is true',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode == 0:
            focused_app = stdout.decode(errors="replace").strip()
    except Exception:
        pass

    return SpatialContext(focused_app=focused_app)
