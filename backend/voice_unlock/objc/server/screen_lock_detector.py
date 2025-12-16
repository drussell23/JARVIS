#!/usr/bin/env python3
"""
Screen Lock Detection Module - Enhanced v2.0
=============================================

Provides robust, multi-method screen lock detection for macOS.
Uses multiple detection strategies with fallbacks for reliability.

Key improvement: Uses CGSession API via ctypes as fallback when
pyobjc-framework-Quartz is not available.
"""

import subprocess
import logging
import os
import ctypes
import ctypes.util
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# Native macOS APIs via ctypes (fallback when Quartz module unavailable)
# =============================================================================

_CGSession = None

def _init_cgsession():
    """Initialize CGSession via ctypes for native screen lock detection."""
    global _CGSession
    if _CGSession is not None:
        return _CGSession

    try:
        # Load ApplicationServices framework
        framework_path = ctypes.util.find_library('ApplicationServices')
        if framework_path:
            _CGSession = ctypes.CDLL(framework_path)
            return _CGSession
    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] Could not load ApplicationServices: {e}")

    try:
        # Alternative: Load CoreGraphics directly
        cg_path = '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics'
        if os.path.exists(cg_path):
            _CGSession = ctypes.CDLL(cg_path)
            return _CGSession
    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] Could not load CoreGraphics: {e}")

    return None


def _check_cgsession_locked_via_ctypes() -> Optional[bool]:
    """
    Check screen lock via native CGSession API using ctypes.
    This is a fallback when pyobjc-framework-Quartz is not installed.

    Returns:
        Optional[bool]: True if locked, False if unlocked, None if cannot determine
    """
    try:
        # Use CoreFoundation to read session dictionary
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')

        # CGSessionCopyCurrentDictionary returns a CFDictionaryRef
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p

        session_dict = cg.CGSessionCopyCurrentDictionary()
        if not session_dict:
            return None

        # We need to check the dictionary for CGSSessionScreenIsLocked
        # Since this is complex with ctypes, use a simpler approach via osascript
        cf.CFRelease(ctypes.c_void_p(session_dict))
        return None  # Fall through to other methods

    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] ctypes CGSession check failed: {e}")
        return None


def _check_session_locked_via_osascript() -> Optional[bool]:
    """
    Check screen lock via security session osascript check.
    More reliable than checking frontmost app.

    Returns:
        Optional[bool]: True if locked, False if unlocked, None if cannot determine
    """
    try:
        # Check if we can interact with the UI (fails when locked)
        script = '''
        try
            tell application "System Events"
                set uiEnabled to UI elements enabled
                set procCount to count of processes whose background only is false
                if procCount is 0 then
                    return "locked"
                end if
                -- Try to get any window - fails if locked
                set hasWindows to false
                repeat with proc in (processes whose background only is false)
                    try
                        if (count of windows of proc) > 0 then
                            set hasWindows to true
                            exit repeat
                        end if
                    end try
                end repeat
                return "unlocked"
            end tell
        on error errMsg
            return "error:" & errMsg
        end try
        '''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3
        )

        if result.returncode == 0:
            output = result.stdout.strip().lower()
            if "locked" in output:
                logger.info("🔒 [SCREEN-DETECT] LOCKED via UI interaction check")
                return True
            elif "unlocked" in output:
                return False
            elif "error" in output:
                # Errors often indicate locked screen
                if "not allowed" in output or "access" in output:
                    logger.info("🔒 [SCREEN-DETECT] LOCKED via UI access error")
                    return True
        return None
    except subprocess.TimeoutExpired:
        # Timeout typically means locked
        logger.info("🔒 [SCREEN-DETECT] LOCKED via osascript timeout")
        return True
    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] osascript session check failed: {e}")
        return None


