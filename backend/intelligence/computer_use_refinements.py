"""
Computer Use Refinements for JARVIS - Open Interpreter Inspired
================================================================

Implements refined computer use patterns based on Open Interpreter's
approach to tool execution, streaming, and safety.

Features:
- Frozen ToolResult dataclass pattern for immutable results
- Async streaming tool execution loop
- Safety sandbox with timeouts and exit conditions
- Image filtering for context window management
- Refined prompts for mouse/keyboard control
- Platform-aware system prompts

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import signal
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, AsyncIterator, Callable, Dict, Generic, List, Literal,
    Optional, Protocol, Set, Tuple, Type, TypeVar, Union, cast
)
from uuid import uuid4

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# ============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class ComputerUseConfig:
    """Configuration for computer use refinements."""
    # Safety settings
    max_execution_time_ms: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_MAX_EXEC_TIME_MS", 30000)
    )
    exit_on_corner: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_CU_EXIT_ON_CORNER", True)
    )
    corner_threshold_px: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_CORNER_THRESHOLD", 10)
    )

    # Context management
    max_recent_images: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_MAX_RECENT_IMAGES", 5)
    )
    image_removal_chunk_size: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_IMAGE_CHUNK_SIZE", 5)
    )

    # Execution settings
    default_timeout_ms: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_DEFAULT_TIMEOUT", 120000)
    )
    retry_attempts: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_RETRY_ATTEMPTS", 3)
    )
    retry_delay_ms: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_RETRY_DELAY_MS", 1000)
    )

    # Streaming settings
    stream_chunk_delay_ms: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CU_STREAM_DELAY_MS", 0)
    )


# ============================================================================
# Tool Result Pattern (Frozen Dataclass - from Open Interpreter)
# ============================================================================

@dataclass(frozen=True, kw_only=True)
class ToolResult:
    """
    Immutable result of a tool execution.

    This pattern from Open Interpreter ensures tool results cannot be
    accidentally modified after creation, providing safety guarantees.
    """
    output: Optional[str] = None
    error: Optional[str] = None
    base64_image: Optional[str] = None
    system: Optional[str] = None
    duration_ms: Optional[float] = None
    exit_code: Optional[int] = None

    def __bool__(self) -> bool:
        """Result is truthy if any field has content."""
        return any(getattr(self, f.name) for f in fields(self) if f.name != "duration_ms")

    def __add__(self, other: "ToolResult") -> "ToolResult":
        """Combine two tool results."""
        def combine(field_name: str, concatenate: bool = True) -> Optional[str]:
            self_val = getattr(self, field_name)
            other_val = getattr(other, field_name)
            if self_val and other_val:
                if concatenate:
                    return f"{self_val}\n{other_val}"
                raise ValueError(f"Cannot combine non-concatenatable field: {field_name}")
            return self_val or other_val

        return ToolResult(
            output=combine("output"),
            error=combine("error"),
            base64_image=combine("base64_image", False),
            system=combine("system"),
            duration_ms=(self.duration_ms or 0) + (other.duration_ms or 0),
            exit_code=other.exit_code if other.exit_code is not None else self.exit_code,
        )

    def with_updates(self, **kwargs) -> "ToolResult":
        """Return a new ToolResult with specified fields updated."""
        return replace(self, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def is_success(self) -> bool:
        """Check if execution was successful."""
        return self.error is None and (self.exit_code is None or self.exit_code == 0)


class CLIResult(ToolResult):
    """ToolResult that originated from CLI execution."""
    pass


class ToolFailure(ToolResult):
    """ToolResult representing a failure."""
    pass


class ToolError(Exception):
    """Exception raised when a tool encounters an error."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)


# ============================================================================
# Tool Protocol and Base Implementation
# ============================================================================

