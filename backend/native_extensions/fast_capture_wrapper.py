"""
Python wrapper for the Fast Capture C++ extension
Provides a clean, Pythonic interface to the high-performance capture engine
"""

import os
import sys
import time
import asyncio
from typing import List, Dict, Optional, Callable, Any, Union, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image
import io

# Import the C++ extension
try:
    from . import fast_capture
except ImportError:
    # Try direct import if running as script
    import fast_capture


@dataclass
class CaptureConfig:
    """Python-friendly capture configuration"""
    capture_cursor: bool = False
    capture_shadow: bool = False
    capture_only_visible: bool = True
    output_format: str = "auto"  # "auto", "jpeg", "png", "raw"
    jpeg_quality: int = 85
    use_gpu_acceleration: bool = True
    parallel_capture: bool = True
    max_threads: int = 0  # 0 = auto
    max_width: int = 0  # 0 = no limit
    max_height: int = 0  # 0 = no limit
    maintain_aspect_ratio: bool = True
    include_apps: List[str] = field(default_factory=list)
    exclude_apps: List[str] = field(default_factory=list)
    capture_metadata: bool = True
    custom_filter: Optional[Callable] = None
    
    def to_cpp_config(self) -> fast_capture.CaptureConfig:
        """Convert to C++ config object"""
        cpp_config = fast_capture.CaptureConfig()
        cpp_config.capture_cursor = self.capture_cursor
        cpp_config.capture_shadow = self.capture_shadow
        cpp_config.capture_only_visible = self.capture_only_visible
        cpp_config.output_format = self.output_format
        cpp_config.jpeg_quality = self.jpeg_quality
        cpp_config.use_gpu_acceleration = self.use_gpu_acceleration
        cpp_config.parallel_capture = self.parallel_capture
        cpp_config.max_threads = self.max_threads
        cpp_config.max_width = self.max_width
        cpp_config.max_height = self.max_height
        cpp_config.maintain_aspect_ratio = self.maintain_aspect_ratio
        cpp_config.include_apps = self.include_apps
        cpp_config.exclude_apps = self.exclude_apps
        cpp_config.capture_metadata = self.capture_metadata
        
        if self.custom_filter:
            cpp_config.set_custom_filter(self.custom_filter)
            
        return cpp_config


