"""
High-Level Python Wrapper for ScreenCaptureKit Streaming
Async-friendly interface for continuous 60 FPS window capture

This is the "Ferrari Engine" for JARVIS Vision - native ScreenCaptureKit
streaming with GPU acceleration, replacing CLI-based fallbacks.
"""

import asyncio
import logging
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass
import numpy as np
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

try:
    import fast_capture_stream
    SCK_AVAILABLE = True
except ImportError as e:
    logging.warning(f"ScreenCaptureKit streaming not available: {e}")
    SCK_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class StreamingConfig:
    """High-level streaming configuration"""
    target_fps: int = 30  # 1-60 FPS
    max_buffer_size: int = 10  # Frame buffer size
    output_format: str = "raw"  # "raw" for zero-copy, "jpeg"/"png" for compression
    jpeg_quality: int = 85
    use_gpu_acceleration: bool = True
    drop_frames_on_overflow: bool = True
    capture_cursor: bool = False
    resolution_scale: float = 1.0  # 1.0 = native, 0.5 = half, 2.0 = retina

    def to_native_config(self) -> 'fast_capture_stream.StreamConfig':
        """Convert to C++ StreamConfig"""
        if not SCK_AVAILABLE:
            raise RuntimeError("ScreenCaptureKit not available")

        config = fast_capture_stream.StreamConfig()
        config.target_fps = self.target_fps
        config.max_buffer_size = self.max_buffer_size
        config.output_format = self.output_format
        config.jpeg_quality = self.jpeg_quality
        config.use_gpu_acceleration = self.use_gpu_acceleration
        config.drop_frames_on_overflow = self.drop_frames_on_overflow
        config.capture_cursor = self.capture_cursor
        config.resolution_scale = self.resolution_scale
        return config


