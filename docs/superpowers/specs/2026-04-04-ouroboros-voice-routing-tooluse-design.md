# Ouroboros Voice Routing + Tool-Use Orchestration — Design Spec

**Date:** 2026-04-04
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Approved
**Manifesto Alignment:** §5 Intelligence-Driven Routing, §6 Neuroplasticity, §1 Boundary Principle

## Overview

Wire voice commands from the Swift HUD through Ouroboros for intelligent routing. Add seamless tool-use orchestration where the model decides what tools to call and loops until done. The brainstem becomes a dumb pipe — Ouroboros classifies, routes, and executes.

**Provider config:** Doubleword primary (J-Prime/GCP disabled), Claude API fallback.

## 1. Voice Command → Ouroboros Intake

### Current Flow (broken)
```
Swift HUD → IPC → brainstem main.py → JarvisCU (VLA) directly
                                       ↑ bypasses Ouroboros entirely
```

### New Flow
```
Swift HUD → IPC → brainstem main.py → VoiceCommandRouter
                                           ↓
                                    Doubleword 35B classifies intent
                                           ↓
                              ┌────────────┼────────────────┐
                              ↓            ↓                ↓
                        AppleScript    VLA Pipeline    Tool-Use Loop
                        Executor       (JarvisCU)     (397B + tools)
                        (deterministic) (vision-based)  (agentic)
```

### VoiceCommandRouter

New class in `backend/hud/voice_command_router.py`.

```python
class VoiceCommandRouter:
    def __init__(self, doubleword: DoublewordProvider) -> None

    async def route(
        self,
        command: str,
        screenshot_b64: Optional[str] = None,
    ) -> CommandResult
```

**Classification via Doubleword 35B** (cheap, fast, ~2s):

The router sends the raw voice command to the 35B model with a structured prompt:

```
Given this voice command, classify the intent and return JSON:
Command: "{command}"

Categories:
- "app_action": open/close/switch apps (e.g., "open chrome", "close Safari")
- "navigation": go to URL/website (e.g., "go to LinkedIn", "search Google for X")
- "vision_action": interact with UI elements visible on screen (e.g., "click the button", "scroll down")
- "composite": multi-step task combining app + navigation + vision (e.g., "open chrome and go to LinkedIn")
- "code_action": modify code/fix bugs/system tasks (e.g., "fix the parser bug")
- "query": answer a question, no action needed (e.g., "what time is it")

Return: {"category": "...", "steps": [...], "needs_vision": true/false, "needs_tools": true/false}
```

**Routing decision (deterministic — code, not LLM):**

| Category | Executor | Model | Vision? |
|----------|----------|-------|---------|
| `app_action` | AppleScriptExecutor | None (deterministic) | No |
| `navigation` | AppleScriptExecutor | None (deterministic) | No |
| `vision_action` | VLAExecutor (JarvisCU) | Claude/Doubleword VL | Yes |
| `composite` | ToolUseOrchestrator | Doubleword 397B | Maybe |
| `code_action` | GovernedLoopService | Doubleword 397B | No |
| `query` | QueryExecutor | Doubleword 35B | No |

**Key principle:** The 35B classifies the intent. The routing table is deterministic code (Tier 0). The 397B only fires for composite/code tasks that need multi-step reasoning.

### CommandResult

```python
@dataclass
class CommandResult:
    success: bool
    category: str
    steps_completed: int
    steps_total: int
    response_text: Optional[str]  # What JARVIS should say back
    error: Optional[str]
```

## 2. Executors

### AppleScriptExecutor

Handles `app_action` and `navigation` deterministically via `osascript`. No LLM needed.

```python
class AppleScriptExecutor:
    async def open_app(self, app_name: str) -> bool
    async def open_url(self, url: str) -> bool
    async def close_app(self, app_name: str) -> bool
    async def activate_app(self, app_name: str) -> bool
```

