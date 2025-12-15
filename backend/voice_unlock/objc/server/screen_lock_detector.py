#!/usr/bin/env python3
"""
Screen Lock Detection Module
============================

Provides reliable screen lock detection for macOS
"""

import subprocess
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def is_screen_locked() -> bool:
    """
    Check if the macOS screen is currently locked
    
    Uses multiple detection methods for reliability:
    1. CGSessionCopyCurrentDictionary check
    2. Screensaver status
    3. Security session state
    
    Returns:
        bool: True if screen is locked, False otherwise
    """
    try:
        # Method 1: Check CGSession dictionary for lock state
        check_cmd = """python3 -c "
import Quartz
session_dict = Quartz.CGSessionCopyCurrentDictionary()
if session_dict:
    # Check multiple indicators
    locked = session_dict.get('CGSSessionScreenIsLocked', False)
    screensaver = session_dict.get('CGSSessionScreenLockedTime', 0) > 0
    print(locked or screensaver)
else:
    print(False)
"
"""
        result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            is_locked = result.stdout.strip().lower() == 'true'
            if is_locked:
                logger.debug("Screen locked detected via CGSession")
                return True
        
        # Method 2: Check if screensaver is active
        screensaver_cmd = """osascript -e 'tell application "System Events" to get running of screen saver'"""
        result = subprocess.run(screensaver_cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0 and result.stdout.strip().lower() == 'true':
            logger.debug("Screen locked detected via screensaver")
            return True
            
        # Method 3: Check security session state
        # This checks if we're at the login window
        loginwindow_cmd = """osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'"""
        result = subprocess.run(loginwindow_cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            front_app = result.stdout.strip().lower()
            if "loginwindow" in front_app:
                logger.debug("Screen locked detected via loginwindow")
                return True
        
        # If none of the checks indicate locked, screen is unlocked
        return False
        
    except Exception as e:
        logger.error(f"Error checking screen lock status: {e}")
        # Conservative approach: assume unlocked on error
        return False


def get_screen_state_details() -> Dict[str, Any]:
    """
    Get detailed screen state information
    
    Returns:
        dict: Detailed state including lock status and method
    """
    details = {
        "isLocked": False,
        "detectionMethod": None,
        "screensaverActive": False,
        "loginWindowActive": False,
        "sessionLocked": False
    }
    
    try:
        # Check each method
        
        # CGSession check
        session_cmd = """python3 -c "
import Quartz
session_dict = Quartz.CGSessionCopyCurrentDictionary()
if session_dict:
    locked = session_dict.get('CGSSessionScreenIsLocked', False)
    print(locked)
"
"""
        result = subprocess.run(session_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            details["sessionLocked"] = result.stdout.strip().lower() == 'true'
        
        # Screensaver check
        screensaver_cmd = """osascript -e 'tell application "System Events" to get running of screen saver'"""
        result = subprocess.run(screensaver_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            details["screensaverActive"] = result.stdout.strip().lower() == 'true'
        
        # Login window check
        loginwindow_cmd = """osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'"""
        result = subprocess.run(loginwindow_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            details["loginWindowActive"] = "loginwindow" in result.stdout.strip().lower()
        
        # Determine overall lock state
        if details["sessionLocked"]:
            details["isLocked"] = True
            details["detectionMethod"] = "CGSession"
        elif details["screensaverActive"]:
            details["isLocked"] = True
            details["detectionMethod"] = "Screensaver"
        elif details["loginWindowActive"]:
            details["isLocked"] = True
            details["detectionMethod"] = "LoginWindow"
        else:
            details["detectionMethod"] = "None"
            
    except Exception as e:
        logger.error(f"Error getting screen state details: {e}")
        
    return details


if __name__ == "__main__":
    # Test the detection
    print("Testing screen lock detection...")
    
    is_locked = is_screen_locked()
    print(f"\nScreen is {'LOCKED' if is_locked else 'UNLOCKED'}")
    
    details = get_screen_state_details()
    print("\nDetailed state:")
    for key, value in details.items():
        print(f"  {key}: {value}")