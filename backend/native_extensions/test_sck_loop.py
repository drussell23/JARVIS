#!/usr/bin/env python3
"""
Multi-Frame Loop Test - Find the crash point
"""

import asyncio
import sys
import time
from pathlib import Path
import logging
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

try:
    import fast_capture_stream
    import fast_capture
except ImportError as e:
    print(f"❌ Import failed: {e}")
    sys.exit(1)

from macos_sck_stream import AsyncCaptureStream, StreamingConfig

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def loop_test():
    """Test capturing multiple frames in a loop"""
    print("\n" + "="*70)
    print("🔁 MULTI-FRAME LOOP TEST")
    print("="*70)

    # Get a window
    engine = fast_capture.FastCaptureEngine()
    windows = engine.get_visible_windows()

    window = None
    for w in windows:
        if w.width >= 100 and w.height >= 100:
            window = w
            break

    if not window:
        print("❌ No suitable window found!")
        return False

    print(f"\n✅ Selected: {window.app_name} - {window.window_title}")
    print(f"   Window ID: {window.window_id}")

    # Create stream
    config = StreamingConfig(
        target_fps=30,
        max_buffer_size=10,
        output_format="raw",
        use_gpu_acceleration=True,
        drop_frames_on_overflow=True
    )

    stream = AsyncCaptureStream(window.window_id, config)

    try:
        print("\n🚀 Starting stream...")
        success = await stream.start()
        if not success:
            print("❌ Failed to start stream!")
            return False

        print("✅ Stream started!")
        await asyncio.sleep(0.5)

        # Capture 50 frames
        num_frames = 50
        print(f"\n📦 Capturing {num_frames} frames...")

        frames_received = 0
        frames_missed = 0
        latencies = []

        start_time = time.time()

        for i in range(num_frames):
            try:
                frame = await stream.get_frame(timeout_ms=100)

                if frame:
                    frames_received += 1

                    # Extract latency safely
                    latency_us = frame.get('capture_latency_us', 0)
                    latency_ms = latency_us / 1000.0
                    latencies.append(latency_ms)

                    # Get frame size safely
                    if 'image' in frame:
                        img = frame['image']
                        if isinstance(img, np.ndarray):
                            frame_size = img.nbytes
                        else:
                            frame_size = 0
                    elif 'image_data' in frame:
                        frame_size = len(frame['image_data'])
                    else:
                        frame_size = 0

                    # Print progress every 10 frames
                    if frames_received % 10 == 0:
                        elapsed = time.time() - start_time
                        current_fps = frames_received / elapsed if elapsed > 0 else 0
                        print(f"  Frame {frames_received:3d}: {current_fps:5.1f} FPS, "
                              f"{latency_ms:6.2f}ms latency, {frame_size/1024:7.1f} KB")
                else:
                    frames_missed += 1

                # Pace to target FPS
                await asyncio.sleep(1.0 / 30)

            except Exception as e:
                logger.error(f"Error on frame {i}: {e}", exc_info=True)
                frames_missed += 1

        # Calculate stats
        total_time = time.time() - start_time
        actual_fps = frames_received / total_time

        print(f"\n📊 Results:")
        print(f"   Frames received: {frames_received}/{num_frames}")
        print(f"   Frames missed: {frames_missed}")
        print(f"   Actual FPS: {actual_fps:.2f}")

        if latencies:
            avg_latency = np.mean(latencies)
            min_latency = np.min(latencies)
            max_latency = np.max(latencies)
            print(f"   Latency - Avg: {avg_latency:.2f}ms, Min: {min_latency:.2f}ms, Max: {max_latency:.2f}ms")

        print("\n🛑 Stopping stream...")
        await stream.stop()
        print("✅ Test completed successfully!")

        return True

    except Exception as e:
        logger.error(f"Test error: {e}", exc_info=True)
        return False
    finally:
        if stream.is_active():
            await stream.stop()


if __name__ == "__main__":
    try:
        passed = asyncio.run(loop_test())
        sys.exit(0 if passed else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
