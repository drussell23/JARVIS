# Vision and Screen Automation System

## Overview

The JARVIS vision system enables autonomous screen interaction through a
closed-loop pipeline: capture the screen, reason about what to do next,
execute the action, and verify the result.

The system implements a **Reverse-Engineered Computer Use Architecture** —
the same architectural patterns that power Anthropic's Claude Computer Use,
rebuilt as a standalone capability inside JARVIS. This gives JARVIS its own
native computer use infrastructure that works with ANY vision model, not just
Claude's proprietary beta API.

### Design Philosophy (The Symbiotic Way)

The vision system embodies the **Boundary Principle** from the Trinity Manifesto:

- **Deterministic Skeleton**: Screenshot capture, coordinate mapping, action
  execution, token management — these are *known physics*. They do not vary
  by context and must never require model inference.
- **Agentic Intelligence**: Deciding what element to click, whether the goal
  is achieved, and how to recover from failure — these are *novel decisions*
  that require visual reasoning.

The skeleton is shared infrastructure; the intelligence is pluggable.

---

## Architecture: 2-Tier Cascade

```
User: "Open WhatsApp and message Zach saying 'what's up this is jarvis!'"
                                    |
                                    v
                            +-------+--------+
                            |   _loop()      |
                            | Cascade router |
                            +---+--------+---+
                                |        |
                  +-------------+        +-------------+
                  |                                    |
     +------------v-----------+         +--------------v-----------+
     |  TIER 1: CU Native     |         |  TIER 2: Agentic        |
     |  _loop_computer_use()  |         |  _loop_agentic()        |
     |  [LeanVision:CU]       |         |  [LeanVision:AG]        |
     +------------------------+         +--------------------------+
     | Claude drives the loop  |         | JARVIS drives the loop  |
     | computer_20251124 tool  |         | Multi-image prompts     |
     | Multi-turn tool_result  |         | Last 3 screenshots      |
     | Extended thinking       |         | CU action vocabulary    |
     | Zoom for precision      |         | Provider cascade:       |
     | Native coord training   |         |   Claude Vision         |
     +--------+---------------+         |   Doubleword VL-235B    |
              |                         +--------+-----------------+
              |                                  |
              +----------------------------------+
                              |
              +---------------v----------------+
              |   SHARED CU INFRASTRUCTURE     |
              |   (Deterministic Skeleton)      |
              +--------------------------------+
              | _capture_cu_screenshot()       |
              |   PNG @ 1280x800, Retina-aware |
              | _cu_to_screen()               |
              |   Bidirectional coord mapping  |
              | _execute_cu_action()          |
              |   15 action types             |
              | _prune_cu_screenshots()       |
              |   Token budget management     |
              | _map_cu_key()                |
              |   Key name normalization      |
              +--------------------------------+
```

### Fallback Logic

```python
async def _loop(goal):
    # TIER 1: Claude Computer Use native API (best accuracy)
    if CU_ENABLED and ANTHROPIC_API_KEY:
        result = await _loop_computer_use(goal)
        if result is not None:
            return result

    # TIER 2: Reverse-engineered CU architecture (any model)
    result = await _loop_agentic(goal)
    if result is not None:
        return result

    return {"success": False, "result": "All vision paths exhausted"}
```

---

## Tier 1: Claude Computer Use Native API

**Log prefix**: `[LeanVision:CU]`

Uses Anthropic's `computer_20251124` beta tool — the model was specifically
trained to understand screenshots and produce precise coordinates through
this tool interface.

### How It Works

1. JARVIS sends the goal to Claude with the `computer` tool definition
2. Claude responds with `tool_use` blocks requesting actions (screenshot, click, type, etc.)
3. JARVIS executes each action and returns a screenshot as `tool_result`
4. Claude sees the result and decides the next step
5. Loop continues until Claude responds with text only (task complete)

### Key Advantages Over Custom Prompts

| Feature | CU Native | Custom Prompt |
|---------|-----------|---------------|
| Coordinate precision | Specifically trained | General vision |
| Action vocabulary | 15 built-in actions | Must teach in prompt |
| Visual verification | Claude sees every screenshot | Text-only feedback |
| Reasoning | Extended thinking (1024 tokens) | None |
| Zoom | Can inspect regions at full resolution | Not available |
| Conversation history | Full multi-turn with images | Single-shot |