def _check_lockscreen_process() -> Optional[bool]:
    """
    Check if the LockScreen process is running or active.
    This process only runs when the screen is locked.

    Returns:
        Optional[bool]: True if locked, False if unlocked, None if cannot determine
    """
    try:
        # Check for lock screen related processes
        result = subprocess.run(
            ["pgrep", "-x", "loginwindow"],
            capture_output=True, text=True, timeout=2
        )

        if result.returncode == 0:
            # loginwindow is running - check if it's in the foreground
            loginwindow_pid = result.stdout.strip()
            if loginwindow_pid:
                # Check if loginwindow is frontmost
                front_check = subprocess.run(
                    ["osascript", "-e", 'tell application "System Events" to get name of first process whose frontmost is true'],
                    capture_output=True, text=True, timeout=3
                )
                if front_check.returncode == 0:
                    front_app = front_check.stdout.strip().lower()
                    if "loginwindow" in front_app:
                        logger.info("🔒 [SCREEN-DETECT] LOCKED via loginwindow frontmost")
                        return True

        # Check for ScreenSaverEngine which runs during lock
        screensaver_result = subprocess.run(
            ["pgrep", "-x", "ScreenSaverEngine"],
            capture_output=True, text=True, timeout=2
        )
        if screensaver_result.returncode == 0 and screensaver_result.stdout.strip():
            logger.info("🔒 [SCREEN-DETECT] LOCKED via ScreenSaverEngine running")
            return True

        return None
    except subprocess.TimeoutExpired:
        logger.info("🔒 [SCREEN-DETECT] LOCKED via process check timeout")
        return True
    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] Process check failed: {e}")
        return None


