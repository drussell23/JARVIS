#!/usr/bin/env python3
"""
Test script to verify cached coordinates are being used properly
"""
import asyncio
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import pytest

# Skips instead of erroring where the optional dependency is absent (headless
# CI). Must precede the import below, which needs pyautogui transitively via backend.display.adaptive_control_center_clicker.
pytest.importorskip("pyautogui", reason="pyautogui not installed (headless environment)")
from backend.display.adaptive_control_center_clicker import AdaptiveControlCenterClicker, CachedDetection, CoordinateCache

async def test_cache_detection():
    """Test that cached coordinates are detected and used"""

    print("\n" + "="*60)
    print("Testing Cached Coordinate Detection")
    print("="*60 + "\n")

    # Create a cache instance
    print("Creating coordinate cache...")
    cache = CoordinateCache()

    # Check what's in the cache
    print("\n--- Cache Contents ---")
    control_center = cache.get("control_center")
    if control_center:
        print(f"✅ Control Center in cache: {control_center.coordinates}")
        print(f"   Confidence: {control_center.confidence}")
        print(f"   Method: {control_center.method}")
    else:
        print("❌ Control Center NOT in cache")

    # Create cached detection
    print("\n--- Testing CachedDetection.detect() ---")
    cached_detector = CachedDetection(cache)

    result = await cached_detector.detect("control_center", context=None)

    print(f"\nDetection result:")
    print(f"  Success: {result.success}")
    print(f"  Coordinates: {result.coordinates}")
    print(f"  Confidence: {result.confidence:.2%}")
    print(f"  Method: {result.method}")

    # Check coordinates (handle both tuple and list)
    coords = tuple(result.coordinates) if result.coordinates else None
    if result.success and coords == (1235, 10):
        print("\n✅ SUCCESS: Cached detection returned correct coordinates (1235, 10)")
    elif result.success:
        print(f"\n❌ WRONG: Cached detection returned {result.coordinates} instead of (1235, 10)")
    else:
        print(f"\n❌ FAILED: Cached detection did not succeed: {result.error}")

    print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    asyncio.run(test_cache_detection())
