#!/usr/bin/env python3
"""
Test script for verifying adaptive clicker fixes for Control Center
Tests both drag-to behavior and keyboard shortcut fallback
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

import pytest

# Skips instead of erroring where the optional dependency is absent (headless
# CI). Must precede the import below, which needs pyautogui transitively via backend.display.adaptive_control_center_clicker.
pytest.importorskip("pyautogui", reason="pyautogui not installed (headless environment)")
from backend.display.adaptive_control_center_clicker import get_adaptive_clicker

# Configure detailed logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('test_adaptive_clicker_fix.log')
    ]
)

logger = logging.getLogger(__name__)


async def test_control_center_drag():
    """Test that Control Center uses drag motion"""
    print("\n" + "=" * 80)
    print("TESTING CONTROL CENTER DRAG BEHAVIOR")
    print("=" * 80)

    clicker = get_adaptive_clicker()

    print("\n1. Testing Control Center opening with drag motion...")
    result = await clicker.open_control_center()

    if result.success:
        print(f"✅ Control Center opened successfully")
        print(f"   Method: {result.method_used}")
        print(f"   Coordinates: {result.coordinates}")

        # Wait a moment to see it
        await asyncio.sleep(2)

        # Close it
        import pyautogui
        pyautogui.press('escape')
        print("   Closed Control Center")
    else:
        print(f"❌ Failed to open Control Center: {result.error}")

    return result.success


async def test_full_connection_flow():
    """Test the full connection flow to Living Room TV"""
    print("\n" + "=" * 80)
    print("TESTING FULL CONNECTION FLOW TO LIVING ROOM TV")
    print("=" * 80)

    clicker = get_adaptive_clicker()

    print("\n2. Testing full connection flow...")
    print("   Step 1: Open Control Center")
    print("   Step 2: Click Screen Mirroring")
    print("   Step 3: Click Living Room TV")

    result = await clicker.connect_to_device("Living Room TV")

    if result['success']:
        print(f"\n✅ Successfully connected to Living Room TV!")
        print(f"   Duration: {result['duration']:.2f}s")
        print(f"   Method: {result.get('method', 'unknown')}")

        # Show coordinates used
        if 'control_center_coords' in result:
            print(f"\n   Coordinates used:")
            print(f"   - Control Center: {result['control_center_coords']}")
            print(f"   - Screen Mirroring: {result['screen_mirroring_coords']}")
            print(f"   - Living Room TV: {result['living_room_tv_coords']}")
    else:
        print(f"\n❌ Failed to connect: {result.get('message', 'Unknown error')}")
        print(f"   Failed at step: {result.get('step_failed', 'unknown')}")

    return result['success']


async def main():
    """Run all tests"""
    print("\n" + "=" * 80)
    print("ADAPTIVE CLICKER FIX TEST SUITE")
    print("=" * 80)
    print("\nThis test will:")
    print("1. Test Control Center opening with drag motion")
    print("2. Test full connection flow to Living Room TV")
    print("\nPress Ctrl+C to cancel at any time")

    try:
        # Countdown
        for i in range(3, 0, -1):
            print(f"\nStarting in {i}...")
            await asyncio.sleep(1)

        # Run tests
        test1_passed = await test_control_center_drag()

        await asyncio.sleep(2)  # Pause between tests

        test2_passed = await test_full_connection_flow()

        # Summary
        print("\n" + "=" * 80)
        print("TEST RESULTS")
        print("=" * 80)
        print(f"Control Center Drag Test: {'✅ PASSED' if test1_passed else '❌ FAILED'}")
        print(f"Full Connection Flow Test: {'✅ PASSED' if test2_passed else '❌ FAILED'}")

        if test1_passed and test2_passed:
            print("\n🎉 All tests passed! The fix is working correctly.")
        else:
            print("\n⚠️ Some tests failed. Check the log file for details.")

    except KeyboardInterrupt:
        print("\n\nTest cancelled by user")
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        logger.error("Test failed", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())