class ComputerTool(Protocol):
    """Protocol for computer use tools."""

    @property
    def name(self) -> str:
        """Tool name."""
        ...

    @property
    def description(self) -> str:
        """Tool description."""
        ...

    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given parameters."""
        ...

    def to_params(self) -> Dict[str, Any]:
        """Convert to API parameter format."""
        ...


@dataclass
class BaseComputerTool(ABC):
    """Base class for computer use tools."""
    config: ComputerUseConfig = field(default_factory=ComputerUseConfig)

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description."""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool."""
        pass

    @abstractmethod
    def to_params(self) -> Dict[str, Any]:
        """Convert to API parameters."""
        pass

    async def execute_with_timeout(self, timeout_ms: Optional[int] = None, **kwargs) -> ToolResult:
        """Execute with timeout protection."""
        timeout = (timeout_ms or self.config.default_timeout_ms) / 1000.0
        start_time = time.time()

        try:
            result = await asyncio.wait_for(
                self.execute(**kwargs),
                timeout=timeout
            )
            duration = (time.time() - start_time) * 1000
            return result.with_updates(duration_ms=duration)
        except asyncio.TimeoutError:
            duration = (time.time() - start_time) * 1000
            return ToolFailure(
                error=f"Tool execution timed out after {timeout}s",
                duration_ms=duration,
            )
        except Exception as e:
            duration = (time.time() - start_time) * 1000
            return ToolFailure(
                error=f"Tool execution failed: {str(e)}",
                system=traceback.format_exc(),
                duration_ms=duration,
            )


# ============================================================================
# Tool Collection
# ============================================================================

class ToolCollection:
    """
    Collection of computer use tools.

    Manages tool registration, lookup, and execution.
    """

    def __init__(self, *tools: BaseComputerTool):
        self._tools: Dict[str, BaseComputerTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseComputerTool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseComputerTool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def to_params(self) -> List[Dict[str, Any]]:
        """Get API parameters for all tools."""
        return [tool.to_params() for tool in self._tools.values()]

    async def run(self, name: str, tool_input: Dict[str, Any]) -> ToolResult:
        """Run a tool by name."""
        tool = self._tools.get(name)
        if not tool:
            return ToolFailure(error=f"Unknown tool: {name}")

        try:
            return await tool.execute_with_timeout(**tool_input)
        except Exception as e:
            return ToolFailure(
                error=f"Tool execution error: {str(e)}",
                system=traceback.format_exc(),
            )

    def list_tools(self) -> List[str]:
        """List all registered tool names."""
        return list(self._tools.keys())


# ============================================================================
# Platform-Aware System Prompts
# ============================================================================

def get_system_prompt() -> str:
    """
    Generate platform-aware system prompt for computer use.

    This follows Open Interpreter's pattern of adapting prompts
    based on the operating system.
    """
    current_platform = platform.system()
    current_date = datetime.today().strftime("%A, %B %d, %Y")

    base_prompt = f"""<SYSTEM_CAPABILITY>
* You are JARVIS, an AI assistant with access to a computer running {current_platform} with internet access.
* When using your computer function calls, they take a while to run and send back to you. Where possible/feasible, try to chain multiple of these calls all into one function calls request.
* The current date is {current_date}.
</SYSTEM_CAPABILITY>"""

    # Platform-specific additions
    if current_platform == "Darwin":  # macOS
        base_prompt += """

<IMPORTANT>
* Open applications using Spotlight by using the computer tool to simulate pressing Command+Space, typing the application name, and pressing Enter.
* For keyboard shortcuts on macOS, use Command (⌘) instead of Control.
* System Preferences are accessed via the Apple menu or Spotlight.
</IMPORTANT>"""

    elif current_platform == "Windows":
        base_prompt += """

<IMPORTANT>
* Open applications using the Start menu or by pressing Win+S to search.
* For keyboard shortcuts on Windows, use Control (Ctrl) as the primary modifier.
* System settings are accessed via Settings app or Control Panel.
</IMPORTANT>"""

    elif current_platform == "Linux":
        base_prompt += """

