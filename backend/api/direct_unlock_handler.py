#!/usr/bin/env python3
"""
Direct Unlock Handler
====================

Provides direct screen unlock functionality.

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
WEBSOCKET_RESPONSE_TIMEOUT = 20.0  # Max time to wait for unlock response
HEALTH_CHECK_TIMEOUT = 2.0  # Quick health check timeout


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
            await ws.send(json.dumps({"type": "command", "command": "get_status"}))
            response = await asyncio.wait_for(ws.recv(), timeout=1.0)
            result = json.loads(response)
            return result.get("type") == "status" or result.get("success", False)
        finally:
            await ws.close()
    except asyncio.TimeoutError:
        logger.debug("[HEALTH CHECK] Daemon connection timed out")
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
    logger.info("Checking voice unlock daemon health...")
    if not await _check_daemon_health():
        logger.error("Voice unlock daemon is not running or not responsive")
        logger.error("Start the daemon with: python -m backend.voice_unlock.jarvis_voice_unlock start")
        return False

    try:
        # Connect to voice unlock daemon with timeout (Python 3.9 compatible)
        logger.info("Connecting to voice unlock daemon for direct unlock")

        websocket = await asyncio.wait_for(
            websockets.connect(
                VOICE_UNLOCK_WS_URL,
                ping_interval=20,
                close_timeout=2.0
            ),
            timeout=WEBSOCKET_CONNECT_TIMEOUT
        )

        try:
            # Send unlock command (Voice Unlock expects this format)
            unlock_command = {
                "type": "command",
                "command": "unlock_screen"
            }

            await websocket.send(json.dumps(unlock_command))
            logger.info(f"Sent unlock command: {unlock_command}")

            # Wait for response with timeout
            response = await asyncio.wait_for(
                websocket.recv(),
                timeout=WEBSOCKET_RESPONSE_TIMEOUT
            )
            result = json.loads(response)

            logger.info(f"Unlock response: {result}")

            # Check for command_response type (what Voice Unlock actually sends)
            if result.get("type") == "command_response":
                success = result.get("success", False)
                logger.info(f"Unlock success: {success}, message: {result.get('message')}")
                return success
            elif result.get("type") == "unlock_result":
                return result.get("success", False)
            else:
                logger.error(f"Unexpected response type: {result.get('type')}")
                logger.error(f"Full response: {result}")
                return False
        finally:
            await websocket.close()

    except asyncio.TimeoutError:
        logger.error("Timeout waiting for unlock response")
        return False
    except Exception as e:
        logger.error(f"Error in direct unlock: {e}")
        return False


async def check_screen_locked_direct() -> bool:
    """Check if screen is locked using Context Intelligence"""
    try:
        # Use Context Intelligence screen state detector for accurate detection
        from context_intelligence.core.screen_state import ScreenStateDetector, ScreenState
        
        logger.info("[DIRECT UNLOCK] Checking screen lock via Context Intelligence")
        detector = ScreenStateDetector()
        state = await detector.get_screen_state()
        
        is_locked = state.state == ScreenState.LOCKED
        logger.info(f"[DIRECT UNLOCK] Screen state: {state.state.value} (confidence: {state.confidence:.2f})")
        logger.info(f"[DIRECT UNLOCK] Screen locked: {is_locked}")
        
        # Also try Voice Unlock daemon for comparison (but don't rely on it)
        try:
            # Python 3.9 compatible timeout approach
            ws = await asyncio.wait_for(
                websockets.connect(
                    VOICE_UNLOCK_WS_URL,
                    ping_interval=None,
                    close_timeout=1.0
                ),
                timeout=HEALTH_CHECK_TIMEOUT
            )
            try:
                status_command = {"type": "command", "command": "get_status"}
                await ws.send(json.dumps(status_command))
                response = await asyncio.wait_for(ws.recv(), timeout=2.0)
                result = json.loads(response)
                daemon_locked = result.get("status", {}).get("isScreenLocked", False)
                logger.debug(f"[DIRECT UNLOCK] Voice Unlock daemon reports: {daemon_locked} (ignored)")
            finally:
                await ws.close()
        except Exception:
            pass  # Non-critical comparison, safe to ignore
        
        return is_locked
        
    except Exception as e:
        logger.error(f"Error checking screen lock: {e}")
        # Fallback to system check
        return check_screen_locked_system()


def check_screen_locked_system() -> bool:
    """Check screen lock state using system API"""
    try:
        logger.info("[DIRECT UNLOCK] Checking screen lock via system API")
        import subprocess
        result = subprocess.run(['python', '-c', '''
import Quartz
session_dict = Quartz.CGSessionCopyCurrentDictionary()
if session_dict:
    locked = session_dict.get("CGSSessionScreenIsLocked", False)
    print("true" if locked else "false")
else:
    print("false")
'''], capture_output=True, text=True)
        
        is_locked = result.stdout.strip().lower() == "true"
        logger.info(f"[DIRECT UNLOCK] Screen locked from system: {is_locked}")
        return is_locked
        
    except Exception as e:
        logger.error(f"Error in system screen check: {e}")
        return False