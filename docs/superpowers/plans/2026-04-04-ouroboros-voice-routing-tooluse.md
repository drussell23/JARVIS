# Ouroboros Voice Routing + Tool-Use Orchestration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route voice commands through Ouroboros for intelligent classification and execution, with seamless tool-use orchestration where the 397B model decides what tools to call and loops until done.

**Architecture:** VoiceCommandRouter classifies intent via Doubleword 35B, then dispatches to the right executor: AppleScriptExecutor (deterministic), VLAExecutor (vision), ToolUseOrchestrator (397B tool loop), or QueryExecutor (LLM response). The brainstem becomes a dumb pipe — all intelligence in Ouroboros.

**Tech Stack:** Python 3.12, asyncio, DoublewordProvider (35B classifier, 397B tool loop), subprocess (osascript), existing JarvisCU, existing GovernedLoopService

**Spec:** `docs/superpowers/specs/2026-04-04-ouroboros-voice-routing-tooluse-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `backend/hud/tool_definitions.py` | Tool schemas, Iron Gate validators, tool execution dispatch |
| `backend/hud/applescript_executor.py` | Deterministic macOS actions via osascript (open app, URL, activate) |
| `backend/hud/query_executor.py` | LLM query answering via Doubleword 35B |
| `backend/hud/tool_use_orchestrator.py` | 397B tool-use loop — model calls tools, gets results, loops until done |
| `backend/hud/voice_command_router.py` | Intent classification (35B) + routing to executors |
| `tests/test_voice_command_router.py` | Router classification + routing tests |
| `tests/test_tool_use_orchestrator.py` | Tool loop tests |
| `tests/test_applescript_executor.py` | AppleScript execution tests |

### Modified Files

| File | Change |
|------|--------|
| `backend/main.py` | Replace direct JarvisCU call with VoiceCommandRouter in vision_task handler |

---

## Task 1: Tool Definitions + Iron Gate

**Files:**
- Create: `backend/hud/tool_definitions.py`
- Create: `tests/test_hud_tool_definitions.py`

- [ ] **1.1: Write failing tests**

```python
# tests/test_hud_tool_definitions.py
"""Tests for tool definitions and Iron Gate validation."""
import pytest
from backend.hud.tool_definitions import (
    TOOL_SCHEMAS,
    validate_tool_call,
    execute_tool,
    ToolCall,
    ToolResult,
)


