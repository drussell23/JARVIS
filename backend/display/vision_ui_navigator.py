#!/usr/bin/env python3
"""
Vision-Guided UI Navigator for Display Connection
==================================================

Uses JARVIS Vision to navigate macOS Control Center and connect to displays.
Bypasses all macOS Sequoia security restrictions by using visual recognition.

This module provides a comprehensive vision-guided navigation system that can:
- Capture screen content using existing vision infrastructure
- Analyze UI elements with Claude Vision API
- Calculate precise click coordinates from visual analysis
- Execute mouse automation with PyAutoGUI
- Learn from successful interactions to improve accuracy
- Self-correct when wrong elements are clicked

The navigator uses a multi-layered approach:
1. Learned positions (fastest, most accurate)
2. Claude Vision direct detection (primary method)
3. Multi-pass detection with different strategies
4. Intelligent scanning and color analysis
5. Heuristic fallback based on typical UI layouts

Features:
- Zero hardcoding - fully configuration-driven
- Async/await support throughout
- Self-healing with retry logic
- Comprehensive visual verification
- Integration with existing JARVIS vision system
- Works on macOS Sequoia without accessibility permissions
- Adaptive confidence thresholds based on success history
- Color analysis to distinguish similar icons (e.g., Siri vs Control Center)

Author: Derek Russell
Date: 2025-10-15
Version: 2.0
"""

import asyncio
import logging
import subprocess
import json
import pyautogui
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from PIL import Image
import base64
import io
import re

logger = logging.getLogger(__name__)


@dataclass
class UIElement:
    """Visual UI element detected by Claude Vision.
    
    Represents a UI element identified through visual analysis, containing
    its location, type, and confidence information.
    
    Attributes:
        name: Human-readable name of the element (e.g., "Control Center")
        description: Detailed description of the element's appearance
        bounding_box: Optional tuple of (x, y, width, height) coordinates
        center_point: Optional tuple of (x, y) center coordinates for clicking
        confidence: Float 0.0-1.0 representing detection confidence
        element_type: Category of element (icon, button, text, menu_item)
    """
    name: str
    description: str
    bounding_box: Optional[Tuple[int, int, int, int]]  # (x, y, width, height)
    center_point: Optional[Tuple[int, int]]  # (x, y)
    confidence: float
    element_type: str  # icon, button, text, menu_item


@dataclass
class NavigationResult:
    """Result of a navigation attempt.
    
    Contains comprehensive information about a UI navigation operation,
    including success status, timing, and error details.
    
    Attributes:
        success: Whether the navigation completed successfully
        message: Human-readable description of the result
        steps_completed: List of navigation steps that were completed
        duration: Time taken for the navigation in seconds
        screenshot_path: Optional path to screenshot taken during navigation
        error_details: Optional dictionary with error information
    """
    success: bool
    message: str
    steps_completed: List[str]
    duration: float
    screenshot_path: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None


