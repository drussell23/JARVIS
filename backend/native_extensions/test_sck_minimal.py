#!/usr/bin/env python3
"""
Minimal ScreenCaptureKit Streaming Test
Isolate the crash point with minimal code
"""

import asyncio
import sys
from pathlib import Path
import logging

sys.path.insert(0, str(Path(__file__).parent))

try:
    import fast_capture_stream
    import fast_capture
except ImportError as e:
    print(f"❌ Import failed: {e}")
    sys.exit(1)

from macos_sck_stream import AsyncCaptureStream, StreamingConfig

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def minimal_test():
    """Minimal test to isolate crash"""
    print("\n" + "="*70)
    print("🔬 MINIMAL STREAMING TEST - Isolate Crash Point")
    print("="*70)

    # Get a window
    engine = fast_capture.FastCaptureEngine()
    windows = engine.get_visible_windows()

    if not windows:
        print("❌ No windows found!")
        return False

    # Pick first suitable window
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
    print(f"   Size: {window.width}x{window.height}")

    # Create stream
    config = StreamingConfig(
        target_fps=10,  # Start low
        max_buffer_size=5,
        output_format="raw",
        use_gpu_acceleration=True,
        drop_frames_on_overflow=True
    )

    print("\n🔧 Creating stream...")
    stream = AsyncCaptureStream(window.window_id, config)

    try:
        print("🚀 Starting stream...")
        success = await stream.start()

        if not success:
            print("❌ Failed to start stream!")
            return False

        print("✅ Stream started!")
        print("⏳ Waiting 1 second for stabilization...")
        await asyncio.sleep(1)

        print("\n📦 Attempting to get first frame...")
        frame = await stream.get_frame(timeout_ms=1000)

        if frame:
            print(f"✅ Got frame!")
            print(f"   Frame number: {frame.get('frame_number', 'N/A')}")
            print(f"   Width: {frame.get('width', 'N/A')}")
            print(f"   Height: {frame.get('height', 'N/A')}")
            print(f"   Format: {frame.get('format', 'N/A')}")
            print(f"   Latency: {frame.get('capture_latency_us', 0) / 1000.0:.2f}ms")

            # Check if image data is present
            has_image = 'image' in frame
            has_image_data = 'image_data' in frame
            print(f"   Has image array: {has_image}")
            print(f"   Has image_data bytes: {has_image_data}")

            if has_image:
                import numpy as np
                img = frame['image']
                print(f"   Image shape: {img.shape}")
                print(f"   Image dtype: {img.dtype}")
        else:
            print("❌ No frame received!")

        print("\n🛑 Stopping stream...")
        await stream.stop()
        print("✅ Stream stopped cleanly!")

        return True

    except Exception as e:
        logger.error(f"Test error: {e}", exc_info=True)
        return False
    finally:
        if stream.is_active():
            await stream.stop()


if __name__ == "__main__":
    try:
        passed = asyncio.run(minimal_test())
        sys.exit(0 if passed else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