<IMPORTANT>
* Application launching depends on your desktop environment.
* Common launchers include Super key for GNOME, Alt+F2 for many DEs.
* System settings location varies by distribution and desktop environment.
</IMPORTANT>"""

    return base_prompt


# ============================================================================
# Safety Mechanisms
# ============================================================================

class SafetyMonitor:
    """
    Monitor for safety conditions during computer use.

    Implements Open Interpreter's corner-exit pattern and other
    safety mechanisms.
    """

    def __init__(self, config: Optional[ComputerUseConfig] = None):
        self.config = config or ComputerUseConfig()
        self._exit_requested = asyncio.Event()
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None

    async def start_monitoring(self) -> None:
        """Start safety monitoring."""
        if self._monitoring:
            return

        self._monitoring = True
        self._exit_requested.clear()

        if self.config.exit_on_corner:
            self._monitor_task = asyncio.create_task(self._monitor_mouse_corners())

    async def stop_monitoring(self) -> None:
        """Stop safety monitoring."""
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

    async def _monitor_mouse_corners(self) -> None:
        """Monitor for mouse-in-corner exit condition."""
        try:
            import pyautogui
        except ImportError:
            logger.warning("pyautogui not available for corner detection")
            return

        threshold = self.config.corner_threshold_px
        screen_width, screen_height = pyautogui.size()

        while self._monitoring:
            try:
                x, y = pyautogui.position()

                # Check if mouse is in any corner
                in_corner = (
                    (x <= threshold and y <= threshold) or  # Top-left
                    (x <= threshold and y >= screen_height - threshold) or  # Bottom-left
                    (x >= screen_width - threshold and y <= threshold) or  # Top-right
                    (x >= screen_width - threshold and y >= screen_height - threshold)  # Bottom-right
                )

                if in_corner:
                    logger.info("Mouse moved to corner - requesting exit")
                    self._exit_requested.set()
                    break

                await asyncio.sleep(0.1)  # Check every 100ms

            except Exception as e:
                logger.debug(f"Corner detection error: {e}")
                await asyncio.sleep(1.0)

    def should_exit(self) -> bool:
        """Check if exit has been requested."""
        return self._exit_requested.is_set()

    async def wait_for_exit(self, timeout: Optional[float] = None) -> bool:
        """Wait for exit request."""
        try:
            await asyncio.wait_for(self._exit_requested.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# ============================================================================
# Streaming Execution Loop
# ============================================================================

@dataclass
class StreamChunk:
    """A chunk of streaming output."""
    type: Literal["text", "tool_start", "tool_result", "image", "complete", "error"]
    content: Any
    tool_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class ComputerUseLoop:
    """
    Agentic sampling loop for computer use.

    Based on Open Interpreter's pattern of streaming tool execution
    with safety monitoring.
    """

    def __init__(
        self,
        tool_collection: ToolCollection,
        config: Optional[ComputerUseConfig] = None,
    ):
        self.tools = tool_collection
        self.config = config or ComputerUseConfig()
        self.safety = SafetyMonitor(self.config)
        self._messages: List[Dict[str, Any]] = []

    async def execute_stream(
        self,
        initial_prompt: str,
        system_prompt: Optional[str] = None,
        llm_caller: Optional[Callable] = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Execute the computer use loop with streaming output.

        Args:
            initial_prompt: The user's initial request
            system_prompt: Optional custom system prompt
            llm_caller: Async callable that sends messages to LLM

        Yields:
            StreamChunk objects as execution progresses
        """
        # Start safety monitoring
        await self.safety.start_monitoring()

        try:
            # Initialize message history
            self._messages = [
                {"role": "user", "content": initial_prompt}
            ]

            system = system_prompt or get_system_prompt()

            while not self.safety.should_exit():
                # Filter old images to manage context
                self._filter_old_images()

                # Check if we have an LLM caller
                if not llm_caller:
                    yield StreamChunk(type="error", content="No LLM caller provided")
                    break

                # Call LLM
                try:
                    response = await llm_caller(
                        messages=self._messages,
                        system=system,
                        tools=self.tools.to_params(),
                    )
                except Exception as e:
                    yield StreamChunk(type="error", content=str(e))
                    break

                # Process response
                tool_calls = []
                text_content = ""

                for block in response.get("content", []):
                    if block.get("type") == "text":
                        text_content += block.get("text", "")
                        yield StreamChunk(type="text", content=block.get("text", ""))

                    elif block.get("type") == "tool_use":
                        tool_calls.append(block)

                # Add assistant message to history
                self._messages.append({
                    "role": "assistant",
                    "content": response.get("content", []),
                })

                # If no tool calls, we're done
                if not tool_calls:
                    yield StreamChunk(type="complete", content=text_content)
                    break

                # Execute tool calls
                tool_results = []
                for tool_call in tool_calls:
                    tool_name = tool_call.get("name", "")
                    tool_input = tool_call.get("input", {})
                    tool_id = tool_call.get("id", str(uuid4()))

                    yield StreamChunk(
                        type="tool_start",
                        content={"name": tool_name, "input": tool_input},
                        tool_id=tool_id,
                    )

                    # Execute tool
                    result = await self.tools.run(tool_name, tool_input)

                    yield StreamChunk(
                        type="tool_result",
                        content=result.to_dict(),
                        tool_id=tool_id,
                    )

                    # Format for API
                    tool_result = self._format_tool_result(result, tool_id)
                    tool_results.append(tool_result)

                # Add tool results to message history
                self._messages.append({
                    "role": "user",
                    "content": tool_results,
                })

        finally:
            await self.safety.stop_monitoring()

    def _filter_old_images(self) -> None:
        """Filter to keep only recent images in message history."""
        max_images = self.config.max_recent_images
        chunk_size = self.config.image_removal_chunk_size

        if max_images <= 0:
            return

        # Count images
        image_count = 0
        for msg in self._messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "tool_result":
                            sub_content = item.get("content", [])
                            if isinstance(sub_content, list):
                                for sub in sub_content:
                                    if isinstance(sub, dict) and sub.get("type") == "image":
                                        image_count += 1

        # Remove old images if necessary
        images_to_remove = image_count - max_images
        if images_to_remove <= 0:
            return

        # Round down to chunk size for cache efficiency
        images_to_remove = (images_to_remove // chunk_size) * chunk_size

        for msg in self._messages:
            if images_to_remove <= 0:
                break

            content = msg.get("content", [])
            if isinstance(content, list):
                new_content = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        sub_content = item.get("content", [])
                        if isinstance(sub_content, list):
                            new_sub = []
                            for sub in sub_content:
                                if isinstance(sub, dict) and sub.get("type") == "image":
                                    if images_to_remove > 0:
                                        images_to_remove -= 1
                                        continue
                                new_sub.append(sub)
                            item["content"] = new_sub
                    new_content.append(item)
                msg["content"] = new_content

    def _format_tool_result(self, result: ToolResult, tool_id: str) -> Dict[str, Any]:
        """Format a ToolResult for the API."""
        content: List[Dict[str, Any]] = []
        is_error = False

        if result.error:
            is_error = True
            text = result.error
            if result.system:
                text = f"<system>{result.system}</system>\n{text}"
            content = text  # Error is string, not list
        else:
            if result.output:
                text = result.output
                if result.system:
                    text = f"<system>{result.system}</system>\n{text}"
                content.append({"type": "text", "text": text})

            if result.base64_image:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": result.base64_image,
                    },
                })

        return {
            "type": "tool_result",
            "content": content,
            "tool_use_id": tool_id,
            "is_error": is_error,
        }


