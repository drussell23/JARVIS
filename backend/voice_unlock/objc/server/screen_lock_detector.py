#!/usr/bin/env python3
"""
Screen Lock Detection Module - Enhanced v3.0
=============================================

Provides robust, multi-method screen lock detection for macOS.
Uses multiple detection strategies with fallbacks for reliability.

v3.0: PRIMARY method is now pure ctypes (CoreFoundation + CoreGraphics C APIs).
      This avoids importing pyobjc-framework-Quartz which triggers loading
      AppKit._metadata — a 15K+ line ObjC bridge registration. That import is
      NOT safe in threads when CoreAudio IO thread is running (AudioBus active),
      as it causes SIGSEGV due to concurrent native ObjC runtime mutation.
      Same class of bug as v270.0 (torch/whisper) and v268.0 (scipy/BLAS).
"""

import subprocess
import logging
import os
import ctypes
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# Pure ctypes CGSession implementation (CoreAudio-IO-thread-safe)
# =============================================================================
# Uses CoreFoundation + CoreGraphics C APIs directly. NO ObjC bridge loading,
# NO AppKit._metadata import, safe to call from ANY thread concurrently with
# CoreAudio IO thread.

_cf_lib = None
_cg_lib = None
_ctypes_ready = False

# CoreFoundation constants
_kCFStringEncodingUTF8 = 0x08000100
_kCFNumberFloat64Type = 13


def _ensure_ctypes_frameworks() -> bool:
    """One-time init of CoreFoundation + CoreGraphics ctypes bindings.

    Loads the C dylibs and sets up function signatures. These are pure C
    libraries — no ObjC runtime interaction, no AppKit, no pyobjc.
    Thread-safe: dylib loading is idempotent and the global assignment is atomic.
    """
    global _cf_lib, _cg_lib, _ctypes_ready
    if _ctypes_ready:
        return _cf_lib is not None and _cg_lib is not None

    try:
        cf = ctypes.CDLL(
            '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation'
        )
        cg = ctypes.CDLL(
            '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics'
        )

        # CGSessionCopyCurrentDictionary() -> CFDictionaryRef
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p

        # CFStringCreateWithCString(alloc, cStr, encoding) -> CFStringRef
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32
        ]

        # CFDictionaryGetValue(dict, key) -> value (does NOT retain)
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        # CFBooleanGetValue(boolean) -> bool
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]

        # CFGetTypeID(cf) -> CFTypeID
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]

        # Type ID getters (no args)
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong

        # CFNumberGetValue(number, theType, valuePtr) -> bool
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [
            ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p
        ]

        # CFRelease(cf)
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        _cf_lib = cf
        _cg_lib = cg
        _ctypes_ready = True
        return True

    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] Failed to init ctypes frameworks: {e}")
        _ctypes_ready = True  # Don't retry on every call
        return False


def _cf_dict_get_bool(cf, session_dict: int, key_name: bytes) -> Optional[bool]:
    """Read a CFBoolean value from a CFDictionary by key name."""
    key_cf = cf.CFStringCreateWithCString(None, key_name, _kCFStringEncodingUTF8)
    if not key_cf:
        return None
    try:
        val = cf.CFDictionaryGetValue(session_dict, key_cf)
        if not val:
            return None
        type_id = cf.CFGetTypeID(val)
        if type_id == cf.CFBooleanGetTypeID():
            return bool(cf.CFBooleanGetValue(val))
        return None
    finally:
        cf.CFRelease(key_cf)


def _cf_dict_get_number(cf, session_dict: int, key_name: bytes) -> Optional[float]:
    """Read a CFNumber value from a CFDictionary by key name."""
    key_cf = cf.CFStringCreateWithCString(None, key_name, _kCFStringEncodingUTF8)
    if not key_cf:
        return None
    try:
        val = cf.CFDictionaryGetValue(session_dict, key_cf)
        if not val:
            return None
        type_id = cf.CFGetTypeID(val)
        if type_id == cf.CFNumberGetTypeID():
            result = ctypes.c_double(0)
            if cf.CFNumberGetValue(val, _kCFNumberFloat64Type, ctypes.byref(result)):
                return result.value
        return None
    finally:
        cf.CFRelease(key_cf)


