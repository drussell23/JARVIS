"""
Screen Sharing Module for JARVIS Vision System
Memory-safe, configurable screen sharing with WebRTC support
Designed for 16GB macOS systems with aggressive memory management
"""

import asyncio
import base64
import gc
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable, Set
from collections import deque
import weakref

import numpy as np
import psutil
from PIL import Image

logger = logging.getLogger(__name__)

try:
    import cv2
except ImportError:
    cv2 = None
    logger.warning("OpenCV not available - change detection will be limited")

@dataclass
class ScreenSharingConfig:
    """Dynamic configuration for screen sharing - NO HARDCODING"""
    # WebRTC Configuration
    enable_webrtc: bool = field(default_factory=lambda: os.getenv('SCREEN_SHARE_WEBRTC_ENABLED', 'true').lower() == 'true')
    signaling_server: str = field(default_factory=lambda: os.getenv('SCREEN_SHARE_SIGNAL_SERVER', 'ws://localhost:8765'))
    stun_servers: List[str] = field(default_factory=lambda: json.loads(os.getenv('SCREEN_SHARE_STUN_SERVERS', '["stun:stun.l.google.com:19302"]')))
    
    # Stream Quality Settings
    target_fps: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_TARGET_FPS', '15')))
    min_fps: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MIN_FPS', '5')))
    max_fps: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MAX_FPS', '30')))
    
    initial_quality: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_INITIAL_QUALITY', '75')))
    min_quality: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MIN_QUALITY', '30')))
    max_quality: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MAX_QUALITY', '95')))
    
    initial_resolution: str = field(default_factory=lambda: os.getenv('SCREEN_SHARE_INITIAL_RES', '1280x720'))
    min_resolution: str = field(default_factory=lambda: os.getenv('SCREEN_SHARE_MIN_RES', '640x360'))
    max_resolution: str = field(default_factory=lambda: os.getenv('SCREEN_SHARE_MAX_RES', '1920x1080'))
    
    # Memory Management
    max_buffer_frames: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MAX_BUFFER', '5')))
    memory_limit_mb: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MEMORY_LIMIT_MB', '500')))  # 500MB for screen sharing
    memory_warning_threshold: float = field(default_factory=lambda: float(os.getenv('SCREEN_SHARE_MEMORY_WARNING', '0.7')))  # 70% of limit
    memory_critical_threshold: float = field(default_factory=lambda: float(os.getenv('SCREEN_SHARE_MEMORY_CRITICAL', '0.9')))  # 90% of limit
    
    # Adaptive Quality Control
    enable_adaptive_quality: bool = field(default_factory=lambda: os.getenv('SCREEN_SHARE_ADAPTIVE_QUALITY', 'true').lower() == 'true')
    quality_check_interval: float = field(default_factory=lambda: float(os.getenv('SCREEN_SHARE_QUALITY_CHECK_INTERVAL', '2.0')))
    cpu_threshold_percent: float = field(default_factory=lambda: float(os.getenv('SCREEN_SHARE_CPU_THRESHOLD', '60')))
    enable_sliding_window: bool = field(default_factory=lambda: os.getenv('SCREEN_SHARE_SLIDING_WINDOW', 'true').lower() == 'true')
    sliding_window_threshold_pixels: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_SLIDING_THRESHOLD_PX', '800000')))
    
    # Network Settings
    enable_bandwidth_adaptation: bool = field(default_factory=lambda: os.getenv('SCREEN_SHARE_BANDWIDTH_ADAPT', 'true').lower() == 'true')
    initial_bitrate_kbps: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_INITIAL_BITRATE', '1000')))
    min_bitrate_kbps: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MIN_BITRATE', '300')))
    max_bitrate_kbps: int = field(default_factory=lambda: int(os.getenv('SCREEN_SHARE_MAX_BITRATE', '3000')))
    
    # Integration Settings
    share_with_vision: bool = field(default_factory=lambda: os.getenv('SCREEN_SHARE_WITH_VISION', 'true').lower() == 'true')
    vision_priority: str = field(default_factory=lambda: os.getenv('SCREEN_SHARE_VISION_PRIORITY', 'balanced'))  # 'vision_first', 'sharing_first', 'balanced'
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'target_fps': self.target_fps,
            'quality': self.initial_quality,
            'resolution': self.initial_resolution,
            'memory_limit_mb': self.memory_limit_mb,
            'adaptive_quality': self.enable_adaptive_quality,
            'vision_priority': self.vision_priority
        }