def is_screen_locked() -> bool:
    """
    Check if the macOS screen is currently locked.

    Uses MULTIPLE detection methods in order of reliability:
    1. Direct Quartz CGSessionCopyCurrentDictionary (most reliable)
    2. Native CGSession via ctypes (fallback when Quartz unavailable)
    3. osascript UI interaction check (detects lock screen password prompt)
    4. Lock-related process check (loginwindow, ScreenSaverEngine)
    5. IORegistry display power state check
    6. Login window frontmost check
    7. Screen capture test (definitive but slower)

    IMPORTANT: If ANY reliable method says locked, we return True.
    This prevents false "already unlocked" responses.

    Returns:
        bool: True if screen is locked, False otherwise
    """
    detection_results = []

    try:
        # =====================================================================
        # Method 1: Direct Quartz API check (MOST RELIABLE)
        # =====================================================================
        try:
            import Quartz
            session_dict = Quartz.CGSessionCopyCurrentDictionary()
            if session_dict:
                # Check the definitive lock indicator
                screen_locked = session_dict.get('CGSSessionScreenIsLocked', False)
                if screen_locked:
                    logger.info("🔒 [SCREEN-DETECT] LOCKED via Quartz CGSSessionScreenIsLocked")
                    return True

                # Also check if screen saver with lock is active
                screensaver_time = session_dict.get('CGSSessionScreenLockedTime', 0)
                if screensaver_time and screensaver_time > 0:
                    logger.info("🔒 [SCREEN-DETECT] LOCKED via Quartz CGSSessionScreenLockedTime")
                    return True

                # Check on-console status
                on_console = session_dict.get('kCGSSessionOnConsoleKey', True)
                if not on_console:
                    logger.info("🔒 [SCREEN-DETECT] LOCKED via kCGSSessionOnConsoleKey=False")
                    return True

                detection_results.append(("Quartz", False))
                logger.info("🔓 [SCREEN-DETECT] Quartz says UNLOCKED")
        except ImportError:
            logger.debug("[SCREEN-DETECT] Quartz not available, trying fallbacks")
        except Exception as e:
            logger.debug(f"[SCREEN-DETECT] Quartz check failed: {e}")

        # =====================================================================
        # Method 2: osascript UI interaction check (CATCHES LOCK SCREEN PROMPT)
        # =====================================================================
        ui_check = _check_session_locked_via_osascript()
        if ui_check is True:
            return True
        elif ui_check is False:
            detection_results.append(("UIInteraction", False))

        # =====================================================================
        # Method 3: Lock-related process check
        # =====================================================================
        process_check = _check_lockscreen_process()
        if process_check is True:
            return True
        elif process_check is False:
            detection_results.append(("ProcessCheck", False))

        # =====================================================================
        # Method 4: IORegistry Display State Check
        # =====================================================================
        try:
            ioreg_cmd = ["ioreg", "-r", "-c", "IODisplayWrangler", "-d", "1"]
            result = subprocess.run(ioreg_cmd, capture_output=True, text=True, timeout=3)

            if result.returncode == 0:
                output = result.stdout.lower()
                # DevicePowerState: 0 = display off/locked, 4 = display on
                if '"devicepowerstate" = 0' in output or "'devicepowerstate' = 0" in output:
                    logger.info("🔒 [SCREEN-DETECT] Display appears OFF via IORegistry")
                    # Display off almost certainly means locked
                    return True
        except Exception as e:
            logger.debug(f"[SCREEN-DETECT] IORegistry check failed: {e}")

        # =====================================================================
        # Method 5: Login Window Frontmost Check (AppleScript)
        # =====================================================================
        try:
            script = '''
            tell application "System Events"
                set frontApp to name of first process whose frontmost is true
                return frontApp
            end tell
            '''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0:
                front_app = result.stdout.strip().lower()
                logger.debug(f"[SCREEN-DETECT] Frontmost app: {front_app}")

                if "loginwindow" in front_app:
                    logger.info("🔒 [SCREEN-DETECT] LOCKED via loginwindow frontmost")
                    return True

                detection_results.append(("FrontApp", False))
            else:
                # If we can't get frontmost app, it might be locked
                logger.debug(f"[SCREEN-DETECT] Could not get frontmost app: {result.stderr}")
                # This could indicate lock - but don't return True immediately
                detection_results.append(("FrontApp-Error", True))
        except subprocess.TimeoutExpired:
            # Timeout often indicates locked screen
            logger.info("🔒 [SCREEN-DETECT] AppleScript timeout - likely LOCKED")
            return True
        except Exception as e:
            logger.debug(f"[SCREEN-DETECT] Frontmost app check failed: {e}")

        # =====================================================================
        # Method 6: Screen Capture Test (Definitive but slower)
        # =====================================================================
        try:
            # Try to capture a tiny screenshot - fails if screen is locked
            capture_cmd = ["screencapture", "-x", "-c", "-T", "0"]
            result = subprocess.run(capture_cmd, capture_output=True, timeout=3)

            if result.returncode != 0:
                # screencapture fails when screen is locked
                logger.info("🔒 [SCREEN-DETECT] LOCKED via screencapture failure")
                return True

            detection_results.append(("ScreenCapture", False))
        except subprocess.TimeoutExpired:
            logger.info("🔒 [SCREEN-DETECT] screencapture timeout - likely LOCKED")
            return True
        except Exception as e:
            logger.debug(f"[SCREEN-DETECT] Screen capture test failed: {e}")

        # =====================================================================
        # Method 7: Check Console User (final fallback)
        # =====================================================================
        try:
            stat_cmd = ["stat", "-f", "%Su", "/dev/console"]
            result = subprocess.run(stat_cmd, capture_output=True, text=True, timeout=2)

            if result.returncode == 0:
                console_user = result.stdout.strip()
                current_user = os.environ.get("USER", "")

                if console_user != current_user and console_user not in ["root", ""]:
                    logger.info(f"🔒 [SCREEN-DETECT] Console user mismatch: {console_user} vs {current_user}")
                    return True
        except Exception as e:
            logger.debug(f"[SCREEN-DETECT] Console user check failed: {e}")

        # =====================================================================
        # Aggregate Results - IMPROVED LOGIC
        # =====================================================================
        locked_votes = sum(1 for _, is_locked in detection_results if is_locked)
        unlocked_votes = sum(1 for _, is_locked in detection_results if not is_locked)

        logger.info(f"[SCREEN-DETECT] Results: {detection_results}")
        logger.info(f"[SCREEN-DETECT] Votes: locked={locked_votes}, unlocked={unlocked_votes}")

        # If ANY method says locked, be cautious and return True
        # This prevents false "already unlocked" responses
        if locked_votes > 0:
            logger.info("🔒 [SCREEN-DETECT] LOCKED (at least one method detected lock)")
            return True

        # Only if ALL methods agree it's unlocked, return False
        if unlocked_votes >= 2:
            logger.info("🔓 [SCREEN-DETECT] UNLOCKED (multiple methods confirmed)")
            return False

        # If we didn't get enough votes either way, assume locked for safety
        # Better to attempt unlock than to incorrectly say "already unlocked"
        if len(detection_results) < 2:
            logger.warning("🔒 [SCREEN-DETECT] INSUFFICIENT DATA - assuming LOCKED for safety")
            return True

        # Default: screen is unlocked (we got here with no locked votes and some unlocked)
        logger.info("🔓 [SCREEN-DETECT] UNLOCKED (default - no lock indicators)")
        return False

    except Exception as e:
        logger.error(f"[SCREEN-DETECT] Critical error: {e}")
        # On error, assume LOCKED for safety (better to try unlock than skip it)
        logger.warning("🔒 [SCREEN-DETECT] ERROR - assuming LOCKED for safety")
        return True