# ============================================================================
# Concrete Tool Implementations
# ============================================================================

@dataclass
class ScreenshotTool(BaseComputerTool):
    """Tool for taking screenshots."""

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return "Take a screenshot of the current screen"

    async def execute(self, **kwargs) -> ToolResult:
        try:
            import pyautogui
            from io import BytesIO

            # Take screenshot
            screenshot = pyautogui.screenshot()

            # Convert to base64
            buffer = BytesIO()
            screenshot.save(buffer, format="PNG")
            image_data = base64.b64encode(buffer.getvalue()).decode("utf-8")

            return ToolResult(
                output="Screenshot captured successfully",
                base64_image=image_data,
            )
        except ImportError:
            return ToolFailure(error="pyautogui not available")
        except Exception as e:
            return ToolFailure(error=str(e))

    def to_params(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }


@dataclass
class MouseTool(BaseComputerTool):
    """Tool for mouse operations."""

    @property
    def name(self) -> str:
        return "mouse"

    @property
    def description(self) -> str:
        return "Control the mouse - move, click, drag, or scroll"

    async def execute(
        self,
        action: str = "click",
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
        scroll_amount: int = 0,
    ) -> ToolResult:
        try:
            import pyautogui

            if action == "move":
                if x is not None and y is not None:
                    pyautogui.moveTo(x, y)
                    return ToolResult(output=f"Moved mouse to ({x}, {y})")
                return ToolFailure(error="Move requires x and y coordinates")

            elif action == "click":
                if x is not None and y is not None:
                    pyautogui.click(x=x, y=y, button=button, clicks=clicks)
                else:
                    pyautogui.click(button=button, clicks=clicks)
                return ToolResult(output=f"Clicked {button} button {clicks} time(s)")

            elif action == "drag":
                if x is not None and y is not None:
                    pyautogui.drag(x, y, button=button)
                    return ToolResult(output=f"Dragged to ({x}, {y})")
                return ToolFailure(error="Drag requires x and y coordinates")

            elif action == "scroll":
                pyautogui.scroll(scroll_amount)
                return ToolResult(output=f"Scrolled by {scroll_amount}")

            else:
                return ToolFailure(error=f"Unknown action: {action}")

        except ImportError:
            return ToolFailure(error="pyautogui not available")
        except Exception as e:
            return ToolFailure(error=str(e))

    def to_params(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["move", "click", "drag", "scroll"],
                            "description": "The mouse action to perform",
                        },
                        "x": {"type": "integer", "description": "X coordinate"},
                        "y": {"type": "integer", "description": "Y coordinate"},
                        "button": {
                            "type": "string",
                            "enum": ["left", "middle", "right"],
                            "default": "left",
                        },
                        "clicks": {"type": "integer", "default": 1},
                        "scroll_amount": {"type": "integer", "default": 0},
                    },
                    "required": ["action"],
                },
            },
        }


