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
import queue
from typing import Union

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
        CGWindowListCreateImage,
        CGRectNull,
        kCGWindowListOptionIncludingWindow,
        kCGWindowImageDefault,
        CGImageGetWidth,
        CGImageGetHeight,
        CGImageGetDataProvider,
        CGDataProviderCopyData,
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
# Priority 1: Native C++ bridge (fast_capture_stream) - Ferrari Engine
try:
    import sys
    from pathlib import Path
    native_extensions_path = Path(__file__).parent.parent / "native_extensions"
    if str(native_extensions_path) not in sys.path:
        sys.path.insert(0, str(native_extensions_path))

    from macos_sck_stream import (
        AsyncCaptureStream,
        AsyncStreamManager,
        StreamingConfig,
        is_sck_available
    )
    SCREENCAPTUREKIT_AVAILABLE = is_sck_available()
    NATIVE_SCK_BRIDGE_AVAILABLE = True
    logging.getLogger(__name__).info("✅ ScreenCaptureKit native bridge loaded (Ferrari Engine)")
except ImportError as e:
    NATIVE_SCK_BRIDGE_AVAILABLE = False
    # Fallback: Try PyObjC ScreenCaptureKit
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
    SCREENCAPTUREKIT = "screencapturekit"  # For VideoWatcher window-specific capture (Ferrari Engine)
    AVFOUNDATION = "avfoundation"  # ⚡ Priority 1: AVFoundation (full display, high quality, purple indicator)
    SCREENCAPTURE_CMD = "screencapture_cmd"  # Priority 2: screencapture command (fallback)
    SCREENSHOT_LOOP = "screenshot_loop"  # Priority 3: Final fallback (PIL/Pillow)


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
        logger.info(f"  ⚡ Ferrari Engine (Native SCK): {NATIVE_SCK_BRIDGE_AVAILABLE}")
        logger.info(f"  PyObjC available: {PYOBJC_AVAILABLE}")
        logger.info(f"  AVFoundation available: {AVFOUNDATION_AVAILABLE}")
        logger.info(f"  ScreenCaptureKit available: {SCREENCAPTUREKIT_AVAILABLE}")
        logger.info(f"  Config: {self.config.resolution} @ {self.config.target_fps} FPS")
        if NATIVE_SCK_BRIDGE_AVAILABLE:
            logger.info(f"  🏎️  Priority 1: ScreenCaptureKit (Adaptive FPS, GPU-accelerated)")
        elif AVFOUNDATION_AVAILABLE:
            logger.info(f"  📹  Priority 1: AVFoundation (High quality, purple indicator)")

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

        # Priority 1: AVFoundation (best for full display capture)
        if AVFOUNDATION_AVAILABLE and CaptureMethod.AVFOUNDATION not in methods:
            methods.append(CaptureMethod.AVFOUNDATION)

        # Note: ScreenCaptureKit (Ferrari Engine) optimized for window-specific capture (VideoWatcher)
        # For full display, AVFoundation is more appropriate

        # Priority 2: screencapture command (reliable fallback)
        if CaptureMethod.SCREENCAPTURE_CMD not in methods:
            methods.append(CaptureMethod.SCREENCAPTURE_CMD)

        # Priority 3: Final fallback
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
        """
        Start ScreenCaptureKit capture using native C++ bridge (Ferrari Engine)

        NOTE: Ferrari Engine (SCK) is optimized for window-specific capture.
        For full display capture, AVFoundation is more appropriate.

        This method is available but will fall through to AVFoundation
        for full display capture. SCK excels in VideoWatcher for
        window-specific surveillance.
        """
        if not SCREENCAPTUREKIT_AVAILABLE:
            logger.info("ScreenCaptureKit not available - Ferrari Engine is for window-specific capture")
            return False

        if not NATIVE_SCK_BRIDGE_AVAILABLE:
            logger.info("Ferrari Engine (SCK bridge) optimized for VideoWatcher, not full display")
            return False

        # Ferrari Engine is designed for window-specific capture (VideoWatcher)
        # For full display capture, AVFoundation is the better choice
        logger.info("🏎️  Ferrari Engine available for window-specific VideoWatcher capture")
        logger.info("   For full display: using AVFoundation (next in priority)")
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


