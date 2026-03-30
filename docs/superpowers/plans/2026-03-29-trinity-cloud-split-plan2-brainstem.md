# Trinity Cloud Split — Plan 2: Mac Thin Client (Brainstem)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a lightweight Python entry point (`brainstem.py`) that connects to the Vercel nervous system via SSE, dispatches actions to local hardware (Ghost Hands, Audio, HUD), and sends voice/text commands signed with HMAC.

**Architecture:** The brainstem is a ~1200-line Python module that sits alongside `unified_supervisor.py` (never modified). It imports existing hardware modules from `backend/` and creates new modules for SSE consumption, HMAC auth, command sending, and action dispatch. Boot target: less than 5 seconds.

**Tech Stack:** Python 3.12, asyncio, aiohttp (SSE streaming), aiofiles, existing backend/ modules (AudioBus, YabaiAwareActuator, StreamingSTTEngine, FramePipeline, JarvisCU)

**Spec:** `docs/superpowers/specs/2026-03-29-trinity-cloud-split-design.md` Section 6

**Depends on:** Plan 1 (Vercel App) - the API routes this client connects to

**Critical Constraint:** `unified_supervisor.py` is NEVER deleted or modified. Shared `backend/` modules must maintain backward compatibility with both entry points.

---

## File Structure

```
brainstem/
  __init__.py
  __main__.py              # python3 -m brainstem
  main.py                  # Entry point + boot sequence
  auth.py                  # HMAC signing + stream token refresh
  config.py                # Environment config dataclass
  sse_consumer.py          # Vercel SSE stream listener
  action_dispatcher.py     # SSE event to local hardware execution
  command_sender.py        # Mac to Vercel POST /api/command
  voice_intake.py          # STT to command_sender bridge
  vision_bridge.py         # 60fps capture + JarvisCU orchestration
  tts.py                   # Lightweight TTS (say + afplay)
  hud.py                   # Terminal HUD (v1 stub)
  tests/
    __init__.py
    test_auth.py
    test_config.py
    test_sse_consumer.py
    test_action_dispatcher.py
    test_command_sender.py
    test_vision_bridge.py
```

**Key design decisions:**

1. TTS uses `say -o tempfile` then `afplay tempfile` (proven safe pattern). Does NOT import from unified_voice_orchestrator to avoid pulling in the 102K-line supervisor chain.

2. STT uses existing `StreamingSTTEngine` from `backend/voice/streaming_stt.py` which loads faster-whisper. Adds ~2s to boot but is already integrated with AudioBus.

3. Ghost Hands imports `YabaiAwareActuator` directly. The `click()` method takes `coordinates: Optional[Tuple[float, float]]`.

4. HUD is a terminal-based stub for v1 (ANSI formatted output). Full transparent overlay is a v2 task.

5. **Vision bypasses Vercel entirely.** The 60fps FramePipeline captures to SHM locally. JarvisCU's 3-layer step executor calls Doubleword VL-235B (sync `/chat/completions`, ~1-3s) and Claude Vision (fallback, ~5-15s) DIRECTLY from the Mac. Adding a Vercel hop would only add latency. Only ad-hoc "what do you see" text commands go through Vercel's intent router.

6. **Vision is on-demand.** The FramePipeline starts when the first `vision_task` action arrives or when `JARVIS_VISION_LOOP_ENABLED=true`. It does NOT run at boot by default — saving ~1GB RAM and CPU when vision isn't needed.

---

## Task 1: Config + Auth Module

9 tests (3 config + 6 auth). Creates the brainstem package, environment config loader, and HMAC signing that matches the TypeScript server's canonical format exactly.

## Task 2: TTS Module

Self-contained TTS using `say -o tempfile` then `afplay tempfile`. No orchestrator dependency.

## Task 3: HUD Stub

Terminal-based HUD with ANSI formatting. Handles token streaming, daemon narration, progress bars, action display.

## Task 4: SSE Consumer

6 tests. Connects to Vercel SSE stream, parses SSE protocol, handles reconnection with exponential backoff, Last-Event-ID replay, and token refresh every 4 minutes.

## Task 5: Action Dispatcher

6 tests. Routes SSE events to local hardware: tokens to HUD, actions to Ghost Hands/file edit/terminal, daemon events to TTS, status to progress bars, complete to cleanup.

## Task 6: Command Sender

3 tests. Signs and sends commands to POST /api/command. Builds payloads with HMAC, handles both SSE and JSON response types.

## Task 7: Voice Intake Bridge

Bridges STT transcription callbacks to the command sender. Lazy STT engine attachment after AudioBus is ready.

## Task 8: Vision Bridge

3 tests. On-demand vision pipeline: lazy-starts FramePipeline (60fps SHM capture) and JarvisCU when `vision_task` actions arrive. Calls Doubleword VL-235B and Claude Vision directly (no Vercel hop). Wired into ActionDispatcher as a new action type.

## Task 9: Main Entry Point

5-phase boot: config/auth, component creation, hardware init (AudioBus + Ghost Hands + STT), vision bridge (lazy, not started), SSE connect + voice intake. Signal handling for graceful shutdown.

## Task 10: Integration Smoke Test

Verifies all modules import without backend dependencies (lazy-imported in main.py) and all 27 tests pass.

---

**Total: 10 tasks, 27 unit tests, ~1400 lines**

**Boot: less than 5 seconds** (config, auth, hardware, SSE connect, voice intake — vision starts on-demand)

**Vision data flow (bypasses Vercel):**
```
Mac FramePipeline (60fps SHM) ──▶ L1 Scene Graph Cache (local, 5ms)
                                          ↓ (miss)
                                  Doubleword VL-235B (direct API, 1-3s)
                                          ↓ (failure)
                                  Claude Vision API (direct API, 5-15s)
                                          ↓
                                  Ghost Hands executes action locally
```

---

## Deferred to Plan 2.1

- macOS transparent overlay HUD (PyObjC/AppKit floating window)
- Automatic context gathering (frontmost app, active file via AppleScript)
- macOS native STT (replace faster-whisper with Apple Speech framework)
- Continuous vision loop (VisionCortex Phase 2 — always-on ambient awareness)
