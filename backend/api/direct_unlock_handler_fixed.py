#!/usr/bin/env python3
"""
Direct Unlock Handler - FIXED
=============================

Provides direct screen unlock functionality with correct message format.

Robustness Features:
- Connection timeout (5s) to prevent hanging if daemon is unavailable
- Health check before unlock attempt
- Graceful degradation with clear error messages
"""

import asyncio
import logging
import websockets
import json
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

VOICE_UNLOCK_WS_URL = "ws://localhost:8765/voice-unlock"

# Timeout constants (seconds)
WEBSOCKET_CONNECT_TIMEOUT = 5.0  # Max time to establish connection
WEBSOCKET_RESPONSE_TIMEOUT = 30.0  # Max time to wait for unlock response
HEALTH_CHECK_TIMEOUT = 2.0  # Quick health check timeout


async def _connect_with_timeout(url: str, timeout: float):
    """
    Connect to WebSocket with timeout (Python 3.9 compatible).
    Returns websocket connection or raises TimeoutError.
    """
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        websockets.connect(
            url,
            ping_interval=None,
            close_timeout=1.0
        ),
        timeout=timeout
    )


async def _check_daemon_health() -> bool:
    """
    Quick health check to verify daemon is responsive.
    Returns True if daemon is running and responsive, False otherwise.
    Uses socket-level check for faster detection of unavailable daemon.
    """
    import socket

    # First, do a quick socket-level check (much faster than websockets.connect)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)  # 500ms timeout for socket connect
        result = sock.connect_ex(('localhost', 8765))
        sock.close()
        if result != 0:
            logger.debug("[HEALTH CHECK] Daemon port not responding (socket check)")
            return False
    except Exception as e:
        logger.debug(f"[HEALTH CHECK] Socket check failed: {e}")
        return False

    # Port is open, now verify WebSocket protocol works
    try:
        ws = await asyncio.wait_for(
            websockets.connect(
                VOICE_UNLOCK_WS_URL,
                ping_interval=None,
                close_timeout=0.5,
                open_timeout=1.0  # Fast open timeout
            ),
            timeout=HEALTH_CHECK_TIMEOUT
        )
        try:
            # Send a quick status check
            await ws.send(json.dumps({"type": "command", "command": "get_status"}))
            response = await asyncio.wait_for(ws.recv(), timeout=1.0)
            result = json.loads(response)
            return result.get("type") == "status" or result.get("success", False)
        finally:
            await ws.close()
    except asyncio.TimeoutError:
        logger.debug("[HEALTH CHECK] Daemon WebSocket connection timed out")
        return False
    except Exception as e:
        logger.debug(f"[HEALTH CHECK] Daemon not responsive: {e}")
        return False


async def unlock_screen_direct(reason: str = "User request") -> bool:
    """
    Directly unlock the screen using WebSocket connection.

    Features:
    - Fast-fail health check before attempting unlock
    - Connection timeout to prevent infinite hang
    - Clear error messages for troubleshooting
    """
    # Quick health check first (fast-fail pattern)
    logger.info("[DIRECT UNLOCK] Checking daemon health...")
    if not await _check_daemon_health():
        logger.error("[DIRECT UNLOCK] Voice unlock daemon is not running or not responsive")
        logger.error("[DIRECT UNLOCK] Start the daemon with: python -m backend.voice_unlock.jarvis_voice_unlock start")
        return False

    logger.info("[DIRECT UNLOCK] Daemon responsive, proceeding with unlock")

    try:
        # Connect with explicit timeout to prevent hanging (Python 3.9 compatible)
        logger.info("[DIRECT UNLOCK] Connecting to voice unlock daemon for direct unlock")

        websocket = await asyncio.wait_for(
            websockets.connect(
                VOICE_UNLOCK_WS_URL,
                ping_interval=20,
                close_timeout=2.0
            ),
            timeout=WEBSOCKET_CONNECT_TIMEOUT
        )

        try:
            # Send unlock command with CORRECT format
            unlock_command = {
                "type": "command",
                "command": "unlock_screen",
                "parameters": {
                    "source": "context_handler",
                    "reason": reason,
                    "authenticated": True,
                },
            }

            await websocket.send(json.dumps(unlock_command))
            logger.info(f"[DIRECT UNLOCK] Sent unlock command: {unlock_command}")

            # Wait for response with timeout
            response = await asyncio.wait_for(
                websocket.recv(),
                timeout=WEBSOCKET_RESPONSE_TIMEOUT
            )
            result = json.loads(response)

            logger.info(f"[DIRECT UNLOCK] Unlock response: {result}")

            # Check for correct response format
            if (
                result.get("type") == "command_response"
                and result.get("command") == "unlock_screen"
            ):
                success = result.get("success", False)
                message = result.get("message", "")
                logger.info(
                    f"[DIRECT UNLOCK] Unlock {'succeeded' if success else 'failed'}: {message}"
                )
                return success
            else:
                logger.error(f"[DIRECT UNLOCK] Unexpected response: {result}")
                return False
        finally:
            await websocket.close()

    except asyncio.TimeoutError:
        logger.error(f"[DIRECT UNLOCK] Connection/response timed out")
        logger.error("[DIRECT UNLOCK] Voice unlock daemon may be stuck or not responding")
        return False
    except (ConnectionRefusedError, OSError) as e:
        if "Connect call failed" in str(e) or "connection refused" in str(e).lower():
            logger.error("[DIRECT UNLOCK] Voice unlock daemon not running on port 8765")
        else:
            logger.error(f"[DIRECT UNLOCK] Connection error: {e}")
        return False
    except Exception as e:
        logger.error(f"[DIRECT UNLOCK] Error in direct unlock: {e}")
        return False