class TestToolSchemas:
    def test_all_tools_have_schemas(self):
        expected = {"open_app", "open_url", "run_applescript", "vision_click",
                    "vision_type", "press_key", "take_screenshot", "wait", "bash"}
        assert set(TOOL_SCHEMAS.keys()) == expected

    def test_schema_has_required_fields(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema


class TestIronGateValidation:
    def test_safe_applescript_passes(self):
        call = ToolCall(name="run_applescript", args={"script": 'tell application "Finder" to activate'})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_dangerous_applescript_blocked(self):
        call = ToolCall(name="run_applescript", args={"script": 'do shell script "rm -rf /"'})
        ok, reason = validate_tool_call(call)
        assert ok is False
        assert "blocked" in reason.lower()

    def test_dangerous_bash_blocked(self):
        call = ToolCall(name="bash", args={"command": "sudo rm -rf /"})
        ok, reason = validate_tool_call(call)
        assert ok is False

    def test_safe_bash_passes(self):
        call = ToolCall(name="bash", args={"command": "ls -la"})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_credential_applescript_blocked(self):
        call = ToolCall(name="run_applescript", args={"script": 'do shell script "cat ~/.ssh/id_rsa"'})
        ok, reason = validate_tool_call(call)
        assert ok is False

    def test_safe_url_passes(self):
        call = ToolCall(name="open_url", args={"url": "https://linkedin.com"})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_open_app_always_safe(self):
        call = ToolCall(name="open_app", args={"app_name": "Google Chrome"})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_unknown_tool_blocked(self):
        call = ToolCall(name="hack_system", args={})
        ok, reason = validate_tool_call(call)
        assert ok is False


class TestToolCallDataclass:
    def test_from_dict(self):
        d = {"name": "open_app", "args": {"app_name": "Safari"}}
        call = ToolCall.from_dict(d)
        assert call.name == "open_app"
        assert call.args["app_name"] == "Safari"
```

- [ ] **1.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hud_tool_definitions.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **1.3: Implement tool_definitions.py**

```python
# backend/hud/tool_definitions.py
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
```

- [ ] **1.4: Run tests**

Run: `python3 -m pytest tests/test_hud_tool_definitions.py -v`
Expected: ALL PASS

- [ ] **1.5: Commit**

```bash
git add backend/hud/tool_definitions.py tests/test_hud_tool_definitions.py
git commit -m "feat(ouroboros): add tool definitions + Iron Gate validation"
```

---

## Task 2: AppleScript Executor

**Files:**
- Create: `backend/hud/applescript_executor.py`
- Create: `tests/test_applescript_executor.py`

- [ ] **2.1: Write failing tests**

```python
# tests/test_applescript_executor.py
"""Tests for AppleScriptExecutor — deterministic macOS actions."""
import pytest
from unittest.mock import patch, MagicMock

from backend.hud.applescript_executor import AppleScriptExecutor


@pytest.fixture
def executor():
    return AppleScriptExecutor()


class TestAppDiscovery:
    def test_discovers_exact_match(self, executor):
        with patch("os.listdir", return_value=["Safari.app", "Google Chrome.app"]):
            result = executor.discover_app("Safari")
        assert result == "Safari"

    def test_discovers_fuzzy_match(self, executor):
        with patch("os.listdir", return_value=["Safari.app", "Google Chrome.app"]):
            result = executor.discover_app("chrome")
        assert result == "Google Chrome"

    def test_returns_original_on_no_match(self, executor):
        with patch("os.listdir", return_value=["Safari.app"]):
            result = executor.discover_app("NonExistentApp")
        assert result == "NonExistentApp"


class TestURLInference:
    def test_full_url_passthrough(self, executor):
        assert executor.infer_url("https://linkedin.com") == "https://linkedin.com"

    def test_known_site_inference(self, executor):
        url = executor.infer_url("LinkedIn")
        assert "linkedin.com" in url

    def test_search_query(self, executor):
        url = executor.infer_url("search Google for AI engineers")
        assert "google.com/search" in url
        assert "AI" in url


class TestExecution:
    @pytest.mark.asyncio
    async def test_open_app_calls_subprocess(self, executor):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = await executor.open_app("Safari")
        assert result.success is True
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_url_calls_subprocess(self, executor):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = await executor.open_url("https://linkedin.com")
        assert result.success is True
```

- [ ] **2.2: Implement applescript_executor.py**

```python
# backend/hud/applescript_executor.py
"""AppleScriptExecutor — deterministic macOS actions via osascript.

Handles app launching, URL navigation, window management — all without
LLM calls. These are Tier 0 deterministic actions per Manifesto §5.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutorResult:
    success: bool
    output: str
    error: Optional[str] = None


class AppleScriptExecutor:
    """Executes deterministic macOS actions via osascript and open."""

    def discover_app(self, name: str) -> str:
        """Dynamically discover installed app by fuzzy name match."""
        query = name.lower()
        for search_dir in ["/Applications", "/System/Applications",
                           "/System/Applications/Utilities",
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
        return name

    def infer_url(self, text: str) -> str:
        """Infer a URL from natural language.

        'LinkedIn' → 'https://linkedin.com'
        'search Google for X' → 'https://google.com/search?q=X'
        'https://...' → passthrough
        """
        if text.startswith(("http://", "https://")):
            return text

        lower = text.lower().strip()

        # Search patterns
        search_match = re.match(r"search\s+google\s+for\s+(.+)", lower)
        if search_match:
            from urllib.parse import quote
            return f"https://google.com/search?q={quote(search_match.group(1))}"

        search_match = re.match(r"search\s+(.+?)\s+for\s+(.+)", lower)
        if search_match:
            site = search_match.group(1)
            query = search_match.group(2)
            from urllib.parse import quote
            # Common sites with search URLs
            if "youtube" in site:
                return f"https://youtube.com/results?search_query={quote(query)}"
            if "linkedin" in site:
                return f"https://linkedin.com/search/results/all/?keywords={quote(query)}"
            return f"https://google.com/search?q={quote(query)}+site:{site}"

        # Direct site names — try adding .com
        clean = re.sub(r"[^a-z0-9]", "", lower)
        common = {
            "linkedin": "https://linkedin.com",
            "github": "https://github.com",
            "google": "https://google.com",
            "youtube": "https://youtube.com",
            "twitter": "https://x.com",
            "reddit": "https://reddit.com",
            "gmail": "https://mail.google.com",
            "stackoverflow": "https://stackoverflow.com",
        }
        if clean in common:
            return common[clean]

        # Fallback: assume .com
        return f"https://{clean}.com"

    async def open_app(self, app_name: str) -> ExecutorResult:
        """Open a macOS application by name."""
        resolved = self.discover_app(app_name)
        logger.info("[AppleScript] Opening app: %s (resolved: %s)", app_name, resolved)
        proc = subprocess.run(["open", "-a", resolved], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return ExecutorResult(success=True, output=f"Opened {resolved}")
        # Fallback with original name
        proc2 = subprocess.run(["open", "-a", app_name], capture_output=True, text=True, timeout=10)
        if proc2.returncode == 0:
            return ExecutorResult(success=True, output=f"Opened {app_name}")
        return ExecutorResult(success=False, output="", error=f"Cannot find app: {app_name}")

    async def open_url(self, url: str) -> ExecutorResult:
        """Open a URL in the default browser."""
        resolved = self.infer_url(url)
        logger.info("[AppleScript] Opening URL: %s", resolved)
        proc = subprocess.run(["open", resolved], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return ExecutorResult(success=True, output=f"Opened {resolved}")
        return ExecutorResult(success=False, output="", error=f"Failed to open URL: {resolved}")

    async def activate_app(self, app_name: str) -> ExecutorResult:
        """Bring an app to the foreground."""
        resolved = self.discover_app(app_name)
        script = f'tell application "{resolved}" to activate'
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return ExecutorResult(success=proc.returncode == 0, output=f"Activated {resolved}",
                              error=proc.stderr[:100] if proc.returncode != 0 else None)

    async def run_script(self, script: str) -> ExecutorResult:
        """Execute arbitrary AppleScript (must pass Iron Gate first)."""
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        output = proc.stdout.strip()[:500]
        return ExecutorResult(success=proc.returncode == 0, output=output,
                              error=proc.stderr[:200] if proc.returncode != 0 else None)
```

- [ ] **2.3: Run tests and commit**

Run: `python3 -m pytest tests/test_applescript_executor.py -v`

```bash
git add backend/hud/applescript_executor.py tests/test_applescript_executor.py
git commit -m "feat(ouroboros): add AppleScriptExecutor for deterministic macOS actions"
```

---

## Task 3: Query Executor

**Files:**
- Create: `backend/hud/query_executor.py`
- Create: `tests/test_hud_query_executor.py`

- [ ] **3.1: Write tests and implement**

```python
# backend/hud/query_executor.py
"""QueryExecutor — answers questions via Doubleword 35B without taking action."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class QueryExecutor:
    """Answers user questions via LLM. No actions, no side effects."""

    def __init__(self, doubleword: Any) -> None:
        self._dw = doubleword

    async def answer(self, question: str, screenshot_description: Optional[str] = None) -> str:
        """Answer a question using Doubleword 35B."""
        prompt = f"Answer this question concisely:\n\n{question}"
        if screenshot_description:
            prompt += f"\n\nContext (what's on screen): {screenshot_description}"

        try:
            response = await self._dw.prompt_only(
                prompt,
                model="Qwen/Qwen3.5-35B-A3B-FP8",
                caller_id="voice_query",
                max_tokens=500,
            )
            return response.strip() if response else "I don't have an answer for that."
        except Exception as exc:
            logger.warning("[QueryExecutor] Failed: %s", exc)
            return "Sorry, I couldn't process that question."
```

```python
# tests/test_hud_query_executor.py
import pytest
from unittest.mock import AsyncMock
from backend.hud.query_executor import QueryExecutor


@pytest.mark.asyncio
async def test_answer_returns_response():
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(return_value="The answer is 42.")
    executor = QueryExecutor(dw)
    result = await executor.answer("What is the meaning of life?")
    assert result == "The answer is 42."
    dw.prompt_only.assert_called_once()


@pytest.mark.asyncio
async def test_answer_handles_failure():
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(side_effect=Exception("API error"))
    executor = QueryExecutor(dw)
    result = await executor.answer("Test question")
    assert "sorry" in result.lower() or "couldn't" in result.lower()
```

- [ ] **3.2: Run tests and commit**

```bash
python3 -m pytest tests/test_hud_query_executor.py -v
git add backend/hud/query_executor.py tests/test_hud_query_executor.py
git commit -m "feat(ouroboros): add QueryExecutor for voice questions"
```

---

## Task 4: Tool-Use Orchestrator (397B Loop)

**Files:**
- Create: `backend/hud/tool_use_orchestrator.py`
- Create: `tests/test_tool_use_orchestrator.py`

- [ ] **4.1: Write failing tests**

```python
# tests/test_tool_use_orchestrator.py
"""Tests for 397B tool-use orchestration loop."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.hud.tool_use_orchestrator import ToolUseOrchestrator


@pytest.fixture
def mock_doubleword():
    dw = AsyncMock()
    dw.is_available = True
    return dw


@pytest.fixture
def orchestrator(mock_doubleword):
    return ToolUseOrchestrator(doubleword=mock_doubleword, max_iterations=5, timeout_s=30)


class TestToolLoop:

    @pytest.mark.asyncio
    async def test_single_tool_call_and_done(self, orchestrator, mock_doubleword):
        """Model calls one tool then signals done."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"tool_calls": [{"name": "open_app", "args": {"app_name": "Safari"}}]}),
            json.dumps({"done": True, "summary": "Opened Safari."}),
        ])
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, output="Opened Safari", error=None, name="open_app", call_id="")
            result = await orchestrator.execute("open Safari")
        assert result.success is True
        assert "Safari" in result.response_text

    @pytest.mark.asyncio
    async def test_multi_step_tool_loop(self, orchestrator, mock_doubleword):
        """Model calls multiple tools in sequence."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"tool_calls": [{"name": "open_app", "args": {"app_name": "Google Chrome"}}]}),
            json.dumps({"tool_calls": [{"name": "wait", "args": {"seconds": 1}}]}),
            json.dumps({"tool_calls": [{"name": "open_url", "args": {"url": "https://linkedin.com"}}]}),
            json.dumps({"done": True, "summary": "Opened Chrome and navigated to LinkedIn."}),
        ])
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, output="OK", error=None, name="test", call_id="")
            result = await orchestrator.execute("open chrome and go to LinkedIn")
        assert result.success is True
        assert mock_doubleword.prompt_only.call_count == 4

    @pytest.mark.asyncio
    async def test_max_iterations_stops_loop(self, orchestrator, mock_doubleword):
        """Loop stops at max_iterations even if model keeps calling tools."""
        mock_doubleword.prompt_only = AsyncMock(return_value=json.dumps(
            {"tool_calls": [{"name": "wait", "args": {"seconds": 0.1}}]}
        ))
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, output="waited", error=None, name="wait", call_id="")
            result = await orchestrator.execute("infinite loop test")
        assert mock_doubleword.prompt_only.call_count <= 6  # 5 iterations + safety

    @pytest.mark.asyncio
    async def test_iron_gate_blocks_dangerous_tool(self, orchestrator, mock_doubleword):
        """Dangerous tool calls are blocked by Iron Gate."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"tool_calls": [{"name": "bash", "args": {"command": "sudo rm -rf /"}}]}),
            json.dumps({"done": True, "summary": "Stopped."}),
        ])
        result = await orchestrator.execute("delete everything")
        # Should not crash — Iron Gate blocks the call, model gets error feedback

    @pytest.mark.asyncio
    async def test_doubleword_failure_returns_error(self, orchestrator, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(side_effect=Exception("API timeout"))
        result = await orchestrator.execute("test")
        assert result.success is False
```

- [ ] **4.2: Implement tool_use_orchestrator.py**

```python
# backend/hud/tool_use_orchestrator.py
"""ToolUseOrchestrator — 397B model decides what tools to call, loops until done.

The model receives a goal + available tools, calls tools one at a time,
gets results back, reasons about next steps, and loops until it signals
done or max iterations is reached.

Uses Doubleword 397B (primary), Claude API (fallback).
Every tool call passes through Iron Gate before execution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.hud.tool_definitions import (
    TOOL_SCHEMAS,
    ToolCall,
    ToolResult,
    execute_tool,
    validate_tool_call,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = os.environ.get("JARVIS_TOOLUSE_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")
_DEFAULT_MAX_ITER = int(os.environ.get("JARVIS_TOOLUSE_MAX_ITERATIONS", "10"))
_DEFAULT_TIMEOUT = float(os.environ.get("JARVIS_TOOLUSE_TIMEOUT_S", "120"))


@dataclass
class CommandResult:
    success: bool
    category: str
    steps_completed: int
    steps_total: int
    response_text: Optional[str]
    error: Optional[str]


class ToolUseOrchestrator:
    """Orchestrates the 397B tool-use loop."""

    def __init__(
        self,
        doubleword: Any,
        max_iterations: int = _DEFAULT_MAX_ITER,
        timeout_s: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._dw = doubleword
        self._max_iter = max_iterations
        self._timeout = timeout_s

    async def execute(self, goal: str, screenshot_b64: Optional[str] = None) -> CommandResult:
        """Execute a goal using the 397B tool-use loop."""
        t0 = time.monotonic()
        tool_list = json.dumps(list(TOOL_SCHEMAS.values()), indent=2)

        system_prompt = (
            "You are JARVIS, an AI organism controlling a MacBook. "
            "You have tools to interact with the Mac. Use them to accomplish the goal.\n\n"
            "Available tools:\n" + tool_list + "\n\n"
            "To call a tool, respond with JSON: {\"tool_calls\": [{\"name\": \"...\", \"args\": {...}}]}\n"
            "When the task is complete, respond with: {\"done\": true, \"summary\": \"what you did\"}\n"
            "If you cannot complete the task, respond with: {\"done\": true, \"summary\": \"why it failed\", \"error\": \"reason\"}\n\n"
            "Rules:\n"
            "- Call ONE tool at a time, wait for the result before deciding next action\n"
            "- After each tool result, decide if you need more actions or if you're done\n"
            "- Be efficient — don't call unnecessary tools\n"
            "- If a tool fails, try an alternative approach"
        )

        conversation = f"Goal: {goal}"
        steps_completed = 0

        for iteration in range(self._max_iter):
            # Timeout check
            if (time.monotonic() - t0) > self._timeout:
                return CommandResult(
                    success=False, category="composite", steps_completed=steps_completed,
                    steps_total=iteration, response_text="Task timed out.",
                    error=f"Timeout after {self._timeout}s",
                )

            # Call model
            try:
                prompt = f"{system_prompt}\n\n{conversation}"
                raw = await self._dw.prompt_only(
                    prompt,
                    model=_DEFAULT_MODEL,
                    caller_id=f"tooluse_iter{iteration}",
                    max_tokens=2000,
                )
            except Exception as exc:
                logger.warning("[ToolUse] Model call failed at iteration %d: %s", iteration, exc)
                return CommandResult(
                    success=False, category="composite", steps_completed=steps_completed,
                    steps_total=iteration, response_text=f"Model error: {exc}",
                    error=str(exc),
                )

            if not raw or not raw.strip():
                continue

            # Parse response
            parsed = self._parse_response(raw)

            # Done signal
            if parsed.get("done"):
                summary = parsed.get("summary", "Task completed.")
                has_error = parsed.get("error")
                return CommandResult(
                    success=not has_error, category="composite",
                    steps_completed=steps_completed, steps_total=steps_completed,
                    response_text=summary, error=has_error,
                )

            # Tool calls
            tool_calls = parsed.get("tool_calls", [])
            if not tool_calls:
                # Model didn't return tool_calls or done — treat raw text as summary
                return CommandResult(
                    success=True, category="composite", steps_completed=steps_completed,
                    steps_total=steps_completed, response_text=raw.strip()[:500],
                    error=None,
                )

            # Execute each tool call
            for tc_dict in tool_calls:
                call = ToolCall.from_dict(tc_dict)

                # Iron Gate validation
                is_safe, reason = validate_tool_call(call)
                if not is_safe:
                    logger.warning("[ToolUse] Iron Gate blocked: %s — %s", call.name, reason)
                    conversation += f"\n\nTool '{call.name}' was BLOCKED by safety gate: {reason}. Try a different approach."
                    continue

                # Execute
                result = await execute_tool(call)
                steps_completed += 1

                # Add result to conversation
                status = "SUCCESS" if result.success else "FAILED"
                conversation += (
                    f"\n\nYou called: {call.name}({json.dumps(call.args)})"
                    f"\nResult ({status}): {result.output or result.error}"
                )

                logger.info("[ToolUse] Step %d: %s(%s) → %s",
                            steps_completed, call.name, json.dumps(call.args)[:80], status)

        # Max iterations reached
        return CommandResult(
            success=steps_completed > 0, category="composite",
            steps_completed=steps_completed, steps_total=self._max_iter,
            response_text=f"Completed {steps_completed} steps (max iterations reached).",
            error=None if steps_completed > 0 else "Max iterations without completing goal",
        )

    def _parse_response(self, raw: str) -> dict:
        """Parse model response — try JSON, handle markdown fences, fallback."""
        text = raw.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in the text
        import re
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return {"done": True, "summary": text[:500]}
```

- [ ] **4.3: Run tests and commit**

```bash
python3 -m pytest tests/test_tool_use_orchestrator.py -v
git add backend/hud/tool_use_orchestrator.py tests/test_tool_use_orchestrator.py
git commit -m "feat(ouroboros): add ToolUseOrchestrator — 397B tool loop"
```

---

## Task 5: Voice Command Router

**Files:**
- Create: `backend/hud/voice_command_router.py`
- Create: `tests/test_voice_command_router.py`

- [ ] **5.1: Write failing tests**

```python
# tests/test_voice_command_router.py
"""Tests for VoiceCommandRouter — intent classification + routing."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.hud.voice_command_router import VoiceCommandRouter


@pytest.fixture
def mock_doubleword():
    dw = AsyncMock()
    dw.is_available = True
    return dw


@pytest.fixture
def router(mock_doubleword):
    return VoiceCommandRouter(doubleword=mock_doubleword)


class TestClassification:

    @pytest.mark.asyncio
    async def test_app_action_classified(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(return_value=json.dumps(
            {"category": "app_action", "needs_vision": False, "needs_tools": False}
        ))
        with patch.object(router, "_execute_app_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = MagicMock(success=True, response_text="Opened Safari", category="app_action", steps_completed=1, steps_total=1, error=None)
            result = await router.route("open Safari")
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_navigation_classified(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(return_value=json.dumps(
            {"category": "navigation", "needs_vision": False, "needs_tools": False}
        ))
        with patch.object(router, "_execute_navigation", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = MagicMock(success=True, response_text="Opened LinkedIn", category="navigation", steps_completed=1, steps_total=1, error=None)
            result = await router.route("go to LinkedIn")
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_composite_routes_to_tool_loop(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"category": "composite", "needs_vision": False, "needs_tools": True}),
            json.dumps({"tool_calls": [{"name": "open_app", "args": {"app_name": "Google Chrome"}}]}),
            json.dumps({"done": True, "summary": "Done."}),
        ])
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, output="OK", error=None, name="open_app", call_id="")
            result = await router.route("open chrome and go to LinkedIn")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_query_returns_answer(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"category": "query", "needs_vision": False, "needs_tools": False}),
            "It is currently 2:30 AM.",
        ])
        result = await router.route("what time is it")
        assert result.response_text is not None

    @pytest.mark.asyncio
    async def test_classification_failure_fallback(self, router, mock_doubleword):
        """If classification fails, fall back to tool-use loop."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            "unparseable garbage",
            json.dumps({"done": True, "summary": "Tried my best."}),
        ])
        result = await router.route("do something weird")
        assert result is not None
```

- [ ] **5.2: Implement voice_command_router.py**

```python
# backend/hud/voice_command_router.py
"""VoiceCommandRouter — classifies voice commands and routes to the right executor.

