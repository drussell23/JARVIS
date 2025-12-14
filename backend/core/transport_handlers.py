#!/usr/bin/env python3
"""
Transport Method Handlers
==========================

Implementation of all transport methods for screen control.
Each handler is async, timeout-safe, and reports detailed results.
"""

import asyncio
import logging
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
            # FAST LOCK v2.0 - Fire-and-forget, NO blocking verification
            # =================================================================
            # The _wait_for_locked() function uses synchronous subprocess calls
            # that block the event loop, causing the UI to hang on "Locking..."
            # 
            # Solution: Execute lock command and return immediately if successful.
            # Do NOT poll for lock verification - trust the command return code.
            # =================================================================
            
            attempted: List[str] = []

            # Method 1: CGSession -suspend (most reliable, no UI/Accessibility needed)
            cgsession_path = "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
            have_cgsession = os.path.exists(cgsession_path)
            have_launchctl = bool(shutil.which("launchctl"))

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

                _stdout, _stderr, rc = await _run_subprocess(cmd, timeout_s=3.0)
                # FAST: Return immediately if command succeeded (rc == 0)
                # Do NOT wait for verification - it blocks the event loop!
                if rc == 0:
                    logger.info(f"[APPLESCRIPT] ✅ {action} succeeded via CGSession (rc=0, no blocking verification)")
                    return {"success": True, "method": "cgsession", "action": action}

            # Method 2: AppleScript shortcut (requires Accessibility permission)
            if shutil.which("osascript"):
                attempted.append("osascript")
                script = 'tell application "System Events" to keystroke "q" using {command down, control down}'
                _stdout, stderr, rc = await _run_subprocess(["osascript", "-e", script], timeout_s=2.0)
                # FAST: Return immediately if command succeeded
                if rc == 0:
                    logger.info(f"[APPLESCRIPT] ✅ {action} succeeded via osascript (rc=0, no blocking verification)")
                    return {"success": True, "method": "applescript_shortcut", "action": action}

                error_msg = stderr.decode(errors="replace").strip() if stderr else "unknown error"
                logger.debug(f"[APPLESCRIPT] osascript lock failed: {error_msg}")

            # Method 3: pmset displaysleepnow (non-UI, generally available)
            if shutil.which("pmset"):
                attempted.append("pmset")
                _stdout, _stderr, rc = await _run_subprocess(["pmset", "displaysleepnow"], timeout_s=2.0)
                # FAST: Return immediately if command succeeded
                if rc == 0:
                    logger.info(f"[APPLESCRIPT] ✅ {action} succeeded via pmset (rc=0, no blocking verification)")
                    return {"success": True, "method": "pmset", "action": action}

            # Method 4: Start screensaver (locks if system security requires auth immediately)
            if shutil.which("open"):
                attempted.append("screensaver")
                _stdout, _stderr, rc = await _run_subprocess(["open", "-a", "ScreenSaverEngine"], timeout_s=2.0)
                # FAST: Return immediately if command succeeded
                if rc == 0:
                    logger.info(f"[APPLESCRIPT] ✅ {action} succeeded via screensaver (rc=0, no blocking verification)")
                    return {"success": True, "method": "screensaver", "action": action}

            return {
                "success": False,
                "error": "lock_failed",
                "message": "Unable to lock screen (all methods failed)",
                "attempted_methods": attempted,
            }

        else:
            return {
                "success": False,
                "error": "unknown_action",
                "message": f"Unknown action: {action}",
            }

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
        import aiohttp

        endpoint_map = {
            "unlock_screen": "http://localhost:8000/api/screen/unlock",
            "lock_screen": "http://localhost:8000/api/screen/lock",
        }

        endpoint = endpoint_map.get(action)
        if not endpoint:
            return {"success": False, "error": "unknown_action"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json={"action": action, "context": context},
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
            # Keep lock behavior consistent with the AppleScript transport, but label as system_api.
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

    except Exception as e:
        logger.error(f"[SYSTEM-API] Exception: {e}", exc_info=True)
        return {
            "success": False,
            "error": "system_api_exception",
            "message": str(e),
        }


# Handler registry
TRANSPORT_HANDLERS = {
    "applescript": applescript_handler,
    "http_rest": http_rest_handler,
    "unified_websocket": unified_websocket_handler,
    "system_api": system_api_handler,
}
