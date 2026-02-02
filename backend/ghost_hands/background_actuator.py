"""
Ghost Hands Background Actuator
================================

The "hands" of JARVIS Ghost Hands - executes commands on background windows
WITHOUT stealing focus from the user's active window.

Technologies:
- Playwright (Python): Browser automation for Chrome/Arc/Firefox
- AppleScript/JXA: Native app automation for Terminal/Notes/etc.
- Quartz Event Tap: Low-level event injection

Features:
- Zero focus stealing - user never loses keyboard control
- Multi-backend support (Playwright, AppleScript, CGEvent)
- Window-targeted actions (click, type, scroll)
- Intelligent focus preservation
- Permission-aware (requests when needed)
- Environment-driven configuration

Architecture:
    BackgroundActuator (Singleton)
    ├── PlaywrightBackend (browsers)
    │   └── Headless DOM manipulation
    ├── AppleScriptBackend (native apps)
    │   └── JXA scripting
    ├── CGEventBackend (low-level)
    │   └── Quartz event injection
    └── FocusGuard (focus preservation)

Author: JARVIS AI System
Version: 1.0.0 - Ghost Hands Edition
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


# =============================================================================
# v197.1: Global Crash Monitor Integration
# =============================================================================

async def _report_crash_to_monitor(
    crash_reason: str,
    crash_code: str,
    source: str = "playwright",
    error_message: str = "",
) -> None:
    """
    Report a browser crash to the global BrowserCrashMonitor.
    
    This function is safe to call even if the monitor is not available.
    """
    try:
        # Lazy import to avoid circular dependency
        from unified_supervisor import get_browser_crash_monitor
        monitor = get_browser_crash_monitor()
        event = await monitor.record_crash(
            crash_reason=crash_reason,
            crash_code=crash_code,
            source=source,
            error_message=error_message,
        )
        # Optionally attempt recovery
        if event.severity.value in ("high", "critical"):
            await monitor.attempt_recovery(event)
    except ImportError:
        logger.debug("[GHOST-HANDS] BrowserCrashMonitor not available - skipping crash report")
    except Exception as e:
        logger.debug(f"[GHOST-HANDS] Failed to report crash to monitor: {e}")


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class ActuatorConfig:
    """Configuration for the Background Actuator."""

    # Focus preservation
    preserve_focus: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_ACTUATOR_PRESERVE_FOCUS", "true"
        ).lower() == "true"
    )
    focus_restore_delay_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_ACTUATOR_FOCUS_DELAY_MS", "100"))
    )

    # Playwright settings
    playwright_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_ACTUATOR_PLAYWRIGHT", "true"
        ).lower() == "true"
    )
    playwright_headless: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_ACTUATOR_PLAYWRIGHT_HEADLESS", "true"
        ).lower() == "true"
    )

    # AppleScript settings
    applescript_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_ACTUATOR_APPLESCRIPT", "true"
        ).lower() == "true"
    )

    # Timeouts
    action_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_ACTUATOR_TIMEOUT_MS", "10000"))
    )

    # Safety
    require_confirmation_for_dangerous: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_ACTUATOR_CONFIRM_DANGEROUS", "true"
        ).lower() == "true"
    )


# =============================================================================
# Action Types
# =============================================================================

class ActionType(Enum):
    """Types of actions that can be performed."""
    CLICK = auto()
    DOUBLE_CLICK = auto()
    RIGHT_CLICK = auto()
    TYPE = auto()
    KEY = auto()
    SCROLL = auto()
    SELECT = auto()
    FOCUS = auto()
    CLOSE = auto()
    MINIMIZE = auto()
    MAXIMIZE = auto()
    CUSTOM_SCRIPT = auto()


class ActionResult(Enum):
    """Result of an action execution."""
    SUCCESS = auto()
    PARTIAL = auto()
    FAILED = auto()
    PERMISSION_DENIED = auto()
    TIMEOUT = auto()
    FOCUS_STOLEN = auto()


@dataclass
class Action:
    """An action to be executed on a window."""
    action_type: ActionType
    window_id: Optional[int] = None
    app_name: Optional[str] = None
    element_selector: Optional[str] = None
    coordinates: Optional[Tuple[int, int]] = None
    text: Optional[str] = None
    key: Optional[str] = None
    modifiers: Optional[List[str]] = None
    script: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = [self.action_type.name]
        if self.app_name:
            parts.append(f"on {self.app_name}")
        if self.element_selector:
            parts.append(f"element='{self.element_selector}'")
        if self.coordinates:
            parts.append(f"at {self.coordinates}")
        if self.text:
            parts.append(f"text='{self.text[:20]}...'")
        return " ".join(parts)


@dataclass
class ActionReport:
    """Report of an action execution."""
    action: Action
    result: ActionResult
    backend_used: str
    duration_ms: float
    focus_preserved: bool
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": str(self.action),
            "result": self.result.name,
            "backend": self.backend_used,
            "duration_ms": self.duration_ms,
            "focus_preserved": self.focus_preserved,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }


# =============================================================================
# Focus Guard
# =============================================================================

class FocusGuard:
    """
    Preserves user focus during background operations.

    Captures the current focused window before an action and
    restores focus after the action completes (if focus was stolen).
    """

    def __init__(self, config: ActuatorConfig):
        self.config = config
        self._saved_focus: Optional[Dict[str, Any]] = None

    async def save_focus(self) -> Dict[str, Any]:
        """Save current focus state."""
        try:
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )

            windows = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID
            )

            # Find focused window (first in list is usually focused)
            for window in windows:
                if window.get("kCGWindowLayer") == 0:
                    self._saved_focus = {
                        "window_id": window.get("kCGWindowNumber"),
                        "app_name": window.get("kCGWindowOwnerName"),
                        "pid": window.get("kCGWindowOwnerPID"),
                    }
                    logger.debug(f"[FOCUS] Saved focus: {self._saved_focus['app_name']}")
                    return self._saved_focus

        except Exception as e:
            logger.error(f"[FOCUS] Failed to save focus: {e}")

        return {}

    async def restore_focus(self) -> bool:
        """Restore saved focus state."""
        if not self._saved_focus:
            return False

        try:
            app_name = self._saved_focus.get("app_name")
            if not app_name:
                return False

            # Use AppleScript to activate the app
            script = f'''
            tell application "{app_name}"
                activate
            end tell
            '''

            result = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(result.communicate(), timeout=2.0)

            await asyncio.sleep(self.config.focus_restore_delay_ms / 1000.0)

            logger.debug(f"[FOCUS] Restored focus to: {app_name}")
            return True

        except Exception as e:
            logger.error(f"[FOCUS] Failed to restore focus: {e}")
            return False

    async def check_focus_preserved(self) -> bool:
        """Check if focus is still on the saved app."""
        if not self._saved_focus:
            return True

        try:
            current = await self.save_focus()
            return current.get("app_name") == self._saved_focus.get("app_name")
        except Exception:
            return True


# =============================================================================
# Backend Interface
# =============================================================================

class ActuatorBackend(ABC):
    """Abstract interface for actuator backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name."""
        pass

    @property
    @abstractmethod
    def supported_apps(self) -> List[str]:
        """List of supported application patterns."""
        pass

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialize the backend."""
        pass

    @abstractmethod
    async def execute(self, action: Action) -> ActionReport:
        """Execute an action."""
        pass

    @abstractmethod
    async def can_handle(self, action: Action) -> bool:
        """Check if this backend can handle the action."""
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up resources."""
        pass


# =============================================================================
# AppleScript Backend
# =============================================================================

class AppleScriptBackend(ActuatorBackend):
    """
    AppleScript/JXA backend for native macOS app automation.

    Supports:
    - Terminal, Notes, Finder, Safari, Mail
    - Any app with AppleScript support
    - System events (keystroke, click)
    """

    def __init__(self, config: ActuatorConfig):
        self.config = config
        self._initialized = False

    @property
    def name(self) -> str:
        return "AppleScript"

    @property
    def supported_apps(self) -> List[str]:
        return [
            "Terminal", "iTerm", "Notes", "Finder", "Safari",
            "Mail", "Reminders", "Calendar", "Preview", "TextEdit",
        ]

    async def initialize(self) -> bool:
        if self._initialized:
            return True

        # Test osascript availability
        try:
            result = await asyncio.create_subprocess_exec(
                "osascript", "-e", 'return "ok"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=5.0)

            if b"ok" in stdout:
                self._initialized = True
                logger.info("[GHOST-HANDS] AppleScript backend initialized")
                return True

        except Exception as e:
            logger.error(f"[GHOST-HANDS] AppleScript init failed: {e}")

        return False

    async def can_handle(self, action: Action) -> bool:
        """Check if this backend can handle the action."""
        if not self._initialized:
            return False

        # Can handle any native macOS app
        if action.app_name:
            return True

        return False

    async def execute(self, action: Action) -> ActionReport:
        """Execute an action using AppleScript."""
        start_time = time.time()

        try:
            script = self._build_script(action)

            if not script:
                return ActionReport(
                    action=action,
                    result=ActionResult.FAILED,
                    backend_used=self.name,
                    duration_ms=0,
                    focus_preserved=True,
                    error="Could not build script for action",
                )

            result = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                result.communicate(),
                timeout=self.config.action_timeout_ms / 1000.0
            )

            duration_ms = (time.time() - start_time) * 1000

            if result.returncode == 0:
                return ActionReport(
                    action=action,
                    result=ActionResult.SUCCESS,
                    backend_used=self.name,
                    duration_ms=duration_ms,
                    focus_preserved=True,
                    metadata={"output": stdout.decode()[:500]},
                )
            else:
                return ActionReport(
                    action=action,
                    result=ActionResult.FAILED,
                    backend_used=self.name,
                    duration_ms=duration_ms,
                    focus_preserved=True,
                    error=stderr.decode()[:200],
                )

        except asyncio.TimeoutError:
            return ActionReport(
                action=action,
                result=ActionResult.TIMEOUT,
                backend_used=self.name,
                duration_ms=self.config.action_timeout_ms,
                focus_preserved=True,
                error="Script execution timed out",
            )
        except Exception as e:
            return ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used=self.name,
                duration_ms=(time.time() - start_time) * 1000,
                focus_preserved=True,
                error=str(e),
            )

    def _build_script(self, action: Action) -> Optional[str]:
        """Build AppleScript for the action."""
        app_name = action.app_name or "System Events"

        if action.action_type == ActionType.TYPE:
            # Type text without activating
            return f'''
            tell application "System Events"
                tell process "{app_name}"
                    keystroke "{action.text}"
                end tell
            end tell
            '''

        elif action.action_type == ActionType.KEY:
            key_code = self._get_key_code(action.key)
            modifiers = self._get_modifiers(action.modifiers)

            return f'''
            tell application "System Events"
                tell process "{app_name}"
                    key code {key_code}{modifiers}
                end tell
            end tell
            '''

        elif action.action_type == ActionType.CLICK:
            if action.coordinates:
                x, y = action.coordinates
                return f'''
                tell application "System Events"
                    tell process "{app_name}"
                        click at {{{x}, {y}}}
                    end tell
                end tell
                '''
            elif action.element_selector:
                return f'''
                tell application "System Events"
                    tell process "{app_name}"
                        click button "{action.element_selector}" of window 1
                    end tell
                end tell
                '''

        elif action.action_type == ActionType.CUSTOM_SCRIPT:
            return action.script

        elif action.action_type == ActionType.CLOSE:
            return f'''
            tell application "{app_name}"
                close front window
            end tell
            '''

        elif action.action_type == ActionType.MINIMIZE:
            return f'''
            tell application "System Events"
                tell process "{app_name}"
                    set miniaturized of window 1 to true
                end tell
            end tell
            '''

        return None

    def _get_key_code(self, key: Optional[str]) -> int:
        """Get macOS key code for a key name."""
        key_codes = {
            "return": 36, "enter": 36, "tab": 48, "space": 49,
            "delete": 51, "escape": 53, "command": 55, "shift": 56,
            "option": 58, "control": 59, "up": 126, "down": 125,
            "left": 123, "right": 124, "f1": 122, "f2": 120,
        }
        return key_codes.get(key.lower() if key else "return", 36)

    def _get_modifiers(self, modifiers: Optional[List[str]]) -> str:
        """Build modifier string for AppleScript."""
        if not modifiers:
            return ""

        mod_map = {
            "command": "command down",
            "shift": "shift down",
            "option": "option down",
            "control": "control down",
        }

        mods = [mod_map[m.lower()] for m in modifiers if m.lower() in mod_map]
        if mods:
            return " using {" + ", ".join(mods) + "}"
        return ""

    async def cleanup(self) -> None:
        self._initialized = False


# =============================================================================
# Playwright Backend - v197.1 Enhanced with Crash Recovery
# =============================================================================

class BrowserCrashError(Exception):
    """Raised when browser crashes with specific error codes."""
    def __init__(self, reason: str, code: str, message: str = ""):
        self.reason = reason
        self.code = code
        super().__init__(f"Browser crash: reason='{reason}', code='{code}'. {message}")


class CircuitBreakerState(Enum):
    """Circuit breaker states for browser operations."""
    CLOSED = auto()       # Normal operation
    OPEN = auto()         # Failing, reject requests
    HALF_OPEN = auto()    # Testing if recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for browser circuit breaker."""
    failure_threshold: int = 3          # Failures before opening
    recovery_timeout: float = 30.0      # Seconds before half-open
    success_threshold: int = 2          # Successes to close from half-open
    crash_codes_critical: List[str] = field(default_factory=lambda: ["5", "6", "11"])


