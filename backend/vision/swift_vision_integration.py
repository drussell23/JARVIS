#!/usr/bin/env python3
"""
Enhanced Swift Vision Integration for JARVIS Vision System
Memory-aware high-performance vision processing using Swift/Metal acceleration
Optimized for 16GB RAM macOS systems
"""

import asyncio
import logging
import os
import gc
import psutil
import json
from typing import Optional, List, Dict, Any, Tuple, Deque
import numpy as np
from PIL import Image
import io
import time
from dataclasses import dataclass, field
from collections import deque
import weakref
from datetime import datetime, timedelta

# Try to import Swift performance bridge
try:
    from swift_bridge.performance_bridge import (
        get_vision_processor,
        VisionResult,
        SWIFT_PERFORMANCE_AVAILABLE
    )
except ImportError:
    SWIFT_PERFORMANCE_AVAILABLE = False
    VisionResult = None
    get_vision_processor = lambda: None

logger = logging.getLogger(__name__)

@dataclass
class VisionProcessingResult:
    """Unified vision processing result with memory tracking"""
    faces: List[Dict[str, float]]
    text_regions: List[Dict[str, Any]]
    objects: List[Dict[str, float]]
    processing_time: float
    memory_used_mb: int
    method: str  # "swift" or "python"
    compressed_size: Optional[int] = None
    timestamp: datetime = field(default_factory=datetime.now)
    memory_pressure_level: str = "normal"  # normal, low, critical

