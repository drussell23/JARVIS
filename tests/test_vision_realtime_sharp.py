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
# Main loop
# ---------------------------------------------------------------------------

async def main(duration_s: int = 45):
    print("\n" + "=" * 60)
    print("  JARVIS Real-Time Vision -- Read What's On Screen")
    print("=" * 60)

    # Open bouncing ball and bring it to front
    html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vision_smoke_test_bounce.html")
    if os.path.exists(html):
        subprocess.Popen(["open", html], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(2.0)

    # Bring the bouncing ball tab to front in Chrome
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", """
tell application "Google Chrome"
    activate
    repeat with w in windows
        set tabIndex to 0
        repeat with t in tabs of w
            set tabIndex to tabIndex + 1
            if title of t contains "Bouncing Ball" then
                set active tab index of w to tabIndex
                set index of w to 1
                return
            end if
        end repeat
    end repeat
end tell
""",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(1.0)
    except Exception:
        pass

    await jarvis_say("JARVIS vision online. I will read exactly what I see on your screen.")

    # Re-focus Chrome after speech (terminal may have stolen focus)
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
    await loop._ensure_frame_server()
    if loop._frame_server_ready:
        await asyncio.sleep(2.0)
    print("  Capture: ONLINE\n")

    # Cloud model for spatial context (async, non-blocking)
    claude_client = None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            import anthropic
            claude_client = anthropic.AsyncAnthropic(api_key=api_key)
        except ImportError:
            pass

    # --- Ouroboros: VisionReflexCompiler ---
    from backend.vision.vision_reflex import VisionReflexCompiler
    reflex_compiler = VisionReflexCompiler.get_instance()
    TASK_KEY = "ocr_hud"

    print(f"  Running {duration_s}s...")
    print()
    print("  " + "=" * 56)
    print("   PHASE 1: THE SQUINT — Naive Agentic Baseline")
    print("  " + "=" * 56)

    t_start = time.monotonic()
    prev_vals: Dict[str, str] = {}
    last_narr_time = 0.0
    last_cloud_time = 0.0
    n_reads = 0
    n_reflex_reads = 0
    n_speaks = 0
    ocr_latencies: list = []
    reflex_latencies: list = []
    cloud_task: Optional[asyncio.Task] = None

    _tel_dir = os.environ.get(
        "VISION_TELEMETRY_DIR", "/tmp/claude/vision_telemetry",
    )
    _latest = os.path.join(_tel_dir, "vision_last_perception.png")

    while (time.monotonic() - t_start) < duration_s:
        # Capture
        b64 = await loop._capture_cu_screenshot()
        if b64 is None:
            await asyncio.sleep(0.5)
            continue

        # --- Ouroboros routing: reflex or OCR? ---
        t_read = time.monotonic()
        reflex = reflex_compiler.get_reflex(TASK_KEY)

        if reflex is not None:
            # FAST PATH: compiled reflex (may be sync or async)
            if asyncio.iscoroutinefunction(reflex):
                vals = await reflex(b64)
            else:
                vals = reflex(b64)
            read_ms = (time.monotonic() - t_read) * 1000
            n_reflex_reads += 1
            reflex_latencies.append(read_ms)
            label = "REFLEX"
            tier = reflex_compiler.get_active_tier(TASK_KEY)
        else:
            # SLOW PATH: full Apple Vision OCR
            vals = await ocr_read_screen(b64)
            read_ms = (time.monotonic() - t_read) * 1000
            n_reads += 1
            ocr_latencies.append(read_ms)
            label = "OCR"

            # Track call and check for graduation
            event = reflex_compiler.record_call(TASK_KEY, read_ms)
            if event == "graduate" and vals:
                avg_ocr = (
                    sum(ocr_latencies) / len(ocr_latencies)
                    if ocr_latencies else read_ms
                )

                # ============ PHASE 2: THE SURGERY ============
                print()
                print("  " + "=" * 56)
                print("   PHASE 2: THE SURGERY — Ouroboros Triggered")
                print("  " + "=" * 56)
                print(
                    f"  [Ouroboros] CognitiveInefficiencyEvent: "
                    f"{reflex_compiler.get_call_count(TASK_KEY)} repeated "
                    f"reads at avg {avg_ocr:.0f}ms"
                )
                print(
                    f"  [Ouroboros] Latency threshold breached. "
                    f"The Naive Agentic Way is burning {avg_ocr:.0f}ms/read."
                )

                jarvis_say_background(
                    "Cognitive inefficiency detected. Initiating neuro compilation.",
                )

                def _print_status(msg: str) -> None:
                    print(f"  [Ouroboros] {msg}")
                    # Narrate key surgical moments live
                    lower = msg.lower()
                    if any(kw in lower for kw in [
                        "235b", "397b", "tier 4", "validated",
                        "synthesis", "sandbox", "generated",
                    ]):
                        jarvis_say_background(msg[:120])

                t_compile = time.monotonic()
                ok = await reflex_compiler.compile_reflexes(
                    TASK_KEY, b64, vals, on_status=_print_status,
                )
                compile_ms = (time.monotonic() - t_compile) * 1000

                if ok:
                    tier = reflex_compiler.get_active_tier(TASK_KEY)
                    print()
                    print("  " + "=" * 56)
                    print("   PHASE 3: 20/20 VISION — Reflex Assimilated")
                    print("  " + "=" * 56)
                    print(
                        f"  [Retina] Tier {tier} reflex GRADUATED "
                        f"in {compile_ms:.0f}ms"
                    )
                    print(
                        f"  [Retina] Deterministic fast-path active. "
                        f"Baseline {avg_ocr:.0f}ms -> reflex target <200ms"
                    )
                    print()
                    jarvis_say_background(
                        f"Reflex assimilated. Switching from {int(avg_ocr)} "
                        f"millisecond reads to tier {tier} reflex.",
                    )
                else:
                    print(f"  [Ouroboros] All tiers FAILED — staying on baseline OCR")

        # Only speak if we actually read something AND values changed
        if vals:
            changed = vals != prev_vals
            enough_time = (time.monotonic() - last_narr_time) > 2.5

            if changed or enough_time:
                parts = []
                h = vals.get("horizontal")
                v = vals.get("vertical")
                t = vals.get("total")
                prev_h = prev_vals.get("horizontal")
                prev_t = prev_vals.get("total")

                if h and v and t:
                    if prev_t and prev_h and changed:
                        try:
                            delta_t = int(t) - int(prev_t)
                            if delta_t > 0:
                                parts.append(f"{t} total bounces, up {delta_t}")
                            else:
                                parts.append(f"{t} total bounces")
                        except ValueError:
                            parts.append(f"{t} total bounces")
                        parts.append(f"{h} horizontal, {v} vertical")
                    else:
                        parts.append(f"{h} horizontal, {v} vertical, {t} total bounces")

                narr = ". ".join(parts)
                if narr:
                    n_speaks += 1
                    prev_vals = vals.copy()
                    last_narr_time = time.monotonic()

                    _artifact_hint = (
                        f" | verify: {_latest}" if os.path.exists(_latest) else ""
                    )
                    print(
                        f"  [{label} #{n_reads + n_reflex_reads}] ({read_ms:.0f}ms) "
                        f"H:{h} V:{v} T:{t}{_artifact_hint}"
                    )
                    jarvis_say_background(narr)

        # Cloud spatial context every ~10s (async background)
        elapsed = time.monotonic() - t_start
        if cloud_task and cloud_task.done():
            try:
                cloud_result = cloud_task.result()
                if cloud_result:
                    print(f"  [CLOUD] {cloud_result}")
                    await jarvis_say(cloud_result)
            except Exception:
                pass
            cloud_task = None

        if (elapsed - last_cloud_time) >= 10.0 and cloud_task is None and claude_client and n_speaks >= 2:
            last_cloud_time = elapsed
            cloud_task = asyncio.create_task(
                _cloud_spatial(claude_client, b64)
            )

        await asyncio.sleep(0.3)

    # Cleanup
    if cloud_task and not cloud_task.done():
        cloud_task.cancel()

    total = time.monotonic() - t_start
    print(f"\n  " + "-" * 50)
    print(f"  OCR reads: {n_reads} | Reflex reads: {n_reflex_reads} | Duration: {total:.1f}s")
    if ocr_latencies:
        print(f"  OCR avg: {sum(ocr_latencies)/len(ocr_latencies):.0f}ms")
    if reflex_latencies:
        avg_reflex = sum(reflex_latencies) / len(reflex_latencies)
        print(f"  Reflex avg: {avg_reflex:.0f}ms")
        if ocr_latencies:
            speedup = (sum(ocr_latencies) / len(ocr_latencies)) / avg_reflex
            print(f"  Ouroboros speedup: {speedup:.0f}x")

    summary = f"Done. {n_reads} O.C.R. reads, {n_reflex_reads} reflex reads in {int(total)} seconds."
    if reflex_latencies and ocr_latencies:
        speedup = (sum(ocr_latencies) / len(ocr_latencies)) / (sum(reflex_latencies) / len(reflex_latencies))
        summary += f" Ouroboros achieved {int(speedup)}x speedup."
    await jarvis_say(summary)

    print("=" * 60 + "\n")
    if loop._frame_server_proc and loop._frame_server_proc.returncode is None:
        loop._frame_server_proc.terminate()


async def _cloud_spatial(client, b64: str) -> Optional[str]:
    """Ask Claude for spatial context -- where is the ball?"""
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=os.environ.get("JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=40,
                system=(
                    "ONE sentence, under 12 words. "
                    "Where is the green ball on screen and what direction is its trail pointing? "
                    "Address Derek."
                ),
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": "Ball position and trail direction."},
                ]}],
            ),
            timeout=8,
        )
        for block in resp.content:
            if hasattr(block, "text"):
                return block.text.strip()
    except Exception:
        pass
    return None


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=45)
    asyncio.run(main(duration_s=p.parse_args().duration))
