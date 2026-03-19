#!/usr/bin/env python3
"""
Hybrid Vision + Accessibility Test — Run from your terminal (needs GUI session).

Tests:
1. AccessibilityResolver can find real UI elements on your Mac
2. NativeAppControlAgent uses AX-resolved coordinates instead of vision guesses
3. Full WhatsApp flow with AX-precise clicks

Usage:
    # Test 1: See what AX can find in WhatsApp (read-only, safe)
    python3 tests/integration/test_ax_hybrid.py discover WhatsApp

    # Test 2: See what AX can find in Finder (always running)
    python3 tests/integration/test_ax_hybrid.py discover Finder

    # Test 3: Full WhatsApp test with AX clicks (needs J-Prime GPU VM)
    JARVIS_PRIME_HOST=34.70.122.142 python3 tests/integration/test_ax_hybrid.py whatsapp
"""

from __future__ import annotations

import asyncio
import sys
import os
from pathlib import Path

_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "backend"))


async def discover_app(app_name: str):
    """Discover all accessible UI elements in an app."""
    from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver

    resolver = AccessibilityResolver()

    print(f"\n{'=' * 70}")
    print(f"  Accessibility Tree: {app_name}")
    print(f"{'=' * 70}")

    # Get PID
    pid = resolver._get_pid_for_app(app_name)
    if pid is None:
        print(f"  {app_name} is not running.")
        return
    print(f"  PID: {pid}")

    # List elements
    elements = await resolver.list_elements(app_name, max_depth=5)
    print(f"  Elements found: {len(elements)}")
    print()

    # Group by role
    by_role: dict = {}
    for e in elements:
        role = e.get("role", "unknown")
        by_role.setdefault(role, []).append(e)

    for role, items in sorted(by_role.items()):
        print(f"  [{role}] ({len(items)} elements)")
        for item in items[:5]:
            title = item.get("title", "")
            desc = item.get("description", "")
            x, y = item.get("x", "?"), item.get("y", "?")
            w, h = item.get("width", "?"), item.get("height", "?")
            label = title or desc or "(no label)"
            print(f"    - {label[:40]:40s} @ ({x}, {y}) size ({w}x{h})")
        if len(items) > 5:
            print(f"    ... and {len(items) - 5} more")
        print()

    # Try to find common elements
    print(f"  {'─' * 60}")
    print(f"  Testing specific element resolution:")
    print(f"  {'─' * 60}")

    test_queries = [
        ("search", "AXTextField"),
        ("close", "AXButton"),
        ("minimize", "AXButton"),
        ("search", None),
    ]

    for desc, role in test_queries:
        result = await resolver.resolve(desc, app_name, role=role)
        if result:
            print(f"  '{desc}' (role={role}) → ({result['x']}, {result['y']}) [{result['width']}x{result['height']}]")
        else:
            print(f"  '{desc}' (role={role}) → NOT FOUND")


async def test_whatsapp_flow():
    """Full WhatsApp test with AX-resolved clicks."""
    print(f"\n{'=' * 70}")
    print(f"  WhatsApp Hybrid Vision + AX Test")
    print(f"{'=' * 70}")

    from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
    from backend.neural_mesh.agents.native_app_control_agent import NativeAppControlAgent

    resolver = AccessibilityResolver()

    # Check WhatsApp is running
    pid = resolver._get_pid_for_app("WhatsApp")
    if pid is None:
        print("  WhatsApp is not running. Please open it first.")
        return

    print(f"  WhatsApp PID: {pid}")

    # Step 1: Find the search bar via AX
    print("\n  Step 1: Finding search bar via Accessibility API...")
    search_coords = await resolver.resolve("search", app_name="WhatsApp", role="AXTextField")
    if search_coords:
        print(f"  FOUND: search bar at ({search_coords['x']}, {search_coords['y']}) [{search_coords['width']}x{search_coords['height']}]")
    else:
        print("  NOT FOUND via AX. Listing all elements for debugging:")
        elements = await resolver.list_elements("WhatsApp", max_depth=4)
        text_fields = [e for e in elements if "text" in e.get("role", "").lower() or "field" in e.get("role", "").lower()]
        print(f"  Text fields found: {len(text_fields)}")
        for tf in text_fields[:10]:
            print(f"    [{tf.get('role')}] {tf.get('title', tf.get('description', '?'))[:40]} @ ({tf.get('x')}, {tf.get('y')})")
        return

    # Step 2: Click the search bar using AX coordinates
    print("\n  Step 2: Clicking search bar at AX-resolved coordinates...")
    agent = NativeAppControlAgent()
    await agent._activate_app("WhatsApp")
    await asyncio.sleep(0.5)

    click_result = await agent._click_element(
        {"element": "search bar", "role": "AXTextField"},
        "WhatsApp",
    )
    print(f"  Click result: {click_result}")
    await asyncio.sleep(0.5)

    # Step 3: Type "Zach"
    print("\n  Step 3: Typing 'Zach' in search bar...")
    await agent._type_text("Zach")
    await asyncio.sleep(1.0)

    # Step 4: Find Zach's conversation via AX
    print("\n  Step 4: Looking for Zach's conversation via AX...")
    zach_coords = await resolver.resolve("Zach", app_name="WhatsApp")
    if zach_coords:
        print(f"  FOUND: Zach at ({zach_coords['x']}, {zach_coords['y']})")

        # Click on Zach's conversation
        print("\n  Step 5: Clicking Zach's conversation...")
        await agent._click_element(
            {"element": "Zach", "near_text": "Singleton"},
            "WhatsApp",
        )
        await asyncio.sleep(1.0)

        # Find message input
        print("\n  Step 6: Finding message input field...")
        msg_input = await resolver.resolve(
            "message", app_name="WhatsApp", role="AXTextField"
        )
        if msg_input:
            print(f"  FOUND: message input at ({msg_input['x']}, {msg_input['y']})")

            # Click message input
            await agent._click_element(
                {"element": "message", "role": "AXTextField"},
                "WhatsApp",
            )
            await asyncio.sleep(0.3)

            # Type message
            print("\n  Step 7: Typing message...")
            await agent._type_text("testing with JARVIS")
            await asyncio.sleep(0.5)

            print("\n  Step 8: Press Enter to send...")
            await agent._press_key("return")

            print(f"\n  {'=' * 60}")
            print(f"  MESSAGE SENT TO ZACH!")
            print(f"  {'=' * 60}")
        else:
            print("  Message input not found via AX")
    else:
        print("  Zach not found in search results")

    print(f"\n  Test complete.")


async def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 tests/integration/test_ax_hybrid.py discover <AppName>")
        print("  python3 tests/integration/test_ax_hybrid.py whatsapp")
        return

    command = sys.argv[1].lower()

    if command == "discover" and len(sys.argv) >= 3:
        await discover_app(sys.argv[2])
    elif command == "whatsapp":
        await test_whatsapp_flow()
    else:
        print(f"Unknown command: {command}")


if __name__ == "__main__":
    asyncio.run(main())