def _check_cgsession_locked_via_ctypes() -> Optional[bool]:
    """
    Check screen lock via CGSession API using pure ctypes.

    This is the PRIMARY detection method because it uses CoreFoundation and
    CoreGraphics C APIs directly — no ObjC bridge, no AppKit._metadata loading.
    Safe to call from any thread, including when CoreAudio IO thread is running.

    Returns:
        Optional[bool]: True if locked, False if unlocked, None if cannot determine
    """
    if not _ensure_ctypes_frameworks():
        return None

    cf = _cf_lib
    cg = _cg_lib

    try:
        session_dict = cg.CGSessionCopyCurrentDictionary()
        if not session_dict:
            return None

        try:
            # Check CGSSessionScreenIsLocked (boolean — definitive lock indicator)
            is_locked = _cf_dict_get_bool(cf, session_dict, b"CGSSessionScreenIsLocked")
            if is_locked is True:
                logger.info(
                    "🔒 [SCREEN-DETECT] LOCKED via ctypes CGSSessionScreenIsLocked"
                )
                return True

            # Check CGSSessionScreenLockedTime (number > 0 means lock active)
            lock_time = _cf_dict_get_number(
                cf, session_dict, b"CGSSessionScreenLockedTime"
            )
            if lock_time is not None and lock_time > 0:
                logger.info(
                    "🔒 [SCREEN-DETECT] LOCKED via ctypes CGSSessionScreenLockedTime"
                )
                return True

            # Check kCGSSessionOnConsoleKey (False = not on console = locked)
            on_console = _cf_dict_get_bool(
                cf, session_dict, b"kCGSSessionOnConsoleKey"
            )
            if on_console is False:
                logger.info(
                    "🔒 [SCREEN-DETECT] LOCKED via ctypes kCGSSessionOnConsoleKey=False"
                )
                return True

            # Dictionary obtained and no lock indicators — screen is unlocked
            logger.info(
                "🔓 [SCREEN-DETECT] CGSession (ctypes) says UNLOCKED - Fast Path"
            )
            return False

        finally:
            cf.CFRelease(session_dict)

    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] ctypes CGSession check failed: {e}")
        return None


def _get_cgsession_details_via_ctypes() -> Optional[Dict[str, Any]]:
    """
    Get detailed CGSession dictionary values via pure ctypes.

    Returns:
        Optional[dict]: Session details or None if ctypes unavailable
    """
    if not _ensure_ctypes_frameworks():
        return None

    cf = _cf_lib
    cg = _cg_lib

    try:
        session_dict = cg.CGSessionCopyCurrentDictionary()
        if not session_dict:
            return None

        try:
            locked = _cf_dict_get_bool(
                cf, session_dict, b"CGSSessionScreenIsLocked"
            )
            lock_time = _cf_dict_get_number(
                cf, session_dict, b"CGSSessionScreenLockedTime"
            )
            on_console = _cf_dict_get_bool(
                cf, session_dict, b"kCGSSessionOnConsoleKey"
            )
            screensaver = _cf_dict_get_bool(
                cf, session_dict, b"CGSSessionScreenSaverIsActive"
            )

            return {
                "CGSSessionScreenIsLocked": locked if locked is not None else False,
                "CGSSessionScreenLockedTime": lock_time if lock_time is not None else 0,
                "kCGSSessionOnConsoleKey": on_console if on_console is not None else True,
                "CGSSessionScreenSaverIsActive": screensaver if screensaver is not None else False,
            }
        finally:
            cf.CFRelease(session_dict)

    except Exception as e:
        logger.debug(f"[SCREEN-DETECT] ctypes session details failed: {e}")
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
    1. Pure ctypes CGSessionCopyCurrentDictionary (most reliable, CoreAudio-safe)
    2. osascript UI interaction check (detects lock screen password prompt)
    3. Lock-related process check (loginwindow, ScreenSaverEngine)
    4. IORegistry display power state check
    5. Login window frontmost check
    6. Screen capture test (definitive but slower)
    7. Console user check (final fallback)

    v3.0: Quartz pyobjc import removed as primary method. It triggers loading
    AppKit._metadata (15K+ lines of ObjC bridge registration) which causes
    SIGSEGV when called in a thread while CoreAudio IO thread is running.
    Pure ctypes reads the same CGSession dictionary via C APIs — no ObjC bridge.

    IMPORTANT: If ANY reliable method says locked, we return True.
    This prevents false "already unlocked" responses.

    Returns:
        bool: True if screen is locked, False otherwise
    """
    detection_results = []

    try:
        # =====================================================================
        # Method 1: Pure ctypes CGSession API (MOST RELIABLE, THREAD-SAFE)
        # Uses CoreFoundation + CoreGraphics C APIs directly.
        # Does NOT load AppKit._metadata — safe with CoreAudio IO thread.
        # =====================================================================
        ctypes_result = _check_cgsession_locked_via_ctypes()
        if ctypes_result is not None:
            # ctypes gave a definitive answer — trust it (fast path)
            if ctypes_result:
                return True
            else:
                detection_results.append(("CGSession-ctypes", False))
                logger.info(
                    "🔓 [SCREEN-DETECT] ctypes CGSession says UNLOCKED - "
                    "Trusting ctypes (Fast Path)"
                )
                return False

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
        # Method 1: CGSession via pure ctypes (CoreAudio-IO-thread-safe)
        session_info = _get_cgsession_details_via_ctypes()
        if session_info is not None:
            details["methods"]["cgsession_ctypes"] = session_info
            if session_info.get("CGSSessionScreenIsLocked"):
                details["isLocked"] = True
                details["detectionMethod"] = "ctypes-CGSSessionScreenIsLocked"
            elif session_info.get("CGSSessionScreenSaverIsActive"):
                details["isLocked"] = True
                details["detectionMethod"] = "ctypes-CGSSessionScreenSaverIsActive"
        else:
            details["diagnostics"]["ctypes_cgsession"] = "unavailable"

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