class FastCaptureEngine:
    """
    Python wrapper for the C++ Fast Capture Engine
    Provides high-performance screen capture with a Pythonic interface
    """
    
    def __init__(self, default_config: Optional[CaptureConfig] = None):
        """Initialize the capture engine"""
        self._engine = fast_capture.FastCaptureEngine()
        self._executor = ThreadPoolExecutor(max_workers=4)
        
        if default_config:
            self.set_default_config(default_config)
            
        # Callbacks
        self._capture_callback = None
        self._error_callback = None
        
    def __del__(self):
        """Cleanup"""
        self._executor.shutdown(wait=False)
    
    # ===== Single Window Capture =====
    
    def capture_window(self, window_id: int, 
                      config: Optional[CaptureConfig] = None) -> Dict[str, Any]:
        """Capture a single window by ID"""
        cpp_config = (config or CaptureConfig()).to_cpp_config()
        result = self._engine.capture_window(window_id, cpp_config)
        return self._process_result(result)
    
    def capture_window_by_name(self, app_name: str, 
                              window_title: str = "",
                              config: Optional[CaptureConfig] = None) -> Dict[str, Any]:
        """Capture a window by app name and optional title"""
        cpp_config = (config or CaptureConfig()).to_cpp_config()
        result = self._engine.capture_window_by_name(app_name, window_title, cpp_config)
        return self._process_result(result)
    
    def capture_frontmost_window(self, config: Optional[CaptureConfig] = None) -> Dict[str, Any]:
        """Capture the frontmost window"""
        cpp_config = (config or CaptureConfig()).to_cpp_config()
        result = self._engine.capture_frontmost_window(cpp_config)
        return self._process_result(result)
    
    # ===== Multi-Window Capture =====
    
    def capture_all_windows(self, config: Optional[CaptureConfig] = None) -> List[Dict[str, Any]]:
        """Capture all windows"""
        cpp_config = (config or CaptureConfig()).to_cpp_config()
        results = self._engine.capture_all_windows(cpp_config)
        return [self._process_result(r) for r in results]
    
    def capture_visible_windows(self, config: Optional[CaptureConfig] = None) -> List[Dict[str, Any]]:
        """Capture only visible windows"""
        cpp_config = (config or CaptureConfig()).to_cpp_config()
        results = self._engine.capture_visible_windows(cpp_config)
        return [self._process_result(r) for r in results]
    
    def capture_windows_by_app(self, app_name: str,
                              config: Optional[CaptureConfig] = None) -> List[Dict[str, Any]]:
        """Capture all windows from a specific app"""
        cpp_config = (config or CaptureConfig()).to_cpp_config()
        results = self._engine.capture_windows_by_app(app_name, cpp_config)
        return [self._process_result(r) for r in results]
    
    # ===== Async Capture Methods =====
    
    async def capture_window_async(self, window_id: int,
                                  config: Optional[CaptureConfig] = None) -> Dict[str, Any]:
        """Async version of capture_window"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self.capture_window, window_id, config
        )
    
    async def capture_all_windows_async(self, 
                                       config: Optional[CaptureConfig] = None) -> List[Dict[str, Any]]:
        """Async version of capture_all_windows"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self.capture_all_windows, config
        )
    
    # ===== Window Discovery =====
    
    def get_all_windows(self) -> List[Dict[str, Any]]:
        """Get information about all windows"""
        windows = self._engine.get_all_windows()
        return [self._window_info_to_dict(w) for w in windows]
    
    def get_visible_windows(self) -> List[Dict[str, Any]]:
        """Get information about visible windows only"""
        windows = self._engine.get_visible_windows()
        return [self._window_info_to_dict(w) for w in windows]
    
    def get_windows_by_app(self, app_name: str) -> List[Dict[str, Any]]:
        """Get windows for a specific application"""
        windows = self._engine.get_windows_by_app(app_name)
        return [self._window_info_to_dict(w) for w in windows]
    
    def find_window(self, app_name: str, window_title: str = "") -> Optional[Dict[str, Any]]:
        """Find a specific window"""
        window = self._engine.find_window(app_name, window_title)
        if window is not None:
            return self._window_info_to_dict(window)
        return None
    
    def get_frontmost_window(self) -> Optional[Dict[str, Any]]:
        """Get the frontmost window"""
        window = self._engine.get_frontmost_window()
        if window is not None:
            return self._window_info_to_dict(window)
        return None
    
    # ===== Performance Metrics =====
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get performance metrics"""
        metrics = self._engine.get_metrics()
        return {
            'avg_capture_time_ms': metrics.avg_capture_time_ms,
            'min_capture_time_ms': metrics.min_capture_time_ms,
            'max_capture_time_ms': metrics.max_capture_time_ms,
            'p95_capture_time_ms': metrics.p95_capture_time_ms,
            'p99_capture_time_ms': metrics.p99_capture_time_ms,
            'total_captures': metrics.total_captures,
            'successful_captures': metrics.successful_captures,
            'failed_captures': metrics.failed_captures,
            'bytes_processed': metrics.bytes_processed,
            'peak_memory_usage': metrics.peak_memory_usage,
            'captures_per_app': dict(metrics.captures_per_app),
            'avg_time_per_app': dict(metrics.avg_time_per_app)
        }
    
    def reset_metrics(self):
        """Reset performance metrics"""
        self._engine.reset_metrics()
    
    def enable_metrics(self, enable: bool):
        """Enable or disable metrics collection"""
        self._engine.enable_metrics(enable)
    
    # ===== Configuration =====
    
    def set_default_config(self, config: CaptureConfig):
        """Set default capture configuration"""
        self._engine.set_default_config(config.to_cpp_config())
    
    def get_default_config(self) -> CaptureConfig:
        """Get default capture configuration"""
        cpp_config = self._engine.get_default_config()
        config = CaptureConfig()
        config.capture_cursor = cpp_config.capture_cursor
        config.capture_shadow = cpp_config.capture_shadow
        config.output_format = cpp_config.output_format
        config.jpeg_quality = cpp_config.jpeg_quality
        # ... copy other fields
        return config
    
    # ===== Callbacks =====
    
    def set_capture_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback for capture events"""
        self._capture_callback = callback
        self._engine.set_capture_callback(lambda result: callback(self._process_result(result)))
    
    def set_error_callback(self, callback: Callable[[str], None]):
        """Set callback for error events"""
        self._error_callback = callback
        self._engine.set_error_callback(callback)
    
    # ===== Utility Methods =====
    
    def capture_to_pil(self, window_id: int, 
                      config: Optional[CaptureConfig] = None) -> Optional[Image.Image]:
        """Capture window and return as PIL Image"""
        result = self.capture_window(window_id, config)
        if result['success']:
            if 'image' in result:  # Raw numpy array
                return Image.fromarray(result['image'])
            elif 'image_data' in result:  # Compressed data
                return Image.open(io.BytesIO(result['image_data']))
        return None
    
    def capture_to_numpy(self, window_id: int,
                        config: Optional[CaptureConfig] = None) -> Optional[np.ndarray]:
        """Capture window and return as numpy array"""
        # Force raw format for numpy
        if config is None:
            config = CaptureConfig()
        config.output_format = "raw"
        
        result = self.capture_window(window_id, config)
        if result['success'] and 'image' in result:
            return result['image']
        return None
    
    def benchmark(self, window_id: int, iterations: int = 100) -> Dict[str, float]:
        """Benchmark capture performance"""
        times = []
        config = CaptureConfig(output_format="jpeg", jpeg_quality=85)
        
        # Warmup
        for _ in range(5):
            self.capture_window(window_id, config)
        
        # Benchmark
        for _ in range(iterations):
            start = time.perf_counter()
            result = self.capture_window(window_id, config)
            if result['success']:
                times.append(time.perf_counter() - start)
        
        if times:
            return {
                'avg_ms': np.mean(times) * 1000,
                'min_ms': np.min(times) * 1000,
                'max_ms': np.max(times) * 1000,
                'std_ms': np.std(times) * 1000,
                'p95_ms': np.percentile(times, 95) * 1000,
                'p99_ms': np.percentile(times, 99) * 1000,
                'fps': 1.0 / np.mean(times) if np.mean(times) > 0 else 0
            }
        return {}
    
    # ===== Private Methods =====
    
    def _process_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Process capture result from C++"""
        # Convert image data if needed
        if 'image' in result and isinstance(result['image'], np.ndarray):
            # Already a numpy array from C++
            pass
        elif 'image_data' in result:
            # Convert bytes to more useful format if needed
            result['image_data'] = bytes(result['image_data'])
        
        # Add convenience fields
        if result['success']:
            result['capture_fps'] = 1000.0 / result['capture_time_ms'] if result['capture_time_ms'] > 0 else 0
        
        return result
    
    def _window_info_to_dict(self, window: fast_capture.WindowInfo) -> Dict[str, Any]:
        """Convert WindowInfo to dictionary"""
        return {
            'window_id': window.window_id,
            'app_name': window.app_name,
            'window_title': window.window_title,
            'bundle_identifier': window.bundle_identifier,
            'x': window.x,
            'y': window.y,
            'width': window.width,
            'height': window.height,
            'is_visible': window.is_visible,
            'is_minimized': window.is_minimized,
            'is_fullscreen': window.is_fullscreen,
            'layer': window.layer,
            'alpha': window.alpha,
            'metadata': dict(window.metadata) if window.metadata else {}
        }


# ===== Convenience Functions =====

def create_size_filter(min_width: int, min_height: int) -> Callable:
    """Create a filter for minimum window size"""
    def filter_func(window_info):
        return window_info.width >= min_width and window_info.height >= min_height
    return filter_func


def create_app_filter(apps: List[str]) -> Callable:
    """Create a filter for specific applications"""
    def filter_func(window_info):
        for app in apps:
            if app in window_info.app_name or app in window_info.bundle_identifier:
                return True
        return False
    return filter_func


# ===== Example Usage =====

if __name__ == "__main__":
    # Example usage
    engine = FastCaptureEngine()
    
    # List all windows
    print("All windows:")
    for window in engine.get_all_windows():
        print(f"  {window['app_name']} - {window['window_title']}")
    
    # Capture frontmost window
    result = engine.capture_frontmost_window()
    if result['success']:
        print(f"\nCaptured frontmost window in {result['capture_time_ms']:.2f}ms")
        print(f"  Size: {result['width']}x{result['height']}")
        print(f"  Format: {result['format']}")
        print(f"  FPS potential: {result['capture_fps']:.1f}")
    
    # Benchmark
    if engine.get_visible_windows():
        window_id = engine.get_visible_windows()[0]['window_id']
        print(f"\nBenchmarking window {window_id}...")
        bench = engine.benchmark(window_id, iterations=50)
        print(f"  Average: {bench['avg_ms']:.2f}ms ({bench['fps']:.1f} FPS)")
        print(f"  Min/Max: {bench['min_ms']:.2f}ms / {bench['max_ms']:.2f}ms")
        print(f"  P95/P99: {bench['p95_ms']:.2f}ms / {bench['p99_ms']:.2f}ms")