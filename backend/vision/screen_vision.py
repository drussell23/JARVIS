"""
JARVIS Screen Vision System - Computer Vision for macOS Screen Understanding
"""

import asyncio
import base64
import io
import os
import re
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import Quartz
import Vision
import AppKit
from PIL import Image
import pytesseract
import cv2
import numpy as np
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# Import Claude Vision Analyzer if available
try:
    from .optimized_claude_vision import (
        OptimizedClaudeVisionAnalyzer as ClaudeVisionAnalyzer,
    )

    logger.info("Using optimized Claude Vision analyzer")
except ImportError:
    try:
        from .claude_vision_analyzer import ClaudeVisionAnalyzer

        logger.info("Using standard Claude Vision analyzer")
    except ImportError:
        ClaudeVisionAnalyzer = None

# Import OCR Strategy Manager for intelligent OCR fallbacks
try:
    from backend.context_intelligence.managers import (
        get_ocr_strategy_manager,
        initialize_ocr_strategy_manager
    )
    OCR_STRATEGY_AVAILABLE = True
except ImportError:
    OCR_STRATEGY_AVAILABLE = False
    get_ocr_strategy_manager = lambda: None
    initialize_ocr_strategy_manager = lambda **kwargs: None
    logger.warning("OCRStrategyManager not available - using legacy Tesseract only")


class UpdateType(Enum):
    """Types of software updates that can be detected"""

    MACOS_UPDATE = "macos_update"
    APP_UPDATE = "app_update"
    BROWSER_UPDATE = "browser_update"
    SECURITY_UPDATE = "security_update"
    SYSTEM_NOTIFICATION = "system_notification"


@dataclass
class ScreenElement:
    """Represents a detected element on screen"""

    type: str
    text: str
    location: Tuple[int, int, int, int]  # x, y, width, height
    confidence: float
    metadata: Optional[Dict] = None


@dataclass
class UpdateNotification:
    """Represents a detected software update"""

    update_type: UpdateType
    application: str
    version: Optional[str]
    description: str
    urgency: str  # "critical", "recommended", "optional"
    detected_at: datetime
    screenshot_region: Optional[Tuple[int, int, int, int]] = None