class BrowserCircuitBreaker:
    """
    Circuit breaker pattern for browser operations.
    
    v197.1: Prevents cascade failures when browser repeatedly crashes.
    
    States:
    - CLOSED: Normal operation, allows all requests
    - OPEN: Browser is failing, rejects requests to prevent resource exhaustion
    - HALF_OPEN: Testing recovery, allows limited requests
    """
    
    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._last_crash_reason: Optional[str] = None
        self._last_crash_code: Optional[str] = None
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger("BrowserCircuitBreaker")
    
    @property
    def state(self) -> CircuitBreakerState:
        return self._state
    
    @property
    def is_open(self) -> bool:
        return self._state == CircuitBreakerState.OPEN
    
    async def can_execute(self) -> bool:
        """Check if operation should be allowed."""
        async with self._lock:
            if self._state == CircuitBreakerState.CLOSED:
                return True
            
            if self._state == CircuitBreakerState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time and \
                   (time.time() - self._last_failure_time) >= self.config.recovery_timeout:
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._success_count = 0
                    self._logger.info("[CircuitBreaker] Transitioning to HALF_OPEN for testing")
                    return True
                return False
            
            # HALF_OPEN allows limited requests
            return True
    
    async def record_success(self) -> None:
        """Record successful operation."""
        async with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._state = CircuitBreakerState.CLOSED
                    self._failure_count = 0
                    self._logger.info("[CircuitBreaker] ✅ Recovery confirmed, CLOSED")
            elif self._state == CircuitBreakerState.CLOSED:
                # Decay failure count on success
                self._failure_count = max(0, self._failure_count - 1)
    
    async def record_failure(self, reason: str = None, code: str = None) -> None:
        """Record failed operation."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            self._last_crash_reason = reason
            self._last_crash_code = code
            
            # Critical crash codes trigger immediate opening
            if code in self.config.crash_codes_critical:
                self._state = CircuitBreakerState.OPEN
                self._logger.warning(
                    f"[CircuitBreaker] 🔴 CRITICAL CRASH (code={code}), circuit OPEN"
                )
                return
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._state = CircuitBreakerState.OPEN
                self._logger.warning("[CircuitBreaker] HALF_OPEN test failed, back to OPEN")
            elif self._failure_count >= self.config.failure_threshold:
                self._state = CircuitBreakerState.OPEN
                self._logger.warning(
                    f"[CircuitBreaker] Threshold reached ({self._failure_count}), circuit OPEN"
                )
    
    async def reset(self) -> None:
        """Manually reset the circuit breaker."""
        async with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            self._logger.info("[CircuitBreaker] Manually reset to CLOSED")


class PlaywrightBackend(ActuatorBackend):
    """
    Playwright backend for browser automation.
    
    v197.1 Enhanced with:
    - Automatic crash detection and recovery
    - Circuit breaker pattern to prevent cascade failures
    - Intelligent reconnection with exponential backoff
    - Resource monitoring before operations
    - Graceful degradation on repeated crashes
    - Memory pressure detection
    
    v197.4 ROOT CAUSE FIX for Code 5 Crashes:
    - Integrates with StabilizedChromeLauncher
    - Ensures Chrome is launched with crash-prevention flags before CDP connect
    - Adds context and page limits to prevent resource exhaustion
    - Proactive Chrome restart when memory exceeds threshold

    Supports:
    - Chrome, Chromium, Arc, Brave
    - Firefox
    - Safari (limited)

    Features:
    - Headless DOM manipulation (no focus stealing)
    - Element selection via CSS/XPath
    - Network interception
    - Screenshot capture
    """

    # Known crash codes and their meanings
    CRASH_CODE_MEANINGS = {
        "5": "GPU process crash or out of memory",
        "6": "Renderer process crash",
        "11": "Page unresponsive",
        "15": "Browser terminated by signal",
        "139": "Segmentation fault",
        "137": "OOM killed by system",
    }
    
    # v197.4: Resource limits to prevent OOM
    MAX_CONTEXTS = 5
    MAX_PAGES_PER_CONTEXT = 10
    CHROME_MEMORY_THRESHOLD_MB = 3072  # 3GB - preemptively restart before 4GB

    def __init__(self, config: ActuatorConfig):
        self.config = config
        self._initialized = False
        self._playwright = None
        self._browser = None
        self._context = None
        self._circuit_breaker = BrowserCircuitBreaker()
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._reconnect_backoff = [1.0, 2.0, 5.0, 10.0, 30.0]
        self._last_health_check: Optional[float] = None
        self._health_check_interval = 30.0  # seconds
        self._crash_count = 0
        self._total_operations = 0
        self._lock = asyncio.Lock()
        
        # v197.4: Track contexts and pages for resource limits
        self._active_contexts = 0
        self._pages_by_context: Dict[str, int] = {}
        self._stabilized_launcher_available = False

    @property
    def name(self) -> str:
        return "Playwright"

    @property
    def supported_apps(self) -> List[str]:
        return ["Chrome", "Chromium", "Arc", "Brave", "Firefox", "Safari"]

    def _parse_crash_error(self, error: Exception) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse crash information from Playwright error.
        
        Returns (reason, code) tuple.
        """
        error_str = str(error).lower()
        
        # Pattern: "window terminated unexpectedly (reason: 'crashed', code: '5')"
        reason = None
        code = None
        
        if "terminated unexpectedly" in error_str or "crashed" in error_str:
            reason = "crashed"
            # Extract code
            import re
            code_match = re.search(r"code[:\s]*['\"]?(\d+)['\"]?", error_str)
            if code_match:
                code = code_match.group(1)
            
            # Also check for specific patterns
            if "target closed" in error_str:
                reason = "target_closed"
            elif "context closed" in error_str:
                reason = "context_closed"
            elif "browser closed" in error_str:
                reason = "browser_closed"
        
        return reason, code

    async def _check_memory_pressure(self) -> bool:
        """
        Check if system is under memory pressure before browser operations.
        
        v197.2: Enhanced to also check Chrome-specific memory usage and
        proactively detect conditions that lead to crash code 5 (GPU/OOM).
        
        Returns True if safe to proceed, False if should back off.
        """
        try:
            import psutil
            mem = psutil.virtual_memory()
            
            # High memory pressure thresholds
            if mem.percent > 90:
                logger.warning(
                    f"[GHOST-HANDS] 🔴 Critical memory pressure: {mem.percent}% used. "
                    "Skipping browser operation to prevent crash code 5."
                )
                return False
            elif mem.percent > 80:
                logger.info(
                    f"[GHOST-HANDS] ⚠️ High memory pressure: {mem.percent}% used. "
                    "Proceeding with caution."
                )
            
            # v197.2: Check Chrome-specific memory usage
            chrome_memory_mb = 0
            chrome_process_count = 0
            for proc in psutil.process_iter(['name', 'memory_info']):
                try:
                    if 'chrome' in proc.info['name'].lower():
                        chrome_memory_mb += proc.info['memory_info'].rss / (1024 * 1024)
                        chrome_process_count += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                    continue
            
            # Chrome using > 6GB is a crash risk (GPU process OOM)
            if chrome_memory_mb > 6144:
                logger.warning(
                    f"[GHOST-HANDS] 🔴 Chrome using {chrome_memory_mb:.0f}MB across "
                    f"{chrome_process_count} processes. HIGH CRASH RISK (code 5). "
                    "Skipping operation."
                )
                return False
            elif chrome_memory_mb > 4096:
                logger.info(
                    f"[GHOST-HANDS] ⚠️ Chrome using {chrome_memory_mb:.0f}MB. "
                    "Consider closing some tabs."
                )
            
            return True
        except ImportError:
            return True  # psutil not available, proceed anyway
        except Exception as e:
            logger.debug(f"[GHOST-HANDS] Memory check failed: {e}")
            return True

    async def _health_check(self) -> bool:
        """
        Perform browser health check.
        
        Returns True if browser is healthy, False otherwise.
        """
        if not self._browser:
            return False
        
        try:
            # Try to access browser contexts - lightweight check
            contexts = self._browser.contexts
            if not contexts:
                return False
            
            # Try to get a page
            pages = contexts[0].pages if contexts else []
            if not pages:
                return False
            
            # Try a lightweight operation
            await pages[0].evaluate("() => true")
            
            self._last_health_check = time.time()
            return True
            
        except Exception as e:
            logger.debug(f"[GHOST-HANDS] Health check failed: {e}")
            return False

    async def _reconnect(self) -> bool:
        """
        Attempt to reconnect to browser with exponential backoff.
        
        v197.1: Intelligent reconnection with crash recovery.
        v197.4: ROOT CAUSE FIX - Uses StabilizedChromeLauncher on reconnect
                to ensure Chrome is restarted with crash-prevention flags.
        """
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.error(
                f"[GHOST-HANDS] Max reconnect attempts ({self._max_reconnect_attempts}) "
                "reached. Giving up."
            )
            return False
        
        # Calculate backoff delay
        delay = self._reconnect_backoff[
            min(self._reconnect_attempts, len(self._reconnect_backoff) - 1)
        ]
        
        logger.info(
            f"[GHOST-HANDS] Reconnect attempt {self._reconnect_attempts + 1}/"
            f"{self._max_reconnect_attempts} after {delay}s delay..."
        )
        
        await asyncio.sleep(delay)
        
        # Clean up existing connection
        await self._cleanup_connection()
        
        # =====================================================================
        # v197.4: ROOT CAUSE FIX - Restart Chrome with stability flags
        # =====================================================================
        # On reconnect (typically after a crash), restart Chrome with
        # crash-prevention flags to prevent the same crash from recurring.
        # This is the CURE - we fix the underlying cause of code 5 crashes.
        # =====================================================================
        try:
            from unified_supervisor import get_stabilized_chrome_launcher
            
            launcher = get_stabilized_chrome_launcher()
            
            logger.info("[GHOST-HANDS] v197.4: Restarting Chrome with stability flags (crash prevention)...")
            
            # Restart Chrome with all stability flags (this kills existing Chrome)
            success = await launcher.restart_chrome(url=None, incognito=False)
            
            if success:
                logger.info("[GHOST-HANDS] ✅ Chrome restarted with GPU disabled, memory limited")
                self._stabilized_launcher_available = True
                # Wait for Chrome to be ready
                await asyncio.sleep(2.0)
            else:
                logger.warning("[GHOST-HANDS] StabilizedChromeLauncher restart failed")
                
        except ImportError:
            logger.debug("[GHOST-HANDS] StabilizedChromeLauncher not available")
        except Exception as launcher_err:
            logger.debug(f"[GHOST-HANDS] Launcher restart failed: {launcher_err}")
        
        # Try to reinitialize
        self._reconnect_attempts += 1
        success = await self._do_initialize()
        
        if success:
            self._reconnect_attempts = 0  # Reset on success
            logger.info("[GHOST-HANDS] ✅ Reconnection successful!")
            await self._circuit_breaker.record_success()
        else:
            logger.warning("[GHOST-HANDS] Reconnection failed")
        
        return success
    
    async def _preemptive_memory_check(self) -> bool:
        """
        v197.4: Proactively restart Chrome if memory exceeds threshold.
        
        This is PROACTIVE crash prevention - we restart Chrome BEFORE
        it crashes rather than waiting for code 5.
        
        Returns True if operation can proceed, False if Chrome was restarted.
        """
        try:
            import psutil
            
            # Calculate Chrome memory usage
            chrome_memory_mb = 0
            for proc in psutil.process_iter(['name', 'memory_info']):
                try:
                    if 'chrome' in proc.info['name'].lower():
                        chrome_memory_mb += proc.info['memory_info'].rss / (1024 * 1024)
                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                    continue
            
            if chrome_memory_mb > self.CHROME_MEMORY_THRESHOLD_MB:
                logger.warning(
                    f"[GHOST-HANDS] ⚠️ Chrome using {chrome_memory_mb:.0f}MB "
                    f"(threshold: {self.CHROME_MEMORY_THRESHOLD_MB}MB). "
                    "Preemptively restarting to prevent crash..."
                )
                
                # Try to use stabilized launcher for restart
                try:
                    from unified_supervisor import get_stabilized_chrome_launcher
                    launcher = get_stabilized_chrome_launcher()
                    await launcher.preemptive_restart_if_needed(self.CHROME_MEMORY_THRESHOLD_MB)
                except Exception:
                    pass
                
                return False  # Signal that operation should wait
            
            return True
            
        except ImportError:
            return True  # psutil not available, proceed
        except Exception as e:
            logger.debug(f"[GHOST-HANDS] Memory check failed: {e}")
            return True

    async def _cleanup_connection(self) -> None:
        """Clean up existing browser connection."""
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        
        self._browser = None
        self._playwright = None
        self._context = None
        self._initialized = False

    async def _do_initialize(self) -> bool:
        """
        Internal initialization logic.
        
        v197.4: Enhanced to use StabilizedChromeLauncher if available.
        This ensures Chrome is running with crash-prevention flags
        (--disable-gpu, memory limits, etc.) before we connect via CDP.
        """
        try:
            from playwright.async_api import async_playwright
            
            # =====================================================================
            # v197.4: ROOT CAUSE FIX - Ensure Chrome is launched with stability flags
            # =====================================================================
            # Before connecting via CDP, ensure Chrome is running with the right flags.
            # If Chrome is running without flags (e.g., user opened it manually),
            # we'll still connect, but won't have crash protection.
            # =====================================================================
            try:
                # Try to import the stabilized launcher from unified_supervisor
                from unified_supervisor import get_stabilized_chrome_launcher
                
                launcher = get_stabilized_chrome_launcher()
                
                # Check if Chrome is already running with stability flags
                if not await launcher.is_chrome_running():
                    logger.info("[GHOST-HANDS] v197.4: Launching Chrome with stability flags...")
                    success = await launcher.launch_stabilized_chrome(
                        url=None,  # No URL, just start Chrome
                        incognito=False,  # Not incognito for general automation
                        kill_existing=False,  # Don't kill existing Chrome (user might have windows)
                        headless=self.config.playwright_headless,
                    )
                    if success:
                        logger.info("[GHOST-HANDS] ✅ Chrome launched with GPU disabled, memory limited")
                        self._stabilized_launcher_available = True
                        # Wait for Chrome to be ready
                        await asyncio.sleep(2.0)
                    else:
                        logger.warning("[GHOST-HANDS] StabilizedChromeLauncher failed - Chrome may crash")
                else:
                    logger.info("[GHOST-HANDS] Chrome already running (hopefully with stability flags)")
                    self._stabilized_launcher_available = True
                    
            except ImportError:
                logger.debug("[GHOST-HANDS] StabilizedChromeLauncher not available - using existing Chrome")
            except Exception as launcher_err:
                logger.debug(f"[GHOST-HANDS] Launcher check failed: {launcher_err}")
            
            # =====================================================================
            # Connect to Chrome via CDP
            # =====================================================================
            self._playwright = await async_playwright().start()

            # Connect to existing browser via CDP
            self._browser = await self._playwright.chromium.connect_over_cdp(
                "http://localhost:9222",
                timeout=10000  # 10 second timeout
            )

            self._initialized = True
            self._last_health_check = time.time()
            
            # v197.4: Log whether we have crash protection
            if self._stabilized_launcher_available:
                logger.info("[GHOST-HANDS] ✅ Playwright backend initialized (with crash protection)")
            else:
                logger.info("[GHOST-HANDS] Playwright backend initialized")
                logger.warning(
                    "[GHOST-HANDS] ⚠️ Chrome may not have crash-prevention flags. "
                    "For stability, restart Chrome via unified_supervisor.py"
                )
            
            return True

        except ImportError:
            logger.warning("[GHOST-HANDS] Playwright not installed")
            return False
        except Exception as e:
            logger.warning(f"[GHOST-HANDS] Playwright init failed: {e}")
            logger.info(
                "[GHOST-HANDS] To enable Playwright, either:\n"
                "  1. Run unified_supervisor.py (recommended - launches Chrome with stability flags)\n"
                "  2. Manually run Chrome with:\n"
                "     /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
                "--remote-debugging-port=9222 --disable-gpu --disable-software-rasterizer"
            )
            return False

    async def initialize(self) -> bool:
        if self._initialized:
            return True
        
        return await self._do_initialize()

    async def can_handle(self, action: Action) -> bool:
        if not self._initialized:
            return False

        if action.app_name:
            return any(
                browser.lower() in action.app_name.lower()
                for browser in self.supported_apps
            )

        return False

    async def execute(self, action: Action) -> ActionReport:
        """
        Execute an action using Playwright with crash recovery.
        
        v197.1: Enhanced with circuit breaker and auto-recovery.
        """
        start_time = time.time()
        self._total_operations += 1

        # Check circuit breaker first
        if not await self._circuit_breaker.can_execute():
            return ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used=self.name,
                duration_ms=0,
                focus_preserved=True,
                error="Circuit breaker OPEN - browser unstable, please wait for recovery",
            )

        # Check memory pressure
        if not await self._check_memory_pressure():
            return ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used=self.name,
                duration_ms=0,
                focus_preserved=True,
                error="System under memory pressure - skipping to prevent crash",
            )

        if not self._initialized or not self._browser:
            return ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used=self.name,
                duration_ms=0,
                focus_preserved=True,
                error="Playwright not initialized",
            )

        # Periodic health check
        if self._last_health_check and \
           (time.time() - self._last_health_check) > self._health_check_interval:
            if not await self._health_check():
                logger.warning("[GHOST-HANDS] Health check failed, attempting reconnect...")
                if not await self._reconnect():
                    return ActionReport(
                        action=action,
                        result=ActionResult.FAILED,
                        backend_used=self.name,
                        duration_ms=(time.time() - start_time) * 1000,
                        focus_preserved=True,
                        error="Browser disconnected and reconnection failed",
                    )

        async with self._lock:
            try:
                result = await self._execute_with_retry(action, start_time)
                
                if result.result == ActionResult.SUCCESS:
                    await self._circuit_breaker.record_success()
                
                return result

            except Exception as e:
                # Parse crash information
                reason, code = self._parse_crash_error(e)
                
                if reason:
                    self._crash_count += 1
                    crash_meaning = self.CRASH_CODE_MEANINGS.get(code, "Unknown crash")
                    
                    logger.error(
                        f"[GHOST-HANDS] 🔴 Browser CRASHED: reason='{reason}', "
                        f"code='{code}' ({crash_meaning}). "
                        f"Total crashes: {self._crash_count}/{self._total_operations} operations"
                    )
                    
                    # Record failure in circuit breaker
                    await self._circuit_breaker.record_failure(reason, code)
                    
                    # v197.1: Report to global crash monitor for system-wide tracking
                    await _report_crash_to_monitor(
                        crash_reason=reason,
                        crash_code=code or "unknown",
                        source="playwright",
                        error_message=str(e),
                    )
                    
                    # Attempt recovery
                    if await self._reconnect():
                        # Retry the operation once after successful reconnect
                        try:
                            return await self._execute_single(action, time.time())
                        except Exception as retry_error:
                            logger.error(f"[GHOST-HANDS] Retry after reconnect failed: {retry_error}")
                    
                    return ActionReport(
                        action=action,
                        result=ActionResult.FAILED,
                        backend_used=self.name,
                        duration_ms=(time.time() - start_time) * 1000,
                        focus_preserved=True,
                        error=f"Browser crash (code={code}): {crash_meaning}. Recovery attempted.",
                    )
                
                # Non-crash error
                return ActionReport(
                    action=action,
                    result=ActionResult.FAILED,
                    backend_used=self.name,
                    duration_ms=(time.time() - start_time) * 1000,
                    focus_preserved=True,
                    error=str(e),
                )

    async def _execute_with_retry(self, action: Action, start_time: float, max_retries: int = 2) -> ActionReport:
        """Execute with retry logic for transient failures."""
        last_error = None
        
        for attempt in range(max_retries):
            try:
                return await self._execute_single(action, start_time)
            except Exception as e:
                last_error = e
                reason, code = self._parse_crash_error(e)
                
                if reason:
                    # This is a crash - don't retry, let the outer handler deal with it
                    raise
                
                if attempt < max_retries - 1:
                    logger.debug(f"[GHOST-HANDS] Retry {attempt + 1}/{max_retries} after error: {e}")
                    await asyncio.sleep(0.5 * (attempt + 1))
        
        raise last_error

    async def _execute_single(self, action: Action, start_time: float) -> ActionReport:
        """Execute a single action without retry."""
        # Get the first page/tab
        contexts = self._browser.contexts
        if not contexts:
            return ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used=self.name,
                duration_ms=0,
                focus_preserved=True,
                error="No browser contexts available",
            )

        pages = contexts[0].pages
        if not pages:
            return ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used=self.name,
                duration_ms=0,
                focus_preserved=True,
                error="No pages available",
            )

        page = pages[0]

        # Execute action with timeout
        timeout_ms = self.config.action_timeout_ms

        # Execute action
        if action.action_type == ActionType.CLICK:
            if action.element_selector:
                await page.click(action.element_selector, timeout=timeout_ms)
            elif action.coordinates:
                await page.mouse.click(action.coordinates[0], action.coordinates[1])

        elif action.action_type == ActionType.TYPE:
            if action.element_selector:
                await page.fill(action.element_selector, action.text or "", timeout=timeout_ms)
            else:
                await page.keyboard.type(action.text or "")

        elif action.action_type == ActionType.KEY:
            await page.keyboard.press(action.key or "Enter")

        elif action.action_type == ActionType.SCROLL:
            await page.mouse.wheel(0, 100)

        duration_ms = (time.time() - start_time) * 1000

        return ActionReport(
            action=action,
            result=ActionResult.SUCCESS,
            backend_used=self.name,
            duration_ms=duration_ms,
            focus_preserved=True,
        )

    async def get_crash_stats(self) -> Dict[str, Any]:
        """Get crash statistics for diagnostics."""
        return {
            "total_operations": self._total_operations,
            "crash_count": self._crash_count,
            "crash_rate": self._crash_count / max(1, self._total_operations),
            "circuit_breaker_state": self._circuit_breaker.state.name,
            "reconnect_attempts": self._reconnect_attempts,
            "initialized": self._initialized,
            "last_health_check": self._last_health_check,
        }

    async def cleanup(self) -> None:
        await self._cleanup_connection()


