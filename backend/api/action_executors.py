"""
JARVIS Action Executors - Configuration-Driven Execution Functions
Implements individual action executors for workflow steps
"""

import asyncio
import subprocess
import os
import json
import aiohttp
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
import logging
import pyautogui
from abc import ABC, abstractmethod

from .workflow_parser import WorkflowAction, ActionType
from .workflow_engine import ExecutionContext

logger = logging.getLogger(__name__)


class BaseActionExecutor(ABC):
    """Base class for all action executors"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """Initialize with configuration"""
        self.config = config or {}
        
    @abstractmethod
    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Execute the action"""
        pass
        
    async def validate_preconditions(self, action: WorkflowAction, context: ExecutionContext) -> Tuple[bool, str]:
        """Validate action can be executed"""
        return True, ""
        
    async def log_execution(self, action: WorkflowAction, result: Any, duration: float):
        """Log execution details"""
        logger.info(f"Executed {action.action_type.value} in {duration:.2f}s")


class SystemUnlockExecutor(BaseActionExecutor):
    """Executor for system unlock actions"""
    
    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Unlock the system screen"""
        try:
            # Check if screen is locked
            is_locked = await self._check_screen_locked()
            if not is_locked:
                return {"status": "already_unlocked", "message": "Screen is already unlocked"}
                
            # Platform-specific unlock
            if os.uname().sysname == "Darwin":  # macOS
                # Use TouchID or password
                result = await self._unlock_macos(context)
            else:
                result = {"status": "unsupported", "message": "Platform not supported"}
                
            return result
            
        except Exception as e:
            logger.error(f"Failed to unlock system: {e}")
            raise
            
    async def _check_screen_locked(self) -> bool:
        """Check if screen is locked"""
        try:
            # macOS specific check
            cmd = ['ioreg', '-n', 'Root', '-d1']
            result = subprocess.run(cmd, capture_output=True, text=True)
            return 'CGSSessionScreenIsLocked' in result.stdout
        except Exception:
            return False

    async def _unlock_macos(self, context: ExecutionContext) -> Dict[str, Any]:
        """Unlock macOS screen"""
        try:
            # Wake display
            subprocess.run(['caffeinate', '-u', '-t', '1'])
            
            # Simulate mouse movement to wake
            pyautogui.moveRel(1, 0)
            await asyncio.sleep(0.5)
            
            # Check if TouchID is available
            touchid_available = await self._check_touchid()
            
            if touchid_available:
                # Prompt for TouchID
                logger.info("Waiting for TouchID authentication...")
                # In real implementation, would trigger TouchID prompt
                context.set_variable('unlock_method', 'touchid')
            else:
                # Would need password - for security, we don't actually type it
                logger.info("Password required for unlock")
                context.set_variable('unlock_method', 'password_required')
                return {"status": "password_required", "message": "Please unlock manually"}
                
            return {"status": "success", "message": "System unlocked"}
            
        except Exception as e:
            logger.error(f"macOS unlock failed: {e}")
            raise
            
    async def _check_touchid(self) -> bool:
        """Check if TouchID is available"""
        try:
            result = subprocess.run(
                ['system_profiler', 'SPHardwareDataType'], 
                capture_output=True, 
                text=True
            )
            return 'Touch ID' in result.stdout
        except Exception:
            return False


class ApplicationLauncherExecutor(BaseActionExecutor):
    """Executor for launching applications"""
    
    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        # Load app mappings from config
        self.app_mappings = self._load_app_mappings()
        
    def _load_app_mappings(self) -> Dict[str, str]:
        """Load application name mappings from config"""
        config_path = os.path.join(
            os.path.dirname(__file__), 'config', 'app_mappings.json'
        )
        
        default_mappings = {
            "safari": "Safari",
            "chrome": "Google Chrome",
            "firefox": "Firefox",
            "mail": "Mail",
            "calendar": "Calendar",
            "notes": "Notes",
            "finder": "Finder",
            "terminal": "Terminal",
            "vscode": "Visual Studio Code",
            "slack": "Slack",
            "zoom": "zoom.us",
            "teams": "Microsoft Teams"
        }
        
        try:
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    loaded_mappings = json.load(f)
                    default_mappings.update(loaded_mappings)
        except Exception as e:
            logger.error(f"Failed to load app mappings: {e}")
            
        return default_mappings
        
    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Launch the specified application"""
        try:
            app_name = action.target
            
            # Normalize app name
            normalized_name = self.app_mappings.get(app_name.lower(), app_name)
            
            # Platform-specific launch
            if os.uname().sysname == "Darwin":  # macOS
                result = await self._launch_macos_app(normalized_name, context)
            else:
                result = await self._launch_generic_app(normalized_name, context)
                
            return result
            
        except Exception as e:
            logger.error(f"Failed to launch application {action.target}: {e}")
            raise
            
    async def _launch_macos_app(self, app_name: str, context: ExecutionContext) -> Dict[str, Any]:
        """Launch macOS application"""
        try:
            # Check if app is already running
            check_cmd = ['pgrep', '-f', app_name]
            check_result = subprocess.run(check_cmd, capture_output=True)
            
            if check_result.returncode == 0:
                # App is running, bring to front
                script = f'''
                tell application "{app_name}"
                    activate
                end tell
                '''
                subprocess.run(['osascript', '-e', script])
                return {"status": "activated", "message": f"{app_name} brought to front"}
            else:
                # Launch app
                subprocess.run(['open', '-a', app_name], check=True)
                
                # Wait for app to start
                await self._wait_for_app_start(app_name, timeout=5)
                
                context.set_variable(f'app_{app_name.lower()}_pid', 'running')
                return {"status": "launched", "message": f"{app_name} launched successfully"}
                
        except subprocess.CalledProcessError:
            # Try alternative launch methods
            try:
                subprocess.run(['open', f'/Applications/{app_name}.app'], check=True)
                return {"status": "launched", "message": f"{app_name} launched via direct path"}
            except Exception:
                raise Exception(f"Could not launch {app_name}")
                
    async def _launch_generic_app(self, app_name: str, context: ExecutionContext) -> Dict[str, Any]:
        """Launch application on generic platform"""
        try:
            # Try common launch commands
            launch_commands = [
                app_name.lower(),
                app_name.lower().replace(' ', '-'),
                app_name.lower().replace(' ', '_')
            ]
            
            for cmd in launch_commands:
                try:
                    subprocess.Popen([cmd])
                    return {"status": "launched", "message": f"{app_name} launched"}
                except Exception:
                    continue

            raise Exception(f"Could not find launch command for {app_name}")
            
        except Exception as e:
            raise Exception(f"Failed to launch {app_name}: {str(e)}")
            
    async def _wait_for_app_start(self, app_name: str, timeout: int = 5):
        """Wait for application to start"""
        start_time = datetime.now()
        while (datetime.now() - start_time).total_seconds() < timeout:
            check_cmd = ['pgrep', '-f', app_name]
            result = subprocess.run(check_cmd, capture_output=True)
            if result.returncode == 0:
                return
            await asyncio.sleep(0.5)


