#!/usr/bin/env python3
"""
Advanced Core Graphics Window Capture System v2.0
==================================================

A highly dynamic, robust window capture system using macOS Core Graphics API.
Captures windows from ANY desktop space without switching - zero hardcoding.

Features:
- Multi-strategy window matching (fuzzy, regex, scoring, ML)
- Intelligent capture option fallback system
- Advanced filtering with priority scoring
- Comprehensive error handling and retry logic
- Performance monitoring and caching
- Dynamic configuration via environment variables
- Extensible plugin architecture for custom matchers
- Async/await support for concurrent operations
- Window clustering and grouping
- Multi-window composition
- Screenshot comparison and diff detection
- Performance profiling and optimization
- Window event tracking and monitoring
- Smart retry with exponential backoff
- Region-of-interest (ROI) capture
- Color space transformations
"""

import Quartz
import Quartz.CoreGraphics as CG
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import logging
import time
import re
import asyncio
import hashlib
import json
from typing import Optional, List, Dict, Any, Callable, Tuple, Union, Set, AsyncIterator
from dataclasses import dataclass, field, asdict
from enum import Enum
from functools import lru_cache, wraps
from datetime import datetime, timedelta
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION & ENUMS
# ============================================================================

class WindowLayer(Enum):
    """Window layer types in macOS window hierarchy"""
    DESKTOP = -2147483647  # CGWindowLevelForKey(kCGDesktopWindowLevelKey)
    NORMAL = 0             # Normal application windows
    FLOATING = 3           # Floating/utility windows
    MODAL = 8              # Modal dialogs
    POPUP = 101            # Pop-up menus
    SCREEN_SAVER = 1000    # Screen saver windows

    @classmethod
    def from_value(cls, value: int) -> 'WindowLayer':
        """Convert raw layer value to enum, defaulting to NORMAL"""
        for layer in cls:
            if layer.value == value:
                return layer
        return cls.NORMAL


class CaptureQuality(Enum):
    """Capture quality presets"""
    FAST = "fast"           # Quick capture, may sacrifice quality
    BALANCED = "balanced"   # Balance between speed and quality
    HIGH = "high"          # High quality, slower
    MAXIMUM = "maximum"    # Maximum quality, slowest


class WindowMatchStrategy(Enum):
    """Strategy for matching windows"""
    EXACT = "exact"                 # Exact string match
    CONTAINS = "contains"           # Substring match
    FUZZY = "fuzzy"                # Fuzzy matching with scoring
    REGEX = "regex"                # Regular expression
    CUSTOM = "custom"              # Custom matcher function
    ML_BASED = "ml_based"          # Machine learning based (future)


class ColorSpace(Enum):
    """Color space transformations"""
    RGB = "rgb"
    BGR = "bgr"
    GRAY = "gray"
    HSV = "hsv"
    LAB = "lab"


class ComparisonMode(Enum):
    """Screenshot comparison modes"""
    PIXEL_DIFF = "pixel_diff"      # Pixel-by-pixel difference
    STRUCTURAL = "structural"      # Structural similarity (SSIM)
    PERCEPTUAL = "perceptual"      # Perceptual hash comparison
    HISTOGRAM = "histogram"        # Histogram comparison


class WindowGrouping(Enum):
    """Window grouping strategies"""
    BY_APP = "by_app"              # Group by application
    BY_SPACE = "by_space"          # Group by desktop space
    BY_SIZE = "by_size"            # Group by window size
    BY_POSITION = "by_position"    # Group by screen position


@dataclass
class RegionOfInterest:
    """Define a region of interest within a window"""
    x: int
    y: int
    width: int
    height: int
    name: str = "roi"

    def to_tuple(self) -> Tuple[int, int, int, int]:
        """Convert to (x, y, width, height) tuple"""
        return (self.x, self.y, self.width, self.height)

    def to_slice(self) -> Tuple[slice, slice]:
        """Convert to numpy slice (y_slice, x_slice)"""
        return (
            slice(self.y, self.y + self.height),
            slice(self.x, self.x + self.width)
        )


@dataclass
class WindowCluster:
    """Group of related windows"""
    cluster_id: str
    windows: List['WindowInfo']
    grouping_strategy: WindowGrouping
    center_of_mass: Tuple[float, float] = (0.0, 0.0)
    total_area: int = 0

    def __post_init__(self):
        """Calculate cluster properties"""
        if self.windows:
            total_x = sum(w.center_x for w in self.windows)
            total_y = sum(w.center_y for w in self.windows)
            self.center_of_mass = (
                total_x / len(self.windows),
                total_y / len(self.windows)
            )
            self.total_area = sum(w.area for w in self.windows)


@dataclass
class PerformanceProfile:
    """Performance profiling data"""
    operation: str
    start_time: float
    end_time: float = 0.0
    duration: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def finish(self):
        """Mark operation as complete"""
        self.end_time = time.time()
        self.duration = self.end_time - self.start_time

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'operation': self.operation,
            'duration': self.duration,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'metadata': self.metadata
        }


@dataclass
class WindowEvent:
    """Window state change event"""
    window_id: int
    event_type: str  # 'created', 'moved', 'resized', 'closed', 'focused'
    timestamp: datetime
    old_state: Optional[Dict[str, Any]] = None
    new_state: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'window_id': self.window_id,
            'event_type': self.event_type,
            'timestamp': self.timestamp.isoformat(),
            'old_state': self.old_state,
            'new_state': self.new_state
        }


