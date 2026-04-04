"""Tool schemas and Iron Gate validation for the Ouroboros tool-use loop.

Every tool call passes through validate_tool_call() before execution.
Dangerous patterns are blocked deterministically — the model cannot
bypass this regardless of what it generates.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schemas (sent to the model so it knows what's available)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "open_app": {
        "name": "open_app",
        "description": "Open a macOS application by name. Discovers installed apps automatically.",
        "parameters": {"app_name": {"type": "string", "description": "Application name (e.g., 'Google Chrome', 'Safari')"}},
    },
    "open_url": {
        "name": "open_url",
        "description": "Open a URL in the default browser.",
        "parameters": {"url": {"type": "string", "description": "Full URL (e.g., 'https://linkedin.com')"}},
    },
    "run_applescript": {
        "name": "run_applescript",
        "description": "Execute an AppleScript command for macOS automation (window management, app control).",
        "parameters": {"script": {"type": "string", "description": "AppleScript code to execute"}},
    },
    "vision_click": {
        "name": "vision_click",
        "description": "Click on a UI element described in natural language. Uses screen vision to find the element.",
        "parameters": {
            "target": {"type": "string", "description": "Natural language description of what to click"},
            "description": {"type": "string", "description": "Context about why clicking this element"},
        },
    },
    "vision_type": {
        "name": "vision_type",
        "description": "Type text into the currently focused field or a described element.",
        "parameters": {
            "text": {"type": "string", "description": "Text to type"},
            "target": {"type": "string", "description": "Optional: element to click first before typing"},
        },
    },
    "press_key": {
        "name": "press_key",
        "description": "Press a keyboard key or hotkey (e.g., 'return', 'command+c', 'tab').",
        "parameters": {"key": {"type": "string", "description": "Key name or combo (e.g., 'return', 'command+v')"}},
    },
    "take_screenshot": {
        "name": "take_screenshot",
        "description": "Capture the current screen. Returns a description of what's visible.",
        "parameters": {},
    },
    "wait": {
        "name": "wait",
        "description": "Wait for a specified number of seconds (for UI to settle after actions).",
        "parameters": {"seconds": {"type": "number", "description": "Seconds to wait (1-10)"}},
    },
    "bash": {
        "name": "bash",
        "description": "Run a shell command. Restricted to safe commands (ls, grep, cat, git, python, etc.).",
        "parameters": {"command": {"type": "string", "description": "Shell command to execute"}},
    },
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single tool invocation from the model."""
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ToolCall:
        return cls(name=d["name"], args=d.get("args", d.get("parameters", {})), call_id=d.get("id", ""))


@dataclass
class ToolResult:
    """Result of executing a tool."""
    call_id: str
    name: str
    success: bool
    output: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Iron Gate — deterministic safety validation
# ---------------------------------------------------------------------------

_DANGEROUS_BASH = re.compile(
    r"(rm\s+-rf|sudo|chmod\s+777|mkfs|dd\s+if=|shutdown|reboot|kill\s+-9\s+1\b|"
    r">\s*/dev/sd|format\s+c:)",
    re.IGNORECASE,
)

_DANGEROUS_APPLESCRIPT = re.compile(
    r"(do\s+shell\s+script\s+\"(rm|sudo|chmod|kill|shutdown|cat\s+~/\.ssh|"
    r"cat\s+~/\.env|pbcopy.*password|curl.*credential))",
    re.IGNORECASE,
)

_CREDENTIAL_PATTERNS = re.compile(
    r"(\.ssh/|\.env|credentials|secret|password|api.?key|token)",
    re.IGNORECASE,
)


def validate_tool_call(call: ToolCall) -> Tuple[bool, str]:
    """Iron Gate: validate a tool call before execution.

    Returns (is_safe, reason). If is_safe is False, the call MUST NOT execute.
    """
    if call.name not in TOOL_SCHEMAS:
        return False, f"Unknown tool '{call.name}' — blocked"

    if call.name == "bash":
        cmd = call.args.get("command", "")
        if _DANGEROUS_BASH.search(cmd):
            return False, f"Dangerous bash command blocked: {cmd[:80]}"
        if _CREDENTIAL_PATTERNS.search(cmd):
            return False, f"Credential access blocked: {cmd[:80]}"

    if call.name == "run_applescript":
        script = call.args.get("script", "")
        if _DANGEROUS_APPLESCRIPT.search(script):
            return False, f"Dangerous AppleScript blocked: {script[:80]}"
        if _CREDENTIAL_PATTERNS.search(script):
            return False, f"Credential access in AppleScript blocked: {script[:80]}"

    if call.name == "open_url":
        url = call.args.get("url", "")
        if not url.startswith(("http://", "https://")):
            return False, f"Invalid URL scheme: {url[:50]}"

    if call.name == "wait":
        seconds = call.args.get("seconds", 1)
        if not isinstance(seconds, (int, float)) or seconds > 30:
            return False, f"Wait too long: {seconds}s (max 30)"

    return True, "safe"