### API Call Structure

```python
response = await client.beta.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    system=CU_SYSTEM_PROMPT,
    tools=[{
        "type": "computer_20251124",
        "name": "computer",
        "display_width_px": 1280,
        "display_height_px": 800,
    }],
    messages=conversation_history,
    betas=["computer-use-2025-11-24"],
    thinking={"type": "enabled", "budget_tokens": 1024},
)
```

### Tool Result Format

After executing each action, JARVIS returns a screenshot:

```python
{
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": [{
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": b64_png,
        },
    }],
}
```

For failed actions, both an error message and screenshot are returned so
Claude can see what happened and adapt.

---

## Tier 2: Agentic Fallback (Reverse-Engineered CU Architecture)

**Log prefix**: `[LeanVision:AG]`

Mirrors Claude's Computer Use loop pattern but works with **any**
vision-capable model. This is the reverse-engineered architecture —
JARVIS's own computer use infrastructure.

### How It Differs From the Old Legacy Loop

| Aspect | Old Legacy (Removed) | Agentic Fallback |
|--------|---------------------|-------------------|
| Visual memory | None (text summary of past actions) | Last 3 screenshots with annotations |
| Resolution | 1024px JPEG 70% quality | 1280x800 PNG (lossless) |
| Actions | 4 types (click, type, key, scroll) | 15 types (shared with CU) |
| Max tokens | 512 (truncation-prone) | 2048 |
| Settle time | 0.3s | 0.5s (matches macOS animations) |
| Coordinate system | Arbitrary image-pixel space | CU-calibrated 1280x800 → logical |
| Provider cascade | Doubleword → Claude → J-Prime | Claude Vision → Doubleword |

### Visual Memory: The Key Innovation

The old legacy loop sent Claude **one screenshot per turn** plus a text
summary like `"Turn 1: click 'WhatsApp' at [634, 780] -> success"`. Claude
couldn't *see* what happened after its actions — it was flying blind.

The agentic fallback includes the **last 3 screenshots** in every prompt,
with annotations:

```
[Screenshot 1 — After: left_click → success]
<image: screen after clicking WhatsApp icon>

[Screenshot 2 — After: type → success]
<image: screen after typing "Zach" in search>

[Screenshot 3 — CURRENT]
<image: current screen state>
```

This lets any vision model:
- **Compare** before/after to verify actions worked
- **Detect** unexpected states (dialogs, errors, focus changes)
- **Course-correct** when a previous action had no effect

### Provider Cascade

The agentic loop tries vision models in order:

1. **Claude Vision** (standard `messages.create()` API, multi-image)
2. **Doubleword VL-235B** (OpenAI-compatible API, multi-image)

Each provider receives the same multi-image content with the CU-compatible
action vocabulary prompt. If one fails, the next is tried automatically.

### Action Vocabulary (CU-Compatible)

Both tiers share the same action vocabulary:

| Action | Params | Description |
|--------|--------|-------------|
| `left_click` | `coordinate: [x, y]` | Single left click |
| `double_click` | `coordinate: [x, y]` | Double click |
| `right_click` | `coordinate: [x, y]` | Right/context click |
| `triple_click` | `coordinate: [x, y]` | Triple click (select line) |
| `middle_click` | `coordinate: [x, y]` | Middle mouse button |
| `type` | `text: "..."` | Type via clipboard paste |
| `key` | `text: "return"` | Single key press |
| `key` | `text: "command+v"` | Key combo (hotkey) |
| `scroll` | `coordinate, scroll_direction, scroll_amount` | Directional scroll |
| `mouse_move` | `coordinate: [x, y]` | Move cursor |
| `left_click_drag` | `start_coordinate, coordinate` | Click and drag |
| `wait` | `duration: N` | Pause N seconds (capped at 5) |
| `hold_key` | `text, duration` | Hold key for duration |
| `left_mouse_down` | `coordinate: [x, y]` | Press and hold |
| `left_mouse_up` | `coordinate: [x, y]` | Release |

---

## Shared CU Infrastructure (Deterministic Skeleton)

All vision tiers share this infrastructure. It handles the known physics —
no model inference required.

### Screenshot Capture

```python
async def _capture_cu_screenshot() -> Optional[str]:
```