class AsyncCaptureStream:
    """
    Async wrapper for CaptureStream
    Provides asyncio-compatible interface for continuous frame streaming
    """

    def __init__(self, window_id: int, config: Optional[StreamingConfig] = None):
        if not SCK_AVAILABLE:
            raise RuntimeError("ScreenCaptureKit not available - install extension first")

        self.window_id = window_id
        self.config = config or StreamingConfig()
        self._native_stream: Optional[fast_capture_stream.CaptureStream] = None
        self._running = False
        self._frame_callback: Optional[Callable] = None

        logger.info(f"AsyncCaptureStream created for window {window_id}")

    async def start(self) -> bool:
        """Start the capture stream"""
        if self._running:
            return True

        try:
            # Create native stream
            native_config = self.config.to_native_config()
            self._native_stream = fast_capture_stream.CaptureStream(
                self.window_id,
                native_config
            )

            # Start capture
            success = await asyncio.to_thread(self._native_stream.start)

            if success:
                self._running = True
                logger.info(f"Stream started for window {self.window_id} @ {self.config.target_fps} FPS")
            else:
                logger.error(f"Failed to start stream for window {self.window_id}")

            return success

        except Exception as e:
            logger.error(f"Error starting stream: {e}", exc_info=True)
            return False

    async def stop(self):
        """Stop the capture stream"""
        if not self._running:
            return

        try:
            await asyncio.to_thread(self._native_stream.stop)
            self._running = False
            logger.info(f"Stream stopped for window {self.window_id}")
        except Exception as e:
            logger.error(f"Error stopping stream: {e}", exc_info=True)

    def is_active(self) -> bool:
        """Check if stream is active"""
        return self._running and self._native_stream and self._native_stream.is_active()

    async def get_frame(self, timeout_ms: int = 100) -> Optional[Dict[str, Any]]:
        """
        Get latest frame (async blocking with timeout)

        Returns:
            Dict with frame data or None if timeout
            Frame dict contains:
                - 'image': numpy array (if raw format)
                - 'image_data': bytes (if compressed format)
                - 'width', 'height', 'channels'
                - 'frame_number', 'timestamp'
                - 'capture_latency_us', 'gpu_accelerated'
        """
        if not self.is_active():
            return None

        try:
            frame = await asyncio.to_thread(
                self._native_stream.get_frame,
                timeout_ms
            )
            return frame
        except Exception as e:
            logger.error(f"Error getting frame: {e}")
            return None

    async def try_get_frame(self) -> Optional[Dict[str, Any]]:
        """Get latest frame (non-blocking)"""
        if not self.is_active():
            return None

        try:
            frame = await asyncio.to_thread(self._native_stream.try_get_frame)
            return frame
        except Exception as e:
            logger.error(f"Error getting frame: {e}")
            return None

    async def get_all_frames(self) -> List[Dict[str, Any]]:
        """Get all available frames (drains buffer)"""
        if not self.is_active():
            return []

        try:
            frames = await asyncio.to_thread(self._native_stream.get_all_frames)
            return frames
        except Exception as e:
            logger.error(f"Error getting frames: {e}")
            return []

    async def get_stats(self) -> Dict[str, Any]:
        """Get stream statistics"""
        if not self._native_stream:
            return {}

        try:
            stats = await asyncio.to_thread(self._native_stream.get_stats)
            return {
                'total_frames': stats.total_frames,
                'dropped_frames': stats.dropped_frames,
                'actual_fps': stats.actual_fps,
                'avg_latency_ms': stats.avg_latency_ms,
                'min_latency_ms': stats.min_latency_ms,
                'max_latency_ms': stats.max_latency_ms,
                'current_buffer_size': stats.current_buffer_size,
                'peak_buffer_size': stats.peak_buffer_size,
                'bytes_processed': stats.bytes_processed,
                'is_active': stats.is_active
            }
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {}

    async def reset_stats(self):
        """Reset statistics"""
        if self._native_stream:
            await asyncio.to_thread(self._native_stream.reset_stats)

    async def update_config(self, config: StreamingConfig):
        """Update configuration (restarts stream)"""
        self.config = config
        if self._native_stream:
            native_config = config.to_native_config()
            await asyncio.to_thread(self._native_stream.update_config, native_config)

    async def get_window_info(self) -> Dict[str, Any]:
        """Get window information"""
        if not self._native_stream:
            return {}

        try:
            info = await asyncio.to_thread(self._native_stream.get_window_info)
            return {
                'window_id': info.window_id,
                'app_name': info.app_name,
                'window_title': info.window_title,
                'bundle_identifier': info.bundle_identifier,
                'x': info.x,
                'y': info.y,
                'width': info.width,
                'height': info.height,
                'is_visible': info.is_visible
            }
        except Exception as e:
            logger.error(f"Error getting window info: {e}")
            return {}

    async def __aenter__(self):
        """Async context manager entry"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.stop()


class AsyncStreamManager:
    """
    Async wrapper for StreamManager
    Manages multiple concurrent streams (God Mode)
    """

    def __init__(self):
        if not SCK_AVAILABLE:
            raise RuntimeError("ScreenCaptureKit not available")

        self._native_manager = fast_capture_stream.StreamManager()
        logger.info("AsyncStreamManager initialized")

    async def create_stream(self, window_id: int,
                          config: Optional[StreamingConfig] = None) -> str:
        """Create and start a new stream, returns stream ID"""
        try:
            native_config = (config or StreamingConfig()).to_native_config()
            stream_id = await asyncio.to_thread(
                self._native_manager.create_stream,
                window_id,
                native_config
            )
            logger.info(f"Created stream {stream_id} for window {window_id}")
            return stream_id
        except Exception as e:
            logger.error(f"Error creating stream: {e}", exc_info=True)
            raise

    async def create_stream_by_name(self, app_name: str,
                                   window_title: str = "",
                                   config: Optional[StreamingConfig] = None) -> str:
        """Create stream from window name"""
        try:
            native_config = (config or StreamingConfig()).to_native_config()
            stream_id = await asyncio.to_thread(
                self._native_manager.create_stream_by_name,
                app_name,
                window_title,
                native_config
            )
            logger.info(f"Created stream {stream_id} for {app_name}")
            return stream_id
        except Exception as e:
            logger.error(f"Error creating stream by name: {e}", exc_info=True)
            raise

    async def destroy_stream(self, stream_id: str):
        """Stop and destroy a stream"""
        try:
            await asyncio.to_thread(self._native_manager.destroy_stream, stream_id)
            logger.info(f"Destroyed stream {stream_id}")
        except Exception as e:
            logger.error(f"Error destroying stream: {e}")

    async def destroy_all_streams(self):
        """Stop all streams"""
        try:
            await asyncio.to_thread(self._native_manager.destroy_all_streams)
            logger.info("All streams destroyed")
        except Exception as e:
            logger.error(f"Error destroying all streams: {e}")

    async def get_frame(self, stream_id: str, timeout_ms: int = 100) -> Optional[Dict[str, Any]]:
        """Get frame from specific stream"""
        try:
            frame = await asyncio.to_thread(
                self._native_manager.get_frame,
                stream_id,
                timeout_ms
            )
            return frame
        except Exception as e:
            logger.error(f"Error getting frame from stream {stream_id}: {e}")
            return None

    async def get_all_frames(self, timeout_ms: int = 100) -> Dict[str, Dict[str, Any]]:
        """Get frames from all active streams"""
        try:
            frames = await asyncio.to_thread(
                self._native_manager.get_all_frames,
                timeout_ms
            )
            return frames
        except Exception as e:
            logger.error(f"Error getting all frames: {e}")
            return {}

    async def get_active_stream_ids(self) -> List[str]:
        """Get list of active stream IDs"""
        try:
            return await asyncio.to_thread(self._native_manager.get_active_stream_ids)
        except Exception as e:
            logger.error(f"Error getting active streams: {e}")
            return []

    async def get_stream_stats(self, stream_id: str) -> Dict[str, Any]:
        """Get statistics for specific stream"""
        try:
            stats = await asyncio.to_thread(self._native_manager.get_stream_stats, stream_id)
            return {
                'total_frames': stats.total_frames,
                'dropped_frames': stats.dropped_frames,
                'actual_fps': stats.actual_fps,
                'avg_latency_ms': stats.avg_latency_ms,
                'current_buffer_size': stats.current_buffer_size,
                'is_active': stats.is_active
            }
        except Exception as e:
            logger.error(f"Error getting stats for stream {stream_id}: {e}")
            return {}

    async def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all streams"""
        try:
            all_stats = await asyncio.to_thread(self._native_manager.get_all_stats)
            result = {}
            for stream_id, stats in all_stats.items():
                result[stream_id] = {
                    'total_frames': stats.total_frames,
                    'dropped_frames': stats.dropped_frames,
                    'actual_fps': stats.actual_fps,
                    'avg_latency_ms': stats.avg_latency_ms,
                    'is_active': stats.is_active
                }
            return result
        except Exception as e:
            logger.error(f"Error getting all stats: {e}")
            return {}

    async def set_max_concurrent_streams(self, max_streams: int):
        """Set maximum number of concurrent streams"""
        await asyncio.to_thread(self._native_manager.set_max_concurrent_streams, max_streams)


