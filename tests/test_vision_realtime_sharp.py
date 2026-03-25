#!/usr/bin/env python3
"""
JARVIS Vision-Language-Action (VLA) Pipeline

Dual-model parallel perception:
  - Doubleword 235B VL: fast structural read (text, numbers, elements)
  - Claude Vision: deep semantic understanding (scene, spatial, context)
  - Apple Vision OCR: local deterministic text extraction (fallback)

Both cloud models fire in parallel on the same frame. Results are fused
into a rich perception that JARVIS narrates with voice.

Usage:
    python3 tests/test_vision_realtime_sharp.py [--duration 60]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import os
import re
import subprocess
import sys
import time
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_root, ".env"), override=True)
    load_dotenv(os.path.join(_root, "backend", ".env"), override=True)
except ImportError:
    pass

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Voice -- serial queue, ONE speaker at a time, never overlapping
# ---------------------------------------------------------------------------

_speech_queue: asyncio.Queue = None  # type: ignore[assignment]
_speech_task: Optional[asyncio.Task] = None


async def _speech_worker() -> None:
    """Drain the speech queue serially. One utterance at a time."""
    while True:
        text, voice = await _speech_queue.get()
        try:
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", voice, "-r", "220", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            pass
        _speech_queue.task_done()


def _ensure_speech_worker() -> None:
    global _speech_queue, _speech_task
    if _speech_queue is None:
        _speech_queue = asyncio.Queue()
    if _speech_task is None or _speech_task.done():
        _speech_task = asyncio.ensure_future(_speech_worker())


async def jarvis_say(text: str, voice: str = "Daniel") -> None:
    """Queue speech and wait for it to finish. Never overlaps."""
    _ensure_speech_worker()
    await _speech_queue.put((text, voice))
    await _speech_queue.join()


def jarvis_say_background(text: str, voice: str = "Daniel") -> None:
    """Queue speech without waiting. Still serial — no overlap."""
    _ensure_speech_worker()
    _speech_queue.put_nowait((text, voice))


# ---------------------------------------------------------------------------
# OCR -- read exactly what's on screen
# ---------------------------------------------------------------------------

async def ocr_read_screen(b64_png: str) -> Dict[str, str]:
    """Read text from screen using Apple Vision Framework.

    Apple Vision is native macOS, ~50ms, 1.00 confidence on clean text,
    handles glow/shadow that Tesseract struggles with.
    Falls back to Tesseract if Apple Vision unavailable.
    """
    import tempfile

    # Write frame to temp file for Apple Vision
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_ocr_frame.png")
    try:
        raw_bytes = base64.b64decode(b64_png)
        with open(tmp, "wb") as f:
            f.write(raw_bytes)

        # Try Apple Vision first
        try:
            from backend.vision.apple_ocr import apple_ocr_read_async
            lines = await apple_ocr_read_async(tmp, min_confidence=0.8)
            if lines:
                return _parse_ocr_lines([l["text"] for l in lines])
        except Exception:
            pass

        # Fallback: Tesseract
        try:
            import pytesseract
            img = Image.open(tmp)
            w, h = img.size
            hud = img.crop((0, 0, int(w * 0.4), int(h * 0.22)))
            hud = hud.resize((hud.width * 3, hud.height * 3), Image.Resampling.NEAREST)
            text = pytesseract.image_to_string(hud, config="--psm 6")
            return _parse_ocr_lines(text.strip().split("\n"))
        except Exception:
            pass

    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return {}


def _parse_ocr_lines(lines: list) -> Dict[str, str]:
    """Parse bounce counter values from OCR text lines.

    Apple Vision may split 'Horizontal' and 'Bounces: 33' into separate
    lines. We join all lines into one blob then extract with regex.
    """
    blob = " ".join(str(l).strip() for l in lines)
    result = {}

    m = re.search(r"[Hh]orizontal\s*[Bb]ounces?:?\s*(\d+)", blob)
    if m:
        result["horizontal"] = m.group(1)

    m = re.search(r"[Vv]ertical\s*[Bb]ounces?:?\s*(\d+)", blob)
    if m:
        result["vertical"] = m.group(1)

    m = re.search(r"[Tt]otal\s*[Bb]ounces?:?\s*(\d+)", blob)
    if m:
        result["total"] = m.group(1)

    m = re.search(r"[Ss]peed:?\s*(\d+)", blob)
    if m:
        result["speed"] = m.group(1)

    return result


# ---------------------------------------------------------------------------
# Main loop — VLA Pipeline (Vision + Language + Action)
# ---------------------------------------------------------------------------

async def main(duration_s: int = 60):
    print("\n" + "=" * 70)
    print("  JARVIS VLA Pipeline — Dual-Model Parallel Perception")
    print("  OCR (local) + 235B (structural) + Claude (semantic)")
    print("=" * 70)

    # Open bouncing ball and bring it to front
    html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vision_smoke_test_bounce.html")
    if os.path.exists(html):
        subprocess.Popen(["open", html], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(2.0)

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "Google Chrome" to activate',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(1.0)
    except Exception:
        pass

    await jarvis_say(
        "JARVIS Vision Language Action pipeline online. "
        "Dual model perception activated."
    )

    # Re-focus Chrome
    try:
        refocus = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "Google Chrome" to activate',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await refocus.wait()
        await asyncio.sleep(0.5)
    except Exception:
        pass

    # Start capture
    from backend.vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop.get_instance()
    # Force fresh frame_server for main-display-only capture
    loop._frame_server_proc = None
    loop._frame_server_ready = False
    await loop._ensure_frame_server()
    if loop._frame_server_ready:
        await asyncio.sleep(2.0)
    print("  Capture: ONLINE")

    # Initialize cloud clients
    claude_client = None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            import anthropic
            claude_client = anthropic.AsyncAnthropic(api_key=api_key)
            print("  Claude Vision: ONLINE")
        except ImportError:
            print("  Claude Vision: OFFLINE (no anthropic)")

    dw_key = os.environ.get("DOUBLEWORD_API_KEY", "")
    print(f"  Doubleword 235B: {'ONLINE' if dw_key else 'OFFLINE (no key)'}")

    _tel_dir = os.environ.get(
        "VISION_TELEMETRY_DIR", "/tmp/claude/vision_telemetry",
    )
    _latest = os.path.join(_tel_dir, "vision_last_perception.png")

    print(f"\n  Running {duration_s}s...\n  " + "-" * 60)

    t_start = time.monotonic()
    n_cycles = 0
    last_ocr_vals: Dict[str, str] = {}
    # Background tasks for cloud models (non-blocking)
    claude_task: Optional[asyncio.Task] = None
    dw_task: Optional[asyncio.Task] = None
    last_vla_time = 0.0

    while (time.monotonic() - t_start) < duration_s:
        t_cycle = time.monotonic()

        # ---- CAPTURE ----
        b64 = await loop._capture_cu_screenshot()
        if b64 is None:
            await asyncio.sleep(0.5)
            continue

        n_cycles += 1

        # ---- LAYER 1: Local OCR (deterministic skeleton, every cycle) ----
        t_ocr = time.monotonic()
        ocr_vals = await ocr_read_screen(b64)
        ocr_ms = (time.monotonic() - t_ocr) * 1000

        if ocr_vals and ocr_vals != last_ocr_vals:
            h = ocr_vals.get("horizontal", "?")
            v = ocr_vals.get("vertical", "?")
            t = ocr_vals.get("total", "?")
            _verify = f" | verify: {_latest}" if os.path.exists(_latest) else ""
            print(f"  [OCR] ({ocr_ms:.0f}ms) H:{h} V:{v} T:{t}{_verify}")
            jarvis_say_background(f"{t} total bounces. {h} horizontal, {v} vertical.")
            last_ocr_vals = ocr_vals.copy()

        # ---- LAYER 2+3: Cloud VLA (parallel, every ~8s) ----
        elapsed = time.monotonic() - t_start
        should_vla = (elapsed - last_vla_time) >= 8.0 and n_cycles >= 2

        # Collect finished cloud results
        if claude_task and claude_task.done():
            try:
                claude_result = claude_task.result()
                if claude_result:
                    print(f"  [CLAUDE] {claude_result}")
                    jarvis_say_background(claude_result)
            except Exception:
                pass
            claude_task = None

        if dw_task and dw_task.done():
            try:
                dw_result = dw_task.result()
                if dw_result:
                    # Truncate for display
                    short = dw_result.replace("\n", " ").strip()[:200]
                    print(f"  [235B]   {short}")
                    jarvis_say_background(
                        f"235B structural analysis: {short[:120]}"
                    )
            except Exception:
                pass
            dw_task = None

        # Fire new parallel perception if enough time passed
        if should_vla and claude_task is None and dw_task is None:
            last_vla_time = elapsed
            print(f"  [VLA] Firing dual-model perception (T+{elapsed:.0f}s)...")
            if claude_client:
                claude_task = asyncio.create_task(
                    _claude_vision(claude_client, b64)
                )
            if dw_key:
                dw_task = asyncio.create_task(
                    _doubleword_vision(b64)
                )

        await asyncio.sleep(0.3)

    # Cleanup
    for task in [claude_task, dw_task]:
        if task and not task.done():
            task.cancel()

    total = time.monotonic() - t_start
    print(f"\n  " + "-" * 60)
    print(f"  Cycles: {n_cycles} | Duration: {total:.1f}s")

    await jarvis_say(
        f"VLA pipeline complete. {n_cycles} perception cycles in "
        f"{int(total)} seconds."
    )

    print("=" * 70 + "\n")
    if loop._frame_server_proc and loop._frame_server_proc.returncode is None:
        loop._frame_server_proc.terminate()


# ---------------------------------------------------------------------------
# VLA Perception Engines (run in parallel on the same frame)
# ---------------------------------------------------------------------------

async def _claude_vision(client, b64: str) -> Optional[str]:
    """Claude Vision: deep semantic scene understanding."""
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=os.environ.get("JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=80,
                system=(
                    "You are JARVIS reporting to Derek. "
                    "Describe what you see in 1-2 sentences: the scene, "
                    "where the ball is, its direction, and any notable details. "
                    "Be specific about position (quadrant, edge proximity) and motion."
                ),
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": "Describe this screen."},
                ]}],
            ),
            timeout=10,
        )
        for block in resp.content:
            if hasattr(block, "text"):
                return block.text.strip()
    except Exception:
        pass
    return None


async def _doubleword_vision(b64: str) -> Optional[str]:
    """Doubleword 235B VL: fast structural read — text, numbers, layout."""
    dw_key = os.environ.get("DOUBLEWORD_API_KEY", "")
    dw_base = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
    dw_model = os.environ.get(
        "DOUBLEWORD_VISION_MODEL", "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
    )
    if not dw_key:
        return None
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{dw_base}/chat/completions",
                json={
                    "model": dw_model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{b64}",
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Read ALL text on screen precisely. "
                                    "Then describe: where is the green ball, "
                                    "what quadrant, what direction is the trail, "
                                    "and is it near any edge? Be concise."
                                ),
                            },
                        ],
                    }],
                    "max_tokens": 200,
                    "temperature": 0.0,
                },
                headers={
                    "Authorization": f"Bearer {dw_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data["choices"][0]["message"].get("content", "")
    except Exception:
        return None


async def _fused_perception(
    claude_client, b64: str, ocr_vals: Dict[str, str],
) -> str:
    """Fire 235B + Claude in parallel, fuse results into one narration."""
    # Launch both in parallel
    tasks = []
    if claude_client:
        tasks.append(asyncio.create_task(_claude_vision(claude_client, b64)))
    else:
        tasks.append(asyncio.create_task(asyncio.sleep(0)))  # placeholder

    tasks.append(asyncio.create_task(_doubleword_vision(b64)))

    # Wait for both (with timeout so we don't block forever)
    done, pending = await asyncio.wait(tasks, timeout=12)
    for p in pending:
        p.cancel()

    claude_result = None
    dw_result = None
    for t in done:
        try:
            r = t.result()
            if r is None:
                continue
            # Claude results tend to be longer/more narrative
            # 235B results tend to start with the text data
            if claude_client and t == tasks[0]:
                claude_result = r
            else:
                dw_result = r
        except Exception:
            pass

    # Fuse: OCR numbers + 235B detail + Claude spatial reasoning
    parts = []

    # Structured data from OCR
    h = ocr_vals.get("horizontal", "?")
    v = ocr_vals.get("vertical", "?")
    t = ocr_vals.get("total", "?")
    if h != "?" and v != "?":
        parts.append(f"{t} total bounces. {h} horizontal, {v} vertical.")

    # 235B structural detail (if it adds something beyond OCR)
    if dw_result:
        parts.append(f"235B sees: {dw_result[:150]}")

    # Claude semantic understanding
    if claude_result:
        parts.append(f"Claude sees: {claude_result[:150]}")

    return " ".join(parts) if parts else ""


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=45)
    asyncio.run(main(duration_s=p.parse_args().duration))