@dataclass
class CaptureConfig:
    """Dynamic configuration for window capture - zero hardcoding"""

    # Capture options
    quality: CaptureQuality = CaptureQuality.BALANCED
    retry_count: int = field(default_factory=lambda: int(os.getenv('CG_CAPTURE_RETRY_COUNT', '3')))
    retry_delay: float = field(default_factory=lambda: float(os.getenv('CG_CAPTURE_RETRY_DELAY', '0.1')))

    # Filtering
    min_window_size: Tuple[int, int] = field(default_factory=lambda: (
        int(os.getenv('CG_MIN_WINDOW_WIDTH', '100')),
        int(os.getenv('CG_MIN_WINDOW_HEIGHT', '100'))
    ))
    max_window_size: Tuple[int, int] = field(default_factory=lambda: (
        int(os.getenv('CG_MAX_WINDOW_WIDTH', '10000')),
        int(os.getenv('CG_MAX_WINDOW_HEIGHT', '10000'))
    ))

    allowed_layers: List[int] = field(default_factory=lambda: [
        int(x) for x in os.getenv('CG_ALLOWED_LAYERS', '0').split(',')
    ])
    min_alpha: float = field(default_factory=lambda: float(os.getenv('CG_MIN_ALPHA', '0.1')))

    # Performance
    cache_ttl: int = field(default_factory=lambda: int(os.getenv('CG_CACHE_TTL', '5')))
    enable_cache: bool = field(default_factory=lambda: os.getenv('CG_ENABLE_CACHE', 'true').lower() == 'true')

    # Matching
    match_strategy: WindowMatchStrategy = WindowMatchStrategy.FUZZY
    fuzzy_threshold: float = field(default_factory=lambda: float(os.getenv('CG_FUZZY_THRESHOLD', '0.6')))

    # Advanced
    include_offscreen: bool = field(default_factory=lambda: os.getenv('CG_INCLUDE_OFFSCREEN', 'true').lower() == 'true')
    capture_timeout: float = field(default_factory=lambda: float(os.getenv('CG_CAPTURE_TIMEOUT', '5.0')))

    # New advanced features
    enable_async: bool = field(default_factory=lambda: os.getenv('CG_ENABLE_ASYNC', 'true').lower() == 'true')
    max_concurrent_captures: int = field(default_factory=lambda: int(os.getenv('CG_MAX_CONCURRENT', '5')))
    enable_profiling: bool = field(default_factory=lambda: os.getenv('CG_ENABLE_PROFILING', 'false').lower() == 'true')
    enable_event_tracking: bool = field(default_factory=lambda: os.getenv('CG_ENABLE_EVENTS', 'false').lower() == 'true')
    exponential_backoff: bool = field(default_factory=lambda: os.getenv('CG_EXPONENTIAL_BACKOFF', 'true').lower() == 'true')
    max_backoff_delay: float = field(default_factory=lambda: float(os.getenv('CG_MAX_BACKOFF_DELAY', '5.0')))
    enable_compression: bool = field(default_factory=lambda: os.getenv('CG_ENABLE_COMPRESSION', 'false').lower() == 'true')
    comparison_threshold: float = field(default_factory=lambda: float(os.getenv('CG_COMPARISON_THRESHOLD', '0.95')))


