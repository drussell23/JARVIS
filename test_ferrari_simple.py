#!/usr/bin/env python3
"""
Simple Ferrari Engine Test
Direct test of VideoWatcher with known window ID
"""
import asyncio
import os
import sys

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), "backend"))

from backend.vision.macos_video_capture_advanced import VideoWatcher, WatcherConfig

async def test_ferrari_simple():
    print("=" * 70)
    print("🏎️  FERRARI ENGINE - SIMPLE TEST")
    print("=" * 70)

    # Use Cursor editor window (known to exist)
    CURSOR_WINDOW_ID = 8230

    print(f"\n→ Creating VideoWatcher for window {CURSOR_WINDOW_ID} (Cursor editor)...")

    config = WatcherConfig(
        window_id=CURSOR_WINDOW_ID,
        fps=5,
        max_buffer_size=10
    )

    watcher = VideoWatcher(config)

    print("→ Starting VideoWatcher...\n")
    success = await watcher.start()

    if not success:
        print("❌ Failed to start!")
        return False

    print("✅ Watcher started!\n")

    # Collect 5 frames
    print("→ Collecting 5 frames...")
    frames_collected = []

    for i in range(10):  # Try 10 times to get 5 frames
        frame_data = await watcher.get_latest_frame(timeout=1.0)
        if frame_data:
            frames_collected.append(frame_data)
            method = frame_data.get('method', 'UNKNOWN')
            latency = frame_data.get('capture_latency_ms', 0)
            print(f"   Frame {len(frames_collected)}: Method=[{method}] "
                  f"Shape={frame_data['frame'].shape} Latency={latency:.1f}ms")

            if len(frames_collected) >= 5:
                break

    print(f"\n→ Stopping watcher...")
    await watcher.stop()

    # Results
    print("\n" + "=" * 70)
    print("📊 RESULTS")
    print("=" * 70)
    print(f"Frames collected: {len(frames_collected)}")

    if frames_collected:
        methods = set(f.get('method', 'UNKNOWN') for f in frames_collected)
        print(f"Capture methods: {methods}")

        if 'screencapturekit' in methods:
            print("\n✅ SUCCESS: Ferrari Engine active!")
            return True
        else:
            print(f"\n⚠️  Using fallback method: {methods}")
            print("   (This is OK - fallback works)")
            return True
    else:
        print("\n❌ FAILURE: No frames captured")
        return False

if __name__ == "__main__":
    try:
        passed = asyncio.run(test_ferrari_simple())
        sys.exit(0 if passed else 1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