class SearchExecutor(BaseActionExecutor):
    """Executor for search actions"""
    
    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Perform search action"""
        try:
            query = action.parameters.get('query', '')
            platform = action.parameters.get('platform', 'web')
            
            if platform.lower() in ['web', 'browser', 'internet']:
                result = await self._search_web(query, context)
            elif platform.lower() in ['files', 'finder', 'documents']:
                result = await self._search_files(query, context)
            elif platform.lower() in ['mail', 'email']:
                result = await self._search_mail(query, context)
            else:
                result = await self._search_in_app(query, platform, context)
                
            return result
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise
            
    async def _search_web(self, query: str, context: ExecutionContext) -> Dict[str, Any]:
        """Perform web search"""
        try:
            # Ensure browser is open
            browser = context.get_variable('preferred_browser', 'Safari')
            
            # Open browser if needed
            if not context.get_variable(f'app_{browser.lower()}_pid'):
                launcher = ApplicationLauncherExecutor()
                await launcher.execute(
                    WorkflowAction(ActionType.OPEN_APP, browser), 
                    context
                )
                await asyncio.sleep(1)  # Wait for browser
                
            # Perform search using AppleScript
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            script = f'''
            tell application "{browser}"
                open location "{search_url}"
                activate
            end tell
            '''
            
            subprocess.run(['osascript', '-e', script])
            
            context.set_variable('last_search_query', query)
            context.set_variable('last_search_url', search_url)
            
            return {
                "status": "success", 
                "message": f"Searching for '{query}'",
                "url": search_url
            }
            
        except Exception as e:
            raise Exception(f"Web search failed: {str(e)}")
            
    async def _search_files(self, query: str, context: ExecutionContext) -> Dict[str, Any]:
        """Search for files"""
        try:
            # Use mdfind on macOS for Spotlight search
            cmd = ['mdfind', query]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            files = result.stdout.strip().split('\n') if result.stdout else []
            files = [f for f in files if f]  # Filter empty
            
            context.set_variable('search_results', files)
            
            # Open Finder with search if requested
            if context.get_variable('open_finder_search', True):
                script = f'''
                tell application "Finder"
                    activate
                    set search_window to make new Finder window
                    set toolbar visible of search_window to true
                end tell
                '''
                subprocess.run(['osascript', '-e', script])
                
            return {
                "status": "success",
                "message": f"Found {len(files)} files matching '{query}'",
                "count": len(files),
                "sample": files[:5]  # First 5 results
            }
            
        except Exception as e:
            raise Exception(f"File search failed: {str(e)}")
            
    async def _search_mail(self, query: str, context: ExecutionContext) -> Dict[str, Any]:
        """Search in Mail app"""
        try:
            script = f'''
            tell application "Mail"
                activate
                set search_results to every message whose subject contains "{query}" or content contains "{query}"
                return count of search_results
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script], 
                capture_output=True, 
                text=True
            )
            
            count = int(result.stdout.strip()) if result.stdout else 0
            
            return {
                "status": "success",
                "message": f"Found {count} emails matching '{query}'",
                "count": count
            }
            
        except Exception as e:
            raise Exception(f"Mail search failed: {str(e)}")
            
    async def _search_in_app(self, query: str, app: str, context: ExecutionContext) -> Dict[str, Any]:
        """Search within specific application"""
        try:
            # Ensure app is open
            launcher = ApplicationLauncherExecutor()
            await launcher.execute(
                WorkflowAction(ActionType.OPEN_APP, app), 
                context
            )
            await asyncio.sleep(1)
            
            # Use keyboard shortcut for search (Cmd+F)
            pyautogui.hotkey('cmd', 'f')
            await asyncio.sleep(0.5)
            
            # Type search query
            pyautogui.typewrite(query)
            
            return {
                "status": "success",
                "message": f"Searching for '{query}' in {app}"
            }
            
        except Exception as e:
            raise Exception(f"App search failed: {str(e)}")


class ResourceCheckerExecutor(BaseActionExecutor):
    """Executor for checking resources (email, calendar, etc.)"""
    
    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Check specified resource"""
        try:
            resource = action.target.lower()
            
            if resource in ['email', 'mail']:
                result = await self._check_email(context)
            elif resource in ['calendar', 'schedule']:
                result = await self._check_calendar(context)
            elif resource in ['weather']:
                result = await self._check_weather(context)
            elif resource in ['notifications']:
                result = await self._check_notifications(context)
            else:
                result = await self._check_generic_resource(resource, context)
                
            return result
            
        except Exception as e:
            logger.error(f"Resource check failed: {e}")
            raise
            
    async def _check_email(self, context: ExecutionContext) -> Dict[str, Any]:
        """Check email"""
        try:
            script = '''
            tell application "Mail"
                set unread_count to count of (every message of inbox whose read status is false)
                return unread_count
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script], 
                capture_output=True, 
                text=True
            )
            
            unread_count = int(result.stdout.strip()) if result.stdout else 0
            
            # Open Mail if unread messages
            if unread_count > 0:
                subprocess.run(['open', '-a', 'Mail'])
                
            context.set_variable('unread_emails', unread_count)
            
            return {
                "status": "success",
                "message": f"You have {unread_count} unread email(s)",
                "count": unread_count
            }
            
        except Exception as e:
            raise Exception(f"Email check failed: {str(e)}")
            
    async def _check_calendar(self, context: ExecutionContext) -> Dict[str, Any]:
        """Check calendar for events"""
        try:
            # Get today's events
            script = '''
            tell application "Calendar"
                set today to current date
                set tomorrow to today + 1 * days
                set today's time to 0
                set tomorrow's time to 0
                
                set todaysEvents to {}
                repeat with cal in calendars
                    set todaysEvents to todaysEvents & (every event of cal whose start date ≥ today and start date < tomorrow)
                end repeat
                
                return count of todaysEvents
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script], 
                capture_output=True, 
                text=True
            )
            
            event_count = int(result.stdout.strip()) if result.stdout else 0
            
            context.set_variable('todays_events', event_count)
            
            return {
                "status": "success",
                "message": f"You have {event_count} event(s) today",
                "count": event_count
            }
            
        except Exception as e:
            raise Exception(f"Calendar check failed: {str(e)}")
            
    async def _check_weather(self, context: ExecutionContext) -> Dict[str, Any]:
        """Check weather"""
        try:
            # Open Weather app
            subprocess.run(['open', '-a', 'Weather'])
            
            # In production, would integrate with weather API
            return {
                "status": "success",
                "message": "Weather app opened"
            }
            
        except Exception as e:
            raise Exception(f"Weather check failed: {str(e)}")
            
    async def _check_notifications(self, context: ExecutionContext) -> Dict[str, Any]:
        """Check notifications"""
        try:
            # Click notification center
            pyautogui.moveTo(pyautogui.size()[0] - 10, 10)
            pyautogui.click()
            
            return {
                "status": "success",
                "message": "Notification center opened"
            }
            
        except Exception as e:
            raise Exception(f"Notification check failed: {str(e)}")
            
    async def _check_generic_resource(self, resource: str, context: ExecutionContext) -> Dict[str, Any]:
        """Check generic resource"""
        # Attempt to open associated app
        launcher = ApplicationLauncherExecutor()
        try:
            await launcher.execute(
                WorkflowAction(ActionType.OPEN_APP, resource), 
                context
            )
            return {
                "status": "success",
                "message": f"Opened {resource}"
            }
        except Exception:
            return {
                "status": "unknown",
                "message": f"Cannot check {resource}"
            }


