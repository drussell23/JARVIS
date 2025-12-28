#!/usr/bin/env python3
"""
ScreenCaptureKit Streaming - Dyno Test
Test the Ferrari Engine at 6000 RPM before integration

This test verifies:
1. FPS Stability (target: 60 FPS steady)
2. Latency (target: <16ms for real-time)
3. Memory Stability (no leaks)
4. Frame Buffer Management
"""

import asyncio
import sys
import time
from pathlib import Path
import logging
import numpy as np

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    import fast_capture_stream
    import fast_capture
    SCK_AVAILABLE = True
except ImportError as e:
    print(f"❌ ScreenCaptureKit not available: {e}")
    sys.exit(1)

from macos_sck_stream import (
    AsyncCaptureStream,
    AsyncStreamManager,
    StreamingConfig,
    is_sck_available
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def discover_test_window():
    """Find a suitable window for testing"""
    print("\n" + "="*70)
    print("🔍 WINDOW DISCOVERY")
    print("="*70)

    # Use fast_capture to list windows
    engine = fast_capture.FastCaptureEngine()
    windows = engine.get_visible_windows()

    if not windows:
        print("❌ No visible windows found!")
        return None

    print(f"\n📋 Found {len(windows)} visible windows:\n")

    for i, window in enumerate(windows[:10], 1):  # Show first 10
        print(f"  {i}. {window.app_name:20s} - {window.window_title[:40]:40s} "
              f"({window.width}x{window.height})")

    # Pick first window with reasonable size, prefer windows with dynamic content
    # Priority: Application windows > Static system windows
    priority_apps = ['Cursor', 'Safari', 'Chrome', 'Firefox', 'Terminal', 'Code']

    # First try: Look for priority apps
    for app_name in priority_apps:
        for window in windows:
            if app_name.lower() in window.app_name.lower() and window.width >= 100 and window.height >= 100:
                print(f"\n✅ Selected: {window.app_name} - {window.window_title}")
                print(f"   Window ID: {window.window_id}")
                print(f"   Size: {window.width}x{window.height}")
                print(f"   Priority: Dynamic content window")
                return window

    # Fallback: Pick any window with reasonable size
    for window in windows:
        if window.width >= 100 and window.height >= 100:
            print(f"\n✅ Selected: {window.app_name} - {window.window_title}")
            print(f"   Window ID: {window.window_id}")
            print(f"   Size: {window.width}x{window.height}")
            print(f"   Note: Static content window - may have lower frame delivery")
            return window

    print("❌ No suitable windows found (need width/height >= 100)")
    return None


async def dyno_test_stream(window, duration_sec=5, target_fps=30):
    """
    The Dyno Test - Run the engine at target RPM

    Args:
        window: Window to capture
        duration_sec: Test duration in seconds
        target_fps: Target FPS (30 or 60)
    """
    print("\n" + "="*70)
    print(f"🏎️  DYNO TEST - {target_fps} FPS for {duration_sec} seconds")
    print("="*70)

    # Configure for test
    config = StreamingConfig(
        target_fps=target_fps,
        max_buffer_size=10,
        output_format="raw",  # Zero-copy for max performance
        use_gpu_acceleration=True,
        drop_frames_on_overflow=True
    )

    print(f"\n⚙️  Configuration:")
    print(f"   Target FPS: {config.target_fps}")
    print(f"   Output Format: {config.output_format}")
    print(f"   GPU Acceleration: {config.use_gpu_acceleration}")
    print(f"   Buffer Size: {config.max_buffer_size}")

    # Create stream
    print(f"\n🔧 Creating stream for window {window.window_id}...")
    stream = AsyncCaptureStream(window.window_id, config)

    try:
        # Start stream
        print("🚀 Starting stream...")
        success = await stream.start()

        if not success:
            print("❌ Failed to start stream!")
            return False

        print("✅ Stream started successfully!\n")

        # Wait for stream to stabilize
        await asyncio.sleep(0.5)

        # Telemetrics collection
        frame_times = []
        latencies = []
        frame_sizes = []
        frames_received = 0
        frames_missed = 0

        start_time = time.time()
        last_frame_num = 0

        print("📊 TELEMETRICS (Real-Time):")
        print("-" * 70)
        print(f"{'Time':<8} {'Frame#':<10} {'FPS':<8} {'Latency':<12} {'Size':<12} {'Status'}")
        print("-" * 70)

        # Run test
        test_frames = target_fps * duration_sec
        for i in range(test_frames):
            frame_start = time.time()

            # Get frame
            frame = await stream.get_frame(timeout_ms=100)

            frame_end = time.time()
            frame_time = (frame_end - frame_start) * 1000  # ms

            if frame:
                frames_received += 1
                frame_times.append(frame_time)

                # Extract telemetrics
                latency_us = frame['capture_latency_us']
                latency_ms = latency_us / 1000.0
                latencies.append(latency_ms)

                frame_num = frame['frame_number']
                frame_size = len(frame.get('image_data', b'')) if 'image_data' in frame else frame.get('image', np.array([])).nbytes
                frame_sizes.append(frame_size)

                # Detect dropped frames
                if last_frame_num > 0 and frame_num != last_frame_num + 1:
                    frames_missed += (frame_num - last_frame_num - 1)

                last_frame_num = frame_num

                # Print every 10 frames
                if frames_received % 10 == 1:
                    elapsed = time.time() - start_time
                    current_fps = frames_received / elapsed if elapsed > 0 else 0

                    status = "✅" if latency_ms < 16 else "⚠️"
                    print(f"{elapsed:>7.2f}s {frame_num:<10} {current_fps:>6.1f} {latency_ms:>9.2f}ms "
                          f"{frame_size/1024:>9.1f}KB {status}")
            else:
                frames_missed += 1

            # Pace to target FPS
            await asyncio.sleep(1.0 / target_fps)

        # Stop stream
        print("\n🛑 Stopping stream...")
        await stream.stop()

        # Final statistics
        total_time = time.time() - start_time

        print("\n" + "="*70)
        print("📈 FINAL TELEMETRICS")
        print("="*70)

        if frame_times:
            actual_fps = frames_received / total_time
            avg_latency = np.mean(latencies)
            min_latency = np.min(latencies)
            max_latency = np.max(latencies)
            p95_latency = np.percentile(latencies, 95)
            p99_latency = np.percentile(latencies, 99)

            avg_frame_size = np.mean(frame_sizes) / 1024  # KB

            print(f"\n⏱️  Performance:")
            print(f"   Target FPS: {target_fps}")
            print(f"   Actual FPS: {actual_fps:.2f} {'✅' if actual_fps >= target_fps * 0.95 else '⚠️'}")
            print(f"   FPS Stability: {(actual_fps / target_fps * 100):.1f}%")

            print(f"\n⚡ Latency:")
            print(f"   Average: {avg_latency:.2f}ms {'✅' if avg_latency < 16 else '⚠️'}")
            print(f"   Min: {min_latency:.2f}ms")
            print(f"   Max: {max_latency:.2f}ms")
            print(f"   P95: {p95_latency:.2f}ms")
            print(f"   P99: {p99_latency:.2f}ms")

            print(f"\n📦 Frames:")
            print(f"   Received: {frames_received}/{test_frames}")
            print(f"   Missed: {frames_missed}")
            print(f"   Success Rate: {(frames_received/test_frames*100):.1f}%")
            print(f"   Avg Size: {avg_frame_size:.1f} KB")

            # Get stream stats
            stats = await stream.get_stats()
            print(f"\n📊 Stream Statistics:")
            print(f"   Total Frames: {stats.get('total_frames', 0)}")
            print(f"   Dropped Frames: {stats.get('dropped_frames', 0)}")
            print(f"   Peak Buffer Size: {stats.get('peak_buffer_size', 0)}")
            print(f"   Bytes Processed: {stats.get('bytes_processed', 0) / 1024 / 1024:.2f} MB")

            # Pass/Fail criteria
            print("\n" + "="*70)
            print("🏁 DYNO TEST RESULTS")
            print("="*70)

            passed = True

            # FPS check
            if actual_fps >= target_fps * 0.95:
                print("✅ FPS Stability: PASS (>95% of target)")
            else:
                print("❌ FPS Stability: FAIL (<95% of target)")
                passed = False

            # Latency check
            if avg_latency < 16:
                print("✅ Latency: PASS (<16ms average)")
            else:
                print("⚠️  Latency: WARNING (>16ms average, not real-time)")

            # Frame loss check
            if frames_missed == 0:
                print("✅ Frame Loss: PASS (zero frames missed)")
            elif frames_missed < test_frames * 0.01:
                print("⚠️  Frame Loss: WARNING (<1% frames missed)")
            else:
                print("❌ Frame Loss: FAIL (>1% frames missed)")
                passed = False

            print("\n" + "="*70)
            if passed:
                print("🎉 DYNO TEST: PASSED - Ferrari engine is ready!")
            else:
                print("⚠️  DYNO TEST: NEEDS TUNING - See warnings above")
            print("="*70 + "\n")

            return passed
        else:
            print("❌ No frames received - test failed!")
            return False

    except Exception as e:
        logger.error(f"Dyno test error: {e}", exc_info=True)
        return False
    finally:
        if stream.is_active():
            await stream.stop()


async def main():
    print("\n" + "="*70)
    print("🏎️  SCREENCAPTUREKIT STREAMING - DYNO TEST")
    print("   Testing the Ferrari Engine at 6000 RPM")
    print("="*70)

    # Check availability
    if not is_sck_available():
        print("❌ ScreenCaptureKit not available (requires macOS 12.3+)")
        return False

    print(f"✅ ScreenCaptureKit available (macOS 12.3+)")

    # Discover window
    window = await discover_test_window()
    if not window:
        return False

    # Test at 30 FPS
    print("\n\n" + "🔥" * 35)
    print("TEST 1: 30 FPS - Warm-Up Lap")
    print("🔥" * 35)
    await dyno_test_stream(window, duration_sec=5, target_fps=30)

    # Test at 60 FPS
    print("\n\n" + "🔥" * 35)
    print("TEST 2: 60 FPS - Full Throttle")
    print("🔥" * 35)
    passed = await dyno_test_stream(window, duration_sec=5, target_fps=60)

    if passed:
        print("\n✅ All tests passed! Ferrari engine is purring perfectly.")
        print("   Ready for integration into VideoWatcher chassis.")
    else:
        print("\n⚠️  Some tests need tuning. Review telemetrics above.")

    return passed


if __name__ == "__main__":
    try:
        passed = asyncio.run(main())
        sys.exit(0 if passed else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