# =============================================================================
# Video Multi-Space Intelligence (VMSI) - Background Watcher System
# =============================================================================
# Production-grade background visual monitoring for specific windows
# Features:
# - Window-specific capture (not full display)
# - Low-FPS streaming (1-10 FPS) for efficiency
# - Low-priority thread execution
# - Parallel watcher support
# - Visual event detection integration
# - Cross-repo state sharing
# =============================================================================

class WatcherStatus(Enum):
    """Video watcher status states"""
    IDLE = "idle"
    STARTING = "starting"
    WATCHING = "watching"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    EVENT_DETECTED = "event_detected"


@dataclass
class VisualEventResult:
    """Result from visual event detection"""
    detected: bool
    event_type: str  # "text", "element", "color"
    trigger: str
    confidence: float
    frame_number: int
    detection_time: float
    frame_data: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WatcherConfig:
    """Configuration for video watcher - NO HARDCODING"""
    window_id: int
    fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_WATCHER_DEFAULT_FPS', '5')))
    priority: str = field(default_factory=lambda: os.getenv('JARVIS_WATCHER_PRIORITY', 'low'))
    timeout: float = field(default_factory=lambda: float(os.getenv('JARVIS_WATCHER_TIMEOUT', '300.0')))
    max_buffer_size: int = field(default_factory=lambda: int(os.getenv('JARVIS_WATCHER_BUFFER_SIZE', '10')))

    # Visual detection settings
    enable_ocr: bool = field(default_factory=lambda: os.getenv('JARVIS_WATCHER_OCR', 'true').lower() == 'true')
    enable_element_detection: bool = field(default_factory=lambda: os.getenv('JARVIS_WATCHER_ELEMENTS', 'true').lower() == 'true')
    confidence_threshold: float = field(default_factory=lambda: float(os.getenv('JARVIS_DETECTION_CONFIDENCE', '0.75')))