# Utility functions

def is_sck_available() -> bool:
    """Check if ScreenCaptureKit is available"""
    if not SCK_AVAILABLE:
        return False
    try:
        return fast_capture_stream.is_screencapturekit_available()
    except Exception:
        return False


def get_recommended_fps(width: int, height: int, gpu_available: bool = True) -> int:
    """Get recommended FPS for window size"""
    if not SCK_AVAILABLE:
        return 5  # Fallback FPS
    return fast_capture_stream.get_recommended_fps(width, height, gpu_available)


def estimate_stream_memory(config: StreamingConfig, width: int, height: int) -> int:
    """Estimate memory usage for stream"""
    if not SCK_AVAILABLE:
        return 0
    native_config = config.to_native_config()
    return fast_capture_stream.estimate_stream_memory(native_config, width, height)


# Example usage
if __name__ == "__main__":
    async def main():
        if not is_sck_available():
            print("❌ ScreenCaptureKit not available (requires macOS 12.3+)")
            return

        print("✅ ScreenCaptureKit available!")

        # Create stream manager
        manager = AsyncStreamManager()

        try:
            # Create stream for Terminal
            config = StreamingConfig(target_fps=30, max_buffer_size=5, output_format="raw")
            stream_id = await manager.create_stream_by_name("Terminal", config=config)

            print(f"Created stream: {stream_id}")

            # Get frames for 3 seconds
            for i in range(90):  # 30 FPS * 3 seconds
                frame = await manager.get_frame(stream_id, timeout_ms=50)
                if frame:
                    print(f"Frame {frame['frame_number']}: "
                          f"{frame['width']}x{frame['height']}, "
                          f"latency={frame['capture_latency_us']/1000:.2f}ms")
                await asyncio.sleep(1/30)  # 30 FPS

            # Show stats
            stats = await manager.get_stream_stats(stream_id)
            print(f"\nStats: {stats['total_frames']} frames @ {stats['actual_fps']:.1f} FPS, "
                  f"avg latency {stats['avg_latency_ms']:.2f}ms")

        finally:
            await manager.destroy_all_streams()

    asyncio.run(main())
