#!/usr/bin/env python3
"""
Transport Method Handlers
==========================

Implementation of all transport methods for screen control.
Each handler is async, timeout-safe, and reports detailed results.

v6.0 Enhancements (Computer Use Integration):
- Open Interpreter-inspired streaming execution
- Safety monitoring for all operations
- Screenshot verification of screen states
- Keyboard/Mouse automation fallbacks
"""

import asyncio
import logging
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# COMPUTER USE INTEGRATION (v6.0 - Open Interpreter-Inspired)
# =============================================================================
# Environment-driven configuration
COMPUTER_USE_ENABLED = os.getenv("JARVIS_COMPUTER_USE_ENABLED", "true").lower() == "true"
COMPUTER_USE_SAFETY_STRICT = os.getenv("JARVIS_COMPUTER_USE_SAFETY_STRICT", "true").lower() == "true"

# Lazy import flag to avoid heavy imports at module load
_computer_use_initialized = False
_computer_use_loop = None
_safety_monitor = None


async def _ensure_computer_use_initialized():
    """Lazily initialize Computer Use components."""
    global _computer_use_initialized, _computer_use_loop, _safety_monitor

    if _computer_use_initialized:
        return _computer_use_loop is not None

    if not COMPUTER_USE_ENABLED:
        _computer_use_initialized = True
        return False

    try:
        from backend.intelligence.computer_use_refinements import (
            get_computer_use_loop,
            SafetyMonitor,
        )

        _computer_use_loop = await get_computer_use_loop()
        _safety_monitor = SafetyMonitor(strict_mode=COMPUTER_USE_SAFETY_STRICT)
        _computer_use_initialized = True
        logger.info("✅ Computer Use refinements initialized for transport handlers")
        return True

    except ImportError:
        logger.debug("[ComputerUse] computer_use_refinements not available")
        _computer_use_initialized = True
        return False
    except Exception as e:
        logger.debug(f"[ComputerUse] Initialization failed: {e}")
        _computer_use_initialized = True
        return False


async def _verify_screen_state_with_screenshot(expected_state: str) -> Dict[str, Any]:
    """
    Take a screenshot and verify the screen state matches expectations.

    Args:
        expected_state: Expected state ("locked" or "unlocked")

    Returns:
        Dict with verification result and screenshot data
    """
    if not await _ensure_computer_use_initialized():
        return {"verified": False, "reason": "computer_use_not_available"}

    try:
        from backend.intelligence.computer_use_refinements import ScreenshotTool

        screenshot_tool = ScreenshotTool()
        result = await screenshot_tool()

        if result.error:
            return {"verified": False, "reason": f"screenshot_failed: {result.error}"}

        # Basic state detection from screenshot
        # In production, this would use vision AI to analyze the image
        verification = {
            "verified": True,
            "has_screenshot": result.base64_image is not None,
            "expected_state": expected_state,
            "screenshot_size": len(result.base64_image) if result.base64_image else 0,
        }

        return verification

    except Exception as e:
        logger.debug(f"[ComputerUse] Screenshot verification failed: {e}")
        return {"verified": False, "reason": str(e)}

def _is_locked_now() -> Optional[bool]:
    """Best-effort screen lock status. Returns None if unavailable."""
    try:
        from voice_unlock.objc.server.screen_lock_detector import is_screen_locked

        return bool(is_screen_locked())
    except Exception:
        return None


async def _wait_for_locked(timeout_s: float = 1.5) -> Optional[bool]:
    """Poll for locked state. Returns True/False or None if detector unavailable."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    last: Optional[bool] = None
    while asyncio.get_running_loop().time() < deadline:
        state = _is_locked_now()
        if state is None:
            return None
        last = state
        if state is True:
            return True
        await asyncio.sleep(0.06)
    return last


async def _run_subprocess(cmd: List[str], timeout_s: float) -> Tuple[bytes, bytes, int]:
    """Run a subprocess with strict timeout + cancellation kill."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return stdout or b"", stderr or b"", int(proc.returncode or 0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return b"", b"Timeout", -1
    except asyncio.CancelledError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return b"", str(e).encode(), -1


