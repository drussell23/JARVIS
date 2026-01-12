#!/usr/bin/env python3
"""Direct Vision Clicker module for simple screenshot-based UI automation.

This module provides a straightforward approach to UI automation by taking screenshots,
using Claude Vision to locate text elements, and clicking on them. It avoids complex
pipelines and focuses on reliability and simplicity.

The main workflow is:
1. Take a screenshot of the current screen
2. Use Claude Vision API to locate specific text
3. Click on the identified coordinates

Example:
    >>> from direct_vision_clicker import get_direct_clicker
    >>> clicker = get_direct_clicker()
    >>> clicker.set_vision_analyzer(vision_analyzer)
    >>> result = await clicker.find_and_click_text("Living Room TV")
    >>> print(result['success'])
    True

Author: Derek Russell
Date: 2025-10-16
Version: 1.0
"""

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from PIL import Image
import pyautogui
import re

logger = logging.getLogger(__name__)


class DirectVisionClicker:
    """A simple vision-based UI automation tool for clicking text elements.
    
    This class provides a dead-simple approach to UI automation by combining
    screenshot capture with Claude Vision analysis to locate and click on
    text elements in the user interface.
    
    The workflow is intentionally straightforward:
    1. Take a screenshot of the entire screen
    2. Use Claude Vision to analyze the screenshot and locate target text
    3. Click on the identified coordinates
    
    Attributes:
        vision_analyzer: Claude Vision analyzer instance for image analysis
        screenshots_dir: Directory path for storing temporary screenshots
    """

    def __init__(self, vision_analyzer=None):
        """Initialize the DirectVisionClicker.

        Args:
            vision_analyzer: Optional Claude Vision analyzer instance. Can be set
                later using set_vision_analyzer().
        """
        self.vision_analyzer = vision_analyzer
        self.screenshots_dir = Path("/tmp/jarvis_vision_clicks")
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        logger.info("[DIRECT CLICKER] Initialized - simple screenshot → click approach")

    def set_vision_analyzer(self, analyzer):
        """Set or update the vision analyzer instance.
        
        Args:
            analyzer: Claude Vision analyzer instance for image analysis.
        """
        self.vision_analyzer = analyzer
        logger.info("[DIRECT CLICKER] Vision analyzer connected")

    async def _take_screenshot(self) -> Optional[Path]:
        """Take a screenshot of the entire screen using macOS screencapture.

        Uses the macOS screencapture utility for reliable screenshot capture.
        Screenshots are saved with timestamp-based filenames to avoid conflicts.

        Returns:
            Path to the saved screenshot file, or None if screenshot failed.
            
        Raises:
            Exception: If screencapture command fails or file creation fails.
        """
        try:
            timestamp = int(time.time() * 1000)
            screenshot_path = self.screenshots_dir / f"screen_{timestamp}.png"

            # Use screencapture for reliability
            process = await asyncio.create_subprocess_exec(
                'screencapture', '-x', str(screenshot_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            await process.communicate()

            if screenshot_path.exists():
                logger.info(f"[DIRECT CLICKER] Screenshot saved: {screenshot_path}")
                return screenshot_path
            else:
                logger.error("[DIRECT CLICKER] Screenshot file not created")
                return None

        except Exception as e:
            logger.error(f"[DIRECT CLICKER] Screenshot failed: {e}")
            return None

    async def _find_text_coordinates(
        self,
        screenshot_path: Path,
        text_to_find: str
    ) -> Optional[Tuple[int, int]]:
        """Use Claude Vision to locate text coordinates in a screenshot.

        Sends the screenshot to Claude Vision with a specific prompt asking for
        the pixel coordinates of the target text. Parses the response to extract
        x,y coordinates.

        Args:
            screenshot_path: Path to the screenshot image file.
            text_to_find: The text string to locate (e.g., "Living Room TV").

        Returns:
            Tuple of (x, y) coordinates representing the center of the found text,
            or None if text was not found or analysis failed.
            
        Raises:
            Exception: If image loading fails or vision analysis encounters an error.

        Example:
            >>> coords = await clicker._find_text_coordinates(
            ...     Path("/tmp/screen.png"), "Settings"
            ... )
            >>> print(coords)
            (450, 125)
        """
        if not self.vision_analyzer:
            logger.error("[DIRECT CLICKER] No vision analyzer available")
            return None

        try:
            # Load screenshot
            image = Image.open(screenshot_path)
            width, height = image.size

            logger.info(f"[DIRECT CLICKER] Asking Claude to find '{text_to_find}'...")
            logger.info(f"[DIRECT CLICKER] Screenshot size: {width}x{height}")

            # Ask Claude Vision where the text is
            prompt = f"""Look at this screenshot and find the text "{text_to_find}".

IMPORTANT: Return ONLY the coordinates in this EXACT format:
COORDINATES: x=<number>, y=<number>

Where:
- x is the horizontal pixel position from the left edge
- y is the vertical pixel position from the top edge
- Target the CENTER of the text "{text_to_find}"

Example response:
COORDINATES: x=450, y=125

If you cannot find "{text_to_find}", respond with:
NOT_FOUND

Do not include any other text or explanation."""

            # Analyze with Claude Vision
            result = await self.vision_analyzer.analyze_screenshot(
                image=image,
                prompt=prompt,
                use_cache=False
            )

            # Extract response
            if isinstance(result, tuple):
                analysis, metrics = result
                response_text = analysis.get('analysis', '')
            else:
                response_text = result.get('analysis', '')

            logger.info(f"[DIRECT CLICKER] Claude response: {response_text}")

            # Check for NOT_FOUND
            if "NOT_FOUND" in response_text:
                logger.warning(f"[DIRECT CLICKER] Claude could not find '{text_to_find}'")
                return None

            # Parse coordinates from response
            # Look for pattern: COORDINATES: x=123, y=456
            coord_match = re.search(r'x[=:]\s*(\d+).*?y[=:]\s*(\d+)', response_text, re.IGNORECASE)

            if coord_match:
                x = int(coord_match.group(1))
                y = int(coord_match.group(2))

                logger.info(f"[DIRECT CLICKER] ✅ Found '{text_to_find}' at ({x}, {y})")

                # Validate coordinates are within screen bounds
                if 0 <= x <= width and 0 <= y <= height:
                    return (x, y)
                else:
                    logger.warning(f"[DIRECT CLICKER] Coordinates out of bounds: ({x}, {y})")
                    return None
            else:
                logger.warning(f"[DIRECT CLICKER] Could not parse coordinates from: {response_text}")
                return None

        except Exception as e:
            logger.error(f"[DIRECT CLICKER] Vision analysis failed: {e}", exc_info=True)
            return None

    def _click_coordinates(self, x: int, y: int):
        """Click at the specified screen coordinates.

        Handles the conversion from physical screenshot pixels to logical screen
        coordinates that PyAutoGUI expects. On Retina displays, screenshots are
        captured at physical resolution (e.g., 2880x1800) but clicks must be
        performed at logical resolution (e.g., 1440x900).

        Args:
            x: X coordinate in physical pixels from screenshot.
            y: Y coordinate in physical pixels from screenshot.
            
        Raises:
            Exception: If click operation fails or coordinate conversion fails.

        Example:
            >>> clicker._click_coordinates(450, 125)
            # Clicks at the logical screen position corresponding to physical pixels (450, 125)
        """
        try:
            logger.info(f"[DIRECT CLICKER] Received coordinates: ({x}, {y}) (physical pixels)")

            # CRITICAL FIX: Screenshots are captured at physical pixel resolution (e.g., 2880x1800 on Retina)
            # but PyAutoGUI works in logical pixels (e.g., 1440x900 on Retina).
            # We MUST convert from physical to logical pixels!

            try:
                from AppKit import NSScreen
                main_screen = NSScreen.mainScreen()
                dpi_scale = main_screen.backingScaleFactor()
            except Exception:
                dpi_scale = 1.0
                logger.warning("[DIRECT CLICKER] Could not detect DPI scale, assuming 1.0x")

            # Convert from physical to logical pixels
            logical_x = int(round(x / dpi_scale))
            logical_y = int(round(y / dpi_scale))

            logger.info(
                f"[DIRECT CLICKER] DPI correction: "
                f"Physical ({x}, {y}) -> Logical ({logical_x}, {logical_y}) [scale={dpi_scale}x]"
            )

            logger.info(f"[DIRECT CLICKER] Clicking at ({logical_x}, {logical_y}) (logical pixels)")

            # Move mouse and click using LOGICAL pixels
            pyautogui.moveTo(logical_x, logical_y, duration=0.3)
            time.sleep(0.1)
            pyautogui.click()

            logger.info(f"[DIRECT CLICKER] ✅ Click executed at ({logical_x}, {logical_y})")

        except Exception as e:
            logger.error(f"[DIRECT CLICKER] Click failed: {e}")
            raise

    async def find_and_click_text(
        self,
        text_to_find: str,
        max_retries: int = 2
    ) -> Dict[str, Any]:
        """Find and click on text in the current screen display.

        This is the main public method that orchestrates the complete workflow:
        taking a screenshot, analyzing it with Claude Vision to find the target
        text, and clicking on the identified coordinates.

        Args:
            text_to_find: The text string to locate and click (e.g., "Living Room TV").
            max_retries: Maximum number of retry attempts if the first attempt fails.
                Defaults to 2.

        Returns:
            Dictionary containing operation results with the following keys:
            - success (bool): Whether the operation succeeded
            - message (str): Human-readable description of the result
            - coordinates (dict): x,y coordinates where click occurred (if successful)
            - duration (float): Total time taken for the operation
            - attempts (int): Number of attempts made
            - method (str): Always "direct_vision_click"
            - error (str): Error message (if unsuccessful)

        Example:
            >>> result = await clicker.find_and_click_text("Settings")
            >>> if result['success']:
            ...     print(f"Clicked at {result['coordinates']}")
            ... else:
            ...     print(f"Failed: {result['message']}")
        """
        start_time = time.time()

        logger.info(f"[DIRECT CLICKER] ========================================")
        logger.info(f"[DIRECT CLICKER] Finding and clicking: '{text_to_find}'")
        logger.info(f"[DIRECT CLICKER] Method: Screenshot → Claude Vision → Click")
        logger.info(f"[DIRECT CLICKER] ========================================")

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    logger.info(f"[DIRECT CLICKER] Retry attempt {attempt}/{max_retries}")
                    await asyncio.sleep(1.0)  # Brief delay between retries

                # Step 1: Take screenshot
                screenshot_path = await self._take_screenshot()
                if not screenshot_path:
                    continue

                # Step 2: Find text coordinates using Claude Vision
                coordinates = await self._find_text_coordinates(screenshot_path, text_to_find)
                if not coordinates:
                    logger.warning(f"[DIRECT CLICKER] Could not locate '{text_to_find}' (attempt {attempt + 1})")
                    continue

                x, y = coordinates

                # Step 3: Click the coordinates
                self._click_coordinates(x, y)

                duration = time.time() - start_time

                logger.info(f"[DIRECT CLICKER] ✅ SUCCESS in {duration:.2f}s")
                logger.info(f"[DIRECT CLICKER] Found and clicked '{text_to_find}' at ({x}, {y})")
                logger.info(f"[DIRECT CLICKER] ========================================")

                return {
                    "success": True,
                    "message": f"Clicked '{text_to_find}' at ({x}, {y})",
                    "coordinates": {"x": x, "y": y},
                    "duration": duration,
                    "attempts": attempt + 1,
                    "method": "direct_vision_click"
                }

            except Exception as e:
                logger.error(f"[DIRECT CLICKER] Attempt {attempt + 1} failed: {e}")
                if attempt == max_retries:
                    duration = time.time() - start_time
                    return {
                        "success": False,
                        "message": f"Failed to find or click '{text_to_find}' after {max_retries + 1} attempts",
                        "error": str(e),
                        "duration": duration,
                        "attempts": attempt + 1
                    }

        duration = time.time() - start_time
        return {
            "success": False,
            "message": f"Could not locate '{text_to_find}' in {max_retries + 1} attempts",
            "duration": duration,
            "attempts": max_retries + 1
        }


# Singleton instance
_direct_clicker: Optional[DirectVisionClicker] = None


def get_direct_clicker() -> DirectVisionClicker:
    """Get the singleton DirectVisionClicker instance.
    
    Creates a new instance on first call, then returns the same instance
    on subsequent calls. This ensures consistent state across the application.
    
    Returns:
        DirectVisionClicker: The singleton clicker instance.
        
    Example:
        >>> clicker = get_direct_clicker()
        >>> clicker.set_vision_analyzer(my_analyzer)
        >>> # Later in the code...
        >>> same_clicker = get_direct_clicker()  # Returns same instance
    """
    global _direct_clicker
    if _direct_clicker is None:
        _direct_clicker = DirectVisionClicker()
    return _direct_clicker


if __name__ == "__main__":
    # Test the direct clicker
    async def test():
        """Test function to demonstrate DirectVisionClicker usage.
        
        This function shows how to initialize and use the DirectVisionClicker
        in a real application. Note that a vision analyzer must be connected
        for actual functionality.
        """
        logging.basicConfig(level=logging.INFO)

        clicker = get_direct_clicker()

        # Note: In real usage, you'd connect a vision analyzer here
        # clicker.set_vision_analyzer(your_vision_analyzer)

        print("\n=== Testing Direct Vision Clicker ===\n")
        print("This would find and click 'Living Room TV' if vision analyzer was connected")

        # Simulate the API
        result = await clicker.find_and_click_text("Living Room TV")
        print(f"\nResult: {result}")

    asyncio.run(test())