@dataclass
class StreamMetrics:
    """Metrics for monitoring stream performance"""
    frames_sent: int = 0
    frames_dropped: int = 0
    current_fps: float = 0.0
    current_bitrate_kbps: float = 0.0
    current_quality: int = 0
    current_resolution: str = ""
    memory_usage_mb: float = 0.0
    cpu_usage_percent: float = 0.0
    network_latency_ms: float = 0.0
    last_quality_adjustment: datetime = field(default_factory=datetime.now)
    quality_adjustments_count: int = 0

class AdaptiveQualityController:
    """Controls stream quality based on system resources"""
    
    def __init__(self, config: ScreenSharingConfig):
        self.config = config
        self.current_quality = config.initial_quality
        self.current_fps = config.target_fps
        self.current_resolution = self._parse_resolution(config.initial_resolution)
        self.adjustment_history = deque(maxlen=10)
        
    def _parse_resolution(self, res_str: str) -> tuple:
        """Parse resolution string to tuple"""
        try:
            width, height = res_str.split('x')
            return (int(width), int(height))
        except Exception:
            return (1280, 720)  # Default fallback
    
    def calculate_quality_adjustment(self, metrics: StreamMetrics, 
                                   system_memory_available_mb: float,
                                   system_cpu_percent: float) -> Dict[str, Any]:
        """Calculate necessary quality adjustments based on system state"""
        adjustments = {
            'quality': self.current_quality,
            'fps': self.current_fps,
            'resolution': self.current_resolution,
            'reason': []
        }
        
        # Memory-based adjustments
        memory_usage_ratio = metrics.memory_usage_mb / self.config.memory_limit_mb
        
        if memory_usage_ratio > self.config.memory_critical_threshold:
            # Critical memory - aggressive reduction
            adjustments['quality'] = self.config.min_quality
            adjustments['fps'] = self.config.min_fps
            adjustments['resolution'] = self._parse_resolution(self.config.min_resolution)
            adjustments['reason'].append('critical_memory')
            
        elif memory_usage_ratio > self.config.memory_warning_threshold:
            # High memory - moderate reduction
            adjustments['quality'] = max(
                self.config.min_quality,
                self.current_quality - 20
            )
            adjustments['fps'] = max(
                self.config.min_fps,
                self.current_fps - 5
            )
            adjustments['reason'].append('high_memory')
        
        # CPU-based adjustments
        if system_cpu_percent > self.config.cpu_threshold_percent:
            adjustments['fps'] = max(
                self.config.min_fps,
                adjustments['fps'] - 5
            )
            adjustments['reason'].append('high_cpu')
        
        # Network-based adjustments
        if metrics.frames_dropped > metrics.frames_sent * 0.1:  # >10% drop rate
            adjustments['quality'] = max(
                self.config.min_quality,
                adjustments['quality'] - 10
            )
            adjustments['reason'].append('high_packet_loss')
        
        # If everything is good, try to improve quality
        if not adjustments['reason'] and memory_usage_ratio < 0.5:
            if self.current_quality < self.config.max_quality:
                adjustments['quality'] = min(
                    self.config.max_quality,
                    self.current_quality + 5
                )
                adjustments['reason'].append('quality_improvement')
        
        # Record adjustment
        self.adjustment_history.append({
            'timestamp': datetime.now(),
            'adjustments': adjustments,
            'metrics': metrics
        })
        
        # Update current values
        self.current_quality = adjustments['quality']
        self.current_fps = adjustments['fps']
        self.current_resolution = adjustments['resolution']
        
        return adjustments

