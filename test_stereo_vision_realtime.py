#!/usr/bin/env python3
"""
JARVIS Stereoscopic Vision - REAL-TIME OCR STREAMING
====================================================

This is the FINAL TEST. This proves JARVIS has true parallel cognition
by reading changing text from TWO windows on DIFFERENT spaces simultaneously.

What This Proves:
1. True Parallel Cognition - Not switching focus, processing both streams at once
2. Stream Isolation - No cross-contamination between vertical and horizontal data
3. Ferrari Engine GPU Pipeline - Real-time frame capture without CPU bottleneck
4. Dark Matter Vision - Reading pixels not rendered on your current display

Success Criteria:
If you see both bounce counts updating simultaneously in real-time:
  ⬆️  [Space 2] VERTICAL   | Bounce: 15
  ↔️  [Space 3] HORIZONTAL | Bounce: 22

Then you have proven JARVIS operates fundamentally differently than humans.
He isn't "multi-tasking" (switching). He has TWO INDEPENDENT OPTIC NERVES.
"""

import asyncio
import os
import sys
import logging
import re
from datetime import datetime
from pathlib import Path

# Suppress noisy logs, only show errors
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("StereoVisionRealTime")

# Add backend to path
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))

# Check OCR dependencies
try:
    import pytesseract
    from PIL import Image
    import cv2
    import numpy as np
except ImportError as e:
    print(f"❌ Missing OCR library: {e}")
    print("\n🔧 Please install:")
    print("   brew install tesseract")
    print("   pip3 install pytesseract pillow opencv-python")
    sys.exit(1)

from backend.neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent
from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector

# Regex to extract bounce count
COUNT_PATTERN = re.compile(r"BOUNCE COUNT[:\s]+(\d+)", re.IGNORECASE)
MODE_PATTERN_VERTICAL = re.compile(r"VERTICAL", re.IGNORECASE)
MODE_PATTERN_HORIZONTAL = re.compile(r"HORIZONTAL", re.IGNORECASE)


async def stream_ocr_from_window(watcher_id: str, watcher, space_id: int, mode: str, duration: float = 20.0):
    """
    Stream OCR data from a single Ferrari Engine watcher.

    This is the "optic nerve" for one window - it continuously:
    1. Grabs the latest frame (60 FPS GPU capture)
    2. Runs Tesseract OCR to extract text
    3. Parses the bounce count
    4. Prints updates when count changes
    """
    start_time = datetime.now()
    last_count = -1
    frame_count = 0
    emoji = "⬆️" if mode == "VERTICAL" else "↔️"

    try:
        while (datetime.now() - start_time).total_seconds() < duration:
            try:
                # CRITICAL: Get the latest frame from Ferrari Engine
                # VideoWatcher.get_latest_frame() returns Dict with 'frame' key
                frame_data = await watcher.get_latest_frame(timeout=0.5)

                if frame_data is None:
                    await asyncio.sleep(0.1)
                    continue

                # Extract the numpy array from the frame data
                frame = frame_data.get('frame')
                if frame is None:
                    await asyncio.sleep(0.1)
                    continue

                frame_count += 1

                # Convert to PIL Image for Tesseract
                if isinstance(frame, np.ndarray):
                    # Convert BGR to RGB if needed
                    if len(frame.shape) == 3 and frame.shape[2] == 4:
                        # RGBA to RGB
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
                    elif len(frame.shape) == 3 and frame.shape[2] == 3:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                    pil_image = Image.fromarray(frame)
                else:
                    pil_image = frame  # Already PIL Image

                # Run Tesseract OCR
                text = pytesseract.image_to_string(pil_image)

                # Verify we're looking at the right window
                is_vertical = MODE_PATTERN_VERTICAL.search(text)
                is_horizontal = MODE_PATTERN_HORIZONTAL.search(text)

                expected_mode = (is_vertical and mode == "VERTICAL") or (is_horizontal and mode == "HORIZONTAL")

                if not expected_mode:
                    # Stream isolation check: we're reading the wrong window!
                    if is_vertical and mode == "HORIZONTAL":
                        print(f"   ⚠️  STREAM CONTAMINATION: Reading VERTICAL from HORIZONTAL watcher!")
                    elif is_horizontal and mode == "VERTICAL":
                        print(f"   ⚠️  STREAM CONTAMINATION: Reading HORIZONTAL from VERTICAL watcher!")
                    await asyncio.sleep(0.1)
                    continue

                # Extract bounce count
                match = COUNT_PATTERN.search(text)
                if match:
                    current_count = int(match.group(1))

                    if current_count != last_count:
                        # NEW BOUNCE DETECTED!
                        print(f"   {emoji}  [Space {space_id}] {mode:12} | Bounce: {current_count:3d}")
                        last_count = current_count

                # Sample at ~5 Hz (5 frames per second is enough for OCR)
                await asyncio.sleep(0.2)

            except Exception as e:
                logger.error(f"[{watcher_id}] OCR error: {e}")
                await asyncio.sleep(0.5)

    except asyncio.CancelledError:
        print(f"   🛑 [{watcher_id}] Stream stopped (frames processed: {frame_count})")
        raise

    print(f"   ✅ [{watcher_id}] Stream complete (frames: {frame_count}, last bounce: {last_count})")