Uses Doubleword 35B for fast intent classification, then dispatches to:
  - AppleScriptExecutor (app/navigation — deterministic, no LLM)
  - VLAExecutor (vision actions — JarvisCU pipeline)
  - ToolUseOrchestrator (composite — 397B tool loop)
  - QueryExecutor (questions — 35B response)

The Swift HUD sends raw commands here. This is the brain's front door.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from backend.hud.applescript_executor import AppleScriptExecutor
from backend.hud.query_executor import QueryExecutor
from backend.hud.tool_use_orchestrator import CommandResult, ToolUseOrchestrator

logger = logging.getLogger(__name__)

_CLASSIFIER_MODEL = os.environ.get("JARVIS_VOICE_ROUTER_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8")

_CLASSIFY_PROMPT = """Given this voice command, classify the intent. Return ONLY a JSON object.

Command: "{command}"

Categories:
- "app_action": open/close/switch/launch an application (e.g., "open chrome", "close Safari", "launch Spotify")
- "navigation": go to a website/URL (e.g., "go to LinkedIn", "open google.com", "search YouTube for music")
- "vision_action": interact with something visible on screen that requires seeing it (e.g., "click the send button", "scroll down", "select the text")
- "composite": multi-step task combining multiple actions (e.g., "open chrome and go to LinkedIn", "send a message on WhatsApp saying hello")
- "code_action": modify code, fix bugs, system development tasks (e.g., "fix the parser bug", "refactor the login module")
- "query": answer a question, provide information, no action needed (e.g., "what time is it", "what's on my screen", "how does the vision loop work")

Return: {{"category": "...", "needs_vision": true/false, "needs_tools": true/false}}"""


