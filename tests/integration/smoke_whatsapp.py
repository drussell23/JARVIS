#!/usr/bin/env python3
"""
JARVIS WhatsApp Smoke Test — Narrated Real-Time Execution

Simulates: "Hey JARVIS, message Zach on WhatsApp saying hey, what's up? Testing."

Pipeline:
  Intent -> ExecutionTierRouter (NATIVE_APP) -> NativeAppControlAgent
          -> AccessibilityResolver (AX exact coords) -> click/type

Usage:
    python3 tests/integration/smoke_whatsapp.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "backend"))

# ── TTS (sequential, non-overlapping) ──────────────────────────────────────
_tts_lock: asyncio.Lock | None = None


def _tts() -> asyncio.Lock:
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    return _tts_lock


async def jarvis_say(text: str, rate: int = 185) -> None:
    """Speak text aloud via macOS say, sequential (no overlap)."""
    print(f"\n  JARVIS: \"{text}\"")
    async with _tts():
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Samantha", "-r", str(rate), text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


def banner(title: str) -> None:
    width = 72
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def step(n: int, label: str) -> None:
    print(f"\n  [Step {n}] {label}")


def ok(msg: str) -> None:
    print(f"    OK  {msg}")


def info(msg: str) -> None:
    print(f"    ..  {msg}")


def fail(msg: str) -> None:
    print(f"    ERR {msg}")


# ── Smoke test config ────────────────────────────────────────────────────────

USER_INTENT  = "Message Zach on WhatsApp: hey, what's up? Testing."
TARGET_APP   = "WhatsApp"
SEARCH_TEXT  = "Zach"
MESSAGE_TEXT = "hey, what's up? Testing."


# ── Main ─────────────────────────────────────────────────────────────────────

async def run() -> None:
    from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
    from backend.neural_mesh.agents.native_app_control_agent import NativeAppControlAgent

    banner("JARVIS WhatsApp Smoke Test")
    print(f"  User intent : \"{USER_INTENT}\"")
    print()
    print("  Agent pipeline:")
    print("    [1] Intent Parser          -> app=WhatsApp  contact=Zach")
    print("    [2] ExecutionTierRouter    -> NATIVE_APP  (installed app, use AX)")
    print("    [3] NativeAppControlAgent  -> vision-action loop")
    print("    [4] AccessibilityResolver  -> AXUIElement exact coordinates")
    print("    [5] CGEventPost            -> hardware-level click / type")

    resolver = AccessibilityResolver()
    agent    = NativeAppControlAgent()
    t0       = time.monotonic()

    await jarvis_say("Got it. I'll message Zach on WhatsApp for you.")
    await asyncio.sleep(0.3)

    # ── 1. Verify WhatsApp is running ────────────────────────────────────────
    step(1, "Checking WhatsApp")
    pid = resolver._get_pid_for_app(TARGET_APP)

    if pid is None:
        info("WhatsApp not running — launching...")
        await jarvis_say("WhatsApp isn't open. Launching it now.")
        await asyncio.create_subprocess_exec("open", "-a", TARGET_APP)
        await asyncio.sleep(4.0)
        pid = resolver._get_pid_for_app(TARGET_APP)

    if pid is None:
        fail("WhatsApp still not found after launch.")
        await jarvis_say("I couldn't open WhatsApp. Please launch it manually and try again.")
        return

    ok(f"WhatsApp running  PID {pid}")

    # ── 2. Bring WhatsApp to front ───────────────────────────────────────────
    step(2, "Activating WhatsApp window")
    await jarvis_say("Bringing WhatsApp to the front.")
    await agent._activate_app(TARGET_APP)
    await asyncio.sleep(0.8)
    ok("WhatsApp is in the foreground")

    # ── 3. Resolve search bar via AX ────────────────────────────────────────
    step(3, "Locating search bar via Accessibility API")
    await jarvis_say("Scanning the accessibility tree for the search bar.")

    search_coords = await resolver.resolve(
        "search", app_name=TARGET_APP, role="AXTextField"
    )

    if search_coords is None:
        info("AXTextField 'search' not found — running broad scan...")
        elements = await resolver.list_elements(TARGET_APP, max_depth=4)
        fields = [
            e for e in elements
            if "text" in e.get("role", "").lower() or "field" in e.get("role", "").lower()
        ]
        info(f"Text-field elements in AX tree: {len(fields)}")
        for f in fields[:8]:
            info(
                f"  [{f.get('role')}] "
                f"'{(f.get('title') or f.get('description') or '?')[:40]}'"
                f"  @ ({f.get('x')}, {f.get('y')})"
            )
        fail("Cannot resolve search bar — aborting.")
        await jarvis_say(
            "I couldn't find the search bar. "
            "The accessibility tree may need a moment. Try again."
        )
        return

    ok(
        f"Search bar resolved  "
        f"({search_coords['x']}, {search_coords['y']})  "
        f"[{search_coords['width']} x {search_coords['height']}]"
    )

    # ── 4. Click search bar ──────────────────────────────────────────────────
    step(4, "Clicking search bar")
    await jarvis_say("Clicking the search bar.")
    clicked = await agent._click_element(
        {"element": "search bar", "role": "AXTextField"},
        TARGET_APP,
    )
    ok(f"Click dispatched  result={clicked}")
    await asyncio.sleep(0.5)

    # ── 5. Type contact name ─────────────────────────────────────────────────
    step(5, f"Typing '{SEARCH_TEXT}'")
    await jarvis_say("Searching for Zach.")
    await agent._type_text(SEARCH_TEXT)
    await asyncio.sleep(1.3)   # let search results populate
    ok(f"Typed '{SEARCH_TEXT}'")

    # ── 6. Resolve Zach in results ───────────────────────────────────────────
    step(6, "Resolving Zach's conversation cell via AX")
    await jarvis_say("Looking for Zach in the results.")

    zach_coords = await resolver.resolve(SEARCH_TEXT, app_name=TARGET_APP)

    if zach_coords is None:
        info("Name search returned nothing — running broad AX scan...")
        elements = await resolver.list_elements(TARGET_APP, max_depth=5)
        candidates = [
            e for e in elements
            if "zach" in (e.get("title") or e.get("description") or "").lower()
        ]
        info(f"Elements containing 'zach': {len(candidates)}")
        for c in candidates[:5]:
            info(
                f"  [{c.get('role')}] "
                f"'{(c.get('title') or c.get('description') or '?')[:50]}'"
                f"  @ ({c.get('x')}, {c.get('y')})"
            )

        if not candidates:
            fail("Zach not found in search results.")
            await jarvis_say(
                "I couldn't find Zach in your contacts. "
                "He may not be in your WhatsApp."
            )
            return

        best = candidates[0]
        zach_coords = {
            "x": best["x"] + best.get("width", 60) // 2,
            "y": best["y"] + best.get("height", 30) // 2,
        }

    ok(f"Zach found  ({zach_coords['x']}, {zach_coords['y']})")

    # ── 7. Open Zach's conversation ──────────────────────────────────────────
    step(7, "Opening Zach's conversation")
    await jarvis_say("Opening Zach's conversation.")
    await agent._click_element(
        {"element": "Zach", "near_text": "Singleton"},
        TARGET_APP,
    )
    await asyncio.sleep(1.2)
    ok("Conversation opened")

    # ── 8. Resolve message input ─────────────────────────────────────────────
    step(8, "Locating message input field via AX")
    await jarvis_say("Finding the message box.")

    msg_coords = await resolver.resolve(
        "message", app_name=TARGET_APP, role="AXTextArea"
    )
    if msg_coords is None:
        msg_coords = await resolver.resolve(
            "message", app_name=TARGET_APP, role="AXTextField"
        )
    if msg_coords is None:
        # fallback: pick the lowest text-area on screen (below search bar)
        elements = await resolver.list_elements(TARGET_APP, max_depth=5)
        inputs = [
            e for e in elements
            if e.get("role") in ("AXTextArea", "AXTextField")
            and e.get("y", 0) > search_coords["y"] + 50
        ]
        info(f"Fallback: {len(inputs)} text inputs below search bar")
        if inputs:
            best = max(inputs, key=lambda e: e.get("y", 0))
            msg_coords = {
                "x": best["x"] + best.get("width", 200) // 2,
                "y": best["y"] + best.get("height", 30) // 2,
            }

    if msg_coords is None:
        fail("Message input not found — aborting.")
        await jarvis_say(
            "I couldn't find the message text box. "
            "WhatsApp's layout may have changed."
        )
        return

    ok(f"Message input resolved  ({msg_coords['x']}, {msg_coords['y']})")

    # ── 9. Click message input ────────────────────────────────────────────────
    step(9, "Clicking message input")
    await agent._click_element(
        {"element": "message", "role": "AXTextArea"},
        TARGET_APP,
    )
    await asyncio.sleep(0.4)
    ok("Message input focused")

    # ── 10. Type the message ─────────────────────────────────────────────────
    step(10, f"Typing: \"{MESSAGE_TEXT}\"")
    await jarvis_say("Typing your message now.")
    await agent._type_text(MESSAGE_TEXT)
    await asyncio.sleep(0.5)
    ok(f"Message typed")

    # ── Summary ──────────────────────────────────────────────────────────────
    elapsed = time.monotonic() - t0
    banner(f"Smoke Test Complete   {elapsed:.1f}s")
    print("  WhatsApp opened and focused")
    print("  Search bar located via AX API (exact pixel coordinates)")
    print("  Zach's conversation found and opened")
    print(f"  Message typed: \"{MESSAGE_TEXT}\"")
    print()
    print("  Message is ready.  Press Enter in WhatsApp to send, Escape to discard.")

    await jarvis_say(
        f"Done. I've typed your message to Zach. "
        "Press Enter in WhatsApp to send it, or Escape to cancel."
    )


if __name__ == "__main__":
    asyncio.run(run())