- Captures via `screencapture -x -C` (async subprocess)
- Converts RGBA → RGB
- Resizes to `1280x800` using Pillow LANCZOS
- Encodes as **PNG** (lossless — no JPEG artifacts)
- Tracks `_cu_scale` for coordinate mapping
- Returns base64-encoded string

**Future**: Will integrate Ferrari Engine (ScreenCaptureKit) for sub-10ms
frame capture instead of subprocess (see Advanced Features below).

### Coordinate Mapping

```python
def _cu_to_screen(coord) -> (screen_x, screen_y):
```

Claude (or any model) returns coordinates in the **1280x800 image space**.
These must be mapped to actual screen coordinates:

```
CU image coords (1280x800)
  × (logical_screen_w / 1280, logical_screen_h / 800)
  = actual screen coords (e.g., 1440x900)
```

On Retina displays, `screencapture` captures at 2x resolution (2880x1800
for a 1440x900 logical screen). The pipeline:

1. Capture at Retina resolution (2880x1800)
2. Resize to CU display (1280x800) — this is what the model sees
3. Model returns coords in 1280x800 space
4. Scale by `(logical_w / 1280, logical_h / 800)` for pyautogui
5. pyautogui operates in logical pixel space (1440x900)

### Action Execution

```python
async def _execute_cu_action(action, params) -> (success, error_msg):
```

Dispatches to pyautogui-based handlers:

- **Clicks**: `pyautogui.click(x, y, clicks=N, button=B)` with modifier key support
- **Typing**: Clipboard paste via `pbcopy` + `Cmd+V` (handles Unicode/special chars)
- **Keys**: `pyautogui.press()` / `pyautogui.hotkey()` with key name mapping
- **Scrolling**: `pyautogui.scroll()` with direction conversion
- **Dragging**: `pyautogui.moveTo()` + `pyautogui.drag()`
- **Mouse control**: `pyautogui.mouseDown()` / `mouseUp()` for fine-grained control

### Key Name Mapping

Claude's Computer Use outputs key names like `"super"`, `"Return"`, `"ctrl"`.
The `_map_cu_key()` method normalizes these to pyautogui names:

| Claude CU | pyautogui | Notes |
|-----------|-----------|-------|
| `super`, `meta`, `cmd` | `command` | macOS Command key |
| `return`, `enter` | `return` | |
| `ctrl`, `control` | `ctrl` | |
| `alt`, `option` | `alt` | macOS Option key |
| `escape`, `esc` | `escape` | |
| `page_up`, `page_down` | `pageup`, `pagedown` | |

### Token Management (Screenshot Pruning)

Multi-turn conversations with screenshots grow quickly:
- Each 1280x800 PNG ≈ 1,500 tokens
- 20 turns × 1 screenshot/turn = 30,000 tokens in screenshots alone

`_prune_cu_screenshots()` replaces old screenshots with text stubs:

```python
# Before pruning (turn 15):
messages[3]["content"][0]["content"][0] = {"type": "image", "source": {..., "data": "iVBOR..."}}

# After pruning:
messages[3]["content"][0]["content"][0] = {"type": "text", "text": "[earlier screenshot omitted]"}
```

Default: keep last 10 screenshots (`VISION_CU_PRUNE_SCREENSHOTS`).

---

## Ghost Hands: Focus-Preserving Actuator

**Source**: `backend/ghost_hands/background_actuator.py`

Ghost Hands executes UI actions on background windows WITHOUT stealing
focus from the user's active window.

### Architecture

```
BackgroundActuator (Singleton)
  |
  +-- PlaywrightBackend    Browser automation (Chrome, Arc, Firefox)
  |   +-- Headless DOM manipulation via CDP
  |
  +-- AppleScriptBackend   Native app automation (Terminal, Notes, etc.)
  |   +-- JXA scripting engine
  |
  +-- CGEventBackend       Low-level macOS event injection
  |   +-- Quartz CGEvent tap
  |
  +-- FocusGuard           Focus preservation layer
      +-- Saves/restores frontmost app before/after action
```

### Backend Selection

| Target | Backend | Method |
|--------|---------|--------|
| Chrome / Arc / Firefox | PlaywrightBackend | CDP protocol |
| Terminal / Notes / Finder | AppleScriptBackend | JXA System Events |
| Any window (low-level) | CGEventBackend | CGEventCreateMouseEvent |

### Integration with CU Architecture

