#!/usr/bin/env python3
"""
Simple Control Center clicker - no vision needed
Just clicks the verified coordinates (1245, 12)
"""

import pyautogui
import time
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ControlCenterClicker:
    """Click Control Center icon at verified coordinates"""

    # Verified Control Center coordinates for 1440x900 screen
    CONTROL_CENTER_X = 1245
    CONTROL_CENTER_Y = 12

    def __init__(self):
        self.logger = logger

    def open_control_center(self, wait_after_click: float = 0.5) -> Dict[str, Any]:
        """
        Open Control Center by clicking at verified coordinates

        Args:
            wait_after_click: Seconds to wait after clicking (for menu to open)

        Returns:
            Dict with success status and message
        """
        try:
            self.logger.info(f"Opening Control Center at ({self.CONTROL_CENTER_X}, {self.CONTROL_CENTER_Y})")

            # Move to Control Center icon
            pyautogui.moveTo(self.CONTROL_CENTER_X, self.CONTROL_CENTER_Y, duration=0.3)

            # Click it
            pyautogui.click(self.CONTROL_CENTER_X, self.CONTROL_CENTER_Y)

            # Wait for menu to open
            time.sleep(wait_after_click)

            self.logger.info("✓ Control Center opened")
            return {
                "success": True,
                "message": "Control Center opened",
                "coordinates": (self.CONTROL_CENTER_X, self.CONTROL_CENTER_Y)
            }

        except Exception as e:
            self.logger.error(f"Failed to open Control Center: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Failed to open Control Center: {str(e)}",
                "error": str(e)
            }

    def close_control_center(self) -> Dict[str, Any]:
        """
        Close Control Center menu by pressing ESC

        Returns:
            Dict with success status and message
        """
        try:
            self.logger.info("Closing Control Center menu")
            pyautogui.press('escape')
            time.sleep(0.3)

            self.logger.info("✓ Control Center closed")
            return {
                "success": True,
                "message": "Control Center closed"
            }

        except Exception as e:
            self.logger.error(f"Failed to close Control Center: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Failed to close Control Center: {str(e)}",
                "error": str(e)
            }

    def click_control_center_icon(self, wait_for_menu: float = 0.5) -> Dict[str, Any]:
        """
        Convenience method: Open Control Center and return result

        Args:
            wait_for_menu: Seconds to wait for menu to open

        Returns:
            Dict with success status
        """
        return self.open_control_center(wait_after_click=wait_for_menu)


# Singleton instance
_control_center_clicker = None

def get_control_center_clicker() -> ControlCenterClicker:
    """Get singleton Control Center clicker instance"""
    global _control_center_clicker
    if _control_center_clicker is None:
        _control_center_clicker = ControlCenterClicker()
    return _control_center_clicker


def test_control_center_clicker():
    """Test the Control Center clicker"""
    clicker = get_control_center_clicker()

    print("Testing Control Center Clicker\n")
    print("=" * 50)

    # Test opening Control Center
    print("\n1. Opening Control Center...")
    result = clicker.open_control_center()
    print(f"Result: {result}")

    if result["success"]:
        print("\n2. Waiting 3 seconds...")
        time.sleep(3)

        print("\n3. Closing Control Center...")
        result = clicker.close_control_center()
        print(f"Result: {result}")

    print("\n" + "=" * 50)
    print("Test complete!")


if __name__ == "__main__":
    test_control_center_clicker()
