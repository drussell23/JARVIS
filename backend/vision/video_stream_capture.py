"""
Video Stream Capture Module for JARVIS Vision System
Real-time video capture with memory-safe processing and sliding window support
Designed for 16GB macOS systems with intelligent memory management
"""

import asyncio
import time
import os
import gc
import logging
import queue
import threading
from typing import Optional, Dict, Any, List, Tuple, Callable
from dataclasses import dataclass, field
from datetime import datetime
import weakref

import numpy as np
import psutil
from PIL import Image

# Try to import advanced macOS capture system (v10.6)
try:
    from .macos_video_capture_advanced import (
        create_video_capture,
        AdvancedCaptureConfig,
        AdvancedVideoCaptureManager,
        check_capture_availability,
        CaptureStatus,
        AVFOUNDATION_AVAILABLE,
        PYOBJC_AVAILABLE,
    )
    MACOS_CAPTURE_ADVANCED_AVAILABLE = True
    MACOS_CAPTURE_AVAILABLE = AVFOUNDATION_AVAILABLE  # Backward compatibility
except ImportError as e:
    MACOS_CAPTURE_ADVANCED_AVAILABLE = False
    MACOS_CAPTURE_AVAILABLE = False
    logging.warning(f"Advanced macOS capture not available: {e}")
    logging.info("Falling back to basic capture methods")

    # Fallback: Try legacy PyObjC imports
    try:
        import AVFoundation
        import CoreMedia
        from Quartz import CoreVideo
        from Cocoa import NSObject
        import objc
        from Foundation import NSRunLoop
        import libdispatch
        MACOS_CAPTURE_AVAILABLE = True
    except ImportError as e2:
        MACOS_CAPTURE_AVAILABLE = False
        logging.warning(f"macOS capture frameworks not available - will use fallback: {e2}")

# Import Swift bridge for better macOS integration
try:
    from .swift_video_bridge import SwiftVideoBridge, SwiftCaptureConfig
    SWIFT_BRIDGE_AVAILABLE = True
except ImportError as e:
    SWIFT_BRIDGE_AVAILABLE = False
    logging.warning(f"Swift video bridge not available: {e}")

# Try to import alternative capture methods
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

logger = logging.getLogger(__name__)

@dataclass
class VideoStreamConfig:
    """Configuration for video stream capture - NO HARDCODING"""
    # Stream settings
    target_fps: int = field(default_factory=lambda: int(os.getenv('VIDEO_STREAM_FPS', '30')))
    resolution: str = field(default_factory=lambda: os.getenv('VIDEO_STREAM_RESOLUTION', '1920x1080'))
    capture_display_id: int = field(default_factory=lambda: int(os.getenv('VIDEO_STREAM_DISPLAY_ID', '0')))
    
    # Memory management
    max_frame_buffer_size: int = field(default_factory=lambda: int(os.getenv('VIDEO_STREAM_BUFFER_SIZE', '10')))
    memory_limit_mb: int = field(default_factory=lambda: VideoStreamConfig._get_dynamic_memory_limit())
    frame_memory_threshold_mb: int = field(default_factory=lambda: int(os.getenv('VIDEO_STREAM_FRAME_THRESHOLD_MB', '50')))
    
    # Processing settings
    enable_sliding_window: bool = field(default_factory=lambda: os.getenv('VIDEO_STREAM_SLIDING_WINDOW', 'true').lower() == 'true')
    sliding_window_size: str = field(default_factory=lambda: os.getenv('VIDEO_STREAM_WINDOW_SIZE', '640x480'))
    sliding_window_overlap: float = field(default_factory=lambda: float(os.getenv('VIDEO_STREAM_WINDOW_OVERLAP', '0.2')))
    max_windows_per_frame: int = field(default_factory=lambda: int(os.getenv('VIDEO_STREAM_MAX_WINDOWS', '4')))
    
    # Analysis settings
    analyze_every_n_frames: int = field(default_factory=lambda: int(os.getenv('VIDEO_STREAM_ANALYZE_INTERVAL', '30')))  # Analyze every 1 second at 30fps
    enable_motion_detection: bool = field(default_factory=lambda: os.getenv('VIDEO_STREAM_MOTION_DETECTION', 'true').lower() == 'true')
    motion_threshold: float = field(default_factory=lambda: float(os.getenv('VIDEO_STREAM_MOTION_THRESHOLD', '0.1')))
    
    # Adaptive quality
    enable_adaptive_quality: bool = field(default_factory=lambda: os.getenv('VIDEO_STREAM_ADAPTIVE', 'true').lower() == 'true')
    min_fps: int = field(default_factory=lambda: int(os.getenv('VIDEO_STREAM_MIN_FPS', '10')))
    min_resolution: str = field(default_factory=lambda: os.getenv('VIDEO_STREAM_MIN_RES', '960x540'))
    
    @staticmethod
    def _get_dynamic_memory_limit() -> int:
        """Calculate dynamic memory limit based on available system RAM"""
        try:
            import psutil
            vm = psutil.virtual_memory()
            available_mb = vm.available / (1024 * 1024)
            
            # Use 20% of available RAM for video streaming
            dynamic_limit = int(available_mb * 0.2)
            
            # Apply reasonable bounds
            min_limit = 200  # At least 200MB
            max_limit = 1500  # Cap at 1.5GB
            
            final_limit = max(min_limit, min(dynamic_limit, max_limit))
            logging.getLogger(__name__).info(f"Video streaming dynamic memory: {final_limit}MB (20% of {available_mb:.0f}MB available)")
            return final_limit
        except Exception as e:
            logging.getLogger(__name__).warning(f"Failed to calculate dynamic memory, using default: {e}")
            return 500  # Default fallback