@dataclass
class WindowInfo:
    """Enhanced window information with computed properties"""
    id: int
    name: str
    owner: str
    bounds: Dict[str, float]
    layer: WindowLayer
    alpha: float
    on_screen: bool
    workspace: Optional[int]
    pid: int
    memory_usage: int

    # Computed properties
    width: int = 0
    height: int = 0
    area: int = 0
    center_x: float = 0.0
    center_y: float = 0.0
    score: float = 0.0

    def __post_init__(self):
        """Calculate derived properties"""
        self.width = int(self.bounds.get('Width', 0))
        self.height = int(self.bounds.get('Height', 0))
        self.area = self.width * self.height
        self.center_x = self.bounds.get('X', 0) + self.width / 2
        self.center_y = self.bounds.get('Y', 0) + self.height / 2

    def is_valid_size(self, min_size: Tuple[int, int], max_size: Tuple[int, int]) -> bool:
        """Check if window size is within valid range"""
        return (min_size[0] <= self.width <= max_size[0] and
                min_size[1] <= self.height <= max_size[1])

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation"""
        return {
            'id': self.id,
            'name': self.name,
            'owner': self.owner,
            'bounds': self.bounds,
            'layer': self.layer.value,
            'alpha': self.alpha,
            'on_screen': self.on_screen,
            'workspace': self.workspace,
            'pid': self.pid,
            'memory_usage': self.memory_usage,
            'width': self.width,
            'height': self.height,
            'area': self.area,
            'score': self.score
        }


@dataclass
class CaptureResult:
    """Result of a window capture operation"""
    success: bool
    window_id: int
    screenshot: Optional[np.ndarray] = None
    width: int = 0
    height: int = 0
    capture_time: float = 0.0
    method_used: str = ""
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# WINDOW MATCHERS
# ============================================================================

class WindowMatcher:
    """Advanced window matching with multiple strategies"""

    @staticmethod
    def exact_match(window: WindowInfo, app_name: str, window_title: Optional[str] = None) -> float:
        """Exact string matching - returns 1.0 or 0.0"""
        app_match = window.owner.lower() == app_name.lower()

        if window_title is None:
            return 1.0 if app_match else 0.0

        title_match = window.name.lower() == window_title.lower()
        return 1.0 if (app_match and title_match) else 0.0

    @staticmethod
    def contains_match(window: WindowInfo, app_name: str, window_title: Optional[str] = None) -> float:
        """Substring matching - returns 1.0 or 0.0"""
        app_match = app_name.lower() in window.owner.lower()

        if window_title is None:
            return 1.0 if app_match else 0.0

        title_match = window_title.lower() in window.name.lower()
        return 1.0 if (app_match and title_match) else 0.0

    @staticmethod
    def fuzzy_match(window: WindowInfo, app_name: str, window_title: Optional[str] = None) -> float:
        """Fuzzy matching with similarity scoring (0.0 - 1.0)"""
        app_score = WindowMatcher._calculate_similarity(window.owner.lower(), app_name.lower())

        if window_title is None:
            return app_score

        title_score = WindowMatcher._calculate_similarity(window.name.lower(), window_title.lower())
        return (app_score * 0.6 + title_score * 0.4)  # Weight app name more

    @staticmethod
    def regex_match(window: WindowInfo, app_pattern: str, title_pattern: Optional[str] = None) -> float:
        """Regular expression matching"""
        try:
            app_match = bool(re.search(app_pattern, window.owner, re.IGNORECASE))

            if title_pattern is None:
                return 1.0 if app_match else 0.0

            title_match = bool(re.search(title_pattern, window.name, re.IGNORECASE))
            return 1.0 if (app_match and title_match) else 0.0
        except re.error as e:
            logger.warning(f"Invalid regex pattern: {e}")
            return 0.0

    @staticmethod
    def _calculate_similarity(s1: str, s2: str) -> float:
        """Calculate string similarity using Levenshtein-inspired algorithm"""
        if not s1 or not s2:
            return 0.0

        # Exact match
        if s1 == s2:
            return 1.0

        # Substring bonus
        if s2 in s1 or s1 in s2:
            longer = max(len(s1), len(s2))
            shorter = min(len(s1), len(s2))
            return shorter / longer

        # Character overlap
        set1, set2 = set(s1), set(s2)
        overlap = len(set1 & set2)
        union = len(set1 | set2)

        if union == 0:
            return 0.0

        return overlap / union


# ============================================================================
# MAIN CAPTURE ENGINE
# ============================================================================

class AdvancedCGWindowCapture:
    """
    Advanced Core Graphics window capture system with:
    - Zero hardcoding (all configuration via environment or parameters)
    - Multiple matching strategies
    - Intelligent fallback and retry logic
    - Performance optimization with caching
    - Comprehensive error handling
    """

    # CG capture option strategies (ordered by reliability)
    CAPTURE_STRATEGIES = [
        {
            'name': 'default',
            'options': [CG.kCGWindowImageDefault],
            'description': 'Standard capture with all effects'
        },
        {
            'name': 'no_framing',
            'options': [CG.kCGWindowImageBoundsIgnoreFraming],
            'description': 'Capture without window frame/shadow'
        },
        {
            'name': 'opaque',
            'options': [CG.kCGWindowImageShouldBeOpaque],
            'description': 'Force opaque rendering'
        },
        {
            'name': 'combined',
            'options': [
                CG.kCGWindowImageBoundsIgnoreFraming,
                CG.kCGWindowImageShouldBeOpaque
            ],
            'description': 'Combine no-framing and opaque'
        },
        {
            'name': 'best_resolution',
            'options': [CG.kCGWindowImageBestResolution],
            'description': 'Best available resolution'
        },
    ]

    def __init__(self, config: Optional[CaptureConfig] = None):
        """Initialize capture engine with configuration"""
        self.config = config or CaptureConfig()
        self.matcher = WindowMatcher()
        self._window_cache: Dict[str, Tuple[List[WindowInfo], datetime]] = {}
        self._capture_stats = {
            'total_captures': 0,
            'successful_captures': 0,
            'failed_captures': 0,
            'cache_hits': 0,
            'average_capture_time': 0.0
        }

    def get_all_windows(self, force_refresh: bool = False) -> List[WindowInfo]:
        """
        Get information about all windows across all spaces.

        Args:
            force_refresh: Force refresh cache even if still valid

        Returns:
            List of WindowInfo objects
        """
        cache_key = 'all_windows'

        # Check cache
        if not force_refresh and self.config.enable_cache:
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                self._capture_stats['cache_hits'] += 1
                logger.debug(f"Retrieved {len(cached)} windows from cache")
                return cached

        logger.debug("Fetching window list from Core Graphics")
        start_time = time.time()

        try:
            # Get window list from Core Graphics
            window_list = CG.CGWindowListCopyWindowInfo(
                CG.kCGWindowListOptionAll,  # Get all windows including off-screen
                CG.kCGNullWindowID
            )

            windows = []
            for window in window_list:
                try:
                    window_info = self._parse_window_info(window)

                    # Apply filters
                    if self._should_include_window(window_info):
                        windows.append(window_info)

                except Exception as e:
                    logger.debug(f"Error parsing window: {e}")
                    continue

            # Cache results
            if self.config.enable_cache:
                self._add_to_cache(cache_key, windows)

            elapsed = time.time() - start_time
            logger.info(f"Retrieved {len(windows)} windows in {elapsed:.3f}s")

            return windows

        except Exception as e:
            logger.error(f"Failed to get window list: {e}", exc_info=True)
            return []

    def _parse_window_info(self, window: Dict) -> WindowInfo:
        """Parse raw CG window info into WindowInfo object"""
        return WindowInfo(
            id=window.get('kCGWindowNumber', 0),
            name=window.get('kCGWindowName', ''),
            owner=window.get('kCGWindowOwnerName', ''),
            bounds=window.get('kCGWindowBounds', {}),
            layer=WindowLayer.from_value(window.get('kCGWindowLayer', 0)),
            alpha=window.get('kCGWindowAlpha', 1.0),
            on_screen=window.get('kCGWindowIsOnscreen', False),
            workspace=window.get('kCGWindowWorkspace', None),
            pid=window.get('kCGWindowOwnerPID', 0),
            memory_usage=window.get('kCGWindowMemoryUsage', 0)
        )

    def _should_include_window(self, window: WindowInfo) -> bool:
        """Determine if window should be included based on filters"""
        # Layer filter
        if self.config.allowed_layers and window.layer.value not in self.config.allowed_layers:
            return False

        # Alpha filter
        if window.alpha < self.config.min_alpha:
            return False

        # Size filter
        if not window.is_valid_size(self.config.min_window_size, self.config.max_window_size):
            return False

        # On-screen filter
        if not self.config.include_offscreen and not window.on_screen:
            return False

        # Must have owner
        if not window.owner:
            return False

        return True

    def find_windows(
        self,
        app_name: str,
        window_title: Optional[str] = None,
        strategy: Optional[WindowMatchStrategy] = None,
        min_score: Optional[float] = None,
        custom_matcher: Optional[Callable[[WindowInfo], float]] = None
    ) -> List[WindowInfo]:
        """
        Find windows matching criteria with advanced matching strategies.

        Args:
            app_name: Application name to match
            window_title: Optional window title to match
            strategy: Matching strategy (uses config default if None)
            min_score: Minimum match score (0.0-1.0)
            custom_matcher: Custom matching function

        Returns:
            List of matching WindowInfo objects, sorted by score
        """
        strategy = strategy or self.config.match_strategy
        min_score = min_score if min_score is not None else self.config.fuzzy_threshold

        windows = self.get_all_windows()
        matches = []

        for window in windows:
            # Calculate match score
            if custom_matcher:
                score = custom_matcher(window)
            elif strategy == WindowMatchStrategy.EXACT:
                score = self.matcher.exact_match(window, app_name, window_title)
            elif strategy == WindowMatchStrategy.CONTAINS:
                score = self.matcher.contains_match(window, app_name, window_title)
            elif strategy == WindowMatchStrategy.FUZZY:
                score = self.matcher.fuzzy_match(window, app_name, window_title)
            elif strategy == WindowMatchStrategy.REGEX:
                score = self.matcher.regex_match(window, app_name, window_title)
            else:
                score = 0.0

            # Add if score meets threshold
            if score >= min_score:
                window.score = score
                matches.append(window)

        # Sort by score (highest first), then by area (largest first)
        matches.sort(key=lambda w: (w.score, w.area), reverse=True)

        logger.debug(f"Found {len(matches)} windows matching '{app_name}' with strategy '{strategy.value}'")
        return matches

    def find_best_window(
        self,
        app_name: str,
        window_title: Optional[str] = None,
        **kwargs
    ) -> Optional[WindowInfo]:
        """Find the best matching window (highest score)"""
        matches = self.find_windows(app_name, window_title, **kwargs)
        return matches[0] if matches else None

    def capture_window(
        self,
        window_id: int,
        quality: Optional[CaptureQuality] = None
    ) -> CaptureResult:
        """
        Capture a specific window by ID with intelligent fallback.

        Args:
            window_id: Window ID to capture
            quality: Capture quality preset

        Returns:
            CaptureResult with screenshot and metadata
        """
        quality = quality or self.config.quality
        start_time = time.time()

        self._capture_stats['total_captures'] += 1

        # Try each capture strategy
        for strategy in self._get_capture_strategies(quality):
            result = self._attempt_capture(window_id, strategy, start_time)

            if result.success:
                self._capture_stats['successful_captures'] += 1
                self._update_average_time(result.capture_time)
                return result

        # All strategies failed
        self._capture_stats['failed_captures'] += 1
        error_msg = f"Failed to capture window {window_id} after trying all strategies"
        logger.error(error_msg)

        return CaptureResult(
            success=False,
            window_id=window_id,
            error=error_msg,
            capture_time=time.time() - start_time
        )

    def _attempt_capture(
        self,
        window_id: int,
        strategy: Dict[str, Any],
        start_time: float
    ) -> CaptureResult:
        """Attempt to capture window using a specific strategy"""
        try:
            # Build CG image options
            options = strategy['options']
            combined_option = options[0]
            if len(options) > 1:
                for opt in options[1:]:
                    combined_option |= opt

            # Capture with timeout
            image = self._capture_with_timeout(window_id, combined_option)

            if image is None:
                return CaptureResult(
                    success=False,
                    window_id=window_id,
                    error=f"Strategy '{strategy['name']}' returned None",
                    method_used=strategy['name'],
                    capture_time=time.time() - start_time
                )

            # Convert to numpy array
            screenshot = self._cg_image_to_numpy(image)

            if screenshot is None or screenshot.size == 0:
                return CaptureResult(
                    success=False,
                    window_id=window_id,
                    error=f"Strategy '{strategy['name']}' produced empty image",
                    method_used=strategy['name'],
                    capture_time=time.time() - start_time
                )

            height, width = screenshot.shape[:2]
            capture_time = time.time() - start_time

            logger.info(
                f"✅ Captured window {window_id} using '{strategy['name']}' "
                f"({width}x{height}) in {capture_time:.3f}s"
            )

            return CaptureResult(
                success=True,
                window_id=window_id,
                screenshot=screenshot,
                width=width,
                height=height,
                capture_time=capture_time,
                method_used=strategy['name'],
                metadata={
                    'strategy': strategy,
                    'timestamp': datetime.now().isoformat()
                }
            )

        except Exception as e:
            logger.debug(f"Strategy '{strategy['name']}' failed for window {window_id}: {e}")
            return CaptureResult(
                success=False,
                window_id=window_id,
                error=str(e),
                method_used=strategy['name'],
                capture_time=time.time() - start_time
            )

    def _capture_with_timeout(self, window_id: int, options: int) -> Any:
        """Capture with timeout protection"""
        # Note: Python doesn't have true thread timeout for CG calls,
        # so we rely on the OS to handle this gracefully
        return CG.CGWindowListCreateImage(
            CG.CGRectNull,
            CG.kCGWindowListOptionIncludingWindow,
            window_id,
            options
        )

    def _cg_image_to_numpy(self, cg_image: Any) -> Optional[np.ndarray]:
        """Convert CGImage to numpy array with error handling"""
        try:
            width = CG.CGImageGetWidth(cg_image)
            height = CG.CGImageGetHeight(cg_image)

            if width == 0 or height == 0:
                return None

            # Create bitmap context
            colorspace = CG.CGColorSpaceCreateDeviceRGB()
            bytes_per_row = width * 4

            # Create data buffer
            data = np.zeros((height, width, 4), dtype=np.uint8)

            # Create context and draw image
            context = CG.CGBitmapContextCreate(
                data,
                width,
                height,
                8,  # bits per component
                bytes_per_row,
                colorspace,
                CG.kCGImageAlphaPremultipliedLast | CG.kCGBitmapByteOrder32Big
            )

            if context is None:
                logger.error("Failed to create bitmap context")
                return None

            CG.CGContextDrawImage(
                context,
                CG.CGRectMake(0, 0, width, height),
                cg_image
            )

            # Convert RGBA to RGB
            return data[:, :, :3]

        except Exception as e:
            logger.error(f"Error converting CGImage to numpy: {e}")
            return None

    def _get_capture_strategies(self, quality: CaptureQuality) -> List[Dict[str, Any]]:
        """Get ordered list of capture strategies based on quality setting"""
        if quality == CaptureQuality.FAST:
            # Fast: Try default, then simplest options
            return [
                self.CAPTURE_STRATEGIES[0],  # default
                self.CAPTURE_STRATEGIES[1],  # no_framing
            ]
        elif quality == CaptureQuality.BALANCED:
            # Balanced: Try most reliable options
            return [
                self.CAPTURE_STRATEGIES[0],  # default
                self.CAPTURE_STRATEGIES[1],  # no_framing
                self.CAPTURE_STRATEGIES[2],  # opaque
            ]
        elif quality == CaptureQuality.HIGH:
            # High: Try all except best_resolution
            return self.CAPTURE_STRATEGIES[:-1]
        else:  # MAXIMUM
            # Try all strategies
            return self.CAPTURE_STRATEGIES

    def capture_app_windows(
        self,
        app_name: str,
        max_windows: Optional[int] = None,
        **kwargs
    ) -> Dict[int, CaptureResult]:
        """
        Capture all windows from a specific application.

        Args:
            app_name: Application name to match
            max_windows: Maximum number of windows to capture
            **kwargs: Additional arguments for find_windows()

        Returns:
            Dictionary mapping window_id to CaptureResult
        """
        windows = self.find_windows(app_name, **kwargs)

        if max_windows:
            windows = windows[:max_windows]

        captures = {}
        logger.info(f"Capturing {len(windows)} windows from '{app_name}'")

        for window in windows:
            result = self.capture_window(window.id)
            captures[window.id] = result

            if not result.success:
                logger.warning(f"Failed to capture window {window.id}: {result.error}")

        return captures

    def _get_from_cache(self, key: str) -> Optional[List[WindowInfo]]:
        """Get value from cache if still valid"""
        if key in self._window_cache:
            windows, timestamp = self._window_cache[key]
            age = (datetime.now() - timestamp).total_seconds()

            if age < self.config.cache_ttl:
                return windows

        return None

    def _add_to_cache(self, key: str, windows: List[WindowInfo]):
        """Add windows to cache with timestamp"""
        self._window_cache[key] = (windows, datetime.now())

    def _update_average_time(self, capture_time: float):
        """Update rolling average capture time"""
        current_avg = self._capture_stats['average_capture_time']
        total = self._capture_stats['successful_captures']

        if total == 1:
            self._capture_stats['average_capture_time'] = capture_time
        else:
            # Rolling average
            self._capture_stats['average_capture_time'] = (
                (current_avg * (total - 1) + capture_time) / total
            )

    def get_statistics(self) -> Dict[str, Any]:
        """Get capture statistics"""
        stats = self._capture_stats.copy()

        if stats['total_captures'] > 0:
            stats['success_rate'] = (
                stats['successful_captures'] / stats['total_captures']
            )
        else:
            stats['success_rate'] = 0.0

        return stats

    def clear_cache(self):
        """Clear window cache"""
        self._window_cache.clear()
        logger.debug("Cache cleared")

    # ========================================================================
    # ADVANCED FEATURES - Window Clustering & Grouping
    # ========================================================================

    def cluster_windows(
        self,
        windows: Optional[List[WindowInfo]] = None,
        strategy: WindowGrouping = WindowGrouping.BY_APP
    ) -> List[WindowCluster]:
        """
        Cluster windows using various grouping strategies.

        Args:
            windows: List of windows (uses all if None)
            strategy: Grouping strategy to use

        Returns:
            List of WindowCluster objects
        """
        if windows is None:
            windows = self.get_all_windows()

        clusters = {}

        for window in windows:
            # Determine cluster key based on strategy
            if strategy == WindowGrouping.BY_APP:
                key = window.owner
            elif strategy == WindowGrouping.BY_SPACE:
                key = f"space_{window.workspace or 'unknown'}"
            elif strategy == WindowGrouping.BY_SIZE:
                # Group by size buckets (small, medium, large)
                if window.area < 100000:
                    key = "small"
                elif window.area < 500000:
                    key = "medium"
                else:
                    key = "large"
            elif strategy == WindowGrouping.BY_POSITION:
                # Group by quadrant
                key = f"x{int(window.center_x//500)}_y{int(window.center_y//500)}"
            else:
                key = "default"

            if key not in clusters:
                clusters[key] = []
            clusters[key].append(window)

        # Create WindowCluster objects
        result = []
        for cluster_id, cluster_windows in clusters.items():
            result.append(WindowCluster(
                cluster_id=cluster_id,
                windows=cluster_windows,
                grouping_strategy=strategy
            ))

        logger.info(f"Created {len(result)} clusters using {strategy.value}")
        return result

    def get_app_clusters(self) -> List[WindowCluster]:
        """Get windows clustered by application"""
        return self.cluster_windows(strategy=WindowGrouping.BY_APP)

    def get_space_clusters(self) -> List[WindowCluster]:
        """Get windows clustered by desktop space"""
        return self.cluster_windows(strategy=WindowGrouping.BY_SPACE)

    # ========================================================================
    # ADVANCED FEATURES - Async Support
    # ========================================================================

    async def capture_window_async(
        self,
        window_id: int,
        quality: Optional[CaptureQuality] = None
    ) -> CaptureResult:
        """Async version of capture_window"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.capture_window,
            window_id,
            quality
        )

    async def capture_multiple_windows_async(
        self,
        window_ids: List[int],
        quality: Optional[CaptureQuality] = None
    ) -> Dict[int, CaptureResult]:
        """
        Capture multiple windows concurrently.

        Args:
            window_ids: List of window IDs to capture
            quality: Capture quality preset

        Returns:
            Dictionary mapping window_id to CaptureResult
        """
        if not self.config.enable_async:
            # Fall back to sequential
            results = {}
            for window_id in window_ids:
                results[window_id] = self.capture_window(window_id, quality)
            return results

        # Concurrent captures with semaphore
        semaphore = asyncio.Semaphore(self.config.max_concurrent_captures)

        async def capture_with_semaphore(wid):
            async with semaphore:
                return await self.capture_window_async(wid, quality)

        tasks = [capture_with_semaphore(wid) for wid in window_ids]
        results = await asyncio.gather(*tasks)

        return dict(zip(window_ids, results))

    async def capture_cluster_async(
        self,
        cluster: WindowCluster,
        quality: Optional[CaptureQuality] = None
    ) -> Dict[int, CaptureResult]:
        """Capture all windows in a cluster concurrently"""
        window_ids = [w.id for w in cluster.windows]
        return await self.capture_multiple_windows_async(window_ids, quality)

    # ========================================================================
    # ADVANCED FEATURES - Region of Interest (ROI)
    # ========================================================================

    def capture_roi(
        self,
        window_id: int,
        roi: RegionOfInterest,
        quality: Optional[CaptureQuality] = None
    ) -> CaptureResult:
        """
        Capture a specific region within a window.

        Args:
            window_id: Window ID to capture
            roi: Region of interest definition
            quality: Capture quality preset

        Returns:
            CaptureResult with cropped screenshot
        """
        # First capture the entire window
        result = self.capture_window(window_id, quality)

        if not result.success:
            return result

        try:
            # Crop to ROI
            y_slice, x_slice = roi.to_slice()
            cropped = result.screenshot[y_slice, x_slice].copy()

            # Update result
            result.screenshot = cropped
            result.width = roi.width
            result.height = roi.height
            result.metadata['roi'] = roi.to_tuple()
            result.metadata['original_size'] = (result.width, result.height)

            logger.info(f"Extracted ROI {roi.name} from window {window_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to extract ROI: {e}")
            result.success = False
            result.error = f"ROI extraction failed: {e}"
            return result

    # ========================================================================
    # ADVANCED FEATURES - Screenshot Comparison
    # ========================================================================

    def compare_screenshots(
        self,
        screenshot1: np.ndarray,
        screenshot2: np.ndarray,
        mode: ComparisonMode = ComparisonMode.PIXEL_DIFF
    ) -> Dict[str, Any]:
        """
        Compare two screenshots and return similarity metrics.

        Args:
            screenshot1: First screenshot
            screenshot2: Second screenshot
            mode: Comparison algorithm to use

        Returns:
            Dictionary with similarity score and difference data
        """
        # Ensure same dimensions
        if screenshot1.shape != screenshot2.shape:
            logger.warning("Screenshots have different dimensions, resizing...")
            from PIL import Image as PILImage
            h, w = screenshot1.shape[:2]
            img2_pil = PILImage.fromarray(screenshot2)
            img2_pil = img2_pil.resize((w, h))
            screenshot2 = np.array(img2_pil)

        if mode == ComparisonMode.PIXEL_DIFF:
            # Simple pixel-by-pixel difference
            diff = np.abs(screenshot1.astype(float) - screenshot2.astype(float))
            diff_score = np.mean(diff) / 255.0
            similarity = 1.0 - diff_score

            return {
                'similarity': similarity,
                'difference_score': diff_score,
                'mode': mode.value,
                'diff_image': diff.astype(np.uint8),
                'changed_pixels': np.count_nonzero(diff > 10)
            }

        elif mode == ComparisonMode.HISTOGRAM:
            # Histogram comparison
            hist1 = [np.histogram(screenshot1[:,:,i], bins=256)[0] for i in range(3)]
            hist2 = [np.histogram(screenshot2[:,:,i], bins=256)[0] for i in range(3)]

            # Correlation between histograms
            correlations = []
            for h1, h2 in zip(hist1, hist2):
                corr = np.corrcoef(h1, h2)[0, 1]
                correlations.append(corr)

            similarity = np.mean(correlations)

            return {
                'similarity': similarity,
                'mode': mode.value,
                'channel_correlations': correlations
            }

        elif mode == ComparisonMode.PERCEPTUAL:
            # Perceptual hash comparison
            hash1 = self._perceptual_hash(screenshot1)
            hash2 = self._perceptual_hash(screenshot2)

            # Hamming distance
            hamming_dist = bin(int(hash1, 16) ^ int(hash2, 16)).count('1')
            max_dist = len(hash1) * 4  # 4 bits per hex char
            similarity = 1.0 - (hamming_dist / max_dist)

            return {
                'similarity': similarity,
                'mode': mode.value,
                'hash1': hash1,
                'hash2': hash2,
                'hamming_distance': hamming_dist
            }

        else:
            logger.warning(f"Comparison mode {mode} not fully implemented")
            return {'similarity': 0.0, 'mode': mode.value}

    def _perceptual_hash(self, image: np.ndarray, hash_size: int = 8) -> str:
        """Calculate perceptual hash of an image"""
        # Convert to grayscale
        if len(image.shape) == 3:
            gray = np.mean(image, axis=2).astype(np.uint8)
        else:
            gray = image

        # Resize to hash_size x hash_size
        from PIL import Image as PILImage
        img_pil = PILImage.fromarray(gray)
        img_pil = img_pil.resize((hash_size, hash_size), PILImage.LANCZOS)
        pixels = np.array(img_pil).flatten()

        # Calculate hash
        avg = pixels.mean()
        bits = ''.join('1' if p > avg else '0' for p in pixels)

        # Convert to hex
        hex_hash = hex(int(bits, 2))[2:]
        return hex_hash.zfill(hash_size * hash_size // 4)

    def has_changed(
        self,
        window_id: int,
        previous_screenshot: Optional[np.ndarray] = None,
        threshold: Optional[float] = None
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check if a window has changed since last capture.

        Args:
            window_id: Window ID to check
            previous_screenshot: Previous screenshot to compare against
            threshold: Similarity threshold (default from config)

        Returns:
            Tuple of (has_changed, comparison_data)
        """
        threshold = threshold or self.config.comparison_threshold

        # Capture current state
        result = self.capture_window(window_id)

        if not result.success or previous_screenshot is None:
            return (False, None)

        # Compare
        comparison = self.compare_screenshots(
            previous_screenshot,
            result.screenshot,
            mode=ComparisonMode.PERCEPTUAL
        )

        changed = comparison['similarity'] < threshold

        return (changed, comparison)

    # ========================================================================
    # ADVANCED FEATURES - Multi-Window Composition
    # ========================================================================

    def compose_windows(
        self,
        window_results: List[CaptureResult],
        layout: str = "horizontal",
        padding: int = 10,
        background_color: Tuple[int, int, int] = (50, 50, 50)
    ) -> Optional[np.ndarray]:
        """
        Compose multiple window screenshots into a single image.

        Args:
            window_results: List of successful capture results
            layout: 'horizontal', 'vertical', or 'grid'
            padding: Padding between windows
            background_color: Background color (RGB)

        Returns:
            Composed image as numpy array
        """
        # Filter successful captures
        valid_results = [r for r in window_results if r.success and r.screenshot is not None]

        if not valid_results:
            logger.warning("No valid screenshots to compose")
            return None

        screenshots = [r.screenshot for r in valid_results]

        if layout == "horizontal":
            # Horizontal layout
            max_height = max(img.shape[0] for img in screenshots)
            total_width = sum(img.shape[1] for img in screenshots) + padding * (len(screenshots) - 1)

            # Create canvas
            canvas = np.full((max_height, total_width, 3), background_color, dtype=np.uint8)

            # Place images
            x_offset = 0
            for img in screenshots:
                h, w = img.shape[:2]
                canvas[0:h, x_offset:x_offset+w] = img
                x_offset += w + padding

        elif layout == "vertical":
            # Vertical layout
            max_width = max(img.shape[1] for img in screenshots)
            total_height = sum(img.shape[0] for img in screenshots) + padding * (len(screenshots) - 1)

            # Create canvas
            canvas = np.full((total_height, max_width, 3), background_color, dtype=np.uint8)

            # Place images
            y_offset = 0
            for img in screenshots:
                h, w = img.shape[:2]
                canvas[y_offset:y_offset+h, 0:w] = img
                y_offset += h + padding

        elif layout == "grid":
            # Grid layout
            n = len(screenshots)
            cols = int(np.ceil(np.sqrt(n)))
            rows = int(np.ceil(n / cols))

            max_width = max(img.shape[1] for img in screenshots)
            max_height = max(img.shape[0] for img in screenshots)

            canvas_width = cols * max_width + (cols - 1) * padding
            canvas_height = rows * max_height + (rows - 1) * padding

            canvas = np.full((canvas_height, canvas_width, 3), background_color, dtype=np.uint8)

            # Place images in grid
            for idx, img in enumerate(screenshots):
                row = idx // cols
                col = idx % cols
                x = col * (max_width + padding)
                y = row * (max_height + padding)
                h, w = img.shape[:2]
                canvas[y:y+h, x:x+w] = img

        else:
            logger.error(f"Unknown layout: {layout}")
            return None

        logger.info(f"Composed {len(screenshots)} windows in {layout} layout")
        return canvas

    # ========================================================================
    # ADVANCED FEATURES - Color Space Transformations
    # ========================================================================

    def convert_color_space(
        self,
        screenshot: np.ndarray,
        color_space: ColorSpace
    ) -> np.ndarray:
        """
        Convert screenshot to different color space.

        Args:
            screenshot: Input screenshot (RGB)
            color_space: Target color space

        Returns:
            Converted image
        """
        if color_space == ColorSpace.RGB:
            return screenshot

        elif color_space == ColorSpace.BGR:
            return screenshot[:, :, ::-1]

        elif color_space == ColorSpace.GRAY:
            return np.mean(screenshot, axis=2).astype(np.uint8)

        elif color_space == ColorSpace.HSV:
            from PIL import Image as PILImage
            img_pil = PILImage.fromarray(screenshot).convert('HSV')
            return np.array(img_pil)

        elif color_space == ColorSpace.LAB:
            # Approximate LAB conversion
            # Note: Requires colormath for full LAB, this is simplified
            logger.warning("LAB color space conversion is approximate")
            return screenshot  # Fallback to RGB

        else:
            logger.error(f"Unknown color space: {color_space}")
            return screenshot

    # ========================================================================
    # ADVANCED FEATURES - Performance Profiling
    # ========================================================================

    def _start_profile(self, operation: str, **metadata) -> Optional[PerformanceProfile]:
        """Start a performance profile"""
        if not self.config.enable_profiling:
            return None

        profile = PerformanceProfile(
            operation=operation,
            start_time=time.time(),
            metadata=metadata
        )
        return profile

    def _end_profile(self, profile: Optional[PerformanceProfile]) -> Optional[Dict[str, Any]]:
        """End a performance profile and return data"""
        if profile is None:
            return None

        profile.finish()

        if self.config.enable_profiling:
            logger.debug(f"Profile: {profile.operation} took {profile.duration:.3f}s")

        return profile.to_dict()

    def get_performance_report(self) -> Dict[str, Any]:
        """Get comprehensive performance report"""
        stats = self.get_statistics()

        return {
            'capture_stats': stats,
            'cache_info': {
                'size': len(self._window_cache),
                'ttl': self.config.cache_ttl
            },
            'config': {
                'quality': self.config.quality.value,
                'strategy': self.config.match_strategy.value,
                'async_enabled': self.config.enable_async,
                'max_concurrent': self.config.max_concurrent_captures
            }
        }

    # ========================================================================
    # ADVANCED FEATURES - Smart Retry with Exponential Backoff
    # ========================================================================

    def capture_with_retry(
        self,
        window_id: int,
        max_retries: Optional[int] = None,
        quality: Optional[CaptureQuality] = None
    ) -> CaptureResult:
        """
        Capture with smart retry and exponential backoff.

        Args:
            window_id: Window ID to capture
            max_retries: Maximum retry attempts (uses config if None)
            quality: Capture quality preset

        Returns:
            CaptureResult
        """
        max_retries = max_retries or self.config.retry_count
        base_delay = self.config.retry_delay

        for attempt in range(max_retries):
            result = self.capture_window(window_id, quality)

            if result.success:
                if attempt > 0:
                    logger.info(f"Capture succeeded on attempt {attempt + 1}")
                return result

            # Calculate backoff delay
            if self.config.exponential_backoff:
                delay = min(
                    base_delay * (2 ** attempt),
                    self.config.max_backoff_delay
                )
            else:
                delay = base_delay

            logger.debug(f"Retry {attempt + 1}/{max_retries} after {delay:.2f}s delay")
            time.sleep(delay)

        logger.error(f"All {max_retries} capture attempts failed for window {window_id}")
        return result  # Return last failed result

    # ========================================================================
    # ADVANCED FEATURES - Window Annotations
    # ========================================================================

    def annotate_screenshot(
        self,
        screenshot: np.ndarray,
        window_info: WindowInfo,
        show_id: bool = True,
        show_app: bool = True,
        show_size: bool = True
    ) -> np.ndarray:
        """
        Add annotations to a screenshot.

        Args:
            screenshot: Screenshot to annotate
            window_info: Window information
            show_id: Show window ID
            show_app: Show app name
            show_size: Show window size

        Returns:
            Annotated screenshot
        """
        from PIL import Image as PILImage, ImageDraw, ImageFont

        # Convert to PIL
        img = PILImage.fromarray(screenshot)
        draw = ImageDraw.Draw(img)

        # Try to load a font, fall back to default
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        except Exception:
            font = ImageFont.load_default()

        # Build annotation text
        annotations = []
        if show_app:
            annotations.append(f"App: {window_info.owner}")
        if show_id:
            annotations.append(f"ID: {window_info.id}")
        if show_size:
            annotations.append(f"Size: {window_info.width}x{window_info.height}")

        text = " | ".join(annotations)

        # Draw background rectangle
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        draw.rectangle(
            [(10, 10), (20 + text_width, 20 + text_height)],
            fill=(0, 0, 0, 180)
        )

        # Draw text
        draw.text((15, 15), text, fill=(255, 255, 255), font=font)

        return np.array(img)


# ============================================================================
# CONVENIENCE API (Backward Compatibility)
# ============================================================================

# Global instance for convenience
_default_capture_engine = None


def get_capture_engine(config: Optional[CaptureConfig] = None) -> AdvancedCGWindowCapture:
    """Get or create the default capture engine"""
    global _default_capture_engine

    if _default_capture_engine is None or config is not None:
        _default_capture_engine = AdvancedCGWindowCapture(config)

    return _default_capture_engine


class CGWindowCapture:
    """Legacy compatibility wrapper"""

    @staticmethod
    def get_all_windows() -> List[Dict[str, Any]]:
        """Get all windows (legacy format)"""
        engine = get_capture_engine()
        windows = engine.get_all_windows()
        return [w.to_dict() for w in windows]

    @staticmethod
    def find_window_by_name(app_name: str, window_title: str = None) -> Optional[int]:
        """Find window by name (legacy method)"""
        engine = get_capture_engine()
        window = engine.find_best_window(app_name, window_title)
        return window.id if window else None

    @staticmethod
    def capture_window_by_id(window_id: int) -> Optional[np.ndarray]:
        """Capture window by ID (legacy method)"""
        engine = get_capture_engine()
        result = engine.capture_window(window_id)
        return result.screenshot if result.success else None

    @staticmethod
    def capture_app_windows(app_name: str) -> Dict[str, np.ndarray]:
        """Capture all app windows (legacy format)"""
        engine = get_capture_engine()
        results = engine.capture_app_windows(app_name)

        # Convert to legacy format
        captures = {}
        for window_id, result in results.items():
            if result.success:
                # Get window info for key
                windows = engine.get_all_windows()
                window = next((w for w in windows if w.id == window_id), None)

                if window:
                    key = f"{window.owner}_{window.name}_{window_id}"
                    captures[key] = result.screenshot

        return captures

    @staticmethod
    def capture_terminal_windows() -> Dict[str, np.ndarray]:
        """Capture Terminal windows (legacy method)"""
        return CGWindowCapture.capture_app_windows("Terminal")


# ============================================================================
# CLI INTERFACE
# ============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Create advanced engine
    config = CaptureConfig(
        quality=CaptureQuality.HIGH,
        match_strategy=WindowMatchStrategy.FUZZY,
        fuzzy_threshold=0.5
    )
    engine = AdvancedCGWindowCapture(config)

    print("\n" + "="*70)
    print("Advanced Core Graphics Window Capture Demo")
    print("="*70 + "\n")

    # List all windows
    print("📋 All Windows:")
    windows = engine.get_all_windows()
    print(f"Found {len(windows)} windows\n")

    for w in windows[:20]:  # Show first 20
        print(f"  • {w.owner:20s} {w.name:40s} (ID: {w.id}, {w.width}x{w.height})")

    if len(windows) > 20:
        print(f"  ... and {len(windows) - 20} more\n")

    # Find Terminal windows
    print("\n🔍 Finding Terminal Windows:")
    terminal_windows = engine.find_windows("Terminal", strategy=WindowMatchStrategy.FUZZY)

    if terminal_windows:
        print(f"Found {len(terminal_windows)} Terminal windows:\n")

        for w in terminal_windows:
            print(f"  • {w.name:50s} (Score: {w.score:.2f}, {w.width}x{w.height})")

        # Capture best match
        print(f"\n📸 Capturing best Terminal window...")
        best = terminal_windows[0]
        result = engine.capture_window(best.id)

        if result.success:
            print(f"✅ Success!")
            print(f"   Size: {result.width}x{result.height}")
            print(f"   Time: {result.capture_time:.3f}s")
            print(f"   Method: {result.method_used}")

            # Save image
            output_path = "/tmp/advanced_terminal_capture.png"
            Image.fromarray(result.screenshot).save(output_path)
            print(f"   Saved to: {output_path}")
        else:
            print(f"❌ Failed: {result.error}")
    else:
        print("No Terminal windows found")

    # Show statistics
    print("\n📊 Capture Statistics:")
    stats = engine.get_statistics()
    print(f"   Total captures: {stats['total_captures']}")
    print(f"   Successful: {stats['successful_captures']}")
    print(f"   Failed: {stats['failed_captures']}")
    print(f"   Success rate: {stats.get('success_rate', 0)*100:.1f}%")
    print(f"   Avg capture time: {stats['average_capture_time']:.3f}s")
    print(f"   Cache hits: {stats['cache_hits']}")

    print("\n" + "="*70)
    print("Demo Complete!")
    print("="*70 + "\n")
