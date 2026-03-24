"""
Executor Context -- sees the screen, clicks, types, navigates apps.

The Executor is JARVIS's hands and eyes for interacting with the macOS
GUI.  It has access to screen capture, input automation (CGEvent),
app management, browser control, and accessibility resolution.

The Architect dispatches goals to the Executor when the task requires
visual perception or physical UI interaction.  The Executor does NOT
decide what to do -- it executes the plan the Architect provides.

Tool access:
    screen.*          -- capture, compress, dhash, settlement, motion
    input.*           -- click, type, key, scroll, focus management
    apps.*            -- open, activate, switch, find windows
    browser.*         -- navigate, search, extract, fill, click DOM
    accessibility.*   -- find elements by description, AX tree search
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core_contexts.tools import screen, input, apps, accessibility

logger = logging.getLogger(__name__)


@dataclass
class ExecutorResult:
    """Result of an Executor operation.

    Attributes:
        success: Whether the operation completed successfully.
        action_log: Ordered list of actions taken with outcomes.
        final_state: Description of the screen state after execution.
        error: Error message if success is False.
    """
    success: bool
    action_log: List[Dict[str, Any]] = field(default_factory=list)
    final_state: str = ""
    error: str = ""


class Executor:
    """Screen interaction execution context.

    The Executor provides the 397B Architect with a manifest of available
    tools for GUI automation.  The Architect reads this manifest and
    constructs a DAG of tool calls to achieve a goal.

    Usage::

        executor = Executor()
        # The Architect calls individual tools directly:
        frame = await screen.capture_and_compress()
        result = await input.click(500, 300)
        await input.type_text("hello world")
        await input.press_key("return")
    """

    # Tool manifest -- the Architect reads this to know what's available
    TOOLS = {
        "screen.capture_screen": screen.capture_screen,
        "screen.capture_and_compress": screen.capture_and_compress,
        "screen.compute_dhash": screen.compute_dhash,
        "screen.await_pixel_settlement": screen.await_pixel_settlement,
        "screen.detect_motion": screen.detect_motion,
        "input.click": input.click,
        "input.double_click": input.double_click,
        "input.type_text": input.type_text,
        "input.press_key": input.press_key,
        "input.scroll": input.scroll,
        "input.save_focus": input.save_focus,
        "input.restore_focus": input.restore_focus,
        "apps.open_app": apps.open_app,
        "apps.activate_app": apps.activate_app,
        "apps.list_running_apps": apps.list_running_apps,
        "apps.switch_to_app": apps.switch_to_app,
        "apps.find_app_window": apps.find_app_window,
        "apps.get_spatial_context": apps.get_spatial_context,
        "accessibility.find_ui_element": accessibility.find_ui_element,
        "accessibility.find_and_click": accessibility.find_and_click,
        "accessibility.list_ui_elements": accessibility.list_ui_elements,
    }

    @classmethod
    def tool_manifest(cls) -> List[Dict[str, str]]:
        """Return the tool manifest for the Architect to read.

        Each entry has the tool name and its docstring, which the
        397B model uses to decide which tools to call for a given goal.
        """
        manifest = []
        for name, fn in cls.TOOLS.items():
            manifest.append({
                "name": name,
                "description": (fn.__doc__ or "").strip().split("\n")[0],
                "module": name.split(".")[0],
            })
        return manifest

    async def execute_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute a tool by name with the given arguments.

        This is the generic dispatch method that the Architect uses
        to call any Executor tool dynamically.

        Args:
            tool_name: Full tool name (e.g., "input.click").
            **kwargs: Arguments to pass to the tool function.

        Returns:
            The tool's return value.

        Raises:
            KeyError: If the tool name is not in the manifest.
        """
        fn = self.TOOLS.get(tool_name)
        if fn is None:
            raise KeyError(f"Unknown Executor tool: {tool_name}")

        logger.info("[Executor] %s(%s)", tool_name,
                    ", ".join(f"{k}={v!r}" for k, v in list(kwargs.items())[:3]))

        if asyncio.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return fn(**kwargs)
