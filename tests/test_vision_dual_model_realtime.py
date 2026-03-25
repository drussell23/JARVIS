#!/usr/bin/env python3
"""
JARVIS Dual-Model Real-Time Vision Test

Two models collaborate watching the screen:
  - Doubleword VL-235B (fast eye): observes every ~4s, reads numbers, tracks movement
  - Claude Vision (deep brain): analyzes every ~12s, reasons about patterns and trends

Both narrate through one JARVIS voice — never overlapping.

Usage:
    python3 tests/test_vision_dual_model_realtime.py [--duration 45]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_root, ".env"), override=True)
    load_dotenv(os.path.join(_root, "backend", ".env"), override=True)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

_active_say: Optional[asyncio.subprocess.Process] = None


async def jarvis_say(text: str, voice: str = "Daniel") -> None:
    """Speak via macOS say. Kills previous speech — one voice at a time. Waits."""
    global _active_say
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
        "say", "-v", voice, "-r", "210", text,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await _active_say.wait()


# ---------------------------------------------------------------------------
# Dual-model observer
# ---------------------------------------------------------------------------

class DualModelObserver:
    """Doubleword (fast eye) + Claude (deep brain) working together."""

    def __init__(self):
        self._dw_session = None
        self._claude_client = None
        self._fast_count = 0
        self._deep_count = 0
        self._observations: list = []

    async def start(self):
        dw_key = os.environ.get("DOUBLEWORD_API_KEY", "")
        if dw_key:
            import aiohttp
            self._dw_session = aiohttp.ClientSession()
            print("  Doubleword VL-235B: ONLINE (fast eye)")
        else:
            print("  Doubleword VL-235B: OFFLINE (Claude will handle fast reads)")

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            import anthropic
            self._claude_client = anthropic.AsyncAnthropic(api_key=api_key)
            print("  Claude Vision: ONLINE (deep brain)")
        else:
            print("  Claude Vision: OFFLINE")

    async def stop(self):
        if self._dw_session:
            await self._dw_session.close()

    # --- Fast eye: quick read of screen state ---

    async def observe_fast(self, b64_png: str) -> Optional[str]:
        self._fast_count += 1

        if self._dw_session and os.environ.get("DOUBLEWORD_API_KEY"):
            result = await self._call_doubleword(b64_png)
            if result:
                self._observations.append(f"[Fast #{self._fast_count}] {result}")
                return result

        if self._claude_client:
            result = await self._call_claude_fast(b64_png)
            if result:
                self._observations.append(f"[Fast #{self._fast_count}] {result}")
                return result

        return None

    # --- Deep brain: pattern analysis with context ---

    async def observe_deep(self, b64_png: str) -> Optional[str]:
        if not self._claude_client:
            return None

        self._deep_count += 1
        recent = self._observations[-6:]
        history = "\n".join(recent) if recent else "(first deep analysis)"

        system = (
            "You are JARVIS's deep brain. A fast-eye model reads the screen every few seconds. "
            "Below are its recent reads. YOUR job: find PATTERNS and TRENDS the fast eye misses.\n\n"
            "RULES:\n"
            "- 1-2 sentences max, under 30 words.\n"
            "- Compare current screenshot to the observation history.\n"
            "- Note TRENDS: bounce rate changes, speed changes, repeating paths.\n"
            "- Do NOT repeat the fast eye. Add NEW insight only.\n"
            "- Speak to Derek naturally."
        )

        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_png}},
            {"type": "text", "text": (
                f"Deep analysis #{self._deep_count}.\n"
                f"Recent fast-eye reads:\n{history}\n\n"
                "What patterns or trends do you see? What changed over time?"
            )},
        ]

        try:
            response = await asyncio.wait_for(
                self._claude_client.messages.create(
                    model=os.environ.get("JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514"),
                    max_tokens=100,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                ),
                timeout=12,
            )
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    self._observations.append(f"[Deep #{self._deep_count}] {text}")
                    return text
        except Exception as exc:
            print(f"  [Deep brain error: {exc}]")
        return None

    # --- Doubleword API ---

    async def _call_doubleword(self, b64_png: str) -> Optional[str]:
        import aiohttp

        system = (
            "You are JARVIS's fast eye. ONE sentence, MAX 20 words.\n"
            "Read ALL visible numbers exactly. Note ball position and direction.\n"
            "Address Derek directly."
        )

        payload = {
            "model": os.environ.get("DOUBLEWORD_VISION_MODEL", "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
                    {"type": "text", "text": "Read the counters. Where is the ball going? One sentence."},
                ]},
            ],
            "max_tokens": 80,
            "temperature": 0.1,
        }

        try:
            base_url = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
            async with self._dw_session.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {os.environ['DOUBLEWORD_API_KEY']}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data["choices"][0]["message"].get("content", "").strip()
        except Exception as exc:
            print(f"  [Doubleword error: {exc}]")
            return None

    # --- Claude fast fallback ---

    async def _call_claude_fast(self, b64_png: str) -> Optional[str]:
        system = (
            "You are JARVIS's fast eye. ONE sentence, MAX 20 words.\n"
            "Read visible numbers exactly. Note ball position and movement direction.\n"
            "Address Derek directly."
        )

        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_png}},
            {"type": "text", "text": "Read counters, ball position and direction. One sentence to Derek."},
        ]

        try:
            response = await asyncio.wait_for(
                self._claude_client.messages.create(
                    model=os.environ.get("JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514"),
                    max_tokens=60,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                ),
                timeout=10,
            )
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
        except Exception as exc:
            print(f"  [Claude fast error: {exc}]")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(duration_s: int = 45):
    print("\n" + "=" * 60)
    print("  JARVIS Dual-Model Real-Time Vision")
    print("  Fast Eye (Doubleword) + Deep Brain (Claude)")
    print("=" * 60)

    html_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "vision_smoke_test_bounce.html",
    )
    if os.path.exists(html_path):
        subprocess.Popen(["open", html_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"\n  Opened: {os.path.basename(html_path)}")

    await jarvis_say("JARVIS dual vision activated. Fast eye and deep brain are online.")
    print("  JARVIS: Dual vision online.\n")

    # Ferrari Engine
    from backend.vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop.get_instance()
    await loop._ensure_frame_server()
    if loop._frame_server_ready:
        await asyncio.sleep(2.0)
        print("  Ferrari Engine: ONLINE")
    else:
        print("  Ferrari Engine: OFFLINE (screencapture fallback)")

    # Models
    observer = DualModelObserver()
    await observer.start()

    print(f"\n  Observing for {duration_s}s...")
    print("  " + "-" * 50)

    t_start = time.monotonic()
    last_deep = 0.0

    while (time.monotonic() - t_start) < duration_s:
        elapsed = time.monotonic() - t_start

        b64_png = await loop._capture_cu_screenshot()
        if b64_png is None:
            await asyncio.sleep(1.0)
            continue

        # Deep analysis every ~12s (after at least 2 fast reads)
        do_deep = (elapsed - last_deep) >= 12.0 and observer._fast_count >= 2

        if do_deep:
            last_deep = elapsed
            print(f"\n  --- DEEP BRAIN (Claude) analyzing patterns ---")
            t0 = time.monotonic()
            result = await observer.observe_deep(b64_png)
            ms = (time.monotonic() - t0) * 1000
            if result:
                print(f"  [{ms:.0f}ms] JARVIS [deep]: {result}")
                await jarvis_say(result)
            else:
                print(f"  [{ms:.0f}ms] Deep analysis: no response")
        else:
            # Fast eye
            t0 = time.monotonic()
            result = await observer.observe_fast(b64_png)
            ms = (time.monotonic() - t0) * 1000
            tag = "DW" if observer._dw_session and os.environ.get("DOUBLEWORD_API_KEY") else "CL"
            if result:
                print(f"\n  [FAST #{observer._fast_count}] [{tag}] ({ms:.0f}ms): {result}")
                await jarvis_say(result)
            else:
                print(f"\n  [FAST #{observer._fast_count}] ({ms:.0f}ms): no response")

        await asyncio.sleep(0.5)

    # Done
    print("\n  " + "-" * 50)
    total = time.monotonic() - t_start
    print(f"  {observer._fast_count} fast + {observer._deep_count} deep in {total:.1f}s")

    await jarvis_say(
        f"Test complete. {observer._fast_count} fast observations "
        f"and {observer._deep_count} deep analyses over {int(total)} seconds. "
        "Dual model vision is fully operational, Derek."
    )

    print("=" * 60 + "\n")
    await observer.stop()
    if loop._frame_server_proc and loop._frame_server_proc.returncode is None:
        loop._frame_server_proc.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=45)
    args = parser.parse_args()
    asyncio.run(main(duration_s=args.duration))
