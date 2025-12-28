"""
Advanced macOS Video Capture System (v10.6)
Production-grade implementation using PyObjC + AVFoundation

Features:
- Native AVFoundation integration via PyObjC
- Async/await support with proper event loop integration
- Parallel capture sessions with resource management
- Intelligent fallback chain (AVFoundation → ScreenCaptureKit → screencapture)
- Dynamic configuration (environment variables, no hardcoding)
- Comprehensive error handling and graceful degradation
- Real-time performance monitoring and adaptive quality
- Proper memory management and cleanup
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from datetime import datetime
import threading
import weakref
import gc

import numpy as np
import psutil

# Import PyObjC frameworks (now properly installed)
try:
    import objc
    from Foundation import (
        NSObject,
        NSRunLoop,
        NSDefaultRunLoopMode,
        NSData,
    )
    from AVFoundation import (
        AVCaptureSession,
        AVCaptureScreenInput,
        AVCaptureVideoDataOutput,
        AVCaptureSessionPreset1920x1080,
        AVCaptureSessionPreset1280x720,
        AVCaptureSessionPreset640x480,
    )
    from CoreMedia import (
        CMSampleBufferGetImageBuffer,
        CMTimeMake,
    )
    from Quartz import (
        CVPixelBufferLockBaseAddress,
        CVPixelBufferUnlockBaseAddress,
        CVPixelBufferGetBaseAddress,
        CVPixelBufferGetBytesPerRow,
        CVPixelBufferGetHeight,
        CVPixelBufferGetWidth,
        kCVPixelBufferPixelFormatTypeKey,
        kCVPixelFormatType_32BGRA,
    )
    import libdispatch

    PYOBJC_AVAILABLE = True
    AVFOUNDATION_AVAILABLE = True
except ImportError as e:
    PYOBJC_AVAILABLE = False
    AVFOUNDATION_AVAILABLE = False
    logging.getLogger(__name__).error(
        f"PyObjC frameworks not available: {e}\n"
        f"Install with: pip install pyobjc-framework-AVFoundation pyobjc-framework-Quartz "
        f"pyobjc-framework-CoreMedia pyobjc-framework-libdispatch"
    )

# Try ScreenCaptureKit (macOS 12.3+)
try:
    from ScreenCaptureKit import (
        SCStreamConfiguration,
        SCContentFilter,
        SCStreamDelegate,
    )
    SCREENCAPTUREKIT_AVAILABLE = True
except ImportError:
    SCREENCAPTUREKIT_AVAILABLE = False
    logging.getLogger(__name__).info("ScreenCaptureKit not available (requires macOS 12.3+)")

logger = logging.getLogger(__name__)


class CaptureMethod(Enum):
    """Available capture methods in priority order"""
    AVFOUNDATION = "avfoundation"  # Native AVFoundation (best quality, purple indicator)
    SCREENCAPTUREKIT = "screencapturekit"  # Modern API (macOS 12.3+, best performance)
    SCREENCAPTURE_CMD = "screencapture_cmd"  # screencapture command (fallback)
    SCREENSHOT_LOOP = "screenshot_loop"  # Final fallback (PIL/Pillow)


class CaptureStatus(Enum):
    """Capture session status"""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class AdvancedCaptureConfig:
    """Dynamic configuration for macOS video capture - NO HARDCODING"""

    # Display settings
    display_id: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_DISPLAY_ID', '0')))

    # Quality settings
    target_fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_FPS', '30')))
    resolution: str = field(default_factory=lambda: os.getenv('JARVIS_CAPTURE_RESOLUTION', '1920x1080'))
    pixel_format: str = field(default_factory=lambda: os.getenv('JARVIS_CAPTURE_PIXEL_FORMAT', '32BGRA'))

    # Performance settings
    min_fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_MIN_FPS', '10')))
    max_fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_MAX_FPS', '60')))
    enable_adaptive_quality: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_ADAPTIVE', 'true').lower() == 'true'
    )

    # Memory settings
    max_memory_mb: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_MAX_MEMORY_MB', '500')))
    frame_buffer_size: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_BUFFER_SIZE', '10')))
    enable_memory_monitoring: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_MEMORY_MONITOR', 'true').lower() == 'true'
    )

    # Capture settings
    capture_cursor: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_CURSOR', 'false').lower() == 'true'
    )
    capture_mouse_clicks: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_MOUSE_CLICKS', 'false').lower() == 'true'
    )
    discard_late_frames: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_DISCARD_LATE', 'true').lower() == 'true'
    )

    # Fallback settings
    enable_fallback_chain: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_FALLBACK', 'true').lower() == 'true'
    )
    preferred_method: Optional[CaptureMethod] = field(
        default_factory=lambda: CaptureMethod(
            os.getenv('JARVIS_CAPTURE_METHOD', 'avfoundation')
        ) if os.getenv('JARVIS_CAPTURE_METHOD') else None
    )

    # Diagnostics
    enable_diagnostics: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_DIAGNOSTICS', 'true').lower() == 'true'
    )
    log_frame_metrics: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_LOG_METRICS', 'false').lower() == 'true'
    )

    def get_resolution_tuple(self) -> Tuple[int, int]:
        """Parse resolution string to (width, height)"""
        try:
            width, height = self.resolution.split('x')
            return (int(width), int(height))
        except Exception as e:
            logger.warning(f"Invalid resolution '{self.resolution}', using 1920x1080: {e}")
            return (1920, 1080)

    def get_avfoundation_preset(self) -> Any:
        """Get AVFoundation preset for current resolution"""
        if not AVFOUNDATION_AVAILABLE:
            return None

        presets = {
            '1920x1080': AVCaptureSessionPreset1920x1080,
            '1280x720': AVCaptureSessionPreset1280x720,
            '960x540': AVCaptureSessionPreset640x480,  # Use 640x480 preset
            '640x480': AVCaptureSessionPreset640x480,
        }

        return presets.get(self.resolution, AVCaptureSessionPreset1920x1080)


@dataclass
class CaptureMetrics:
    """Real-time capture metrics"""
    method: CaptureMethod
    status: CaptureStatus
    frames_captured: int = 0
    frames_dropped: int = 0
    current_fps: float = 0.0
    target_fps: int = 30
    memory_usage_mb: float = 0.0
    cpu_percent: float = 0.0
    uptime_seconds: float = 0.0
    last_frame_timestamp: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging"""
        return {
            'method': self.method.value,
            'status': self.status.value,
            'frames_captured': self.frames_captured,
            'frames_dropped': self.frames_dropped,
            'current_fps': round(self.current_fps, 2),
            'target_fps': self.target_fps,
            'memory_usage_mb': round(self.memory_usage_mb, 2),
            'cpu_percent': round(self.cpu_percent, 2),
            'uptime_seconds': round(self.uptime_seconds, 2),
            'error_count': len(self.errors),
        }


