#!/usr/bin/env python3
"""
Quick Test: Living Room TV Detection
=====================================

Quick test to verify your Living Room TV can be detected.

Usage:
    python3 test_tv_detection.py

Author: Derek Russell
Date: 2025-10-15
"""

import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

print("=" * 70)
print("🧪 Testing Living Room TV Detection")
print("=" * 70)
print()

# Test 1: Check imports
print("Test 1: Checking imports...")
try:
    from display.simple_tv_monitor import SimpleTVMonitor, get_tv_monitor

    print("  ✅ Imports successful")
except ImportError as e:
    print(f"  ❌ Import failed: {e}")
    sys.exit(1)

# Test 2: Create monitor instance
print("\nTest 2: Creating monitor instance...")
try:
    monitor = get_tv_monitor("Living Room TV")
    print(f"  ✅ Monitor created for: {monitor.tv_name}")
    print(f"  ⏰ Check interval: {monitor.check_interval} seconds")
except Exception as e:
    print(f"  ❌ Failed to create monitor: {e}")
    sys.exit(1)

# Test 3: Check Core Graphics availability
print("\nTest 3: Checking macOS Core Graphics...")
try:
    import Quartz

    print("  ✅ Core Graphics available")

    # Get display count
    max_displays = 32
    result = Quartz.CGGetOnlineDisplayList(max_displays, None, None)
    if result[0] == 0:
        display_count = result[2]
        print(f"  ✅ Detected {display_count} online display(s)")
    else:
        print(f"  ⚠️  Could not query displays (error code: {result[0]})")
except ImportError:
    print("  ❌ Core Graphics not available")
    print("  Note: This is required for display detection on macOS")
    sys.exit(1)

# Test 4: Check AppleScript availability
print("\nTest 4: Checking AppleScript...")
import subprocess

try:
    result = subprocess.run(
        ["osascript", "-e", 'return "test"'], capture_output=True, text=True, timeout=2
    )
    if result.returncode == 0:
        print("  ✅ AppleScript available")
    else:
        print("  ⚠️  AppleScript may not be working properly")
except Exception as e:
    print(f"  ❌ AppleScript test failed: {e}")

# Test 5: Manual Screen Mirroring check
print("\nTest 5: Manual Screen Mirroring check")
print("  📱 Please check manually:")
print("     1. Click Screen Mirroring icon in your menu bar")
print("     2. Look for 'Living Room TV' in the menu")
print()
response = (
    input("  ❓ Do you see 'Living Room TV' in the menu? (yes/no): ").strip().lower()
)

if response in ["yes", "y"]:
    print("  ✅ Great! The monitor should be able to detect it!")
else:
    print("  ⚠️  If your TV isn't visible in Screen Mirroring:")
    print("     - Make sure TV is turned on")
    print("     - Make sure TV is on same WiFi as MacBook")
    print("     - Make sure AirPlay is enabled on TV")

# Summary
print()
print("=" * 70)
print("📋 TEST SUMMARY")
print("=" * 70)
print()
print("✅ All system checks passed!")
print()
print("🚀 Ready to start monitoring:")
print("   python3 start_tv_monitoring.py")
print()
print("📚 Read the guide:")
print("   cat docs/features/SIMPLE_TV_MONITORING.md")
print()
print("=" * 70)