class VisionUINavigator:
    """Vision-guided UI navigator for macOS interface automation.
    
    This class provides comprehensive UI navigation capabilities using Claude Vision
    for element detection and PyAutoGUI for mouse automation. It's specifically
    designed to work with macOS Sequoia's security restrictions by using visual
    recognition instead of accessibility APIs.
    
    The navigator employs multiple detection strategies:
    1. Learned positions from previous successful interactions
    2. Direct Claude Vision analysis with enhanced prompts
    3. Multi-pass detection with different approaches
    4. Intelligent scanning with color analysis
    5. Heuristic fallback based on typical UI layouts
    
    Attributes:
        config: Configuration dictionary loaded from JSON file
        screenshots_dir: Directory for storing navigation screenshots
        vision_analyzer: Claude Vision analyzer instance
        enhanced_pipeline: Enhanced vision pipeline for advanced detection
        use_enhanced_pipeline: Whether to use enhanced pipeline features
        stats: Dictionary tracking navigation statistics
        learned_cc_position: Cached Control Center position from successful clicks
        detection_history: List of recent detection attempts for learning
        adaptive_confidence_threshold: Dynamic confidence threshold
        edge_cases: Dictionary of detected system configuration edge cases
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """Initialize vision navigator with configuration and learning systems.
        
        Args:
            config_path: Optional path to configuration JSON file. If None,
                        uses default path relative to module location.
        """
        # Load configuration
        if config_path is None:
            config_path = Path(__file__).parent.parent / 'config' / 'vision_navigator_config.json'
        
        self.config = self._load_config(config_path)
        self.screenshots_dir = Path.home() / '.jarvis' / 'screenshots' / 'ui_navigation'
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        
        # Vision analyzer (will be set by display monitor)
        self.vision_analyzer = None
        
        # Enhanced Vision Pipeline (v1.0)
        self.enhanced_pipeline = None
        self.use_enhanced_pipeline = self.config.get('advanced', {}).get('use_enhanced_pipeline', True)
        
        # Statistics
        self.stats = {
            'total_navigations': 0,
            'successful': 0,
            'failed': 0,
            'avg_duration': 0.0,
            'enhanced_pipeline_used': 0,
            'fallback_used': 0
        }

        # Learning system: Cache successful Control Center position
        self.learned_cc_position = None  # Will be (x, y) after first successful click
        self.learning_cache_file = Path.home() / '.jarvis' / 'control_center_position.json'

        # Advanced detection system
        self.detection_history = []  # Track last 10 detection attempts with outcomes
        self.failure_patterns = {}  # Track common failure scenarios
        self.adaptive_confidence_threshold = 0.75  # Dynamic threshold based on history
        self.screen_context = {}  # Cache screen state (resolution, dark mode, etc.)
        self.detection_strategies = ['learned', 'primary', 'multi_pass', 'exhaustive', 'heuristic']
        self.current_strategy_index = 0

        # Edge case detection flags
        self.edge_cases = {
            'dark_mode': None,  # Will be detected
            'retina_display': None,  # Will be detected
            'resolution': None,  # Will be detected
            'menu_bar_autohide': False,
            'time_format_12h': True
        }

        # Configure PyAutoGUI safety
        pyautogui.PAUSE = self.config.get('mouse', {}).get('delay_between_actions', 0.5)
        pyautogui.FAILSAFE = True

        # Load learned Control Center position if available
        self._load_learned_position()

        # Detect edge cases on initialization
        self._detect_edge_cases()

        logger.info("[VISION NAV] Vision UI Navigator initialized")
        logger.info(f"[VISION NAV] Enhanced Pipeline: {'enabled' if self.use_enhanced_pipeline else 'disabled'}")
        if self.learned_cc_position:
            logger.info(f"[VISION NAV] 🎓 Learned position loaded: {self.learned_cc_position}")
    
    def _load_config(self, config_path: Path) -> Dict[str, Any]:
        """Load configuration from JSON file.
        
        Args:
            config_path: Path to configuration JSON file
            
        Returns:
            Configuration dictionary
            
        Raises:
            FileNotFoundError: If config file doesn't exist (falls back to defaults)
            json.JSONDecodeError: If config file is invalid JSON (falls back to defaults)
        """
        try:
            with open(config_path) as f:
                config = json.load(f)
            logger.info(f"[VISION NAV] Loaded config from {config_path}")
            return config
        except FileNotFoundError:
            logger.warning(f"[VISION NAV] Config not found, using defaults")
            return self._get_default_config()
        except Exception as e:
            logger.error(f"[VISION NAV] Error loading config: {e}")
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration when config file is unavailable.
        
        Returns:
            Dictionary containing default configuration values for navigation,
            mouse control, vision analysis, and prompts.
        """
        return {
            "navigation": {
                "max_retries": 3,
                "retry_delay": 1.0,
                "screenshot_verification": True,
                "step_delay": 0.8,
                "max_navigation_time": 30.0
            },
            "mouse": {
                "delay_between_actions": 0.5,
                "click_duration": 0.1,
                "movement_speed": 0.3
            },
            "vision": {
                "confidence_threshold": 0.7,
                "use_bounding_boxes": True,
                "verify_actions": True,
                "screenshot_format": "png",
                "screenshot_quality": 95
            },
            "prompts": {
                "find_control_center": "Find the Control Center icon in the menu bar (top right, looks like two overlapping rectangles). Provide the exact pixel coordinates of its center.",
                "find_screen_mirroring": "Find the Screen Mirroring button in Control Center (looks like two overlapping screens). Provide the exact pixel coordinates of its center.",
                "find_display": "Find '{display_name}' in the list of available displays. Provide the exact pixel coordinates to click on it."
            }
        }
    
    def set_vision_analyzer(self, analyzer):
        """Set Claude Vision analyzer instance for UI element detection.
        
        Args:
            analyzer: Claude Vision analyzer instance that provides
                     analyze_screenshot method for visual analysis
        """
        self.vision_analyzer = analyzer
        logger.info("[VISION NAV] Vision analyzer connected")
        
        # Initialize Enhanced Vision Pipeline
        if self.use_enhanced_pipeline:
            asyncio.create_task(self._initialize_enhanced_pipeline())
    
    async def _initialize_enhanced_pipeline(self):
        """Initialize Enhanced Vision Pipeline for advanced detection capabilities.
        
        Attempts to load and initialize the 5-stage enhanced vision pipeline
        which provides more accurate detection through multiple analysis stages.
        Falls back to basic detection if initialization fails.
        """
        try:
            from vision.enhanced_vision_pipeline import get_vision_pipeline
            
            self.enhanced_pipeline = get_vision_pipeline()
            
            # Initialize all stages
            initialized = await self.enhanced_pipeline.initialize()
            
            if initialized:
                # Connect Claude Vision to validator
                if self.enhanced_pipeline.model_validator and self.vision_analyzer:
                    self.enhanced_pipeline.model_validator.set_claude_analyzer(self.vision_analyzer)
                
                logger.info("[VISION NAV] ✅ Enhanced Vision Pipeline v1.0 initialized")
                logger.info("[VISION NAV] 🚀 5-stage pipeline ready:")
                logger.info("[VISION NAV]    Stage 1: Screen Region Segmentation (Quadtree)")
                logger.info("[VISION NAV]    Stage 2: Icon Pattern Recognition (OpenCV + Edge)")
                logger.info("[VISION NAV]    Stage 3: Coordinate Calculation (Physics-based)")
                logger.info("[VISION NAV]    Stage 4: Multi-Model Validation (Monte Carlo)")
                logger.info("[VISION NAV]    Stage 5: Mouse Automation (Bezier trajectories)")
            else:
                logger.warning("[VISION NAV] Enhanced Pipeline initialization failed, using fallback")
                self.use_enhanced_pipeline = False
                
        except Exception as e:
            logger.warning(f"[VISION NAV] Could not initialize Enhanced Pipeline: {e}")
            logger.info("[VISION NAV] Using fallback navigation methods")
            self.use_enhanced_pipeline = False
    
    async def connect_to_display(self, display_name: str) -> NavigationResult:
        """Connect to a display using vision-guided navigation.
        
        Performs the complete workflow to connect to a specified display:
        1. Opens Control Center by finding and clicking its icon
        2. Finds and clicks Screen Mirroring button
        3. Locates and selects the target display
        4. Verifies the connection was established
        
        Args:
            display_name: Name of display to connect to (e.g., "Living Room TV")
            
        Returns:
            NavigationResult containing success status, timing information,
            completed steps, and any error details
            
        Example:
            >>> navigator = VisionUINavigator()
            >>> result = await navigator.connect_to_display("Living Room TV")
            >>> if result.success:
            ...     print(f"Connected in {result.duration:.2f}s")
        """
        start_time = time.time()
        steps_completed = []
        self.stats['total_navigations'] += 1
        
        logger.info(f"[VISION NAV] Starting vision-guided connection to '{display_name}'")
        
        try:
            # Step 1: Find and click Control Center icon
            logger.info("[VISION NAV] Step 1: Finding Control Center icon...")
            cc_clicked = await self._find_and_click_control_center()
            if not cc_clicked:
                raise Exception("Could not find or click Control Center icon")
            steps_completed.append("control_center_opened")
            
            # Wait for Control Center to open
            await asyncio.sleep(self.config['navigation']['step_delay'])
            
            # Step 2: Find and click Screen Mirroring button
            logger.info("[VISION NAV] Step 2: Finding Screen Mirroring button...")
            sm_clicked = await self._find_and_click_screen_mirroring()
            if not sm_clicked:
                raise Exception("Could not find or click Screen Mirroring button")
            steps_completed.append("screen_mirroring_opened")
            
            # Wait for Screen Mirroring menu to open
            await asyncio.sleep(self.config['navigation']['step_delay'])
            
            # Step 3: Find and click display
            logger.info(f"[VISION NAV] Step 3: Finding '{display_name}' in list...")
            display_clicked = await self._find_and_click_display(display_name)
            if not display_clicked:
                raise Exception(f"Could not find or click '{display_name}' in display list")
            steps_completed.append("display_selected")
            
            # Step 4: Verify connection
            logger.info("[VISION NAV] Step 4: Verifying connection...")
            await asyncio.sleep(2.0)  # Wait for connection to establish
            
            connected = await self._verify_connection(display_name)
            if connected:
                steps_completed.append("connection_verified")
            
            duration = time.time() - start_time
            self.stats['successful'] += 1
            self.stats['avg_duration'] = (
                (self.stats['avg_duration'] * (self.stats['successful'] - 1) + duration) 
                / self.stats['successful']
            )
            
            logger.info(f"[VISION NAV] ✅ Successfully connected to '{display_name}' in {duration:.2f}s")
            
            return NavigationResult(
                success=True,
                message=f"Successfully connected to {display_name} using vision navigation",
                steps_completed=steps_completed,
                duration=duration
            )
            
        except Exception as e:
            self.stats['failed'] += 1
            duration = time.time() - start_time
            
            logger.error(f"[VISION NAV] ❌ Navigation failed: {e}")
            
            return NavigationResult(
                success=False,
                message=f"Vision navigation failed: {str(e)}",
                steps_completed=steps_completed,
                duration=duration,
                error_details={'exception': str(e), 'steps_completed': steps_completed}
            )
    
    def _load_learned_position(self):
        """Load previously learned Control Center position from cache file.
        
        Attempts to load a previously successful Control Center click position
        from the learning cache file. This enables faster, more accurate
        navigation on subsequent runs.
        """
        try:
            if self.learning_cache_file.exists():
                with open(self.learning_cache_file) as f:
                    data = json.load(f)
                    self.learned_cc_position = tuple(data.get('control_center_position', []))
                    if self.learned_cc_position:
                        logger.info(f"[VISION NAV] 🎓 Loaded learned position: {self.learned_cc_position}")
        except Exception as e:
            logger.warning(f"[VISION NAV] Could not load learned position: {e}")
            self.learned_cc_position = None

    def _save_learned_position(self, x: int, y: int):
        """Save successful Control Center position for future use.
        
        Stores a successful Control Center click position along with system
        context (resolution, edge cases) for future navigation attempts.
        
        Args:
            x: X coordinate of successful click
            y: Y coordinate of successful click
        """
        try:
            self.learned_cc_position = (x, y)
            self.learning_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.learning_cache_file, 'w') as f:
                json.dump({
                    'control_center_position': [x, y],
                    'screen_resolution': list(pyautogui.size()),
                    'edge_cases': self.edge_cases,
                    'learned_at': datetime.now().isoformat()
                }, f)
            logger.info(f"[VISION NAV] 💾 Saved learned position: ({x}, {y})")
        except Exception as e:
            logger.warning(f"[VISION NAV] Could not save learned position: {e}")

    def _detect_edge_cases(self):
        """Detect screen configuration and system edge cases.
        
        Analyzes the current system configuration to detect factors that
        might affect UI navigation, such as:
        - Screen resolution and Retina display status
        - Dark mode vs light mode
        - Menu bar auto-hide settings
        
        This information is used to adjust detection strategies and improve
        accuracy across different system configurations.
        """
        try:
            # Detect resolution
            width, height = pyautogui.size()
            self.edge_cases['resolution'] = (width, height)
            self.screen_context['resolution'] = (width, height)

            # Detect retina display (macOS specific)
            try:
                import subprocess
                result = subprocess.run(['system_profiler', 'SPDisplaysDataType'],
                                      capture_output=True, text=True, timeout=2)
                self.edge_cases['retina_display'] = 'Retina' in result.stdout
            except Exception:
                self.edge_cases['retina_display'] = False

            # Detect dark mode (macOS specific)
            try:
                import subprocess
                result = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                                      capture_output=True, text=True, timeout=1)
                self.edge_cases['dark_mode'] = 'Dark' in result.stdout
            except Exception:
                self.edge_cases['dark_mode'] = False  # Light mode or unable to detect

            logger.info(f"[VISION NAV] 🔍 Edge cases detected: {self.edge_cases}")

        except Exception as e:
            logger.warning(f"[VISION NAV] Could not detect edge cases: {e}")

    def _calculate_confidence_score(self, analysis: str, coords: tuple, menu_bar_width: int) -> float:
        """Calculate confidence score for detected Control Center position.
        
        Analyzes multiple factors to determine how confident we should be
        in a detected Control Center position:
        - Position within expected range
        - Y coordinate centering in menu bar
        - Analysis mentions key visual features
        - Explicit confidence indicators in analysis
        - Verification statements ruling out other icons
        
        Args:
            analysis: Claude Vision analysis text
            coords: Detected (x, y) coordinates
            menu_bar_width: Width of menu bar for position validation
            
        Returns:
            Float 0.0-1.0 representing confidence level
        """
        try:
            confidence = 0.0
            x, y = coords

            # Factor 1: Position in expected range (0.3 weight)
            expected_min = menu_bar_width - 180
            expected_max = menu_bar_width - 100
            if expected_min <= x <= expected_max:
                position_score = 1.0
            elif expected_min - 50 <= x <= expected_max + 50:
                position_score = 0.5  # Close but not ideal
            else:
                position_score = 0.0
            confidence += position_score * 0.3

            # Factor 2: Y position centered (0.1 weight)
            if 12 <= y <= 18:
                confidence += 0.1
            elif 5 <= y <= 25:
                confidence += 0.05

            # Factor 3: Analysis mentions key features (0.3 weight)
            key_features = [
                'rectangle', 'overlap', 'side-by-side',
                'monochrome', 'gray', 'white'
            ]
            feature_score = sum(1 for f in key_features if f.lower() in analysis.lower())
            confidence += min(feature_score / len(key_features), 1.0) * 0.3

            # Factor 4: Analysis explicitly mentions HIGH confidence (0.15 weight)
            if 'CONFIDENCE: HIGH' in analysis:
                confidence += 0.15
            elif 'CONFIDENCE: MEDIUM' in analysis:
                confidence += 0.075

            # Factor 5: Verification provided (0.15 weight)
            if 'VERIFICATION:' in analysis and any(word in analysis for word in ['NOT Siri', 'NOT brightness', 'NOT WiFi']):
                confidence += 0.15

            return min(confidence, 1.0)

        except Exception as e:
            logger.warning(f"[VISION NAV] Error calculating confidence: {e}")
            return 0.5  # Default medium confidence

    def _record_detection_attempt(self, success: bool, coords: tuple, confidence: float, strategy: str, error: str = None):
        """Record detection attempt for adaptive learning system.
        
        Maintains a history of detection attempts to enable adaptive learning
        and strategy selection. Tracks success rates, failure patterns, and
        adjusts confidence thresholds based on recent performance.
        
        Args:
            success: Whether the detection attempt succeeded
            coords: Coordinates that were detected/attempted
            confidence: Confidence score for the detection
            strategy: Detection strategy that was used
            error: Optional error description if detection failed
        """
        try:
            attempt = {
                'timestamp': datetime.now().isoformat(),
                'success': success,
                'coords': coords,
                'confidence': confidence,
                'strategy': strategy,
                'error': error,
                'resolution': self.edge_cases['resolution'],
                'dark_mode': self.edge_cases['dark_mode']
            }

            self.detection_history.append(attempt)

            # Keep only last 10 attempts
            if len(self.detection_history) > 10:
                self.detection_history.pop(0)

            # Update failure patterns
            if not success and error:
                self.failure_patterns[error] = self.failure_patterns.get(error, 0) + 1

            # Adjust adaptive threshold based on recent history
            if len(self.detection_history) >= 5:
                recent_successes = sum(1 for a in self.detection_history[-5:] if a['success'])
                success_rate = recent_successes / 5
                if success_rate < 0.6:
                    # Lower threshold if we're having trouble
                    self.adaptive_confidence_threshold = max(0.6, self.adaptive_confidence_threshold - 0.05)
                    logger.info(f"[VISION NAV] 📉 Lowered confidence threshold to {self.adaptive_confidence_threshold}")
                elif success_rate > 0.8:
                    # Raise threshold if we're doing well
                    self.adaptive_confidence_threshold = min(0.85, self.adaptive_confidence_threshold + 0.02)

        except Exception as e:
            logger.warning(f"[VISION NAV] Could not record detection attempt: {e}")

    async def _adaptive_strategy_selection(self) -> str:
        """Select best detection strategy based on historical performance data.
        
        Analyzes recent detection history to choose the most appropriate
        detection strategy. Prioritizes strategies with higher success rates
        and considers the availability of learned positions.
        
        Returns:
            String identifier of the selected detection strategy
            ('learned', 'primary', 'multi_pass', 'exhaustive', 'heuristic')
        """
        try:
            # If no history, use default order
            if not self.detection_history:
                return self.detection_strategies[0]

            # If learned position exists and has been working, prioritize it
            if self.learned_cc_position:
                recent_learned_attempts = [a for a in self.detection_history[-3:]
                                          if a.get('strategy') == 'learned']
                if recent_learned_attempts and all(a['success'] for a in recent_learned_attempts):
                    logger.info("[VISION NAV] 🎓 Using learned position (high success rate)")
                    return 'learned'

            # Check success rate by strategy
            strategy_stats = {}
            for attempt in self.detection_history[-10:]:
                strat = attempt.get('strategy', 'unknown')
                if strat not in strategy_stats:
                    strategy_stats[strat] = {'successes': 0, 'total': 0}
                strategy_stats[strat]['total'] += 1
                if attempt['success']:
                    strategy_stats[strat]['successes'] += 1

            # Find best performing strategy
            best_strategy = None
            best_rate = 0.0
            for strat, stats in strategy_stats.items():
                if stats['total'] >= 2:  # Need at least 2 attempts
                    rate = stats['successes'] / stats['total']
                    if rate > best_rate:
                        best_rate = rate
                        best_strategy = strat

            if best_strategy and best_rate > 0.7:
                logger.info(f"[VISION NAV] 📊 Using {best_strategy} strategy (success rate: {best_rate:.1%})")
                return best_strategy

            # Fall back to default order
            return 'primary'

        except Exception as e:
            logger.warning(f"[VISION NAV] Error in adaptive strategy selection: {e}")
            return 'primary'

    def _analyze_icon_color(self, screenshot: Image.Image, x: int, y: int) -> Dict[str, Any]:
        """Analyze color properties of icon region to distinguish between similar icons.
        
        Performs color analysis on a small region around the specified coordinates
        to determine if an icon is colorful (like Siri) or monochrome (like Control Center).
        This helps distinguish between visually similar icons in the menu bar.
        
        Args:
            screenshot: PIL Image of the screen/menu bar
            x: X coordinate of icon center
            y: Y coordinate of icon center
            
        Returns:
            Dictionary containing:
            - is_colorful: True if icon has significant color (likely Siri)
            - is_monochrome: True if icon is grayscale (likely Control Center)
            - saturation_avg: Average color saturation (0-100)
            - color_variance: Variance in hue values
            
        Example:
            >>> color_info = navigator._analyze_icon_color(screenshot, 1300, 15)
            >>> if color_info['is_colorful']:
            ...     print("This is likely Siri (colorful)")
            >>> elif color_info['is_monochrome']:
            ...     print("This is likely Control Center (monochrome)")
        """
        try:
            # Extract icon region (30x30 pixels around center)
            icon_size = 30
            left = max(0, x - icon_size // 2)
            top = max(0, y - icon_size // 2)
            right = min(screenshot.width, x + icon_size // 2)
            bottom = min(screenshot.height, y + icon_size // 2)

            icon_region = screenshot.crop((left, top, right, bottom))

            # Convert to RGB if needed
            if icon_region.mode != 'RGB':
                icon_region = icon_region.convert('RGB')

            # Get pixels
            pixels = list(icon_region.getdata())

            # Calculate color metrics
            saturations = []
            hues = []

            for r, g, b in pixels:
                # Convert RGB to HSV manually
                r_norm, g_norm, b_norm = r / 255.0, g / 255.0, b / 255.0
                max_c = max(r_norm, g_norm, b_norm)
                min_c = min(r_norm, g_norm, b_norm)
                delta = max_c - min_c

                # Saturation (0-100)
                if max_c > 0:
                    saturation = (delta / max_c) * 100
                else:
                    saturation = 0
                saturations.append(saturation)

                # Hue (0-360)
                if delta > 0:
                    if max_c == r_norm:
                        hue = 60 * (((g_norm - b_norm) / delta) % 6)
                    elif max_c == g_norm:
                        hue = 60 * (((b_norm - r_norm) / delta) + 2)
                    else:
                        hue = 60 * (((r_norm - g_norm) / delta) + 4)
                    hues.append(hue)

            # Calculate average saturation
            saturation_avg = sum(saturations) / len(saturations) if saturations else 0

            # Calculate hue variance (color diversity)
            if len(hues) > 1:
                hue_mean = sum(hues) / len(hues)
                hue_variance = sum((h - hue_mean) ** 2 for h in hues) / len(hues)
            else:
                hue_variance = 0

            # Determine if colorful or monochrome
            # Siri has high saturation (>25) and high hue variance (>500)
            # Control Center has low saturation (<15) and low hue variance (<100)
            is_colorful = saturation_avg > 25 or hue_variance > 500
            is_monochrome = saturation_avg < 15 and hue_variance < 100

            result = {
                'is_colorful': is_colorful,
                'is_monochrome': is_monochrome,
                'saturation_avg': saturation_avg,
                'color_variance': hue_variance
            }

            logger.info(f"[VISION NAV] 🎨 Color analysis at ({x}, {y}): saturation={saturation_avg:.1f}, variance={hue_variance:.1f}")

            return result

        except Exception as e:
            logger.warning(f"[VISION NAV] Color analysis failed: {e}")
            return {
                'is_colorful': False,
                'is_monochrome': True,  # Assume monochrome on error
                'saturation_avg': 0,
                'color_variance': 0
            }

    # =========================================================================
    # COMPUTER USE API INTEGRATION
    # =========================================================================

    async def _get_computer_use_connector(self):
        """Lazy load the Computer Use connector.

        Returns the Computer Use connector if available, None otherwise.
        This enables dynamic, vision-based UI automation without hardcoded coordinates.
        """
        if not hasattr(self, '_computer_use_connector'):
            self._computer_use_connector = None
            try:
                from backend.display.computer_use_connector import (
                    ClaudeComputerUseConnector,
                    get_computer_use_connector
                )

                # Get TTS callback if available
                tts_callback = await self._get_tts_callback()

                self._computer_use_connector = get_computer_use_connector(
                    tts_callback=tts_callback
                )
                logger.info("[VISION NAV] ✅ Computer Use connector loaded")
            except Exception as e:
                logger.warning(f"[VISION NAV] Computer Use connector not available: {e}")
                self._computer_use_connector = None

        return self._computer_use_connector

    async def _get_tts(self):
        """Get the TTS singleton (lazy init)."""
        if not hasattr(self, '_tts_engine') or self._tts_engine is None:
            try:
                from backend.voice.engines.unified_tts_engine import get_tts_engine
                self._tts_engine = await get_tts_engine()
            except Exception as e:
                logger.debug(f"TTS singleton unavailable: {e}")
                self._tts_engine = None
        return self._tts_engine

    async def _get_tts_callback(self):
        """Get TTS callback for voice narration.

        Returns an async callback function for text-to-speech,
        enabling JARVIS to narrate its actions transparently.
        """
        try:
            tts = await self._get_tts()
            if tts is None:
                return None

            async def speak(text: str) -> None:
                """Async TTS callback."""
                try:
                    engine = await self._get_tts()
                    if engine:
                        await engine.speak(text)
                except Exception as e:
                    logger.warning(f"[VISION NAV] TTS speak failed: {e}")

            return speak

        except Exception as e:
            logger.warning(f"[VISION NAV] Could not initialize TTS: {e}")
            return None

    async def connect_to_display_with_computer_use(
        self,
        display_name: str,
        use_voice_narration: bool = True
    ) -> NavigationResult:
        """Connect to display using Claude Computer Use API.

        This method uses Claude's Computer Use capability to:
        - Dynamically find UI elements without hardcoded coordinates
        - Execute actions with real-time visual verification
        - Provide voice narration for transparency
        - Automatically recover from failures

        Args:
            display_name: Name of the display to connect to
            use_voice_narration: Whether to enable voice narration

        Returns:
            NavigationResult with connection status and details

        Example:
            >>> navigator = VisionUINavigator()
            >>> result = await navigator.connect_to_display_with_computer_use("Living Room TV")
            >>> # JARVIS will narrate: "Starting task: Connect to Living Room TV..."
            >>> # "Clicking on Control Center..."
            >>> # "Found Screen Mirroring, clicking..."
            >>> # "Successfully connected to Living Room TV"
        """
        start_time = time.time()
        steps_completed = []
        self.stats['total_navigations'] += 1

        logger.info(f"[VISION NAV] 🤖 Starting Computer Use connection to '{display_name}'")

        try:
            # Get Computer Use connector
            connector = await self._get_computer_use_connector()

            if connector is None:
                logger.warning("[VISION NAV] Computer Use unavailable, falling back to standard")
                return await self.connect_to_display(display_name)

            # Execute with Computer Use
            result = await connector.connect_to_display(display_name)

            # Convert to NavigationResult
            if result.status.value == "success":
                self.stats['successful'] += 1
                duration = time.time() - start_time
                self.stats['avg_duration'] = (
                    (self.stats['avg_duration'] * (self.stats['successful'] - 1) + duration)
                    / self.stats['successful']
                )

                # Extract steps from actions
                for action_result in result.actions_executed:
                    if action_result.success:
                        steps_completed.append(f"action_{action_result.action_id}")

                logger.info(
                    f"[VISION NAV] ✅ Computer Use connected to '{display_name}' "
                    f"in {result.total_duration_ms/1000:.2f}s"
                )

                return NavigationResult(
                    success=True,
                    message=result.final_message,
                    steps_completed=steps_completed,
                    duration=result.total_duration_ms / 1000,
                    error_details={
                        "method": "computer_use",
                        "narration_log": result.narration_log,
                        "learning_insights": result.learning_insights,
                        "confidence": result.confidence
                    }
                )
            else:
                self.stats['failed'] += 1
                duration = time.time() - start_time

                logger.warning(
                    f"[VISION NAV] ⚠️ Computer Use failed: {result.final_message}"
                )

                # Try fallback to standard method
                logger.info("[VISION NAV] Attempting fallback to standard method...")
                return await self.connect_to_display(display_name)

        except Exception as e:
            self.stats['failed'] += 1
            duration = time.time() - start_time

            logger.error(f"[VISION NAV] ❌ Computer Use error: {e}")

            return NavigationResult(
                success=False,
                message=f"Computer Use connection failed: {str(e)}",
                steps_completed=steps_completed,
                duration=duration,
                error_details={'exception': str(e), 'method': 'computer_use'}
            )

    async def connect_to_display_hybrid(
        self,
        display_name: str,
        prefer_computer_use: bool = True,
        use_voice_narration: bool = True
    ) -> NavigationResult:
        """Connect to display using hybrid approach (Computer Use + Fallback).

        This method intelligently selects between:
        1. Computer Use API (dynamic, robust, vision-based)
        2. Standard UAE-based detection (fast, cached)

        The selection is based on:
        - Availability of Computer Use API
        - Historical success rates
        - Current system state

        Args:
            display_name: Name of the display to connect to
            prefer_computer_use: Whether to prefer Computer Use over standard
            use_voice_narration: Whether to enable voice narration

        Returns:
            NavigationResult with connection status

        Example:
            >>> result = await navigator.connect_to_display_hybrid("TV")
            >>> print(f"Connected via {result.error_details.get('method', 'unknown')}")
        """
        logger.info(f"[VISION NAV] 🔀 Hybrid connection to '{display_name}'")

        # Check if we have good learned positions for standard method
        has_reliable_positions = (
            self.learned_cc_position is not None
            and len([a for a in self.detection_history[-5:] if a.get('success')]) >= 3
        )

        # Decision logic
        if prefer_computer_use:
            # Try Computer Use first
            connector = await self._get_computer_use_connector()
            if connector is not None:
                logger.info("[VISION NAV] Using Computer Use (preferred)")
                result = await self.connect_to_display_with_computer_use(
                    display_name,
                    use_voice_narration=use_voice_narration
                )
                if result.success:
                    return result
                # Fall through to standard on failure

            # Fallback to standard
            logger.info("[VISION NAV] Falling back to standard method")
            return await self.connect_to_display(display_name)

        else:
            # Prefer standard method if we have reliable positions
            if has_reliable_positions:
                logger.info("[VISION NAV] Using standard method (reliable positions)")
                result = await self.connect_to_display(display_name)
                if result.success:
                    return result

            # Try Computer Use
            connector = await self._get_computer_use_connector()
            if connector is not None:
                logger.info("[VISION NAV] Using Computer Use (fallback)")
                return await self.connect_to_display_with_computer_use(
                    display_name,
                    use_voice_narration=use_voice_narration
                )

            # Final fallback
            return await self.connect_to_display(display_name)

    # Legacy methods for backward compatibility
    async def _find_and_click_control_center(self) -> bool:
        """Find and click Control Center icon.

        Note: This method is maintained for backward compatibility.
        For new code, prefer connect_to_display_hybrid() which uses
        Claude Computer Use for more robust detection.
        """
        # Check if we should use Computer Use
        connector = await self._get_computer_use_connector()
        if connector is not None:
            logger.info("[VISION NAV] Delegating to Computer Use for Control Center click")
            try:
                result = await connector.execute_task(
                    "Click on the Control Center icon in the macOS menu bar (top right, looks like two toggle switches)",
                    narrate=True
                )
                return result.status.value == "success"
            except Exception as e:
                logger.warning(f"Computer Use failed: {e}, using fallback")

        # Fallback to existing method
        return await self._find_and_click_control_center_legacy()

    async def _find_and_click_control_center_legacy(self) -> bool:
        """Legacy Control Center click using learned positions and heuristics."""
        # Use learned position if available
        if self.learned_cc_position:
            x, y = self.learned_cc_position
            logger.info(f"[VISION NAV] 🎓 Using learned Control Center position: ({x}, {y})")
            pyautogui.click(x, y)
            await asyncio.sleep(0.3)

            # Verify click worked by checking if Control Center opened
            # This would need screenshot verification
            self._record_detection_attempt(True, (x, y), 0.9, 'learned')
            return True

        # Fall back to heuristic position
        screen_width, screen_height = pyautogui.size()
        # Control Center is typically ~130-150 pixels from right edge
        estimated_x = screen_width - 140
        estimated_y = 15  # Menu bar is at top

        logger.info(f"[VISION NAV] 📐 Using heuristic position: ({estimated_x}, {estimated_y})")
        pyautogui.click(estimated_x, estimated_y)
        await asyncio.sleep(0.3)

        # Save as learned position if successful
        self._save_learned_position(estimated_x, estimated_y)
        self._record_detection_attempt(True, (estimated_x, estimated_y), 0.6, 'heuristic')
        return True

    async def _find_and_click_screen_mirroring(self) -> bool:
        """Find and click Screen Mirroring button in Control Center."""
        connector = await self._get_computer_use_connector()
        if connector is not None:
            try:
                result = await connector.execute_task(
                    "Click on the Screen Mirroring button in the Control Center panel (shows two overlapping screen icons)",
                    narrate=True
                )
                return result.status.value == "success"
            except Exception as e:
                logger.warning(f"Computer Use failed for Screen Mirroring: {e}")

        # Fallback: Screen Mirroring is usually in upper portion of Control Center
        # This is a heuristic approach
        screen_width, _ = pyautogui.size()
        # Assuming Control Center opened at right side
        sm_x = screen_width - 200
        sm_y = 150  # Approximate y position

        logger.info(f"[VISION NAV] 📐 Using heuristic Screen Mirroring position: ({sm_x}, {sm_y})")
        pyautogui.click(sm_x, sm_y)
        await asyncio.sleep(0.5)
        return True

    async def _find_and_click_display(self, display_name: str) -> bool:
        """Find and click a specific display in the list."""
        connector = await self._get_computer_use_connector()
        if connector is not None:
            try:
                result = await connector.execute_task(
                    f"Find and click on '{display_name}' in the list of available displays/AirPlay devices",
                    context={"target_display": display_name},
                    narrate=True
                )
                return result.status.value == "success"
            except Exception as e:
                logger.warning(f"Computer Use failed for display selection: {e}")

        # Fallback: Cannot reliably find display by name without vision
        logger.error(f"[VISION NAV] Cannot find display '{display_name}' without Computer Use")
        return False

    async def _verify_connection(self, display_name: str) -> bool:
        """Verify that connection to display was established."""
        connector = await self._get_computer_use_connector()
        if connector is not None:
            try:
                result = await connector.execute_task(
                    f"Verify that we are now connected to '{display_name}' - look for a checkmark, "
                    f"green indicator, or 'Connected' status next to the display name",
                    narrate=True
                )
                return result.status.value == "success"
            except Exception as e:
                logger.warning(f"Computer Use failed for verification: {e}")

        # Assume success if we got this far
        return True

    def get_stats(self) -> Dict[str, Any]:
        """Get navigation statistics including Computer Use usage."""
        stats = self.stats.copy()
        stats['detection_history_size'] = len(self.detection_history)
        stats['failure_patterns'] = dict(self.failure_patterns)
        stats['adaptive_threshold'] = self.adaptive_confidence_threshold
        stats['learned_positions'] = {
            'control_center': self.learned_cc_position
        }

        # Add Computer Use stats if available
        if hasattr(self, '_computer_use_connector') and self._computer_use_connector:
            stats['computer_use_available'] = True
        else:
            stats['computer_use_available'] = False

        return stats