@dataclass
class KeyboardTool(BaseComputerTool):
    """Tool for keyboard operations."""

    @property
    def name(self) -> str:
        return "keyboard"

    @property
    def description(self) -> str:
        return "Control the keyboard - type text or press key combinations"

    async def execute(
        self,
        action: str = "type",
        text: str = "",
        keys: Optional[List[str]] = None,
    ) -> ToolResult:
        try:
            import pyautogui

            if action == "type":
                pyautogui.typewrite(text, interval=0.02)
                return ToolResult(output=f"Typed: {text[:50]}...")

            elif action == "press":
                if keys:
                    pyautogui.hotkey(*keys)
                    return ToolResult(output=f"Pressed: {'+'.join(keys)}")
                return ToolFailure(error="Press requires keys list")

            elif action == "keydown":
                if keys:
                    for key in keys:
                        pyautogui.keyDown(key)
                    return ToolResult(output=f"Held down: {'+'.join(keys)}")
                return ToolFailure(error="Keydown requires keys list")

            elif action == "keyup":
                if keys:
                    for key in keys:
                        pyautogui.keyUp(key)
                    return ToolResult(output=f"Released: {'+'.join(keys)}")
                return ToolFailure(error="Keyup requires keys list")

            else:
                return ToolFailure(error=f"Unknown action: {action}")

        except ImportError:
            return ToolFailure(error="pyautogui not available")
        except Exception as e:
            return ToolFailure(error=str(e))

    def to_params(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["type", "press", "keydown", "keyup"],
                        },
                        "text": {"type": "string", "description": "Text to type"},
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keys to press (e.g., ['command', 'c'])",
                        },
                    },
                    "required": ["action"],
                },
            },
        }


@dataclass
class BashTool(BaseComputerTool):
    """Tool for executing bash commands."""

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Execute a bash command in the terminal"

    async def execute(self, command: str = "", timeout_seconds: int = 60) -> ToolResult:
        try:
            import subprocess

            # Security check - block dangerous commands
            dangerous_patterns = [
                "rm -rf /",
                "rm -rf ~",
                ":(){ :|:& };:",  # Fork bomb
                "dd if=/dev/",
                "> /dev/",
            ]
            for pattern in dangerous_patterns:
                if pattern in command:
                    return ToolFailure(error=f"Blocked dangerous command pattern: {pattern}")

            # Execute command
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                process.kill()
                return ToolFailure(error=f"Command timed out after {timeout_seconds}s")

            output = stdout.decode("utf-8", errors="replace")
            error_output = stderr.decode("utf-8", errors="replace")

            if process.returncode != 0:
                return ToolResult(
                    output=output,
                    error=error_output,
                    exit_code=process.returncode,
                )

            return ToolResult(
                output=output or "Command completed successfully",
                exit_code=0,
            )

        except Exception as e:
            return ToolFailure(error=str(e))

    def to_params(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "default": 60,
                            "description": "Command timeout in seconds",
                        },
                    },
                    "required": ["command"],
                },
            },
        }


# ============================================================================
# Factory Functions
# ============================================================================

def create_default_tool_collection(config: Optional[ComputerUseConfig] = None) -> ToolCollection:
    """Create a tool collection with default tools."""
    cfg = config or ComputerUseConfig()
    return ToolCollection(
        ScreenshotTool(config=cfg),
        MouseTool(config=cfg),
        KeyboardTool(config=cfg),
        BashTool(config=cfg),
    )


def create_computer_use_loop(config: Optional[ComputerUseConfig] = None) -> ComputerUseLoop:
    """Create a computer use loop with default tools."""
    cfg = config or ComputerUseConfig()
    tools = create_default_tool_collection(cfg)
    return ComputerUseLoop(tools, cfg)


# ============================================================================
# Singleton Access
# ============================================================================

_loop_instance: Optional[ComputerUseLoop] = None


def get_computer_use_loop() -> ComputerUseLoop:
    """Get the singleton computer use loop."""
    global _loop_instance
    if _loop_instance is None:
        _loop_instance = create_computer_use_loop()
    return _loop_instance


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Configuration
    "ComputerUseConfig",

    # Tool Results
    "ToolResult",
    "CLIResult",
    "ToolFailure",
    "ToolError",

    # Tool Protocol and Base
    "ComputerTool",
    "BaseComputerTool",
    "ToolCollection",

    # Concrete Tools
    "ScreenshotTool",
    "MouseTool",
    "KeyboardTool",
    "BashTool",

    # Safety
    "SafetyMonitor",

    # Execution Loop
    "StreamChunk",
    "ComputerUseLoop",

    # System Prompts
    "get_system_prompt",

    # Factory Functions
    "create_default_tool_collection",
    "create_computer_use_loop",
    "get_computer_use_loop",
]
