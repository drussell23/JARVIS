#!/usr/bin/env python3
"""
macOS System Controller for JARVIS AI Agent with Event Integration
Provides voice-activated control of macOS environment through natural language commands
Now with event-driven architecture for loose coupling
"""

import os
import subprocess
import json
import logging
import time
from typing import Dict, List, Optional, Union, Tuple, Any
from pathlib import Path
import psutil
import asyncio
from enum import Enum
import re

# EVENT BUS INTEGRATION
from backend.core.event_bus import Event, EventPriority, get_event_bus
from backend.core.event_types import (
    EventTypes, EventBuilder, ControlEvents, SystemEvents,
    subscribe_to, subscribe_to_pattern
)

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
    """Controls macOS system operations with safety checks and event integration"""
    
    def __init__(self):
        self.home_dir = Path.home()
        self.safe_directories = [
            self.home_dir / "Desktop",
            self.home_dir / "Documents", 
            self.home_dir / "Downloads",
            self.home_dir / "Pictures",
            self.home_dir / "Music",
            self.home_dir / "Movies"
        ]
        
        # EVENT BUS SETUP
        self.event_bus = get_event_bus()
        self.event_builder = EventBuilder()
        
        # Blocked applications for safety
        self.blocked_apps = {
            "System Preferences", "System Settings", "Activity Monitor",
            "Terminal", "Console", "Disk Utility", "Keychain Access"
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
            "keynote": "Keynote"
        }
        
        # Command history for safety tracking
        self.command_history = []
        self.max_commands_per_minute = 20
        
        # Setup event subscriptions
        self._setup_event_subscriptions()
        
        # Publish startup event
        SystemEvents.startup(
            source="macos_controller",
            version="2.0",
            config={
                "safe_directories": len(self.safe_directories),
                "blocked_apps": len(self.blocked_apps),
                "app_aliases": len(self.app_aliases)
            }
        )
        
    def _setup_event_subscriptions(self):
        """Setup event subscriptions for system control"""
        
        # Subscribe to voice commands
        @subscribe_to(EventTypes.VOICE_COMMAND_RECEIVED)
        async def handle_voice_command(event: Event):
            command = event.payload.get("command", "")
            confidence = event.payload.get("confidence", 0)
            
            # Only process high confidence commands for safety
            if confidence < 0.7:
                logger.warning(f"Ignoring low confidence command: {command} ({confidence})")
                return
                
            # Check for system control keywords
            control_keywords = [
                "open", "close", "launch", "quit", "switch", "show",
                "minimize", "maximize", "create", "delete", "move",
                "copy", "paste", "search", "find", "run"
            ]
            
            if any(keyword in command.lower() for keyword in control_keywords):
                logger.info(f"Processing control command: {command}")
                await self._process_voice_command(command)
                
        # Subscribe to workflow events
        @subscribe_to(EventTypes.CONTROL_WORKFLOW_STARTED)
        async def handle_workflow_start(event: Event):
            workflow = event.payload.get("workflow")
            logger.info(f"System control preparing for workflow: {workflow}")
            
        # Subscribe to memory pressure
        @subscribe_to(EventTypes.MEMORY_PRESSURE_CHANGED)
        async def handle_memory_pressure(event: Event):
            new_level = event.payload.get("new_level")
            
            if new_level in ["high", "critical"]:
                # Suggest closing unused apps
                open_apps = self.list_open_applications()
                if len(open_apps) > 10:
                    self.event_builder.publish(
                        "control.suggestion",
                        source="macos_controller",
                        payload={
                            "suggestion": "close_unused_apps",
                            "reason": "memory_pressure",
                            "app_count": len(open_apps)
                        }
                    )
                    
    async def _process_voice_command(self, command: str):
        """Process a voice command and execute appropriate action"""
        command_lower = command.lower()
        
        # Check rate limiting
        if not self._check_rate_limit():
            SystemEvents.warning(
                source="macos_controller",
                warning="Command rate limit exceeded",
                details={"command": command}
            )
            return
            
        # Parse command intent
        if "open" in command_lower or "launch" in command_lower:
            # Extract app name
            app_name = self._extract_app_name(command)
            if app_name:
                success, message = self.open_application(app_name)
                
        elif "close" in command_lower or "quit" in command_lower:
            app_name = self._extract_app_name(command)
            if app_name:
                success, message = self.close_application(app_name)
                
        elif "switch to" in command_lower:
            app_name = self._extract_app_name(command)
            if app_name:
                success, message = self.switch_to_application(app_name)
                
        elif "minimize all" in command_lower:
            success, message = self.minimize_all_windows()
            
        elif "take screenshot" in command_lower:
            success, message = self.take_screenshot()
            
        elif "volume" in command_lower:
            # Extract volume level
            level = self._extract_number(command)
            if level is not None:
                success, message = self.set_volume(level)
            elif "mute" in command_lower:
                success, message = self.mute_volume(True)
            elif "unmute" in command_lower:
                success, message = self.mute_volume(False)
            else:
                success, message = False, "Could not determine volume action"
                
        else:
            success = False
            message = f"Unknown command: {command}"
            
    def _check_rate_limit(self) -> bool:
        """Check if command rate limit is exceeded"""
        now = time.time()
        # Remove old entries
        self.command_history = [t for t in self.command_history if now - t < 60]
        
        if len(self.command_history) >= self.max_commands_per_minute:
            return False
            
        self.command_history.append(now)
        return True
        
    def _extract_app_name(self, command: str) -> Optional[str]:
        """Extract application name from command"""
        # Remove action words
        text = command.lower()
        for word in ["open", "close", "launch", "quit", "switch to", "show"]:
            text = text.replace(word, "").strip()
            
        # Check aliases first
        for alias, full_name in self.app_aliases.items():
            if alias in text:
                return full_name
                
        # Return cleaned text as app name
        return text.strip() if text else None
        
    def _extract_number(self, command: str) -> Optional[int]:
        """Extract number from command"""
        numbers = re.findall(r'\d+', command)
        return int(numbers[0]) if numbers else None
        
    def execute_applescript(self, script: str) -> Tuple[bool, str]:
        """Execute AppleScript and return result"""
        start_time = time.time()
        
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            execution_time = time.time() - start_time
            
            if result.returncode == 0:
                # Publish successful execution
                ControlEvents.command_executed(
                    source="macos_controller",
                    command=f"AppleScript: {script[:50]}...",
                    success=True,
                    result=result.stdout.strip()
                )
                
                return True, result.stdout.strip()
            else:
                # Publish failed execution
                ControlEvents.command_executed(
                    source="macos_controller",
                    command=f"AppleScript: {script[:50]}...",
                    success=False,
                    result=result.stderr.strip()
                )
                
                return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            SystemEvents.error(
                source="macos_controller",
                error="AppleScript timeout",
                details={"script": script[:100]}
            )
            return False, "Command timed out"
        except Exception as e:
            SystemEvents.error(
                source="macos_controller",
                error=f"AppleScript error: {str(e)}",
                details={"script": script[:100]}
            )
            return False, str(e)
            
    def execute_shell(self, command: str, safe_mode: bool = True) -> Tuple[bool, str]:
        """Execute shell command with safety checks and event publishing"""
        if safe_mode:
            # Block dangerous commands
            dangerous_patterns = [
                r'rm\s+-rf', r'sudo', r'dd\s+', r'mkfs', r'format',
                r'>\s*/dev/', r'chmod\s+777', r'pkill', r'killall'
            ]
            for pattern in dangerous_patterns:
                if re.search(pattern, command, re.IGNORECASE):
                    SystemEvents.warning(
                        source="macos_controller",
                        warning="Blocked dangerous command",
                        details={"command": command, "pattern": pattern}
                    )
                    return False, f"Blocked dangerous command pattern: {pattern}"
                    
        start_time = time.time()
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            execution_time = time.time() - start_time
            
            # Publish execution event
            ControlEvents.command_executed(
                source="macos_controller",
                command=f"Shell: {command[:50]}...",
                success=result.returncode == 0,
                result=result.stdout[:200] or result.stderr[:200]
            )
            
            return result.returncode == 0, result.stdout or result.stderr
        except Exception as e:
            SystemEvents.error(
                source="macos_controller",
                error=f"Shell command error: {str(e)}",
                details={"command": command[:100]}
            )
            return False, str(e)
            
    # Application Control Methods
    
    def open_application(self, app_name: str) -> Tuple[bool, str]:
        """Open an application with event publishing.

        Resolves native apps via aliases, then falls back to web service
        detection via the shared ApplicationLauncherExecutor config.
        """
        # Resolve aliases
        resolved = self.app_aliases.get(app_name.lower(), app_name)

        # Check if blocked
        if resolved in self.blocked_apps:
            SystemEvents.warning(
                source="macos_controller",
                warning="Blocked application",
                details={"app": resolved, "reason": "safety"}
            )
            return False, f"Opening {resolved} is blocked for safety"

        # Try native AppleScript launch
        script = f'tell application "{resolved}" to activate'
        success, message = self.execute_applescript(script)

        if success:
            ControlEvents.app_launched(
                source="macos_controller",
                app_name=resolved,
                success=True
            )
            return True, f"Opened {resolved}"

        # Try open -a
        success, message = self.execute_shell(f"open -a '{resolved}'")
        if success:
            ControlEvents.app_launched(
                source="macos_controller",
                app_name=resolved,
                success=True
            )
            return True, f"Opened {resolved}"

        # Native launch failed — check if it's a web service
        try:
            from backend.api.action_executors import ApplicationLauncherExecutor
            launcher = ApplicationLauncherExecutor()
            svc = launcher.get_web_service(app_name)
            if svc:
                import subprocess as _sp
                url = svc.get("url", f"https://www.{app_name.lower()}.com")
                browser = os.environ.get("JARVIS_DEFAULT_BROWSER", "Google Chrome")
                _sp.run(["open", "-a", browser, url], timeout=5)
                ControlEvents.app_launched(
                    source="macos_controller",
                    app_name=app_name,
                    success=True
                )
                return True, f"Opened {app_name} in browser"
        except Exception:
            pass

        ControlEvents.app_launched(
            source="macos_controller",
            app_name=app_name,
            success=False
        )
        return False, f"Failed to open {app_name}: {message}"
            
    def close_application(self, app_name: str) -> Tuple[bool, str]:
        """Close an application gracefully with event publishing"""
        app_name = self.app_aliases.get(app_name.lower(), app_name)
        
        # Try enhanced app closer first for problematic apps
        problematic_apps = ['whatsapp', 'slack', 'discord', 'spotify', 'zoom']
        if app_name.lower() in problematic_apps:
            try:
                from .app_closer_enhanced import close_app_enhanced
                success, message = close_app_enhanced(app_name)
                if success:
                    self.event_builder.publish(
                        "control.app_closed",
                        source="macos_controller",
                        payload={
                            "app_name": app_name,
                            "method": "enhanced"
                        }
                    )
                    return True, message
            except ImportError:
                logger.warning("Enhanced app closer not available")
        
        # First try the standard quit command
        script = f'tell application "{app_name}" to quit'
        success, message = self.execute_applescript(script)
        
        if success:
            self.event_builder.publish(
                "control.app_closed",
                source="macos_controller",
                payload={
                    "app_name": app_name,
                    "method": "graceful"
                }
            )
            return True, f"Closed {app_name}"
        
        # If that fails, try using System Events
        script = f'''
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
        '''
        success, message = self.execute_applescript(script)
        
        if success:
            self.event_builder.publish(
                "control.app_closed",
                source="macos_controller",
                payload={
                    "app_name": app_name,
                    "method": "keyboard_shortcut"
                }
            )
            return True, f"Closed {app_name}"
        
        # Final attempt: Force quit if necessary
        # But only for non-system apps
        if app_name not in ["Finder", "System Preferences", "System Settings"]:
            success, message = self.execute_shell(f"pkill -x '{app_name}'", safe_mode=False)
            if success:
                self.event_builder.publish(
                    "control.app_closed",
                    source="macos_controller",
                    payload={
                        "app_name": app_name,
                        "method": "force_quit"
                    }
                )
                return True, f"Force closed {app_name}"
        
        return False, f"Failed to close {app_name}: {message}"
        
    def switch_to_application(self, app_name: str) -> Tuple[bool, str]:
        """Switch to an already open application"""
        app_name = self.app_aliases.get(app_name.lower(), app_name)
        
        script = f'''
        tell application "System Events"
            set frontmost of process "{app_name}" to true
        end tell
        '''
        success, message = self.execute_applescript(script)
        
        if success:
            self.event_builder.publish(
                "control.app_switched",
                source="macos_controller",
                payload={"app_name": app_name}
            )
            return True, f"Switched to {app_name}"
        return False, f"Failed to switch to {app_name}: {message}"
        
    def list_open_applications(self) -> List[str]:
        """Get list of currently open applications"""
        script = '''
        tell application "System Events"
            get name of (every process whose background only is false)
        end tell
        '''
        success, output = self.execute_applescript(script)
        
        if success:
            apps = output.split(", ")
            app_list = [app.strip() for app in apps if app.strip()]
            
            # Publish current apps event
            self.event_builder.publish(
                "control.apps_listed",
                source="macos_controller",
                payload={
                    "count": len(app_list),
                    "apps": app_list
                }
            )
            
            return app_list
        return []
        
    def minimize_all_windows(self) -> Tuple[bool, str]:
        """Minimize all windows"""
        script = '''
        tell application "System Events"
            set visible of every process to false
        end tell
        '''
        success, message = self.execute_applescript(script)
        
        if success:
            self.event_builder.publish(
                "control.windows_minimized",
                source="macos_controller",
                payload={"action": "minimize_all"}
            )
            
        return success, message
        
    def activate_mission_control(self) -> Tuple[bool, str]:
        """Activate Mission Control"""
        script = '''
        tell application "Mission Control" to launch
        '''
        return self.execute_applescript(script)
        
    # File Operations
    
    def is_safe_path(self, path: Path) -> bool:
        """Check if a path is in a safe directory"""
        path = path.resolve()
        return any(path.is_relative_to(safe_dir) for safe_dir in self.safe_directories)
        
    def open_file(self, file_path: str) -> Tuple[bool, str]:
        """Open a file with its default application"""
        path = Path(file_path).expanduser()
        
        if not path.exists():
            return False, f"File not found: {file_path}"
            
        if not self.is_safe_path(path):
            SystemEvents.warning(
                source="macos_controller",
                warning="Unsafe file access attempt",
                details={"path": str(path)}
            )
            return False, f"Access to {file_path} is restricted for safety"
            
        success, message = self.execute_shell(f"open '{path}'")
        
        if success:
            ControlEvents.file_operation(
                source="macos_controller",
                operation="open",
                file_path=str(path),
                success=True
            )
            return True, f"Opened {path.name}"
        return False, f"Failed to open file: {message}"
        
    def create_file(self, file_path: str, content: str = "") -> Tuple[bool, str]:
        """Create a new file"""
        path = Path(file_path).expanduser()
        
        if not self.is_safe_path(path):
            return False, f"Cannot create file in restricted directory"
            
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            
            ControlEvents.file_operation(
                source="macos_controller",
                operation="create",
                file_path=str(path),
                success=True
            )
            
            return True, f"Created file: {path.name}"
        except Exception as e:
            ControlEvents.file_operation(
                source="macos_controller",
                operation="create",
                file_path=str(path),
                success=False
            )
            return False, f"Failed to create file: {str(e)}"
            
    def delete_file(self, file_path: str, confirm: bool = True) -> Tuple[bool, str]:
        """Delete a file (requires confirmation)"""
        path = Path(file_path).expanduser()
        
        if not path.exists():
            return False, f"File not found: {file_path}"
            
        if not self.is_safe_path(path):
            return False, f"Cannot delete file in restricted directory"
            
        if confirm:
            # In real implementation, this would trigger a confirmation dialog
            SystemEvents.warning(
                source="macos_controller",
                warning="File deletion requires confirmation",
                details={"file": str(path)}
            )
            return False, "File deletion requires user confirmation"
            
        try:
            path.unlink()
            
            ControlEvents.file_operation(
                source="macos_controller",
                operation="delete",
                file_path=str(path),
                success=True
            )
            
            return True, f"Deleted file: {path.name}"
        except Exception as e:
            ControlEvents.file_operation(
                source="macos_controller",
                operation="delete",
                file_path=str(path),
                success=False
            )
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
            files = output.strip().split('\n')
            # Filter to only safe paths
            safe_files = [f for f in files if f and self.is_safe_path(Path(f))]
            
            self.event_builder.publish(
                "control.files_searched",
                source="macos_controller",
                payload={
                    "query": query,
                    "results_count": len(safe_files),
                    "directory": directory
                }
            )
            
            return safe_files
        return []
        
    # System Settings Control
    
    def set_volume(self, level: int) -> Tuple[bool, str]:
        """Set system volume (0-100)"""
        level = max(0, min(100, level))
        script = f"set volume output volume {level}"
        success, _ = self.execute_applescript(script)
        
        if success:
            self.event_builder.publish(
                EventTypes.CONTROL_SYSTEM_SETTING_CHANGED,
                source="macos_controller",
                payload={
                    "setting": "volume",
                    "value": level
                }
            )
            return True, f"Volume set to {level}%"
        return False, "Failed to set volume"
        
    def mute_volume(self, mute: bool = True) -> Tuple[bool, str]:
        """Mute or unmute system volume"""
        script = f"set volume output muted {str(mute).lower()}"
        success, _ = self.execute_applescript(script)
        
        if success:
            state = "muted" if mute else "unmuted"
            self.event_builder.publish(
                EventTypes.CONTROL_SYSTEM_SETTING_CHANGED,
                source="macos_controller",
                payload={
                    "setting": "mute",
                    "value": mute
                }
            )
            return True, f"Volume {state}"
        return False, "Failed to change mute state"
        
    def adjust_brightness(self, level: float) -> Tuple[bool, str]:
        """Adjust screen brightness (0.0-1.0)"""
        # This requires additional setup with brightness control tools
        self.event_builder.publish(
            "control.feature_unavailable",
            source="macos_controller",
            payload={
                "feature": "brightness_control",
                "reason": "requires_additional_setup"
            }
        )
        return False, "Brightness control requires additional setup"
        
    def toggle_wifi(self, enable: bool) -> Tuple[bool, str]:
        """Toggle WiFi on/off"""
        action = "on" if enable else "off"
        success, message = self.execute_shell(f"networksetup -setairportpower airport {action}")
        
        if success:
            self.event_builder.publish(
                EventTypes.CONTROL_SYSTEM_SETTING_CHANGED,
                source="macos_controller",
                payload={
                    "setting": "wifi",
                    "value": enable
                }
            )
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
            self.event_builder.publish(
                "control.screenshot_taken",
                source="macos_controller",
                payload={
                    "path": str(path),
                    "timestamp": time.time()
                }
            )
            return True, f"Screenshot saved to {path.name}"
        return False, f"Failed to take screenshot: {message}"
        
    def sleep_display(self) -> Tuple[bool, str]:
        """Put display to sleep"""
        success, message = self.execute_shell("pmset displaysleepnow")
        
        if success:
            self.event_builder.publish(
                "control.display_sleep",
                source="macos_controller",
                payload={"action": "sleep"}
            )
            return True, "Display sleeping"
        return False, f"Failed to sleep display: {message}"
        
    # Web Integration
    
    def open_url(self, url: str, browser: Optional[str] = None) -> Tuple[bool, str]:
        """Open URL in browser"""
        if browser:
            browser = self.app_aliases.get(browser.lower(), browser)
            cmd = f"open -a '{browser}' '{url}'"
        else:
            cmd = f"open '{url}'"
            
        success, message = self.execute_shell(cmd)
        
        if success:
            self.event_builder.publish(
                "control.url_opened",
                source="macos_controller",
                payload={
                    "url": url,
                    "browser": browser or "default"
                }
            )
            return True, f"Opened {url}"
        return False, f"Failed to open URL: {message}"
        
    def web_search(self, query: str, engine: str = "google") -> Tuple[bool, str]:
        """Perform web search"""
        engines = {
            "google": f"https://www.google.com/search?q={query}",
            "bing": f"https://www.bing.com/search?q={query}",
            "duckduckgo": f"https://duckduckgo.com/?q={query}"
        }
        
        url = engines.get(engine.lower(), engines["google"])
        return self.open_url(url)
        
    # Complex Workflows
    
    async def execute_workflow(self, workflow_name: str) -> Tuple[bool, str]:
        """Execute predefined workflows with event publishing"""
        # Publish workflow start event
        ControlEvents.workflow_started(
            source="macos_controller",
            workflow_name=workflow_name,
            components=["system_control"]
        )
        
        workflows = {
            "morning_routine": [
                ("open_application", "Mail"),
                ("open_application", "Calendar"),
                ("web_search", "weather today"),
                ("open_url", "https://news.google.com")
            ],
            "development_setup": [
                ("open_application", "Visual Studio Code"),
                ("open_application", "Terminal"),
                ("open_application", "Docker"),
                ("open_url", "http://localhost:3000")
            ],
            "meeting_prep": [
                ("set_volume", 50),
                ("close_application", "Spotify"),
                ("minimize_all_windows", None),
                ("open_application", "zoom.us")
            ]
        }
        
        if workflow_name not in workflows:
            ControlEvents.workflow_completed(
                source="macos_controller",
                workflow_name=workflow_name,
                success=False,
                error="Unknown workflow"
            )
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
                ControlEvents.workflow_completed(
                    source="macos_controller",
                    workflow_name=workflow_name,
                    success=False,
                    error=f"Failed at {action}: {message}"
                )
                return False, f"Workflow failed at {action}: {message}"
                
            # Small delay between actions
            await asyncio.sleep(0.5)
            
        ControlEvents.workflow_completed(
            source="macos_controller",
            workflow_name=workflow_name,
            success=True,
            results=results
        )
        
        return True, f"Completed {workflow_name} workflow"
        
    # Utility Methods
    
    def get_system_info(self) -> Dict[str, Any]:
        """Get system information"""
        info = {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage('/').percent,
            "battery": None
        }
        
        if hasattr(psutil, "sensors_battery"):
            battery = psutil.sensors_battery()
            if battery:
                info["battery"] = {
                    "percent": battery.percent,
                    "charging": battery.power_plugged
                }
                
        # Publish system info event
        self.event_builder.publish(
            "control.system_info",
            source="macos_controller",
            payload=info
        )
        
        return info
        
    def validate_command(self, command: str, category: CommandCategory) -> SafetyLevel:
        """Validate command safety level"""
        if category == CommandCategory.DANGEROUS:
            return SafetyLevel.DANGEROUS
            
        # Check for dangerous patterns
        dangerous_patterns = [
            r'delete.*system', r'remove.*all', r'format', r'shutdown',
            r'restart', r'sudo', r'admin', r'root'
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
        app_dirs = [
            "/Applications",
            "~/Applications",
            "/System/Applications"
        ]
        
        partial_lower = partial_name.lower()
        
        for app_dir in app_dirs:
            expanded_dir = os.path.expanduser(app_dir)
            if not os.path.exists(expanded_dir):
                continue
                
            for app in os.listdir(expanded_dir):
                if app.endswith('.app'):
                    app_name = app[:-4]  # Remove .app extension
                    if partial_lower in app_name.lower():
                        return app_name
        
        return None