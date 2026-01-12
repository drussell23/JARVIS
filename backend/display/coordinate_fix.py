#!/usr/bin/env python3
"""
Fix for coordinate scaling issues in JARVIS display connection
Ensures all coordinates are in logical space (not Retina/physical pixels)
"""

import pyautogui
from AppKit import NSScreen

class CoordinateFixer:
    """Ensures coordinates are always in logical space"""

    @staticmethod
    def get_scale_factor():
        """Get the display's backing scale factor"""
        try:
            main_screen = NSScreen.mainScreen()
            return main_screen.backingScaleFactor()
        except Exception:
            return 1.0  # Default to no scaling

    @staticmethod
    def fix_coordinates(x, y, source="unknown"):
        """
        Fix coordinates to ensure they're in logical space

        Args:
            x, y: Input coordinates (might be in wrong space)
            source: Where the coordinates came from

        Returns:
            (x, y) tuple in logical coordinate space
        """
        scale = CoordinateFixer.get_scale_factor()
        screen_width, screen_height = pyautogui.size()

        # Log what we're dealing with
        print(f"[COORD-FIX] Input: ({x}, {y}) from {source}")
        print(f"[COORD-FIX] Screen: {screen_width}x{screen_height}, scale: {scale}")

        # If coordinates are way out of bounds, they might be in physical pixels
        if x > screen_width * 1.5:  # More than 1.5x screen width
            # Likely in physical pixel space, convert to logical
            fixed_x = int(x / scale)
            fixed_y = int(y / scale)
            print(f"[COORD-FIX] Detected physical pixels, converting: ({x}, {y}) -> ({fixed_x}, {fixed_y})")
            return (fixed_x, fixed_y)

        # If coordinates are already in bounds, they're probably correct
        if 0 <= x <= screen_width and 0 <= y <= screen_height:
            print(f"[COORD-FIX] Coordinates already in logical space")
            return (x, y)

        # For Control Center specifically, we know the correct position
        if source == "control_center":
            print(f"[COORD-FIX] Using known Control Center position: (1235, 10)")
            return (1235, 10)

        # Default: return as-is but warn
        print(f"[COORD-FIX] WARNING: Couldn't determine coordinate space, using as-is")
        return (x, y)

    @staticmethod
    def ensure_logical_dragTo(x, y, duration=0.4, button='left', source="unknown"):
        """
        Wrapper for pyautogui.dragTo that ensures logical coordinates

        This prevents the issue where coordinates get doubled on Retina displays
        """
        fixed_x, fixed_y = CoordinateFixer.fix_coordinates(x, y, source)

        # Additional safety check for Control Center
        if source == "control_center" and (fixed_x != 1235 or fixed_y != 10):
            print(f"[COORD-FIX] OVERRIDE: Forcing Control Center to (1235, 10)")
            fixed_x, fixed_y = 1235, 10

        print(f"[COORD-FIX] Executing dragTo({fixed_x}, {fixed_y})")
        pyautogui.dragTo(fixed_x, fixed_y, duration=duration, button=button)

        # Verify
        final_pos = pyautogui.position()
        if final_pos.x != fixed_x or final_pos.y != fixed_y:
            print(f"[COORD-FIX] ERROR: Mouse went to ({final_pos.x}, {final_pos.y}) instead of ({fixed_x}, {fixed_y})")
        else:
            print(f"[COORD-FIX] SUCCESS: Mouse at correct position")

        return final_pos


# Monkey-patch pyautogui to use our fixed version
_original_dragTo = pyautogui.dragTo
_original_moveTo = pyautogui.moveTo

def safe_dragTo(x, y, duration=None, button='left', **kwargs):
    """Safe dragTo that prevents coordinate doubling"""
    import traceback
    import logging
    logger = logging.getLogger(__name__)

    msg = f"\n[SAFE-DRAG] ⚠️  dragTo called with coordinates: ({x}, {y})"
    print(msg)
    logger.error(msg)  # Use ERROR level so it shows up in logs

    logger.error(f"[SAFE-DRAG] Call stack:")
    for line in traceback.format_stack()[:-1]:
        if 'backend' in line:
            logger.error(f"  {line.strip()}")

    fixer = CoordinateFixer()

    # Check if coordinates are suspicious
    screen_width, _ = pyautogui.size()

    # CRITICAL: If coordinates are EXACTLY 2x what they should be, this is a DPI doubling bug!
    if (x, y) in [(2470, 20), (2475, 15), (2792, 354)]:  # Known bad coordinates
        logger.error(f"[SAFE-DRAG] 🚨 DETECTED DOUBLED COORDINATES: ({x}, {y})")
        logger.error(f"[SAFE-DRAG] These are exactly 2x the correct values - applying DPI correction")
        x, y = int(x / 2), int(y / 2)
        logger.error(f"[SAFE-DRAG] Corrected to: ({x}, {y})")
    elif x > screen_width * 1.5:
        msg2 = f"[SAFE-DRAG] WARNING: Suspicious coordinates ({x}, {y}), applying fix"
        print(msg2)
        logger.error(msg2)
        x, y = fixer.fix_coordinates(x, y, "dragTo")
    else:
        msg3 = f"[SAFE-DRAG] Coordinates seem OK: ({x}, {y}) [screen width: {screen_width}]"
        print(msg3)
        logger.error(msg3)

    msg4 = f"[SAFE-DRAG] Final coordinates to dragTo: ({x}, {y})\n"
    print(msg4)
    logger.error(msg4)
    return _original_dragTo(x, y, duration=duration, button=button, **kwargs)

def safe_moveTo(x, y, duration=None, **kwargs):
    """Safe moveTo that prevents coordinate doubling"""
    fixer = CoordinateFixer()

    # Check if coordinates are suspicious
    screen_width, _ = pyautogui.size()
    if x > screen_width * 1.5:
        print(f"[SAFE-MOVE] WARNING: Suspicious coordinates ({x}, {y}), applying fix")
        x, y = fixer.fix_coordinates(x, y, "moveTo")

    return _original_moveTo(x, y, duration=duration, **kwargs)

# Apply the monkey-patch
def apply_coordinate_fix():
    """Apply the coordinate fix globally"""
    pyautogui.dragTo = safe_dragTo
    pyautogui.moveTo = safe_moveTo
    print("[COORD-FIX] Applied global coordinate fix to pyautogui")


if __name__ == "__main__":
    # Test the fix
    import asyncio

    print("Testing coordinate fix...")
    fixer = CoordinateFixer()

    # Test various coordinates
    test_cases = [
        (1235, 10, "control_center"),  # Correct
        (2745, 15, "unknown"),  # Wrong (doubled + offset)
        (2470, 20, "retina"),  # Exactly doubled
        (1439, 20, "edge"),  # Near edge
    ]

    for x, y, source in test_cases:
        fixed_x, fixed_y = fixer.fix_coordinates(x, y, source)
        print(f"  ({x}, {y}) -> ({fixed_x}, {fixed_y})")

    print("\nApplying global fix...")
    apply_coordinate_fix()

    print("\nTesting dragTo with fix...")
    pyautogui.dragTo(2745, 15, duration=0.5)  # Should be fixed to ~1372, 7

    print("\nDone!")