**App discovery:** Scans `/Applications/`, `~/Applications/`, `/System/Applications/` for fuzzy match (same logic as the Swift resolver we removed, but in Python where Ouroboros can extend it).

**URL inference:** For "go to LinkedIn" → infers `https://linkedin.com`. For "search Google for X" → infers `https://google.com/search?q=X`. Uses the 35B model for ambiguous cases.

### VLAExecutor

Wraps the existing JarvisCU pipeline. Used when the task requires seeing and interacting with screen elements.

```python
class VLAExecutor:
    async def execute(self, goal: str, screenshot_b64: Optional[str]) -> dict
```

Delegates to existing `JarvisCU.run()` with the screenshot from the Swift HUD.

### QueryExecutor

Answers questions without taking action. Uses Doubleword 35B.

```python
class QueryExecutor:
    async def answer(self, question: str, screenshot_b64: Optional[str] = None) -> str
```

### OuroborosExecutor

Routes to the full governance pipeline for code tasks. Creates an `OperationContext` and submits to `GovernedLoopService.submit()`.

```python
class OuroborosExecutor:
    async def execute(self, command: str) -> CommandResult
```

## 3. Tool-Use Orchestration (397B Loop)

For `composite` tasks that need multi-step reasoning, the 397B model gets a set of tools and loops until done.

### ToolUseOrchestrator

```python
class ToolUseOrchestrator:
    async def execute(
        self,
        goal: str,
        screenshot_b64: Optional[str] = None,
    ) -> CommandResult
```

**Flow:**
1. Build system prompt with available tools
2. Send goal + tools to Doubleword 397B via `prompt_only()`
3. Parse response for tool_calls (JSON)
4. Execute each tool call via `ToolExecutor`
5. Append tool results to conversation
6. Re-send to 397B for next step
7. Loop until model returns `{"done": true, "summary": "..."}` or max iterations hit

**Max iterations:** 10
**Max timeout:** 120s
**Fallback:** If Doubleword 397B fails, fallback to Claude API

### Available Tools

```python
TOOLS = [
    {
        "name": "open_app",
        "description": "Open a macOS application by name. Discovers installed apps automatically.",
        "parameters": {"app_name": "string"}
    },
    {
        "name": "open_url",
        "description": "Open a URL in the default browser.",
        "parameters": {"url": "string"}
    },
    {
        "name": "run_applescript",
        "description": "Execute an AppleScript command for macOS automation.",
        "parameters": {"script": "string"}
    },
    {
        "name": "vision_click",
        "description": "Click on a UI element described in natural language. Requires screenshot.",
        "parameters": {"target": "string", "description": "string"}
    },
    {
        "name": "vision_type",
        "description": "Type text into the currently focused field or a described element.",
        "parameters": {"text": "string", "target": "string (optional)"}
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key or hotkey combination.",
        "parameters": {"key": "string"}
    },
    {
        "name": "take_screenshot",
        "description": "Capture the current screen state. Returns base64 image.",
        "parameters": {}
    },
    {
        "name": "wait",
        "description": "Wait for a specified number of seconds (for UI to settle).",
        "parameters": {"seconds": "number"}
    },
    {
        "name": "bash",
        "description": "Run a shell command. Restricted to safe commands.",
        "parameters": {"command": "string"}
    },
]
```

### Iron Gate Validation

Every tool call passes through deterministic safety checks before execution:

- `run_applescript`: blocked if contains `do shell script "rm`, `delete`, or credential access patterns
- `bash`: existing blocklist (rm -rf /, sudo, chmod 777, etc.)
- `open_url`: blocked if URL matches known malicious patterns
- `vision_click`/`vision_type`: no restrictions (user-initiated)

### Example: "Open Chrome and go to my LinkedIn profile"