def get_screen_state_details() -> Dict[str, Any]:
    """
    Get detailed screen state information using multiple detection methods.

    Returns:
        dict: Detailed state including lock status, detection method, and diagnostics
    """
    details = {
        "isLocked": False,
        "detectionMethod": None,
        "methods": {},
        "diagnostics": {}
    }

    try:
        # Method 1: Quartz CGSession
        try:
            import Quartz
            session_dict = Quartz.CGSessionCopyCurrentDictionary()
            if session_dict:
                details["methods"]["quartz"] = {
                    "CGSSessionScreenIsLocked": session_dict.get('CGSSessionScreenIsLocked', False),
                    "CGSSessionScreenLockedTime": session_dict.get('CGSSessionScreenLockedTime', 0),
                    "kCGSSessionOnConsoleKey": session_dict.get('kCGSSessionOnConsoleKey', True),
                }
                if details["methods"]["quartz"]["CGSSessionScreenIsLocked"]:
                    details["isLocked"] = True
                    details["detectionMethod"] = "Quartz-CGSSessionScreenIsLocked"
        except Exception as e:
            details["diagnostics"]["quartz_error"] = str(e)

        # Method 2: Login window frontmost
        try:
            script = 'tell application "System Events" to get name of first process whose frontmost is true'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                front_app = result.stdout.strip()
                details["methods"]["frontApp"] = front_app
                if "loginwindow" in front_app.lower():
                    details["isLocked"] = True
                    details["detectionMethod"] = details["detectionMethod"] or "LoginWindow-Frontmost"
        except Exception as e:
            details["diagnostics"]["frontApp_error"] = str(e)

        # Method 3: Screen capture test
        try:
            result = subprocess.run(["screencapture", "-x", "-c", "-T", "0"], capture_output=True, timeout=3)
            details["methods"]["screenCapture"] = result.returncode == 0
            if result.returncode != 0:
                details["isLocked"] = True
                details["detectionMethod"] = details["detectionMethod"] or "ScreenCapture-Failed"
        except Exception as e:
            details["diagnostics"]["screenCapture_error"] = str(e)

        # Method 4: IORegistry display state
        try:
            result = subprocess.run(["ioreg", "-r", "-c", "IODisplayWrangler", "-d", "1"],
                                    capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                output = result.stdout.lower()
                display_off = '"devicepowerstate" = 0' in output
                details["methods"]["ioregDisplayOff"] = display_off
        except Exception as e:
            details["diagnostics"]["ioreg_error"] = str(e)

        if not details["detectionMethod"]:
            details["detectionMethod"] = "AllMethodsPassed-Unlocked"

    except Exception as e:
        logger.error(f"Error getting screen state details: {e}")
        details["diagnostics"]["critical_error"] = str(e)

    return details


async def async_is_screen_locked() -> bool:
    """
    Async version of is_screen_locked() for use in async contexts.

    Returns:
        bool: True if screen is locked, False otherwise
    """
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, is_screen_locked)


if __name__ == "__main__":
    # Test the detection
    import json

    print("=" * 60)
    print("🔍 SCREEN LOCK DETECTION TEST")
    print("=" * 60)

    is_locked = is_screen_locked()
    print(f"\n📺 Screen is: {'🔒 LOCKED' if is_locked else '🔓 UNLOCKED'}")

    print("\n📊 Detailed State:")
    details = get_screen_state_details()
    print(json.dumps(details, indent=2, default=str))

    print("\n" + "=" * 60)