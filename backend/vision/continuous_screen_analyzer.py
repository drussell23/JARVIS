#!/usr/bin/env python3
"""
Enhanced Continuous Screen Analyzer for JARVIS
Memory-optimized real-time screen monitoring with Claude Vision integration
Optimized for 16GB RAM macOS systems
"""

import asyncio
import logging
import time
import os
import gc
import psutil
from typing import Dict, Any, Optional, Callable, List, Deque
from datetime import datetime, timedelta
from collections import deque
import weakref
import numpy as np
from PIL import Image
import io
import base64

logger = logging.getLogger(__name__)

class MemoryAwareScreenAnalyzer:
    """
    Memory-optimized continuous screen monitoring system
    Fully configurable with no hardcoded values
    """
    
    def __init__(self, vision_handler, update_interval: Optional[float] = None):
        """
        Initialize memory-aware continuous screen analyzer
        
        Args:
            vision_handler: The vision action handler for Claude Vision
            update_interval: How often to capture screen (in seconds)
        """
        self.vision_handler = vision_handler
        
        # Load all configuration from environment variables
        self.config = {
            'update_interval': float(os.getenv('VISION_MONITOR_INTERVAL', str(update_interval or 3.0))),
            'max_captures_in_memory': int(os.getenv('VISION_MAX_CAPTURES', '10')),
            'capture_retention_seconds': int(os.getenv('VISION_CAPTURE_RETENTION', '300')),  # 5 minutes
            'cache_duration_seconds': float(os.getenv('VISION_CACHE_DURATION', '5.0')),
            'memory_limit_mb': int(os.getenv('VISION_MEMORY_LIMIT_MB', '200')),  # 200MB for this component
            'memory_check_interval': float(os.getenv('VISION_MEMORY_CHECK_INTERVAL', '10.0')),
            'low_memory_threshold_mb': int(os.getenv('VISION_LOW_MEMORY_MB', '2000')),  # 2GB free RAM
            'critical_memory_threshold_mb': int(os.getenv('VISION_CRITICAL_MEMORY_MB', '1000')),  # 1GB free RAM
            'dynamic_interval_enabled': os.getenv('VISION_DYNAMIC_INTERVAL', 'true').lower() == 'true',
            'min_interval_seconds': float(os.getenv('VISION_MIN_INTERVAL', '1.0')),
            'max_interval_seconds': float(os.getenv('VISION_MAX_INTERVAL', '10.0')),
        }
        
        self.is_monitoring = False
        self._monitoring_task = None
        self._memory_monitor_task = None
        self._cleanup_task = None
        
        # Use circular buffer for screen captures (memory efficient)
        self.capture_history: Deque[Dict[str, Any]] = deque(maxlen=self.config['max_captures_in_memory'])
        
        # Current screen state with weak references
        self.current_screen_state = {
            'last_capture': None,
            'last_analysis': None,
            'current_app': None,
            'visible_elements': [],
            'context': {},
            'timestamp': None
        }
        
        # Callbacks with weak references to prevent memory leaks
        self.event_callbacks = {
            'app_changed': weakref.WeakSet(),
            'content_changed': weakref.WeakSet(),
            'weather_visible': weakref.WeakSet(),
            'error_detected': weakref.WeakSet(),
            'user_needs_help': weakref.WeakSet(),
            'memory_warning': weakref.WeakSet()
        }
        
        # Performance optimization with size limits
        self._analysis_cache = {}  # Will be cleaned periodically
        self._cache_sizes = {}  # Track size of cached items
        
        # Memory tracking
        self.memory_stats = {
            'current_usage_mb': 0,
            'peak_usage_mb': 0,
            'captures_dropped': 0,
            'memory_warnings': 0
        }
        
        # Dynamic interval adjustment
        self.current_interval = self.config['update_interval']
        self.last_memory_check = time.time()
        
        logger.info(f"Memory-Aware Screen Analyzer initialized with config: {self.config}")
    
    async def start_monitoring(self):
        """Start continuous screen monitoring with memory management"""
        if self.is_monitoring:
            logger.warning("Screen monitoring is already active")
            return
        
        self.is_monitoring = True
        
        # Start monitoring tasks
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        self._memory_monitor_task = asyncio.create_task(self._memory_monitor_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        logger.info("Started memory-aware continuous screen monitoring")
    
    async def stop_monitoring(self):
        """Stop continuous screen monitoring and cleanup"""
        if not self.is_monitoring:
            return
        
        self.is_monitoring = False
        
        # Cancel all tasks
        tasks = [self._monitoring_task, self._memory_monitor_task, self._cleanup_task]
        for task in tasks:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Clear memory
        self._clear_caches()
        gc.collect()
        
        logger.info("Stopped continuous screen monitoring and cleaned up memory")
    
    async def _monitoring_loop(self):
        """Main monitoring loop with dynamic interval adjustment"""
        while self.is_monitoring:
            try:
                # Check memory before capture
                if not self._check_memory_available():
                    logger.warning("Skipping capture due to low memory")
                    await asyncio.sleep(self.current_interval * 2)  # Wait longer
                    continue
                
                # Capture and analyze screen
                await self._capture_and_analyze()
                
                # Adjust interval based on memory if enabled
                if self.config['dynamic_interval_enabled']:
                    self._adjust_interval_based_on_memory()
                
                # Wait for next update
                await asyncio.sleep(self.current_interval)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(self.current_interval)
    
    async def _memory_monitor_loop(self):
        """Monitor memory usage and trigger warnings"""
        while self.is_monitoring:
            try:
                self._update_memory_stats()
                
                # Check if we're exceeding limits
                available_mb = self._get_available_memory_mb()
                
                if available_mb < self.config['critical_memory_threshold_mb']:
                    # Critical memory - emergency cleanup
                    logger.warning(f"Critical memory: {available_mb}MB available")
                    await self._emergency_cleanup()
                    await self._trigger_event('memory_warning', {
                        'level': 'critical',
                        'available_mb': available_mb
                    })
                elif available_mb < self.config['low_memory_threshold_mb']:
                    # Low memory - normal cleanup
                    logger.info(f"Low memory: {available_mb}MB available")
                    self._clear_old_captures()
                    await self._trigger_event('memory_warning', {
                        'level': 'low',
                        'available_mb': available_mb
                    })
                
                await asyncio.sleep(self.config['memory_check_interval'])
                
            except Exception as e:
                logger.error(f"Error in memory monitor: {e}")
                await asyncio.sleep(self.config['memory_check_interval'])
    
    async def _cleanup_loop(self):
        """Periodic cleanup of old data"""
        while self.is_monitoring:
            try:
                # Clean old captures
                self._clear_old_captures()
                
                # Clean cache
                self._clean_cache()
                
                # Force garbage collection
                gc.collect()
                
                # Wait before next cleanup
                await asyncio.sleep(60.0)  # Cleanup every minute
                
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
                await asyncio.sleep(60.0)
    
    def _check_memory_available(self) -> bool:
        """Check if enough memory is available for capture"""
        available_mb = self._get_available_memory_mb()
        process_mb = self._get_process_memory_mb()
        
        # Check system memory
        if available_mb < self.config['critical_memory_threshold_mb']:
            return False
        
        # Check process memory limit
        if process_mb > self.config['memory_limit_mb']:
            logger.warning(f"Process memory {process_mb}MB exceeds limit {self.config['memory_limit_mb']}MB")
            return False
        
        return True
    
    def _get_available_memory_mb(self) -> float:
        """Get available system memory in MB"""
        return psutil.virtual_memory().available / 1024 / 1024
    
    def _get_process_memory_mb(self) -> float:
        """Get current process memory usage in MB"""
        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024
    
    def _update_memory_stats(self):
        """Update memory statistics"""
        current_mb = self._get_process_memory_mb()
        self.memory_stats['current_usage_mb'] = current_mb
        self.memory_stats['peak_usage_mb'] = max(self.memory_stats['peak_usage_mb'], current_mb)
    
    def _adjust_interval_based_on_memory(self):
        """Dynamically adjust capture interval based on memory pressure"""
        available_mb = self._get_available_memory_mb()
        
        if available_mb < self.config['critical_memory_threshold_mb']:
            # Critical - use maximum interval
            self.current_interval = self.config['max_interval_seconds']
        elif available_mb < self.config['low_memory_threshold_mb']:
            # Low memory - increase interval
            ratio = available_mb / self.config['low_memory_threshold_mb']
            range_size = self.config['max_interval_seconds'] - self.config['min_interval_seconds']
            self.current_interval = self.config['max_interval_seconds'] - (ratio * range_size)
        else:
            # Normal memory - use configured interval
            self.current_interval = self.config['update_interval']
        
        # Clamp to configured range
        self.current_interval = max(
            self.config['min_interval_seconds'],
            min(self.current_interval, self.config['max_interval_seconds'])
        )
    
    async def _capture_and_analyze(self):
        """Capture screen and analyze with memory management"""
        try:
            # Capture current screen
            capture_result = await self.vision_handler.capture_screen()
            if capture_result is None:
                return
            
            current_time = time.time()
            
            # Convert to consistent format
            if hasattr(capture_result, 'success'):
                # It's a result object
                if not capture_result.success:
                    return
                screenshot = capture_result
            else:
                # It's a raw image
                screenshot = capture_result
            
            # Store capture with size tracking
            capture_data = {
                'timestamp': current_time,
                'result': screenshot,
                'size_bytes': self._estimate_capture_size(screenshot)
            }
            
            # Add to history (circular buffer handles removal)
            self.capture_history.append(capture_data)
            
            # Quick analysis to detect changes
            quick_analysis = await self._quick_screen_analysis()
            
            # Determine if we need full analysis
            needs_full_analysis = self._needs_full_analysis(quick_analysis)
            
            if needs_full_analysis:
                # Perform full Claude Vision analysis
                analysis = await self._full_screen_analysis()
                
                # Update screen state
                self._update_screen_state(analysis)
                
                # Trigger relevant callbacks
                await self._process_screen_events(analysis)
            
        except Exception as e:
            logger.error(f"Error capturing/analyzing screen: {e}")
    
    def _estimate_capture_size(self, capture_data: Any) -> int:
        """Estimate memory size of capture"""
        # Try to get actual size if it's an image
        try:
            if hasattr(capture_data, 'size'):
                # PIL Image
                width, height = capture_data.size
                return width * height * 3  # RGB channels
            elif hasattr(capture_data, 'shape'):
                # Numpy array
                return capture_data.nbytes
            else:
                # Basic estimation
                return 1024 * 1024  # Assume 1MB per capture
        except Exception:
            return 1024 * 1024  # Default 1MB
    
    async def _quick_screen_analysis(self) -> Dict[str, Any]:
        """Detect major screen changes using local APIs (zero API cost).

        v237.3: Uses macOS NSWorkspace/osascript for focused app detection
        instead of Claude API call. This runs every capture cycle (~3s) so
        it MUST be local — describe_screen() is reserved for full analysis
        only (gated by _needs_full_analysis()).
        """
        app_name = await self._get_focused_app_local()

        return {
            'current_app': app_name,
            'timestamp': time.time()
        }

    async def _get_focused_app_local(self) -> str:
        """Get focused application name using local macOS APIs.

        Zero API cost. Fallback chain:
        1. NSWorkspace (PyObjC) — <1ms, most reliable
        2. osascript via subprocess — ~50ms, reliable fallback
        3. 'Unknown' — safe default (triggers full analysis on first cycle)
        """
        # Method 1: PyObjC NSWorkspace (fastest, <1ms)
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app:
                name = app.localizedName()
                if name:
                    return name
        except ImportError:
            pass
        except Exception:
            pass

        # Method 2: osascript fallback (~50ms)
        try:
            import subprocess
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ['osascript', '-e',
                     'tell application "System Events" to get name of '
                     'first application process whose frontmost is true'],
                    capture_output=True, text=True, timeout=2
                )
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

        return 'Unknown'
    
    async def _full_screen_analysis(self) -> Dict[str, Any]:
        """Perform comprehensive screen analysis with caching"""
        # Check cache first
        cache_key = f"full_analysis_{int(time.time() / self.config['cache_duration_seconds'])}"
        if cache_key in self._analysis_cache:
            return self._analysis_cache[cache_key]
        
        # Comprehensive analysis prompt
        params = {
            'query': '''Analyze the current screen and provide:
1. Currently active application
2. Key UI elements visible
3. Any weather information if Weather app is visible
4. Any error messages or dialogs
5. What the user appears to be doing
6. Any text content that might be relevant

Be concise but thorough.'''
        }
        
        result = await self.vision_handler.describe_screen(params)
        
        analysis = {
            'success': result.success,
            'description': result.description if result.success else '',
            'timestamp': time.time(),
            'raw_data': result.data if hasattr(result, 'data') else {}
        }
        
        # Cache with size tracking
        self._cache_analysis(cache_key, analysis)
        
        return analysis
    
    def _cache_analysis(self, key: str, analysis: Dict[str, Any]):
        """Cache analysis with size tracking"""
        # Estimate size
        size = len(str(analysis).encode())
        
        # Check if adding would exceed memory limit
        total_cache_size = sum(self._cache_sizes.values())
        if total_cache_size + size > self.config['memory_limit_mb'] * 0.1 * 1024 * 1024:  # Use 10% for cache
            # Remove oldest entries
            self._clean_cache(force=True)
        
        self._analysis_cache[key] = analysis
        self._cache_sizes[key] = size
    
    def _clean_cache(self, force: bool = False):
        """Clean old cache entries"""
        current_time = time.time()
        keys_to_remove = []
        
        for key in list(self._analysis_cache.keys()):
            # Extract timestamp from key
            try:
                key_time = int(key.split('_')[-1]) * self.config['cache_duration_seconds']
                if force or (current_time - key_time > self.config['cache_duration_seconds'] * 2):
                    keys_to_remove.append(key)
            except Exception:
                keys_to_remove.append(key)  # Remove malformed keys
        
        for key in keys_to_remove:
            self._analysis_cache.pop(key, None)
            self._cache_sizes.pop(key, None)
    
    def _clear_old_captures(self):
        """Clear captures older than retention period"""
        if not self.capture_history:
            return
        
        current_time = time.time()
        retention_seconds = self.config['capture_retention_seconds']
        
        # Remove old captures
        while self.capture_history:
            oldest = self.capture_history[0]
            if current_time - oldest['timestamp'] > retention_seconds:
                self.capture_history.popleft()
                self.memory_stats['captures_dropped'] += 1
            else:
                break
    
    async def _emergency_cleanup(self):
        """Emergency cleanup when memory is critical"""
        logger.warning("Performing emergency memory cleanup")
        
        # Clear all captures except the latest
        if len(self.capture_history) > 1:
            latest = self.capture_history[-1]
            self.capture_history.clear()
            self.capture_history.append(latest)
        
        # Clear all caches
        self._clear_caches()
        
        # Force garbage collection
        gc.collect()
        
        self.memory_stats['memory_warnings'] += 1
    
    def _clear_caches(self):
        """Clear all caches"""
        self._analysis_cache.clear()
        self._cache_sizes.clear()
    
    def _needs_full_analysis(self, quick_analysis: Dict[str, Any]) -> bool:
        """Determine if full analysis is needed"""
        # Always analyze if no previous state
        if not self.current_screen_state['last_analysis']:
            return True
        
        # Check if app changed
        if quick_analysis.get('current_app') != self.current_screen_state.get('current_app'):
            return True
        
        # Check if enough time has passed (configurable)
        last_analysis_time = self.current_screen_state.get('timestamp', 0)
        full_analysis_interval = float(os.getenv('VISION_FULL_ANALYSIS_INTERVAL', '10.0'))
        if time.time() - last_analysis_time > full_analysis_interval:
            return True
        
        return False
    
    def _update_screen_state(self, analysis: Dict[str, Any]):
        """Update internal screen state"""
        self.current_screen_state.update({
            'last_analysis': analysis.get('description', ''),
            'timestamp': analysis.get('timestamp', time.time()),
            'current_app': self._extract_current_app(analysis)
        })
    
    def _extract_current_app(self, analysis: Dict[str, Any]) -> Optional[str]:
        """Extract current application from analysis"""
        description = analysis.get('description', '').lower()
        
        # Load app detection patterns from environment or use defaults
        app_patterns = os.getenv('VISION_APP_PATTERNS')
        if app_patterns:
            try:
                import json
                apps = json.loads(app_patterns)
            except Exception:
                apps = self._get_default_app_patterns()
        else:
            apps = self._get_default_app_patterns()
        
        for app_name, keywords in apps.items():
            if any(keyword in description for keyword in keywords):
                return app_name
        
        return None
    
    def _get_default_app_patterns(self) -> Dict[str, List[str]]:
        """Get default app detection patterns"""
        return {
            'weather': ['weather app', 'weather.app'],
            'safari': ['safari'],
            'chrome': ['chrome', 'google chrome'],
            'vscode': ['vs code', 'visual studio code', 'vscode', 'cursor'],
            'terminal': ['terminal', 'iterm'],
            'finder': ['finder'],
            'mail': ['mail app', 'mail.app'],
            'messages': ['messages', 'imessage']
        }
    
    async def _process_screen_events(self, analysis: Dict[str, Any]):
        """Process screen events and trigger callbacks"""
        description = analysis.get('description', '').lower()
        
        # Check for weather visibility
        if 'weather' in description and any(word in description for word in ['temperature', 'degrees', '°']):
            await self._trigger_event('weather_visible', {
                'analysis': analysis,
                'weather_info': self._extract_weather_info(description)
            })
        
        # Check for errors
        if any(word in description for word in ['error', 'failed', 'exception', 'crash']):
            await self._trigger_event('error_detected', {
                'analysis': analysis,
                'error_context': description
            })
    
    def _extract_weather_info(self, description: str) -> Optional[str]:
        """Extract weather information from screen description"""
        # Use the weather parser if available
        try:
            from utils.weather_response_parser import WeatherResponseParser
            parser = WeatherResponseParser()
            return parser.extract_weather_info(description)
        except ImportError:
            # Fallback to simple extraction
            return description if 'weather' in description.lower() else None
    
    async def _trigger_event(self, event_type: str, data: Dict[str, Any]):
        """Trigger event callbacks using weak references"""
        if event_type in self.event_callbacks:
            # Convert WeakSet to list to iterate
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
        """Register a callback for specific events"""
        if event_type in self.event_callbacks:
            self.event_callbacks[event_type].add(callback)
            logger.info(f"Registered callback for {event_type}")
    
    async def get_current_screen_context(self) -> Dict[str, Any]:
        """Get current screen context for queries"""
        # If we have recent analysis, return it
        if self.current_screen_state['last_analysis']:
            age = time.time() - self.current_screen_state['timestamp']
            if age < self.config['cache_duration_seconds']:
                return self.current_screen_state
        
        # Otherwise, do a fresh analysis
        analysis = await self._full_screen_analysis()
        self._update_screen_state(analysis)
        return self.current_screen_state
    
    async def query_screen_for_weather(self) -> Optional[str]:
        """
        Query screen specifically for weather information
        Memory-efficient approach
        """
        # First check if Weather app is already visible
        context = await self.get_current_screen_context()
        
        if context.get('current_app') == 'weather':
            # Weather app is already open, just read it
            params = {
                'query': 'Read the weather information from the Weather app. What is the temperature, conditions, and forecast?'
            }
        else:
            # Need to open Weather app first
            try:
                from system_control import MacOSController
                controller = MacOSController()
                
                # Open Weather app
                controller.open_application("Weather")
                await asyncio.sleep(2.0)  # Wait for it to open
                
                # Now read the weather
                params = {
                    'query': 'The Weather app should now be open. Read the weather information: temperature, conditions, and forecast for today.'
                }
            except ImportError:
                return None
        
        result = await self.vision_handler.describe_screen(params)
        
        if result.success:
            # Parse the weather info
            weather_info = self._extract_weather_info(result.description)
            return weather_info
        
        return None
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """Get current memory statistics"""
        return {
            **self.memory_stats,
            'captures_in_memory': len(self.capture_history),
            'cache_entries': len(self._analysis_cache),
            'current_interval': self.current_interval,
            'available_system_mb': self._get_available_memory_mb()
        }

# Backward compatibility alias
ContinuousScreenAnalyzer = MemoryAwareScreenAnalyzer