class ScreenVisionSystem:
    """Computer vision system for understanding macOS screen content"""

    def __init__(self):
        """Initialize the screen vision system"""
        self.update_patterns = self._initialize_update_patterns()
        self.notification_patterns = self._initialize_notification_patterns()
        self.last_scan_time = None
        self.detected_updates = []

        # Initialize Claude Vision Analyzer if API key is available
        self.claude_analyzer = None
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key and ClaudeVisionAnalyzer:
            try:
                self.claude_analyzer = ClaudeVisionAnalyzer(api_key)
            except Exception as e:
                print(f"Failed to initialize Claude Vision Analyzer: {e}")

        # Initialize OCR Strategy Manager for intelligent OCR with fallbacks
        self.ocr_strategy_manager = None
        if OCR_STRATEGY_AVAILABLE and self.claude_analyzer:
            try:
                # Get Claude API client from analyzer if available
                client = getattr(self.claude_analyzer, 'client', None)
                if client:
                    self.ocr_strategy_manager = get_ocr_strategy_manager()
                    if not self.ocr_strategy_manager:
                        self.ocr_strategy_manager = initialize_ocr_strategy_manager(
                            api_client=client,
                            cache_ttl=300.0,  # 5 minutes
                            max_cache_entries=200,
                            enable_error_matrix=True
                        )
                    logger.info("✅ OCR Strategy Manager initialized for intelligent OCR")
            except Exception as e:
                logger.warning(f"Failed to initialize OCR Strategy Manager: {e}")

    def _initialize_update_patterns(self) -> Dict[str, List[re.Pattern]]:
        """Initialize patterns for detecting software updates"""
        return {
            "macos": [
                re.compile(r"macOS.*update.*available", re.I),
                re.compile(r"Software Update.*available", re.I),
                re.compile(r"Update to macOS.*\d+\.\d+", re.I),
                re.compile(r"System.*Update.*Required", re.I),
            ],
            "apps": [
                re.compile(r"Update Available", re.I),
                re.compile(r"New version.*available", re.I),
                re.compile(r"Update to version.*\d+\.\d+", re.I),
                re.compile(r"(\w+)\s+needs?\s+to\s+be\s+updated", re.I),
            ],
            "security": [
                re.compile(r"Security Update", re.I),
                re.compile(r"Critical.*Update", re.I),
                re.compile(r"Important.*Security.*Fix", re.I),
            ],
            "browsers": [
                re.compile(r"Chrome.*update.*available", re.I),
                re.compile(r"Safari.*update", re.I),
                re.compile(r"Firefox.*new version", re.I),
            ],
        }

    def _initialize_notification_patterns(self) -> Dict[str, List[str]]:
        """Initialize patterns for notification areas"""
        return {
            "notification_center": [
                "Notification Center",
                "Updates",
                "Software Update",
            ],
            "menu_bar": ["Updates available", "↓", "●"],  # Common update indicators
            "dock_badges": ["1", "2", "3", "!"],  # App badge indicators
            "system_preferences": ["Software Update", "App Store", "Updates"],
        }

    async def capture_screen(
        self, region: Optional[Tuple[int, int, int, int]] = None
    ) -> Optional[Image.Image]:
        """Capture the screen or a specific region and return as PIL Image"""
        # Use Quartz to capture screen
        if region:
            x, y, width, height = region
            screenshot = Quartz.CGWindowListCreateImage(
                Quartz.CGRectMake(x, y, width, height),
                Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID,
                Quartz.kCGWindowImageDefault,
            )
        else:
            # Capture entire screen
            screenshot = Quartz.CGDisplayCreateImage(Quartz.CGMainDisplayID())

        # Check if screenshot was captured successfully
        if screenshot is None:
            # Try fallback method using screencapture command
            try:
                from .screen_capture_fallback import capture_screen_fallback

                print("Quartz capture failed, trying screencapture fallback...")
                fallback_image = capture_screen_fallback()
                if fallback_image is not None:
                    print("✓ Fallback capture successful!")
                    return fallback_image
            except Exception as e:
                print(f"Fallback also failed: {e}")

            # Return None if capture failed
            print(
                "Warning: Screen capture failed - please grant screen recording permission"
            )
            print(
                "Go to: System Preferences → Security & Privacy → Privacy → Screen Recording"
            )
            print("Then check the box next to Terminal (or your Python/IDE)")
            return None

        # Convert to numpy array
        width = Quartz.CGImageGetWidth(screenshot)
        height = Quartz.CGImageGetHeight(screenshot)
        bytes_per_row = Quartz.CGImageGetBytesPerRow(screenshot)

        pixel_data = Quartz.CGDataProviderCopyData(
            Quartz.CGImageGetDataProvider(screenshot)
        )

        # Check if pixel data was retrieved successfully
        if pixel_data is None:
            print("Warning: Could not get pixel data from screenshot")
            return None

        try:
            # Convert pixel data to numpy array
            image_data = np.frombuffer(pixel_data, dtype=np.uint8)

            # Calculate the expected data size
            expected_size = height * bytes_per_row
            actual_size = len(image_data)

            # Trim any extra data (sometimes there's padding at the end)
            if actual_size > expected_size:
                image_data = image_data[:expected_size]
            elif actual_size < expected_size:
                # If we have less data than expected, something's wrong
                print(
                    f"Warning: Insufficient pixel data. Expected {expected_size}, got {actual_size}"
                )
                return None

            # First reshape to get rows
            image = image_data.reshape((height, bytes_per_row))

            # Now reshape to get pixels (BGRA format, 4 bytes per pixel)
            pixels_per_row = bytes_per_row // 4
            image = image.reshape((height, pixels_per_row, 4))

            # Crop to actual width (in case bytes_per_row includes padding)
            image = image[:, :width, :]

            # Convert BGRA to RGB (skip alpha channel)
            image = image[:, :, [2, 1, 0]]  # BGR to RGB

            # Convert numpy array to PIL Image
            pil_image = Image.fromarray(image)
            return pil_image
        except Exception as e:
            print(f"Error converting screenshot to PIL Image: {e}")
            print(
                f"Debug info: height={height}, width={width}, bytes_per_row={bytes_per_row}"
            )
            print(f"Data size: {len(pixel_data) if pixel_data else 'None'}")
            return None

    async def extract_text_from_image(self, image_path: str) -> Tuple[str, float]:
        """
        Extract all text from an image using intelligent OCR with fallbacks

        Args:
            image_path: Path to image file

        Returns:
            (extracted_text, confidence)
        """
        if self.ocr_strategy_manager:
            try:
                result = await self.ocr_strategy_manager.extract_text_with_fallbacks(
                    image_path=image_path,
                    cache_max_age=300.0
                )

                if result.success:
                    logger.info(
                        f"✅ OCR: extracted {len(result.text)} chars via {result.method} "
                        f"(confidence={result.confidence:.2f})"
                    )
                    return result.text, result.confidence
                else:
                    logger.warning(f"OCR Strategy Manager failed: {result.error}")
            except Exception as e:
                logger.error(f"OCR Strategy Manager error: {e}")

        # Fallback to legacy pytesseract
        try:
            from PIL import Image as PILImage
            img = PILImage.open(image_path)
            text = pytesseract.image_to_string(img).strip()
            return text, 0.5  # Default medium confidence
        except Exception as e:
            logger.error(f"Legacy OCR failed: {e}")
            return "", 0.0

    async def detect_text_regions(self, image: np.ndarray) -> List[ScreenElement]:
        """
        Detect and extract text regions from screen image

        Note: This method uses pytesseract directly for ROI-based detection.
        For full-image OCR with intelligent fallbacks, use extract_text_from_image()
        """
        elements = []

        # Convert to grayscale for better OCR
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

        # Apply thresholding to get better text detection
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # Find contours (potential text regions)
        contours, _ = cv2.findContours(
            thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)

            # Filter out very small regions
            if w < 20 or h < 10:
                continue

            # Extract region and perform OCR (using pytesseract for small ROIs)
            roi = image[y : y + h, x : x + w]
            text = pytesseract.image_to_string(roi, config="--psm 8").strip()

            if text:
                element = ScreenElement(
                    type="text",
                    text=text,
                    location=(x, y, w, h),
                    confidence=0.8,  # Could use pytesseract confidence
                )
                elements.append(element)

        return elements

    async def detect_ui_elements(self, image: np.ndarray) -> List[ScreenElement]:
        """Detect UI elements like buttons, windows, notifications"""
        elements = []

        # Detect notification badges (usually red circles)
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)

        # Red color range for badges
        lower_red = np.array([0, 120, 70])
        upper_red = np.array([10, 255, 255])
        mask1 = cv2.inRange(hsv, lower_red, upper_red)

        lower_red = np.array([170, 120, 70])
        upper_red = np.array([180, 255, 255])
        mask2 = cv2.inRange(hsv, lower_red, upper_red)

        mask = mask1 + mask2

        # Find circular contours (badges)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            area = cv2.contourArea(contour)
            if area > 50 and area < 500:  # Badge size range
                x, y, w, h = cv2.boundingRect(contour)

                # Check if it's circular
                circularity = 4 * np.pi * area / (cv2.arcLength(contour, True) ** 2)
                if circularity > 0.7:
                    element = ScreenElement(
                        type="notification_badge",
                        text="Update indicator",
                        location=(x, y, w, h),
                        confidence=0.9,
                    )
                    elements.append(element)

        return elements

    async def analyze_for_updates(self, image: np.ndarray) -> List[UpdateNotification]:
        """Analyze screen for software update notifications"""
        updates = []

        # Extract text from screen
        text_elements = await self.detect_text_regions(image)
        ui_elements = await self.detect_ui_elements(image)

        # Check text against update patterns
        for element in text_elements:
            text = element.text

            # Check macOS updates
            for pattern in self.update_patterns["macos"]:
                if pattern.search(text):
                    update = UpdateNotification(
                        update_type=UpdateType.MACOS_UPDATE,
                        application="macOS",
                        version=self._extract_version(text),
                        description=text,
                        urgency="recommended",
                        detected_at=datetime.now(),
                        screenshot_region=element.location,
                    )
                    updates.append(update)
                    break

            # Check app updates
            for pattern in self.update_patterns["apps"]:
                match = pattern.search(text)
                if match:
                    app_name = self._extract_app_name(text)
                    update = UpdateNotification(
                        update_type=UpdateType.APP_UPDATE,
                        application=app_name or "Unknown App",
                        version=self._extract_version(text),
                        description=text,
                        urgency="optional",
                        detected_at=datetime.now(),
                        screenshot_region=element.location,
                    )
                    updates.append(update)
                    break

            # Check security updates
            for pattern in self.update_patterns["security"]:
                if pattern.search(text):
                    update = UpdateNotification(
                        update_type=UpdateType.SECURITY_UPDATE,
                        application=self._extract_app_name(text) or "System",
                        version=self._extract_version(text),
                        description=text,
                        urgency="critical",
                        detected_at=datetime.now(),
                        screenshot_region=element.location,
                    )
                    updates.append(update)
                    break

        # Check for notification badges
        if ui_elements:
            # If we found notification badges, there might be updates
            update = UpdateNotification(
                update_type=UpdateType.SYSTEM_NOTIFICATION,
                application="System",
                version=None,
                description=f"Found {len(ui_elements)} notification badges",
                urgency="optional",
                detected_at=datetime.now(),
            )
            updates.append(update)

        return updates

    def _extract_version(self, text: str) -> Optional[str]:
        """Extract version number from text"""
        version_pattern = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)")
        match = version_pattern.search(text)
        return match.group(1) if match else None

    def _extract_app_name(self, text: str) -> Optional[str]:
        """Extract application name from update text"""
        # Common patterns for app names in updates
        patterns = [
            re.compile(r"^(\w+(?:\s+\w+)?)\s+update", re.I),
            re.compile(r"Update\s+(\w+(?:\s+\w+)?)", re.I),
            re.compile(r"(\w+(?:\s+\w+)?)\s+needs?\s+to\s+be\s+updated", re.I),
        ]

        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return match.group(1)

        return None

    async def scan_for_updates(self) -> List[UpdateNotification]:
        """Perform a full screen scan for updates"""
        # Capture full screen
        screen_image = await self.capture_screen()

        # Analyze for updates
        updates = await self.analyze_for_updates(screen_image)

        # Update internal state
        self.last_scan_time = datetime.now()
        self.detected_updates = updates

        return updates

    async def monitor_screen_continuously(self, callback, interval: int = 300):
        """Monitor screen for updates at regular intervals

        Args:
            callback: Function to call when updates are detected
            interval: Seconds between scans (default 5 minutes)
        """
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        while time.monotonic() - session_start < max_runtime:
            try:
                updates = await self.scan_for_updates()
                if updates:
                    await callback(updates)
            except Exception as e:
                print(f"Error during screen monitoring: {e}")

            await asyncio.sleep(interval)
        else:
            logger.info("Screen monitoring session timeout, stopping")

    async def get_screen_context(
        self, region: Optional[Tuple[int, int, int, int]] = None
    ) -> Dict[str, Any]:
        """Get comprehensive context about what's on screen"""
        # Capture screen
        pil_image = await self.capture_screen(region)

        if pil_image is None:
            # Return minimal context if screen capture failed
            return {
                "timestamp": datetime.now().isoformat(),
                "screen_size": (100, 100),  # placeholder
                "text_elements": [],
                "ui_elements": [],
                "detected_apps": [],
                "potential_updates": 0,
            }

        # Convert PIL Image to numpy array for OpenCV operations
        image_array = np.array(pil_image)

        # Extract all elements
        text_elements = await self.detect_text_regions(image_array)
        ui_elements = await self.detect_ui_elements(image_array)

        # Build context
        context = {
            "timestamp": datetime.now().isoformat(),
            "screen_size": pil_image.size,
            "text_elements": [
                {
                    "text": elem.text,
                    "location": elem.location,
                    "confidence": elem.confidence,
                }
                for elem in text_elements
            ],
            "ui_elements": [
                {"type": elem.type, "location": elem.location} for elem in ui_elements
            ],
            "detected_apps": self._detect_open_applications(text_elements),
            "potential_updates": len(await self.analyze_for_updates(image_array)),
        }

        return context

    def _detect_open_applications(
        self, text_elements: List[ScreenElement]
    ) -> List[str]:
        """Detect which applications are visible on screen"""
        common_apps = [
            "Chrome",
            "Safari",
            "Firefox",
            "Mail",
            "Messages",
            "Slack",
            "Discord",
            "Zoom",
            "Teams",
            "Spotify",
            "Music",
            "Terminal",
            "VS Code",
            "Xcode",
            "Finder",
            "System Preferences",
        ]

        detected = []
        all_text = " ".join([elem.text for elem in text_elements])

        for app in common_apps:
            if app.lower() in all_text.lower():
                detected.append(app)

        return detected

    async def capture_and_describe(self) -> str:
        """Capture screen and provide a natural language description"""
        # First, try to capture the screen to test permissions
        test_capture = await self.capture_screen()

        # Check if screen capture is working
        if test_capture is None:
            return (
                "I'm unable to see your screen at the moment. "
                "Please ensure I have screen recording permission in "
                "System Preferences → Security & Privacy → Privacy → Screen Recording."
            )

        # Skip context analysis - go directly to Claude Vision
        # context = await self.get_screen_context()

        # Check if we have valid screen data (100x100 is our placeholder size)
        # Skip this check since we're not using context anymore
        # if context["screen_size"][0] == 100 and context["screen_size"][1] == 100:
        #     return (
        #         "I'm unable to see your screen at the moment. "
        #         "Please ensure I have screen recording permission in "
        #         "System Preferences → Security & Privacy → Privacy → Screen Recording."
        #     )

        # ALWAYS use Claude Vision if available - no generic responses
        if self.claude_analyzer and test_capture is not None:
            try:
                # Pass PIL Image directly to Claude analyzer (it handles conversion)
                prompt = "Please analyze this screenshot and describe what the user appears to be working on. Be specific about the applications open, the content visible, and any relevant details you can see."
                claude_analysis = await self.claude_analyzer.analyze_screenshot(
                    test_capture, prompt
                )

                # Return ONLY Claude's analysis - no generic fallbacks
                if claude_analysis and claude_analysis.get("description"):
                    return f"Yes sir, I can see your screen. {claude_analysis['description']}"
                else:
                    # If Claude doesn't return a description, ask for more specific analysis
                    return "I'm processing your screen. Please ask me something specific about what you'd like to know."
            except Exception as e:
                logger.error(f"Claude Vision analysis failed: {e}")
                return f"I encountered an error analyzing your screen: {str(e)}. Please ensure Claude Vision is properly configured."
        else:
            # No Claude analyzer available
            if not os.getenv("ANTHROPIC_API_KEY"):
                return "I need an Anthropic API key to analyze your screen. Please configure ANTHROPIC_API_KEY in your environment."
            else:
                return "Claude Vision analyzer is not initialized. Please check the system logs."