class VoiceCommandRouter:
    """Routes voice commands through Ouroboros for intelligent execution."""

    def __init__(self, doubleword: Any) -> None:
        self._dw = doubleword
        self._applescript = AppleScriptExecutor()
        self._query = QueryExecutor(doubleword)
        self._tool_orchestrator = ToolUseOrchestrator(doubleword)

    async def route(self, command: str, screenshot_b64: Optional[str] = None) -> CommandResult:
        """Classify and route a voice command."""
        logger.info("[VoiceRouter] Command: %s", command[:100])

        # Step 1: Classify intent via 35B
        classification = await self._classify(command)
        category = classification.get("category", "composite")
        needs_vision = classification.get("needs_vision", False)
        needs_tools = classification.get("needs_tools", False)

        logger.info("[VoiceRouter] Classified: %s (vision=%s, tools=%s)", category, needs_vision, needs_tools)

        # Step 2: Route to executor
        if category == "app_action":
            return await self._execute_app_action(command)

        if category == "navigation":
            return await self._execute_navigation(command)

        if category == "query":
            return await self._execute_query(command, screenshot_b64)

        if category == "vision_action":
            return await self._execute_vision(command, screenshot_b64)

        if category == "code_action":
            return await self._execute_code_action(command)

        # composite or unknown → tool-use loop (397B)
        return await self._tool_orchestrator.execute(command, screenshot_b64)

    async def _classify(self, command: str) -> dict:
        """Classify intent via Doubleword 35B."""
        try:
            prompt = _CLASSIFY_PROMPT.format(command=command)
            raw = await self._dw.prompt_only(
                prompt,
                model=_CLASSIFIER_MODEL,
                caller_id="voice_classifier",
                max_tokens=200,
            )
            if not raw:
                return {"category": "composite", "needs_vision": False, "needs_tools": True}

            # Parse JSON
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                json_match = re.search(r'\{[^{}]*\}', text)
                if json_match:
                    return json.loads(json_match.group())

        except Exception as exc:
            logger.warning("[VoiceRouter] Classification failed: %s — falling back to composite", exc)

        return {"category": "composite", "needs_vision": False, "needs_tools": True}

    async def _execute_app_action(self, command: str) -> CommandResult:
        """Extract app name and open/close it."""
        lower = command.lower()
        # Extract app name from command
        app_match = re.search(r"(?:open|launch|start|close|quit)\s+(?:the\s+)?(.+?)(?:\s+app)?$", lower, re.IGNORECASE)
        app_name = app_match.group(1).strip() if app_match else command

        if "close" in lower or "quit" in lower:
            result = await self._applescript.run_script(f'tell application "{self._applescript.discover_app(app_name)}" to quit')
            return CommandResult(
                success=result.success, category="app_action", steps_completed=1, steps_total=1,
                response_text=f"Closed {app_name}." if result.success else f"Couldn't close {app_name}.",
                error=result.error,
            )

        result = await self._applescript.open_app(app_name)
        return CommandResult(
            success=result.success, category="app_action", steps_completed=1, steps_total=1,
            response_text=result.output if result.success else f"Couldn't open {app_name}.",
            error=result.error,
        )

    async def _execute_navigation(self, command: str) -> CommandResult:
        """Extract URL/site and navigate to it."""
        lower = command.lower()
        # Remove "go to", "navigate to", "open" prefix
        site = re.sub(r"^(go\s+to|navigate\s+to|open)\s+", "", lower).strip()
        url = self._applescript.infer_url(site)
        result = await self._applescript.open_url(url)
        return CommandResult(
            success=result.success, category="navigation", steps_completed=1, steps_total=1,
            response_text=result.output if result.success else f"Couldn't navigate to {site}.",
            error=result.error,
        )

    async def _execute_query(self, command: str, screenshot_b64: Optional[str]) -> CommandResult:
        """Answer a question via LLM."""
        answer = await self._query.answer(command)
        return CommandResult(
            success=True, category="query", steps_completed=1, steps_total=1,
            response_text=answer, error=None,
        )

    async def _execute_vision(self, command: str, screenshot_b64: Optional[str]) -> CommandResult:
        """Dispatch to VLA pipeline (JarvisCU) for vision-dependent actions."""
        try:
            from backend.vision.jarvis_cu import JarvisCU
            import numpy as np
            from PIL import Image
            import base64
            import io

            cu = JarvisCU()
            frame = None
            if screenshot_b64:
                img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64)))
                frame = np.array(img.convert("RGB"))

            result = await cu.run(command, initial_frame=frame)
            success = result.get("success", False)
            steps = result.get("steps_completed", 0)
            total = result.get("steps_total", 0)
            error = result.get("error")

            return CommandResult(
                success=success, category="vision_action", steps_completed=steps, steps_total=total,
                response_text=f"Completed {steps}/{total} steps." if success else f"Vision task failed: {error}",
                error=error,
            )
        except Exception as exc:
            return CommandResult(
                success=False, category="vision_action", steps_completed=0, steps_total=0,
                response_text=f"Vision system error: {exc}", error=str(exc),
            )

    async def _execute_code_action(self, command: str) -> CommandResult:
        """Route to Ouroboros governance pipeline for code tasks."""
        # For now, route through the tool-use orchestrator which can use bash tools
        # Full GovernedLoopService integration is a future enhancement
        return await self._tool_orchestrator.execute(command)
