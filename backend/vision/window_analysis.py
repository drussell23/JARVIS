#!/usr/bin/env python3
"""
Enhanced Window Analysis Module for JARVIS Vision System
Memory-optimized window analysis with no hardcoded values
Optimized for 16GB RAM macOS systems
"""

import asyncio
import logging
import os
import json
import gc
import time
import psutil
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import re
from collections import defaultdict, deque
import weakref

logger = logging.getLogger(__name__)

class ApplicationCategory(Enum):
    """Categories of applications"""
    BROWSER = "browser"
    COMMUNICATION = "communication"
    DEVELOPMENT = "development"
    PRODUCTIVITY = "productivity"
    MEDIA = "media"
    SYSTEM = "system"
    UTILITY = "utility"
    UNKNOWN = "unknown"

class WindowState(Enum):
    """States a window can be in"""
    ACTIVE = "active"
    IDLE = "idle"
    WAITING = "waiting"
    ERROR = "error"
    LOADING = "loading"

@dataclass
class WindowContent:
    """Analyzed content of a window with memory tracking"""
    window_id: int
    app_name: str
    category: ApplicationCategory
    state: WindowState
    title_elements: List[str]
    action_items: List[Dict[str, Any]]
    notifications: List[Dict[str, Any]]
    key_information: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    memory_size_bytes: int = 0  # Track memory usage
    
    def __post_init__(self):
        """Calculate memory size after initialization"""
        self.memory_size_bytes = self._calculate_memory_size()
    
    def _calculate_memory_size(self) -> int:
        """Estimate memory usage of this window content"""
        # Simple estimation based on string and list sizes
        size = 0
        size += len(self.app_name.encode())
        size += sum(len(t.encode()) for t in self.title_elements)
        size += len(json.dumps(self.action_items).encode())
        size += len(json.dumps(self.notifications).encode())
        size += len(json.dumps(self.key_information).encode())
        return size
    
    @property
    def has_urgent_items(self) -> bool:
        """Check if window has urgent items requiring attention"""
        return any(n.get('urgent', False) for n in self.notifications)
        
    @property
    def action_count(self) -> int:
        """Number of actionable items in window"""
        return len(self.action_items)

@dataclass
class WorkspaceLayout:
    """Analyzed workspace layout"""
    primary_app: Optional[str] = None
    layout_type: str = "single"  # single, split, grid
    window_arrangement: Dict[str, List[Any]] = field(default_factory=dict)
    screen_utilization: float = 0.0
    overlap_detected: bool = False