class MemoryAwareSwiftVisionIntegration:
    """
    Memory-aware Swift vision processing with Metal acceleration
    Provides significant performance improvement with memory safeguards
    """
    
    def __init__(self):
        # Load configuration from environment
        self.config = self._load_config()
        
        self.swift_processor = None
        self.enabled = False
        self._cleanup_task = None
        self._process_limit_warned = False
        self._critical_memory_warned = False
        
        # Memory management
        self.result_cache: Deque[VisionProcessingResult] = deque(maxlen=self.config['max_cached_results'])
        self.cache_timestamps = {}
        self.total_memory_used = 0
        
        # Circuit breaker for memory protection
        self.circuit_breaker = {
            'failures': 0,
            'last_failure': None,
            'is_open': False,
            'last_check': time.time()
        }
        
        # Processing statistics with memory tracking
        self.processing_stats = {
            "total_processed": 0,
            "swift_processed": 0,
            "python_processed": 0,
            "total_time": 0.0,
            "swift_time": 0.0,
            "python_time": 0.0,
            "memory_fallbacks": 0,
            "circuit_breaker_trips": 0,
            "current_memory_mb": 0,
            "peak_memory_mb": 0
        }
        
        # Try to initialize Swift processor with memory check
        memory_available = self._check_memory_available()
        if memory_available and SWIFT_PERFORMANCE_AVAILABLE:
            try:
                self.swift_processor = get_vision_processor()
                if self.swift_processor:
                    self.enabled = True
                    logger.info(f"✅ Swift vision acceleration enabled (Metal) with config: {self.config}")
                else:
                    logger.warning("Swift vision processor not available")
            except Exception as e:
                logger.error(f"Failed to initialize Swift vision processor: {e}")
        else:
            if not memory_available:
                logger.warning("Insufficient memory for Swift vision - using Python fallback")
            else:
                logger.info("Swift performance bridge not available - using Python fallback")
        
        # Start cleanup task only when an event loop is available.
        # This class can be imported from sync startup contexts.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self._cleanup_task = loop.create_task(
                self._cleanup_loop(),
                name="swift_vision_cleanup",
            )
        else:
            logger.debug("No running loop; Swift vision cleanup loop disabled")
        
        logger.info(f"Memory-Aware Swift Vision Integration initialized with config: {self.config}")
    
    def _calculate_swift_memory_limit(self) -> int:
        """Calculate dynamic memory limit for Swift/Metal processing"""
        try:
            import psutil
            vm = psutil.virtual_memory()
            available_mb = vm.available / (1024 * 1024)
            
            # Use 15% of available RAM for Swift/Metal processing
            dynamic_limit = int(available_mb * 0.15)
            
            # Apply reasonable bounds
            min_limit = 150  # At least 150MB
            max_limit = 1000  # Cap at 1GB for GPU operations
            
            final_limit = max(min_limit, min(dynamic_limit, max_limit))
            logger.info(f"Swift vision dynamic memory: {final_limit}MB (15% of {available_mb:.0f}MB available)")
            return final_limit
        except Exception as e:
            logger.warning(f"Failed to calculate Swift dynamic memory: {e}")
            # Fallback: use conservative 10% of 4GB minimum system
            return 400  # 10% of 4GB minimum expected system
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from environment variables"""
        return {
            # Memory limits - dynamic allocation
            'max_memory_mb': self._calculate_swift_memory_limit(),
            'max_cached_results': int(os.getenv('SWIFT_VISION_MAX_CACHED', '20')),
            'cache_ttl_seconds': int(os.getenv('SWIFT_VISION_CACHE_TTL', '300')),  # 5 minutes
            
            # Processing limits
            'max_image_dimension': int(os.getenv('SWIFT_VISION_MAX_DIMENSION', '4096')),
            'batch_size': int(os.getenv('SWIFT_VISION_BATCH_SIZE', '5')),
            'processing_timeout': float(os.getenv('SWIFT_VISION_TIMEOUT', '10.0')),
            
            # Quality settings
            'jpeg_quality': int(os.getenv('SWIFT_VISION_JPEG_QUALITY', '80')),
            'high_memory_quality': int(os.getenv('SWIFT_VISION_HIGH_QUALITY', '95')),
            'low_memory_quality': int(os.getenv('SWIFT_VISION_LOW_QUALITY', '60')),
            
            # Memory pressure thresholds
            'low_memory_mb': int(os.getenv('SWIFT_VISION_LOW_MEMORY_MB', '3000')),  # 3GB
            'critical_memory_mb': int(os.getenv('SWIFT_VISION_CRITICAL_MEMORY_MB', '1500')),  # 1.5GB
            'metal_memory_limit_mb': int(os.getenv('SWIFT_VISION_METAL_LIMIT_MB', '1000')),  # 1GB for Metal
            
            # Circuit breaker settings
            'circuit_breaker_threshold': int(os.getenv('SWIFT_VISION_CB_THRESHOLD', '3')),
            'circuit_breaker_timeout': int(os.getenv('SWIFT_VISION_CB_TIMEOUT', '60')),  # 60 seconds
            
            # Cleanup intervals
            'cleanup_interval_seconds': int(os.getenv('SWIFT_VISION_CLEANUP_INTERVAL', '120')),
            'memory_check_interval': float(os.getenv('SWIFT_VISION_MEM_CHECK_INTERVAL', '5.0'))
        }
    
    def _check_memory_available(self) -> bool:
        """Check if enough memory is available for Swift/Metal processing.

        v255.1: Respects JARVIS_STARTUP_MEMORY_MODE set by OOM bridge pre-flight.
        Under severe pressure modes (minimal/cloud_only/cloud_first), Swift/Metal
        is skipped immediately — the decision was already made at startup.
        """
        # v255.1: Respect startup memory mode from OOM bridge (set in unified_supervisor.py)
        _mem_mode = os.environ.get("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        if _mem_mode in ("minimal", "cloud_only", "cloud_first"):
            if not self._critical_memory_warned:
                logger.warning(
                    "Swift vision deferred: startup memory_mode=%s (OOM bridge decision)",
                    _mem_mode,
                )
                self._critical_memory_warned = True
            return False

        available_mb = psutil.virtual_memory().available / 1024 / 1024
        process_mb = psutil.Process().memory_info().rss / 1024 / 1024
        
        # Update stats
        self.processing_stats['current_memory_mb'] = process_mb
        self.processing_stats['peak_memory_mb'] = max(
            self.processing_stats['peak_memory_mb'], process_mb
        )
        
        # Check system memory
        if available_mb < self.config['critical_memory_mb']:
            if not self._critical_memory_warned:
                logger.warning(f"Critical system memory: {available_mb}MB")
                self._critical_memory_warned = True
            return False
        self._critical_memory_warned = False
        
        # Check process memory
        if process_mb > self.config['max_memory_mb']:
            if not self._process_limit_warned:
                logger.warning(f"Process memory {process_mb}MB exceeds limit")
                self._process_limit_warned = True
            return False
        self._process_limit_warned = False
        
        return True
    
    def _get_memory_pressure_level(self) -> str:
        """Get current memory pressure level"""
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        
        if available_mb < self.config['critical_memory_mb']:
            return 'critical'
        elif available_mb < self.config['low_memory_mb']:
            return 'low'
        else:
            return 'normal'
    
    def _check_circuit_breaker(self) -> bool:
        """Check if circuit breaker allows processing"""
        current_time = time.time()
        
        # Check if circuit breaker should reset
        if self.circuit_breaker['is_open']:
            if self.circuit_breaker['last_failure'] and \
               current_time - self.circuit_breaker['last_failure'] > self.config['circuit_breaker_timeout']:
                # Reset circuit breaker
                self.circuit_breaker['is_open'] = False
                self.circuit_breaker['failures'] = 0
                logger.info("Circuit breaker reset")
            else:
                return False
        
        return True
    
    def _trip_circuit_breaker(self, error: Exception):
        """Trip the circuit breaker on failure"""
        self.circuit_breaker['failures'] += 1
        self.circuit_breaker['last_failure'] = time.time()
        
        if self.circuit_breaker['failures'] >= self.config['circuit_breaker_threshold']:
            self.circuit_breaker['is_open'] = True
            self.processing_stats['circuit_breaker_trips'] += 1
            logger.warning(f"Circuit breaker tripped after {self.circuit_breaker['failures']} failures")
    
    async def _cleanup_loop(self):
        """Periodic cleanup of old cached results"""
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        while time.monotonic() - session_start < max_runtime:
            try:
                await asyncio.sleep(self.config['cleanup_interval_seconds'])
                self._cleanup_old_cache()
                gc.collect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
        else:
            logger.info("Swift vision cleanup loop timeout, stopping")
    
    def _cleanup_old_cache(self):
        """Remove old entries from cache"""
        current_time = time.time()
        ttl = self.config['cache_ttl_seconds']
        
        # Clean cache timestamps
        expired_keys = [
            k for k, v in self.cache_timestamps.items()
            if current_time - v > ttl
        ]
        
        for key in expired_keys:
            self.cache_timestamps.pop(key, None)
    
    def _resize_image_if_needed(self, image: Image.Image) -> Image.Image:
        """Resize image if it exceeds maximum dimensions"""
        max_dim = self.config['max_image_dimension']
        
        if image.width > max_dim or image.height > max_dim:
            # Calculate new size maintaining aspect ratio
            ratio = min(max_dim / image.width, max_dim / image.height)
            new_width = int(image.width * ratio)
            new_height = int(image.height * ratio)
            
            logger.info(f"Resizing image from {image.width}x{image.height} to {new_width}x{new_height}")
            return image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        return image
    
    async def process_screenshot(self, image: Image.Image) -> VisionProcessingResult:
        """
        Process screenshot with Swift/Metal acceleration if available
        Memory-aware processing with automatic fallback
        
        Args:
            image: PIL Image to process
            
        Returns:
            VisionProcessingResult with detected features
        """
        start_time = time.time()
        memory_pressure = self._get_memory_pressure_level()
        
        # Check memory before processing
        if not self._check_memory_available():
            logger.warning("Insufficient memory for vision processing")
            self.processing_stats['memory_fallbacks'] += 1
            return self._create_minimal_result(memory_pressure)
        
        # Resize image if needed to save memory
        image = self._resize_image_if_needed(image)
        
        # Adjust quality based on memory pressure
        quality = self._get_quality_for_memory_pressure(memory_pressure)
        
        # Convert PIL image to JPEG bytes for Swift processing
        image_bytes = self._image_to_jpeg_bytes(image, quality=quality)
        
        # Check circuit breaker and try Swift processing first
        if self.enabled and self.swift_processor and self._check_circuit_breaker():
            try:
                # Add timeout for processing
                result = await asyncio.wait_for(
                    self.swift_processor.process_image_async(image_bytes),
                    timeout=self.config['processing_timeout']
                )
                processing_time = time.time() - start_time
                
                # Check if Metal memory usage is within limits
                if hasattr(result, 'memory_used') and result.memory_used > self.config['metal_memory_limit_mb']:
                    logger.warning(f"Metal memory usage {result.memory_used}MB exceeds limit")
                    raise MemoryError("Metal memory limit exceeded")
                
                # Update statistics
                self._update_stats("swift", processing_time)
                
                vision_result = VisionProcessingResult(
                    faces=[{"box": f} for f in result.faces][:self.config['batch_size']],  # Limit results
                    text_regions=result.text[:self.config['batch_size']],
                    objects=[{"box": o} for o in result.objects][:self.config['batch_size']],
                    processing_time=processing_time,
                    memory_used_mb=result.memory_used,
                    method="swift",
                    compressed_size=len(image_bytes),
                    memory_pressure_level=memory_pressure
                )
                
                # Cache result
                self._cache_result(vision_result)
                
                return vision_result
                
            except asyncio.TimeoutError:
                logger.error(f"Swift vision processing timeout after {self.config['processing_timeout']}s")
                self._trip_circuit_breaker(asyncio.TimeoutError())
            except MemoryError as e:
                logger.error(f"Swift vision memory error: {e}")
                self._trip_circuit_breaker(e)
                self.processing_stats['memory_fallbacks'] += 1
            except Exception as e:
                logger.error(f"Swift vision processing failed: {e}")
                self._trip_circuit_breaker(e)
                # Fall through to Python implementation
        
        # Fallback to Python implementation with memory check
        if memory_pressure == 'critical':
            logger.warning("Critical memory - returning minimal result")
            return self._create_minimal_result(memory_pressure)
        
        result = await self._process_image_python(image)
        result.processing_time = time.time() - start_time
        result.compressed_size = len(image_bytes)
        result.memory_pressure_level = memory_pressure
        self._update_stats("python", result.processing_time)
        
        # Cache result
        self._cache_result(result)
        
        return result
    
    def _get_quality_for_memory_pressure(self, pressure: str) -> int:
        """Get JPEG quality based on memory pressure"""
        if pressure == 'critical':
            return self.config['low_memory_quality']
        elif pressure == 'low':
            return self.config['jpeg_quality']
        else:
            return self.config['high_memory_quality']
    
    def _create_minimal_result(self, memory_pressure: str) -> VisionProcessingResult:
        """Create minimal result when memory is critical"""
        return VisionProcessingResult(
            faces=[],
            text_regions=[],
            objects=[],
            processing_time=0.0,
            memory_used_mb=0,
            method="minimal",
            memory_pressure_level=memory_pressure
        )
    
    def _cache_result(self, result: VisionProcessingResult):
        """Cache result with memory management"""
        # Estimate size
        result_size = len(str(result.__dict__).encode())
        
        # Check if adding would exceed memory limit
        if self.total_memory_used + result_size > self.config['max_memory_mb'] * 0.1 * 1024 * 1024:
            # Remove oldest entries
            while self.result_cache and self.total_memory_used + result_size > self.config['max_memory_mb'] * 0.1 * 1024 * 1024:
                removed = self.result_cache.popleft()
                self.total_memory_used -= len(str(removed.__dict__).encode())
        
        # Add to cache
        self.result_cache.append(result)
        self.total_memory_used += result_size
        self.cache_timestamps[id(result)] = time.time()
    
    def compress_image(self, image: Image.Image, quality: Optional[int] = None) -> bytes:
        """
        Compress image using Swift/Metal if available
        Memory-aware compression with quality adjustment
        
        Args:
            image: PIL Image to compress
            quality: JPEG quality (0-100), auto-adjusted if None
            
        Returns:
            Compressed image bytes
        """
        # Auto-adjust quality based on memory if not specified
        if quality is None:
            memory_pressure = self._get_memory_pressure_level()
            quality = self._get_quality_for_memory_pressure(memory_pressure)
        
        # Check memory and circuit breaker
        if self.enabled and self.swift_processor and self._check_memory_available() and self._check_circuit_breaker():
            try:
                # Resize if needed
                image = self._resize_image_if_needed(image)
                
                # Convert to JPEG for Swift processing
                image_bytes = self._image_to_jpeg_bytes(image, quality=100)
                
                # Check size before processing
                if len(image_bytes) > self.config['max_memory_mb'] * 0.5 * 1024 * 1024:
                    logger.warning("Image too large for Swift processing")
                    raise MemoryError("Image size exceeds limit")
                
                # Swift processor will recompress with Metal
                # This is still faster than PIL for large images
                return image_bytes
            except Exception as e:
                logger.error(f"Swift compression failed: {e}")
                if isinstance(e, MemoryError):
                    self._trip_circuit_breaker(e)
        
        # Fallback to PIL compression
        image = self._resize_image_if_needed(image)
        return self._image_to_jpeg_bytes(image, quality=quality)
    
    async def extract_text_regions(self, image: Image.Image, max_regions: Optional[int] = None) -> List[Image.Image]:
        """
        Extract regions containing text for focused OCR
        Memory-aware extraction with configurable limits
        
        Args:
            image: PIL Image to analyze
            max_regions: Maximum number of regions to extract (None for config default)
            
        Returns:
            List of cropped images containing text
        """
        # Use config default if not specified
        if max_regions is None:
            max_regions = self.config['batch_size']
        
        # Check memory before processing
        if not self._check_memory_available():
            logger.warning("Insufficient memory for text extraction")
            return []
        
        # Process image to find text regions
        result = await self.process_screenshot(image)
        
        text_regions = []
        for i, region in enumerate(result.text_regions):
            if i >= max_regions:
                break
                
            if 'boundingBox' in region:
                box = region['boundingBox']
                
                # Convert normalized coordinates to pixels
                x = int(box.get('x', 0) * image.width)
                y = int(box.get('y', 0) * image.height)
                w = int(box.get('width', 0) * image.width)
                h = int(box.get('height', 0) * image.height)
                
                # Skip very small or very large regions
                min_size = int(os.getenv('SWIFT_VISION_MIN_REGION_SIZE', '20'))
                max_size = int(os.getenv('SWIFT_VISION_MAX_REGION_SIZE', '2000'))
                
                if w < min_size or h < min_size or w > max_size or h > max_size:
                    continue
                
                # Crop region
                try:
                    cropped = image.crop((x, y, x + w, y + h))
                    text_regions.append(cropped)
                except Exception as e:
                    logger.error(f"Failed to crop region: {e}")
        
        return text_regions
    
    async def _process_image_python(self, image: Image.Image) -> VisionProcessingResult:
        """Python fallback for vision processing"""
        # Simple placeholder implementation
        # In real use, this would use OpenCV or similar
        
        return VisionProcessingResult(
            faces=[],
            text_regions=[],
            objects=[],
            processing_time=0.0,
            memory_used_mb=0,
            method="python"
        )
    
    def _image_to_jpeg_bytes(self, image: Image.Image, quality: Optional[int] = None) -> bytes:
        """Convert PIL Image to JPEG bytes with memory-aware compression"""
        # Use config default if not specified
        if quality is None:
            quality = self.config['jpeg_quality']
        
        buffer = io.BytesIO()
        
        # Convert RGBA to RGB if necessary
        if image.mode == 'RGBA':
            # Use configurable background color
            bg_color = tuple(json.loads(os.getenv('SWIFT_VISION_BG_COLOR', '[255, 255, 255]')))
            rgb_image = Image.new('RGB', image.size, bg_color)
            rgb_image.paste(image, mask=image.split()[3])
            image = rgb_image
        elif image.mode != 'RGB':
            image = image.convert('RGB')
        
        # Progressive encoding for large images
        progressive = image.width * image.height > int(os.getenv('SWIFT_VISION_PROGRESSIVE_THRESHOLD', '1000000'))
        
        image.save(
            buffer, 
            format='JPEG', 
            quality=quality, 
            optimize=True,
            progressive=progressive
        )
        return buffer.getvalue()
    
    def _update_stats(self, method: str, processing_time: float):
        """Update processing statistics"""
        self.processing_stats["total_processed"] += 1
        self.processing_stats["total_time"] += processing_time
        
        if method == "swift":
            self.processing_stats["swift_processed"] += 1
            self.processing_stats["swift_time"] += processing_time
        else:
            self.processing_stats["python_processed"] += 1
            self.processing_stats["python_time"] += processing_time
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """Get current memory statistics"""
        return {
            'current_usage_mb': self.processing_stats['current_memory_mb'],
            'peak_usage_mb': self.processing_stats['peak_memory_mb'],
            'cache_size': len(self.result_cache),
            'total_cache_memory_mb': self.total_memory_used / 1024 / 1024,
            'available_system_mb': psutil.virtual_memory().available / 1024 / 1024,
            'memory_pressure': self._get_memory_pressure_level(),
            'circuit_breaker_open': self.circuit_breaker['is_open'],
            'circuit_breaker_trips': self.processing_stats['circuit_breaker_trips'],
            'memory_fallbacks': self.processing_stats['memory_fallbacks']
        }
    
    def get_performance_stats(self) -> dict:
        """Get performance statistics"""
        stats = self.processing_stats.copy()
        
        # Calculate averages
        if stats["swift_processed"] > 0:
            stats["swift_avg_ms"] = (stats["swift_time"] / stats["swift_processed"]) * 1000
        else:
            stats["swift_avg_ms"] = 0
        
        if stats["python_processed"] > 0:
            stats["python_avg_ms"] = (stats["python_time"] / stats["python_processed"]) * 1000
        else:
            stats["python_avg_ms"] = 0
        
        # Calculate speedup
        if stats["python_avg_ms"] > 0 and stats["swift_avg_ms"] > 0:
            stats["speedup"] = stats["python_avg_ms"] / stats["swift_avg_ms"]
        else:
            stats["speedup"] = 1.0
        
        stats["enabled"] = self.enabled
        
        # Add memory stats
        stats.update(self.get_memory_stats())
        
        return stats
    
    async def cleanup(self):
        """Cleanup resources"""
        if hasattr(self, '_cleanup_task') and self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        
        # Clear caches
        self.result_cache.clear()
        self.cache_timestamps.clear()
        self.total_memory_used = 0
        
        # Disable Swift processor
        if self.swift_processor:
            self.swift_processor = None
            self.enabled = False
        
        gc.collect()

# Backward compatibility
SwiftVisionIntegration = MemoryAwareSwiftVisionIntegration

# Global instance with weak reference
_swift_vision = None
_swift_vision_ref = None

def get_swift_vision_integration() -> MemoryAwareSwiftVisionIntegration:
    """Get singleton Swift vision integration instance with memory management"""
    global _swift_vision, _swift_vision_ref
    
    # Check if instance exists via weak reference
    if _swift_vision_ref is not None:
        instance = _swift_vision_ref()
        if instance is not None:
            return instance
    
    # Create new instance
    _swift_vision = MemoryAwareSwiftVisionIntegration()
    _swift_vision_ref = weakref.ref(_swift_vision)
    
    return _swift_vision

# Convenience functions
async def process_screenshot_swift(image: Image.Image) -> VisionProcessingResult:
    """Process screenshot using Swift/Metal acceleration if available"""
    integration = get_swift_vision_integration()
    return await integration.process_screenshot(image)

def compress_image_swift(image: Image.Image, quality: Optional[int] = None) -> bytes:
    """Compress image using Swift if available with memory awareness"""
    integration = get_swift_vision_integration()
    return integration.compress_image(image, quality)

async def extract_text_regions_swift(image: Image.Image, max_regions: Optional[int] = None) -> List[Image.Image]:
    """Extract text regions using Swift if available with memory limits"""
    integration = get_swift_vision_integration()
    return await integration.extract_text_regions(image, max_regions)

def get_vision_performance_stats() -> dict:
    """Get vision processing performance statistics"""
    integration = get_swift_vision_integration()
    return integration.get_performance_stats()

async def test_swift_vision_integration():
    """Test memory-aware Swift vision integration"""
    print("👁️ Testing Memory-Aware Swift Vision Integration")
    print("=" * 50)
    
    integration = get_swift_vision_integration()
    print(f"\n📊 Configuration:")
    print(f"   Max Memory: {integration.config['max_memory_mb']}MB")
    print(f"   Metal Limit: {integration.config['metal_memory_limit_mb']}MB")
    print(f"   Swift enabled: {integration.enabled}")
    
    # Create test images of different sizes
    test_sizes = [(800, 600), (1920, 1080), (4096, 2160)]
    
    for width, height in test_sizes:
        print(f"\n🖼️ Testing {width}x{height} image:")
        test_image = Image.new('RGB', (width, height), color='white')
        
        # Test processing
        result = await integration.process_screenshot(test_image)
        print(f"   Method: {result.method}")
        print(f"   Processing time: {result.processing_time:.2f}s")
        print(f"   Memory pressure: {result.memory_pressure_level}")
        print(f"   Compressed size: {result.compressed_size} bytes")
        
        # Test compression with different quality levels
        for quality in [60, 80, 95]:
            compressed = integration.compress_image(test_image, quality=quality)
            print(f"   Quality {quality}: {len(compressed)} bytes")
    
    # Test text region extraction
    print("\n📝 Testing text region extraction:")
    regions = await integration.extract_text_regions(test_image, max_regions=5)
    print(f"   Extracted {len(regions)} text regions")
    
    # Show stats
    print("\n📈 Performance Statistics:")
    stats = integration.get_performance_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    
    # Cleanup
    await integration.cleanup()
    print("\n✅ Test complete!")

if __name__ == "__main__":
    asyncio.run(test_swift_vision_integration())