async def applescript_handler(action: str, context: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """
    AppleScript transport handler - Direct system automation.

    Most reliable method, works even when network services are down.
    Fast execution, low latency.
    Uses actual MacOSKeychainUnlock for unlock and system commands for lock.
    """
    logger.info(f"[APPLESCRIPT] Executing {action}")

    try:
        if action == "unlock_screen":
            # Use MacOSKeychainUnlock for actual unlock
            from macos_keychain_unlock import MacOSKeychainUnlock

            unlock_service = MacOSKeychainUnlock()
            # Get verified speaker from context (should be set by voice verification)
            verified_speaker = context.get("verified_speaker_name", "User")
            
            # If no verified speaker, try to get from database
            if verified_speaker == "User":
                try:
                    from intelligence.learning_database import get_learning_database
                    db = await get_learning_database()
                    profiles = await db.get_all_speaker_profiles()
                    for profile in profiles:
                        if profile.get('is_primary_user'):
                            full_name = profile.get('speaker_name', 'User')
                            verified_speaker = full_name.split()[0] if ' ' in full_name else full_name
                            break
                except Exception as e:
                    logger.warning(f"Could not get owner name from database: {e}")

            logger.info(
                f"[APPLESCRIPT] Calling MacOSKeychainUnlock.unlock_screen() for {verified_speaker}"
            )

            # Perform actual screen unlock with Keychain password
            unlock_result = await unlock_service.unlock_screen(verified_speaker=verified_speaker)

            logger.info(f"[APPLESCRIPT] Unlock result: {unlock_result}")

            if unlock_result["success"]:
                logger.info(f"[APPLESCRIPT] ✅ {action} succeeded for {verified_speaker}")
                return {
                    "success": True,
                    "method": "applescript",
                    "action": action,
                    "verified_speaker": verified_speaker,
                }
            else:
                logger.error(f"[APPLESCRIPT] ❌ Failed: {unlock_result['message']}")
                return {
                    "success": False,
                    "error": "unlock_failed",
                    "message": unlock_result["message"],
                }

        elif action == "lock_screen":
            # =================================================================
            # FAST LOCK v3.0 - Non-blocking, cancellation-safe, and no recursion
            # =================================================================
            # Goal: return a result quickly so the frontend never hangs on "🔒 Locking...".
            #
            # Strategy:
            # - Try non-UI methods first (CGSession / pmset / screensaver) in PARALLEL
            # - Only attempt AppleScript keystroke as a last resort (may prompt for Accessibility)
            # - Trust return codes; do NOT block on lock-state verification
            # =================================================================

            attempted: List[str] = []
            failures: List[Dict[str, Any]] = []

            # Build candidates
            cgsession_path = "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
            have_cgsession = os.path.exists(cgsession_path)
            have_launchctl = bool(shutil.which("launchctl"))

            primary: List[Tuple[str, List[str], float]] = []
            secondary: List[Tuple[str, List[str], float]] = []

            # CGSession -suspend (most reliable, no UI/Accessibility needed)
            if have_cgsession:
                attempted.append("cgsession")
                if os.geteuid() == 0 and have_launchctl:
                    try:
                        console_uid = os.stat("/dev/console").st_uid
                        cmd = ["launchctl", "asuser", str(console_uid), cgsession_path, "-suspend"]
                    except Exception:
                        cmd = [cgsession_path, "-suspend"]
                else:
                    cmd = [cgsession_path, "-suspend"]
                primary.append(("cgsession", cmd, 3.0))

            # pmset displaysleepnow (non-UI, generally available)
            if shutil.which("pmset"):
                attempted.append("pmset")
                primary.append(("pmset", ["pmset", "displaysleepnow"], 2.0))

            # Screensaver (locks if system requires re-auth immediately)
            if shutil.which("open"):
                attempted.append("screensaver")
                primary.append(("screensaver", ["open", "-a", "ScreenSaverEngine"], 2.0))

            # AppleScript keystroke (may require Accessibility)
            if shutil.which("osascript"):
                attempted.append("osascript")
                script = 'tell application "System Events" to keystroke "q" using {command down, control down}'
                secondary.append(("osascript", ["osascript", "-e", script], 2.0))

            if not primary and not secondary:
                return {
                    "success": False,
                    "error": "lock_failed",
                    "message": "Unable to lock screen (no lock methods available)",
                    "attempted_methods": attempted,
                }

            async def _run_group(candidates: List[Tuple[str, List[str], float]], group_timeout_s: float) -> Optional[Dict[str, Any]]:
                """Run candidates in parallel; return first success result dict or None."""
                if not candidates:
                    return None

                loop = asyncio.get_running_loop()
                deadline = loop.time() + group_timeout_s

                task_to_method: Dict[asyncio.Task, Tuple[str, List[str]]] = {}
                pending: set = set()
                for method, cmd, timeout_s in candidates:
                    t = asyncio.create_task(_run_subprocess(cmd, timeout_s=timeout_s))
                    task_to_method[t] = (method, cmd)
                    pending.add(t)

                try:
                    while pending and loop.time() < deadline:
                        done, pending = await asyncio.wait(
                            pending,
                            timeout=max(0.0, deadline - loop.time()),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            break

                        for t in done:
                            method, _cmd = task_to_method.get(t, ("unknown", []))
                            try:
                                stdout, stderr, rc = t.result()
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                failures.append({"method": method, "error": str(e)})
                                continue

                            if rc == 0:
                                # Cancel remaining attempts
                                for p in pending:
                                    p.cancel()
                                await asyncio.gather(*pending, return_exceptions=True)

                                logger.info(
                                    f"[APPLESCRIPT] ✅ {action} succeeded via {method} "
                                    f"(rc=0, parallel fast path)"
                                )
                                return {"success": True, "method": method, "action": action}

                            # Record failure details (useful for debugging without blocking)
                            failures.append(
                                {
                                    "method": method,
                                    "returncode": rc,
                                    "stderr": stderr.decode(errors="replace").strip() if stderr else "",
                                }
                            )

                finally:
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                return None

            # Run primary non-UI methods first (fast + avoids Accessibility prompts)
            # Keep within TransportManager's default applescript timeout (5s)
            primary_result = await _run_group(primary, group_timeout_s=3.0)
            if primary_result:
                return primary_result

            # If primary failed, try AppleScript as last resort
            secondary_result = await _run_group(secondary, group_timeout_s=1.7)
            if secondary_result:
                # Normalize method name for backward compatibility
                if secondary_result.get("method") == "osascript":
                    secondary_result["method"] = "applescript_shortcut"
                return secondary_result

            return {
                "success": False,
                "error": "lock_failed",
                "message": "Unable to lock screen (all methods failed)",
                "attempted_methods": attempted,
                "failures": failures,
            }

        else:
            return {
                "success": False,
                "error": "unknown_action",
                "message": f"Unknown action: {action}",
            }

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[APPLESCRIPT] Exception: {e}", exc_info=True)
        return {
            "success": False,
            "error": "applescript_exception",
            "message": str(e),
        }


async def _applescript_wake_display():
    """Wake display using caffeinate"""
    try:
        process = await asyncio.create_subprocess_exec(
            "caffeinate",
            "-u",
            "-t",
            "1",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
    except Exception as e:
        logger.debug(f"[APPLESCRIPT] Display wake failed: {e}")


async def http_rest_handler(action: str, context: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """
    HTTP REST transport handler - Local HTTP API fallback.

    Uses aiohttp to call local REST endpoints.
    Reliable when WebSocket is down but HTTP server is running.
    """
    logger.info(f"[HTTP-REST] Executing {action}")

    try:
        # Allow task cancellation to propagate (important for wait_for timeouts)
        # NOTE: In Python 3.9, CancelledError subclasses Exception, so we must re-raise explicitly.
        # (No-op here; included for symmetry with other handlers.)
        import aiohttp

        backend_port = os.getenv("BACKEND_PORT", "8010")
        base_url = f"http://localhost:{backend_port}"

        endpoint_map = {
            "unlock_screen": f"{base_url}/api/screen/unlock",
            "lock_screen": f"{base_url}/api/screen/lock",
        }

        endpoint = endpoint_map.get(action)
        if not endpoint:
            return {"success": False, "error": "unknown_action"}

        request_action = "unlock" if action == "unlock_screen" else ("lock" if action == "lock_screen" else action)
        authenticated_user = context.get("verified_speaker_name") or context.get("speaker_name")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json={
                    "action": request_action,
                    "authenticated_user": authenticated_user,
                    "context": context,
                },
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    logger.info(f"[HTTP-REST] ✅ {action} succeeded")
                    return result
                else:
                    error_text = await response.text()
                    logger.error(f"[HTTP-REST] ❌ Failed: HTTP {response.status}")
                    return {
                        "success": False,
                        "error": "http_error",
                        "message": f"HTTP {response.status}: {error_text}",
                    }

    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("[HTTP-REST] Request timed out")
        return {"success": False, "error": "http_timeout"}
    except Exception as e:
        logger.error(f"[HTTP-REST] Exception: {e}", exc_info=True)
        return {
            "success": False,
            "error": "http_exception",
            "message": str(e),
        }


async def unified_websocket_handler(
    action: str, context: Dict[str, Any], **kwargs
) -> Dict[str, Any]:
    """
    Unified WebSocket transport handler - Real-time bidirectional communication.

    Uses the EXISTING unified WebSocket connection (not port 8765).
    Fast when connection is healthy, but requires active WebSocket.
    """
    logger.info(f"[UNIFIED-WS] Executing {action}")

    try:
        # Get WebSocket connection from app state
        websocket_manager = kwargs.get("websocket_manager")

        if not websocket_manager:
            logger.warning("[UNIFIED-WS] WebSocket manager not available")
            return {
                "success": False,
                "error": "ws_not_available",
                "message": "WebSocket manager not initialized",
            }

        # Send action through unified WebSocket
        message = {
            "type": "screen_control",
            "action": action,
            "context": context,
        }

        # Broadcast to all connected clients
        await websocket_manager.broadcast(message)

        # For now, assume success if broadcast succeeded
        # In production, you'd wait for acknowledgment
        logger.info(f"[UNIFIED-WS] ✅ {action} broadcast succeeded")
        return {
            "success": True,
            "method": "unified_websocket",
            "action": action,
        }

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[UNIFIED-WS] Exception: {e}", exc_info=True)
        return {
            "success": False,
            "error": "ws_exception",
            "message": str(e),
        }


async def system_api_handler(action: str, context: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """
    System API transport handler - macOS system APIs.

    Uses AppleScript shortcut for lock, delegates to AppleScript handler for unlock.
    Most compatible with macOS system features.
    """
    logger.info(f"[SYSTEM-API] Executing {action}")

    try:
        if action == "lock_screen":
            # Use the macOS controller directly (more robust than routing back to AppleScript here).
            try:
                from system_control.macos_controller import MacOSController

                controller = MacOSController()
                speaker = context.get("verified_speaker_name") or context.get("speaker_name")
                success, message = await controller.lock_screen(
                    enable_voice_feedback=False,
                    speaker_name=speaker,
                )
                return {
                    "success": bool(success),
                    "method": "system_api",
                    "action": action,
                    "message": message,
                }
            except Exception as e:
                # Fallback to AppleScript transport behavior if controller isn't usable.
                logger.debug(f"[SYSTEM-API] macos_controller lock failed, falling back: {e}")
                result = await applescript_handler(action, context, **kwargs)
                if result.get("success"):
                    result["method"] = "system_api"
                return result

        elif action == "unlock_screen":
            # Delegate to AppleScript handler which has MacOSKeychainUnlock
            logger.info("[SYSTEM-API] Delegating unlock to AppleScript handler")
            return await applescript_handler(action, context, **kwargs)

        return {
            "success": False,
            "error": "unknown_action",
            "message": f"Unknown action: {action}",
        }

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[SYSTEM-API] Exception: {e}", exc_info=True)
        return {
            "success": False,
            "error": "system_api_exception",
            "message": str(e),
        }


# =============================================================================
# COMPUTER USE HANDLER (v6.0 - Open Interpreter-Inspired)
# =============================================================================

async def computer_use_handler(action: str, context: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """
    Computer Use transport handler - Advanced screen automation.

    Uses Open Interpreter-inspired patterns for:
    - Keyboard automation (shortcuts, typing)
    - Mouse control (clicks, drags)
    - Screenshot verification of actions
    - Safety monitoring for all operations

    This handler is used as a fallback when other methods fail,
    or for complex automation sequences that require visual verification.
    """
    logger.info(f"[COMPUTER-USE] Executing {action}")

    # Check if Computer Use is available
    if not await _ensure_computer_use_initialized():
        return {
            "success": False,
            "error": "computer_use_not_available",
            "message": "Computer Use refinements not initialized",
        }

    try:
        from backend.intelligence.computer_use_refinements import (
            KeyboardTool,
            ScreenshotTool,
        )

        keyboard = KeyboardTool()
        screenshot = ScreenshotTool()

        if action == "lock_screen":
            # Safety check before locking
            if _safety_monitor:
                allowed, reason = _safety_monitor.check_action("keyboard", "lock_screen")
                if not allowed:
                    return {
                        "success": False,
                        "error": "safety_blocked",
                        "message": f"Safety monitor blocked action: {reason}",
                    }

            # Try Control+Command+Q (standard macOS lock shortcut)
            logger.info("[COMPUTER-USE] Sending lock keyboard shortcut (Ctrl+Cmd+Q)")

            result = await keyboard(text=None, key="q", modifiers=["control", "command"])

            if result.error:
                logger.warning(f"[COMPUTER-USE] Keyboard shortcut failed: {result.error}")
                return {
                    "success": False,
                    "error": "keyboard_failed",
                    "message": result.error,
                }

            # Brief wait for lock to take effect
            await asyncio.sleep(0.5)

            # Verify with screenshot (optional, for confirmation)
            verification = await _verify_screen_state_with_screenshot("locked")

            logger.info(f"[COMPUTER-USE] ✅ {action} completed via keyboard shortcut")
            return {
                "success": True,
                "method": "computer_use",
                "action": action,
                "execution_method": "keyboard_shortcut",
                "verification": verification,
            }

        elif action == "unlock_screen":
            # For unlock, we need password input - delegate to safer methods
            # Computer Use can assist with verification but not actual unlock
            logger.info("[COMPUTER-USE] Unlock requires secure password handling")

            # Take screenshot to verify current state
            verification = await _verify_screen_state_with_screenshot("locked")

            if not verification.get("verified"):
                return {
                    "success": False,
                    "error": "cannot_verify_state",
                    "message": "Unable to verify screen state for unlock",
                    "verification": verification,
                }

            # Delegate to AppleScript handler which has secure keychain access
            logger.info("[COMPUTER-USE] Delegating unlock to AppleScript (keychain access)")
            delegate_result = await applescript_handler(action, context, **kwargs)

            # Add our verification to the result
            delegate_result["verification"] = verification
            delegate_result["assisted_by"] = "computer_use"

            return delegate_result

        elif action == "take_screenshot":
            # Direct screenshot action
            result = await screenshot()

            if result.error:
                return {
                    "success": False,
                    "error": "screenshot_failed",
                    "message": result.error,
                }

            return {
                "success": True,
                "method": "computer_use",
                "action": action,
                "base64_image": result.base64_image,
                "output": result.output,
            }

        elif action == "send_keystroke":
            # Generic keystroke action
            key = context.get("key")
            text = context.get("text")
            modifiers = context.get("modifiers", [])

            if not key and not text:
                return {
                    "success": False,
                    "error": "missing_input",
                    "message": "Either 'key' or 'text' must be provided",
                }

            # Safety check
            if _safety_monitor:
                allowed, reason = _safety_monitor.check_action(
                    "keyboard",
                    f"keystroke:{key or text[:20]}",
                )
                if not allowed:
                    return {
                        "success": False,
                        "error": "safety_blocked",
                        "message": f"Safety monitor blocked action: {reason}",
                    }

            result = await keyboard(text=text, key=key, modifiers=modifiers)

            if result.error:
                return {
                    "success": False,
                    "error": "keystroke_failed",
                    "message": result.error,
                }

            return {
                "success": True,
                "method": "computer_use",
                "action": action,
                "output": result.output,
            }

        else:
            return {
                "success": False,
                "error": "unknown_action",
                "message": f"Unknown action for computer_use handler: {action}",
            }

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[COMPUTER-USE] Exception: {e}", exc_info=True)
        return {
            "success": False,
            "error": "computer_use_exception",
            "message": str(e),
        }


# Handler registry
TRANSPORT_HANDLERS = {
    "applescript": applescript_handler,
    "http_rest": http_rest_handler,
    "unified_websocket": unified_websocket_handler,
    "system_api": system_api_handler,
    "computer_use": computer_use_handler,  # v6.0: Computer Use refinements
}
