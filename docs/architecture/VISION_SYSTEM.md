# Vision and Screen Automation System

## Overview

The JARVIS vision system enables autonomous screen interaction through a
closed-loop pipeline: capture the screen, reason about what to do next,
execute the action, and verify the result.  Two architectural paths exist:

| Path | Status | Description |
|------|--------|-------------|
| **Path A** | Active | Lean Vision Loop -- Claude Vision API over screencapture |
| **Path B** | Future | On-premise multimodal model on GCP (Qwen2-VL / Qwen3-VL) |

---

## Path A: Lean Vision Loop (Current)

**Source**: `backend/vision/lean_loop.py`

The Lean Vision Loop replaces the earlier 12-hop pipeline with a tight
3-step loop that prioritizes reliability over latency.

### Loop Architecture

```
                   +-------------------+
                   |   User Goal       |
                   | "Open WhatsApp    |
                   |  and message Zach"|
                   +--------+----------+
                            |
              +-------------v--------------+
              |                            |
              |   for turn in 1..MAX_TURNS |
              |                            |
              |   +-------+   +-------+   +-------+
              |   |CAPTURE|-->| THINK |-->|  ACT  |
              |   +-------+   +-------+   +-------+
              |       |            |           |
              |       |    goal_achieved?      |
              |       |    stagnation?         |
              |       |            |           |
              +-------+----+-----------+------+
                            |
                   +--------v----------+
                   | Result            |
                   | success / failure |
                   | action_log[]      |
                   +-------------------+
```

### Step 1: CAPTURE

Captures the current screen state as a JPEG image.

**Primary path**: Reads the latest frame from `FramePipeline` if the
real-time vision loop is active (sub-10ms latency).

**Fallback path**: Spawns `screencapture -x -C <tmpfile>` as an async
subprocess.  The `-x` flag silences the shutter sound; `-C` includes the
cursor.

Post-processing:
- RGBA images are converted to RGB
- Images exceeding `VISION_LEAN_MAX_IMAGE_DIM` (default 1024) are downscaled
- The downscale ratio is stored in `_last_coord_scale` for coordinate mapping
- Output is base64-encoded JPEG at `VISION_LEAN_JPEG_QUALITY` (default 70)

### Step 2: THINK

Sends the screenshot and conversation history to a vision-capable LLM.

**Default model**: `claude-sonnet-4-20250514` (configurable via
`JARVIS_CLAUDE_VISION_MODEL`).

The prompt instructs Claude to return structured JSON:

```json
{
  "reasoning": "The WhatsApp icon is visible in the dock at position...",
  "scene_summary": "macOS desktop with Dock visible at bottom",
  "goal_achieved": false,
  "next_action": {
    "action_type": "click",
    "target": "WhatsApp icon in Dock",
    "coords": [512, 740]
  }
}
```

Supported action types: `click`, `double_click`, `right_click`, `type`,
`key`, `scroll`, `drag`, `wait`.

### Step 3: ACT

Executes the proposed action on screen.  The executor maps image-space
coordinates back to logical screen coordinates by applying the inverse
of `_last_coord_scale` and the Retina display factor.

**Coordinate mapping**:
```
Image coords (from Claude)
  --> multiply by (1 / _last_coord_scale)   [undo JPEG downscale]
  --> result is logical screen pixels        [pyautogui operates here]
```

Action execution uses `pyautogui` for clicks and keyboard input, with
`subprocess` calls for clipboard operations via `pbcopy`/`pbpaste`.

### Loop Termination

The loop terminates when any of these conditions is met:

| Condition | Env Var | Default |
|-----------|---------|---------|
| Goal achieved | (Claude returns `goal_achieved: true`) | -- |
| Max turns exhausted | `VISION_LEAN_MAX_TURNS` | 10 |
| Overall timeout | `VISION_LEAN_TIMEOUT_S` | 180s |
| Stagnation detected | `VISION_LEAN_STAGNATION_WINDOW` | 3 identical actions |
| No action proposed | (Claude returns no `next_action`) | -- |

### Stagnation Detection

The loop tracks the last N actions.  If the same `(action_type, target)`
pair repeats for `_STAGNATION_WINDOW` consecutive turns, the loop aborts
with a stagnation error.  This prevents infinite loops where the model
repeatedly clicks the same element without progress.

### Integration Point

The Lean Vision Loop is invoked by `RuntimeTaskOrchestrator._dispatch_to_vision()`
when a task step is classified as `TaskResolution.VISION_ACTION`.  The
orchestrator's `PredictivePlanningAgent` decomposes user goals into steps,
and steps requiring visual interaction are routed here.

### Smoke Test

**Source**: `backend/vision/test_lean_loop_smoke.py`

Tests each step independently:
1. CAPTURE -- verifies `screencapture` permissions and output
2. THINK -- sends a test screenshot to Claude Vision API
3. ACT -- verifies pyautogui click/type execution
4. FULL -- end-to-end loop with a simple goal

---

## Path B: On-Premise Multimodal (Future)