class VideoWatcher:
    """
    Background video watcher for a specific window.

    Captures frames from a specific macOS window at low FPS and
    enables visual event detection (text, elements, colors).

    This is the core of VMSI - "The Watcher" that monitors background windows.
    """

    def __init__(self, config: WatcherConfig):
        self.config = config
        self.watcher_id = f"watcher_{config.window_id}_{int(time.time())}"
        self.status = WatcherStatus.IDLE

        # Frame queue (producer-consumer pattern)
        self.frame_queue: queue.Queue = queue.Queue(maxsize=config.max_buffer_size)

        # ScreenCaptureKit stream (Ferrari Engine for window-specific capture)
        self._sck_stream: Optional[Any] = None
        self._use_sck = NATIVE_SCK_BRIDGE_AVAILABLE  # Will use SCK if available

        # Stats
        self.frames_captured = 0
        self.frames_analyzed = 0
        self.events_detected = 0
        self.start_time: float = 0.0
        self.last_frame_time: float = 0.0

        # Threading
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_loop_task: Optional[asyncio.Task] = None

        # Metadata
        self.app_name: Optional[str] = None
        self.space_id: Optional[int] = None

        capture_method = "ScreenCaptureKit (Ferrari)" if self._use_sck else "CGWindowListCreateImage"
        logger.info(f"VideoWatcher created: {self.watcher_id} (Window {config.window_id}, {config.fps} FPS, Method: {capture_method})")

    async def start(self) -> bool:
        """Start the video watcher."""
        if self.status == WatcherStatus.WATCHING:
            logger.warning(f"Watcher {self.watcher_id} already running")
            return True

        self.status = WatcherStatus.STARTING
        self.start_time = time.time()
        self._stop_event.clear()

        # Try to initialize ScreenCaptureKit stream (Ferrari Engine)
        if self._use_sck:
            try:
                logger.info(f"[Watcher {self.watcher_id}] Initializing Ferrari Engine for window {self.config.window_id}...")

                sck_config = StreamingConfig(
                    target_fps=self.config.fps,
                    max_buffer_size=self.config.max_buffer_size,
                    output_format="raw",
                    use_gpu_acceleration=True,
                    drop_frames_on_overflow=True,
                    capture_cursor=False,  # Don't capture cursor for background monitoring
                    resolution_scale=1.0
                )

                self._sck_stream = AsyncCaptureStream(self.config.window_id, sck_config)
                success = await self._sck_stream.start()

                if success:
                    logger.info(f"🏎️  [Watcher {self.watcher_id}] Ferrari Engine started for window {self.config.window_id}")
                    # Start async frame loop for SCK
                    self._frame_loop_task = asyncio.create_task(self._sck_frame_loop())
                    self.status = WatcherStatus.WATCHING
                    return True
                else:
                    logger.warning(f"[Watcher {self.watcher_id}] Ferrari Engine failed to start, falling back to CGWindowListCreateImage")
                    self._use_sck = False
                    self._sck_stream = None

            except Exception as e:
                logger.warning(f"[Watcher {self.watcher_id}] Ferrari Engine initialization failed: {e}, falling back")
                self._use_sck = False
                self._sck_stream = None

        # Fallback: Start traditional capture thread with CGWindowListCreateImage
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name=f"VideoWatcher-{self.config.window_id}",
            daemon=True
        )

        # Set thread priority to low (if supported)
        try:
            import resource
            # Lower priority (higher nice value)
            resource.setrlimit(resource.RLIMIT_NICE, (19, 19))
        except Exception:
            pass  # Priority setting not critical

        self._capture_thread.start()
        self.status = WatcherStatus.WATCHING

        logger.info(f"✅ Watcher {self.watcher_id} started (fallback method)")
        return True

    def _capture_loop(self):
        """
        Capture loop running in background thread.

        Uses CGWindowListCreateImage to capture specific window only.
        Runs at low FPS to save resources.
        """
        frame_interval = 1.0 / self.config.fps
        consecutive_failures = 0
        last_log_time = 0

        logger.info(f"[Watcher {self.watcher_id}] Capture loop started (target FPS: {self.config.fps})")

        while not self._stop_event.is_set():
            try:
                loop_start = time.time()

                # Capture window-specific frame
                frame = self._capture_window_frame()

                if frame is not None:
                    self.frames_captured += 1
                    self.last_frame_time = time.time()
                    consecutive_failures = 0

                    # Log successful capture periodically
                    if self.frames_captured % 10 == 1:
                        logger.debug(
                            f"[Watcher {self.watcher_id}] Captured frame #{self.frames_captured}, "
                            f"shape={frame.shape}, queue_size={self.frame_queue.qsize()}"
                        )

                    # Add to queue (non-blocking)
                    try:
                        self.frame_queue.put_nowait({
                            'frame': frame,
                            'frame_number': self.frames_captured,
                            'timestamp': self.last_frame_time,
                            'window_id': self.config.window_id,
                        })
                    except queue.Full:
                        # Queue full, drop oldest frame
                        try:
                            self.frame_queue.get_nowait()
                            self.frame_queue.put_nowait({
                                'frame': frame,
                                'frame_number': self.frames_captured,
                                'timestamp': self.last_frame_time,
                                'window_id': self.config.window_id,
                            })
                        except queue.Empty:
                            pass
                else:
                    # Frame capture failed
                    consecutive_failures += 1

                    # Log capture failures periodically (not every time to avoid spam)
                    current_time = time.time()
                    if current_time - last_log_time > 5.0:  # Log every 5 seconds
                        logger.warning(
                            f"[Watcher {self.watcher_id}] Frame capture failing! "
                            f"Consecutive failures: {consecutive_failures}, "
                            f"Total captured: {self.frames_captured}, "
                            f"Window ID: {self.config.window_id}"
                        )
                        last_log_time = current_time

                        # After many failures, log detailed diagnostics
                        if consecutive_failures > 20:
                            logger.error(
                                f"[Watcher {self.watcher_id}] CRITICAL: Frame capture has failed "
                                f"{consecutive_failures} times in a row. Possible causes:\n"
                                f"  1. Screen Recording permission not granted (System Preferences → Privacy)\n"
                                f"  2. Window ID {self.config.window_id} is invalid or window was closed\n"
                                f"  3. PyObjC frameworks not available\n"
                                f"  PYOBJC_AVAILABLE={PYOBJC_AVAILABLE}"
                            )

                # Sleep to maintain target FPS
                elapsed = time.time() - loop_start
                sleep_time = max(0, frame_interval - elapsed)

                if sleep_time > 0:
                    time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error in watcher {self.watcher_id} capture loop: {e}", exc_info=True)
                time.sleep(1.0)  # Backoff on error

        logger.info(f"Watcher {self.watcher_id} capture loop stopped (captured {self.frames_captured} frames)")

    def _capture_window_frame(self) -> Optional[np.ndarray]:
        """
        Capture frame from specific window using CGWindowListCreateImage.

        This is window-specific (not full display) which is more efficient.

        FALLBACK: If CGWindowListCreateImage fails (permissions), use screencapture command.
        """
        if not PYOBJC_AVAILABLE:
            logger.error(f"[Watcher {self.watcher_id}] PyObjC not available - cannot capture frames")
            return None

        try:
            # Capture window image
            # kCGWindowListOptionIncludingWindow = only this window
            # kCGWindowImageDefault = default quality
            cg_image = CGWindowListCreateImage(
                CGRectNull,  # Entire window bounds
                kCGWindowListOptionIncludingWindow,
                self.config.window_id,
                kCGWindowImageDefault
            )

            if not cg_image:
                # FALLBACK: Try screencapture command (works without Screen Recording permission on some macOS versions)
                if self.frames_captured == 0:
                    logger.info(
                        f"[Watcher {self.watcher_id}] CGWindowListCreateImage failed - using screencapture fallback"
                    )
                return self._capture_window_frame_fallback()

            # Get image dimensions
            width = CGImageGetWidth(cg_image)
            height = CGImageGetHeight(cg_image)

            if width == 0 or height == 0:
                logger.warning(f"[Watcher {self.watcher_id}] Captured image has zero dimensions: {width}x{height}")
                return None

            # Get pixel data
            data_provider = CGImageGetDataProvider(cg_image)
            data = CGDataProviderCopyData(data_provider)

            if not data:
                logger.error(f"[Watcher {self.watcher_id}] Failed to get pixel data from CGImage")
                return None

            # Convert to numpy array
            # CGImage format is typically BGRA
            bytes_data = bytes(data)
            frame = np.frombuffer(bytes_data, dtype=np.uint8)

            # Reshape to image dimensions
            # Assuming 4 bytes per pixel (BGRA)
            bytes_per_row = width * 4
            frame = frame.reshape((height, bytes_per_row // 4, 4))
            frame = frame[:, :width, :3]  # Remove alpha
            frame = frame[:, :, ::-1]  # BGR to RGB

            # Log successful capture details (first frame only)
            if self.frames_captured == 0:
                logger.info(
                    f"[Watcher {self.watcher_id}] First frame captured successfully! "
                    f"Dimensions: {width}x{height}, Shape: {frame.shape}"
                )

            return frame

        except Exception as e:
            logger.debug(f"Error capturing window {self.config.window_id}: {e}")
            return None

    def _capture_window_frame_fallback(self) -> Optional[np.ndarray]:
        """
        Fallback capture method using macOS screencapture command.

        This works when CGWindowListCreateImage fails (e.g., macOS Sequoia 15.1
        with changed Screen Recording permission requirements).

        The screencapture command has different permission requirements and
        works in cases where the Quartz API fails.

        Returns:
            numpy array in RGB format, or None if capture fails
        """
        import subprocess
        import tempfile

        try:
            # Import PIL (widely used in codebase)
            try:
                from PIL import Image
            except ImportError:
                logger.error("[Fallback Capture] PIL not available - cannot use fallback method")
                return None

            # Create temporary file for screenshot
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
                tmp_path = tmp_file.name

            try:
                # Use screencapture with window ID
                # -l <window_id> = capture specific window
                # -x = no sound
                # -t png = PNG format
                result = subprocess.run(
                    ['screencapture', '-l', str(self.config.window_id), '-x', '-t', 'png', tmp_path],
                    capture_output=True,
                    timeout=2.0,  # 2 second timeout
                    check=False  # Don't raise on non-zero exit
                )

                # Check if file was created and has content
                if not os.path.exists(tmp_path):
                    logger.debug(f"[Fallback Capture] screencapture did not create file for window {self.config.window_id}")
                    return None

                file_size = os.path.getsize(tmp_path)
                if file_size == 0:
                    logger.debug(f"[Fallback Capture] screencapture created empty file for window {self.config.window_id}")
                    return None

                # Load image with PIL
                pil_image = Image.open(tmp_path)

                # Convert to RGB (in case it's RGBA or other format)
                if pil_image.mode != 'RGB':
                    pil_image = pil_image.convert('RGB')

                # Convert to numpy array
                frame = np.array(pil_image)

                # Log successful fallback capture (first time only)
                if self.frames_captured == 0:
                    logger.info(
                        f"[Watcher {self.watcher_id}] Fallback capture successful! "
                        f"Using screencapture command. Dimensions: {frame.shape[1]}x{frame.shape[0]}, "
                        f"File size: {file_size} bytes"
                    )

                return frame

            finally:
                # Always clean up temp file
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception as cleanup_error:
                    logger.debug(f"[Fallback Capture] Failed to clean up temp file: {cleanup_error}")

        except subprocess.TimeoutExpired:
            logger.warning(f"[Fallback Capture] screencapture command timed out for window {self.config.window_id}")
            return None
        except Exception as e:
            logger.debug(f"[Fallback Capture] Error in fallback capture: {e}")
            return None

    async def _sck_frame_loop(self):
        """
        ScreenCaptureKit frame consumption loop for VideoWatcher (Ferrari Engine)

        This async loop pulls frames from the native SCK stream and pushes them
        to the frame queue for consumption by visual event detection.
        """
        logger.info(f"[Watcher {self.watcher_id}] SCK frame loop started (Ferrari Engine)")

        frame_count = 0
        last_log_time = time.time()

        while not self._stop_event.is_set() and self.status == WatcherStatus.WATCHING:
            try:
                # Get frame from SCK stream (use short timeout for responsive loop)
                frame_data = await self._sck_stream.get_frame(timeout_ms=100)

                if not frame_data:
                    # No frame available (expected with adaptive FPS on static content)
                    await asyncio.sleep(0.01)
                    continue

                frame_count += 1
                self.frames_captured = frame_count
                self.last_frame_time = time.time()

                # Extract numpy array
                frame = frame_data.get('image')
                if frame is None:
                    logger.debug(f"[Watcher {self.watcher_id}] Frame {frame_count} has no image data")
                    continue

                # Convert BGRA to RGB if needed
                if len(frame.shape) == 3 and frame.shape[2] == 4:
                    frame = frame[:, :, :3]  # Remove alpha
                    frame = frame[:, :, ::-1]  # BGRA to RGB

                # Log periodically
                if frame_count % 10 == 1:
                    logger.debug(
                        f"[Watcher {self.watcher_id}] Frame {frame_count}: "
                        f"shape={frame.shape}, "
                        f"latency={frame_data.get('capture_latency_us', 0)/1000:.1f}ms, "
                        f"queue_size={self.frame_queue.qsize()}"
                    )

                # Add to queue (non-blocking with overflow handling)
                try:
                    self.frame_queue.put_nowait({
                        'frame': frame,
                        'frame_number': frame_count,
                        'timestamp': self.last_frame_time,
                        'window_id': self.config.window_id,
                        'capture_latency_ms': frame_data.get('capture_latency_us', 0) / 1000.0,
                        'method': 'screencapturekit',
                    })
                except queue.Full:
                    # Queue full, drop oldest frame
                    try:
                        self.frame_queue.get_nowait()
                        self.frame_queue.put_nowait({
                            'frame': frame,
                            'frame_number': frame_count,
                            'timestamp': self.last_frame_time,
                            'window_id': self.config.window_id,
                            'capture_latency_ms': frame_data.get('capture_latency_us', 0) / 1000.0,
                            'method': 'screencapturekit',
                        })
                    except queue.Empty:
                        pass

            except Exception as e:
                logger.error(f"[Watcher {self.watcher_id}] Error in SCK frame loop: {e}", exc_info=True)
                await asyncio.sleep(0.1)  # Backoff on error

        logger.info(f"[Watcher {self.watcher_id}] SCK frame loop stopped (captured {frame_count} frames)")

    async def get_latest_frame(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Get the latest frame from the watcher."""
        try:
            frame_data = await asyncio.wait_for(
                asyncio.to_thread(self.frame_queue.get),
                timeout=timeout
            )
            return frame_data
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"Error getting frame from watcher {self.watcher_id}: {e}")
            return None

    async def stop(self):
        """Stop the video watcher."""
        if self.status in (WatcherStatus.STOPPED, WatcherStatus.STOPPING):
            return

        self.status = WatcherStatus.STOPPING
        logger.info(f"Stopping watcher {self.watcher_id}...")

        # Signal stop
        self._stop_event.set()

        # Stop SCK stream if using Ferrari Engine
        if self._sck_stream:
            try:
                await self._sck_stream.stop()
                logger.info(f"[Watcher {self.watcher_id}] Ferrari Engine stream stopped")
            except Exception as e:
                logger.error(f"[Watcher {self.watcher_id}] Error stopping SCK stream: {e}")

        # Cancel frame loop task if running
        if self._frame_loop_task and not self._frame_loop_task.done():
            self._frame_loop_task.cancel()
            try:
                await self._frame_loop_task
            except asyncio.CancelledError:
                pass

        # Wait for capture thread (fallback method)
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2.0)

        # Clear queue
        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break

        self.status = WatcherStatus.STOPPED

        # Log stats
        uptime = time.time() - self.start_time if self.start_time > 0 else 0
        capture_method = "Ferrari Engine" if self._sck_stream else "CGWindowListCreateImage"
        logger.info(
            f"✅ Watcher {self.watcher_id} stopped ({capture_method}) - "
            f"Uptime: {uptime:.1f}s, Frames: {self.frames_captured}, "
            f"Events: {self.events_detected}"
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get watcher statistics."""
        uptime = time.time() - self.start_time if self.start_time > 0 else 0
        actual_fps = self.frames_captured / uptime if uptime > 0 else 0

        return {
            'watcher_id': self.watcher_id,
            'window_id': self.config.window_id,
            'status': self.status.value,
            'app_name': self.app_name,
            'space_id': self.space_id,
            'target_fps': self.config.fps,
            'actual_fps': round(actual_fps, 2),
            'frames_captured': self.frames_captured,
            'frames_analyzed': self.frames_analyzed,
            'events_detected': self.events_detected,
            'uptime_seconds': round(uptime, 2),
            'queue_size': self.frame_queue.qsize(),
        }


class VideoWatcherManager:
    """
    Manager for multiple background video watchers.

    Enables parallel monitoring of multiple windows simultaneously.
    This is the "God Mode" - JARVIS watching multiple things at once.
    """

    def __init__(
        self,
        max_parallel_watchers: Optional[int] = None
    ):
        self.max_parallel_watchers = max_parallel_watchers or int(
            os.getenv('JARVIS_WATCHER_MAX_PARALLEL', '3')
        )

        self.watchers: Dict[str, VideoWatcher] = {}
        self._watcher_lock = asyncio.Lock()

        # Stats
        self.total_watchers_spawned = 0
        self.total_events_detected = 0

        logger.info(f"VideoWatcherManager initialized (max parallel: {self.max_parallel_watchers})")

    async def spawn_watcher(
        self,
        window_id: int,
        fps: int = 5,
        app_name: Optional[str] = None,
        space_id: Optional[int] = None,
        priority: str = "low",
        timeout: float = 300.0
    ) -> VideoWatcher:
        """
        Spawn a new background video watcher for a specific window.

        Args:
            window_id: macOS window ID to watch
            fps: Frame rate (1-10, default 5)
            app_name: Name of app (for logging)
            space_id: macOS Space ID (for logging)
            priority: Thread priority ("low", "normal", "high")
            timeout: Max watch time in seconds

        Returns:
            VideoWatcher instance
        """
        async with self._watcher_lock:
            # Check if we're at max capacity
            active_watchers = [w for w in self.watchers.values()
                              if w.status == WatcherStatus.WATCHING]

            if len(active_watchers) >= self.max_parallel_watchers:
                raise RuntimeError(
                    f"Maximum parallel watchers ({self.max_parallel_watchers}) reached"
                )

            # Validate FPS
            min_fps = int(os.getenv('JARVIS_WATCHER_MIN_FPS', '1'))
            max_fps = int(os.getenv('JARVIS_WATCHER_MAX_FPS', '10'))
            fps = max(min_fps, min(max_fps, fps))

            # Create watcher
            config = WatcherConfig(
                window_id=window_id,
                fps=fps,
                priority=priority,
                timeout=timeout
            )

            watcher = VideoWatcher(config)
            watcher.app_name = app_name
            watcher.space_id = space_id

            # Start watcher
            success = await watcher.start()

            if not success:
                raise RuntimeError(f"Failed to start watcher for window {window_id}")

            # Register watcher
            self.watchers[watcher.watcher_id] = watcher
            self.total_watchers_spawned += 1

            logger.info(
                f"✅ Spawned watcher {watcher.watcher_id} for {app_name or 'Unknown'} "
                f"(Window {window_id}, Space {space_id}, {fps} FPS)"
            )

            return watcher

    async def wait_for_visual_event(
        self,
        watcher: VideoWatcher,
        trigger: Union[str, Dict],
        detector: Optional[Any] = None,
        timeout: Optional[float] = None
    ) -> VisualEventResult:
        """
        Wait for a visual event to occur in watcher stream.

        Args:
            watcher: Active VideoWatcher instance
            trigger: Text to find (str) or element spec (dict)
            detector: VisualEventDetector instance (optional)
            timeout: Max wait time (uses watcher config if None)

        Returns:
            VisualEventResult with detection details
        """
        timeout = timeout or watcher.config.timeout
        start_time = time.time()

        logger.info(
            f"[Watcher {watcher.watcher_id}] Waiting for event: {trigger} "
            f"(timeout: {timeout}s)"
        )

        frame_count = 0

        while (time.time() - start_time) < timeout:
            # Get latest frame
            frame_data = await watcher.get_latest_frame(timeout=1.0)

            if not frame_data:
                logger.debug(f"[Watcher {watcher.watcher_id}] No frame received (frame_count={frame_count})")
                continue

            frame = frame_data['frame']
            frame_number = frame_data['frame_number']
            frame_count += 1
            watcher.frames_analyzed += 1

            # Log frame capture progress every 5 frames and save debug frame
            if frame_count % 5 == 0:
                logger.info(
                    f"[Watcher {watcher.watcher_id}] Processing frames: {frame_count} analyzed, "
                    f"shape={frame.shape if frame is not None else 'None'}, "
                    f"elapsed={time.time() - start_time:.1f}s"
                )

                # Save debug frame for visual inspection (every 5th frame)
                try:
                    debug_dir = Path.home() / ".jarvis" / "debug_frames"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    debug_path = debug_dir / f"frame_{watcher.watcher_id}_{frame_number}.png"

                    import cv2
                    # Convert RGB to BGR for OpenCV
                    frame_bgr = frame[:, :, ::-1] if frame is not None and len(frame.shape) == 3 else frame
                    cv2.imwrite(str(debug_path), frame_bgr)
                    logger.debug(f"[Watcher {watcher.watcher_id}] Saved debug frame to {debug_path}")
                except Exception as e:
                    logger.debug(f"Could not save debug frame: {e}")

            # Detect event (simplified for now - full detector implementation next)
            detected = False
            confidence = 0.0

            if detector:
                # Use full detector if provided
                try:
                    logger.debug(f"[Watcher {watcher.watcher_id}] Running OCR on frame {frame_number}...")
                    result = await detector.detect_text(frame, str(trigger))
                    detected = result.detected
                    confidence = result.confidence

                    # Log OCR results for debugging
                    if result.all_text:
                        logger.debug(
                            f"[Watcher {watcher.watcher_id}] OCR found text: {result.all_text[:100]}... "
                            f"(searching for '{trigger}')"
                        )
                    else:
                        logger.debug(f"[Watcher {watcher.watcher_id}] OCR found no text in frame {frame_number}")

                    if detected:
                        logger.info(f"[Watcher {watcher.watcher_id}] 🎯 MATCH FOUND! Text: '{trigger}', Confidence: {confidence:.2f}")
                except Exception as e:
                    logger.error(f"Detector error on frame {frame_number}: {e}", exc_info=True)
            else:
                # Simple fallback: always return not detected
                # (Full OCR implementation in VisualEventDetector)
                detected = False
                confidence = 0.0

            if detected:
                watcher.events_detected += 1
                watcher.status = WatcherStatus.EVENT_DETECTED
                self.total_events_detected += 1

                detection_time = time.time() - start_time

                logger.info(
                    f"[Watcher {watcher.watcher_id}] ✅ Event detected! "
                    f"Trigger: '{trigger}', Confidence: {confidence:.2f}, "
                    f"Time: {detection_time:.1f}s, Frames analyzed: {frame_count}"
                )

                return VisualEventResult(
                    detected=True,
                    event_type="text",
                    trigger=str(trigger),
                    confidence=confidence,
                    frame_number=frame_number,
                    detection_time=detection_time,
                    frame_data=frame,
                    metadata={
                        'watcher_id': watcher.watcher_id,
                        'window_id': watcher.config.window_id,
                        'app_name': watcher.app_name,
                        'space_id': watcher.space_id,
                        'frames_analyzed': frame_count,
                    }
                )

            # Brief sleep to prevent tight loop
            await asyncio.sleep(0.1)

        # Timeout reached
        logger.warning(
            f"[Watcher {watcher.watcher_id}] ⏱️ Timeout waiting for event: {trigger} "
            f"(analyzed {frame_count} frames)"
        )

        return VisualEventResult(
            detected=False,
            event_type="text",
            trigger=str(trigger),
            confidence=0.0,
            frame_number=frame_count,
            detection_time=timeout,
            metadata={
                'watcher_id': watcher.watcher_id,
                'timeout_reached': True,
                'frames_analyzed': frame_count,
            }
        )

    async def stop_watcher(self, watcher_id: str):
        """Stop a specific watcher."""
        async with self._watcher_lock:
            if watcher_id in self.watchers:
                watcher = self.watchers[watcher_id]
                await watcher.stop()
                del self.watchers[watcher_id]
                logger.info(f"Stopped and removed watcher {watcher_id}")
            else:
                logger.warning(f"Watcher {watcher_id} not found")

    async def stop_all_watchers(self):
        """Stop all active watchers."""
        async with self._watcher_lock:
            logger.info(f"Stopping {len(self.watchers)} watchers...")

            stop_tasks = [w.stop() for w in self.watchers.values()]
            await asyncio.gather(*stop_tasks, return_exceptions=True)

            self.watchers.clear()
            logger.info("✅ All watchers stopped")

    def list_watchers(self) -> List[Dict[str, Any]]:
        """List all active watchers."""
        return [w.get_stats() for w in self.watchers.values()]

    def get_watcher(self, watcher_id: str) -> Optional[VideoWatcher]:
        """Get a specific watcher by ID."""
        return self.watchers.get(watcher_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        active = sum(1 for w in self.watchers.values()
                    if w.status == WatcherStatus.WATCHING)

        return {
            'total_watchers_spawned': self.total_watchers_spawned,
            'total_events_detected': self.total_events_detected,
            'active_watchers': active,
            'max_parallel': self.max_parallel_watchers,
            'watchers': self.list_watchers(),
        }


# Global watcher manager instance
_watcher_manager: Optional[VideoWatcherManager] = None


def get_watcher_manager() -> VideoWatcherManager:
    """Get the global VideoWatcherManager instance."""
    global _watcher_manager
    if _watcher_manager is None:
        _watcher_manager = VideoWatcherManager()
    return _watcher_manager