# Integration with JARVIS
class JARVISVisionIntegration:
    """Integrate screen vision with JARVIS voice commands"""

    def __init__(self, vision_system: ScreenVisionSystem):
        self.vision = vision_system
        self.monitoring_active = False

    async def handle_vision_command(self, command: str) -> str:
        """Handle vision-related voice commands"""
        command_lower = command.lower()

        # More flexible pattern matching for screen analysis
        # IMPORTANT: Handle monitoring commands FIRST (they should start video capture)
        if any(
            phrase in command_lower
            for phrase in [
                "start monitoring",
                "begin monitoring",
                "monitor my screen",
                "watch my screen continuously",
                "enable monitoring",
                "activate monitoring",
                "start watching my screen",
                "keep watching my screen"
            ]
        ):
            # This should be handled by the monitoring system, not here
            return "Monitoring commands should be handled by the JARVIS voice system for proper video capture activation."
        
        # Then handle regular screen viewing commands
        elif any(
            phrase in command_lower
            for phrase in [
                "can you see my screen",
                "do you see my screen",
                "what's on my screen",
                "what can you see",
                "analyze my screen",
                "see my screen",
                "look at my screen",
                "describe my screen",
                "what do you see",
                "analyze what's on my screen",
            ]
        ):
            description = await self.vision.capture_and_describe()
            # Remove "Sir," prefix since description already starts appropriately
            if (
                "can you see my screen" in command_lower
                or "do you see my screen" in command_lower
            ):
                return description  # Returns "Yes sir, I can see your screen..."
            else:
                return f"Sir, {description}"

        elif (
            "check for updates" in command_lower or "look for updates" in command_lower
        ):
            updates = await self.vision.scan_for_updates()
            if updates:
                urgent = [u for u in updates if u.urgency == "critical"]
                if urgent:
                    return f"Sir, I've detected {len(urgent)} critical updates that require your immediate attention, including {urgent[0].description}"
                else:
                    return f"I've found {len(updates)} available updates. The most recent is {updates[0].description}"
            else:
                return "No software updates detected at this time, sir."

        elif "start monitoring" in command_lower:
            if not self.monitoring_active:
                self.monitoring_active = True
                asyncio.create_task(self._start_monitoring())
                return "I'll start monitoring your screen for updates and notify you of any changes, sir."
            else:
                return "Screen monitoring is already active, sir."

        elif "stop monitoring" in command_lower:
            self.monitoring_active = False
            return "Screen monitoring has been deactivated, sir."

        elif "analyze" in command_lower and "activity" in command_lower:
            context = await self.vision.get_screen_context()
            apps = context["detected_apps"]
            if apps:
                return f"I can see you're working with {', '.join(apps)}. Would you like me to help with any of these applications?"
            else:
                return "I'm analyzing your screen now, sir. Everything appears to be in order."

        return "I'm not sure what you'd like me to look for, sir. I can check for updates, describe what's on your screen, or start monitoring for changes."

    async def _start_monitoring(self):
        """Start continuous monitoring"""

        async def update_callback(updates: List[UpdateNotification]):
            # This would integrate with JARVIS's notification system
            critical = [u for u in updates if u.urgency == "critical"]
            if critical:
                # Speak notification through JARVIS
                print(
                    f"JARVIS: Sir, critical update detected: {critical[0].description}"
                )

        await self.vision.monitor_screen_continuously(update_callback)