When a vision-language model is deployed on the GCP VM, the system will
use a tiered routing strategy:

```
Vision Request
     |
     v
+----+----+
| L1 Cache|  Scene graph cache -- sub-1ms for repeated frames
+----+----+
     | cache miss
     v
+----+----+
| L2 GPU  |  Qwen2-VL / Qwen3-VL on NVIDIA L4
+----+----+
     | model unavailable or low confidence
     v
+----+----+
| L3 Cloud|  Claude Vision API (current Path A)
+----+----+
```

The L2 tier eliminates per-call API costs and reduces latency to ~200ms
for on-device inference.  L3 remains as a reliability fallback.

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
  |   +-- Headless DOM manipulation
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
| Chrome / Arc / Firefox | PlaywrightBackend | CDP protocol, headless DOM |
| Terminal / Notes / Finder | AppleScriptBackend | JXA `System Events` |
| Any window (low-level) | CGEventBackend | `CGEventCreateMouseEvent` |

### FocusGuard

Before any action, FocusGuard:
1. Records the currently focused application and window
2. Executes the action on the target window
3. Restores focus to the original application

This ensures the user never experiences unexpected focus switches during
JARVIS background operations.

### Crash Monitoring

Ghost Hands integrates with `BrowserStabilityManager` for Playwright
crash detection.  When a browser crash is detected, the event is recorded
and automatic recovery is attempted.  The integration uses the modular
`backend/core/browser_stability.py` module rather than importing from the
monolithic supervisor.

---

## Frame Pipeline: Real-Time Capture

**Source**: `backend/vision/realtime/frame_pipeline.py`

The Frame Pipeline provides a continuous stream of screen frames for the
real-time vision action loop.

### Components

| Component | Purpose |
|-----------|---------|
| `FrameData` | Dataclass: RGB numpy array + metadata (width, height, timestamp, frame_number, scale_factor) |
| `FramePipeline` | Wraps SCK capture stream with bounded asyncio queue |
| `_dhash()` | Perceptual difference hash for motion detection |
| `_hamming_distance()` | Bit distance between two dhash values |

### Motion Detection

The pipeline uses dhash (difference hash) to detect frame-to-frame changes:

1. Each frame is resized to `(hash_size+1, hash_size)` grayscale
2. Adjacent pixel brightness relationships are encoded as a 64-bit integer
3. Hamming distance between consecutive frames determines motion
4. Frames below `VISION_MOTION_THRESHOLD` (default 0.05) are dropped

This prevents unnecessary processing when the screen is static.

### Environment Tunables

| Variable | Default | Purpose |
|----------|---------|---------|
| `VISION_MOTION_THRESHOLD` | `0.05` | dhash distance threshold for motion |
| `VISION_MOTION_DEBOUNCE_MS` | `0` | Minimum ms between motion events |
| `VISION_FRAME_QUEUE_SIZE` | `10` | Bounded queue size (drops oldest on overflow) |
| `VISION_DHASH_SIZE` | `8` | Hash grid size (8 = 64-bit hash) |

---

## Environment Variables Summary

| Variable | Default | Scope |
|----------|---------|-------|
| `VISION_LEAN_MAX_TURNS` | `10` | Max loop iterations |
| `VISION_LEAN_SETTLE_S` | `0.3` | Post-action UI settle delay |
| `VISION_LEAN_TIMEOUT_S` | `180` | Overall loop timeout |
| `VISION_LEAN_CLAUDE_TIMEOUT_S` | `30` | Per-call Claude API timeout |
| `VISION_LEAN_CAPTURE_TIMEOUT_S` | `5` | Screenshot subprocess timeout |
| `VISION_LEAN_MAX_IMAGE_DIM` | `1024` | Max image dimension before downscale |
| `VISION_LEAN_JPEG_QUALITY` | `70` | JPEG compression quality |
| `JARVIS_CLAUDE_VISION_MODEL` | `claude-sonnet-4-20250514` | Vision LLM model |
| `VISION_LEAN_STAGNATION_WINDOW` | `3` | Consecutive identical actions before abort |
| `VISION_LEAN_TMP_DIR` | `/tmp/claude` | Temporary screenshot directory |
| `VISION_MOTION_THRESHOLD` | `0.05` | Frame pipeline motion threshold |
| `VISION_FRAME_QUEUE_SIZE` | `10` | Frame pipeline queue depth |

---

## File Reference

| File | Purpose |
|------|---------|
| `backend/vision/lean_loop.py` | Lean Vision Loop (Path A) |
| `backend/vision/realtime/frame_pipeline.py` | Frame capture pipeline with motion detection |
| `backend/vision/test_lean_loop_smoke.py` | Smoke test for all vision steps |
| `backend/ghost_hands/background_actuator.py` | Focus-preserving UI actuator |
| `backend/core/runtime_task_orchestrator.py` | Dispatches vision tasks via `_dispatch_to_vision()` |
| `backend/core/browser_stability.py` | Playwright crash monitoring |