@dataclass
class FrameMetrics:
    """Metrics for frame processing"""
    timestamp: float
    frame_number: int
    processing_time: float
    memory_usage_mb: float
    motion_score: float
    windows_processed: int
    analysis_triggered: bool

class MemorySafeFrameBuffer:
    """Thread-safe circular buffer for video frames with memory management"""
    
    def __init__(self, max_frames: int, memory_limit_mb: int):
        self.max_frames = max_frames
        self.memory_limit_mb = memory_limit_mb
        self.frames = []
        self.lock = threading.Lock()
        self.total_memory_bytes = 0
        self.frame_counter = 0
        
    def add_frame(self, frame: np.ndarray) -> bool:
        """Add frame with memory checking"""
        frame_size = frame.nbytes
        
        with self.lock:
            # Check memory before adding
            while (self.total_memory_bytes + frame_size > self.memory_limit_mb * 1024 * 1024 or
                   len(self.frames) >= self.max_frames) and self.frames:
                # Remove oldest frame
                old_frame = self.frames.pop(0)
                self.total_memory_bytes -= old_frame['size']
            
            # Add new frame
            self.frames.append({
                'data': frame,
                'timestamp': time.time(),
                'frame_number': self.frame_counter,
                'size': frame_size
            })
            self.total_memory_bytes += frame_size
            self.frame_counter += 1
            
            return True
    
    def get_latest_frame(self) -> Optional[Dict[str, Any]]:
        """Get most recent frame"""
        with self.lock:
            return self.frames[-1] if self.frames else None
    
    def get_frames_for_analysis(self, count: int = 1) -> List[Dict[str, Any]]:
        """Get multiple frames for analysis"""
        with self.lock:
            return self.frames[-count:] if len(self.frames) >= count else self.frames.copy()
    
    def clear(self):
        """Clear all frames"""
        with self.lock:
            self.frames.clear()
            self.total_memory_bytes = 0
            gc.collect()