```
Step 1: 35B classifies → {"category": "composite", "needs_tools": true}
Step 2: 397B receives goal + tools
Step 3: 397B calls open_app("Google Chrome") → success
Step 4: 397B calls wait(2) → waited
Step 5: 397B calls open_url("https://linkedin.com/in/") → opened
Step 6: 397B returns {"done": true, "summary": "Opened Chrome and navigated to LinkedIn."}
```

No vision needed. No screenshot. No VLA. Just deterministic tool calls orchestrated by the 397B.

### Example: "Click the send button in WhatsApp"

```
Step 1: 35B classifies → {"category": "vision_action", "needs_vision": true}
Step 2: Routes to VLAExecutor with screenshot
Step 3: JarvisCU 3-layer cascade finds "send button"
Step 4: CGEvent click at coordinates
Step 5: Returns result
```

### Example: "Search LinkedIn for AI engineers in Oakland"

```
Step 1: 35B classifies → {"category": "composite", "needs_tools": true}
Step 2: 397B receives goal + tools
Step 3: 397B calls open_url("https://linkedin.com/search/results/people/?keywords=AI%20engineers%20Oakland")
Step 4: 397B returns {"done": true, "summary": "Opened LinkedIn search for AI engineers in Oakland."}
```

One tool call. Done.

## 4. Brainstem Integration

### Modified: `backend/main.py`

The `vision_task` action handler changes from:

```python
# OLD: Direct to JarvisCU
cu = JarvisCU()
result = await cu.run(goal, initial_frame)
```

To:

```python
# NEW: Through VoiceCommandRouter → Ouroboros
from backend.hud.voice_command_router import VoiceCommandRouter
router = VoiceCommandRouter(doubleword_provider)
result = await router.route(command=goal, screenshot_b64=screenshot_b64)
```

The router handles classification, executor selection, and tool-use loops internally. The brainstem just passes the raw command and gets a result back.

### Voice Response

The `CommandResult.response_text` is sent back to the Swift HUD via IPC for TTS:

```python
if result.response_text:
    # Send response back through IPC → Swift HUD → TTS
    await send_ipc_response(result.response_text)
```

## 5. New Files

| File | Responsibility |
|------|----------------|
| `backend/hud/voice_command_router.py` | Intent classification + routing |
| `backend/hud/applescript_executor.py` | Deterministic macOS actions via osascript |
| `backend/hud/vla_executor.py` | Wrapper around JarvisCU |
| `backend/hud/query_executor.py` | LLM query answering |
| `backend/hud/tool_use_orchestrator.py` | 397B tool-use loop |
| `backend/hud/ouroboros_executor.py` | Bridge to GovernedLoopService |
| `backend/hud/tool_definitions.py` | Tool schemas + Iron Gate validators |
| `tests/test_voice_command_router.py` | Router classification tests |
| `tests/test_tool_use_orchestrator.py` | Tool loop tests |
| `tests/test_applescript_executor.py` | AppleScript execution tests |

### Modified Files

| File | Change |
|------|--------|
| `backend/main.py` | Replace direct JarvisCU call with VoiceCommandRouter |

## 6. Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_VOICE_ROUTER_MODEL` | `Qwen/Qwen3.5-35B-A3B-FP8` | Model for intent classification |
| `JARVIS_TOOLUSE_MODEL` | `Qwen/Qwen3.5-397B-A17B-FP8` | Model for tool-use orchestration |
| `JARVIS_TOOLUSE_MAX_ITERATIONS` | `10` | Max tool-use loop iterations |
| `JARVIS_TOOLUSE_TIMEOUT_S` | `120` | Max tool-use loop timeout |
| `JARVIS_TOOLUSE_ENABLED` | `true` | Master switch for tool-use |

## 7. Out of Scope (v1)

- MCP server integration (tools are direct function calls, not MCP)
- Tool graduation (ephemeral → persistent)
- Multi-device tool execution (Mac only)
- Vision tool calling the 235B Qwen VL (uses existing JarvisCU cascade)
