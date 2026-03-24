# Vision Cortex + Silent Actuator Design

**Date**: 2026-03-24
**Status**: Approved (auto-approved per standing instructions)
**Directives**: 1 (Continuous Visual Cortex) and 4 (Silent Actuator)

## Context

Path A (Lean Vision Loop) is confirmed working. The lean loop currently uses:
- `screencapture` subprocess per turn (~2s per capture)
- `pyautogui` for clicks/keystrokes (visible cursor hijack)
- `asyncio.sleep(0.3)` blind waits between actions

This spec upgrades the local Senses and Nervous System for Path B readiness.

## Directive 1: Continuous Visual Cortex

### Problem
Each vision turn spawns a new `screencapture` subprocess (~2s latency). The main process then loads the file, converts RGBA to RGB, compresses to JPEG. This is slow and wasteful.

### Solution: FrameServer + FrameReader

**FrameServer** (`backend/vision/frame_server.py`):
- Runs as a persistent subprocess (spawned by lean loop on first use)
- Imports Quartz (safe in separate process -- avoids 15K+ ObjC class registration in main process)
- Captures screen at ~15fps using `CGWindowListCreateImage`
- Compresses to JPEG (quality 70, max 1024px)
- Atomic writes to `/tmp/claude/latest_frame.jpg` (write .tmp, rename)
- Writes metadata to `/tmp/claude/frame_meta.json` (timestamp, width, height, dhash)

**FrameReader** (integrated into lean_loop.py):
- Reads `/tmp/claude/latest_frame.jpg` -- pure file I/O, <10ms
- Returns (base64_jpeg, width, height, timestamp)
- Falls back to screencapture subprocess if server not running

**PixelDeltaMonitor** (integrated into lean_loop.py):
- After an action, reads two consecutive frames
- Computes dhash difference
- If frames are different, waits 50ms and re-checks (up to 2s max)
- Returns when pixels settle (or timeout)
- Replaces blind `asyncio.sleep(0.3)`

### Frame Server Lifecycle
- Started lazily on first `_capture_screen()` call
- Health checked via metadata file freshness (stale > 2s = restart)
- Killed on lean loop stop or JARVIS shutdown

## Directive 4: Silent Actuator

### Problem
`pyautogui` visibly moves the mouse cursor and steals focus. It also imports Quartz-related modules that are unsafe in threaded contexts.

### Solution: CGEventWorker + SilentActuator

**CGEventWorker** (`backend/ghost_hands/cgevent_worker.py`):
- Runs as a persistent subprocess
- Imports Quartz once at startup
- Reads JSON-line commands from stdin
- Executes via CoreGraphics CGEvent API:
  - `click`: CGEventCreateMouseEvent (down + up at coords)
  - `key`: CGEventCreateKeyboardEvent (keycode mapping)
  - `type`: pbcopy + CGEvent Cmd+V (clipboard paste)
- Writes JSON-line results to stdout
- No visible cursor movement for clicks (CGEvent posts directly)

**SilentActuator** (`backend/ghost_hands/silent_actuator.py`):
- Main-process async interface
- Starts CGEventWorker on first use
- Methods: `click(x, y)`, `key(name)`, `type(text)`, `scroll(amount)`
- Timeout per command (5s default)
- Health check: restart worker if it dies

### Key Mapping
CGEvent needs numeric keycodes, not key names. Standard mapping:
- return=36, tab=48, escape=53, space=49, delete=51
- up=126, down=125, left=123, right=124

## Integration with Lean Loop

`lean_loop.py` gains two new methods:
- `_capture_screen()` -- tries FrameReader first, falls back to subprocess
- `_execute_action()` -- tries SilentActuator first, falls back to pyautogui

Both fallbacks ensure the system works even if the workers fail to start.

## Files to Create/Modify

New:
- `backend/vision/frame_server.py` -- frame capture daemon
- `backend/ghost_hands/cgevent_worker.py` -- CGEvent action daemon
- `backend/ghost_hands/silent_actuator.py` -- async client for cgevent_worker

Modify:
- `backend/vision/lean_loop.py` -- use FrameReader + SilentActuator + PixelDelta