The CU architecture uses **pyautogui as the primary actuator** (not Ghost
Hands) because the Computer Use API expects foreground interaction — Claude
assumes its actions affect the visible screen. Ghost Hands remains available
for future background-window operations.

---

## Frame Pipeline: Real-Time Capture (Ferrari Engine)

**Source**: `backend/vision/realtime/frame_pipeline.py`

The Frame Pipeline provides continuous screen frames using Apple's
ScreenCaptureKit (SCK) framework — up to 60fps with sub-10ms latency.

### Components

| Component | Purpose |
|-----------|---------|
| `FrameData` | Dataclass: RGB numpy array + metadata |
| `FramePipeline` | Wraps SCK stream with bounded asyncio queue |
| `_dhash()` | Perceptual difference hash for motion detection |
| `_hamming_distance()` | Bit distance between consecutive frames |

### Motion Detection

The pipeline uses dhash to skip static frames:

1. Each frame → resize to `(hash_size+1, hash_size)` grayscale
2. Adjacent pixel brightness → 64-bit integer
3. Hamming distance between consecutive frames
4. Frames below `VISION_MOTION_THRESHOLD` (default 0.05) are dropped

### Integration Status

The Ferrari Engine is **built but not yet wired** into the CU architecture's
capture path. The CU path currently uses `screencapture` subprocess (reliable
but slow — ~200ms per capture vs ~10ms for SCK).

**Wiring plan**: Replace `_capture_cu_screenshot()` with a FramePipeline-backed
capture path that falls back to `screencapture` when SCK is unavailable.

---

## Environment Variables

### Computer Use (CU) — Tier 1

| Variable | Default | Purpose |
|----------|---------|---------|
| `VISION_CU_ENABLED` | `true` | Enable CU native API path |
| `VISION_CU_DISPLAY_W` | `1280` | CU display width (px) |
| `VISION_CU_DISPLAY_H` | `800` | CU display height (px) |
| `VISION_CU_MAX_TOKENS` | `4096` | Max response tokens |
| `VISION_CU_THINKING_BUDGET` | `1024` | Extended thinking budget |
| `VISION_CU_SETTLE_S` | `0.5` | Post-action UI settle delay |
| `VISION_CU_MAX_TURNS` | `20` | Max loop iterations |
| `VISION_CU_PRUNE_SCREENSHOTS` | `10` | Screenshots to keep in history |
| `JARVIS_CU_MODEL` | `claude-sonnet-4-6` | Claude model for CU API |
| `VISION_CU_TOOL_VERSION` | `computer_20251124` | CU tool version |
| `VISION_CU_BETA_FLAG` | `computer-use-2025-11-24` | Beta flag |

### Agentic Fallback — Tier 2

