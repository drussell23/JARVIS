#!/usr/bin/env python3
"""
WebSocket Server Bridge for JARVIS Voice Unlock Daemon
======================================================

Provides WebSocket API for the Objective-C daemon with:
- Hybrid STT integration
- Speaker recognition
- Database recording
- CAI/SAI intelligence
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

import websockets

# Add parent paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from screen_lock_detector import is_screen_locked

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class VoiceUnlockWebSocketServer:
    """
    WebSocket server with advanced intelligence.

    Features:
    - Hybrid STT for accurate transcription
    - Speaker recognition for identity verification
    - Database recording for learning
    - CAI/SAI integration for context/scenario awareness
    """

    def __init__(self, port: int = 8765):
        self.port = port
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.daemon_process = None
        self.enrolled_users_file = os.path.expanduser("~/.jarvis/voice_unlock/enrolled_users.json")

        # Intelligent Voice Unlock Service
        self.intelligent_service = None

    async def initialize_intelligent_service(self):
        """Initialize intelligent voice unlock service"""
        if self.intelligent_service is not None:
            return

        try:
            from voice_unlock.intelligent_voice_unlock_service import get_intelligent_unlock_service

            self.intelligent_service = get_intelligent_unlock_service()
            await self.intelligent_service.initialize()
            logger.info("✅ Intelligent Voice Unlock Service initialized")
        except Exception as e:
            logger.error(f"Failed to initialize intelligent service: {e}")
            self.intelligent_service = None

    async def register_client(self, websocket):
        """Register a new WebSocket client"""
        self.clients.add(websocket)

        # Initialize intelligent service when first client connects
        if self.intelligent_service is None:
            await self.initialize_intelligent_service()
        logger.info(f"Client connected. Total clients: {len(self.clients)}")

    async def unregister_client(self, websocket):
        """Unregister a WebSocket client"""
        self.clients.discard(websocket)
        logger.info(f"Client disconnected. Total clients: {len(self.clients)}")

    async def handle_message(self, websocket, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming WebSocket message"""
        msg_type = message.get("type")
        command = message.get("command")
        parameters = message.get("parameters", {})

        logger.info(f"Received: type={msg_type}, command={command}")

        if msg_type == "command":
            if command == "handshake":
                return {
                    "type": "handshake",
                    "success": True,
                    "version": "1.0",
                    "daemon": "JARVIS Voice Unlock",
                }

            elif command == "get_status":
                # Get daemon status
                is_running = self.check_daemon_running()
                enrolled_users = self.get_enrolled_users()

                # Check actual screen lock status
                screen_locked = is_screen_locked()
                logger.info(
                    f"Screen lock status check: {'LOCKED' if screen_locked else 'UNLOCKED'}"
                )

                return {
                    "type": "status",
                    "success": True,
                    "status": {
                        "isMonitoring": is_running,
                        "isScreenLocked": screen_locked,
                        "enrolledUser": enrolled_users[0] if enrolled_users else "none",
                        "failedAttempts": 0,
                        "state": 1 if is_running else 0,
                    },
                }

            elif command == "start_monitoring":
                # Start daemon if not running
                if not self.check_daemon_running():
                    success = self.start_daemon()
                    return {
                        "type": "command_response",
                        "command": command,
                        "success": success,
                        "message": (
                            "Monitoring started" if success else "Failed to start monitoring"
                        ),
                    }
                else:
                    return {
                        "type": "command_response",
                        "command": command,
                        "success": True,
                        "message": "Monitoring already active",
                    }

            elif command == "stop_monitoring":
                # Stop daemon
                success = self.stop_daemon()
                return {
                    "type": "command_response",
                    "command": command,
                    "success": success,
                    "message": "Monitoring stopped" if success else "Failed to stop monitoring",
                }

            elif command == "unlock_screen":
                # Direct unlock command from JARVIS
                logger.info("Received unlock_screen command from JARVIS")

                # Check if this includes voice authentication data
                audio_data = parameters.get("audio_data")
                context_data = parameters.get("context")

                if audio_data and self.intelligent_service:
                    # Voice-authenticated unlock with full intelligence
                    logger.info("🎤 Processing voice-authenticated unlock")
                    result = await self.handle_voice_authenticated_unlock(audio_data, context_data)
                    return {"type": "command_response", "command": command, **result}
                else:
                    # Fallback to direct unlock (legacy mode)
                    logger.info("⚠️  Direct unlock (no voice authentication)")

                    # Check if password is stored
                    password = self.retrieve_keychain_password()
                    if not password:
                        return {
                            "type": "command_response",
                            "command": command,
                            "success": False,
                            "message": "No password stored. Run enable_screen_unlock.sh first.",
                        }

                    # Perform unlock
                    success = await self.perform_screen_unlock(password)
                    return {
                        "type": "command_response",
                        "command": command,
                        "success": success,
                        "message": "Screen unlocked" if success else "Failed to unlock screen",
                    }

            elif command == "voice_unlock":
                # New command specifically for voice-authenticated unlock
                logger.info("🎤 Received voice_unlock command with audio data")

                audio_data = parameters.get("audio_data")
                context_data = parameters.get("context")

                if not audio_data:
                    return {
                        "type": "error",
                        "success": False,
                        "message": "No audio data provided for voice unlock",
                    }

                if not self.intelligent_service:
                    return {
                        "type": "error",
                        "success": False,
                        "message": "Intelligent voice unlock service not available",
                    }

                # Process voice-authenticated unlock
                result = await self.handle_voice_authenticated_unlock(audio_data, context_data)
                return {"type": "command_response", "command": command, **result}

            elif command == "lock_screen":
                # Lock screen command from JARVIS
                logger.info("Received lock_screen command from JARVIS")
                
                audio_data = parameters.get("audio_data")
                context_data = parameters.get("context") or {}
                
                # Check for VBI verification if audio is provided
                if audio_data and self.intelligent_service and self.intelligent_service.voice_biometric_intelligence:
                    logger.info("🎤 Processing voice-authenticated lock")
                    try:
                        # Verify voice using VBI
                        vbi_result = await self.intelligent_service.voice_biometric_intelligence.verify_and_announce(
                            audio_data=audio_data,
                            context={
                                'action': 'lock',
                                'device_trusted': True,
                                **context_data
                            },
                            speak=False  # Don't announce "Voice verified" for locking, just do it
                        )
                        
                        if not vbi_result.verified:
                            logger.warning(f"Voice verification failed for lock: {vbi_result.confidence:.1%}")
                            # Optional: We could enforce strict checking here. 
                            # For now, we'll log it. If the user wants strict security, we can return failure.
                            # Given "beef it up", let's enforce it if audio was sent.
                            return {
                                "type": "command_response",
                                "command": command,
                                "success": False,
                                "message": f"Voice verification failed ({vbi_result.confidence:.0%})",
                            }
                            
                        logger.info(f"✅ Voice verified for lock: {vbi_result.speaker_name}")
                        
                    except Exception as e:
                        logger.error(f"VBI check failed: {e}")
                        # Fallback to allowing lock if VBI errors? 
                        # Or fail safe? "Robust" implies working.
                        # We'll log and proceed to lock to avoid getting stuck due to VBI error.
                
                # Perform async lock
                success = await self.perform_screen_lock()
                
                return {
                    "type": "command_response",
                    "command": command,
                    "success": success,
                    "message": "Screen locked" if success else "Failed to lock screen",
                }

            else:
                return {"type": "error", "message": f"Unknown command: {command}", "success": False}

        return {"type": "error", "message": f"Unknown message type: {msg_type}", "success": False}

    async def handle_voice_authenticated_unlock(
        self, audio_data: bytes, context_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Handle voice-authenticated unlock with full intelligence stack.

        This uses:
        - Hybrid STT for transcription
        - Speaker recognition for identity
        - Database recording for learning
        - CAI/SAI for context awareness
        """
        if not self.intelligent_service:
            return {"success": False, "message": "Intelligent service not initialized"}

        try:
            # Process voice unlock through intelligent service
            result = await self.intelligent_service.process_voice_unlock_command(
                audio_data=audio_data, context=context_data
            )

            return result

        except Exception as e:
            logger.error(f"Voice authenticated unlock failed: {e}")
            import traceback

            traceback.print_exc()

            return {"success": False, "message": f"Voice unlock error: {str(e)}"}

    def check_daemon_running(self) -> bool:
        """Check if the Voice Unlock daemon is running"""
        try:
            result = subprocess.run(["pgrep", "-f", "JARVISVoiceUnlockDaemon"], capture_output=True)
            return result.returncode == 0
        except Exception:
            return False

    def get_enrolled_users(self) -> list:
        """Get list of enrolled user names"""
        try:
            if os.path.exists(self.enrolled_users_file):
                with open(self.enrolled_users_file) as f:
                    users = json.load(f)
                    return [user.get("name", "unknown") for user in users.values()]
        except Exception:
            pass
        return []

    def start_daemon(self) -> bool:
        """Start the Voice Unlock daemon"""
        try:
            daemon_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "bin/JARVISVoiceUnlockDaemon",
            )

            if not os.path.exists(daemon_path):
                logger.error(f"Daemon not found at {daemon_path}")
                return False

            self.daemon_process = subprocess.Popen(
                [daemon_path, "--debug"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            # Give it time to start
            asyncio.get_event_loop().call_later(1.0, self.check_daemon_started)

            return True
        except Exception as e:
            logger.error(f"Failed to start daemon: {e}")
            return False

    def check_daemon_started(self):
        """Check if daemon started successfully"""
        if self.daemon_process and self.daemon_process.poll() is None:
            logger.info("Daemon started successfully")
        else:
            logger.error("Daemon failed to start")

    def stop_daemon(self) -> bool:
        """Stop the Voice Unlock daemon"""
        try:
            subprocess.run(["pkill", "-f", "JARVISVoiceUnlockDaemon"])
            return True
        except Exception:
            return False

    async def websocket_handler(self, websocket, path):
        """Handle WebSocket connections"""
        logger.info(f"New connection on path: {path}")
        await self.register_client(websocket)

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    response = await self.handle_message(websocket, data)
                    await websocket.send(json.dumps(response))

                except json.JSONDecodeError:
                    error_response = {"type": "error", "message": "Invalid JSON", "success": False}
                    await websocket.send(json.dumps(error_response))

                except Exception as e:
                    logger.error(f"Error handling message: {e}")
                    error_response = {"type": "error", "message": str(e), "success": False}
                    await websocket.send(json.dumps(error_response))

        except websockets.exceptions.ConnectionClosed:
            pass

        finally:
            await self.unregister_client(websocket)

    def retrieve_keychain_password(self) -> Optional[str]:
        """Retrieve the stored password from macOS Keychain"""
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "com.jarvis.voiceunlock",
                    "-a",
                    "unlock_token",
                    "-w",  # Print only the password
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                return result.stdout.strip()
            else:
                logger.error(f"Failed to retrieve password: {result.stderr}")
                return None
        except Exception as e:
            logger.error(f"Error retrieving password: {e}")
            return None

    def escape_password_for_applescript(self, password: str) -> str:
        """Escape special characters in password for AppleScript"""
        # For AppleScript strings, we only need to escape quotes and backslashes
        # The password will be typed character by character, not evaluated as a string
        escaped = password.replace("\\", "\\\\")  # Escape backslashes
        escaped = escaped.replace('"', '\\"')  # Escape double quotes
        # Don't escape other characters as they'll be typed literally
        return escaped

    async def perform_screen_unlock(self, password: str) -> bool:
        """Perform the actual screen unlock using AppleScript"""
        try:
            # Wake the display first
            logger.info("Waking display...")
            subprocess.run(["caffeinate", "-u", "-t", "1"])
            await asyncio.sleep(1)

            # Move mouse to wake screen and ensure loginwindow is active
            wake_script = """
            tell application "System Events"
                -- Wake the display by moving mouse
                do shell script "caffeinate -u -t 2"
                delay 0.5

                -- Click on the user profile to show password field
                -- This is more reliable than keyboard navigation
                click at {720, 860}
                delay 1

                -- Make sure loginwindow is frontmost
                set frontmost of process "loginwindow" to true
                delay 0.5

                -- Sometimes need to click again to ensure password field is active
                click at {720, 500}
                delay 0.5

                -- Clear any existing text
                keystroke "a" using command down
                delay 0.1
                key code 51
                delay 0.2
            end tell
            """

            subprocess.run(["osascript", "-e", wake_script])
            await asyncio.sleep(0.5)

            # Escape password for AppleScript
            self.escape_password_for_applescript(password)
            logger.info("Password escaped for AppleScript input")

            # Type password and press return
            # Type the password using System Events
            logger.info(f"Typing password with {len(password)} characters")

            # Clear any existing text first
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to keystroke "a" using command down',
                ]
            )
            await asyncio.sleep(0.1)
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to key code 51']  # Delete key
            )
            await asyncio.sleep(0.2)

            # Type password character by character with proper special character handling
            logger.info(f"Typing password with {len(password)} characters")

            # Map special characters to their key codes with modifiers
            special_char_map = {
                "!": {"keycode": 18, "modifiers": "shift down"},  # Shift+1
                "@": {"keycode": 19, "modifiers": "shift down"},  # Shift+2
                "#": {"keycode": 20, "modifiers": "shift down"},  # Shift+3
                "$": {"keycode": 21, "modifiers": "shift down"},  # Shift+4
                "%": {"keycode": 22, "modifiers": "shift down"},  # Shift+5
                "^": {"keycode": 23, "modifiers": "shift down"},  # Shift+6
                "&": {"keycode": 24, "modifiers": "shift down"},  # Shift+7
                "*": {"keycode": 25, "modifiers": "shift down"},  # Shift+8
                "(": {"keycode": 26, "modifiers": "shift down"},  # Shift+9
                ")": {"keycode": 27, "modifiers": "shift down"},  # Shift+0
            }

            # Type each character
            for i, char in enumerate(password):
                if char in special_char_map:
                    # Use key code for special characters
                    info = special_char_map[char]
                    script = f'tell application "System Events" to key code {info["keycode"]} using {{{info["modifiers"]}}}'
                    logger.info(
                        f"Typing special char at position {i+1}: [special] using keycode {info['keycode']} with shift"
                    )
                else:
                    # Escape the character for AppleScript
                    if char == '"':
                        escaped_char = '\\"'
                    elif char == "\\":
                        escaped_char = "\\\\"
                    else:
                        escaped_char = char
                    # Use keystroke for regular characters
                    script = f'tell application "System Events" to keystroke "{escaped_char}"'
                    logger.info(f"Typing regular char at position {i+1}")

                result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
                if result.returncode != 0:
                    logger.error(f"Failed to type character at position {i+1}: {result.stderr}")
                await asyncio.sleep(0.01)  # Faster typing - reduced from 0.05 to 0.01

            # Press return
            await asyncio.sleep(0.2)
            logger.info("Pressing return key...")
            result = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to key code 36'],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                logger.info("Screen unlock command executed successfully")
                return True
            else:
                logger.error(f"Screen unlock failed: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Error performing screen unlock: {e}")
            return False

    async def _run_command_async(self, cmd_list: list) -> tuple[int, str, str]:
        """Run a command asynchronously without blocking the event loop"""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            return process.returncode, stdout.decode(), stderr.decode()
        except Exception as e:
            logger.error(f"Async command failed {cmd_list}: {e}")
            return -1, "", str(e)

    async def perform_screen_lock(self) -> bool:
        """Lock the Mac screen using various methods (Async & Non-blocking)"""
        try:
            logger.info("Locking screen (Async)...")

            # Method 1: Use CGSession (most reliable)
            try:
                returncode, _, _ = await self._run_command_async([
                    "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession",
                    "-suspend"
                ])
                
                if returncode == 0:
                    logger.info("Screen locked successfully using CGSession")
                    return True
            except Exception as e:
                logger.debug(f"CGSession method failed: {e}")

            # Method 2: Use loginwindow
            try:
                returncode, _, _ = await self._run_command_async([
                    "osascript",
                    "-e",
                    'tell application "System Events" to tell process "loginwindow" to keystroke "q" using {command down, control down}'
                ])
                
                if returncode == 0:
                    logger.info("Screen locked successfully using loginwindow")
                    return True
            except Exception as e:
                logger.debug(f"Loginwindow method failed: {e}")

            # Method 3: Use ScreenSaverEngine
            try:
                # Start screensaver which will require authentication on wake
                returncode, _, _ = await self._run_command_async(["open", "-a", "ScreenSaverEngine"])
                if returncode == 0:
                    logger.info("Started screensaver (will lock if authentication required)")
                    return True
            except Exception as e:
                logger.debug(f"ScreenSaver method failed: {e}")

            logger.error("All lock methods failed")
            return False

        except Exception as e:
            logger.error(f"Error locking screen: {e}")
            return False

    async def start(self):
        """Start the WebSocket server"""
        logger.info(f"Starting Voice Unlock WebSocket server on port {self.port}")

        async with websockets.serve(self.websocket_handler, "localhost", self.port):
            logger.info(f"Server listening on ws://localhost:{self.port}")
            await asyncio.Future()  # Run forever


def main():
    """Main entry point"""
    server = VoiceUnlockWebSocketServer(port=8765)
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
