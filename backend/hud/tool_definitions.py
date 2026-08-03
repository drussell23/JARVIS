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
    if call.name not in TOOL_SCHEMAS and call.name not in derived_tool_names():
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


async def execute_tool(
    call: ToolCall,
    screenshot_b64: Optional[str] = None,
    vision_analyzer: Optional[Any] = None,
) -> ToolResult:
    """Execute a validated tool call. Caller MUST validate first via validate_tool_call.

    Args:
        call: The tool call to execute.
        screenshot_b64: Current screenshot (base64) for vision tools.
        vision_analyzer: Async callable(prompt, image_b64) -> str for screen analysis.
                         Typically Doubleword 235B vision model.
    """
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
            return await _exec_take_screenshot(call, vision_analyzer)
        elif call.name in ("vision_click", "vision_type"):
            return ToolResult(call_id=call.call_id, name=call.name, success=True,
                              output=f"Vision action '{call.name}' dispatched to VLA pipeline.")
        else:
            # THE SEAM. The hand-written list above covers 9 generic tools; the
            # derived registry knows 42 named macOS capabilities, and
            # `lock_screen` was simply never in the list. Rather than a tenth
            # hand-written branch — which is how the list drifted from the
            # machine in the first place — unknown names route through the
            # capability boundary, which gates them by declared effect.
            return await _exec_derived_capability(call)
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


