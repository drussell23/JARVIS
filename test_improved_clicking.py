#!/usr/bin/env python3
"""
Test the improved clicking implementation for display connection
"""

import asyncio
import pytest

# Skips instead of erroring where the optional dependency is absent (headless
# CI). Must precede the import below, which needs pyautogui directly.
pytest.importorskip("pyautogui", reason="pyautogui not installed (headless environment)")
import pyautogui
import time

async def test_improved_clicking():
    """Test the improved clicking with better timing"""

    print("\n" + "="*60)
    print("🎯 Testing Improved Display Connection Clicking")
    print("="*60)

    print("\n📍 Testing with improved timing:")
    print("  - Longer mouse movement duration (0.2s)")
    print("  - Better wait after positioning (0.1s)")
    print("  - MouseDown/Up for Control Center")
    print("  - Longer menu wait times (0.5s)")

    print("\n🚀 Starting in 3 seconds...")
    time.sleep(3)

    try:
        # Step 1: Control Center with improved click
        print("\n📍 Step 1: Control Center (1236, 12)")
        pyautogui.moveTo(1236, 12, duration=0.2)
        await asyncio.sleep(0.1)

        # More deliberate click for menu bar
        pyautogui.mouseDown()
        await asyncio.sleep(0.05)
        pyautogui.mouseUp()

        print("   ✅ Clicked - waiting for menu...")
        await asyncio.sleep(0.5)  # Wait for menu to open

        # Step 2: Screen Mirroring
        print("\n📍 Step 2: Screen Mirroring (1396, 177)")
        pyautogui.moveTo(1396, 177, duration=0.2)
        await asyncio.sleep(0.1)
        pyautogui.click()

        print("   ✅ Clicked - waiting for submenu...")
        await asyncio.sleep(0.5)  # Wait for submenu

        # Step 3: Living Room TV
        print("\n📍 Step 3: Living Room TV (1223, 115)")
        pyautogui.moveTo(1223, 115, duration=0.2)
        await asyncio.sleep(0.1)
        pyautogui.click()

        print("   ✅ Clicked!")

        print("\n" + "="*60)
        print("✅ Test Complete - Check if display connected!")
        print("="*60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        # Clean up
        print("🧹 Pressing ESC to close menus...")
        pyautogui.press('escape')
        await asyncio.sleep(0.2)
        pyautogui.press('escape')

if __name__ == "__main__":
    asyncio.run(test_improved_clicking())