class MacOSVideoCapture:
    """Native macOS video capture using AVFoundation"""
    
    def __init__(self, config: VideoStreamConfig):
        self.config = config
        self.session = None
        self.output = None
        self.is_running = False
        self.frame_callback = None
        
        if not MACOS_CAPTURE_AVAILABLE:
            raise ImportError("macOS capture frameworks not available")
    
    def start_capture(self, frame_callback: Callable):
        """Start video capture session"""
        logger.info("MacOSVideoCapture.start_capture called")
        self.frame_callback = frame_callback
        
        try:
            # Create capture session
            logger.info("[MACOS] Creating AVCaptureSession...")
            self.session = AVFoundation.AVCaptureSession.alloc().init()
            logger.info("Created AVCaptureSession")
            
            # Configure session
            logger.info(f"[MACOS] Setting resolution preset for {self.config.resolution}")
            if self.config.resolution == '1920x1080':
                self.session.setSessionPreset_(AVFoundation.AVCaptureSessionPreset1920x1080)
            elif self.config.resolution == '1280x720':
                self.session.setSessionPreset_(AVFoundation.AVCaptureSessionPreset1280x720)
            else:
                self.session.setSessionPreset_(AVFoundation.AVCaptureSessionPreset640x480)
            
            # Create screen input
            display_id = self.config.capture_display_id
            logger.info(f"[MACOS] Creating screen input for display ID {display_id}")
            screen_input = AVFoundation.AVCaptureScreenInput.alloc().initWithDisplayID_(display_id)
        except Exception as e:
            logger.error(f"[MACOS] Error in start_capture: {e}", exc_info=True)
            raise
        
        if screen_input:
            # Configure capture settings
            screen_input.setMinFrameDuration_(CoreMedia.CMTimeMake(1, self.config.target_fps))
            screen_input.setCapturesCursor_(False)
            screen_input.setCapturesMouseClicks_(False)
            
            # Add input to session
            if self.session.canAddInput_(screen_input):
                self.session.addInput_(screen_input)
            
            # Create output
            self.output = AVFoundation.AVCaptureVideoDataOutput.alloc().init()
            self.output.setAlwaysDiscardsLateVideoFrames_(True)
            
            # Configure output pixel format
            self.output.setVideoSettings_({
                CoreVideo.kCVPixelBufferPixelFormatTypeKey: CoreVideo.kCVPixelFormatType_32BGRA
            })
            
            # Set delegate (frame callback)
            delegate = VideoFrameDelegate.alloc().initWithCallback_(self._handle_frame)
            # Create a serial dispatch queue for video processing
            queue = libdispatch.dispatch_queue_create(b"com.jarvis.videoqueue", None)
            self.output.setSampleBufferDelegate_queue_(delegate, queue)
            
            # Add output to session
            if self.session.canAddOutput_(self.output):
                self.session.addOutput_(self.output)
            
            # Start capture
            logger.info("Starting capture session...")
            self.session.startRunning()
            self.is_running = True
            logger.info("Started macOS video capture - purple indicator should be visible")
        else:
            logger.error("Failed to create screen input")
            raise RuntimeError("Failed to create screen input")
    
    def _handle_frame(self, sample_buffer):
        """Handle captured frame"""
        if self.frame_callback:
            # Convert CMSampleBuffer to numpy array
            image_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
            if image_buffer:
                CoreVideo.CVPixelBufferLockBaseAddress(image_buffer, 0)
                
                base_address = CoreVideo.CVPixelBufferGetBaseAddress(image_buffer)
                bytes_per_row = CoreVideo.CVPixelBufferGetBytesPerRow(image_buffer)
                height = CoreVideo.CVPixelBufferGetHeight(image_buffer)
                width = CoreVideo.CVPixelBufferGetWidth(image_buffer)
                
                # Create numpy array from pixel data
                frame = np.frombuffer(base_address, dtype=np.uint8)
                frame = frame.reshape((height, bytes_per_row // 4, 4))
                frame = frame[:, :width, :3]  # Remove alpha channel
                frame = frame[:, :, ::-1]  # BGRA to RGB
                
                CoreVideo.CVPixelBufferUnlockBaseAddress(image_buffer, 0)
                
                # Call the callback
                self.frame_callback(frame)
    
    def stop_capture(self):
        """Stop video capture"""
        if self.session and self.is_running:
            self.session.stopRunning()
            self.is_running = False
            logger.info("Stopped macOS video capture")

# Only define VideoFrameDelegate if macOS frameworks are available
if MACOS_CAPTURE_AVAILABLE:
    class VideoFrameDelegate(NSObject):
        """Delegate for handling video frames"""
        
        def initWithCallback_(self, callback):
            self = objc.super(VideoFrameDelegate, self).init()
            if self:
                self.callback = callback
            return self
        
        def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample_buffer, connection):
            """Handle frame capture"""
            self.callback(sample_buffer)
else:
    # Placeholder class when macOS frameworks aren't available
    class VideoFrameDelegate:
        """Placeholder delegate for non-macOS systems"""
        
        def __init__(self):
            raise NotImplementedError("VideoFrameDelegate requires macOS frameworks")

class VideoStreamCapture:
    """Main video stream capture manager with memory-safe processing"""
    
    def __init__(self, vision_analyzer, config: Optional[VideoStreamConfig] = None):
        self.vision_analyzer = vision_analyzer
        self.config = config or VideoStreamConfig()
        
        # State
        self.is_capturing = False
        self.capture_thread = None
        self.process_thread = None
        
        # Frame buffer
        self.frame_buffer = MemorySafeFrameBuffer(
            self.config.max_frame_buffer_size,
            self.config.memory_limit_mb
        )
        
        # Metrics
        self.metrics = []
        self.frames_processed = 0
        self.frames_analyzed = 0
        
        # Motion detection
        self.previous_frame = None
        self.motion_regions = []
        
        # Callbacks
        self.event_callbacks = {
            'frame_captured': weakref.WeakSet(),
            'frame_analyzed': weakref.WeakSet(),
            'motion_detected': weakref.WeakSet(),
            'memory_warning': weakref.WeakSet()
        }
        
        # Platform-specific capture
        self.capture_impl = None
        
        logger.info(f"Video Stream Capture initialized with config: {self.config}")
    
    async def start_streaming(self) -> bool:
        """Start video stream capture"""
        logger.info("[VIDEO] start_streaming called")
        logger.info(f"[VIDEO] Current state - is_capturing: {self.is_capturing}")

        if self.is_capturing:
            logger.warning("Video capture already running")
            return True  # Return True if already capturing instead of False

        # CRITICAL: Clean up any existing threads before creating new ones
        if self.capture_thread and self.capture_thread.is_alive():
            logger.warning("Old capture thread still running - stopping it first")
            await self.stop_streaming()
            # Wait a bit for cleanup
            await asyncio.sleep(0.5)

        if self.process_thread and self.process_thread.is_alive():
            logger.warning("Old process thread still running - stopping it first")
            await self.stop_streaming()
            # Wait a bit for cleanup
            await asyncio.sleep(0.5)

        try:
            # Check memory before starting
            logger.info(f"[VIDEO] Checking memory availability...")
            if not self._check_memory_available():
                logger.error("Insufficient memory for video streaming")
                return False
            logger.info(f"[VIDEO] Memory check passed")
            
            # Initialize capture implementation
            # Try ADVANCED macOS capture first (v10.6 - native AVFoundation)
            advanced_capture_started = False
            if MACOS_CAPTURE_ADVANCED_AVAILABLE:
                try:
                    logger.info("🚀 Starting Advanced macOS Capture (v10.6)...")

                    # Check system availability first
                    availability = check_capture_availability()
                    logger.info(f"   System check: {availability}")

                    # Create advanced capture configuration
                    capture_config = AdvancedCaptureConfig(
                        display_id=self.config.capture_display_id,
                        target_fps=self.config.target_fps,
                        resolution=self.config.resolution,
                        max_memory_mb=self.config.memory_limit_mb,
                        frame_buffer_size=self.config.max_frame_buffer_size,
                    )

                    # Create capture manager
                    self.advanced_capture = await create_video_capture(capture_config)

                    # Start capture with frame callback
                    success = await self.advanced_capture.start_capture(
                        self._on_frame_captured_async
                    )

                    if success:
                        logger.info("✅ Advanced AVFoundation capture active!")
                        logger.info("   🟣 Purple indicator should be visible!")
                        self.capture_method = 'advanced_avfoundation'
                        advanced_capture_started = True
                    else:
                        logger.warning("Advanced capture failed, trying fallback methods...")

                except Exception as e:
                    logger.warning(f"Advanced macOS capture error: {e}", exc_info=True)
                    advanced_capture_started = False

            # Try direct Swift capture if advanced capture failed
            swift_capture_started = False
            if not advanced_capture_started:
                try:
                    from .direct_swift_capture import start_direct_swift_capture

                    logger.info("🟣 Starting direct Swift capture for purple indicator...")
                    success = await start_direct_swift_capture()

                    if success:
                        logger.info("✅ Direct Swift capture active - purple indicator visible!")
                        self.capture_method = 'direct_swift'
                        swift_capture_started = True

                        # Start processing thread for frame analysis using screenshots
                        self.capture_thread = threading.Thread(
                            target=self._direct_swift_capture_loop,
                            daemon=True
                        )
                        self.capture_thread.start()

                    else:
                        logger.warning("Direct Swift capture failed, trying other methods...")

                except ImportError as e:
                    logger.warning(f"Direct Swift capture module not available: {e}")
                except Exception as e:
                    logger.warning(f"Direct Swift capture error: {e}")
                
            if not swift_capture_started:
                
                # Try simple purple indicator as second option
                try:
                    from .simple_purple_indicator import start_purple_indicator
                    
                    logger.info("🟣 Trying simple purple indicator...")
                    success = await start_purple_indicator()
                    
                    if success:
                        logger.info("✅ Purple indicator active!")
                        self.capture_method = 'purple_indicator'
                        swift_capture_started = True
                        
                        # Start processing thread for frame analysis using screenshots
                        self.capture_thread = threading.Thread(
                            target=self._screenshot_capture_loop,
                            daemon=True
                        )
                        self.capture_thread.start()
                        
                    else:
                        logger.warning("Purple indicator failed, trying other methods...")
                        
                except ImportError as e:
                    logger.warning(f"Purple indicator module not available: {e}")
                except Exception as e:
                    logger.warning(f"Purple indicator error: {e}")
                    
            if not swift_capture_started:
                # Try other methods as fallback
                if MACOS_CAPTURE_AVAILABLE:
                    logger.info("Trying native macOS capture...")
                    try:
                        from .macos_native_capture import start_native_capture
                        
                        # Start native capture with frame callback
                        if start_native_capture(self._on_frame_captured):
                            logger.info("✅ Native macOS capture started!")
                            self.capture_method = 'macos_native_direct'
                            
                            # Still need processing thread for frame analysis
                            self.capture_thread = threading.Thread(
                                target=self._native_capture_loop,
                                daemon=True
                            )
                            self.capture_thread.start()
                            
                        else:
                            logger.warning("Native capture failed, trying Swift bridge...")
                            raise Exception("Native capture failed")
                            
                    except Exception as e:
                        logger.warning(f"Direct native capture failed: {e}")
                        
                        # Try Swift bridge as fallback
                        if SWIFT_BRIDGE_AVAILABLE:
                            logger.info("Trying Swift video bridge...")
                            success = await self._start_swift_capture()
                            if success:
                                logger.info("Swift video capture started successfully")
                            else:
                                # Final fallback to original MacOS capture
                                logger.info("Falling back to original macOS video capture")
                                self.capture_impl = MacOSVideoCapture(self.config)
                                self.capture_impl.start_capture(self._on_frame_captured)
                                logger.info("MacOS video capture started successfully")
                        else:
                            # Use original implementation
                            logger.info("Using original macOS video capture")
                            self.capture_impl = MacOSVideoCapture(self.config)
                            self.capture_impl.start_capture(self._on_frame_captured)
                            logger.info("MacOS video capture started successfully")
                
                elif SWIFT_BRIDGE_AVAILABLE:
                    logger.info("Trying Swift video bridge as primary fallback...")
                    success = await self._start_swift_capture()
                    if success:
                        logger.info("Swift video capture started successfully")
                    else:
                        logger.error("All macOS capture methods failed")
                        raise Exception("No macOS capture method available")
                elif CV2_AVAILABLE:
                    # Fallback to OpenCV
                    logger.info(f"[VIDEO] Using OpenCV fallback (CV2_AVAILABLE = {CV2_AVAILABLE})")
                    self._start_cv2_capture()
                else:
                    # Final fallback to screenshot loop
                    logger.info("[VIDEO] Using screenshot loop fallback")
                    self._start_screenshot_loop()
            
            self.is_capturing = True
            
            # Start processing thread
            self.process_thread = threading.Thread(target=self._process_frames_loop)
            self.process_thread.daemon = True
            self.process_thread.start()
            
            logger.info("Video streaming started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start video streaming: {e}", exc_info=True)
            import traceback
            traceback.print_exc()
            self.is_capturing = False  # Reset state on failure
            return False
    
    def _on_frame_captured(self, frame: np.ndarray):
        """Handle captured frame (synchronous version for legacy compatibility)"""
        try:
            # Add to buffer
            self.frame_buffer.add_frame(frame)
            self.frames_processed += 1

            # Trigger callback
            asyncio.create_task(self._trigger_event('frame_captured', {
                'frame_number': self.frames_processed,
                'timestamp': time.time()
            }))
        except Exception as e:
            logger.error(f"Error in _on_frame_captured: {e}", exc_info=True)

    async def _on_frame_captured_async(self, frame: np.ndarray, metadata: Dict[str, Any]):
        """
        Handle captured frame from advanced capture system (v10.6)

        Args:
            frame: numpy array of captured frame (RGB format)
            metadata: frame metadata (timestamp, fps, dimensions, etc.)
        """
        try:
            # Add to buffer
            self.frame_buffer.add_frame(frame)
            self.frames_processed += 1

            # Update metrics from advanced capture
            if 'fps' in metadata:
                # Store current FPS for diagnostics
                self._current_fps = metadata['fps']

            # Trigger callback
            await self._trigger_event('frame_captured', {
                'frame_number': self.frames_processed,
                'timestamp': metadata.get('timestamp', time.time()),
                'fps': metadata.get('fps', 0.0),
                'width': metadata.get('width', 0),
                'height': metadata.get('height', 0),
            })

        except Exception as e:
            logger.error(f"Error in _on_frame_captured_async: {e}", exc_info=True)
    
    def _process_frames_loop(self):
        """Process frames in separate thread"""
        frame_count = 0
        
        while self.is_capturing:
            try:
                # Get latest frame
                frame_data = self.frame_buffer.get_latest_frame()
                if not frame_data:
                    time.sleep(0.033)  # ~30fps
                    continue
                
                frame = frame_data['data']
                frame_count += 1
                
                # Check if we should analyze this frame
                should_analyze = False
                
                # 1. Periodic analysis
                if frame_count % self.config.analyze_every_n_frames == 0:
                    should_analyze = True
                
                # 2. Motion-triggered analysis
                if self.config.enable_motion_detection:
                    motion_score = self._detect_motion(frame)
                    if motion_score > self.config.motion_threshold:
                        should_analyze = True
                        asyncio.create_task(self._trigger_event('motion_detected', {
                            'motion_score': motion_score,
                            'regions': self.motion_regions
                        }))
                
                # 3. Analyze frame if needed
                if should_analyze:
                    asyncio.create_task(self._analyze_frame(frame, frame_data['frame_number']))
                
                # Adaptive quality adjustment
                if self.config.enable_adaptive_quality:
                    self._adjust_quality_based_on_memory()
                
                # Small delay to prevent CPU overload
                time.sleep(0.001)
                
            except Exception as e:
                logger.error(f"Error processing frame: {e}")
    
    async def _analyze_frame(self, frame: np.ndarray, frame_number: int):
        """Analyze frame using Claude Vision API with sliding window if needed"""
        start_time = time.time()
        
        try:
            # Check if we should use sliding window
            if self.config.enable_sliding_window and self._should_use_sliding_window(frame):
                results = await self._analyze_with_sliding_window(frame)
            else:
                # Full frame analysis
                results = await self._analyze_full_frame(frame)
            
            self.frames_analyzed += 1
            
            # Record metrics
            metrics = FrameMetrics(
                timestamp=time.time(),
                frame_number=frame_number,
                processing_time=time.time() - start_time,
                memory_usage_mb=psutil.Process().memory_info().rss / 1024 / 1024,
                motion_score=0.0,  # Set by motion detection
                windows_processed=results.get('windows_processed', 1),
                analysis_triggered=True
            )
            self.metrics.append(metrics)
            
            # Trigger callback
            await self._trigger_event('frame_analyzed', {
                'frame_number': frame_number,
                'results': results,
                'metrics': metrics
            })
            
        except Exception as e:
            logger.error(f"Error analyzing frame: {e}")
    
    def _should_use_sliding_window(self, frame: np.ndarray) -> bool:
        """Determine if sliding window should be used"""
        height, width = frame.shape[:2]
        total_pixels = height * width
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        
        # Use sliding window if frame is large or memory is low
        return (total_pixels > 2_000_000 or  # >2MP
                available_mb < 3000 or  # <3GB available
                psutil.Process().memory_info().rss / 1024 / 1024 > self.config.frame_memory_threshold_mb)
    
    async def _analyze_with_sliding_window(self, frame: np.ndarray) -> Dict[str, Any]:
        """Analyze frame using sliding window approach"""
        # Parse window size
        w_width, w_height = map(int, self.config.sliding_window_size.split('x'))
        
        # Generate windows with priority on motion regions
        windows = self._generate_priority_windows(frame, w_width, w_height)
        
        # Analyze each window
        window_results = []
        for i, window in enumerate(windows[:self.config.max_windows_per_frame]):
            x, y, w, h = window['bounds']
            window_frame = frame[y:y+h, x:x+w]
            
            # Quick analysis per window
            result = await self.vision_analyzer.analyze_screenshot(
                window_frame,
                f"Analyze this region of the screen (window {i+1}/{len(windows)})",
                custom_config={'max_tokens': 300}  # Smaller response per window
            )
            
            window_results.append({
                'window': window,
                'analysis': result[0] if isinstance(result, tuple) else result
            })
        
        # Combine results
        return {
            'windows_processed': len(window_results),
            'results': window_results,
            'method': 'sliding_window'
        }
    
    async def _analyze_full_frame(self, frame: np.ndarray) -> Dict[str, Any]:
        """Analyze full frame"""
        # Optionally downsample if memory is tight
        if psutil.virtual_memory().available / 1024 / 1024 < 2000:
            # Downsample to half resolution
            frame = frame[::2, ::2]
        
        result = await self.vision_analyzer.analyze_screenshot(
            frame,
            "Analyze the current screen content"
        )
        
        return {
            'windows_processed': 1,
            'results': result[0] if isinstance(result, tuple) else result,
            'method': 'full_frame'
        }
    
    def _generate_priority_windows(self, frame: np.ndarray, w_width: int, w_height: int) -> List[Dict]:
        """Generate windows with priority on areas with motion or importance"""
        height, width = frame.shape[:2]
        windows = []
        
        # If we have motion regions, prioritize those
        if self.motion_regions:
            for region in self.motion_regions:
                windows.append({
                    'bounds': region,
                    'priority': 1.0,
                    'type': 'motion'
                })
        
        # Add regular grid windows
        overlap = self.config.sliding_window_overlap
        step_x = int(w_width * (1 - overlap))
        step_y = int(w_height * (1 - overlap))
        
        for y in range(0, height - w_height + 1, step_y):
            for x in range(0, width - w_width + 1, step_x):
                # Calculate priority based on position (center gets higher priority)
                center_x = x + w_width // 2
                center_y = y + w_height // 2
                dx = (center_x - width // 2) / width
                dy = (center_y - height // 2) / height
                distance = np.sqrt(dx**2 + dy**2)
                priority = 1.0 - min(distance, 1.0)
                
                windows.append({
                    'bounds': (x, y, w_width, w_height),
                    'priority': priority * 0.5,  # Regular windows have lower priority
                    'type': 'grid'
                })
        
        # Sort by priority
        windows.sort(key=lambda w: w['priority'], reverse=True)
        
        return windows
    
    def _detect_motion(self, frame: np.ndarray) -> float:
        """Detect motion between frames"""
        if self.previous_frame is None:
            self.previous_frame = frame
            return 0.0
        
        try:
            # Simple motion detection using frame difference
            diff = np.abs(frame.astype(np.float32) - self.previous_frame.astype(np.float32))
            motion_score = np.mean(diff) / 255.0
            
            # Find regions with significant motion
            if motion_score > self.config.motion_threshold:
                # Simple grid-based motion regions
                grid_size = 128
                h, w = frame.shape[:2]
                self.motion_regions = []
                
                for y in range(0, h, grid_size):
                    for x in range(0, w, grid_size):
                        region_diff = diff[y:min(y+grid_size, h), x:min(x+grid_size, w)]
                        if np.mean(region_diff) > self.config.motion_threshold * 255:
                            self.motion_regions.append((x, y, grid_size, grid_size))
            
            self.previous_frame = frame
            return motion_score
            
        except Exception as e:
            logger.error(f"Motion detection failed: {e}")
            return 0.0
    
    def _check_memory_available(self) -> bool:
        """Check if enough memory is available"""
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        process_mb = psutil.Process().memory_info().rss / 1024 / 1024
        
        # Need at least 1.5x the memory limit available
        return available_mb > self.config.memory_limit_mb * 1.5
    
    def _adjust_quality_based_on_memory(self):
        """Dynamically adjust capture quality based on memory"""
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        process_mb = psutil.Process().memory_info().rss / 1024 / 1024
        
        if available_mb < 2000 or process_mb > self.config.memory_limit_mb * 0.8:
            # Reduce quality
            if hasattr(self.capture_impl, 'config'):
                self.capture_impl.config.target_fps = max(
                    self.config.min_fps,
                    self.capture_impl.config.target_fps - 5
                )
                logger.warning(f"Reduced capture FPS to {self.capture_impl.config.target_fps}")
    
    async def stop_streaming(self):
        """Stop video streaming"""
        self.is_capturing = False

        # Stop advanced capture if using it (v10.6)
        if hasattr(self, 'capture_method') and self.capture_method == 'advanced_avfoundation':
            try:
                if hasattr(self, 'advanced_capture') and self.advanced_capture:
                    await self.advanced_capture.stop_capture()
                    logger.info("Stopped advanced AVFoundation capture")
            except Exception as e:
                logger.error(f"Error stopping advanced capture: {e}")

        # Stop purple indicator if using it
        elif hasattr(self, 'capture_method') and self.capture_method == 'purple_indicator':
            try:
                from .simple_purple_indicator import stop_purple_indicator
                stop_purple_indicator()
                logger.info("Stopped purple indicator")
            except Exception as e:
                logger.error(f"Error stopping purple indicator: {e}")
        # Stop direct Swift capture if using it
        elif hasattr(self, 'capture_method') and self.capture_method == 'direct_swift':
            try:
                from .direct_swift_capture import stop_direct_swift_capture
                stop_direct_swift_capture()
                logger.info("Stopped direct Swift capture")
            except Exception as e:
                logger.error(f"Error stopping direct Swift capture: {e}")
        # Stop native capture if using it
        elif hasattr(self, 'capture_method') and self.capture_method == 'macos_native_direct':
            try:
                from .macos_native_capture import stop_native_capture
                stop_native_capture()
                logger.info("Stopped native macOS capture")
            except Exception as e:
                logger.error(f"Error stopping native capture: {e}")
        # Stop Swift capture if active
        elif hasattr(self, 'using_persistent_swift') and self.using_persistent_swift:
            try:
                from .swift_video_capture_persistent import stop_persistent_video_capture
                await stop_persistent_video_capture()
                logger.info("Stopped persistent Swift capture")
            except Exception as e:
                logger.error(f"Error stopping persistent Swift capture: {e}")
        elif hasattr(self, 'swift_bridge') and self.swift_bridge:
            try:
                await self.swift_bridge.stop_capture()
                self.swift_bridge.cleanup()
            except Exception as e:
                logger.error(f"Error stopping Swift capture: {e}")
        
        # Stop other capture implementations
        if self.capture_impl:
            if hasattr(self.capture_impl, 'stop_capture'):
                self.capture_impl.stop_capture()
        
        # Wait for threads with proper cleanup
        if self.process_thread and self.process_thread.is_alive():
            logger.info("Waiting for process thread to stop...")
            self.process_thread.join(timeout=2.0)
            if self.process_thread.is_alive():
                logger.warning("Process thread did not stop within timeout - will be orphaned")
                # Mark as daemon so it doesn't block shutdown
                self.process_thread.daemon = True
            self.process_thread = None

        if self.capture_thread and self.capture_thread.is_alive():
            logger.info("Waiting for capture thread to stop...")
            self.capture_thread.join(timeout=2.0)
            if self.capture_thread.is_alive():
                logger.warning("Capture thread did not stop within timeout - will be orphaned")
                # Mark as daemon so it doesn't block shutdown
                self.capture_thread.daemon = True
            self.capture_thread = None

        # Clear buffers
        self.frame_buffer.clear()

        logger.info("Video streaming stopped")
    
    async def _trigger_event(self, event_type: str, data: Dict[str, Any]):
        """Trigger event callbacks"""
        if event_type in self.event_callbacks:
            callbacks = list(self.event_callbacks[event_type])
            for callback in callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(data)
                    else:
                        callback(data)
                except Exception as e:
                    logger.error(f"Error in callback for {event_type}: {e}")
    
    def register_callback(self, event_type: str, callback: Callable):
        """Register event callback"""
        if event_type in self.event_callbacks:
            self.event_callbacks[event_type].add(callback)
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get streaming metrics"""
        recent_metrics = self.metrics[-10:] if self.metrics else []

        # Determine capture method
        capture_method = 'unknown'
        if hasattr(self, 'capture_method'):
            capture_method = self.capture_method
        elif hasattr(self, 'swift_bridge') and self.swift_bridge:
            capture_method = 'swift_native'
        elif self.capture_impl and isinstance(self.capture_impl, MacOSVideoCapture):
            capture_method = 'macos_native'
        elif self.capture_thread:
            capture_method = 'screenshot_loop'

        metrics = {
            'is_capturing': self.is_capturing,
            'frames_processed': self.frames_processed,
            'frames_analyzed': self.frames_analyzed,
            'buffer_size': len(self.frame_buffer.frames),
            'memory_usage_mb': psutil.Process().memory_info().rss / 1024 / 1024,
            'recent_analysis': recent_metrics,
            'capture_method': capture_method,
            'swift_available': SWIFT_BRIDGE_AVAILABLE,
            'macos_available': MACOS_CAPTURE_AVAILABLE,
            'macos_advanced_available': MACOS_CAPTURE_ADVANCED_AVAILABLE,
            'pyobjc_available': PYOBJC_AVAILABLE if MACOS_CAPTURE_ADVANCED_AVAILABLE else False,
            'avfoundation_available': AVFOUNDATION_AVAILABLE if MACOS_CAPTURE_ADVANCED_AVAILABLE else False,
        }

        # Add advanced capture metrics if using advanced capture (v10.6)
        if hasattr(self, 'advanced_capture') and self.advanced_capture:
            try:
                advanced_metrics = self.advanced_capture.get_metrics()
                metrics['advanced_capture'] = advanced_metrics
                metrics['current_fps'] = advanced_metrics.get('current_fps', 0.0)
            except Exception as e:
                logger.error(f"Error getting advanced capture metrics: {e}")

        return metrics
    
    # Fallback methods
    def _start_cv2_capture(self):
        """Start capture using OpenCV"""
        logger.info("Starting OpenCV video capture (fallback)")
        # Implementation would go here
        pass
    
    async def _start_swift_capture(self) -> bool:
        """Start capture using Swift video bridge"""
        logger.info("[VIDEO] Attempting Swift video capture...")
        
        try:
            # Try persistent capture for purple indicator
            try:
                from .swift_video_capture_persistent import start_persistent_video_capture
                
                logger.info("[VIDEO] Using persistent Swift capture for purple indicator...")
                success = await start_persistent_video_capture()
                
                if success:
                    logger.info("[VIDEO] Persistent Swift capture started - purple indicator visible!")
                    
                    # Start monitoring thread
                    self.capture_thread = threading.Thread(
                        target=self._swift_capture_loop,
                        daemon=True
                    )
                    self.capture_thread.start()
                    
                    # Mark as using persistent Swift
                    self.capture_method = 'swift_native'
                    self.using_persistent_swift = True
                    
                    return True
                else:
                    logger.warning("[VIDEO] Persistent capture failed, trying regular Swift bridge...")
                    
            except Exception as e:
                logger.warning(f"[VIDEO] Persistent capture not available: {e}")
            
            # Fall back to regular Swift bridge
            # Create Swift configuration
            swift_config = SwiftCaptureConfig(
                display_id=self.config.capture_display_id,
                fps=self.config.target_fps,
                resolution=self.config.resolution
            )
            
            # Create bridge
            self.swift_bridge = SwiftVideoBridge(swift_config)
            
            # Ensure permissions
            logger.info("[VIDEO] Checking screen recording permissions...")
            has_permission = await self.swift_bridge.ensure_permission()
            
            if not has_permission:
                logger.error("[VIDEO] Screen recording permission denied")
                return False
            
            logger.info("[VIDEO] Screen recording permission granted")
            
            # Start capture
            result = await self.swift_bridge.start_capture()
            
            if result.get('success'):
                logger.info(f"[VIDEO] Swift capture started: {result.get('message')}")
                
                # Start monitoring thread for Swift capture
                self.capture_thread = threading.Thread(
                    target=self._swift_capture_loop,
                    daemon=True
                )
                self.capture_thread.start()
                
                # Mark Swift as capture method
                self.capture_method = 'swift_native'
                
                return True
            else:
                logger.error(f"[VIDEO] Swift capture failed: {result.get('error')}")
                return False
                
        except Exception as e:
            logger.error(f"[VIDEO] Error starting Swift capture: {e}")
            return False
    
    def _swift_capture_loop(self):
        """Monitor Swift capture and process frames"""
        while self.is_capturing:
            try:
                # For now, use screenshot fallback for frame processing
                # In a full implementation, Swift would send frames directly
                screenshot = asyncio.run(self.vision_analyzer.capture_screen())
                if screenshot:
                    frame = np.array(screenshot)
                    self._on_frame_captured(frame)
                
                # Sleep to achieve target FPS
                time.sleep(1.0 / self.config.target_fps)
                
            except Exception as e:
                logger.error(f"Swift capture loop error: {e}")
    
    def _native_capture_loop(self):
        """Process frames from native capture"""
        # Native capture handles frames via callback
        # This thread just keeps things alive and can do additional processing
        while self.is_capturing:
            try:
                time.sleep(0.1)  # Just keep thread alive
            except Exception as e:
                logger.error(f"Native capture loop error: {e}")
    
    def _direct_swift_capture_loop(self):
        """Process frames while direct Swift capture is running"""
        logger.info("[DIRECT_SWIFT] Capture loop started")
        while self.is_capturing:
            try:
                # For direct Swift capture, we use screenshot fallback for frame processing
                # The Swift process handles the actual recording and purple indicator
                screenshot = asyncio.run(self.vision_analyzer.capture_screen())
                if screenshot:
                    frame = np.array(screenshot)
                    self._on_frame_captured(frame)
                
                # Sleep to achieve target FPS
                time.sleep(1.0 / self.config.target_fps)
                
            except Exception as e:
                logger.error(f"Direct Swift capture loop error: {e}")
                
    def _screenshot_capture_loop(self):
        """Capture screenshots while purple indicator is active"""
        logger.info("[SCREENSHOT] Capture loop started with purple indicator")
        while self.is_capturing:
            try:
                # Use vision analyzer's capture method
                screenshot = asyncio.run(self.vision_analyzer.capture_screen())
                if screenshot:
                    frame = np.array(screenshot)
                    self._on_frame_captured(frame)
                
                # Sleep to achieve target FPS
                time.sleep(1.0 / self.config.target_fps)
                
            except Exception as e:
                logger.error(f"Screenshot capture error: {e}")
    
    def _start_screenshot_loop(self):
        """Start capture using screenshot loop"""
        logger.info("Starting screenshot loop capture (fallback)")
        
        def screenshot_loop():
            while self.is_capturing:
                try:
                    # Use vision analyzer's capture method
                    screenshot = asyncio.run(self.vision_analyzer.capture_screen())
                    if screenshot:
                        frame = np.array(screenshot)
                        self._on_frame_captured(frame)
                    
                    # Sleep to achieve target FPS
                    time.sleep(1.0 / self.config.target_fps)
                    
                except Exception as e:
                    logger.error(f"Screenshot capture error: {e}")
        
        self.capture_thread = threading.Thread(target=screenshot_loop)
        self.capture_thread.daemon = True
        self.capture_thread.start()