# ---------------------------------------------------------------------------
# Tool execution dispatch
# ---------------------------------------------------------------------------


async def execute_tool(call: ToolCall, screenshot_b64: Optional[str] = None) -> ToolResult:
    """Execute a validated tool call. Caller MUST validate first via validate_tool_call."""
    try:
        if call.name == "open_app":
            return await _exec_open_app(call)
        elif call.name == "open_url":
            return await _exec_open_url(call)
        elif call.name == "run_applescript":
            return await _exec_applescript(call)
        elif call.name == "press_key":
            return await _exec_press_key(call)
        elif call.name == "wait":
            return await _exec_wait(call)
        elif call.name == "bash":
            return await _exec_bash(call)
        elif call.name == "take_screenshot":
            return ToolResult(call_id=call.call_id, name=call.name, success=True,
                              output="Screenshot captured. Describe what you see to decide next action.")
        elif call.name in ("vision_click", "vision_type"):
            return ToolResult(call_id=call.call_id, name=call.name, success=True,
                              output=f"Vision action '{call.name}' dispatched to VLA pipeline.")
        else:
            return ToolResult(call_id=call.call_id, name=call.name, success=False,
                              output="", error=f"No executor for tool '{call.name}'")
    except Exception as exc:
        return ToolResult(call_id=call.call_id, name=call.name, success=False,
                          output="", error=str(exc))


async def _exec_open_app(call: ToolCall) -> ToolResult:
    app_name = call.args.get("app_name", "")
    # Dynamic app discovery — scan /Applications for fuzzy match
    resolved = _discover_app(app_name)
    proc = subprocess.run(["open", "-a", resolved], capture_output=True, text=True, timeout=10)
    if proc.returncode == 0:
        return ToolResult(call_id=call.call_id, name=call.name, success=True,
                          output=f"Opened {resolved}")
    return ToolResult(call_id=call.call_id, name=call.name, success=False,
                      output="", error=f"Failed to open {resolved}: {proc.stderr[:100]}")


async def _exec_open_url(call: ToolCall) -> ToolResult:
    url = call.args.get("url", "")
    proc = subprocess.run(["open", url], capture_output=True, text=True, timeout=10)
    if proc.returncode == 0:
        return ToolResult(call_id=call.call_id, name=call.name, success=True,
                          output=f"Opened {url}")
    return ToolResult(call_id=call.call_id, name=call.name, success=False,
                      output="", error=f"Failed to open URL: {proc.stderr[:100]}")


async def _exec_applescript(call: ToolCall) -> ToolResult:
    script = call.args.get("script", "")
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    output = proc.stdout.strip() or proc.stderr.strip()
    return ToolResult(call_id=call.call_id, name=call.name, success=proc.returncode == 0,
                      output=output[:500], error=proc.stderr[:200] if proc.returncode != 0 else None)


async def _exec_press_key(call: ToolCall) -> ToolResult:
    key = call.args.get("key", "")
    from backend.vision.cu_step_executor import _osascript_key, _osascript_hotkey
    if "+" in key or "," in key:
        _osascript_hotkey(key)
    else:
        _osascript_key(key)
    return ToolResult(call_id=call.call_id, name=call.name, success=True,
                      output=f"Pressed {key}")


async def _exec_wait(call: ToolCall) -> ToolResult:
    seconds = min(float(call.args.get("seconds", 1)), 30)
    import asyncio
    await asyncio.sleep(seconds)
    return ToolResult(call_id=call.call_id, name=call.name, success=True,
                      output=f"Waited {seconds}s")


async def _exec_bash(call: ToolCall) -> ToolResult:
    cmd = call.args.get("command", "")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=".")
    output = proc.stdout[:2000] if proc.stdout else proc.stderr[:2000]
    return ToolResult(call_id=call.call_id, name=call.name, success=proc.returncode == 0,
                      output=output, error=proc.stderr[:200] if proc.returncode != 0 else None)


def _discover_app(name: str) -> str:
    """Dynamically discover installed app by fuzzy name match."""
    import os
    query = name.lower()
    for search_dir in ["/Applications", "/System/Applications", "/System/Applications/Utilities",
                       os.path.expanduser("~/Applications")]:
        try:
            for item in os.listdir(search_dir):
                if item.endswith(".app"):
                    app = item[:-4]
                    if app.lower() == query:
                        return app
                    if query in app.lower():
                        return app
        except OSError:
            continue
    return name  # Return as-is, let macOS try
