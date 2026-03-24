"""
Atomic input tools -- click, type, key press, scroll via CGEvent/AppleScript.

These tools provide the Executor context with silent, focus-preserving
UI automation.  All actions delegate to the existing BackgroundActuator
(Ghost Hands) which uses CGEvent, AppleScript, and Playwright backends.

ZERO pyautogui.  pyautogui steals focus and moves the visible cursor.
CGEvent posts events directly to the window server without cursor hijack.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ACTION_TIMEOUT_S = float(os.environ.get("TOOL_ACTION_TIMEOUT_S", "5.0"))

# Lazy-initialized BackgroundActuator singleton
_actuator = None
_actuator_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionResult:
    """Outcome of an input action.

    Attributes:
        success: True if the action executed without error.
        backend: Which backend handled it ("cgevent", "applescript", "playwright").
        duration_ms: Execution time in milliseconds.
        focus_preserved: True if the user's active window was not disturbed.
        error: Error message if success is False.
    """
    success: bool
    backend: str
    duration_ms: float
    focus_preserved: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Tool: click
# ---------------------------------------------------------------------------

async def click(x: int, y: int) -> ActionResult:
    """Click at screen coordinates (x, y) using CGEvent.

    Posts a mouse-down + mouse-up event pair directly to the macOS window
    server via CoreGraphics.  The click targets whatever UI element is at
    the specified coordinates.  Focus preservation is handled by FocusGuard.

    Args:
        x: Horizontal screen coordinate (logical pixels, not Retina).
        y: Vertical screen coordinate (logical pixels, not Retina).

    Returns:
        ActionResult with success status and backend used.

    Use when:
        The Executor needs to click a button, link, menu item, text field,
        or any other interactive element at known coordinates.
    """
    return await _execute_ghost_hands("CLICK", coordinates=(x, y))


async def double_click(x: int, y: int) -> ActionResult:
    """Double-click at screen coordinates (x, y).

    Two rapid click events in succession.  Useful for selecting words in
    text fields or opening files in Finder.

    Args:
        x: Horizontal screen coordinate.
        y: Vertical screen coordinate.

    Returns:
        ActionResult with success status.

    Use when:
        The Executor needs to select a word, open a file, or trigger a
        double-click interaction.
    """
    return await _execute_ghost_hands("DOUBLE_CLICK", coordinates=(x, y))


# ---------------------------------------------------------------------------
# Tool: type_text
# ---------------------------------------------------------------------------

async def type_text(text: str, target_coords: Optional[Tuple[int, int]] = None) -> ActionResult:
    """Type text into the currently focused field via clipboard paste.

    Copies the text to the macOS clipboard (pbcopy), then sends Cmd+V via
    CGEvent.  This handles Unicode, special characters, and multi-line text
    reliably.  If target_coords is provided, clicks the field first.

    Args:
        text: The text to type.  Supports Unicode, newlines, special chars.
        target_coords: Optional (x, y) to click before typing (focus the field).

    Returns:
        ActionResult with success status.

    Use when:
        The Executor needs to enter text into a search bar, message field,
        form input, or any text-accepting UI element.
    """
    if target_coords is not None:
        click_result = await click(target_coords[0], target_coords[1])
        if not click_result.success:
            return click_result
        await asyncio.sleep(0.15)

    return await _execute_ghost_hands("TYPE", text=text)


# ---------------------------------------------------------------------------
# Tool: press_key
# ---------------------------------------------------------------------------

async def press_key(key_name: str, modifiers: Optional[List[str]] = None) -> ActionResult:
    """Press a keyboard key by name via CGEvent.

    Sends a key-down + key-up event pair.  Optional modifier keys (command,
    shift, option, control) can be held during the press.

    Args:
        key_name: Key name -- "return", "tab", "escape", "space", "delete",
            "up", "down", "left", "right", "f1"-"f12", or any letter/digit.
        modifiers: Optional list of modifier keys to hold.  Valid values:
            "command", "shift", "option", "control".

    Returns:
        ActionResult with success status.

    Use when:
        The Executor needs to press Enter to send a message, Tab to move
        between fields, Escape to close a dialog, or a keyboard shortcut.
    """
    return await _execute_ghost_hands("KEY", key=key_name, modifiers=modifiers)


# ---------------------------------------------------------------------------
# Tool: scroll
# ---------------------------------------------------------------------------

async def scroll(
    amount: int = -3,
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> ActionResult:
    """Scroll the screen at the current or specified position.

    Positive amount scrolls up, negative scrolls down.  If coordinates are
    provided, moves the cursor there first (useful for scrolling a specific
    panel in a multi-pane layout).

    Args:
        amount: Number of scroll units.  Negative = down, positive = up.
        x: Optional horizontal coordinate to scroll at.
        y: Optional vertical coordinate to scroll at.

    Returns:
        ActionResult with success status.

    Use when:
        The Executor needs to scroll a page, list, or panel to reveal
        content that is not currently visible on screen.
    """
    coords = (x, y) if x is not None and y is not None else None
    return await _execute_ghost_hands("SCROLL", coordinates=coords, text=str(amount))


# ---------------------------------------------------------------------------
# Tool: save_focus / restore_focus
# ---------------------------------------------------------------------------

async def save_focus() -> Dict[str, Any]:
    """Save the currently focused application and window.

    Captures which app has keyboard focus so it can be restored after
    performing background actions.  Uses Quartz CGWindowListCopyWindowInfo.

    Returns:
        Dict with "app_name", "window_id", "pid" of the focused app.
        Empty dict if focus could not be determined.

    Use when:
        The Executor is about to perform multiple actions on a background
        window and needs to restore the user's focus afterward.
    """
    actuator = await _get_actuator()
    if actuator is None:
        return {}

    try:
        return await actuator._focus_guard.save_focus()
    except Exception as exc:
        logger.warning("[tool:input] save_focus error: %s", exc)
        return {}


async def restore_focus() -> bool:
    """Restore keyboard focus to the previously saved application.

    Must be called after save_focus().  Uses AppleScript to activate
    the saved app.

    Returns:
        True if focus was successfully restored.

    Use when:
        The Executor has finished performing actions on a background
        window and needs to return focus to the user's original app.
    """
    actuator = await _get_actuator()
    if actuator is None:
        return False

    try:
        return await actuator._focus_guard.restore_focus()
    except Exception as exc:
        logger.warning("[tool:input] restore_focus error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Internal: delegate to BackgroundActuator
# ---------------------------------------------------------------------------

async def _get_actuator():
    """Get or create the BackgroundActuator singleton."""
    global _actuator

    if _actuator is not None:
        return _actuator

    async with _actuator_lock:
        if _actuator is not None:
            return _actuator

        try:
            for import_path in ("backend.ghost_hands.background_actuator",
                                "ghost_hands.background_actuator"):
                try:
                    import importlib
                    mod = importlib.import_module(import_path)
                    cls = mod.BackgroundActuator
                    _actuator = cls()
                    started = await _actuator.start()
                    if started:
                        logger.info("[tool:input] BackgroundActuator started (%d backends)",
                                   len(_actuator._backends))
                        return _actuator
                    else:
                        logger.warning("[tool:input] BackgroundActuator.start() returned False")
                        _actuator = None
                except ImportError:
                    continue
        except Exception as exc:
            logger.error("[tool:input] BackgroundActuator init failed: %s", exc)
            _actuator = None

    return None


async def _execute_ghost_hands(
    action_type_name: str,
    coordinates: Optional[Tuple[int, int]] = None,
    text: Optional[str] = None,
    key: Optional[str] = None,
    modifiers: Optional[List[str]] = None,
) -> ActionResult:
    """Build an Action and execute via BackgroundActuator."""
    actuator = await _get_actuator()
    if actuator is None:
        return ActionResult(
            success=False, backend="none", duration_ms=0,
            focus_preserved=True, error="BackgroundActuator not available",
        )

    try:
        for import_path in ("backend.ghost_hands.background_actuator",
                            "ghost_hands.background_actuator"):
            try:
                import importlib
                mod = importlib.import_module(import_path)
                Action = mod.Action
                ActionType = mod.ActionType
                GHActionResult = mod.ActionResult
                break
            except ImportError:
                continue
        else:
            return ActionResult(
                success=False, backend="none", duration_ms=0,
                focus_preserved=True, error="Cannot import Action types",
            )

        # Map string to ActionType enum
        type_map = {
            "CLICK": ActionType.CLICK,
            "DOUBLE_CLICK": ActionType.DOUBLE_CLICK,
            "TYPE": ActionType.TYPE,
            "KEY": ActionType.KEY,
            "SCROLL": ActionType.SCROLL,
        }
        action_type = type_map.get(action_type_name)
        if action_type is None:
            return ActionResult(
                success=False, backend="none", duration_ms=0,
                focus_preserved=True, error=f"Unknown action type: {action_type_name}",
            )

        action = Action(
            action_type=action_type,
            coordinates=coordinates,
            text=text,
            key=key,
            modifiers=modifiers,
        )

        report = await asyncio.wait_for(
            actuator.execute(action, preserve_focus=True),
            timeout=_ACTION_TIMEOUT_S,
        )

        return ActionResult(
            success=report.result == GHActionResult.SUCCESS,
            backend=report.backend_used,
            duration_ms=report.duration_ms,
            focus_preserved=report.focus_preserved,
            error=getattr(report, "error", "") or "",
        )

    except asyncio.TimeoutError:
        return ActionResult(
            success=False, backend="timeout", duration_ms=_ACTION_TIMEOUT_S * 1000,
            focus_preserved=True, error=f"Action timed out after {_ACTION_TIMEOUT_S}s",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ActionResult(
            success=False, backend="error", duration_ms=0,
            focus_preserved=True, error=str(exc),
        )