Uses CU env vars above for resolution, settle time, and max turns.
Additionally:

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_CLAUDE_VISION_MODEL` | `claude-sonnet-4-20250514` | Claude model for standard vision |
| `DOUBLEWORD_API_KEY` | (none) | Doubleword API key (enables VL-235B) |
| `DOUBLEWORD_BASE_URL` | `https://api.doubleword.ai/v1` | Doubleword API endpoint |
| `DOUBLEWORD_VISION_MODEL` | `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | Doubleword model |
| `VISION_DOUBLEWORD_TIMEOUT_S` | `30` | Doubleword per-call timeout |

### Legacy (still read but used only by dormant code paths)

| Variable | Default | Purpose |
|----------|---------|---------|
| `VISION_LEAN_MAX_TURNS` | `10` | Legacy max turns |
| `VISION_LEAN_SETTLE_S` | `0.3` | Legacy settle delay |
| `VISION_LEAN_TIMEOUT_S` | `180` | Legacy overall timeout |
| `VISION_LEAN_MAX_IMAGE_DIM` | `1024` | Legacy max image dimension |
| `VISION_LEAN_JPEG_QUALITY` | `70` | Legacy JPEG quality |

### Frame Pipeline

| Variable | Default | Purpose |
|----------|---------|---------|
| `VISION_MOTION_THRESHOLD` | `0.05` | dhash motion threshold |
| `VISION_MOTION_DEBOUNCE_MS` | `0` | Minimum ms between motion events |
| `VISION_FRAME_QUEUE_SIZE` | `10` | Bounded frame queue depth |
| `VISION_DHASH_SIZE` | `8` | Hash grid size |

---

## Advanced Features Roadmap

### 1. Ferrari Engine Integration (Real-Time Frames)

**Status**: Built, not wired

Replace `screencapture` subprocess with ScreenCaptureKit-backed continuous
frame capture. Benefits:
- ~10ms latency vs ~200ms for subprocess
- 60fps continuous monitoring for proactive awareness
- Motion detection skips static frames (saves tokens)
- Purple screen recording indicator means permission is already granted

**Integration path**: `_capture_cu_screenshot()` checks for active
`FramePipeline` first, falls back to subprocess.

### 2. Zoom for Precision (CU Tier 1)

Claude Opus 4.6 supports the `zoom` action — inspects a rectangular
screen region at full resolution. Enable via `enable_zoom: true` in tool
definition. Useful for:
- Reading small text (contact names, labels)
- Precise click targeting on tiny elements
- Verifying text was typed correctly

### 3. J-Prime GCP as Agentic Provider

Add J-Prime (LLaVA/32B on g2-standard-4 GPU) as a third provider in
the agentic cascade. Benefits:
- Zero per-call API costs
- 43-47 tok/s generation
- Runs on JARVIS's own infrastructure

### 4. Action Replay & Learning

Store successful action sequences in ConsciousnessBridge. When a similar
goal is requested:
1. Check if a known action sequence exists
2. If yes, replay it (Tier 0 deterministic fast-path)
3. If no, run the full vision loop (Tier 1/2 agentic path)

This implements the Manifesto's **Ouroboros graduation**: discovered
solutions crystallize into determined code.

### 5. Continuous Screen Awareness

Use FramePipeline + motion detection to maintain a persistent scene graph:
- Track which app is foreground
- Detect notifications and dialogs
- Pre-classify screen state before the user asks for anything
- Enable proactive suggestions ("I notice you have 3 unread WhatsApp messages")

### 6. Multi-Display Support

Extend `_capture_cu_screenshot()` to handle multiple displays:
- Detect active display from cursor position
- Capture the correct display
- Pass `display_number` to CU tool definition

---

## File Reference

| File | Purpose | Status |
|------|---------|--------|
| `backend/vision/lean_loop.py` | Main vision loop (CU + Agentic + shared infra) | **Active** |
| `backend/vision/realtime/frame_pipeline.py` | Ferrari Engine (SCK continuous capture) | Built, wiring needed |
| `backend/vision/realtime/vision_cortex.py` | Higher-level vision orchestrator | Built, dormant |
| `backend/vision/realtime/vision_action_loop.py` | Real-time action execution loop | Built, dormant |
| `backend/vision/realtime/vision_router.py` | L1/L2/L3 tiered routing | Built, dormant |
| `backend/vision/realtime/action_executor.py` | Action dispatch (pre-CU) | Built, dormant |
| `backend/vision/realtime/verification.py` | Post-action pixel-diff verification | Built, dormant |
| `backend/vision/frame_server.py` | Frame serving endpoint | Built, dormant |
| `backend/vision/cg_window_capture.py` | CoreGraphics capture (pre-SCK) | Built, dormant |
| `backend/vision/continuous_screen_analyzer.py` | Continuous analysis pipeline | Built, dormant |
| `backend/ghost_hands/background_actuator.py` | Focus-preserving actuator | **Active** (via lean_loop) |
| `backend/core/runtime_task_orchestrator.py` | Dispatches vision tasks | **Active** |
| `backend/vision/test_lean_loop_smoke.py` | Smoke test | **Active** |

---

## How to Test

### Quick Smoke Test

```bash
# Run the vision smoke test
python3 backend/vision/test_lean_loop_smoke.py
```

### Manual Test

```bash
# Start JARVIS normally, then issue a voice or text command:
# "Open WhatsApp and message Zach saying 'what's up this is jarvis!'"
#
# Watch logs for:
#   [LeanVision:CU] --- Turn 1/20 ---    (Tier 1: CU native)
#   [LeanVision:AG] --- Turn 1/20 ---    (Tier 2: Agentic fallback)
```

### Force Specific Tier

```bash
# Force Tier 2 only (disable CU native):
export VISION_CU_ENABLED=false

# Force higher resolution:
export VISION_CU_DISPLAY_W=1366
export VISION_CU_DISPLAY_H=768

# Force specific model:
export JARVIS_CU_MODEL=claude-opus-4-6
```