async def check_screen_locked_direct() -> bool:
    """Check if screen is locked via direct WebSocket with timeout protection."""
    try:
        logger.info("[DIRECT UNLOCK] Checking screen lock status via WebSocket")

        # Connect with timeout (Python 3.9 compatible)
        websocket = await asyncio.wait_for(
            websockets.connect(VOICE_UNLOCK_WS_URL, ping_interval=20, close_timeout=2.0),
            timeout=WEBSOCKET_CONNECT_TIMEOUT
        )

        try:
            # Get status with correct format
            status_command = {"type": "command", "command": "get_status"}
            await websocket.send(json.dumps(status_command))

            # Wait for response
            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            result = json.loads(response)
            logger.info(f"[DIRECT UNLOCK] Voice unlock status: {result}")

            if result.get("type") == "status" and result.get("success"):
                status = result.get("status", {})
                is_locked = status.get("isScreenLocked", False)
                logger.info(f"[DIRECT UNLOCK] Screen locked from daemon: {is_locked}")
                return is_locked

            return False
        finally:
            await websocket.close()

    except asyncio.TimeoutError:
        logger.warning("[DIRECT UNLOCK] Daemon connection timed out, checking via system")
        return check_screen_locked_system()
    except (ConnectionRefusedError, OSError) as e:
        if "Connect call failed" in str(e) or "connection refused" in str(e).lower():
            logger.warning(
                "[DIRECT UNLOCK] Voice unlock daemon not running, checking via system"
            )
        else:
            logger.warning(f"[DIRECT UNLOCK] Connection error: {e}")
        return check_screen_locked_system()
    except Exception as e:
        logger.error(f"[DIRECT UNLOCK] Error checking screen lock: {e}")
        # Fallback to system check
        return check_screen_locked_system()


def check_screen_locked_system() -> bool:
    """Check screen lock state using system API"""
    try:
        logger.info("[DIRECT UNLOCK] Checking screen lock via system API")
        import subprocess

        # Use a more reliable method to check screen lock
        check_script = """
import Quartz
import sys

try:
    # Get the current session dictionary
    session_dict = Quartz.CGSessionCopyCurrentDictionary()
    if session_dict:
        # Check multiple indicators
        screen_locked = session_dict.get("CGSSessionScreenIsLocked", False)
        screen_saver = session_dict.get("CGSSessionScreenSaverIsActive", False)
        on_console = session_dict.get("kCGSSessionOnConsoleKey", True)
        
        # Screen is considered locked if locked flag is True or screensaver is active
        is_locked = bool(screen_locked or screen_saver)
        print("true" if is_locked else "false")
    else:
        # If we can't get session dict, assume unlocked
        print("false")
except Exception as e:
    print("false")
    sys.exit(1)
"""

        result = subprocess.run(
            ["python3", "-c", check_script], capture_output=True, text=True, timeout=5
        )

        is_locked = result.stdout.strip().lower() == "true"
        logger.info(f"[DIRECT UNLOCK] Screen locked from system: {is_locked}")
        return is_locked

    except subprocess.TimeoutExpired:
        logger.error("[DIRECT UNLOCK] Timeout checking screen lock state")
        return False
    except Exception as e:
        logger.error(f"[DIRECT UNLOCK] Error in system screen check: {e}")
        return False


async def test_screen_lock_context():
    """Test function to verify screen lock detection and unlock"""
    print("\n🔍 Testing Screen Lock Context Detection")
    print("=" * 50)

    # Check if screen is locked
    is_locked = await check_screen_locked_direct()
    print(f"Screen is {'LOCKED' if is_locked else 'UNLOCKED'}")

    if is_locked:
        print("\n🔓 Attempting to unlock screen...")
        success = await unlock_screen_direct("Testing context awareness")
        if success:
            print("✅ Screen unlocked successfully!")
        else:
            print("❌ Failed to unlock screen")
    else:
        print("\n💡 Lock your screen (Cmd+Ctrl+Q) and run this test again")

    return is_locked


if __name__ == "__main__":
    # Run test
    asyncio.run(test_screen_lock_context())