class MemorySafeFrameBuffer:
    """Memory-safe circular buffer for video frames"""
    
    def __init__(self, max_frames: int, memory_limit_mb: int):
        self.max_frames = max_frames
        self.memory_limit_mb = memory_limit_mb
        self.frames = deque(maxlen=max_frames)
        self.total_size_bytes = 0
        self.lock = asyncio.Lock()
        
    async def add_frame(self, frame: np.ndarray, timestamp: float) -> bool:
        """Add frame to buffer with memory checking"""
        async with self.lock:
            # Estimate frame size
            frame_size = frame.nbytes
            
            # Check if adding would exceed memory limit
            if self.total_size_bytes + frame_size > self.memory_limit_mb * 1024 * 1024:
                # Remove oldest frames until we have space
                while self.frames and self.total_size_bytes + frame_size > self.memory_limit_mb * 1024 * 1024:
                    old_frame = self.frames.popleft()
                    self.total_size_bytes -= old_frame['size']
            
            # Add new frame
            self.frames.append({
                'data': frame,
                'timestamp': timestamp,
                'size': frame_size
            })
            self.total_size_bytes += frame_size
            
            return True
    
    async def get_latest_frame(self) -> Optional[Dict[str, Any]]:
        """Get the most recent frame"""
        async with self.lock:
            return self.frames[-1] if self.frames else None
    
    async def clear(self):
        """Clear all frames and free memory"""
        async with self.lock:
            self.frames.clear()
            self.total_size_bytes = 0
            gc.collect()

