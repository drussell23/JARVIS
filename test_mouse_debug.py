#!/usr/bin/env python3
"""
Debug script to see what coordinates are actually being used
"""
import sys
import os
import pytest

# Skips instead of erroring where the optional dependency is absent (headless
# CI). Must precede the import below, which needs pyautogui directly.
pytest.importorskip("pyautogui", reason="pyautogui not installed (headless environment)")
import pyautogui
import time

sys.path.insert(0, os.path.dirname(__file__))

def test_coordinates():
    print("\n" + "="*80)
    print("MOUSE COORDINATE DEBUG TEST")
    print("="*80 + "\n")

    # Test what PyAutoGUI thinks the screen size is
    width, height = pyautogui.size()
    print(f"PyAutoGUI Screen Size: {width} x {height}")

    # Test current mouse position
    x, y = pyautogui.position()
    print(f"Current Mouse Position: ({x}, {y})")

    # Test moving to Control Center coordinates
    print("\n" + "-"*40)
    print("Testing Control Center coordinates:")
    print("Target: (1236, 12)")
    print("-"*40)

    print("\nMoving mouse to (1236, 12) in 2 seconds...")
    print("Watch where the mouse actually goes!")
    time.sleep(2)

    pyautogui.moveTo(1236, 12, duration=1)

    # Get actual position after move
    actual_x, actual_y = pyautogui.position()
    print(f"\nAfter moveTo(1236, 12):")
    print(f"  Actual position: ({actual_x}, {actual_y})")

    if actual_x != 1236 or actual_y != 12:
        print(f"  ❌ MISMATCH! Expected (1236, 12), got ({actual_x}, {actual_y})")
        print(f"  X ratio: {actual_x / 1236:.2f}")
        print(f"  Y ratio: {actual_y / 12:.2f}")
    else:
        print(f"  ✅ Correct position!")

    # Test with the problematic coordinates
    print("\n" + "-"*40)
    print("Testing if coordinates get doubled:")
    print("-"*40)

    # Test if 1236 gets doubled to ~2472
    test_x = 1236
    test_y = 12

    print(f"\nMoving to ({test_x}, {test_y})...")
    pyautogui.moveTo(test_x, test_y, duration=0.5)

    actual_x, actual_y = pyautogui.position()
    print(f"Result: ({actual_x}, {actual_y})")

    # Check for doubling
    if abs(actual_x - test_x * 2) < 10:  # Within 10 pixels of double
        print(f"❌ COORDINATE DOUBLING DETECTED!")
        print(f"   Expected: {test_x}, Got: {actual_x} (≈ {test_x} × 2)")

    print("\n" + "="*80 + "\n")

if __name__ == "__main__":
    test_coordinates()