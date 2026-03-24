"""
Atomic accessibility tools -- find UI elements by description, not coordinates.

These tools provide the Executor context with macOS Accessibility API
(AXUIElement) integration.  Instead of guessing pixel coordinates from
a screenshot, these tools query the OS-level accessibility tree to find
the exact position of named UI elements.

Delegates to the existing AccessibilityResolver which implements a
6-step fallback chain: AX exact title -> AX fuzzy -> AX role -> AX
placeholder -> AppleScript UI query -> None.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_RESOLVE_TIMEOUT_S = float(os.environ.get("TOOL_AX_RESOLVE_TIMEOUT_S", "5.0"))

_resolver = None


@dataclass(frozen=True)
class UIElement:
    """A resolved macOS UI element with precise coordinates.

    Attributes:
        x: Center X coordinate (logical screen pixels).
        y: Center Y coordinate (logical screen pixels).
        width: Element width in pixels.
        height: Element height in pixels.
        description: The search description that matched this element.
        app_name: The application that owns this element.
    """
    x: int
    y: int
    width: int
    height: int
    description: str
    app_name: str


async def find_ui_element(
    description: str,
    app_name: str,
    role: Optional[str] = None,
) -> Optional[UIElement]:
    """Find a UI element by its description using the macOS Accessibility API.

    Searches the accessibility tree of the specified application for an
    element matching the description.  Returns the element's center
    coordinates, which can be passed directly to input.click().

    This is more reliable than vision-based coordinate guessing for
    native macOS apps, because the OS knows exactly where each element is.

    Args:
        description: Human-readable description of the element to find.
            Examples: "Send button", "Search field", "Zach's conversation",
            "New Message", "Close", "Submit".
        app_name: Name of the application to search in.
            Examples: "WhatsApp", "Safari", "Terminal", "Mail".
        role: Optional AX role filter.  Narrows the search to elements
            of a specific type.  Common values: "AXButton", "AXTextField",
            "AXStaticText", "AXLink", "AXMenuItem".

    Returns:
        UIElement with center coordinates and dimensions.
        None if the element could not be found (app not running,
        description too vague, or Accessibility permissions not granted).

    Use when:
        The Executor needs to click or interact with a specific UI element
        in a native macOS app, and pixel-coordinate guessing from a
        screenshot would be unreliable (small buttons, dense UIs).
    """
    resolver = await _get_resolver()
    if resolver is None:
        return None

    try:
        result = await asyncio.wait_for(
            resolver.resolve(
                description=description,
                app_name=app_name,
                role=role,
            ),
            timeout=_RESOLVE_TIMEOUT_S,
        )

        if result is None:
            logger.info("[tool:ax] Element not found: '%s' in %s", description, app_name)
            return None

        element = UIElement(
            x=result["x"],
            y=result["y"],
            width=result.get("width", 0),
            height=result.get("height", 0),
            description=description,
            app_name=app_name,
        )
        logger.info(
            "[tool:ax] Found '%s' at (%d, %d) [%dx%d] in %s",
            description, element.x, element.y,
            element.width, element.height, app_name,
        )
        return element

    except asyncio.TimeoutError:
        logger.warning("[tool:ax] Resolve timed out for '%s' in %s", description, app_name)
        return None
    except Exception as exc:
        logger.error("[tool:ax] Resolve error: %s", exc)
        return None


async def find_and_click(
    description: str,
    app_name: str,
    role: Optional[str] = None,
) -> bool:
    """Find a UI element by description and click it.

    Combines find_ui_element() with input.click() in a single call.
    Uses Accessibility API for precise element location, then CGEvent
    for the click.

    Args:
        description: Human-readable description of the element to click.
        app_name: Application name.
        role: Optional AX role filter.

    Returns:
        True if the element was found and clicked.
        False if the element was not found or the click failed.

    Use when:
        The Executor knows the element name but not its coordinates.
        This is the preferred way to interact with native macOS apps
        when the element has an accessibility label.
    """
    element = await find_ui_element(description, app_name, role)
    if element is None:
        return False

    from backend.core_contexts.tools.input import click
    result = await click(element.x, element.y)
    return result.success


async def list_ui_elements(
    app_name: str,
    window_index: int = 0,
) -> list:
    """List visible UI elements in an application window.

    Walks the accessibility tree and returns a list of elements with
    their roles, titles, and descriptions.  Useful for discovering
    what elements are available before interacting.

    Args:
        app_name: Application name to inspect.
        window_index: Which window to inspect (0 = first/main window).

    Returns:
        List of dicts with: role, title, description.
        Empty list if the app is not running or AX is unavailable.

    Use when:
        The Architect needs to discover what UI elements are available
        in an app before planning an interaction sequence.
    """
    safe_name = app_name.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{safe_name}"\n'
        f'    set _els to every UI element of window {window_index + 1}\n'
        f'    set _result to {{}}\n'
        f'    repeat with _e in _els\n'
        f'      try\n'
        f'        set _role to role of _e\n'
        f'        set _title to name of _e\n'
        f'        set _desc to description of _e\n'
        f'        set end of _result to _role & "|" & _title & "|" & _desc\n'
        f'      end try\n'
        f'    end repeat\n'
        f'    return _result as text\n'
        f'  end tell\n'
        f'end tell\n'
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

        elements = []
        for item in raw.split(", "):
            parts = item.split("|")
            if len(parts) >= 3:
                elements.append({
                    "role": parts[0].strip(),
                    "title": parts[1].strip(),
                    "description": parts[2].strip(),
                })
        return elements

    except Exception as exc:
        logger.error("[tool:ax] list_ui_elements error: %s", exc)
        return []


async def _get_resolver():
    """Get or create the AccessibilityResolver singleton."""
    global _resolver

    if _resolver is not None:
        return _resolver

    for import_path in ("backend.neural_mesh.agents.accessibility_resolver",
                        "neural_mesh.agents.accessibility_resolver"):
        try:
            import importlib
            mod = importlib.import_module(import_path)
            get_fn = getattr(mod, "get_accessibility_resolver", None)
            if get_fn:
                _resolver = get_fn()
                logger.info("[tool:ax] AccessibilityResolver initialized")
                return _resolver
        except (ImportError, Exception) as exc:
            logger.debug("[tool:ax] %s: %s", import_path, exc)
            continue

    logger.warning("[tool:ax] AccessibilityResolver not available")
    return None