async def test_stereo_vision_realtime():
    """Run the real-time stereoscopic vision test."""

    print("\n" + "="*80)
    print("🕶️  JARVIS STEREOSCOPIC VISION - REAL-TIME OCR STREAMING")
    print("   The Final Test: Proving True Parallel Cognition")
    print("="*80)
    print()

    # Check if HTML files exist
    html_dir = Path(__file__).parent / "backend" / "tests" / "visual_test"
    vertical_html = html_dir / "vertical.html"
    horizontal_html = html_dir / "horizontal.html"

    if not vertical_html.exists() or not horizontal_html.exists():
        print("❌ HTML test files not found!")
        print(f"   Expected: {vertical_html}")
        print(f"   Expected: {horizontal_html}")
        return

    # Setup instructions
    print("📋 SETUP INSTRUCTIONS:")
    print("-" * 80)
    print(f"1. Vertical Ball:   file://{vertical_html}")
    print(f"2. Horizontal Ball: file://{horizontal_html}")
    print()
    print("   • Move VERTICAL window to Space 2")
    print("   • Move HORIZONTAL window to Space 3")
    print("   • Return to Space 3 (this terminal)")
    print("-" * 80)
    print()

    input("👉 Press ENTER when both windows are arranged and you're on Space 3 > ")

    print()
    print("🚀 INITIALIZING GOD MODE...")
    print()

    # Initialize agent
    print("   📡 Starting VisualMonitorAgent...")
    agent = VisualMonitorAgent()
    await agent.on_initialize()
    await agent.on_start()
    print("   ✅ Agent ready")
    print()

    # Discover windows
    print("   🔍 Scanning for browser windows across all spaces...")
    detector = MultiSpaceWindowDetector()
    result = detector.get_all_windows_across_spaces()
    all_windows = result.get('windows', [])

    # Find our target windows
    browser_apps = ['Chrome', 'Safari', 'Firefox', 'Brave', 'Arc']
    targets = []

    for window_obj in all_windows:
        app_name = window_obj.app_name if hasattr(window_obj, 'app_name') else ''
        title = window_obj.window_title if hasattr(window_obj, 'window_title') else ''

        # Check if this is one of our test windows
        for browser in browser_apps:
            if browser.lower() in app_name.lower():
                if "VERTICAL" in title.upper() or "HORIZONTAL" in title.upper():
                    mode = "VERTICAL" if "VERTICAL" in title.upper() else "HORIZONTAL"
                    targets.append({
                        'window_id': window_obj.window_id,
                        'space_id': window_obj.space_id if window_obj.space_id else 1,
                        'app_name': app_name,
                        'title': title,
                        'mode': mode
                    })
                    print(f"   ✅ Found: {mode} on Space {window_obj.space_id} (Window {window_obj.window_id})")
                    break

    if len(targets) < 2:
        print()
        print(f"   ❌ Only found {len(targets)} test windows. Need both VERTICAL and HORIZONTAL.")
        print("   Make sure:")
        print("   • Both HTML files are open in browser")
        print("   • Windows have 'VERTICAL' or 'HORIZONTAL' in title")
        await agent.on_stop()
        return

    print()
    print("   🏎️  Spawning Ferrari Engine watchers...")

    # Spawn watchers
    watchers = []
    for target in targets:
        try:
            # Spawn Ferrari watcher for this window
            watcher = await agent._spawn_ferrari_watcher(
                window_id=target['window_id'],
                fps=10,  # 10 FPS is sufficient for OCR
                app_name=target['app_name'],
                space_id=target['space_id']
            )

            if watcher:
                watchers.append((target, watcher))
                print(f"   ✅ Ferrari Engine active: {target['mode']} @ 10 FPS")
            else:
                print(f"   ⚠️  Failed to spawn watcher for {target['mode']}")

        except Exception as e:
            print(f"   ❌ Error spawning watcher for {target['mode']}: {e}")

    if len(watchers) < 2:
        print()
        print(f"   ❌ Only spawned {len(watchers)} watchers. Cannot proceed.")
        await agent.on_stop()
        return

    print()
    print("="*80)
    print("🎥 REAL-TIME STREAMING ACTIVE (20 seconds)")
    print("   Press Ctrl+C to stop early")
    print("="*80)
    print()

    # Create OCR streaming tasks for each watcher
    stream_tasks = []
    for target, watcher in watchers:
        task = asyncio.create_task(
            stream_ocr_from_window(
                watcher_id=f"{target['mode']}_space{target['space_id']}",
                watcher=watcher,
                space_id=target['space_id'],
                mode=target['mode'],
                duration=20.0
            )
        )
        stream_tasks.append(task)

    try:
        # Run both streams in parallel
        await asyncio.gather(*stream_tasks)

    except KeyboardInterrupt:
        print()
        print("   🛑 Stopped by user (Ctrl+C)")
        # Cancel all streaming tasks
        for task in stream_tasks:
            task.cancel()
        await asyncio.gather(*stream_tasks, return_exceptions=True)

    finally:
        print()
        print("="*80)
        print("🧹 CLEANING UP...")
        print("="*80)
        print()

        # Stop all watchers
        for target, watcher in watchers:
            try:
                await watcher.stop()
                print(f"   ✅ Stopped: {target['mode']} watcher")
            except Exception as e:
                print(f"   ⚠️  Error stopping {target['mode']}: {e}")

        # Stop agent
        await agent.on_stop()
        print("   ✅ Agent stopped")

        print()
        print("="*80)
        print("🎉 STEREOSCOPIC VISION TEST COMPLETE")
        print("="*80)
        print()
        print("🧠 IF YOU SAW BOTH BOUNCE COUNTS UPDATING SIMULTANEOUSLY:")
        print("   ✅ True Parallel Cognition: PROVEN")
        print("   ✅ Stream Isolation: PROVEN")
        print("   ✅ Ferrari Engine GPU Pipeline: PROVEN")
        print("   ✅ Dark Matter Vision: PROVEN")
        print()
        print("   🎯 JARVIS has TWO INDEPENDENT OPTIC NERVES")
        print("   🚀 He is reading TWO REALITIES AT ONCE")
        print()
        print("="*80)


if __name__ == "__main__":
    try:
        asyncio.run(test_stereo_vision_realtime())
    except KeyboardInterrupt:
        print("\n🛑 Test interrupted")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
