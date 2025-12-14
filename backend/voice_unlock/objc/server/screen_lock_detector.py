#!/usr/bin/env python3
"""
Screen Lock Detection Module
============================

Provides reliable, asynchronous screen lock detection for macOS
"""

import asyncio
import logging
import subprocess
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


async def async_is_screen_locked() -> bool:
    """
    Check if the macOS screen is currently locked (Async)
    
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
        # Create subprocess
        proc = await asyncio.create_subprocess_shell(
            check_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0:
                is_locked = stdout.decode().strip().lower() == 'true'
                if is_locked:
                    logger.debug("Screen locked detected via CGSession")
                    return True
        except asyncio.TimeoutError:
            logger.warning("CGSession check timed out")
            try:
                proc.kill()
            except:
                pass

        # Method 2: Check if screensaver is active via sysadminctl (faster)
        sysadmin_cmd = ["/usr/sbin/sysadminctl", "-screenLock", "status"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *sysadmin_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1.0)
            
            if proc.returncode == 0 and "locked" in stderr.decode().lower():
                logger.debug("Screen locked detected via sysadminctl")
                return True
        except Exception:
            pass

        # Method 2b: Screensaver via AppleScript (fallback)
        screensaver_cmd = """osascript -e 'tell application "System Events" to get running of screen saver'"""
        try:
            proc = await asyncio.create_subprocess_shell(
                screensaver_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            
            if proc.returncode == 0 and stdout.decode().strip().lower() == 'true':
                logger.debug("Screen locked detected via screensaver")
                return True
        except asyncio.TimeoutError:
            logger.warning("Screensaver check timed out")
            try:
                proc.kill()
            except:
                pass
            
        # Method 3: Check security session state (LoginWindow)
        loginwindow_cmd = """osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'"""
        try:
            proc = await asyncio.create_subprocess_shell(
                loginwindow_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            
            if proc.returncode == 0:
                front_app = stdout.decode().strip().lower()
                if "loginwindow" in front_app:
                    logger.debug("Screen locked detected via loginwindow")
                    return True
        except asyncio.TimeoutError:
            logger.warning("LoginWindow check timed out")
            try:
                proc.kill()
            except:
                pass
        
        # If none of the checks indicate locked, screen is unlocked
        return False
        
    except Exception as e:
        logger.error(f"Error checking screen lock status: {e}")
        # Conservative approach: assume unlocked on error to prevent being stuck
        return False


def is_screen_locked() -> bool:
    """
    Synchronous wrapper for backward compatibility.
    WARNING: This blocks! Prefer async_is_screen_locked().
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If we're already in a loop, we can't block waiting for async
            # This is a dangerous fallback, but better than crashing
            # Ideally callers should be updated to use async_is_screen_locked
            logger.warning("is_screen_locked called from running loop - forcing async via specialized runner not possible, returning False to avoid block")
            return False
            
        return loop.run_until_complete(async_is_screen_locked())
    except Exception:
        # Fallback to creating a new loop if none exists
        return asyncio.run(async_is_screen_locked())


async def get_screen_state_details() -> Dict[str, Any]:
    """
    Get detailed screen state information (Async)
    
    Returns:
        dict: Detailed state including lock status and method
    """
    details = {
        "isLocked": False,
        "detectionMethod": None,
        "screensaverActive": False,
        "loginWindowActive": False,
        "sessionLocked": False,
        "error": None
    }
    
    try:
        # CGSession check
        session_cmd = """python3 -c "
import Quartz
session_dict = Quartz.CGSessionCopyCurrentDictionary()
if session_dict:
    locked = session_dict.get('CGSSessionScreenIsLocked', False)
    print(locked)
"
"""
        proc = await asyncio.create_subprocess_shell(
            session_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0:
                details["sessionLocked"] = stdout.decode().strip().lower() == 'true'
        except asyncio.TimeoutError:
            details["error"] = "CGSession check timed out"

        # Screensaver check
        screensaver_cmd = """osascript -e 'tell application "System Events" to get running of screen saver'"""
        proc = await asyncio.create_subprocess_shell(
            screensaver_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0:
                details["screensaverActive"] = stdout.decode().strip().lower() == 'true'
        except asyncio.TimeoutError:
            pass
        
        # Login window check
        loginwindow_cmd = """osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'"""
        proc = await asyncio.create_subprocess_shell(
            loginwindow_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0:
                details["loginWindowActive"] = "loginwindow" in stdout.decode().strip().lower()
        except asyncio.TimeoutError:
            pass
        
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
        details["error"] = str(e)
        
    return details


if __name__ == "__main__":
    # Test the detection
    print("Testing screen lock detection...")
    
    # Run async test
    is_locked = asyncio.run(async_is_screen_locked())
    print(f"\nScreen is {'LOCKED' if is_locked else 'UNLOCKED'}")
    
    details = asyncio.run(get_screen_state_details())
    print("\nDetailed state:")
    for key, value in details.items():
        print(f"  {key}: {value}")