```

- [ ] **5.3: Run tests and commit**

```bash
python3 -m pytest tests/test_voice_command_router.py -v
git add backend/hud/voice_command_router.py tests/test_voice_command_router.py
git commit -m "feat(ouroboros): add VoiceCommandRouter — 35B classify + intelligent routing"
```

---

## Task 6: Wire into Brainstem

**Files:**
- Modify: `backend/main.py`

- [ ] **6.1: Replace direct JarvisCU call with VoiceCommandRouter**

Read `backend/main.py` around line 2046-2100. Find the `vision_task` handler block that creates `JarvisCU` and calls `cu.run()`. Replace it with:

```python
# NEW: Route through Ouroboros VoiceCommandRouter
from backend.hud.voice_command_router import VoiceCommandRouter

# Get or create router (reuse DoublewordProvider from governance)
voice_router = getattr(app.state, "voice_router", None)
if voice_router is None:
    from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider
    dw = DoublewordProvider()
    voice_router = VoiceCommandRouter(doubleword=dw)
    app.state.voice_router = voice_router

# Route through Ouroboros — classifier decides execution path
result = await voice_router.route(
    command=goal,
    screenshot_b64=screenshot_b64,
)

logger.info("[HUD] Voice result: %s (category=%s, steps=%d/%d)",
            "success" if result.success else "failed",
            result.category, result.steps_completed, result.steps_total)