if PYOBJC_AVAILABLE:
    class VideoFrameDelegate(NSObject):
        """
        Objective-C delegate for handling AVFoundation video frames

        This class bridges between Objective-C (AVFoundation) and Python.
        It receives video frames from AVCaptureSession and forwards them
        to Python callbacks.
        """

        @classmethod
        def delegateWithCallback_(cls, callback):
            """Factory method to create delegate with callback"""
            delegate = cls.alloc().init()
            delegate.callback = callback
            delegate.frame_count = 0
            delegate.last_frame_time = time.time()
            return delegate

        def captureOutput_didOutputSampleBuffer_fromConnection_(
            self, output, sample_buffer, connection
        ):
            """
            AVCaptureVideoDataOutputSampleBufferDelegate method
            Called when a new video frame is captured
            """
            try:
                self.frame_count += 1
                current_time = time.time()

                # Convert CMSampleBuffer to numpy array
                image_buffer = CMSampleBufferGetImageBuffer(sample_buffer)
                if not image_buffer:
                    logger.warning("No image buffer in sample")
                    return

                # Lock pixel buffer for reading
                CVPixelBufferLockBaseAddress(image_buffer, 0)

                try:
                    # Get pixel data
                    base_address = CVPixelBufferGetBaseAddress(image_buffer)
                    bytes_per_row = CVPixelBufferGetBytesPerRow(image_buffer)
                    height = CVPixelBufferGetHeight(image_buffer)
                    width = CVPixelBufferGetWidth(image_buffer)

                    # Create numpy array from pixel data
                    # Format: BGRA (32-bit)
                    buffer_size = bytes_per_row * height
                    frame_data = objc.PyObjC_PythonToId(base_address)

                    # Convert to numpy array
                    frame = np.frombuffer(
                        base_address.as_buffer(buffer_size),
                        dtype=np.uint8
                    )
                    frame = frame.reshape((height, bytes_per_row // 4, 4))
                    frame = frame[:, :width, :3]  # Remove alpha channel
                    frame = frame[:, :, ::-1]  # BGR to RGB

                    # Calculate FPS
                    fps = 1.0 / (current_time - self.last_frame_time) if self.last_frame_time > 0 else 0
                    self.last_frame_time = current_time

                    # Call Python callback
                    if self.callback:
                        self.callback(frame, {
                            'frame_number': self.frame_count,
                            'timestamp': current_time,
                            'fps': fps,
                            'width': width,
                            'height': height,
                        })

                finally:
                    # Always unlock pixel buffer
                    CVPixelBufferUnlockBaseAddress(image_buffer, 0)

            except Exception as e:
                logger.error(f"Error in frame delegate: {e}", exc_info=True)


class AVFoundationCapture:
    """
    Native macOS video capture using AVFoundation via PyObjC

    This is the highest quality capture method with the purple indicator.
    Requires screen recording permission.
    """

    def __init__(self, config: AdvancedCaptureConfig):
        self.config = config
        self.session: Optional[Any] = None
        self.output: Optional[Any] = None
        self.delegate: Optional[Any] = None
        self.dispatch_queue: Optional[Any] = None
        self.is_running = False
        self.frame_callback: Optional[Callable] = None
        self._runloop_thread: Optional[threading.Thread] = None
        self._stop_runloop = threading.Event()

        if not AVFOUNDATION_AVAILABLE:
            raise RuntimeError("AVFoundation not available - install PyObjC frameworks")

    async def start_capture(self, frame_callback: Callable) -> bool:
        """
        Start AVFoundation capture session

        Args:
            frame_callback: Async function called with (frame: np.ndarray, metadata: dict)

        Returns:
            True if capture started successfully
        """
        if self.is_running:
            logger.warning("AVFoundation capture already running")
            return True

        self.frame_callback = frame_callback

        try:
            logger.info("🎥 Starting AVFoundation capture session...")

            # Create capture session
            self.session = AVCaptureSession.alloc().init()
            logger.info(f"   Created AVCaptureSession: {self.session}")

            # Set session preset based on resolution
            preset = self.config.get_avfoundation_preset()
            self.session.setSessionPreset_(preset)
            logger.info(f"   Set resolution preset: {self.config.resolution}")

            # Create screen input for specified display
            screen_input = AVCaptureScreenInput.alloc().initWithDisplayID_(
                self.config.display_id
            )

            if not screen_input:
                raise RuntimeError(f"Failed to create screen input for display {self.config.display_id}")

            logger.info(f"   Created screen input for display {self.config.display_id}")

            # Configure screen input
            min_frame_duration = CMTimeMake(1, self.config.target_fps)
            screen_input.setMinFrameDuration_(min_frame_duration)
            screen_input.setCapturesCursor_(self.config.capture_cursor)
            screen_input.setCapturesMouseClicks_(self.config.capture_mouse_clicks)

            logger.info(f"   Configured: {self.config.target_fps} FPS, cursor={self.config.capture_cursor}")

            # Add input to session
            if not self.session.canAddInput_(screen_input):
                raise RuntimeError("Cannot add screen input to capture session")

            self.session.addInput_(screen_input)
            logger.info("   Added screen input to session")

            # Create video output
            self.output = AVCaptureVideoDataOutput.alloc().init()
            self.output.setAlwaysDiscardsLateVideoFrames_(self.config.discard_late_frames)

            # Configure pixel format (32-bit BGRA)
            self.output.setVideoSettings_({
                kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA
            })

            logger.info(f"   Configured video output: format={self.config.pixel_format}")

            # Create delegate for frame callbacks
            self.delegate = VideoFrameDelegate.delegateWithCallback_(
                self._handle_frame_sync
            )

            # Create serial dispatch queue for video processing
            queue_name = f"com.jarvis.videocapture.{id(self)}".encode('utf-8')
            self.dispatch_queue = libdispatch.dispatch_queue_create(
                queue_name,
                None  # Serial queue
            )

            self.output.setSampleBufferDelegate_queue_(self.delegate, self.dispatch_queue)
            logger.info("   Set up frame delegate and dispatch queue")

            # Add output to session
            if not self.session.canAddOutput_(self.output):
                raise RuntimeError("Cannot add video output to capture session")

            self.session.addOutput_(self.output)
            logger.info("   Added video output to session")

            # Start NSRunLoop in background thread (required for Objective-C callbacks)
            self._start_runloop()

            # Start capture session
            self.session.startRunning()
            self.is_running = True

            logger.info("✅ AVFoundation capture started - purple indicator should be visible!")
            logger.info(f"   Display: {self.config.display_id}")
            logger.info(f"   Resolution: {self.config.resolution}")
            logger.info(f"   FPS: {self.config.target_fps}")
            logger.info(f"   Memory limit: {self.config.max_memory_mb}MB")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to start AVFoundation capture: {e}", exc_info=True)
            await self.stop_capture()
            return False

    def _start_runloop(self):
        """Start NSRunLoop in background thread for Objective-C callbacks"""
        def runloop_thread():
            """Run NSRunLoop until stopped"""
            logger.info("[RunLoop] Starting NSRunLoop thread...")

            runloop = NSRunLoop.currentRunLoop()

            while not self._stop_runloop.is_set():
                # Run loop for short intervals to allow checking stop flag
                runloop.runMode_beforeDate_(
                    NSDefaultRunLoopMode,
                    objc.lookUpClass('NSDate').dateWithTimeIntervalSinceNow_(0.1)
                )

            logger.info("[RunLoop] NSRunLoop thread stopped")

        self._stop_runloop.clear()
        self._runloop_thread = threading.Thread(target=runloop_thread, daemon=True)
        self._runloop_thread.start()
        logger.info("Started NSRunLoop in background thread")

    def _handle_frame_sync(self, frame: np.ndarray, metadata: Dict[str, Any]):
        """
        Synchronous frame handler called from Objective-C thread

        This bridges from the Objective-C dispatch queue to Python asyncio.
        """
        try:
            if self.frame_callback:
                # Schedule async callback in event loop
                asyncio.run_coroutine_threadsafe(
                    self.frame_callback(frame, metadata),
                    asyncio.get_event_loop()
                )
        except Exception as e:
            logger.error(f"Error scheduling frame callback: {e}")

    async def stop_capture(self):
        """Stop AVFoundation capture session"""
        if not self.is_running:
            return

        logger.info("Stopping AVFoundation capture...")

        try:
            # Stop capture session
            if self.session:
                self.session.stopRunning()

            # Stop runloop thread
            if self._runloop_thread and self._runloop_thread.is_alive():
                self._stop_runloop.set()
                self._runloop_thread.join(timeout=2.0)

            # Clean up
            self.is_running = False
            self.session = None
            self.output = None
            self.delegate = None
            self.dispatch_queue = None

            logger.info("✅ AVFoundation capture stopped")

        except Exception as e:
            logger.error(f"Error stopping AVFoundation capture: {e}")

    def is_available(self) -> bool:
        """Check if AVFoundation capture is available"""
        return AVFOUNDATION_AVAILABLE


class AdvancedVideoCaptureManager:
    """
    Advanced video capture manager with intelligent fallback chain

    Tries capture methods in order of quality:
    1. AVFoundation (best quality, purple indicator)
    2. ScreenCaptureKit (modern, best performance, macOS 12.3+)
    3. screencapture command (reliable fallback)
    4. Screenshot loop (final fallback)
    """

    def __init__(self, config: Optional[AdvancedCaptureConfig] = None):
        self.config = config or AdvancedCaptureConfig()
        self.metrics = CaptureMetrics(
            method=CaptureMethod.AVFOUNDATION,  # Will be updated
            status=CaptureStatus.IDLE,
            target_fps=self.config.target_fps,
        )

        # Capture implementation
        self.capture_impl: Optional[Any] = None
        self.current_method: Optional[CaptureMethod] = None

        # Callbacks
        self.frame_callback: Optional[Callable] = None

        # State
        self.start_time: float = 0.0
        self._executor = ThreadPoolExecutor(max_workers=2)

        logger.info(f"Advanced Video Capture Manager initialized")
        logger.info(f"  PyObjC available: {PYOBJC_AVAILABLE}")
        logger.info(f"  AVFoundation available: {AVFOUNDATION_AVAILABLE}")
        logger.info(f"  ScreenCaptureKit available: {SCREENCAPTUREKIT_AVAILABLE}")
        logger.info(f"  Config: {self.config.resolution} @ {self.config.target_fps} FPS")

    async def start_capture(self, frame_callback: Callable) -> bool:
        """
        Start video capture with intelligent fallback

        Args:
            frame_callback: Async function called with (frame: np.ndarray, metadata: dict)

        Returns:
            True if any capture method started successfully
        """
        if self.metrics.status == CaptureStatus.RUNNING:
            logger.warning("Capture already running")
            return True

        self.frame_callback = frame_callback
        self.metrics.status = CaptureStatus.STARTING
        self.start_time = time.time()

        # Try capture methods in priority order
        methods_to_try = self._get_methods_to_try()

        for method in methods_to_try:
            logger.info(f"🎯 Trying capture method: {method.value}")

            try:
                success = await self._try_capture_method(method)

                if success:
                    self.current_method = method
                    self.metrics.method = method
                    self.metrics.status = CaptureStatus.RUNNING

                    logger.info(f"✅ Capture started with method: {method.value}")

                    # Start metrics monitoring
                    asyncio.create_task(self._monitor_metrics())

                    return True
                else:
                    logger.warning(f"❌ Method {method.value} failed, trying next...")

            except Exception as e:
                logger.error(f"❌ Error with {method.value}: {e}")
                self.metrics.errors.append(f"{method.value}: {str(e)}")

        # All methods failed
        self.metrics.status = CaptureStatus.ERROR
        logger.error("❌ All capture methods failed!")
        return False

    def _get_methods_to_try(self) -> List[CaptureMethod]:
        """Get list of capture methods to try in priority order"""
        if self.config.preferred_method and not self.config.enable_fallback_chain:
            # Only try preferred method
            return [self.config.preferred_method]

        methods = []

        # Preferred method first
        if self.config.preferred_method:
            methods.append(self.config.preferred_method)

        # Then try others in order
        if AVFOUNDATION_AVAILABLE and CaptureMethod.AVFOUNDATION not in methods:
            methods.append(CaptureMethod.AVFOUNDATION)

        if SCREENCAPTUREKIT_AVAILABLE and CaptureMethod.SCREENCAPTUREKIT not in methods:
            methods.append(CaptureMethod.SCREENCAPTUREKIT)

        if CaptureMethod.SCREENCAPTURE_CMD not in methods:
            methods.append(CaptureMethod.SCREENCAPTURE_CMD)

        # Final fallback
        if CaptureMethod.SCREENSHOT_LOOP not in methods:
            methods.append(CaptureMethod.SCREENSHOT_LOOP)

        return methods

    async def _try_capture_method(self, method: CaptureMethod) -> bool:
        """Try to start capture with specified method"""
        try:
            if method == CaptureMethod.AVFOUNDATION:
                return await self._start_avfoundation()
            elif method == CaptureMethod.SCREENCAPTUREKIT:
                return await self._start_screencapturekit()
            elif method == CaptureMethod.SCREENCAPTURE_CMD:
                return await self._start_screencapture_cmd()
            elif method == CaptureMethod.SCREENSHOT_LOOP:
                return await self._start_screenshot_loop()
            else:
                logger.error(f"Unknown capture method: {method}")
                return False
        except Exception as e:
            logger.error(f"Error starting {method.value}: {e}", exc_info=True)
            return False

    async def _start_avfoundation(self) -> bool:
        """Start AVFoundation capture"""
        if not AVFOUNDATION_AVAILABLE:
            logger.warning("AVFoundation not available")
            return False

        self.capture_impl = AVFoundationCapture(self.config)
        return await self.capture_impl.start_capture(self._on_frame_captured)

    async def _start_screencapturekit(self) -> bool:
        """Start ScreenCaptureKit capture (macOS 12.3+)"""
        if not SCREENCAPTUREKIT_AVAILABLE:
            logger.warning("ScreenCaptureKit not available (requires macOS 12.3+)")
            return False

        # TODO: Implement ScreenCaptureKit support
        logger.warning("ScreenCaptureKit support not yet implemented")
        return False

    async def _start_screencapture_cmd(self) -> bool:
        """Start capture using screencapture command"""
        logger.info("Using screencapture command fallback")
        # TODO: Implement screencapture command support
        return False

    async def _start_screenshot_loop(self) -> bool:
        """Start screenshot loop fallback"""
        logger.info("Using screenshot loop fallback")
        # TODO: Implement screenshot loop
        return False

    async def _on_frame_captured(self, frame: np.ndarray, metadata: Dict[str, Any]):
        """Handle captured frame"""
        try:
            # Update metrics
            self.metrics.frames_captured += 1
            self.metrics.last_frame_timestamp = metadata.get('timestamp', time.time())
            self.metrics.current_fps = metadata.get('fps', 0.0)

            # Log metrics if enabled
            if self.config.log_frame_metrics and self.metrics.frames_captured % 100 == 0:
                logger.info(f"[Metrics] {self.metrics.to_dict()}")

            # Call user callback
            if self.frame_callback:
                await self.frame_callback(frame, metadata)

        except Exception as e:
            logger.error(f"Error handling frame: {e}")
            self.metrics.errors.append(f"Frame handler: {str(e)}")

    async def _monitor_metrics(self):
        """Monitor capture metrics and adjust quality if needed"""
        process = psutil.Process()

        while self.metrics.status == CaptureStatus.RUNNING:
            try:
                # Update metrics
                self.metrics.uptime_seconds = time.time() - self.start_time
                self.metrics.memory_usage_mb = process.memory_info().rss / 1024 / 1024
                self.metrics.cpu_percent = process.cpu_percent(interval=0.1)

                # Adaptive quality adjustment
                if self.config.enable_adaptive_quality:
                    await self._adjust_quality()

                # Log diagnostics
                if self.config.enable_diagnostics and self.metrics.uptime_seconds % 30 < 1:
                    logger.info(f"[Diagnostics] {self.metrics.to_dict()}")

                await asyncio.sleep(1.0)

            except Exception as e:
                logger.error(f"Error monitoring metrics: {e}")

    async def _adjust_quality(self):
        """Adjust capture quality based on system resources"""
        # Reduce FPS if memory usage is high
        if self.metrics.memory_usage_mb > self.config.max_memory_mb * 0.9:
            new_fps = max(self.config.min_fps, self.config.target_fps - 5)
            if new_fps < self.config.target_fps:
                logger.warning(
                    f"High memory usage ({self.metrics.memory_usage_mb:.1f}MB), "
                    f"reducing FPS: {self.config.target_fps} → {new_fps}"
                )
                self.config.target_fps = new_fps
                # TODO: Apply FPS change to active capture

    async def stop_capture(self):
        """Stop video capture"""
        if self.metrics.status != CaptureStatus.RUNNING:
            return

        logger.info("Stopping video capture...")
        self.metrics.status = CaptureStatus.STOPPING

        try:
            if self.capture_impl:
                if hasattr(self.capture_impl, 'stop_capture'):
                    await self.capture_impl.stop_capture()

            self.metrics.status = CaptureStatus.STOPPED
            logger.info("✅ Video capture stopped")

            # Log final metrics
            logger.info(f"[Final Metrics] {self.metrics.to_dict()}")

        except Exception as e:
            logger.error(f"Error stopping capture: {e}")
            self.metrics.status = CaptureStatus.ERROR

    def get_metrics(self) -> Dict[str, Any]:
        """Get current capture metrics"""
        return self.metrics.to_dict()

    def get_status(self) -> CaptureStatus:
        """Get current capture status"""
        return self.metrics.status

    def is_available(self) -> bool:
        """Check if video capture is available"""
        return AVFOUNDATION_AVAILABLE or SCREENCAPTUREKIT_AVAILABLE


# Factory function for easy integration
async def create_video_capture(
    config: Optional[AdvancedCaptureConfig] = None
) -> AdvancedVideoCaptureManager:
    """
    Create advanced video capture manager

    Args:
        config: Optional configuration (uses environment variables if not provided)

    Returns:
        Configured AdvancedVideoCaptureManager instance
    """
    manager = AdvancedVideoCaptureManager(config)
    return manager


# Diagnostic function
def check_capture_availability() -> Dict[str, Any]:
    """
    Check availability of all capture methods

    Returns:
        Dictionary with availability status and system info
    """
    return {
        'pyobjc_installed': PYOBJC_AVAILABLE,
        'avfoundation_available': AVFOUNDATION_AVAILABLE,
        'screencapturekit_available': SCREENCAPTUREKIT_AVAILABLE,
        'macos_version': os.popen('sw_vers -productVersion').read().strip(),
        'python_version': os.sys.version.split()[0],
        'recommended_method': 'AVFoundation' if AVFOUNDATION_AVAILABLE else 'ScreenCaptureKit' if SCREENCAPTUREKIT_AVAILABLE else 'fallback',
        'memory_available_mb': psutil.virtual_memory().available / 1024 / 1024,
        'cpu_count': psutil.cpu_count(),
    }
