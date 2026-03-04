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
from pathlib import Path

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


class NavigationExecutor(BaseActionExecutor):
    """Executor for navigation targets (URLs, repositories, local paths)."""

    _REPO_ENV_KEYS = (
        "JARVIS_REPO_PATH",
        "JARVIS_PRIME_PATH",
        "REACTOR_CORE_PATH",
    )

    _REPO_HINT_KEYWORDS = ("repo", "repository", "jarvis", "prime", "reactor")

    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Navigate to a URL, repository, or local filesystem target."""
        target = (
            action.parameters.get("destination")
            or action.target
            or action.description
            or ""
        ).strip()

        if not target:
            return {
                "status": "skipped",
                "message": "No navigation target was provided.",
            }

        # URL navigation (explicit URL or domain-like target)
        if self._looks_like_url(target):
            url = self._normalize_url(target)
            return await self._open_url(url, context)

        # Local filesystem path navigation
        expanded_path = Path(target).expanduser()
        if expanded_path.exists():
            return await self._open_path(expanded_path, context)

        # Repository-aware navigation (JARVIS / Prime / Reactor / dynamic repo names)
        resolved_repo = self._resolve_repository(target)
        if resolved_repo:
            remote_url = self._get_git_remote_url(resolved_repo)
            if remote_url:
                result = await self._open_url(remote_url, context)
                result["repository_path"] = str(resolved_repo)
                return result
            return await self._open_path(resolved_repo, context)

        # Graceful fallback: unresolved navigation target should not crash workflow.
        return {
            "status": "skipped",
            "message": f"Could not resolve navigation target '{target}'.",
            "target": target,
        }

    def _looks_like_url(self, target: str) -> bool:
        lower_target = target.strip().lower()
        if lower_target.startswith(("http://", "https://")):
            return True

        # Accept domain-like targets (e.g., github.com/drussell23/JARVIS-AI-Agent).
        return "." in lower_target and " " not in lower_target

    def _normalize_url(self, target: str) -> str:
        stripped = target.strip()
        if stripped.lower().startswith(("http://", "https://")):
            return stripped
        return f"https://{stripped}"

    async def _open_url(self, url: str, context: ExecutionContext) -> Dict[str, Any]:
        # v283.3: Default to Chrome (was Safari). JARVIS uses Chrome for its UI.
        browser = context.get_variable("preferred_browser", os.getenv("JARVIS_DEFAULT_BROWSER", "Google Chrome"))

        try:
            subprocess.run(["open", "-a", browser, url], check=True)
        except Exception:
            # Fallback to system default browser if requested browser is unavailable.
            subprocess.run(["open", url], check=True)

        context.set_variable("last_navigation_url", url)
        return {
            "status": "success",
            "message": f"Opened {url}",
            "destination": url,
        }

    async def _open_path(self, path: Path, context: ExecutionContext) -> Dict[str, Any]:
        subprocess.run(["open", str(path)], check=True)
        context.set_variable("last_navigation_path", str(path))
        return {
            "status": "success",
            "message": f"Opened {path}",
            "destination": str(path),
        }

    def _resolve_repository(self, target: str) -> Optional[Path]:
        normalized_target = target.strip().lower()
        if not normalized_target:
            return None

        if not any(keyword in normalized_target for keyword in self._REPO_HINT_KEYWORDS):
            return None

        discovered = self._discover_repositories()
        if not discovered:
            return None

        best_match: Optional[Path] = None
        best_score = 0

        for alias, repo_path in discovered.items():
            score = self._score_repository_match(normalized_target, alias, repo_path)
            if score > best_score:
                best_score = score
                best_match = repo_path

        return best_match

    def _score_repository_match(self, normalized_target: str, alias: str, repo_path: Path) -> int:
        alias_norm = alias.lower()
        repo_name = repo_path.name.lower()
        score = 0

        priority_terms = ("jarvis prime", "reactor core", "jarvis")
        for term in priority_terms:
            if term in normalized_target:
                term_compact = term.replace(" ", "")
                if term_compact in alias_norm.replace("_", "").replace("-", ""):
                    score += 8
                if term.split()[0] in repo_name:
                    score += 4

        # Generic token matching for dynamic repo names
        for token in normalized_target.replace("-", " ").replace("_", " ").split():
            if len(token) < 3:
                continue
            if token in alias_norm:
                score += 2
            if token in repo_name:
                score += 2

        if "repo" in normalized_target or "repository" in normalized_target:
            score += 1

        # Prefer the current JARVIS repo for generic "jarvis repo" requests.
        if "jarvis" in normalized_target and "prime" not in normalized_target and "reactor" not in normalized_target:
            current_repo = self._find_git_root(Path(__file__).resolve())
            if current_repo and repo_path == current_repo:
                score += 5

        return score

    def _discover_repositories(self) -> Dict[str, Path]:
        discovered: Dict[str, Path] = {}

        def register_repo(path: Path, alias_hint: Optional[str] = None):
            repo_root = self._find_git_root(path)
            if not repo_root:
                return
            alias_candidates = {
                repo_root.name.lower(),
                repo_root.name.lower().replace("-", "_"),
            }
            if alias_hint:
                alias_candidates.add(alias_hint.lower())

            repo_name = repo_root.name.lower()
            if "jarvis" in repo_name and "prime" not in repo_name:
                alias_candidates.add("jarvis")
            if "prime" in repo_name:
                alias_candidates.update({"jarvis_prime", "prime"})
            if "reactor" in repo_name:
                alias_candidates.update({"reactor_core", "reactor"})

            for alias in alias_candidates:
                discovered[alias] = repo_root

        current_repo = self._find_git_root(Path(__file__).resolve())
        if current_repo:
            register_repo(current_repo, alias_hint="jarvis")

        for env_key in self._REPO_ENV_KEYS:
            raw_path = os.getenv(env_key, "").strip()
            if raw_path:
                register_repo(Path(raw_path), alias_hint=env_key.lower().replace("_path", ""))

        # Optional shared repo registry used by supervisor.
        registry_path = Path.home() / ".jarvis" / "repos.json"
        if registry_path.exists():
            try:
                with registry_path.open("r", encoding="utf-8") as f:
                    repo_registry = json.load(f)
                if isinstance(repo_registry, dict):
                    for alias, raw_path in repo_registry.items():
                        if isinstance(raw_path, str) and raw_path.strip():
                            register_repo(Path(raw_path), alias_hint=str(alias))
            except Exception as e:
                logger.debug(f"Failed to parse repository registry {registry_path}: {e}")

        # Discover sibling Trinity repos in common locations.
        search_roots = []
        if current_repo:
            search_roots.append(current_repo.parent)
        search_roots.extend([
            Path.home() / "Documents" / "repos",
            Path.home() / "repos",
        ])

        seen_roots = set()
        for root in search_roots:
            root_key = str(root.resolve()) if root.exists() else str(root)
            if root_key in seen_roots or not root.exists() or not root.is_dir():
                continue
            seen_roots.add(root_key)

            try:
                for child in root.iterdir():
                    if not child.is_dir():
                        continue
                    lowered = child.name.lower()
                    if "jarvis" in lowered or "reactor" in lowered:
                        register_repo(child)
            except Exception as e:
                logger.debug(f"Repository scan skipped for {root}: {e}")

        return discovered

    def _find_git_root(self, path: Path) -> Optional[Path]:
        current = path if path.is_dir() else path.parent
        for candidate in [current, *current.parents]:
            if (candidate / ".git").exists():
                return candidate
        return None

    def _get_git_remote_url(self, repo_path: Path) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return None

            remote = result.stdout.strip()
            if not remote:
                return None

            # Convert SSH remotes into browser-navigable HTTPS URLs.
            if remote.startswith("git@github.com:"):
                remote = remote.replace("git@github.com:", "https://github.com/", 1)
            if remote.startswith("ssh://git@github.com/"):
                remote = remote.replace("ssh://git@github.com/", "https://github.com/", 1)
            if remote.endswith(".git"):
                remote = remote[:-4]

            return remote
        except Exception as e:
            logger.debug(f"Failed to resolve git remote for {repo_path}: {e}")
            return None


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


class GenericFallbackExecutor(BaseActionExecutor):
    """Fallback executor for unregistered action types (UNKNOWN, NAVIGATE, SET, etc.)

    v263.1: Instead of crashing with "No executor for action type", this executor
    logs the unhandled action, records it for future executor development, and
    returns a graceful result so the workflow can continue.
    """

    # Action types that can be mapped to existing executors.
    # Only map to keys that exist in _reroute_action's executor_map.
    _REROUTE_MAP = {
        ActionType.NAVIGATE: "open_app",    # navigate → open browser + URL
        ActionType.START: "open_app",       # start X → open X
        ActionType.READ: "open_app",        # read X → open file
        ActionType.WRITE: "create",         # write X → create X
        ActionType.PREPARE: "check",        # prepare for meeting → check calendar/email
        ActionType.MONITOR: "check",        # monitor X → check X status
        ActionType.ANALYZE: "search",       # analyze X → search for X
        ActionType.SCHEDULE: "check",       # schedule X → check calendar
    }

    async def execute(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Handle unregistered action types gracefully"""
        action_type = action.action_type
        target = action.target or "unspecified"

        logger.warning(
            f"GenericFallbackExecutor handling {action_type.value} "
            f"(target={target!r}) — no dedicated executor registered"
        )

        # For truly UNKNOWN actions, try to interpret via the target/description
        if action_type == ActionType.UNKNOWN:
            return await self._handle_unknown(action, context)

        # For known-but-unimplemented types, try rerouting
        reroute = self._REROUTE_MAP.get(action_type)
        if reroute:
            return await self._reroute_action(action, reroute, context)

        # For everything else, return a skipped result
        return {
            "status": "skipped",
            "action_type": action_type.value,
            "target": target,
            "message": f"No dedicated executor for '{action_type.value}'. "
                       f"Action skipped gracefully.",
            "suggestion": "This action type needs a dedicated executor implementation."
        }

    async def _handle_unknown(self, action: WorkflowAction, context: ExecutionContext) -> Any:
        """Attempt to interpret an UNKNOWN action from its target/description text"""
        target = (action.target or "").lower()
        desc = (action.description or "").lower()
        text = f"{target} {desc}"

        # Try keyword-based rerouting from the raw text
        keyword_map = [
            (["open", "launch", "start", "run"], "open_app"),
            (["search", "find", "look up", "google"], "search"),
            (["check", "show", "review", "status"], "check"),
            (["create", "make", "new", "write"], "create"),
            (["mute", "silence", "quiet", "dnd"], "mute"),
            (["close", "quit", "stop", "kill", "exit"], "system_command"),
        ]

        for keywords, reroute_type in keyword_map:
            if any(kw in text for kw in keywords):
                logger.info(
                    f"UNKNOWN action rerouted to '{reroute_type}' "
                    f"based on keywords in: {text!r}"
                )
                return await self._reroute_action(action, reroute_type, context)

        # Genuinely uninterpretable — skip gracefully
        logger.info(f"UNKNOWN action could not be interpreted: {text!r}")
        return {
            "status": "skipped",
            "action_type": "unknown",
            "target": action.target,
            "message": f"Could not interpret action: '{action.description or action.target}'. "
                       f"Skipping to continue workflow.",
        }

    async def _reroute_action(self, action: WorkflowAction, executor_key: str,
                              context: ExecutionContext) -> Any:
        """Reroute to an existing executor by key"""
        executor_map = {
            "open_app": ApplicationLauncherExecutor,
            "search": SearchExecutor,
            "check": ResourceCheckerExecutor,
            "create": ItemCreatorExecutor,
            "mute": NotificationMuterExecutor,
        }

        executor_cls = executor_map.get(executor_key)
        if executor_cls:
            logger.info(
                f"Rerouting {action.action_type.value} → {executor_key} executor"
            )
            try:
                executor = executor_cls()
                return await executor.execute(action, context)
            except Exception as e:
                logger.warning(
                    f"Rerouted {action.action_type.value} → {executor_key} failed: {e}"
                )
                return {
                    "status": "skipped",
                    "action_type": action.action_type.value,
                    "target": action.target,
                    "message": f"Reroute to '{executor_key}' failed: {e}. Action skipped.",
                }

        # Unmapped reroute target — skip
        return {
            "status": "skipped",
            "action_type": action.action_type.value,
            "target": action.target,
            "message": f"Reroute target '{executor_key}' not yet implemented. Action skipped.",
        }


async def handle_generic_action(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for generic/fallback action handling"""
    executor = GenericFallbackExecutor()
    return await executor.execute(action, context)


# Executor factory functions for configuration-driven loading
async def unlock_system(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for system unlock"""
    executor = SystemUnlockExecutor()
    return await executor.execute(action, context)

async def open_application(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for opening applications"""
    executor = ApplicationLauncherExecutor()
    return await executor.execute(action, context)

async def navigate_to_target(action: WorkflowAction, context: ExecutionContext) -> Any:
    """Factory function for navigation actions"""
    executor = NavigationExecutor()
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
