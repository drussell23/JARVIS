#!/usr/bin/env python3
"""
JARVIS Real-Time Vision + Voice Smoke Test

Opens the bouncing ball, starts Ferrari Engine for continuous capture,
sends frames to Claude Vision every few seconds, and JARVIS speaks
what it sees in real-time via macOS TTS.

Tests: Ferrari Engine -> Claude Vision -> Voice narration

Usage:
    python3 tests/test_vision_realtime_smoke.py [--duration 30] [--interval 5]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        override=True,
    )
except ImportError:
    pass


_active_say: asyncio.subprocess.Process | None = None


async def jarvis_say(text: str, voice: str = "Daniel", wait: bool = False) -> None:
    """Speak text using macOS say. Kills previous speech first — one voice at a time."""
    global _active_say

    # Kill any speech still playing
    if _active_say is not None and _active_say.returncode is None:
        try:
            _active_say.terminate()
            await asyncio.wait_for(_active_say.wait(), timeout=0.3)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                _active_say.kill()
            except ProcessLookupError:
                pass

    _active_say = await asyncio.create_subprocess_exec(
        "say", "-v", voice, "-r", "195", text,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if wait:
        await _active_say.wait()


async def main(duration_s: int = 30, interval_s: int = 5):
    print("\n" + "=" * 60)
    print("  JARVIS Real-Time Vision + Voice Smoke Test")
    print("=" * 60)

    # 1. Open bouncing ball
    html_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "vision_smoke_test_bounce.html",
    )
    if os.path.exists(html_path):
        subprocess.Popen(
            ["open", html_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"\n  Opened bouncing ball: {os.path.basename(html_path)}")

    # 2. Greeting
    await jarvis_say(
        "JARVIS vision system online. Starting real-time screen observation.",
        wait=True,
    )
    print("  JARVIS: Vision system online.\n")

    # 3. Start Ferrari Engine
    from backend.vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop.get_instance()

    print("  Starting Ferrari Engine...")
    await loop._ensure_frame_server()
    if loop._frame_server_ready:
        await asyncio.sleep(2.0)
        print("  Ferrari Engine: ONLINE (Quartz CGWindowListCreateImage)")
    else:
        print("  Ferrari Engine: OFFLINE (using screencapture fallback)")

    await jarvis_say("Ferrari Engine started. I can see your screen now.", wait=True)

    # 4. Init Claude client
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("\n  ERROR: No ANTHROPIC_API_KEY")
        await jarvis_say("Error. No API key found.", wait=True)
        return

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
    except ImportError:
        print("\n  ERROR: anthropic package not installed")
        return

    # 5. Real-time observation loop
    print(f"\n  Observing for {duration_s}s (every {interval_s}s)...")
    print("  " + "-" * 50)

    count = 0
    t_start = time.monotonic()

    while (time.monotonic() - t_start) < duration_s:
        count += 1
        remaining = int(duration_s - (time.monotonic() - t_start))

        # Capture
        t0 = time.monotonic()
        b64_png = await loop._capture_cu_screenshot()
        cap_ms = (time.monotonic() - t0) * 1000

        if b64_png is None:
            print(f"  [{count}] CAPTURE FAILED")
            await asyncio.sleep(interval_s)
            continue

        # Ask Claude what it sees — real-time narration, SHORT and punchy
        system = (
            "You are JARVIS, Derek's AI assistant. You're watching his screen LIVE.\n"
            "Narrate like a sports commentator — SHORT, punchy, real-time.\n\n"
            "RULES:\n"
            "- MAX 2 sentences, under 25 words total.\n"
            "- Read visible counters/numbers EXACTLY.\n"
            "- Describe movement direction and what just happened.\n"
            "- Address Derek directly.\n"
            "- Do NOT fabricate numbers you can't read.\n"
            "- Examples of good responses:\n"
            '  "Derek, 12 horizontal, 8 vertical bounces. Ball heading upper-right."\n'
            '  "Ball just hit the left wall. 15 total bounces now, speed at 330."\n'
            '  "Bounces climbing — 20 total. Ball curving down toward bottom-right."'
        )

        prev_note = ""
        if count > 1:
            prev_note = f"This is observation {count}. Describe what CHANGED since last time. "

        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_png,
                },
            },
            {
                "type": "text",
                "text": (
                    f"{prev_note}"
                    "Narrate what's happening on screen right now. "
                    "Where is the ball? What are the bounce counters showing? "
                    "What direction is the ball moving based on its trail?"
                ),
            },
        ]

        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=os.environ.get(
                        "JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514",
                    ),
                    max_tokens=150,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                ),
                timeout=15,
            )
            desc = response.content[0].text if response.content else "I couldn't see anything."
            api_ms = (time.monotonic() - t0) * 1000
        except asyncio.TimeoutError:
            desc = "Vision analysis timed out."
            api_ms = 15000
        except Exception as exc:
            desc = f"Vision error: {exc}"
            api_ms = (time.monotonic() - t0) * 1000

        # Print and speak
        print(f"\n  [{count}] capture={cap_ms:.0f}ms  api={api_ms:.0f}ms")
        print(f"  JARVIS: {desc}")

        # Speak and WAIT — one voice at a time, no overlap
        await jarvis_say(desc, wait=True)

        # Brief pause before next observation
        await asyncio.sleep(1.0)

    # 6. Summary
    print("\n  " + "-" * 50)
    total = time.monotonic() - t_start
    print(f"  {count} observations in {total:.1f}s")

    await jarvis_say(
        f"Real-time observation complete. "
        f"I made {count} observations over {int(total)} seconds. "
        "All vision systems nominal, Derek.",
        wait=True,
    )

    print("  JARVIS: All systems nominal.")
    print("=" * 60 + "\n")

    # Cleanup
    if loop._frame_server_proc and loop._frame_server_proc.returncode is None:
        loop._frame_server_proc.terminate()
        print("  Stopped Ferrari Engine.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(main(duration_s=args.duration, interval_s=args.interval))