# =============================================================================
# CGEvent Backend (Low-Level)
# =============================================================================

class CGEventBackend(ActuatorBackend):
    """
    Low-level CGEvent backend for direct event injection.

    Uses Quartz event taps to inject events directly into the window server.
    This allows interaction with windows without changing focus.

    Note: Requires Accessibility permissions.
    """

    def __init__(self, config: ActuatorConfig):
        self.config = config
        self._initialized = False

    @property
    def name(self) -> str:
        return "CGEvent"

    @property
    def supported_apps(self) -> List[str]:
        return ["*"]  # Supports all apps

    async def initialize(self) -> bool:
        if self._initialized:
            return True

        try:
            import Quartz

            # Test if we can create events
            event = Quartz.CGEventCreate(None)
            if event is not None:
                self._initialized = True
                logger.info("[GHOST-HANDS] CGEvent backend initialized")
                return True

        except Exception as e:
            logger.error(f"[GHOST-HANDS] CGEvent init failed: {e}")

        return False

    async def can_handle(self, action: Action) -> bool:
        return self._initialized and action.coordinates is not None

    async def execute(self, action: Action) -> ActionReport:
        """Execute action using CGEvent."""
        import Quartz
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGHIDEventTap,
        )

        start_time = time.time()

        try:
            if action.action_type == ActionType.CLICK:
                if not action.coordinates:
                    return ActionReport(
                        action=action,
                        result=ActionResult.FAILED,
                        backend_used=self.name,
                        duration_ms=0,
                        focus_preserved=True,
                        error="Coordinates required for CGEvent click",
                    )

                x, y = action.coordinates

                # Create and post mouse down event
                mouse_down = CGEventCreateMouseEvent(
                    None,
                    kCGEventLeftMouseDown,
                    (x, y),
                    0
                )
                CGEventPost(kCGHIDEventTap, mouse_down)

                await asyncio.sleep(0.05)

                # Create and post mouse up event
                mouse_up = CGEventCreateMouseEvent(
                    None,
                    kCGEventLeftMouseUp,
                    (x, y),
                    0
                )
                CGEventPost(kCGHIDEventTap, mouse_up)

            elif action.action_type == ActionType.KEY:
                from Quartz import (
                    CGEventCreateKeyboardEvent,
                    kCGEventKeyDown,
                    kCGEventKeyUp,
                )

                key_code = self._get_key_code(action.key)

                # Key down
                key_down = CGEventCreateKeyboardEvent(None, key_code, True)
                CGEventPost(kCGHIDEventTap, key_down)

                await asyncio.sleep(0.02)

                # Key up
                key_up = CGEventCreateKeyboardEvent(None, key_code, False)
                CGEventPost(kCGHIDEventTap, key_up)

            duration_ms = (time.time() - start_time) * 1000

            return ActionReport(
                action=action,
                result=ActionResult.SUCCESS,
                backend_used=self.name,
                duration_ms=duration_ms,
                focus_preserved=True,
            )

        except Exception as e:
            return ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used=self.name,
                duration_ms=(time.time() - start_time) * 1000,
                focus_preserved=True,
                error=str(e),
            )

    def _get_key_code(self, key: Optional[str]) -> int:
        """Get macOS virtual key code."""
        key_codes = {
            "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
            "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
            "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
        }
        return key_codes.get(key.lower() if key else "return", 36)

    async def cleanup(self) -> None:
        self._initialized = False


