#!/usr/bin/env python3
"""
Multi-Space Capture Engine for JARVIS Vision System
Implements comprehensive multi-space screenshot capture with intelligent caching and performance optimization
According to PRD requirements FR-1 and NFR-1
"""

import asyncio
import hashlib
import logging
import os
import subprocess
import tempfile
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Import purple indicator integration
try:
    from .direct_swift_capture import get_direct_capture, is_direct_capturing
except ImportError:
    logger.warning("Direct Swift capture not available - purple indicator will not work")
    get_direct_capture = None

    def is_direct_capturing():
        return False


# Import vision status manager
try:
    from .vision_status_manager import get_vision_status_manager
except ImportError:
    logger.warning("Vision status manager not available")
    get_vision_status_manager = None

# Import window capture manager for robust edge case handling
try:
    import sys
    from pathlib import Path as PathLib

    backend_path = PathLib(__file__).parent.parent
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))
    from context_intelligence.managers.capture_strategy_manager import (
        get_capture_strategy_manager,
        initialize_capture_strategy_manager,
    )
    from context_intelligence.managers.system_state_manager import (
        SystemHealth,
        get_system_state_manager,
    )
    from context_intelligence.managers.window_capture_manager import get_window_capture_manager

    WINDOW_CAPTURE_AVAILABLE = True
    SYSTEM_STATE_AVAILABLE = True
    CAPTURE_STRATEGY_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Context intelligence managers not available: {e}")
    get_window_capture_manager = None
    get_system_state_manager = None
    get_capture_strategy_manager = None
    initialize_capture_strategy_manager = None
    WINDOW_CAPTURE_AVAILABLE = False
    SYSTEM_STATE_AVAILABLE = False
    CAPTURE_STRATEGY_AVAILABLE = False


class CaptureMethod(Enum):
    """Available capture methods with fallback hierarchy"""

    SCREENCAPTURE_API = "screencapture_api"  # macOS screencapture with space
    SWIFT_CAPTURE = "swift_capture"  # Custom Swift implementation
    APPLESCRIPT_CAPTURE = "applescript_capture"  # AppleScript automation
    SPACE_SWITCH = "space_switch"  # Physical space switching
    CACHED = "cached"  # Retrieved from cache


class CaptureQuality(Enum):
    """Capture quality levels for performance optimization"""

    FULL = "full"  # Full resolution
    OPTIMIZED = "optimized"  # Balanced quality/performance
    FAST = "fast"  # Lower quality for speed
    THUMBNAIL = "thumbnail"  # Minimal for preview


@dataclass
class SpaceCaptureMetadata:
    """Metadata for captured screenshots"""

    space_id: int
    capture_time: datetime
    capture_method: CaptureMethod
    quality: CaptureQuality
    resolution: Tuple[int, int]
    file_size: int
    capture_duration: float
    applications: List[str] = field(default_factory=list)
    window_count: int = 0
    is_active_space: bool = False
    content_hash: str = ""


@dataclass
class SpaceCaptureRequest:
    """Request for space capture with configuration"""

    space_ids: List[int]
    quality: CaptureQuality = CaptureQuality.OPTIMIZED
    use_cache: bool = True
    cache_ttl: int = 30  # seconds
    priority: int = 5
    require_permission: bool = True
    callback: Optional[Callable] = None
    reason: str = "multi_space_analysis"
    parallel: bool = False  # Disabled to prevent segfaults on macOS
    max_workers: int = 2  # Reduced for safety


@dataclass
class SpaceCaptureResult:
    """Result of multi-space capture operation"""

    screenshots: Dict[int, np.ndarray]  # space_id -> screenshot
    metadata: Dict[int, SpaceCaptureMetadata]
    success: bool
    total_duration: float
    errors: Dict[int, str] = field(default_factory=dict)
    cache_hits: int = 0
    new_captures: int = 0