class ScreenSharingManager:
    """Main screen sharing manager integrated with vision system"""
    
    def __init__(self, vision_analyzer, config: Optional[ScreenSharingConfig] = None):
        self.vision_analyzer = vision_analyzer
        self.config = config or ScreenSharingConfig()
        
        # State management
        self.is_sharing = False
        self.connected_peers: Set[str] = set()
        self.sharing_task = None
        self.quality_monitor_task = None
        
        # Components
        self.frame_buffer = MemorySafeFrameBuffer(
            self.config.max_buffer_frames,
            self.config.memory_limit_mb
        )
        self.quality_controller = AdaptiveQualityController(self.config)
        self.metrics = StreamMetrics()
        
        # Track previous frame for change detection
        self.previous_frame = None
        self.static_regions = []  # Regions that haven't changed
        
        # Callbacks with weak references
        self.event_callbacks = {
            'peer_connected': weakref.WeakSet(),
            'peer_disconnected': weakref.WeakSet(),
            'quality_changed': weakref.WeakSet(),
            'memory_warning': weakref.WeakSet(),
            'sharing_started': weakref.WeakSet(),
            'sharing_stopped': weakref.WeakSet()
        }
        
        # WebRTC components (lazy loaded)
        self.peer_connections = {}
        self.signaling_client = None
        
        logger.info(f"Screen Sharing Manager initialized with config: {self.config.to_dict()}")
    
    async def start_sharing(self, peer_id: Optional[str] = None) -> bool:
        """Start screen sharing session"""
        if self.is_sharing:
            logger.warning("Screen sharing already active")
            return False
        
        try:
            # Check memory before starting
            if not await self._check_memory_available():
                logger.error("Insufficient memory to start screen sharing")
                await self._trigger_event('memory_warning', {
                    'reason': 'insufficient_memory',
                    'available_mb': psutil.virtual_memory().available / 1024 / 1024
                })
                return False
            
            # Initialize WebRTC if enabled
            if self.config.enable_webrtc:
                await self._initialize_webrtc()
            
            self.is_sharing = True
            
            # Start sharing task
            self.sharing_task = asyncio.create_task(self._sharing_loop())
            self.quality_monitor_task = asyncio.create_task(self._quality_monitor_loop())
            
            # Notify callbacks
            await self._trigger_event('sharing_started', {
                'config': self.config.to_dict(),
                'timestamp': datetime.now()
            })
            
            logger.info("Screen sharing started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start screen sharing: {e}")
            self.is_sharing = False
            return False
    
    async def stop_sharing(self):
        """Stop screen sharing session"""
        if not self.is_sharing:
            return
        
        self.is_sharing = False
        
        # Cancel tasks
        for task in [self.sharing_task, self.quality_monitor_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Cleanup WebRTC
        if self.peer_connections:
            await self._cleanup_webrtc()
        
        # Clear frame buffer
        await self.frame_buffer.clear()
        
        # Reset metrics
        self.metrics = StreamMetrics()
        
        # Notify callbacks
        await self._trigger_event('sharing_stopped', {
            'timestamp': datetime.now()
        })
        
        logger.info("Screen sharing stopped")
    
    async def _sharing_loop(self):
        """Main sharing loop that captures and streams frames"""
        frame_interval = 1.0 / self.quality_controller.current_fps
        last_frame_time = 0
        
        while self.is_sharing:
            try:
                current_time = time.time()
                
                # Check if it's time for next frame
                if current_time - last_frame_time < frame_interval:
                    await asyncio.sleep(0.01)
                    continue
                
                # Capture frame based on vision priority
                frame = await self._capture_frame_with_priority()
                if frame is None:
                    continue
                
                # Add to buffer
                await self.frame_buffer.add_frame(frame, current_time)
                
                # Encode and send to peers if WebRTC is enabled
                if self.config.enable_webrtc and self.connected_peers:
                    await self._send_frame_to_peers(frame)
                
                # Update metrics
                self.metrics.frames_sent += 1
                self.metrics.current_fps = 1.0 / (current_time - last_frame_time)
                
                last_frame_time = current_time
                
                # Adjust frame interval based on current FPS setting
                frame_interval = 1.0 / self.quality_controller.current_fps
                
            except Exception as e:
                logger.error(f"Error in sharing loop: {e}")
                self.metrics.frames_dropped += 1
                await asyncio.sleep(0.1)
    
    async def _capture_frame_with_priority(self) -> Optional[np.ndarray]:
        """Capture frame considering vision system priority with sliding window support"""
        try:
            if self.config.vision_priority == 'vision_first':
                # Check if vision system is busy
                if hasattr(self.vision_analyzer, 'is_analyzing') and self.vision_analyzer.is_analyzing:
                    return None
            
            # Use vision system's capture method if available
            if hasattr(self.vision_analyzer, 'capture_screen'):
                screenshot = await self.vision_analyzer.capture_screen()
                if screenshot is not None:
                    # Convert to numpy array if needed
                    if isinstance(screenshot, Image.Image):
                        frame = np.array(screenshot)
                    else:
                        frame = screenshot
                    
                    # Check if we should use sliding window for memory efficiency
                    height, width = frame.shape[:2]
                    total_pixels = height * width
                    available_mb = psutil.virtual_memory().available / 1024 / 1024
                    
                    # Use sliding window if frame is large or memory is low
                    if (total_pixels > self.config.sliding_window_threshold_pixels or available_mb < 2000) and self.config.enable_sliding_window:
                        # For screen sharing, we can use a region-based approach
                        # Capture only changed regions or high-priority areas
                        if hasattr(self.vision_analyzer, '_generate_sliding_windows'):
                            windows = self.vision_analyzer._generate_sliding_windows(
                                frame, 
                                {
                                    'window_width': 640,
                                    'window_height': 480,
                                    'overlap': 0.1,  # Less overlap for streaming
                                    'max_windows': 1,  # Single window for real-time
                                    'prioritize_center': True,
                                    'adaptive_sizing': True
                                }
                            )
                            if windows:
                                # Use the highest priority window
                                x, y, w, h = windows[0]['bounds']
                                frame = frame[y:y+h, x:x+w]
                                logger.debug(f"Using sliding window region: {w}x{h} at ({x},{y})")
                    
                    # Resize based on current resolution setting
                    frame = await self._resize_frame(frame)
                    
                    return frame
            else:
                # Fallback to continuous analyzer
                continuous = await self.vision_analyzer.get_continuous_analyzer()
                if continuous and continuous.capture_history:
                    latest = continuous.capture_history[-1]
                    return latest.get('result')
            
        except Exception as e:
            logger.error(f"Error capturing frame: {e}")
            return None
    
    async def _resize_frame(self, frame: np.ndarray) -> np.ndarray:
        """Resize frame to current resolution setting"""
        target_width, target_height = self.quality_controller.current_resolution
        height, width = frame.shape[:2]
        
        if (width, height) == (target_width, target_height):
            return frame
        
        # Use PIL for high-quality resizing
        img = Image.fromarray(frame)
        img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
        
        return np.array(img)
    
    def _detect_changed_regions(self, current_frame: np.ndarray, 
                               previous_frame: Optional[np.ndarray]) -> List[tuple]:
        """Detect regions that have changed between frames for efficient streaming"""
        if previous_frame is None:
            return [(0, 0, current_frame.shape[1], current_frame.shape[0])]
        
        try:
            # Ensure frames have same shape
            if current_frame.shape != previous_frame.shape:
                return [(0, 0, current_frame.shape[1], current_frame.shape[0])]
            
            if cv2 is not None:
                # Use OpenCV for better performance if available
                diff = cv2.absdiff(current_frame, previous_frame)
                gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY) if len(diff.shape) == 3 else diff
                
                # Threshold to find changed pixels
                _, thresh = cv2.threshold(gray_diff, 30, 255, cv2.THRESH_BINARY)
                
                # Find contours of changed regions
                contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                # Get bounding boxes for changed regions
                changed_regions = []
                for contour in contours:
                    x, y, w, h = cv2.boundingRect(contour)
                    # Only include regions above minimum size
                    if w * h > 100:  # 10x10 minimum
                        changed_regions.append((x, y, w, h))
            else:
                # Fallback to numpy-based change detection
                diff = np.abs(current_frame.astype(np.float32) - previous_frame.astype(np.float32))
                
                # Convert to grayscale if needed
                if len(diff.shape) == 3:
                    gray_diff = np.mean(diff, axis=2)
                else:
                    gray_diff = diff
                
                # Simple grid-based change detection
                grid_size = 64  # Check 64x64 pixel blocks
                h, w = gray_diff.shape
                changed_regions = []
                
                for y in range(0, h, grid_size):
                    for x in range(0, w, grid_size):
                        block = gray_diff[y:min(y+grid_size, h), x:min(x+grid_size, w)]
                        if np.mean(block) > 30:  # Threshold
                            changed_regions.append((x, y, min(grid_size, w-x), min(grid_size, h-y)))
            
            # Merge overlapping regions
            merged_regions = self._merge_overlapping_regions(changed_regions)
            
            return merged_regions if merged_regions else [(0, 0, current_frame.shape[1], current_frame.shape[0])]
            
        except Exception as e:
            logger.debug(f"Change detection failed: {e}")
            return [(0, 0, current_frame.shape[1], current_frame.shape[0])]
    
    def _merge_overlapping_regions(self, regions: List[tuple]) -> List[tuple]:
        """Merge overlapping regions to reduce redundancy"""
        if not regions:
            return regions
        
        # Sort by x coordinate
        regions = sorted(regions, key=lambda r: r[0])
        
        merged = [regions[0]]
        for current in regions[1:]:
            last = merged[-1]
            
            # Check if regions overlap
            if (current[0] <= last[0] + last[2] and 
                current[1] <= last[1] + last[3] and
                current[0] + current[2] >= last[0] and
                current[1] + current[3] >= last[1]):
                
                # Merge regions
                x1 = min(last[0], current[0])
                y1 = min(last[1], current[1])
                x2 = max(last[0] + last[2], current[0] + current[2])
                y2 = max(last[1] + last[3], current[1] + current[3])
                
                merged[-1] = (x1, y1, x2 - x1, y2 - y1)
            else:
                merged.append(current)
        
        return merged
    
    async def _send_frame_to_peers(self, frame: np.ndarray):
        """Send frame to connected WebRTC peers"""
        # This would contain WebRTC-specific implementation
        # For now, it's a placeholder
        pass
    
    async def _quality_monitor_loop(self):
        """Monitor system resources and adjust quality"""
        while self.is_sharing:
            try:
                # Get system stats
                memory_available_mb = psutil.virtual_memory().available / 1024 / 1024
                cpu_percent = psutil.cpu_percent(interval=0.1)
                
                # Update metrics
                process = psutil.Process()
                self.metrics.memory_usage_mb = process.memory_info().rss / 1024 / 1024
                self.metrics.cpu_usage_percent = cpu_percent
                
                # Calculate quality adjustments
                if self.config.enable_adaptive_quality:
                    adjustments = self.quality_controller.calculate_quality_adjustment(
                        self.metrics,
                        memory_available_mb,
                        cpu_percent
                    )
                    
                    if adjustments['reason']:
                        self.metrics.quality_adjustments_count += 1
                        self.metrics.last_quality_adjustment = datetime.now()
                        
                        # Notify about quality change
                        await self._trigger_event('quality_changed', adjustments)
                        
                        logger.info(f"Quality adjusted: {adjustments}")
                
                await asyncio.sleep(self.config.quality_check_interval)
                
            except Exception as e:
                logger.error(f"Error in quality monitor: {e}")
                await asyncio.sleep(self.config.quality_check_interval)
    
    async def _check_memory_available(self) -> bool:
        """Check if enough memory is available for screen sharing"""
        memory = psutil.virtual_memory()
        available_mb = memory.available / 1024 / 1024
        
        # Need at least memory_limit_mb free
        return available_mb > self.config.memory_limit_mb * 1.5
    
    async def _initialize_webrtc(self):
        """Initialize WebRTC components"""
        # This would contain actual WebRTC initialization
        # Using aiortc or similar library
        pass
    
    async def _cleanup_webrtc(self):
        """Cleanup WebRTC connections"""
        for peer_id, pc in self.peer_connections.items():
            # Close peer connections
            pass
        self.peer_connections.clear()
    
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
        """Register callback for events"""
        if event_type in self.event_callbacks:
            self.event_callbacks[event_type].add(callback)
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current streaming metrics"""
        return {
            'frames_sent': self.metrics.frames_sent,
            'frames_dropped': self.metrics.frames_dropped,
            'drop_rate': self.metrics.frames_dropped / max(1, self.metrics.frames_sent),
            'current_fps': self.metrics.current_fps,
            'current_quality': self.metrics.current_quality,
            'current_resolution': f"{self.quality_controller.current_resolution[0]}x{self.quality_controller.current_resolution[1]}",
            'memory_usage_mb': self.metrics.memory_usage_mb,
            'cpu_usage_percent': self.metrics.cpu_usage_percent,
            'quality_adjustments': self.metrics.quality_adjustments_count,
            'is_sharing': self.is_sharing,
            'connected_peers': len(self.connected_peers),
            'buffer_frames': len(self.frame_buffer.frames),
            'config': self.config.to_dict()
        }
    
    async def add_peer(self, peer_id: str, offer: Optional[Dict] = None) -> bool:
        """Add a new peer for screen sharing"""
        if not self.is_sharing:
            logger.warning("Cannot add peer - sharing not active")
            return False
        
        try:
            # WebRTC peer connection setup would go here
            self.connected_peers.add(peer_id)
            
            await self._trigger_event('peer_connected', {
                'peer_id': peer_id,
                'timestamp': datetime.now()
            })
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to add peer {peer_id}: {e}")
            return False
    
    async def remove_peer(self, peer_id: str):
        """Remove a peer from screen sharing"""
        if peer_id in self.connected_peers:
            self.connected_peers.remove(peer_id)
            
            # Cleanup WebRTC connection
            if peer_id in self.peer_connections:
                # Close connection
                self.peer_connections.pop(peer_id)
            
            await self._trigger_event('peer_disconnected', {
                'peer_id': peer_id,
                'timestamp': datetime.now()
            })
    
    def get_sharing_url(self) -> Optional[str]:
        """Get URL for others to connect to screen share"""
        if not self.is_sharing:
            return None
        
        # This would return the actual sharing URL
        # For now, return a placeholder
        return f"screen-share://localhost:8765/share/{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"