class MemoryAwareWindowAnalyzer:
    """Memory-optimized window analyzer with full configurability"""
    
    def __init__(self):
        # Load configuration from environment
        self.config = self._load_config()
        
        # Lazy imports to save memory
        self.window_detector = None
        self.ocr_processor = None
        self.screen_capture = None
        
        # Memory management
        self.window_cache = deque(maxlen=self.config['max_cached_windows'])
        self.cache_timestamps = {}
        self.total_memory_used = 0
        
        # Load app categories from config or defaults
        self.app_categories = self._load_app_categories()
        
        # Load state patterns from config or defaults
        self.state_patterns = self._load_state_patterns()
        
        # Load notification patterns from config or defaults
        self.notification_patterns = self._load_notification_patterns()
        
        # Memory tracking
        self.memory_stats = {
            'current_usage_mb': 0,
            'peak_usage_mb': 0,
            'windows_analyzed': 0,
            'cache_hits': 0,
            'cache_evictions': 0
        }
        
        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        logger.info(f"Memory-Aware Window Analyzer initialized with config: {self.config}")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from environment variables"""
        return {
            # Memory limits
            'max_memory_mb': int(os.getenv('WINDOW_ANALYZER_MAX_MEMORY_MB', '100')),  # 100MB limit
            'max_cached_windows': int(os.getenv('WINDOW_MAX_CACHED', '50')),
            'cache_ttl_seconds': int(os.getenv('WINDOW_CACHE_TTL', '300')),  # 5 minutes
            
            # Analysis settings
            'max_windows_per_analysis': int(os.getenv('WINDOW_MAX_PER_ANALYSIS', '20')),
            'ocr_timeout_seconds': float(os.getenv('WINDOW_OCR_TIMEOUT', '5.0')),
            'skip_minimized': os.getenv('WINDOW_SKIP_MINIMIZED', 'true').lower() == 'true',
            
            # Screen resolution (configurable for different displays)
            'screen_width': int(os.getenv('WINDOW_SCREEN_WIDTH', '1920')),
            'screen_height': int(os.getenv('WINDOW_SCREEN_HEIGHT', '1080')),
            
            # Thresholds
            'overlap_threshold': float(os.getenv('WINDOW_OVERLAP_THRESHOLD', '0.2')),  # 20%
            'quadrant_threshold_x': int(os.getenv('WINDOW_QUADRANT_X', '960')),
            'quadrant_threshold_y': int(os.getenv('WINDOW_QUADRANT_Y', '540')),
            
            # Memory pressure thresholds
            'low_memory_mb': int(os.getenv('WINDOW_LOW_MEMORY_MB', '2000')),
            'critical_memory_mb': int(os.getenv('WINDOW_CRITICAL_MEMORY_MB', '1000')),
            
            # Cleanup intervals
            'cleanup_interval_seconds': int(os.getenv('WINDOW_CLEANUP_INTERVAL', '60'))
        }
    
    def _load_app_categories(self) -> Dict[ApplicationCategory, List[str]]:
        """Load app categories from environment or use defaults"""
        categories_json = os.getenv('WINDOW_APP_CATEGORIES')
        
        if categories_json:
            try:
                loaded = json.loads(categories_json)
                return {
                    ApplicationCategory[k.upper()]: v 
                    for k, v in loaded.items()
                }
            except Exception as e:
                logger.warning(f"Failed to load app categories from env: {e}")
        
        # Default categories
        return {
            ApplicationCategory.BROWSER: [
                'chrome', 'safari', 'firefox', 'edge', 'opera', 'brave'
            ],
            ApplicationCategory.COMMUNICATION: [
                'slack', 'teams', 'zoom', 'discord', 'messages', 'mail', 
                'outlook', 'skype', 'telegram', 'whatsapp'
            ],
            ApplicationCategory.DEVELOPMENT: [
                'code', 'vscode', 'cursor', 'sublime', 'atom', 'intellij', 
                'xcode', 'terminal', 'iterm', 'docker', 'postman'
            ],
            ApplicationCategory.PRODUCTIVITY: [
                'notion', 'obsidian', 'word', 'excel', 'powerpoint', 
                'pages', 'numbers', 'keynote', 'google docs'
            ],
            ApplicationCategory.MEDIA: [
                'spotify', 'music', 'vlc', 'quicktime', 'photos', 
                'preview', 'photoshop', 'figma'
            ],
            ApplicationCategory.SYSTEM: [
                'finder', 'activity monitor', 'system preferences', 
                'settings', 'installer'
            ]
        }
    
    def _load_state_patterns(self) -> Dict[WindowState, List[str]]:
        """Load state patterns from environment or use defaults"""
        patterns_json = os.getenv('WINDOW_STATE_PATTERNS')
        
        if patterns_json:
            try:
                loaded = json.loads(patterns_json)
                return {
                    WindowState[k.upper()]: v 
                    for k, v in loaded.items()
                }
            except Exception as e:
                logger.warning(f"Failed to load state patterns from env: {e}")
        
        # Default patterns
        return {
            WindowState.LOADING: [
                'loading', 'please wait', 'processing', 'connecting'
            ],
            WindowState.ERROR: [
                'error', 'failed', 'cannot', 'unable', 'exception'
            ],
            WindowState.WAITING: [
                'waiting', 'pending', 'paused', 'stopped'
            ]
        }
    
    def _load_notification_patterns(self) -> Dict[str, str]:
        """Load notification patterns from environment or use defaults"""
        patterns_json = os.getenv('WINDOW_NOTIFICATION_PATTERNS')
        
        if patterns_json:
            try:
                return json.loads(patterns_json)
            except Exception as e:
                logger.warning(f"Failed to load notification patterns from env: {e}")
        
        # Default patterns
        return {
            'message': r'\((\d+)\)|(\d+)\s+(new|unread)\s+(message|notification)',
            'update': r'update\s+available|new\s+version|upgrade',
            'alert': r'alert|warning|attention|important',
            'reminder': r'reminder|due|deadline|scheduled'
        }
    
    def _ensure_imports(self):
        """Lazy load heavy imports only when needed"""
        if self.window_detector is None:
            try:
                from .window_detector import WindowDetector, WindowInfo
                self.window_detector = WindowDetector()
            except ImportError:
                logger.warning("WindowDetector not available")
        
        if self.ocr_processor is None:
            try:
                from .ocr_processor import OCRProcessor, OCRResult
                self.ocr_processor = OCRProcessor()
            except ImportError:
                logger.warning("OCRProcessor not available")
        
        if self.screen_capture is None:
            try:
                # Try to use the main vision analyzer for captures
                from .claude_vision_analyzer_main import ClaudeVisionAnalyzer
                # We'll use this for screen capture functionality
                self.vision_analyzer = ClaudeVisionAnalyzer(
                    api_key=os.getenv('ANTHROPIC_API_KEY', 'dummy')
                )
            except ImportError:
                logger.warning("Screen capture not available")
    
    async def _cleanup_loop(self):
        """Periodic cleanup of old cached data"""
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
            logger.info("Window analysis cleanup loop timeout, stopping")
    
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
            self.memory_stats['cache_evictions'] += 1
    
    def _check_memory_available(self) -> bool:
        """Check if we have enough memory to continue"""
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        process_mb = psutil.Process().memory_info().rss / 1024 / 1024
        
        # Check system memory
        if available_mb < self.config['critical_memory_mb']:
            logger.warning(f"Critical system memory: {available_mb}MB")
            return False
        
        # Check process memory
        if process_mb > self.config['max_memory_mb']:
            logger.warning(f"Process memory {process_mb}MB exceeds limit")
            return False
        
        return True
    
    def categorize_application(self, app_name: str) -> ApplicationCategory:
        """Categorize an application based on its name"""
        app_lower = app_name.lower()
        
        for category, apps in self.app_categories.items():
            if any(app in app_lower for app in apps):
                return category
                
        return ApplicationCategory.UNKNOWN
        
    def detect_window_state(self, window_title: str, ocr_text: str = "") -> WindowState:
        """Detect the current state of a window"""
        combined_text = f"{window_title} {ocr_text}".lower()
        
        for state, patterns in self.state_patterns.items():
            if any(pattern in combined_text for pattern in patterns):
                return state
                
        return WindowState.ACTIVE
        
    async def analyze_window(self, window: Any, 
                           capture: Optional[Any] = None) -> WindowContent:
        """Analyze a single window's content with memory management"""
        # Check memory before analysis
        if not self._check_memory_available():
            logger.warning("Skipping window analysis due to memory pressure")
            return self._create_minimal_content(window)
        
        # Check cache first
        cache_key = f"{window.window_id}_{window.window_title}"
        if cache_key in self.cache_timestamps:
            if time.time() - self.cache_timestamps[cache_key] < self.config['cache_ttl_seconds']:
                self.memory_stats['cache_hits'] += 1
                # Find in cache
                for content in self.window_cache:
                    if content.window_id == window.window_id:
                        return content
        
        # Initialize content
        content = WindowContent(
            window_id=window.window_id,
            app_name=window.app_name,
            category=self.categorize_application(window.app_name),
            state=WindowState.ACTIVE,
            title_elements=[],
            action_items=[],
            notifications=[],
            key_information={}
        )
        
        # Perform OCR if we have OCR processor and capture
        if self.ocr_processor and capture:
            try:
                # Timeout for OCR to prevent hanging
                ocr_result = await asyncio.wait_for(
                    self.ocr_processor.process_image(capture.image),
                    timeout=self.config['ocr_timeout_seconds']
                )
                
                # Extract structured data
                structured = self.ocr_processor.extract_structured_data(ocr_result)
                
                # Update content based on OCR
                content.title_elements = structured.get('titles', [])[:10]  # Limit elements
                
                # Detect state from OCR text
                content.state = self.detect_window_state(
                    window.window_title, 
                    ocr_result.full_text[:1000]  # Limit text length
                )
                
                # Extract limited action items
                for button in structured.get('buttons', [])[:20]:  # Limit buttons
                    content.action_items.append({
                        'type': 'button',
                        'text': button['text'][:100],  # Limit text length
                        'location': button.get('location'),
                        'clickable': True
                    })
                    
                # Detect notifications
                content.notifications = self._detect_notifications(
                    window.window_title,
                    ocr_result.full_text[:1000]  # Limit text
                )
                
                # Extract key information based on app category
                content.key_information = self._extract_key_info(
                    content.category,
                    structured,
                    ocr_result
                )
            except asyncio.TimeoutError:
                logger.warning(f"OCR timeout for window {window.app_name}")
            except Exception as e:
                logger.error(f"Error in OCR analysis: {e}")
        else:
            # Basic analysis from window title only
            content.state = self.detect_window_state(window.window_title)
            content.notifications = self._detect_notifications(window.window_title)
        
        # Add to cache with memory management
        self._add_to_cache(content, cache_key)
        
        # Update stats
        self.memory_stats['windows_analyzed'] += 1
        self._update_memory_stats()
        
        return content
    
    def _create_minimal_content(self, window: Any) -> WindowContent:
        """Create minimal window content when memory is low"""
        return WindowContent(
            window_id=window.window_id,
            app_name=window.app_name,
            category=self.categorize_application(window.app_name),
            state=WindowState.UNKNOWN,
            title_elements=[],
            action_items=[],
            notifications=[],
            key_information={'low_memory': True}
        )
    
    def _add_to_cache(self, content: WindowContent, cache_key: str):
        """Add content to cache with memory management"""
        # Check if adding would exceed memory limit
        if self.total_memory_used + content.memory_size_bytes > self.config['max_memory_mb'] * 1024 * 1024:
            # Remove oldest entries
            while self.window_cache and self.total_memory_used + content.memory_size_bytes > self.config['max_memory_mb'] * 1024 * 1024:
                removed = self.window_cache.popleft()
                self.total_memory_used -= removed.memory_size_bytes
                self.memory_stats['cache_evictions'] += 1
        
        # Add to cache
        self.window_cache.append(content)
        self.cache_timestamps[cache_key] = time.time()
        self.total_memory_used += content.memory_size_bytes
    
    def _update_memory_stats(self):
        """Update memory statistics"""
        process_mb = psutil.Process().memory_info().rss / 1024 / 1024
        self.memory_stats['current_usage_mb'] = process_mb
        self.memory_stats['peak_usage_mb'] = max(
            self.memory_stats['peak_usage_mb'],
            process_mb
        )
        
    def _detect_notifications(self, window_title: str, 
                            ocr_text: str = "") -> List[Dict[str, Any]]:
        """Detect notifications in window with limits"""
        notifications = []
        combined_text = f"{window_title} {ocr_text}"[:2000]  # Limit text length
        
        for notif_type, pattern in self.notification_patterns.items():
            matches = list(re.finditer(pattern, combined_text, re.IGNORECASE))[:5]  # Limit matches
            for match in matches:
                notification = {
                    'type': notif_type,
                    'match': match.group()[:100],  # Limit match length
                    'urgent': notif_type in ['alert', 'reminder']
                }
                
                # Extract count if available
                if match.groups():
                    try:
                        count = int(match.group(1) or match.group(2))
                        notification['count'] = count
                    except Exception:
                        pass
                        
                notifications.append(notification)
                
        return notifications[:10]  # Limit total notifications
        
    def _extract_key_info(self, category: ApplicationCategory, 
                         structured: Dict[str, Any],
                         ocr_result: Any) -> Dict[str, Any]:
        """Extract key information based on app category with limits"""
        key_info = {}
        
        if category == ApplicationCategory.BROWSER:
            # Extract limited URLs and page title
            key_info['urls'] = structured.get('urls', [])[:5]
            key_info['page_title'] = structured['titles'][0][:100] if structured.get('titles') else None
            
        elif category == ApplicationCategory.COMMUNICATION:
            # Extract limited info
            key_info['has_messages'] = bool(structured.get('numbers', []))
            key_info['email_addresses'] = structured.get('emails', [])[:3]
            
        elif category == ApplicationCategory.DEVELOPMENT:
            # Extract limited file paths
            key_info['file_paths'] = []
            for text in ocr_result.full_text.split('\n')[:20]:  # Limit lines
                if '/' in text or '\\' in text:
                    key_info['file_paths'].append(text.strip()[:200])
                    if len(key_info['file_paths']) >= 5:
                        break
                    
            # Look for error patterns
            error_patterns = ['error:', 'exception:', 'failed:']
            text_lower = ocr_result.full_text[:1000].lower()
            key_info['has_errors'] = any(pattern in text_lower for pattern in error_patterns)
                    
        return key_info
        
    async def analyze_workspace(self, windows: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Analyze the entire workspace with memory limits"""
        self._ensure_imports()
        
        if not windows and self.window_detector:
            windows = self.window_detector.get_all_windows()
            
        if not windows:
            return {'error': 'No windows available'}
        
        # Limit number of windows to analyze
        if len(windows) > self.config['max_windows_per_analysis']:
            # Prioritize visible and focused windows
            windows = sorted(windows, key=lambda w: (not w.is_visible, not w.is_focused))
            windows = windows[:self.config['max_windows_per_analysis']]
            
        # Analyze layout
        layout = self._analyze_layout(windows)
        
        # Analyze each visible window
        window_analyses = []
        for window in windows:
            if window.is_visible or not self.config['skip_minimized']:
                analysis = await self.analyze_window(window)
                window_analyses.append(analysis)
                
        # Summarize workspace
        summary = self._summarize_workspace(window_analyses, layout)
        
        return {
            'timestamp': datetime.now().isoformat(),
            'layout': layout,
            'windows': window_analyses,
            'summary': summary,
            'memory_stats': self.memory_stats
        }
        
    def _analyze_layout(self, windows: List[Any]) -> WorkspaceLayout:
        """Analyze the layout of windows"""
        layout = WorkspaceLayout()
        
        if not windows:
            return layout
            
        # Find primary (focused) app
        focused = next((w for w in windows if w.is_focused), None)
        if focused:
            layout.primary_app = focused.app_name
            
        # Group windows by screen position using configurable thresholds
        quadrants = defaultdict(list)
        for window in windows:
            if window.is_visible:
                # Determine quadrant using config
                quad_x = 'left' if window.x < self.config['quadrant_threshold_x'] else 'right'
                quad_y = 'top' if window.y < self.config['quadrant_threshold_y'] else 'bottom'
                quadrant = f"{quad_y}_{quad_x}"
                quadrants[quadrant].append(window)
                
        # Determine layout type
        if len(quadrants) >= 3:
            layout.layout_type = 'grid'
        elif len(quadrants) == 2:
            layout.layout_type = 'split'
        else:
            layout.layout_type = 'single'
            
        # Check for overlapping windows
        layout.overlap_detected = self._check_overlap(windows)
        
        # Calculate screen utilization
        if windows:
            total_area = sum(w.width * w.height for w in windows if w.is_visible)
            screen_area = self.config['screen_width'] * self.config['screen_height']
            layout.screen_utilization = min(total_area / screen_area, 1.0)
            
        return layout
        
    def _check_overlap(self, windows: List[Any]) -> bool:
        """Check if windows overlap significantly"""
        visible_windows = [w for w in windows if w.is_visible]
        
        for i, w1 in enumerate(visible_windows):
            for w2 in visible_windows[i+1:]:
                # Check if windows overlap
                if (w1.x < w2.x + w2.width and
                    w1.x + w1.width > w2.x and
                    w1.y < w2.y + w2.height and
                    w1.y + w1.height > w2.y):
                    
                    # Calculate overlap area
                    overlap_x = min(w1.x + w1.width, w2.x + w2.width) - max(w1.x, w2.x)
                    overlap_y = min(w1.y + w1.height, w2.y + w2.height) - max(w1.y, w2.y)
                    overlap_area = overlap_x * overlap_y
                    
                    # Check if overlap is significant (using config threshold)
                    smaller_area = min(w1.width * w1.height, w2.width * w2.height)
                    if overlap_area > smaller_area * self.config['overlap_threshold']:
                        return True
                        
        return False
        
    def _summarize_workspace(self, analyses: List[WindowContent], 
                           layout: WorkspaceLayout) -> Dict[str, Any]:
        """Create a summary of the workspace state"""
        summary = {
            'total_windows': len(analyses),
            'layout_type': layout.layout_type,
            'primary_app': layout.primary_app,
            'categories': defaultdict(int),
            'states': defaultdict(int),
            'urgent_count': 0,
            'total_actions': 0,
            'total_notifications': 0,
            'memory_pressure': self._get_memory_pressure()
        }
        
        for analysis in analyses:
            summary['categories'][analysis.category.value] += 1
            summary['states'][analysis.state.value] += 1
            summary['total_actions'] += analysis.action_count
            summary['total_notifications'] += len(analysis.notifications)
            if analysis.has_urgent_items:
                summary['urgent_count'] += 1
                
        return dict(summary)
    
    def _get_memory_pressure(self) -> str:
        """Get current memory pressure level"""
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        
        if available_mb < self.config['critical_memory_mb']:
            return 'critical'
        elif available_mb < self.config['low_memory_mb']:
            return 'low'
        else:
            return 'normal'
        
    def get_actionable_windows(self, analyses: List[WindowContent]) -> List[WindowContent]:
        """Get windows that require user action"""
        actionable = []
        
        for analysis in analyses:
            # Check if window needs attention
            if (analysis.has_urgent_items or 
                analysis.action_count > 0 or
                analysis.state in [WindowState.ERROR, WindowState.WAITING]):
                actionable.append(analysis)
                
        # Sort by priority
        actionable.sort(key=lambda x: (
            not x.has_urgent_items,  # Urgent first
            x.state != WindowState.ERROR,  # Errors second
            -x.action_count  # More actions = higher priority
        ))
        
        return actionable
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """Get current memory statistics"""
        return {
            **self.memory_stats,
            'cache_size': len(self.window_cache),
            'total_memory_used_mb': self.total_memory_used / 1024 / 1024,
            'available_system_mb': psutil.virtual_memory().available / 1024 / 1024
        }
    
    async def cleanup(self):
        """Cleanup resources"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        self.window_cache.clear()
        self.cache_timestamps.clear()
        gc.collect()

# Backward compatibility
WindowAnalyzer = MemoryAwareWindowAnalyzer

async def test_window_analyzer():
    """Test window analysis functionality"""
    print("🪟 Testing Memory-Aware Window Analyzer")
    print("=" * 50)
    
    analyzer = MemoryAwareWindowAnalyzer()
    
    print(f"\n📊 Configuration:")
    print(f"   Max Memory: {analyzer.config['max_memory_mb']}MB")
    print(f"   Max Cached Windows: {analyzer.config['max_cached_windows']}")
    print(f"   Cache TTL: {analyzer.config['cache_ttl_seconds']}s")
    
    # Test categorization
    print("\n🔍 Testing app categorization:")
    test_apps = ['Visual Studio Code', 'Safari', 'Slack', 'Spotify']
    for app in test_apps:
        category = analyzer.categorize_application(app)
        print(f"   {app} -> {category.value}")
    
    # Test state detection
    print("\n🔍 Testing state detection:")
    test_titles = ['Loading...', 'Error: File not found', 'Ready']
    for title in test_titles:
        state = analyzer.detect_window_state(title)
        print(f"   '{title}' -> {state.value}")
    
    # Show memory stats
    print("\n💾 Memory Statistics:")
    stats = analyzer.get_memory_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    
    # Cleanup
    await analyzer.cleanup()
    
    print("\n✅ Window analyzer test complete!")

if __name__ == "__main__":
    asyncio.run(test_window_analyzer())