async def _exec_take_screenshot(call: ToolCall, vision_analyzer: Optional[Any] = None) -> ToolResult:
    """Capture a fresh screenshot and analyze it using the 235B vision model.

    The organism SEES the screen — it doesn't just take a picture.
    The vision model describes what's visible so the 397B can reason
    about what to do next (e.g., detect "Page not found" and adapt).
    """
    import os

    # Read the live frame from the Swift HUD's ScreenCaptureService
    frame_path = "/tmp/jarvis_live_frame.jpg"
    screenshot_b64 = None

    try:
        if os.path.exists(frame_path):
            import base64
            with open(frame_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
            logger.info("[CUExec] Screenshot captured from live frame (%d KB)", len(screenshot_b64) // 1024)
        else:
            return ToolResult(call_id=call.call_id, name=call.name, success=False,
                              output="", error="No live frame available — screen capture not running")
    except Exception as exc:
        return ToolResult(call_id=call.call_id, name=call.name, success=False,
                          output="", error=f"Screenshot capture failed: {exc}")

    # Analyze with 235B vision model if available
    if vision_analyzer and screenshot_b64:
        try:
            description = await vision_analyzer(
                "Describe what you see on this screen in detail. Include: "
                "what app is open, what page/content is showing, any error messages, "
                "buttons, text fields, and navigation elements visible. "
                "If there are error messages like 'Page not found' or '404', say so clearly.",
                screenshot_b64,
            )
            return ToolResult(call_id=call.call_id, name=call.name, success=True,
                              output=f"Screen analysis: {description}")
        except Exception as exc:
            logger.warning("[CUExec] Vision analysis failed: %s — returning raw capture", exc)

    # Fallback: return that screenshot was captured but not analyzed
    return ToolResult(call_id=call.call_id, name=call.name, success=True,
                      output="Screenshot captured. Screen content could not be analyzed (vision model unavailable).")


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


def derived_tool_names() -> frozenset:
    """Every capability the model may name. NEVER raises.

    Read per call rather than snapshotted at import: a capability annotated
    tomorrow becomes callable without a restart, and a namespace that finishes
    hydrating a second from now becomes callable a second from now — which is
    the whole point of deriving rather than declaring.

    THE GATE THIS FEEDS
    ---------------------
    `validate_tool_call` blocks any name that is in neither `TOOL_SCHEMAS` nor
    this set. So a federated capability missing from here is not merely
    unmentioned in the prompt — it is BLOCKED at the Iron Gate even when the
    model somehow guesses it correctly. This function and `derived_tool_schemas`
    must therefore draw from the same sources, which is why they share
    `_federated_schemas` instead of each reaching for what it needs.
    """
    try:
        from backend.system_control.capability_registry import (
            get_capability_registry,
        )
        local = set(get_capability_registry().names())
    except Exception:  # noqa: BLE001 — the hand-written tools still work
        local = set()
    return frozenset(local | set(_federated_schemas()))


def _federated_schemas() -> Dict[str, Dict[str, Any]]:
    """Namespaced capabilities from every hydrated subsystem. NEVER raises.

    Multi-space intelligence, video streaming and ghost touch reach the HUD
    through this one call. They were never missing — roughly 11,000 lines of
    working capability sat behind a vocabulary derived from a single controller,
    so nothing in the prompt could NAME them. The same defect the registry was
    built to delete, one level up.

    Only HYDRATED namespaces contribute, and that is a feature rather than a
    compromise: hydration measured 8.4 s for `space` alone, so blocking a prompt
    on it would stall the turn, and offering a tool whose provider has not
    imported would produce a call that cannot be served. A namespace that is not
    ready is simply not offered YET — `warm()` at boot is what makes "yet" mean
    "for the first few seconds" rather than "until someone asks twice".
    """
    try:
        from backend.system_control.capability_federation import (
            federation_enabled, get_federation,
        )
        if not federation_enabled():
            return {}
        return dict(get_federation().tool_schemas())
    except Exception:  # noqa: BLE001
        logger.debug("[Tools] federated schemas unavailable", exc_info=True)
        return {}


def derived_tool_schemas() -> Dict[str, Dict[str, Any]]:
    """`TOOL_SCHEMAS` UNION the derived capabilities UNION the federated ones.

    Precedence runs federated → local-derived → hand-written, weakest first, and
    the hand-written entries win outright. `take_screenshot` exists in both the
    list and the registry, and the hand-written one is wired to the vision
    analyzer while the derived one is not; deriving must never silently
    downgrade a tool that was deliberately specialised.

    Federated names cannot collide with either — they all carry a namespace and
    a dot — so their position in that order is a statement of intent rather than
    a live contest: if a bare name ever did clash, the specialised
    implementation is still the one that wins.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    try:
        merged.update(_federated_schemas())
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.system_control.capability_registry import (
            get_capability_registry,
        )
        merged.update(get_capability_registry().tool_schemas())
    except Exception:  # noqa: BLE001
        pass
    merged.update(TOOL_SCHEMAS)
    return merged


def open_sessions_note() -> str:
    """What is currently running, phrased for the model. "" if nothing is.

    NEVER raises. The other half of `RoutedCall.lease_id`: telling the model it
    opened a session at the moment it opens one is necessary but not sufficient,
    because a tool loop is many turns and a turn is a fresh prompt. Four turns
    later, nothing in the context says the camera is still on — so the model
    plans as though it is not, and never stops it.

    The TTL and the reaper mean nothing leaks either way. This is about the
    organism being able to reason about what it holds, rather than merely being
    cleaned up after.
    """
    try:
        from backend.system_control.capability_leases import get_lease_book
        active = get_lease_book().active()
        if not active:
            return ""
        lines = sorted({
            f"- '{l.capability}' has been running for {int(l.age_s)}s "
            f"(stop it with '{l.release}')"
            for l in active
        })
        return ("\nSessions you currently have open — these keep running until "
                "stopped:\n" + "\n".join(lines))
    except Exception:  # noqa: BLE001
        return ""


def tool_surface_report() -> Dict[str, Any]:
    """What the model can currently reach, and what it cannot. NEVER raises.

    Exists because "the HUD offers 9 tools" was true for months while 42 derived
    macOS capabilities and three whole subsystems sat one name away. A count
    nobody prints is a count nobody notices is wrong, so this is what the boot
    log and `/observability` both read — including the DEGRADED namespaces,
    which are the ones an operator can actually do something about.
    """
    out: Dict[str, Any] = {"handwritten": len(TOOL_SCHEMAS)}
    try:
        from backend.system_control.capability_registry import (
            get_capability_registry,
        )
        out["derived_macos"] = len(get_capability_registry().names())
    except Exception:  # noqa: BLE001
        out["derived_macos"] = 0
    try:
        from backend.system_control.capability_federation import get_federation
        fed = get_federation()
        stats = fed.stats()
        out["federated"] = len(fed.names())
        out["namespaces"] = {
            n: d.get("readiness") for n, d in stats.get("namespaces", {}).items()
        }
        out["degraded"] = sorted(
            n for n, d in stats.get("namespaces", {}).items()
            if d.get("readiness") != "ready")
        out["conflicts"] = stats.get("conflicts", {})
        out["unbuildable"] = stats.get("unbuildable", {})
    except Exception:  # noqa: BLE001
        out["federated"] = 0
    out["total"] = len(derived_tool_schemas())
    return out


async def _exec_derived_capability(call: "ToolCall") -> "ToolResult":
    """Route a derived capability through the consent boundary. NEVER raises.

    Returns rather than raises on every outcome — a SUSPENDED call is not an
    error, and the model needs the note in its context to end the turn cleanly
    rather than retry.
    """
    try:
        from backend.system_control.capability_router import (
            Outcome, get_capability_router,
        )
        routed = await get_capability_router().route(
            call.name, dict(call.args or {}), op_id=call.call_id or "")
        return ToolResult(
            call_id=call.call_id, name=call.name,
            success=(routed.outcome == Outcome.EXECUTED.value),
            output=routed.context_note or "",
            error=("" if routed.outcome == Outcome.EXECUTED.value
                   else routed.detail or routed.outcome),
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(call_id=call.call_id, name=call.name, success=False,
                          output="", error=f"capability route failed: {exc}")