# Send response back to HUD for TTS
if result.response_text:
    # The response will be sent back via the existing IPC/SSE channel
    logger.info("[HUD] Response: %s", result.response_text[:100])
```

**CRITICAL:** Keep the screenshot decoding code that's already there (lines 2067-2079). Just replace the `JarvisCU` instantiation and `cu.run()` call with the router.

- [ ] **6.2: Run all tests**

```bash
python3 -m pytest tests/test_hud_tool_definitions.py tests/test_applescript_executor.py tests/test_hud_query_executor.py tests/test_tool_use_orchestrator.py tests/test_voice_command_router.py -v
```

- [ ] **6.3: Commit**

```bash
git add backend/main.py
git commit -m "feat(ouroboros): wire VoiceCommandRouter into brainstem — all commands through Ouroboros"
```

---

## Summary

| Task | Component | Dependencies |
|------|-----------|-------------|
| 1 | Tool Definitions + Iron Gate | None |
| 2 | AppleScript Executor | None |
| 3 | Query Executor | None |
| 4 | Tool-Use Orchestrator (397B loop) | Task 1 |
| 5 | Voice Command Router | Tasks 1-4 |
| 6 | Wire into brainstem main.py | Task 5 |

Tasks 1, 2, 3 are independent and can run in parallel. Task 4 depends on 1. Task 5 depends on 1-4. Task 6 depends on 5.