class MultiSpaceCaptureCache:
    """
    Smart caching system for multi-space screenshots.
    Adapts to macOS memory pressure dynamically (NO HARDCODING).
    """

    def __init__(self, max_size_mb: Optional[int] = None, default_ttl: Optional[int] = None):
        # Dynamic configuration from environment
        self.max_size_mb = max_size_mb or int(os.getenv("CACHE_MAX_SIZE_MB", "200"))
        self.default_ttl = default_ttl or int(os.getenv("CACHE_DEFAULT_TTL", "30"))

        # Async-compatible lock
        self.lock = asyncio.Lock()

        # Cache storage
        self.cache: OrderedDict[str, Tuple[np.ndarray, SpaceCaptureMetadata, datetime]] = (
            OrderedDict()
        )
        self.size_bytes = 0

        # Statistics
        self.hit_count = 0
        self.miss_count = 0

        # Memory pressure integration
        self._memory_manager = None
        self._dynamic_sizing = os.getenv("CACHE_DYNAMIC_SIZING", "true").lower() == "true"

        # Compression settings
        self.compression_quality = int(os.getenv("IMAGE_COMPRESSION_QUALITY", "85"))
        self.enable_aggressive_compression = (
            os.getenv("ENABLE_AGGRESSIVE_COMPRESSION", "true").lower() == "true"
        )

        # Parse thumbnail size
        thumbnail_size_str = os.getenv("IMAGE_THUMBNAIL_SIZE", "400,300")
        try:
            w, h = thumbnail_size_str.split(",")
            self.thumbnail_size = (int(w.strip()), int(h.strip()))
        except ValueError:
            self.thumbnail_size = (400, 300)
            logger.warning(f"Invalid IMAGE_THUMBNAIL_SIZE: {thumbnail_size_str}, using (400, 300)")

    def _get_cache_key(self, space_id: int, quality: CaptureQuality) -> str:
        """Generate cache key for space and quality"""
        return f"space_{space_id}_{quality.value}"

    def set_memory_manager(self, manager):
        """Set memory manager for dynamic sizing"""
        self._memory_manager = manager
        logger.info("✅ Cache connected to memory pressure manager")

    def _compress_image(self, screenshot: np.ndarray, quality: CaptureQuality) -> np.ndarray:
        """
        Compress image to reduce memory footprint.
        Applied based on quality level and memory pressure.
        """
        if not self.enable_aggressive_compression:
            return screenshot

        # Check if we should compress based on memory pressure
        should_compress = False
        if self._memory_manager:
            from .macos_memory_manager import MemoryPressure

            pressure = self._memory_manager.current_pressure
            # Compress on yellow/red pressure, or for thumbnail quality always
            should_compress = (
                pressure == MemoryPressure.YELLOW
                or pressure == MemoryPressure.RED
                or quality == CaptureQuality.THUMBNAIL
            )
        else:
            # No memory manager - compress thumbnails only
            should_compress = quality == CaptureQuality.THUMBNAIL

        if not should_compress:
            return screenshot

        try:
            # Convert to PIL Image
            img = Image.fromarray(screenshot)

            # Resize if thumbnail quality
            if quality == CaptureQuality.THUMBNAIL:
                img.thumbnail(self.thumbnail_size, Image.Resampling.LANCZOS)

            # Compress via JPEG encode/decode cycle (lossy compression)
            import io

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=self.compression_quality, optimize=True)
            buffer.seek(0)
            compressed_img = Image.open(buffer)

            # Convert back to numpy array
            compressed = np.array(compressed_img)

            reduction = 100 * (1 - compressed.nbytes / screenshot.nbytes)
            logger.debug(f"Compressed image: {reduction:.1f}% size reduction")

            return compressed

        except Exception as e:
            logger.warning(f"Image compression failed: {e}, using original")
            return screenshot

    async def get(
        self, space_id: int, quality: CaptureQuality, max_age: Optional[int] = None
    ) -> Optional[Tuple[np.ndarray, SpaceCaptureMetadata]]:
        """Retrieve cached screenshot if valid (async)"""
        key = self._get_cache_key(space_id, quality)

        async with self.lock:
            if key not in self.cache:
                self.miss_count += 1
                return None

            screenshot, metadata, timestamp = self.cache[key]

            # Check age
            age = (datetime.now() - timestamp).total_seconds()
            ttl = max_age or self.default_ttl

            if age > ttl:
                # Expired
                self._remove_entry(key)
                self.miss_count += 1
                return None

            # Move to end (LRU)
            self.cache.move_to_end(key)
            self.hit_count += 1

            return screenshot, metadata

    async def put(self, space_id: int, screenshot: np.ndarray, metadata: SpaceCaptureMetadata):
        """Cache a screenshot (async, memory-pressure aware with compression)"""
        key = self._get_cache_key(space_id, metadata.quality)

        # Apply compression before caching
        compressed_screenshot = self._compress_image(screenshot, metadata.quality)

        async with self.lock:
            # Remove old entry if exists
            if key in self.cache:
                self._remove_entry(key)

            # Get dynamic size limit based on memory pressure
            size_limit_mb = await self._get_effective_size_limit()

            # Check size limit
            entry_size = compressed_screenshot.nbytes
            while self.size_bytes + entry_size > size_limit_mb * 1024 * 1024 and self.cache:
                # Remove oldest
                oldest_key = next(iter(self.cache))
                self._remove_entry(oldest_key)

            # Add new entry (with compressed screenshot)
            self.cache[key] = (compressed_screenshot, metadata, datetime.now())
            self.size_bytes += entry_size

    async def _get_effective_size_limit(self) -> int:
        """Get effective cache size limit based on memory pressure"""
        if self._dynamic_sizing and self._memory_manager:
            return self._memory_manager.get_recommended_cache_size()
        return self.max_size_mb

    def _remove_entry(self, key: str):
        """Remove entry from cache"""
        if key in self.cache:
            screenshot, _, _ = self.cache[key]
            self.size_bytes -= screenshot.nbytes
            del self.cache[key]

    async def clear(self):
        """Clear all cached data (async)"""
        async with self.lock:
            self.cache.clear()
            self.size_bytes = 0
            logger.info("🗑️  Cache cleared")

    async def evict_to_target_size(self, target_mb: int):
        """Aggressively evict cache entries to reach target size"""
        async with self.lock:
            target_bytes = target_mb * 1024 * 1024
            evicted = 0

            while self.size_bytes > target_bytes and self.cache:
                # Remove oldest entry
                oldest_key = next(iter(self.cache))
                self._remove_entry(oldest_key)
                evicted += 1

            if evicted > 0:
                logger.info(f"🗑️  Evicted {evicted} cache entries due to memory pressure")

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics with compression info"""
        total_requests = self.hit_count + self.miss_count
        hit_rate = self.hit_count / total_requests if total_requests > 0 else 0

        return {
            "size_mb": self.size_bytes / (1024 * 1024),
            "max_size_mb": self.max_size_mb,
            "entries": len(self.cache),
            "hit_count": self.hit_count,
            "miss_count": self.miss_count,
            "hit_rate": hit_rate,
            "dynamic_sizing": self._dynamic_sizing,
            "compression_enabled": self.enable_aggressive_compression,
            "compression_quality": self.compression_quality,
            "thumbnail_size": self.thumbnail_size,
        }


class MultiSpaceCaptureEngine:
    """
    Core engine for capturing screenshots across multiple desktop spaces
    Implements PRD requirements FR-1.1 through FR-1.6
    """

    def __init__(self, cache_size_mb: int = 200, memory_manager=None):
        self.cache = MultiSpaceCaptureCache(max_size_mb=cache_size_mb)
        self.space_switcher = None  # Will be initialized if needed
        if _HAS_MANAGED_EXECUTOR:

            self.executor = ManagedThreadPoolExecutor(max_workers=4, name='pool')

        else:

            self.executor = ThreadPoolExecutor(max_workers=4)
        self.capture_methods = self._initialize_capture_methods()
        self.current_space_id = 1
        self._capture_lock = asyncio.Lock()
        # Subprocess management is now handled by AsyncSubprocessManager
        self.optimizer = None  # Will be set by vision intelligence
        self.monitoring_active = False  # Track if monitoring is active
        self.direct_capture = get_direct_capture() if get_direct_capture else None

        # Memory manager integration
        self.memory_manager = memory_manager
        if memory_manager:
            self.cache.set_memory_manager(memory_manager)

        # Monitored spaces configuration (empty = all spaces)
        monitored_env = os.getenv("MONITORED_SPACES", "")
        self.monitored_spaces = []
        if monitored_env:
            try:
                self.monitored_spaces = [int(s.strip()) for s in monitored_env.split(",")]
                logger.info(f"🎯 Monitoring specific spaces: {self.monitored_spaces}")
            except ValueError:
                logger.warning(f"Invalid MONITORED_SPACES format: {monitored_env}")

        # Monitor active only under pressure
        self.monitor_active_only_on_pressure = (
            os.getenv("MONITOR_ACTIVE_ONLY_PRESSURE", "true").lower() == "true"
        )
        self.system_state_manager = get_system_state_manager() if SYSTEM_STATE_AVAILABLE else None

        # Initialize Capture Strategy Manager for intelligent fallbacks
        self.capture_strategy_manager = None
        if CAPTURE_STRATEGY_AVAILABLE:
            try:
                # Try to get existing instance
                self.capture_strategy_manager = get_capture_strategy_manager()
                if not self.capture_strategy_manager:
                    # Initialize with default settings
                    self.capture_strategy_manager = initialize_capture_strategy_manager(
                        cache_ttl=60.0,  # 60-second cache
                        max_cache_entries=100,
                        enable_error_matrix=True,
                    )
                logger.info("✅ Capture Strategy Manager available for intelligent fallbacks")
            except Exception as e:
                logger.warning(f"Failed to initialize Capture Strategy Manager: {e}")

    def _initialize_capture_methods(self) -> Dict[CaptureMethod, bool]:
        """Check available capture methods"""
        methods = {}

        # Check screencapture command
        try:
            result = subprocess.run(["which", "screencapture"], capture_output=True)
            methods[CaptureMethod.SCREENCAPTURE_API] = result.returncode == 0
        except Exception:
            methods[CaptureMethod.SCREENCAPTURE_API] = False

        # Check for Swift capture (if implemented)
        swift_path = Path(__file__).parent / "swift_capture" / "capture_space"
        methods[CaptureMethod.SWIFT_CAPTURE] = swift_path.exists()

        # AppleScript is usually available on macOS
        methods[CaptureMethod.APPLESCRIPT_CAPTURE] = True

        # Space switching as fallback
        methods[CaptureMethod.SPACE_SWITCH] = True

        logger.info(
            f"Available capture methods: {[m.value for m, available in methods.items() if available]}"
        )
        return methods

    async def check_system_health(self) -> Tuple[bool, str, Optional[Any]]:
        """
        Check system health before capture operations.

        Returns:
            Tuple of (is_healthy, message, state_info)
        """
        if not SYSTEM_STATE_AVAILABLE or not self.system_state_manager:
            # System state manager not available, assume healthy
            return True, "System state checking not available", None

        try:
            state_info = await self.system_state_manager.check_system_state()

            if state_info.health == SystemHealth.HEALTHY:
                return True, "System is healthy", state_info

            elif state_info.health == SystemHealth.DEGRADED:
                # System degraded but usable
                warnings = "; ".join(state_info.warnings)
                logger.warning(f"[SYSTEM-HEALTH] System degraded: {warnings}")
                return True, f"System degraded: {warnings}", state_info

            else:
                # System unhealthy or critical
                failures = "; ".join(state_info.checks_failed)
                logger.error(f"[SYSTEM-HEALTH] System unhealthy: {failures}")

                # Generate helpful error message
                if not state_info.can_use_vision:
                    error_msg = state_info.display_status.message
                elif not state_info.can_use_spaces:
                    error_msg = state_info.yabai_status.message
                else:
                    error_msg = f"System unhealthy: {failures}"

                return False, error_msg, state_info

        except Exception as e:
            logger.error(f"[SYSTEM-HEALTH] Error checking system health: {e}")
            return True, "Health check failed, proceeding anyway", None

    async def capture_all_spaces(self, request: SpaceCaptureRequest) -> SpaceCaptureResult:
        """
        Capture screenshots from all requested spaces
        Implements PRD FR-1.1: Capture screenshots from any desktop space
        """
        start_time = time.time()

        # Check system health first
        is_healthy, health_message, state_info = await self.check_system_health()

        if not is_healthy:
            logger.error(f"[MULTI-SPACE] System health check failed: {health_message}")
            # Return error result
            return SpaceCaptureResult(
                screenshots={},
                metadata={},
                success=False,
                total_duration=time.time() - start_time,
                errors={-1: health_message},  # Use -1 as system-level error
            )

        if state_info and state_info.health == SystemHealth.DEGRADED:
            logger.warning(f"[MULTI-SPACE] System degraded: {health_message}")
            # Continue but log warnings

        # If monitoring is active with purple indicator, use that for better performance
        if self.monitoring_active and is_direct_capturing():
            logger.info("Using active monitoring session with purple indicator for captures")

        # Optimize request if optimizer available
        if self.optimizer:
            request = await self.optimizer.optimize_capture_request(request)
        results = {}
        metadata = {}
        errors = {}
        cache_hits = 0
        new_captures = 0

        # Check cache first if enabled
        spaces_to_capture = []
        for space_id in request.space_ids:
            if request.use_cache:
                cache_start = time.time()
                cached = await self.cache.get(space_id, request.quality, request.cache_ttl)
                cache_time = time.time() - cache_start

                if cached:
                    screenshot, meta = cached
                    results[space_id] = screenshot
                    metadata[space_id] = meta
                    cache_hits += 1
                    logger.debug(f"Cache hit for space {space_id}")

                    # Track performance if optimizer available
                    if self.optimizer:
                        self.optimizer.track_capture_performance(space_id, cache_time, True)
                    continue

            spaces_to_capture.append(space_id)

        # Capture remaining spaces
        if spaces_to_capture:
            if request.parallel and len(spaces_to_capture) > 1:
                # Parallel capture
                capture_results = await self._capture_parallel(spaces_to_capture, request)
            else:
                # Sequential capture
                capture_results = await self._capture_sequential(spaces_to_capture, request)

            # Process results
            for space_id, (success, screenshot, meta, error) in capture_results.items():
                if success and screenshot is not None:
                    results[space_id] = screenshot
                    metadata[space_id] = meta
                    new_captures += 1

                    # Cache the result
                    if request.use_cache:
                        self.cache.put(space_id, screenshot, meta)
                else:
                    errors[space_id] = error or "Unknown error"

        # Execute callback if provided
        if request.callback:
            await request.callback(results, metadata)

        total_duration = time.time() - start_time

        # Return partial results even if some spaces failed
        # This allows the vision system to work with available screenshots
        success = len(results) > 0  # Success if we captured at least one space

        logger.info(
            f"[MULTI-SPACE] Capture completed: {len(results)}/{len(request.space_ids)} spaces captured successfully"
        )

        if errors:
            logger.warning(
                f"[MULTI-SPACE] {len(errors)} spaces failed to capture: {list(errors.keys())}"
            )
            # Don't log individual errors to avoid spam unless specifically requested

        return SpaceCaptureResult(
            screenshots=results,
            metadata=metadata,
            success=success,  # True if any spaces captured successfully
            total_duration=total_duration,
            errors=errors,
            cache_hits=cache_hits,
            new_captures=new_captures,
        )

    async def _capture_parallel(self, space_ids: List[int], request: SpaceCaptureRequest) -> Dict[
        int,
        Tuple[bool, Optional[np.ndarray], Optional[SpaceCaptureMetadata], Optional[str]],
    ]:
        """Capture multiple spaces in parallel"""
        tasks = []
        for space_id in space_ids:
            task = asyncio.create_task(self._capture_single_space(space_id, request))
            tasks.append((space_id, task))

        results = {}
        for space_id, task in tasks:
            try:
                result = await task
                results[space_id] = result
            except Exception as e:
                logger.error(f"Failed to capture space {space_id}: {e}")
                results[space_id] = (False, None, None, str(e))

        return results

    async def _capture_sequential(self, space_ids: List[int], request: SpaceCaptureRequest) -> Dict[
        int,
        Tuple[bool, Optional[np.ndarray], Optional[SpaceCaptureMetadata], Optional[str]],
    ]:
        """Capture spaces sequentially"""
        results = {}

        for space_id in space_ids:
            try:
                result = await self._capture_single_space(space_id, request)
                results[space_id] = result
            except Exception as e:
                logger.error(f"Failed to capture space {space_id}: {e}")
                results[space_id] = (False, None, None, str(e))

        return results

    async def _capture_single_space(
        self, space_id: int, request: SpaceCaptureRequest
    ) -> Tuple[bool, Optional[np.ndarray], Optional[SpaceCaptureMetadata], Optional[str]]:
        """Capture a single space using best available method"""
        capture_start = time.time()

        # Adapt quality based on memory pressure
        effective_quality = self.get_quality_for_pressure(request.quality)

        # Create modified request with adapted quality if needed
        if effective_quality != request.quality:
            from dataclasses import replace

            request = replace(request, quality=effective_quality)

        # Try methods in order of preference
        # Determine current space to optimize method selection
        current_space_id = 1  # Default
        try:
            from .multi_space_window_detector import MultiSpaceWindowDetector

            detector = MultiSpaceWindowDetector()
            window_data = detector.get_all_windows_across_spaces()
            current_space_id = window_data.get("current_space", {}).get("id", 1)
        except Exception:
            pass

        # Smart method selection based on whether we're capturing current or other space
        if space_id == current_space_id:
            # Current space - use fast screencapture
            logger.info(f"[CAPTURE] Space {space_id} is current space - using screencapture")
            methods_to_try = [
                CaptureMethod.SCREENCAPTURE_API,
                CaptureMethod.SWIFT_CAPTURE,
            ]
        else:
            # Other space - Try CG capture first (no switching!), then fall back
            logger.info(
                f"Capturing space {space_id} (current: {current_space_id}) - attempting CG capture without switching"
            )

            # Try CG capture directly first
            cg_screenshot = await self._capture_with_cg_windows(space_id, request)
            if cg_screenshot is not None:
                logger.info(
                    f"[SUCCESS] Captured space {space_id} using CG Windows API without switching!"
                )
                # Create proper metadata with correct field names
                metadata = SpaceCaptureMetadata(
                    space_id=space_id,
                    capture_time=datetime.now(),
                    capture_method=CaptureMethod.SPACE_SWITCH,  # Using SPACE_SWITCH as CG falls under it
                    quality=effective_quality,
                    resolution=(cg_screenshot.shape[1], cg_screenshot.shape[0]),
                    file_size=cg_screenshot.nbytes,
                    capture_duration=0.0,
                    window_count=1,
                    content_hash="",
                )
                return True, cg_screenshot, metadata, None

            # If CG fails, try space switch as fallback
            methods_to_try = [CaptureMethod.SPACE_SWITCH]
            logger.warning(
                f"CG capture failed for space {space_id}, falling back to space switching"
            )

        for method in methods_to_try:

            if not self.capture_methods.get(method, False):
                # SPACE_SWITCH might not be in self.capture_methods, allow it anyway
                if method != CaptureMethod.SPACE_SWITCH:
                    continue

            try:
                screenshot = await self._capture_with_method(space_id, method, request)
                if screenshot is not None:
                    # Get window info for metadata
                    from .multi_space_window_detector import MultiSpaceWindowDetector

                    detector = MultiSpaceWindowDetector()
                    window_data = detector.get_all_windows_across_spaces()

                    space_windows = [
                        w
                        for w in window_data.get("windows", [])
                        if hasattr(w, "space_id") and w.space_id == space_id
                    ]

                    apps = list(set(w.app_name for w in space_windows if hasattr(w, "app_name")))

                    # Create metadata
                    metadata = SpaceCaptureMetadata(
                        space_id=space_id,
                        capture_time=datetime.now(),
                        capture_method=method,
                        quality=effective_quality,
                        resolution=(screenshot.shape[1], screenshot.shape[0]),
                        file_size=screenshot.nbytes,
                        capture_duration=time.time() - capture_start,
                        applications=apps[:10],  # Top 10 apps
                        window_count=len(space_windows),
                        is_active_space=(space_id == self.current_space_id),
                        content_hash=hashlib.md5(
                            screenshot.tobytes(), usedforsecurity=False
                        ).hexdigest(),
                    )

                    return True, screenshot, metadata, None

            except Exception as e:
                logger.warning(f"Method {method.value} failed for space {space_id}: {e}")
                continue

        return False, None, None, "All capture methods failed"

    async def _capture_with_method(
        self, space_id: int, method: CaptureMethod, request: SpaceCaptureRequest
    ) -> Optional[np.ndarray]:
        """Execute capture using specific method"""

        # If monitoring is active and we're using Swift, leverage the purple indicator session
        if (
            method == CaptureMethod.SWIFT_CAPTURE
            and self.monitoring_active
            and is_direct_capturing()
        ):
            logger.info(f"Using active purple indicator session for space {space_id} capture")
            # The Swift capture can leverage the existing session
            return await self._capture_with_swift_monitoring(space_id, request.quality)

        if method == CaptureMethod.SCREENCAPTURE_API:
            return await self._capture_with_screencapture(space_id, request.quality)
        elif method == CaptureMethod.SWIFT_CAPTURE:
            return await self._capture_with_swift(space_id, request.quality)
        elif method == CaptureMethod.APPLESCRIPT_CAPTURE:
            return await self._capture_with_applescript(space_id, request.quality)
        elif method == CaptureMethod.SPACE_SWITCH:
            # Try CG Window capture FIRST (no switching needed!)
            cg_result = await self._capture_with_cg_windows(space_id, request)
            if cg_result is not None:
                return cg_result
            # Fall back to space switching if CG fails
            return await self._capture_with_space_switch(space_id, request)
        else:
            return None

    async def _capture_with_screencapture(
        self, space_id: int, quality: CaptureQuality
    ) -> Optional[np.ndarray]:
        """Use macOS screencapture command with robust async subprocess management"""
        try:
            from .async_subprocess_manager import get_subprocess_manager
        except ImportError:
            from async_subprocess_manager import get_subprocess_manager

        temp_path = None
        temp_fd = None
        try:
            # Create temporary file securely
            temp_fd, temp_path = tempfile.mkstemp(suffix=".png", prefix=f"jarvis_space_{space_id}_")
            # Close the file descriptor so screencapture can write to it
            os.close(temp_fd)
            temp_fd = None  # Mark as closed

            # Build command
            cmd = ["screencapture", "-x", "-C"]  # -x: no sound, -C: capture cursor

            # Add quality settings
            if quality == CaptureQuality.FAST:
                cmd.extend(["-t", "jpg", "-q", "70"])
            elif quality == CaptureQuality.THUMBNAIL:
                cmd.extend(["-t", "jpg", "-q", "50"])

            cmd.append(temp_path)

            # Use robust async subprocess manager
            # Note: screencapture writes to file, no need to capture stdout/stderr
            # Setting capture_output=False prevents semaphore leaks
            manager = get_subprocess_manager()
            return_code, stdout, stderr = await manager.run_command(
                cmd, timeout=5.0, capture_output=False
            )

            if return_code != 0:
                error_msg = stderr.decode("utf-8", errors="ignore") if stderr else ""
                # v241.0: Exit code 1 with empty stderr almost always means missing
                # Screen Recording permission on macOS. Provide actionable diagnostic.
                if return_code == 1 and not error_msg.strip():
                    logger.error(
                        "Screencapture failed with code 1 (no stderr). This usually "
                        "means Screen Recording permission is not granted. Grant it "
                        "in System Settings → Privacy & Security → Screen Recording, "
                        "then restart JARVIS."
                    )
                else:
                    logger.error(
                        "Screencapture failed with code %d: %s",
                        return_code,
                        error_msg or "(no stderr)",
                    )
                return None

            # Check if capture succeeded
            if Path(temp_path).exists():
                # Load image
                image = Image.open(temp_path)
                screenshot = np.array(image)

                # Apply quality adjustments
                if quality == CaptureQuality.THUMBNAIL:
                    # Resize for thumbnail
                    image.thumbnail((400, 300), Image.Resampling.LANCZOS)
                    screenshot = np.array(image)

                # Clean up temp file
                try:
                    Path(temp_path).unlink()
                except Exception:
                    pass

                return screenshot

        except Exception as e:
            logger.error(f"Screencapture failed: {e}")
        finally:
            # Close temp file descriptor
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except Exception:
                    pass
            # Clean up temp file if it exists
            if temp_path and Path(temp_path).exists():
                try:
                    Path(temp_path).unlink()
                except Exception:
                    pass

        return None

    async def _capture_with_swift(
        self, space_id: int, quality: CaptureQuality
    ) -> Optional[np.ndarray]:
        """Use custom Swift capture tool"""
        # This would call a custom Swift implementation
        # For now, return None as placeholder
        return None

    async def _capture_with_swift_monitoring(
        self, space_id: int, quality: CaptureQuality
    ) -> Optional[np.ndarray]:
        """Use active Swift monitoring session with purple indicator"""
        try:
            # When monitoring is active, we already have screen recording permission
            # We can capture more efficiently without needing to request permission again
            logger.info(f"Capturing space {space_id} using active monitoring session")

            # For now, use screencapture but with the knowledge that we have permission
            # In the future, this could directly interface with the Swift capture buffer
            return await self._capture_with_screencapture(space_id, quality)

        except Exception as e:
            logger.error(f"Failed to capture with monitoring session: {e}")
            # Fall back to regular capture
            return await self._capture_with_screencapture(space_id, quality)

    async def _capture_with_applescript(
        self, space_id: int, quality: CaptureQuality
    ) -> Optional[np.ndarray]:
        """Use AppleScript automation"""
        # This would use AppleScript to automate capture
        # For now, return None as placeholder
        return None

    async def _capture_with_cg_windows(
        self, space_id: int, request: SpaceCaptureRequest
    ) -> Optional[np.ndarray]:
        """
        Capture windows from a specific space using Core Graphics API.
        This can capture windows from ANY space without switching!
        """
        try:
            logger.info(
                f"[CG_CAPTURE] Attempting to capture windows from space {space_id} without switching"
            )

            # Import our CG capture module
            try:
                from .cg_window_capture import CGWindowCapture
                from .multi_space_window_detector import MultiSpaceWindowDetector
            except ImportError:
                from cg_window_capture import CGWindowCapture
                from multi_space_window_detector import MultiSpaceWindowDetector

            detector = MultiSpaceWindowDetector()
            window_data = detector.get_all_windows_across_spaces()

            # Find windows in the target space
            # Windows are EnhancedWindowInfo objects, not dicts
            target_windows = []
            for window in window_data.get("windows", []):
                # Check if it's an object with attributes or a dict
                if hasattr(window, "space_id"):
                    if window.space_id == space_id:
                        target_windows.append(window)
                elif isinstance(window, dict) and window.get("space") == space_id:
                    target_windows.append(window)

            if not target_windows:
                logger.warning(f"[CG_CAPTURE] No windows found in space {space_id}")
                return None

            # Look specifically for Terminal first if that's what was requested
            query_wants_terminal = any(
                term in str(request.reason).lower() for term in ["terminal", "shell", "command"]
            )
            logger.info(
                f"[CG_CAPTURE] Query wants terminal: {query_wants_terminal}, reason: {request.reason}"
            )

            # Priority: Terminal > Other apps
            priority_order = []
            if query_wants_terminal:
                priority_order = [
                    "terminal",
                    "iterm",
                    "chrome",
                    "safari",
                    "firefox",
                    "code",
                ]
            else:
                priority_order = [
                    "chrome",
                    "safari",
                    "firefox",
                    "terminal",
                    "iterm",
                    "code",
                ]

            # Log all windows in target space
            logger.info(f"[CG_CAPTURE] Windows in space {space_id}:")
            # Handle both object attributes and dict keys for logging purposes here
            for window in target_windows:
                # Handle both object attributes and dict keys here for logging purposes
                if hasattr(window, "app_name"):
                    logger.info(f"  - {window.app_name}: {window.window_title}")
                else:
                    logger.info(
                        f"  - {window.get('app', 'Unknown')}: {window.get('title', 'Unknown')}"
                    )

            # Try to capture the most relevant window
            for priority_app in priority_order:
                for window in target_windows:
                    # Handle both object attributes and dict keys
                    if hasattr(window, "app_name"):
                        app_name = window.app_name
                        window_title = window.window_title
                    else:
                        app_name = window.get("app", "")
                        window_title = window.get("title", "")

                    if priority_app in app_name.lower():
                        logger.info(
                            f"[CG_CAPTURE] Found {app_name} '{window_title}' in space {space_id}, capturing..."
                        )

                        # Find window ID (try yabai first for more reliable ID)
                        if hasattr(window, "window_id"):
                            window_id = window.window_id
                        else:
                            window_id = CGWindowCapture.find_window_by_name(app_name, window_title)

                        if window_id:
                            # Try WindowCaptureManager first for robust edge case handling
                            if WINDOW_CAPTURE_AVAILABLE:
                                logger.info(
                                    f"[CG_CAPTURE] Using WindowCaptureManager for window {window_id} in space {space_id}"
                                )
                                try:
                                    capture_manager = get_window_capture_manager()
                                    capture_result = await capture_manager.capture_window(
                                        window_id=window_id, space_id=space_id, use_fallback=True
                                    )

                                    if capture_result.success:
                                        # Load image as numpy array
                                        img = Image.open(capture_result.image_path)
                                        screenshot = np.array(img)
                                        logger.info(
                                            f"[CG_CAPTURE] Successfully captured {app_name} (ID: {window_id}) from space {space_id} using WindowCaptureManager!"
                                        )
                                        if capture_result.status.value == "fallback_used":
                                            logger.info(
                                                f"[CG_CAPTURE] Used fallback window {capture_result.fallback_window_id}"
                                            )
                                        return screenshot
                                    else:
                                        logger.warning(
                                            f"[CG_CAPTURE] WindowCaptureManager failed: {capture_result.error}, falling back to CGWindowCapture"
                                        )
                                except Exception as e:
                                    logger.warning(
                                        f"[CG_CAPTURE] WindowCaptureManager exception: {e}, falling back to CGWindowCapture"
                                    )

                            # Fallback to CGWindowCapture
                            screenshot = CGWindowCapture.capture_window_by_id(window_id)
                            if screenshot is not None:
                                logger.info(
                                    f"[CG_CAPTURE] Successfully captured {app_name} (ID: {window_id}) from space {space_id} using CGWindowCapture!"
                                )
                                return screenshot

            logger.warning(f"[CG_CAPTURE] Could not capture any windows from space {space_id}")
            return None

        except Exception as e:
            logger.error(f"[CG_CAPTURE] Error during CG window capture: {e}", exc_info=True)
            return None

    async def _capture_with_space_switch(
        self, space_id: int, request: SpaceCaptureRequest
    ) -> Optional[np.ndarray]:
        """Use space switching as last resort"""
        logger.info(f"[SPACE_SWITCH] Attempting to capture space {space_id} via space switching")

        if not self.space_switcher:
            # Initialize space switcher
            logger.info("[SPACE_SWITCH] Initializing MinimalSpaceSwitcher")
            try:
                from .minimal_space_switcher import MinimalSpaceSwitcher, SwitchRequest
            except ImportError:
                from minimal_space_switcher import MinimalSpaceSwitcher, SwitchRequest

            self.space_switcher = MinimalSpaceSwitcher()
        else:
            # Import SwitchRequest here to avoid circular imports
            try:
                from .minimal_space_switcher import SwitchRequest
            except ImportError:
                from minimal_space_switcher import SwitchRequest

        # Create switch request
        switch_req = SwitchRequest(
            target_space=space_id,
            reason=request.reason,
            requester="capture_engine",
            priority=request.priority,
            require_permission=request.require_permission,
        )

        # Define capture callback
        async def capture_callback():
            # Use regular screencapture when on the space
            return await self._capture_with_screencapture(space_id, request.quality)

        # Quick switch and capture
        logger.info(f"[SPACE_SWITCH] Calling quick_capture_and_return for space {space_id}")
        try:
            screenshot = await self.space_switcher.quick_capture_and_return(
                space_id, capture_callback
            )
            if screenshot is not None:
                logger.info(f"[SPACE_SWITCH] Successfully captured space {space_id} via switching")
            else:
                logger.warning(
                    f"[SPACE_SWITCH] Failed to capture space {space_id} - screenshot is None"
                )
        except Exception as e:
            logger.error(f"[SPACE_SWITCH] Error during space switch capture: {e}", exc_info=True)
            screenshot = None

        return screenshot

    async def enumerate_spaces(self) -> List[int]:
        """
        Enumerate all available desktop spaces
        Implements PRD FR-1.2: Enumerate all available desktop spaces
        """
        try:
            # Use window detector to get space info
            from .multi_space_window_detector import MultiSpaceWindowDetector

            detector = MultiSpaceWindowDetector()
            window_data = detector.get_all_windows_across_spaces()

            # Try both 'spaces_list' and 'spaces' for compatibility
            spaces = window_data.get("spaces_list", window_data.get("spaces", []))
            space_ids = []

            for space in spaces:
                if hasattr(space, "space_id"):
                    space_ids.append(space.space_id)
                elif isinstance(space, dict) and "space_id" in space:
                    space_ids.append(space["space_id"])

            return sorted(space_ids)

        except Exception as e:
            logger.error(f"Failed to enumerate spaces: {e}")
            return [1]  # Default to single space

    async def get_current_space(self) -> int:
        """
        Get the currently active space
        Implements PRD FR-1.3: Identify which space is currently active
        """
        try:
            from .multi_space_window_detector import MultiSpaceWindowDetector

            detector = MultiSpaceWindowDetector()
            window_data = detector.get_all_windows_across_spaces()

            current_space = window_data.get("current_space", {})
            self.current_space_id = current_space.get("id", 1)

            return self.current_space_id

        except Exception as e:
            logger.error(f"Failed to get current space: {e}")
            return 1

    async def get_spaces_to_monitor(self) -> List[int]:
        """
        Determine which spaces to monitor based on:
        1. Configured monitored_spaces list (if set)
        2. Memory pressure (active only if under pressure)
        3. All spaces (if no pressure and no filter)
        """
        # If specific spaces configured, use those
        if self.monitored_spaces:
            return self.monitored_spaces

        # Get all available spaces
        all_spaces = await self.enumerate_spaces()

        # Check memory pressure if manager available
        if self.memory_manager and self.monitor_active_only_on_pressure:
            should_monitor_all = self.memory_manager.should_monitor_all_spaces()

            if not should_monitor_all:
                # Under pressure - monitor active space only
                current_space = await self.get_current_space()
                logger.info(f"🔴 Memory pressure - monitoring active space {current_space} only")
                return [current_space]

        # Normal conditions - monitor all spaces
        return all_spaces

    def get_quality_for_pressure(self, requested_quality: CaptureQuality) -> CaptureQuality:
        """
        Adapt capture quality based on memory pressure.
        Returns potentially degraded quality if system is under pressure.
        """
        if not self.memory_manager:
            return requested_quality

        recommended_quality_str = self.memory_manager.get_recommended_quality()

        # Map quality string to enum
        quality_map = {
            "full": CaptureQuality.FULL,
            "optimized": CaptureQuality.OPTIMIZED,
            "fast": CaptureQuality.FAST,
            "thumbnail": CaptureQuality.THUMBNAIL,
        }

        recommended_quality = quality_map.get(
            recommended_quality_str.lower(), CaptureQuality.OPTIMIZED
        )

        # Don't upgrade quality, only maintain or degrade
        if recommended_quality.value < requested_quality.value:
            logger.info(
                f"⚠️ Reducing capture quality from {requested_quality.value} to {recommended_quality.value} due to memory pressure"
            )
            return recommended_quality

        return requested_quality

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        stats = self.cache.get_stats()

        # Add memory manager stats if available
        if self.memory_manager:
            stats["memory_pressure"] = self.memory_manager.get_stats_summary()

        return stats

    async def clear_cache(self, space_ids: Optional[List[int]] = None):
        """Clear cache for specific spaces or all"""
        if space_ids:
            # Clear specific spaces
            for space_id in space_ids:
                for quality in CaptureQuality:
                    key = self.cache._get_cache_key(space_id, quality)
                    with self.cache.lock:
                        if key in self.cache.cache:
                            self.cache._remove_entry(key)
        else:
            # Clear all
            self.cache.clear()

    async def prefetch_spaces(
        self, space_ids: List[int], quality: CaptureQuality = CaptureQuality.OPTIMIZED
    ):
        """Prefetch screenshots for specific spaces"""
        request = SpaceCaptureRequest(
            space_ids=space_ids,
            quality=quality,
            use_cache=True,
            priority=3,  # Lower priority for prefetch
            reason="prefetch",
            require_permission=False,  # No permission for background prefetch
        )

        # Run in background
        asyncio.create_task(self.capture_all_spaces(request))

    async def monitor_space_changes(self, callback: Callable, interval: int = 5):
        """Monitor spaces for changes and trigger captures"""
        last_space_count = len(await self.enumerate_spaces())
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()

        while time.monotonic() - session_start < max_runtime:
            try:
                await asyncio.sleep(interval)

                # Check for space changes
                current_spaces = await self.enumerate_spaces()

                if len(current_spaces) != last_space_count:
                    # Spaces changed
                    logger.info(f"Space count changed: {last_space_count} -> {len(current_spaces)}")
                    await callback(current_spaces)
                    last_space_count = len(current_spaces)

            except Exception as e:
                logger.error(f"Error monitoring spaces: {e}")
        else:
            logger.info("Space monitoring session timeout, stopping")

    async def start_monitoring_session(self) -> bool:
        """Start monitoring session with purple indicator"""
        if self.monitoring_active:
            logger.info("Monitoring session already active")
            return True

        if self.direct_capture:
            logger.info("Starting monitoring session with purple indicator...")

            # Set up vision status callback
            if get_vision_status_manager:
                status_manager = get_vision_status_manager()

                async def vision_status_callback(connected: bool):
                    await status_manager.update_vision_status(connected)

                self.direct_capture.set_vision_status_callback(vision_status_callback)

            success = await self.direct_capture.start_capture()
            if success:
                self.monitoring_active = True
                logger.info("✅ Monitoring session started - purple indicator active")
                return True
            else:
                logger.error("Failed to start monitoring session")
                return False
        else:
            logger.warning(
                "Direct Swift capture not available - monitoring without purple indicator"
            )
            self.monitoring_active = True
            return True

    def stop_monitoring_session(self):
        """Stop monitoring session and remove purple indicator"""
        if not self.monitoring_active:
            logger.info("No active monitoring session to stop")
            return

        if self.direct_capture:
            logger.info("Stopping monitoring session...")
            self.direct_capture.stop_capture()
            logger.info("✅ Monitoring session stopped - purple indicator removed")

        self.monitoring_active = False