class ItemCreatorExecutor(BaseActionExecutor):
    """Executor for creating items (documents, events, etc.)"""
    
    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Create specified item"""
        try:
            item_type = action.target.lower()
            
            if 'document' in item_type:
                result = await self._create_document(item_type, context)
            elif 'event' in item_type or 'meeting' in item_type:
                result = await self._create_calendar_event(context)
            elif 'email' in item_type:
                result = await self._create_email(context)
            elif 'note' in item_type:
                result = await self._create_note(context)
            else:
                result = await self._create_generic_item(item_type, context)
                
            return result
            
        except Exception as e:
            logger.error(f"Item creation failed: {e}")
            raise
            
    async def _create_document(self, doc_type: str, context: ExecutionContext) -> Dict[str, Any]:
        """Create a new document"""
        try:
            # Determine app based on document type
            if 'word' in doc_type:
                app = 'Microsoft Word'
                file_ext = 'docx'
            elif 'excel' in doc_type or 'spreadsheet' in doc_type:
                app = 'Microsoft Excel'
                file_ext = 'xlsx'
            elif 'powerpoint' in doc_type or 'presentation' in doc_type:
                app = 'Microsoft PowerPoint'
                file_ext = 'pptx'
            else:
                app = 'TextEdit'
                file_ext = 'txt'
                
            # Launch app
            launcher = ApplicationLauncherExecutor()
            await launcher.execute(
                WorkflowAction(ActionType.OPEN_APP, app), 
                context
            )
            await asyncio.sleep(2)
            
            # Create new document (Cmd+N)
            pyautogui.hotkey('cmd', 'n')
            
            # Set document context
            doc_name = f"Document_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{file_ext}"
            context.set_variable('current_document', doc_name)
            
            return {
                "status": "success",
                "message": f"Created new {doc_type} in {app}",
                "document": doc_name
            }
            
        except Exception as e:
            raise Exception(f"Document creation failed: {str(e)}")
            
    async def _create_calendar_event(self, context: ExecutionContext) -> Dict[str, Any]:
        """Create calendar event"""
        try:
            # Open Calendar
            subprocess.run(['open', '-a', 'Calendar'])
            await asyncio.sleep(1)
            
            # Create new event (Cmd+N)
            pyautogui.hotkey('cmd', 'n')
            
            return {
                "status": "success",
                "message": "New calendar event dialog opened"
            }
            
        except Exception as e:
            raise Exception(f"Calendar event creation failed: {str(e)}")
            
    async def _create_email(self, context: ExecutionContext) -> Dict[str, Any]:
        """Create new email"""
        try:
            # Open Mail
            subprocess.run(['open', '-a', 'Mail'])
            await asyncio.sleep(1)
            
            # Create new email (Cmd+N)
            pyautogui.hotkey('cmd', 'n')
            
            return {
                "status": "success",
                "message": "New email compose window opened"
            }
            
        except Exception as e:
            raise Exception(f"Email creation failed: {str(e)}")
            
    async def _create_note(self, context: ExecutionContext) -> Dict[str, Any]:
        """Create new note"""
        try:
            # Open Notes
            subprocess.run(['open', '-a', 'Notes'])
            await asyncio.sleep(1)
            
            # Create new note (Cmd+N)
            pyautogui.hotkey('cmd', 'n')
            
            return {
                "status": "success",
                "message": "New note created"
            }
            
        except Exception as e:
            raise Exception(f"Note creation failed: {str(e)}")
            
    async def _create_generic_item(self, item_type: str, context: ExecutionContext) -> Dict[str, Any]:
        """Create generic item"""
        return {
            "status": "unsupported",
            "message": f"Creating {item_type} is not yet supported"
        }


class NotificationMuterExecutor(BaseActionExecutor):
    """Executor for muting notifications"""
    
    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Mute notifications"""
        try:
            target = action.target.lower() if action.target else 'all'
            
            if os.uname().sysname == "Darwin":  # macOS
                result = await self._mute_macos_notifications(target, context)
            else:
                result = {"status": "unsupported", "message": "Platform not supported"}
                
            return result
            
        except Exception as e:
            logger.error(f"Failed to mute notifications: {e}")
            raise
            
    async def _mute_macos_notifications(self, target: str, context: ExecutionContext) -> Dict[str, Any]:
        """Mute macOS notifications"""
        try:
            if target in ['all', 'notifications']:
                # Enable Do Not Disturb
                script = '''
                tell application "System Events"
                    tell process "SystemUIServer"
                        key down option
                        click menu bar item "Control Center" of menu bar 1
                        key up option
                    end tell
                end tell
                '''
                subprocess.run(['osascript', '-e', script])
                
                context.set_variable('dnd_enabled', True)
                
                return {
                    "status": "success",
                    "message": "Do Not Disturb enabled"
                }
            else:
                # App-specific notification muting would require more complex implementation
                return {
                    "status": "partial",
                    "message": f"Cannot mute {target} specifically, enabled Do Not Disturb instead"
                }
                
        except Exception as e:
            raise Exception(f"Notification muting failed: {str(e)}")


# Executor factory functions for configuration-driven loading
async def unlock_system(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for system unlock"""
    executor = SystemUnlockExecutor()
    return await executor.execute(action, context)

async def open_application(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for opening applications"""
    executor = ApplicationLauncherExecutor()
    return await executor.execute(action, context)

async def perform_search(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for search"""
    executor = SearchExecutor()
    return await executor.execute(action, context)

async def check_resource(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for checking resources"""
    executor = ResourceCheckerExecutor()
    return await executor.execute(action, context)

async def create_item(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for creating items"""
    executor = ItemCreatorExecutor()
    return await executor.execute(action, context)

async def mute_notifications(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for muting notifications"""
    executor = NotificationMuterExecutor()
    return await executor.execute(action, context)