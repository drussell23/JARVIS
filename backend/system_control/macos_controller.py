#!/usr/bin/env python3
"""
macOS System Controller for JARVIS AI Agent
Provides voice-activated control of macOS environment through natural language commands
"""

import asyncio
import logging
import os
import re
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil

# Import async pipeline for non-blocking operations
from core.async_pipeline import get_async_pipeline

logger = logging.getLogger(__name__)


class CommandCategory(Enum):
    """Categories of system commands"""

    APPLICATION = "application"
    FILE = "file"
    SYSTEM = "system"
    WEB = "web"
    WORKFLOW = "workflow"
    VISION = "vision"
    DANGEROUS = "dangerous"
    UNKNOWN = "unknown"


class SafetyLevel(Enum):
    """Safety levels for commands"""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    FORBIDDEN = "forbidden"


class MacOSController:
    """Controls macOS system operations with safety checks"""

    def __init__(self):
        self.home_dir = Path.home()
        self.safe_directories = [
            self.home_dir / "Desktop",
            self.home_dir / "Documents",
            self.home_dir / "Downloads",
            self.home_dir / "Pictures",
            self.home_dir / "Music",
            self.home_dir / "Movies",
        ]
        self._screen_lock_checked = False
        self._is_locked = False

        # Blocked applications for safety
        self.blocked_apps = {
            "System Preferences",
            "System Settings",
            "Activity Monitor",
            "Terminal",
            "Console",
            "Disk Utility",
            "Keychain Access",
        }

        # Common application mappings
        self.app_aliases = {
            "chrome": "Google Chrome",
            "firefox": "Firefox",
            "safari": "Safari",
            "whatsapp": "WhatsApp",
            "whatsapp desktop": "WhatsApp",
            "spotify": "Spotify",
            "slack": "Slack",
            "zoom": "zoom.us",
            "vscode": "Visual Studio Code",
            "code": "Visual Studio Code",
            "mail": "Mail",
            "calendar": "Calendar",
            "finder": "Finder",
            "messages": "Messages",
            "notes": "Notes",
            "preview": "Preview",
            "terminal": "Terminal",
            "music": "Music",
            "photos": "Photos",
            "pages": "Pages",
            "numbers": "Numbers",
            "keynote": "Keynote",
        }

        # Initialize async pipeline for non-blocking operations
        self.pipeline = get_async_pipeline()
        self._register_pipeline_stages()

    def _register_pipeline_stages(self):
        """Register async pipeline stages for system operations"""

        # AppleScript execution stage
        self.pipeline.register_stage(
            "applescript_execution",
            self._execute_applescript_async,
            timeout=5.0,
            retry_count=2,
            required=True,
        )

        # Shell command execution stage
        self.pipeline.register_stage(
            "shell_execution", self._execute_shell_async, timeout=30.0, retry_count=1, required=True
        )

        # Application control stage
        self.pipeline.register_stage(
            "app_control", self._app_control_async, timeout=15.0, retry_count=2, required=True
        )

        # Screen unlock WebSocket stage
        self.pipeline.register_stage(
            "screen_unlock_ws",
            self._screen_unlock_ws_async,
            timeout=20.0,
            retry_count=1,
            required=False,
        )

        # Screen unlock AppleScript stage (fallback)
        self.pipeline.register_stage(
            "screen_unlock_applescript",
            self._screen_unlock_applescript_async,
            timeout=15.0,
            retry_count=1,
            required=False,
        )

    async def _execute_applescript_async(self, context):
        """Non-blocking AppleScript execution via async pipeline"""
        from api.jarvis_voice_api import async_osascript

        script = context.metadata.get("script", "")
        timeout = context.metadata.get("timeout", 10.0)

        stdout, stderr, returncode = await async_osascript(script, timeout=timeout)

        context.metadata["returncode"] = returncode
        context.metadata["stdout"] = stdout.decode() if stdout else ""
        context.metadata["stderr"] = stderr.decode() if stderr else ""
        context.metadata["success"] = returncode == 0

    async def _execute_shell_async(self, context):
        """Non-blocking shell command execution via async pipeline"""
        import shlex

        from api.jarvis_voice_api import async_subprocess_run

        command = context.metadata.get("command", "")
        timeout = context.metadata.get("timeout", 30.0)
        safe_mode = context.metadata.get("safe_mode", True)

        # Safety checks if safe_mode is enabled
        if safe_mode:
            dangerous_patterns = [
                r"rm\s+-rf",
                r"sudo",
                r"dd\s+",
                r"mkfs",
                r"format",
                r">\s*/dev/",
                r"chmod\s+777",
                r"pkill",
                r"killall",
            ]
            for pattern in dangerous_patterns:
                if re.search(pattern, command, re.IGNORECASE):
                    context.metadata["success"] = False
                    context.metadata["error"] = f"Blocked dangerous command pattern: {pattern}"
                    return

        # Convert command string to list using shlex (handles spaces and quotes properly)
        try:
            cmd_list = shlex.split(command)
        except ValueError as e:
            context.metadata["success"] = False
            context.metadata["error"] = f"Invalid command format: {e}"
            return

        stdout, stderr, returncode = await async_subprocess_run(cmd_list, timeout=timeout)

        context.metadata["returncode"] = returncode
        context.metadata["stdout"] = stdout.decode() if stdout else ""
        context.metadata["stderr"] = stderr.decode() if stderr else ""
        context.metadata["success"] = returncode == 0

    async def _app_control_async(self, context):
        """Non-blocking application control via async pipeline"""
        action = context.metadata.get("action", "")
        app_name = context.metadata.get("app_name", "")

        if action == "open":
            script = f'tell application "{app_name}" to activate'
        elif action == "close":
            script = f'tell application "{app_name}" to quit'
        elif action == "switch":
            script = (
                f'tell application "System Events" to set frontmost of process "{app_name}" to true'
            )
        else:
            context.metadata["success"] = False
            context.metadata["error"] = f"Unknown action: {action}"
            return

        context.metadata["script"] = script
        await self._execute_applescript_async(context)

    async def _screen_unlock_ws_async(self, context):
        """Non-blocking screen unlock via WebSocket to voice unlock daemon"""
        try:
            import json

            import websockets

            VOICE_UNLOCK_WS_URL = "ws://localhost:8765/voice-unlock"

            async with websockets.connect(VOICE_UNLOCK_WS_URL, ping_interval=20) as websocket:
                # Send unlock command using the daemon's expected format
                unlock_command = {"type": "command", "command": "unlock_screen"}

                await websocket.send(json.dumps(unlock_command))
                logger.info("[Pipeline] Sent unlock command to voice unlock daemon")

                # Wait for response (longer timeout for unlock)
                response = await asyncio.wait_for(websocket.recv(), timeout=15.0)
                result = json.loads(response)

                if (
                    result.get("type") == "command_response"
                    or result.get("type") == "unlock_result"
                ):
                    if result.get("success"):
                        context.metadata["success"] = True
                        context.metadata["message"] = "Screen unlocked successfully via daemon"
                        logger.info(
                            "[Pipeline] Screen unlocked successfully via voice unlock daemon"
                        )
                    else:
                        context.metadata["success"] = False
                        context.metadata["error"] = result.get("message", "Unable to unlock screen")
                else:
                    context.metadata["success"] = False
                    context.metadata["error"] = "Unexpected response from daemon"

        except (ConnectionRefusedError, OSError) as e:
            context.metadata["success"] = False
            context.metadata["error"] = "Voice unlock daemon not running"
            logger.warning(f"[Pipeline] Voice unlock daemon not available: {e}")
        except asyncio.TimeoutError:
            context.metadata["success"] = False
            context.metadata["error"] = "Unlock operation timed out"
            logger.error("[Pipeline] Timeout waiting for unlock response")
        except Exception as e:
            context.metadata["success"] = False
            context.metadata["error"] = str(e)
            logger.error(f"[Pipeline] Unlock WebSocket error: {e}")

    async def _screen_unlock_applescript_async(self, context):
        """Non-blocking screen unlock via AppleScript (fallback)"""
        password = context.metadata.get("password")

        if not password:
            # Try to retrieve from keychain
            try:
                from api.jarvis_voice_api import async_subprocess_run

                stdout, stderr, returncode = await async_subprocess_run(
                    [
                        "security",
                        "find-generic-password",
                        "-s",
                        "com.jarvis.voiceunlock",
                        "-a",
                        "unlock_token",
                        "-w",
                    ],
                    timeout=5.0,
                )
                if returncode == 0 and stdout:
                    password = stdout.decode().strip()
                else:
                    context.metadata["success"] = False
                    context.metadata["error"] = "No password available for unlock"
                    return
            except Exception as e:
                context.metadata["success"] = False
                context.metadata["error"] = f"Failed to retrieve password: {e}"
                return

        # Use simple_unlock_handler's direct unlock method
        try:
            from api.simple_unlock_handler import _perform_direct_unlock

            success = await _perform_direct_unlock(password)

            if success:
                context.metadata["success"] = True
                context.metadata["message"] = "Screen unlocked successfully via AppleScript"
            else:
                context.metadata["success"] = False
                context.metadata["error"] = "AppleScript unlock failed"
        except Exception as e:
            context.metadata["success"] = False
            context.metadata["error"] = f"AppleScript unlock error: {e}"

    def _check_screen_lock_status(self) -> bool:
        """
        Check if screen is currently locked

        Returns:
            bool: True if screen is locked, False otherwise
        """
        try:
            from voice_unlock.objc.server.screen_lock_detector import is_screen_locked

            self._is_locked = is_screen_locked()
            self._screen_lock_checked = True
            return self._is_locked
        except Exception as e:
            logger.debug(f"Could not check screen lock status: {e}")
            return False

    def _handle_locked_screen_command(self, command_type: str) -> Tuple[bool, str]:
        """
        Handle commands when screen is locked

        Args:
            command_type: Type of command being attempted

        Returns:
            Tuple of (should_proceed, message)
        """
        # Commands that should work when locked
        allowed_when_locked = {"unlock_screen", "lock_screen", "get_status", "check_time"}

        if command_type in allowed_when_locked:
            return True, ""

        # For other commands, inform user and suggest unlock
        return (
            False,
            f"Your screen is locked, Sir. I cannot execute {command_type} commands while locked. Would you like me to unlock your screen first?",
        )

    async def execute_applescript_pipeline(
        self, script: str, timeout: float = 10.0
    ) -> Tuple[bool, str]:
        """Execute AppleScript through async pipeline (NEW METHOD)"""
        try:
            result = await self.pipeline.process_async(
                text=f"Execute AppleScript",
                metadata={"script": script, "timeout": timeout, "stage": "applescript_execution"},
            )

            metadata = result.get("metadata", {})
            success = metadata.get("success", False)
            stdout = metadata.get("stdout", "")
            stderr = metadata.get("stderr", "")

            return (success, stdout if success else stderr)

        except Exception as e:
            logger.error(f"Pipeline AppleScript execution failed: {e}")
            return False, str(e)

    def execute_applescript(self, script: str) -> Tuple[bool, str]:
        """Execute AppleScript (LEGACY - synchronous fallback without pipeline)"""
        # Direct execution without pipeline to avoid timeouts
        import subprocess

        try:
            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            else:
                return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "AppleScript execution timed out"
        except Exception as e:
            return False, str(e)

    async def execute_shell_pipeline(
        self, command: str, safe_mode: bool = True, timeout: float = 30.0
    ) -> Tuple[bool, str]:
        """Execute shell command through async pipeline (NEW METHOD)"""
        try:
            result = await self.pipeline.process_async(
                text=f"Execute shell command",
                metadata={
                    "command": command,
                    "safe_mode": safe_mode,
                    "timeout": timeout,
                    "stage": "shell_execution",
                },
            )

            metadata = result.get("metadata", {})

            # Check if command was blocked
            if "error" in metadata:
                return False, metadata["error"]

            success = metadata.get("success", False)
            stdout = metadata.get("stdout", "")
            stderr = metadata.get("stderr", "")

            return (success, stdout if success else stderr)

        except Exception as e:
            logger.error(f"Pipeline shell execution failed: {e}")
            return False, str(e)

    async def execute_shell(self, command: str, safe_mode: bool = True) -> Tuple[bool, str]:
        """Execute shell command (async)"""
        # Directly call async pipeline - no need for run_until_complete
        return await self.execute_shell_pipeline(command, safe_mode)

    # Application Control Methods

    async def open_application_pipeline(self, app_name: str) -> Tuple[bool, str]:
        """Open application through async pipeline (NEW METHOD)"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("open_application")
            if not should_proceed:
                return False, message

        # Resolve aliases
        app_name = self.app_aliases.get(app_name.lower(), app_name)

        # Check if blocked
        if app_name in self.blocked_apps:
            return False, f"Opening {app_name} is blocked for safety"

        try:
            # Use async pipeline for app control
            result = await self.pipeline.process_async(
                text=f"Open application {app_name}",
                metadata={"action": "open", "app_name": app_name, "stage": "app_control"},
            )

            metadata = result.get("metadata", {})
            success = metadata.get("success", False)

            if success:
                return True, f"Opening {app_name}, Sir"
            else:
                # Try alternative method through shell pipeline
                return await self.execute_shell_pipeline(f"open -a '{app_name}'")

        except Exception as e:
            logger.error(f"Failed to open {app_name}: {e}")
            return False, f"I'm unable to open {app_name}, Sir"

    def open_application(self, app_name: str) -> Tuple[bool, str]:
        """Open an application directly without pipeline (for system commands)"""
        # Resolve aliases
        app_name = self.app_aliases.get(app_name.lower(), app_name)

        # Check if blocked
        if app_name in self.blocked_apps:
            return False, f"Opening {app_name} is blocked for safety"

        # Direct AppleScript to open application
        script = f'tell application "{app_name}" to activate'
        success, message = self.execute_applescript(script)

        if success:
            return True, f"Opening {app_name}, Sir"
        else:
            # Fallback: try with 'open' command
            import subprocess

            try:
                result = subprocess.run(
                    ["open", "-a", app_name], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    return True, f"Opening {app_name}, Sir"
                else:
                    return False, f"Couldn't find application: {app_name}"
            except Exception as e:
                return False, f"Error opening {app_name}: {str(e)}"

    def close_application(self, app_name: str) -> Tuple[bool, str]:
        """Close an application gracefully"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("close_application")
            if not should_proceed:
                return False, message

        app_name = self.app_aliases.get(app_name.lower(), app_name)

        # First try the standard quit command
        script = f'tell application "{app_name}" to quit'
        success, message = self.execute_applescript(script)

        if success:
            return True, f"Closing {app_name}"

        # If that fails, try using System Events
        script = f"""
        tell application "System Events"
            if exists process "{app_name}" then
                tell process "{app_name}"
                    set frontmost to true
                    keystroke "q" using command down
                end tell
                return "Closed using keyboard shortcut"
            else
                return "Application not running"
            end if
        end tell
        """
        success, message = self.execute_applescript(script)

        if success:
            return True, f"Closing {app_name}"

        # Final attempt: Force quit if necessary
        # But only for non-system apps
        if app_name not in ["Finder", "System Preferences", "System Settings"]:
            success, message = self.execute_shell(f"pkill -x '{app_name}'", safe_mode=False)
            if success:
                return True, f"Force closed {app_name}"

        return False, f"Failed to close {app_name}: {message}"

    def switch_to_application(self, app_name: str) -> Tuple[bool, str]:
        """Switch to an already open application"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("switch_to_application")
            if not should_proceed:
                return False, message

        app_name = self.app_aliases.get(app_name.lower(), app_name)

        script = f"""
        tell application "System Events"
            set frontmost of process "{app_name}" to true
        end tell
        """
        success, message = self.execute_applescript(script)

        if success:
            return True, f"Switched to {app_name}"
        return False, f"Failed to switch to {app_name}: {message}"

    def list_open_applications(self) -> List[str]:
        """Get list of currently open applications"""
        script = """
        tell application "System Events"
            get name of (every process whose background only is false)
        end tell
        """
        success, output = self.execute_applescript(script)

        if success:
            apps = output.split(", ")
            return [app.strip() for app in apps if app.strip()]
        return []

    def minimize_all_windows(self) -> Tuple[bool, str]:
        """Minimize all windows"""
        script = """
        tell application "System Events"
            set visible of every process to false
        end tell
        """
        return self.execute_applescript(script)

    def activate_mission_control(self) -> Tuple[bool, str]:
        """Activate Mission Control"""
        script = """
        tell application "Mission Control" to launch
        """
        return self.execute_applescript(script)

    # File Operations

    def is_safe_path(self, path: Path) -> bool:
        """Check if a path is in a safe directory"""
        path = path.resolve()
        return any(path.is_relative_to(safe_dir) for safe_dir in self.safe_directories)

    def open_file(self, file_path: str) -> Tuple[bool, str]:
        """Open a file with its default application"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("open_file")
            if not should_proceed:
                return False, message

        path = Path(file_path).expanduser()

        if not path.exists():
            return False, f"File not found: {file_path}"

        if not self.is_safe_path(path):
            return False, f"Access to {file_path} is restricted for safety"

        success, message = self.execute_shell(f"open '{path}'")
        if success:
            return True, f"Opened {path.name}"
        return False, f"Failed to open file: {message}"

    def create_file(self, file_path: str, content: str = "") -> Tuple[bool, str]:
        """Create a new file"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("create_file")
            if not should_proceed:
                return False, message

        path = Path(file_path).expanduser()

        if not self.is_safe_path(path):
            return False, f"Cannot create file in restricted directory"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return True, f"Created file: {path.name}"
        except Exception as e:
            return False, f"Failed to create file: {str(e)}"

    def delete_file(self, file_path: str, confirm: bool = True) -> Tuple[bool, str]:
        """Delete a file (requires confirmation)"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("delete_file")
            if not should_proceed:
                return False, message

        path = Path(file_path).expanduser()

        if not path.exists():
            return False, f"File not found: {file_path}"

        if not self.is_safe_path(path):
            return False, f"Cannot delete file in restricted directory"

        if confirm:
            # In real implementation, this would trigger a confirmation dialog
            return False, "File deletion requires user confirmation"

        try:
            path.unlink()
            return True, f"Deleted file: {path.name}"
        except Exception as e:
            return False, f"Failed to delete file: {str(e)}"

    def search_files(self, query: str, directory: Optional[str] = None) -> List[str]:
        """Search for files using Spotlight"""
        if directory:
            path = Path(directory).expanduser()
            if not self.is_safe_path(path):
                return []
            search_cmd = f"mdfind -onlyin '{path}' '{query}'"
        else:
            search_cmd = f"mdfind '{query}'"

        success, output = self.execute_shell(search_cmd)

        if success:
            files = output.strip().split("\n")
            # Filter to only safe paths
            return [f for f in files if f and self.is_safe_path(Path(f))]
        return []

    # System Settings Control

    async def set_volume_async(self, level: int) -> Tuple[bool, str]:
        """Set system volume (0-100) - ASYNC VERSION for better performance"""
        from api.jarvis_voice_api import async_osascript

        level = max(0, min(100, level))
        script = f"set volume output volume {level}"
        stdout, stderr, returncode = await async_osascript(script, timeout=5.0)

        if returncode == 0:
            return True, f"Setting volume to {level}%"
        return False, "I couldn't adjust the volume"

    def set_volume(self, level: int) -> Tuple[bool, str]:
        """Set system volume (0-100) - LEGACY sync version"""
        level = max(0, min(100, level))
        script = f"set volume output volume {level}"
        success, _ = self.execute_applescript(script)

        if success:
            return True, f"Setting volume to {level}%"
        return False, "I couldn't adjust the volume"

    async def mute_volume_async(self, mute: bool = True) -> Tuple[bool, str]:
        """Mute or unmute system volume - ASYNC VERSION for better performance"""
        from api.jarvis_voice_api import async_osascript

        script = f"set volume output muted {str(mute).lower()}"
        stdout, stderr, returncode = await async_osascript(script, timeout=5.0)

        if returncode == 0:
            state = "muted" if mute else "unmuted"
            return True, f"Volume {state}"
        return False, "Failed to change mute state"

    def mute_volume(self, mute: bool = True) -> Tuple[bool, str]:
        """Mute or unmute system volume - LEGACY sync version"""
        script = f"set volume output muted {str(mute).lower()}"
        success, _ = self.execute_applescript(script)

        if success:
            state = "muted" if mute else "unmuted"
            return True, f"Volume {state}"
        return False, "Failed to change mute state"

    def adjust_brightness(self, level: float) -> Tuple[bool, str]:
        """Adjust screen brightness (0.0-1.0)"""
        # This requires additional setup with brightness control tools
        return False, "Brightness control requires additional setup"

    def toggle_wifi(self, enable: bool) -> Tuple[bool, str]:
        """Toggle WiFi on/off"""
        action = "on" if enable else "off"
        success, message = self.execute_shell(f"networksetup -setairportpower airport {action}")

        if success:
            return True, f"WiFi turned {action}"
        return False, f"Failed to toggle WiFi: {message}"

    def take_screenshot(self, save_path: Optional[str] = None) -> Tuple[bool, str]:
        """Take a screenshot"""
        if save_path:
            path = Path(save_path).expanduser()
            if not self.is_safe_path(path.parent):
                return False, "Cannot save screenshot to restricted directory"
            cmd = f"screencapture '{path}'"
        else:
            # Save to desktop with timestamp
            from datetime import datetime

            filename = f"Screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = self.home_dir / "Desktop" / filename
            cmd = f"screencapture '{path}'"

        success, message = self.execute_shell(cmd)

        if success:
            return True, f"Screenshot saved to {path.name}"
        return False, f"Failed to take screenshot: {message}"

    def sleep_display(self) -> Tuple[bool, str]:
        """Put display to sleep"""
        success, message = self.execute_shell("pmset displaysleepnow")

        if success:
            return True, "Display sleeping"
        return False, f"Failed to sleep display: {message}"

    async def click_at(self, x: int, y: int) -> Tuple[bool, str]:
        """Click at specific coordinates"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("click_at")
            if not should_proceed:
                return False, message

        try:
            # Use AppleScript to click at coordinates
            script = f"""
            tell application "System Events"
                click at {{{x}, {y}}}
            end tell
            """
            success, result = self.execute_applescript(script)
            if success:
                return True, f"Clicked at ({x}, {y})"
            else:
                # Fallback: Use cliclick if available
                try:
                    subprocess.run(["cliclick", f"c:{x},{y}"], check=True, capture_output=True)
                    return True, f"Clicked at ({x}, {y})"
                except:
                    return False, f"Failed to click: {result}"
        except Exception as e:
            return False, f"Click error: {str(e)}"

    async def click_and_hold(self, x: int, y: int, hold_duration: float = 0.2) -> Tuple[bool, str]:
        """Click and hold at specific coordinates (simulates human press-and-hold)"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("click_and_hold")
            if not should_proceed:
                return False, message

        try:
            # Try cliclick first for more reliable click-and-hold
            try:
                # Mouse down
                subprocess.run(["cliclick", f"dd:{x},{y}"], check=True, capture_output=True)
                # Hold
                await asyncio.sleep(hold_duration)
                # Mouse up
                subprocess.run(["cliclick", f"du:{x},{y}"], check=True, capture_output=True)
                return True, f"Click and hold at ({x}, {y}) for {hold_duration}s"
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Fallback to AppleScript
                script = f"""
                tell application "System Events"
                    -- First click
                    click at {{{x}, {y}}}
                    delay {hold_duration}
                    -- Click again to ensure selection
                    click at {{{x}, {y}}}
                end tell
                """
                success, result = self.execute_applescript(script)
                if success:
                    return True, f"Click and hold at ({x}, {y})"
                else:
                    return False, f"Failed to click and hold: {result}"
        except Exception as e:
            logger.error(f"Click and hold at {x},{y} failed: {e}")
            return False, str(e)

    async def key_press(self, key: str) -> Tuple[bool, str]:
        """Press a keyboard key"""
        try:
            # Map key names to AppleScript key codes
            key_map = {
                "up": "key code 126",
                "down": "key code 125",
                "left": "key code 123",
                "right": "key code 124",
                "return": "key code 36",
                "enter": "key code 36",
                "space": "key code 49",
                "tab": "key code 48",
                "delete": "key code 51",
                "escape": "key code 53",
            }

            # Get the key code or use the key directly
            key_action = key_map.get(key.lower(), f'keystroke "{key}"')

            script = f"""
            tell application "System Events"
                {key_action}
            end tell
            """

            success, result = self.execute_applescript(script)
            return (True, f"Pressed {key}") if success else (False, result)

        except Exception as e:
            return False, f"Key press error: {str(e)}"

    # Web Integration

    def open_new_tab(
        self, browser: Optional[str] = None, url: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Open a new tab in browser"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("open_new_tab")
            if not should_proceed:
                return False, message

        if not browser:
            browser = "Safari"  # Default browser
        browser = self.app_aliases.get(browser.lower(), browser)

        script = f"""
        tell application "{browser}"
            activate
            tell window 1
                set current tab to (make new tab)
            end tell
        """

        if url:
            script += f"""
            set URL of current tab of window 1 to "{url}"
        """

        script += """
        end tell
        """

        success, message = self.execute_applescript(script)
        if success:
            if url:
                from urllib.parse import urlparse

                domain = urlparse(url).netloc.replace("www.", "") if "://" in url else url
                return True, f"Opening new tab and navigating to {domain}"
            else:
                return True, f"Opening new tab in {browser}"
        return False, f"Failed to open new tab: {message}"

    def type_in_browser(
        self, text: str, browser: Optional[str] = None, press_enter: bool = False
    ) -> Tuple[bool, str]:
        """Type text in the active browser element (like search bar)"""
        if not browser:
            browser = "Safari"
        browser = self.app_aliases.get(browser.lower(), browser)

        # First ensure browser is active
        activate_script = f'tell application "{browser}" to activate'
        self.execute_applescript(activate_script)

        # Small delay to ensure browser is ready
        import time

        time.sleep(0.5)

        # Use System Events to type
        script = f"""
        tell application "System Events"
            tell process "{browser}"
                set frontmost to true
                keystroke "{text}"
        """

        if press_enter:
            script += """
                key code 36  -- Enter key
        """

        script += """
            end tell
        end tell
        """

        success, message = self.execute_applescript(script)
        if success:
            if press_enter:
                return True, f"Typing '{text}' and pressing Enter"
            else:
                return True, f"Typing '{text}'"
        return False, f"Failed to type: {message}"

    def click_search_bar(self, browser: Optional[str] = None) -> Tuple[bool, str]:
        """Click on the browser's search/address bar"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("click_search_bar")
            if not should_proceed:
                return False, message

        if not browser:
            browser = "Safari"
        browser = self.app_aliases.get(browser.lower(), browser)

        # Use keyboard shortcut to focus address bar (Cmd+L works in most browsers)
        script = f"""
        tell application "{browser}"
            activate
        end tell
        tell application "System Events"
            tell process "{browser}"
                set frontmost to true
                keystroke "l" using command down
            end tell
        end tell
        """

        success, message = self.execute_applescript(script)
        if success:
            return True, f"Focusing on search bar"
        return False, f"Failed to focus search bar: {message}"

    async def open_url(self, url: str, browser: Optional[str] = None) -> Tuple[bool, str]:
        """Open URL in browser (async)"""
        # Check if screen is locked
        if self._check_screen_lock_status():
            should_proceed, message = self._handle_locked_screen_command("open_url")
            if not should_proceed:
                return False, message

        if browser:
            browser = self.app_aliases.get(browser.lower(), browser)
            # Use AppleScript for better browser control
            script = f"""
            tell application "{browser}"
                activate
                if (count of windows) = 0 then
                    make new document
                end if
                set URL of current tab of front window to "{url}"
            end tell
            """
            success, message = self.execute_applescript(script)
            if success:
                # Make URL response more conversational
                if "google.com/search?q=" in url.lower():
                    # Extract search query for better response
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(url)
                    query_params = parse_qs(parsed.query)
                    search_query = query_params.get("q", [""])[0]
                    if search_query:
                        return True, f"Searching for {search_query}, Sir"
                    else:
                        return True, f"Opening Google search"
                elif "google.com" in url.lower():
                    return True, f"Opening Google, Sir"
                elif "github.com" in url.lower():
                    return True, f"Opening GitHub for you"
                elif "amazon.com" in url.lower():
                    return True, f"Taking you to Amazon"
                else:
                    # For other URLs, simplify the domain
                    from urllib.parse import urlparse

                    domain = urlparse(url).netloc.replace("www.", "")
                    return True, f"Opening {domain} for you"
            else:
                # Fallback to shell command
                cmd = f"open -a '{browser}' '{url}'"
                success, message = await self.execute_shell(cmd)
                if success:
                    # Consistent conversational format
                    if "google.com/search?q=" in url.lower():
                        # Extract search query for better response
                        from urllib.parse import parse_qs, urlparse

                        parsed = urlparse(url)
                        query_params = parse_qs(parsed.query)
                        search_query = query_params.get("q", [""])[0]
                        if search_query:
                            return True, f"searching for {search_query}"
                        else:
                            return True, f"Opening Google search in {browser.title()}"
                    elif "google.com" in url.lower():
                        return True, f"opening Google in {browser}"
                    else:
                        from urllib.parse import urlparse

                        domain = urlparse(url).netloc.replace("www.", "")
                        return True, f"opening {domain} in {browser}"
                return False, f"I couldn't open that URL"
        else:
            cmd = f"open '{url}'"
            success, message = await self.execute_shell(cmd)
            if success:
                # More conversational response for default browser
                if "google.com/search?q=" in url.lower():
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(url)
                    query_params = parse_qs(parsed.query)
                    search_query = query_params.get("q", [""])[0]
                    if search_query:
                        return True, f"Searching for {search_query}, Sir"
                    else:
                        return True, f"Opening Google search"
                elif "google.com" in url.lower():
                    return True, f"Opening Google, Sir"
                else:
                    from urllib.parse import urlparse

                    domain = urlparse(url).netloc.replace("www.", "")
                    return True, f"Navigating to {domain}, Sir"
            return False, f"Failed to open URL: {message}"

    async def web_search(
        self, query: str, engine: str = "google", browser: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Perform web search (async)"""
        engines = {
            "google": f"https://www.google.com/search?q={query}",
            "bing": f"https://www.bing.com/search?q={query}",
            "duckduckgo": f"https://duckduckgo.com/?q={query}",
        }

        url = engines.get(engine.lower(), engines["google"])
        return await self.open_url(url, browser)

    # Complex Workflows

    async def execute_workflow(self, workflow_name: str) -> Tuple[bool, str]:
        """Execute predefined workflows"""
        workflows = {
            "morning_routine": [
                ("open_application", "Mail"),
                ("open_application", "Calendar"),
                ("web_search", "weather today"),
                ("open_url", "https://news.google.com"),
            ],
            "development_setup": [
                ("open_application", "Visual Studio Code"),
                ("open_application", "Terminal"),
                ("open_application", "Docker"),
                ("open_url", "http://localhost:3000"),
            ],
            "meeting_prep": [
                ("set_volume", 50),
                ("close_application", "Spotify"),
                ("minimize_all_windows", None),
                ("open_application", "zoom.us"),
            ],
        }

        if workflow_name not in workflows:
            return False, f"Unknown workflow: {workflow_name}"

        results = []
        for action, param in workflows[workflow_name]:
            method = getattr(self, action)
            if param is not None:
                success, message = method(param)
            else:
                success, message = method()
            results.append(f"{action}: {message}")

            if not success:
                return False, f"Workflow failed at {action}: {message}"

            # Small delay between actions
            await asyncio.sleep(0.5)

        return True, f"Completed {workflow_name} workflow"

    # Utility Methods

    def get_system_info(self) -> Dict[str, Any]:
        """Get system information"""
        info = {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage("/").percent,
            "battery": None,
        }

        if hasattr(psutil, "sensors_battery"):
            battery = psutil.sensors_battery()
            if battery:
                info["battery"] = {"percent": battery.percent, "charging": battery.power_plugged}

        return info

    def validate_command(self, command: str, category: CommandCategory) -> SafetyLevel:
        """Validate command safety level"""
        if category == CommandCategory.DANGEROUS:
            return SafetyLevel.DANGEROUS

        # Check for dangerous patterns
        dangerous_patterns = [
            r"delete.*system",
            r"remove.*all",
            r"format",
            r"shutdown",
            r"restart",
            r"sudo",
            r"admin",
            r"root",
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return SafetyLevel.DANGEROUS

        if category in [CommandCategory.APPLICATION, CommandCategory.WEB]:
            return SafetyLevel.SAFE

        return SafetyLevel.CAUTION

    def find_installed_application(self, partial_name: str) -> Optional[str]:
        """Find installed application by partial name"""
        # Common application directories
        app_dirs = ["/Applications", "~/Applications", "/System/Applications"]

        partial_lower = partial_name.lower()

        for app_dir in app_dirs:
            expanded_dir = os.path.expanduser(app_dir)
            if not os.path.exists(expanded_dir):
                continue

            for app in os.listdir(expanded_dir):
                if app.endswith(".app"):
                    app_name = app[:-4]  # Remove .app extension
                    if partial_lower in app_name.lower():
                        return app_name

        return None

    async def lock_screen(
        self,
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        enable_voice_feedback: bool = True,
        speaker_name: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Lock the macOS screen - optimized for speed with transparent progress
        and Daniel's British voice feedback.

        Args:
            progress_callback: Optional callback for transparent progress updates
            enable_voice_feedback: Enable Daniel's voice narration
            speaker_name: User's name for personalized feedback (auto-detected if None)

        Returns:
            Tuple of (success, message)
        """
        import time
        start_time = time.time()

        # Initialize voice communicator for real-time feedback (non-blocking)
        voice = None
        if enable_voice_feedback:
            try:
                from agi_os.realtime_voice_communicator import get_voice_communicator
                voice = await asyncio.wait_for(get_voice_communicator(), timeout=1.0)
            except Exception as e:
                logger.debug(f"Voice feedback unavailable: {e}")
                voice = None

        # Get dynamic speaker name if not provided
        if speaker_name is None and voice:
            try:
                speaker_name = await asyncio.wait_for(voice.get_owner_name(), timeout=1.0)
            except Exception:
                speaker_name = "there"
        elif speaker_name is None:
            speaker_name = "there"

        async def voice_feedback(stage: str):
            """Fire-and-forget voice feedback."""
            if voice:
                try:
                    asyncio.create_task(
                        voice.vbi_lock_feedback(stage, speaker_name)
                    )
                except Exception as e:
                    logger.debug(f"Voice feedback error: {e}")

        async def _progress(stage: str, pct: int, msg: str):
            """Send transparent progress update."""
            if progress_callback:
                try:
                    data = {
                        "type": "lock_progress",
                        "stage": stage,
                        "progress": pct,
                        "message": msg,
                        "timestamp": time.time()
                    }
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(data)
                    else:
                        progress_callback(data)
                except Exception as e:
                    logger.debug(f"Progress callback error: {e}")

        try:
            # 🎙️ Voice: "Locking the screen, Derek."
            await _progress("init", 10, "Locking screen...")
            await voice_feedback("init")

            # FAST PATH: Go straight to AppleScript (most reliable and fastest)
            from api.jarvis_voice_api import async_osascript

            await _progress("applescript", 30, "Sending lock command...")

            script = 'tell application "System Events" to keystroke "q" using {command down, control down}'
            try:
                stdout, stderr, returncode = await async_osascript(script, timeout=2.0)
                if returncode == 0:
                    duration_ms = (time.time() - start_time) * 1000
                    await _progress("complete", 100, f"Screen locked ({duration_ms:.0f}ms)")
                    # 🎙️ Voice: "Screen locked. See you soon, Derek."
                    await voice_feedback("complete")
                    logger.info(f"[FAST LOCK] Screen locked successfully using AppleScript in {duration_ms:.0f}ms")
                    return True, f"🔒 Locking the screen now, {speaker_name}. See you soon."
            except Exception as e:
                logger.debug(f"AppleScript method failed: {e}")

            # Fallback Method 1: Use CGSession directly (most reliable)
            await _progress("cgsession", 50, "Trying CGSession...")
            from api.jarvis_voice_api import async_subprocess_run

            cgsession_path = (
                "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
            )
            if os.path.exists(cgsession_path):
                # Direct async execution
                stdout, stderr, returncode = await async_subprocess_run(
                    [cgsession_path, "-suspend"], timeout=5.0
                )
                if returncode == 0:
                    duration_ms = (time.time() - start_time) * 1000
                    await _progress("complete", 100, f"Screen locked ({duration_ms:.0f}ms)")
                    await voice_feedback("complete")
                    logger.info(f"Screen locked successfully using CGSession in {duration_ms:.0f}ms")
                    return True, f"🔒 Locking the screen now, {speaker_name}. See you soon."

            # Fallback Method 2: Use pmset command directly
            await _progress("pmset", 70, "Trying pmset...")
            stdout, stderr, returncode = await async_subprocess_run(
                ["pmset", "displaysleepnow"], timeout=5.0
            )
            if returncode == 0:
                duration_ms = (time.time() - start_time) * 1000
                await _progress("complete", 100, f"Screen locked ({duration_ms:.0f}ms)")
                await voice_feedback("complete")
                logger.info(f"Screen locked successfully using pmset in {duration_ms:.0f}ms")
                return True, f"🔒 Locking the screen now, {speaker_name}. See you soon."

            await _progress("failed", 90, "Lock methods exhausted")
            await voice_feedback("failed")
            return False, "❌ Unable to lock screen. Please use Control+Command+Q manually."

        except Exception as e:
            logger.error(f"Error locking screen: {e}")
            await _progress("error", 90, f"Lock error: {e}")
            await voice_feedback("failed")
            return False, f"❌ Failed to lock screen: {str(e)}"

    async def unlock_screen(self, password: Optional[str] = None) -> Tuple[bool, str]:
        """
        Unlock the macOS screen using direct async methods (avoiding full pipeline loops)

        Integrates with voice unlock daemon and AppleScript fallback

        Returns:
            Tuple of (success, message)
        """
        try:
            logger.info("[UnlockScreen] Starting screen unlock")

            # Method 1: Try WebSocket unlock directly
            try:
                import json

                import websockets

                VOICE_UNLOCK_WS_URL = "ws://localhost:8765/voice-unlock"

                async with websockets.connect(VOICE_UNLOCK_WS_URL, ping_interval=20) as websocket:
                    # Send unlock command using the daemon's expected format
                    unlock_command = {"type": "command", "command": "unlock_screen"}

                    await websocket.send(json.dumps(unlock_command))
                    logger.info("[UnlockScreen] Sent unlock command to voice unlock daemon")

                    # Wait for response (longer timeout for unlock)
                    response = await asyncio.wait_for(websocket.recv(), timeout=15.0)
                    result = json.loads(response)

                    if (
                        result.get("type") == "command_response"
                        or result.get("type") == "unlock_result"
                    ):
                        if result.get("success"):
                            logger.info("[UnlockScreen] Successfully unlocked via WebSocket daemon")
                            return True, "Screen unlocked successfully, Sir."
                        else:
                            logger.warning(
                                f"[UnlockScreen] Daemon unlock failed: {result.get('message', 'Unknown error')}"
                            )

            except (ConnectionRefusedError, OSError):
                logger.info("[UnlockScreen] Voice unlock daemon not running, trying fallback")
            except asyncio.TimeoutError:
                logger.warning("[UnlockScreen] Timeout waiting for unlock response from daemon")
            except Exception as e:
                logger.warning(f"[UnlockScreen] WebSocket error: {e}")

            # Method 2: Try direct AppleScript unlock (fallback)
            if not password:
                # Try to retrieve from keychain
                try:
                    from api.jarvis_voice_api import async_subprocess_run

                    stdout, stderr, returncode = await async_subprocess_run(
                        [
                            "security",
                            "find-generic-password",
                            "-s",
                            "com.jarvis.voiceunlock",
                            "-a",
                            "unlock_token",
                            "-w",
                        ],
                        timeout=5.0,
                    )
                    if returncode == 0 and stdout:
                        password = stdout.decode().strip()
                    else:
                        logger.info("[UnlockScreen] No password in keychain")
                        return (
                            False,
                            "Unable to unlock screen. The Voice Unlock daemon is not running. Please run './backend/voice_unlock/enable_screen_unlock.sh' to enable it.",
                        )
                except Exception as e:
                    logger.warning(f"[UnlockScreen] Failed to retrieve password: {e}")
                    return (
                        False,
                        "Unable to unlock screen without password. Please setup Voice Unlock first.",
                    )

            # DYNAMIC DIAGNOSTIC: Intelligently diagnose the issue
            logger.info("[UnlockScreen] Performing dynamic diagnostics...")

            # Build diagnostic report
            diagnostic_issues = []

            # Check keychain password
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "com.jarvis.voiceunlock", "-a", "unlock_token", "-w"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                has_password = (result.returncode == 0)
            except:
                has_password = False

            if not has_password:
                diagnostic_issues.append("Keychain password not configured")

            # Check enrollment data
            import json
            from pathlib import Path
            enrollment_file = Path.home() / ".jarvis" / "voice_unlock_enrollment.json"
            enrollment_status = "missing"

            if enrollment_file.exists():
                try:
                    with open(enrollment_file) as f:
                        enrollment = json.load(f)
                        enrollment_status = enrollment.get("status", "unknown")
                except:
                    enrollment_status = "corrupted"

            if enrollment_status != "complete":
                diagnostic_issues.append(f"Enrollment {enrollment_status}")

            # Generate intelligent response based on diagnostics
            if diagnostic_issues:
                issue_list = ", ".join(diagnostic_issues)
                logger.warning(f"[UnlockScreen] Issues found: {issue_list}")

                # Provide specific guidance based on the issues
                if "Keychain password" in issue_list:
                    suggestion = "I need your password stored in keychain. Run: ./backend/voice_unlock/enable_screen_unlock.sh"
                elif "Enrollment" in issue_list:
                    suggestion = "Voice enrollment needs to be completed. Your biometric data is in CloudSQL but enrollment config is incomplete."
                else:
                    suggestion = "Configuration issue detected. Checking system status..."

                return (
                    False,
                    f"I detected issues with voice unlock configuration: {issue_list}. {suggestion}",
                )
            else:
                # All checks passed but daemon not running - try alternative method
                logger.info("[UnlockScreen] Configuration OK but daemon not available, trying direct unlock...")

                # Import and use the simple unlock handler directly
                try:
                    from api.simple_unlock_handler import handle_unlock_command

                    # Call the handler directly (it's already async)
                    result = await handle_unlock_command("unlock my screen")

                    if result.get("success"):
                        return True, result.get("response", "Screen unlocked")
                    else:
                        return False, result.get("response", "Unable to unlock screen")
                except Exception as e:
                    logger.error(f"[UnlockScreen] Direct handler failed: {e}")
                    return False, f"Voice unlock service error: {str(e)}. Attempting to diagnose and self-heal..."

        except Exception as e:
            logger.error(f"[UnlockScreen] Error unlocking screen: {e}")
            return False, f"Failed to unlock screen: {str(e)}"

    async def handle_command(self, command: str) -> Dict[str, Any]:
        """
        Main command handler for system commands

        Args:
            command: The command to process

        Returns:
            Dict with result and response
        """
        command_lower = command.lower()

        # Handle lock/unlock screen commands
        # IMPORTANT: Token-based matching to avoid substring collisions ("unlock" contains "lock")
        import re

        tokens = set(re.findall(r"[a-z']+", command_lower))
        if "screen" in tokens or "mac" in tokens or "computer" in tokens:
            if "unlock" in tokens:
                success, message = await self.unlock_screen()
                return {"success": success, "response": message, "command_type": "screen_unlock"}
            if "lock" in tokens:
                success, message = await self.lock_screen()
                return {"success": success, "response": message, "command_type": "screen_lock"}

        # Handle application commands
        if any(word in command_lower for word in ["open", "launch", "start", "run"]):
            # Extract app name
            words = command_lower.split()
            app_words = []
            start_collecting = False

            for word in words:
                if word in ["open", "launch", "start", "run"]:
                    start_collecting = True
                elif start_collecting:
                    app_words.append(word)

            if app_words:
                app_name = " ".join(app_words)
                success, message = self.open_application(app_name)
                return {"success": success, "response": message, "command_type": "application"}

        # Handle volume commands (using ASYNC versions for better performance)
        if "volume" in command_lower:
            if "mute" in command_lower or "unmute" in command_lower:
                mute = "mute" in command_lower
                success, message = await self.mute_volume_async(mute)
                return {"success": success, "response": message, "command_type": "volume_control"}
            elif any(word in command_lower for word in ["set", "change", "adjust"]):
                # Extract volume level
                import re

                numbers = re.findall(r"\d+", command_lower)
                if numbers:
                    level = int(numbers[0])
                    success, message = await self.set_volume_async(level)
                    return {
                        "success": success,
                        "response": message,
                        "command_type": "volume_control",
                    }

        # Handle file operations
        if any(word in command_lower for word in ["create", "make", "new"]) and any(
            word in command_lower for word in ["file", "folder", "directory"]
        ):
            # Extract file/folder name
            if "folder" in command_lower or "directory" in command_lower:
                # Create folder logic
                folder_name = self._extract_name(command_lower, ["folder", "directory"])
                if folder_name:
                    success, message = self.create_folder(
                        str(self.home_dir / "Desktop" / folder_name)
                    )
                    return {
                        "success": success,
                        "response": message,
                        "command_type": "file_operation",
                    }

        # Default response for unhandled commands
        return {
            "success": False,
            "response": f"I'm not sure how to handle that system command: {command}",
            "command_type": "unknown",
        }

    def _extract_name(self, command: str, keywords: List[str]) -> Optional[str]:
        """Extract name from command after keywords"""
        for keyword in keywords:
            if keyword in command:
                parts = command.split(keyword)
                if len(parts) > 1:
                    name = parts[1].strip()
                    # Remove common words
                    name = name.replace("called", "").replace("named", "").strip()
                    if name:
                        return name
        return None