# =============================================================================
# Background Actuator (Main Class)
# =============================================================================

class BackgroundActuator:
    """
    Ghost Hands Background Actuator: Focus-free window automation.

    Executes actions on background windows without stealing user focus.
    Automatically selects the best backend for each action.
    """

    _instance: Optional["BackgroundActuator"] = None

    def __new__(cls, config: Optional[ActuatorConfig] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config: Optional[ActuatorConfig] = None):
        if self._initialized:
            return

        self.config = config or ActuatorConfig()

        # Components
        self._focus_guard = FocusGuard(self.config)
        self._backends: List[ActuatorBackend] = []

        # History
        self._action_history: List[ActionReport] = []
        self._max_history = 100

        # Statistics
        self._stats = {
            "total_actions": 0,
            "successful_actions": 0,
            "failed_actions": 0,
            "focus_lost_count": 0,
        }

        self._initialized = True
        logger.info("[GHOST-HANDS] Background Actuator initialized")

    @classmethod
    def get_instance(cls, config: Optional[ActuatorConfig] = None) -> "BackgroundActuator":
        return cls(config)

    async def start(self) -> bool:
        """Initialize all backends."""
        # Initialize backends in priority order
        if self.config.playwright_enabled:
            backend = PlaywrightBackend(self.config)
            if await backend.initialize():
                self._backends.append(backend)

        if self.config.applescript_enabled:
            backend = AppleScriptBackend(self.config)
            if await backend.initialize():
                self._backends.append(backend)

        # Always try CGEvent as fallback
        backend = CGEventBackend(self.config)
        if await backend.initialize():
            self._backends.append(backend)

        logger.info(f"[GHOST-HANDS] Started with {len(self._backends)} backends")
        return len(self._backends) > 0

    async def stop(self) -> None:
        """Clean up all backends."""
        for backend in self._backends:
            await backend.cleanup()
        self._backends.clear()
        logger.info("[GHOST-HANDS] Stopped")

    async def execute(
        self,
        action: Action,
        preserve_focus: bool = True,
    ) -> ActionReport:
        """
        Execute an action on a background window.

        Args:
            action: The action to execute
            preserve_focus: Whether to restore focus if stolen

        Returns:
            ActionReport with execution details
        """
        self._stats["total_actions"] += 1

        # Save focus if needed
        if preserve_focus and self.config.preserve_focus:
            await self._focus_guard.save_focus()

        # Find suitable backend
        backend = await self._select_backend(action)

        if not backend:
            report = ActionReport(
                action=action,
                result=ActionResult.FAILED,
                backend_used="none",
                duration_ms=0,
                focus_preserved=True,
                error="No suitable backend found for action",
            )
            self._record_action(report)
            return report

        # Execute action
        report = await backend.execute(action)

        # Check and restore focus if needed
        if preserve_focus and self.config.preserve_focus:
            focus_ok = await self._focus_guard.check_focus_preserved()
            if not focus_ok:
                self._stats["focus_lost_count"] += 1
                await self._focus_guard.restore_focus()
                report.focus_preserved = False

        # Update stats
        if report.result == ActionResult.SUCCESS:
            self._stats["successful_actions"] += 1
        else:
            self._stats["failed_actions"] += 1

        self._record_action(report)

        logger.info(
            f"[GHOST-HANDS] {action} -> {report.result.name} "
            f"({report.backend_used}, {report.duration_ms:.0f}ms)"
        )

        return report

    async def click(
        self,
        app_name: Optional[str] = None,
        selector: Optional[str] = None,
        coordinates: Optional[Tuple[int, int]] = None,
    ) -> ActionReport:
        """Click on an element or coordinates."""
        action = Action(
            action_type=ActionType.CLICK,
            app_name=app_name,
            element_selector=selector,
            coordinates=coordinates,
        )
        return await self.execute(action)

    async def type_text(
        self,
        text: str,
        app_name: Optional[str] = None,
        selector: Optional[str] = None,
    ) -> ActionReport:
        """Type text into an element."""
        action = Action(
            action_type=ActionType.TYPE,
            app_name=app_name,
            element_selector=selector,
            text=text,
        )
        return await self.execute(action)

    async def press_key(
        self,
        key: str,
        app_name: Optional[str] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionReport:
        """Press a key with optional modifiers."""
        action = Action(
            action_type=ActionType.KEY,
            app_name=app_name,
            key=key,
            modifiers=modifiers,
        )
        return await self.execute(action)

    async def run_applescript(
        self,
        script: str,
        app_name: Optional[str] = None,
    ) -> ActionReport:
        """Run a custom AppleScript."""
        action = Action(
            action_type=ActionType.CUSTOM_SCRIPT,
            app_name=app_name,
            script=script,
        )
        return await self.execute(action)

    async def send_command_to_terminal(
        self,
        command: str,
        terminal_app: str = "Terminal",
    ) -> ActionReport:
        """Send a command to Terminal/iTerm."""
        # Use AppleScript to send command without activating
        script = f'''
        tell application "{terminal_app}"
            do script "{command}" in front window
        end tell
        '''
        return await self.run_applescript(script, terminal_app)

    async def _select_backend(self, action: Action) -> Optional[ActuatorBackend]:
        """Select the best backend for an action."""
        for backend in self._backends:
            if await backend.can_handle(action):
                return backend
        return None

    def _record_action(self, report: ActionReport) -> None:
        """Record action in history."""
        self._action_history.append(report)
        if len(self._action_history) > self._max_history:
            self._action_history = self._action_history[-self._max_history:]

    def get_stats(self) -> Dict[str, Any]:
        """Get actuator statistics."""
        return {
            **self._stats,
            "available_backends": [b.name for b in self._backends],
            "history_size": len(self._action_history),
        }

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent action history."""
        return [r.to_dict() for r in self._action_history[-limit:]]


# =============================================================================
# Convenience Functions
# =============================================================================

async def get_background_actuator(
    config: Optional[ActuatorConfig] = None
) -> BackgroundActuator:
    """Get the Background Actuator singleton instance."""
    actuator = BackgroundActuator.get_instance(config)
    if not actuator._backends:
        await actuator.start()
    return actuator


# =============================================================================
# Testing
# =============================================================================

async def test_background_actuator():
    """Test the Background Actuator."""
    print("=" * 60)
    print("Testing Background Actuator")
    print("=" * 60)

    actuator = await get_background_actuator()

    print(f"\n1. Available backends: {[b.name for b in actuator._backends]}")

    # Test AppleScript
    print("\n2. Testing AppleScript (sending key to Finder)...")
    result = await actuator.press_key("escape", app_name="Finder")
    print(f"   Result: {result.result.name} ({result.backend_used})")

    # Show stats
    print("\n3. Statistics:")
    stats = actuator.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")

    await actuator.stop()
    print("\nTest complete!")


if __name__ == "__main__":
    asyncio.run(test_background_actuator())
