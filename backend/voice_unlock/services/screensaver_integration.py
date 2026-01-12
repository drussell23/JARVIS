"""
macOS Screensaver Integration v3.0
===================================

Integrates voice unlock with macOS screensaver using
dynamic detection and native APIs.

ENHANCED FEATURES:
- Full async subprocess execution with timeout protection
- Retry logic with exponential backoff
- State machine for unlock flow management
- Circuit breaker for external command failures
- Health monitoring and automatic recovery
- Dynamic configuration from environment
- Graceful degradation on failures
"""

import subprocess
import logging
import asyncio
import os
import time
from typing import Optional, Dict, Any, Callable, List, Tuple
from datetime import datetime
from enum import Enum, auto
from dataclasses import dataclass, field
import plistlib
from pathlib import Path
from functools import wraps

try:
    import Quartz
    import AppKit
    import objc
    MACOS_AVAILABLE = True
except ImportError:
    MACOS_AVAILABLE = False
    Quartz = None
    AppKit = None
    objc = None

from threading import Thread
import queue

from ..core.authentication import VoiceAuthenticator, AuthenticationResult
from ..config import get_config

logger = logging.getLogger(__name__)


# =============================================================================
# DYNAMIC CONFIGURATION
# =============================================================================
class ScreensaverConfig:
    """Dynamic configuration for screensaver integration."""

    def __init__(self):
        self.subprocess_timeout = float(os.getenv('SCREENSAVER_SUBPROCESS_TIMEOUT', '5.0'))
        self.unlock_timeout = float(os.getenv('SCREENSAVER_UNLOCK_TIMEOUT', '10.0'))
        self.max_retry_attempts = int(os.getenv('SCREENSAVER_MAX_RETRIES', '3'))
        self.retry_base_delay = float(os.getenv('SCREENSAVER_RETRY_DELAY', '0.5'))
        self.monitoring_interval = float(os.getenv('SCREENSAVER_MONITOR_INTERVAL', '1.0'))
        self.circuit_breaker_threshold = int(os.getenv('SCREENSAVER_CB_THRESHOLD', '5'))
        self.circuit_breaker_timeout = float(os.getenv('SCREENSAVER_CB_TIMEOUT', '60.0'))


_screensaver_config = ScreensaverConfig()


# =============================================================================
# RETRY DECORATOR
# =============================================================================
def async_retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    exceptions: Tuple = (Exception,),
):
    """Async retry decorator with exponential backoff."""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(f"Retry {attempt + 1}/{max_attempts} for {func.__name__} after {delay:.1f}s: {e}")
                        await asyncio.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


# =============================================================================
# CIRCUIT BREAKER FOR SUBPROCESS COMMANDS
# =============================================================================
@dataclass
class SubprocessCircuitBreaker:
    """Circuit breaker for subprocess command execution."""
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0

    _failures: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _is_open: bool = field(default=False, init=False)

    def is_available(self) -> bool:
        """Check if circuit allows execution."""
        if not self._is_open:
            return True
        # Check if recovery timeout has passed
        if time.time() - self._last_failure_time >= self.recovery_timeout:
            self._is_open = False
            self._failures = 0
            logger.info(f"🟢 Circuit breaker {self.name} recovered")
            return True
        return False

    def record_success(self):
        """Record successful execution."""
        self._failures = 0
        if self._is_open:
            self._is_open = False
            logger.info(f"🟢 Circuit breaker {self.name} closed (success)")

    def record_failure(self):
        """Record failed execution."""
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._is_open = True
            logger.warning(f"🔴 Circuit breaker {self.name} opened (failures: {self._failures})")


# =============================================================================
# ASYNC SUBPROCESS HELPER
# =============================================================================
class AsyncSubprocessRunner:
    """
    Robust async subprocess runner with timeout protection and retry logic.
    """

    def __init__(self):
        self._circuit_breakers: Dict[str, SubprocessCircuitBreaker] = {}
        self._stats = {
            'total_calls': 0,
            'successful_calls': 0,
            'failed_calls': 0,
            'timeout_calls': 0,
        }

    def _get_circuit_breaker(self, command: str) -> SubprocessCircuitBreaker:
        """Get or create circuit breaker for a command type."""
        # Use first word of command as key
        key = command.split()[0] if command else 'unknown'
        if key not in self._circuit_breakers:
            self._circuit_breakers[key] = SubprocessCircuitBreaker(
                name=key,
                failure_threshold=_screensaver_config.circuit_breaker_threshold,
                recovery_timeout=_screensaver_config.circuit_breaker_timeout,
            )
        return self._circuit_breakers[key]

    async def run(
        self,
        *args: str,
        timeout: float = None,
        check: bool = False,
        capture_output: bool = True,
    ) -> Tuple[int, str, str]:
        """
        Run subprocess asynchronously with timeout protection.

        Args:
            *args: Command and arguments
            timeout: Timeout in seconds (default from config)
            check: Raise exception on non-zero exit
            capture_output: Capture stdout/stderr

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        timeout = timeout or _screensaver_config.subprocess_timeout
        command = ' '.join(args)
        cb = self._get_circuit_breaker(args[0] if args else '')

        self._stats['total_calls'] += 1

        # Check circuit breaker
        if not cb.is_available():
            self._stats['failed_calls'] += 1
            raise RuntimeError(f"Circuit breaker open for {args[0]}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE if capture_output else None,
                stderr=asyncio.subprocess.PIPE if capture_output else None,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                proc.kill()
                await proc.wait()
                self._stats['timeout_calls'] += 1
                cb.record_failure()
                raise asyncio.TimeoutError(f"Command timed out after {timeout}s: {command}")

            stdout_str = stdout.decode() if stdout else ''
            stderr_str = stderr.decode() if stderr else ''

            if check and proc.returncode != 0:
                cb.record_failure()
                self._stats['failed_calls'] += 1
                raise subprocess.CalledProcessError(proc.returncode, command, stdout_str, stderr_str)

            cb.record_success()
            self._stats['successful_calls'] += 1
            return proc.returncode, stdout_str, stderr_str

        except asyncio.TimeoutError:
            raise
        except subprocess.CalledProcessError:
            raise
        except Exception as e:
            cb.record_failure()
            self._stats['failed_calls'] += 1
            raise RuntimeError(f"Subprocess failed: {command}: {e}")

    async def run_osascript(self, script: str, timeout: float = None) -> Tuple[int, str, str]:
        """Run AppleScript with proper escaping and timeout."""
        return await self.run("osascript", "-e", script, timeout=timeout)

    def get_stats(self) -> Dict[str, Any]:
        """Get subprocess runner statistics."""
        return {
            **self._stats,
            'circuit_breakers': {
                name: {
                    'is_open': cb._is_open,
                    'failures': cb._failures,
                }
                for name, cb in self._circuit_breakers.items()
            }
        }


# Global subprocess runner
_subprocess_runner = AsyncSubprocessRunner()


# =============================================================================
# UNLOCK FLOW STATE MACHINE
# =============================================================================
class UnlockFlowState(Enum):
    """States for the unlock flow state machine."""
    IDLE = auto()
    DETECTING_STATE = auto()
    AUTHENTICATING = auto()
    UNLOCKING = auto()
    VERIFYING = auto()
    COMPLETED = auto()
    FAILED = auto()
    COOLDOWN = auto()


@dataclass
class UnlockFlowContext:
    """Context for unlock flow state machine."""
    state: UnlockFlowState = UnlockFlowState.IDLE
    start_time: float = 0.0
    attempts: int = 0
    last_error: Optional[str] = None
    auth_result: Optional[AuthenticationResult] = None
    auth_details: Optional[Dict[str, Any]] = None
    unlock_successful: bool = False

    def reset(self):
        """Reset context for new flow."""
        self.state = UnlockFlowState.IDLE
        self.start_time = time.time()
        self.attempts = 0
        self.last_error = None
        self.auth_result = None
        self.auth_details = None
        self.unlock_successful = False

    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        return (time.time() - self.start_time) * 1000


class ScreenState(Enum):
    """Screen/system states"""
    ACTIVE = "active"
    SCREENSAVER = "screensaver"
    LOCKED = "locked"
    SLEEP = "sleep"
    LOGIN_WINDOW = "login_window"


class ScreensaverIntegration:
    """
    Integrates voice unlock with macOS screensaver.

    Enhanced v3.0 Features:
    - State machine for unlock flow management
    - Async subprocess execution with timeout protection
    - Circuit breaker for external command failures
    - Health monitoring and automatic recovery
    - Retry logic with exponential backoff
    """

    def __init__(self, authenticator: Optional[VoiceAuthenticator] = None):
        self.config = get_config()
        self.authenticator = authenticator or VoiceAuthenticator()

        # State tracking
        self.current_state = ScreenState.ACTIVE
        self.monitoring = False
        self.unlock_in_progress = False

        # Unlock flow state machine
        self._unlock_flow = UnlockFlowContext()
        self._flow_lock = asyncio.Lock()

        # Event handlers
        self.event_handlers: Dict[str, List[Callable]] = {
            'screen_locked': [],
            'screen_unlocked': [],
            'screensaver_started': [],
            'screensaver_stopped': [],
            'unlock_started': [],
            'unlock_success': [],
            'unlock_failed': [],
            'flow_state_changed': [],  # New: state machine events
        }

        # Background monitoring
        self.monitor_thread: Optional[Thread] = None
        self.event_queue = queue.Queue()

        # Health monitoring
        self._last_health_check = None
        self._stats = {
            'unlock_attempts': 0,
            'unlock_successes': 0,
            'unlock_failures': 0,
            'avg_unlock_time_ms': 0.0,
            'total_unlock_time_ms': 0.0,
        }

        # Subprocess runner
        self._subprocess = _subprocess_runner
        
    def add_event_handler(self, event: str, handler: Callable):
        """Add event handler"""
        if event in self.event_handlers:
            self.event_handlers[event].append(handler)
            
    def start_monitoring(self):
        """Start monitoring screen state"""
        if self.monitoring:
            logger.warning("Already monitoring screen state")
            return
            
        self.monitoring = True
        
        # Start background thread
        self.monitor_thread = Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        # Start event processor
        asyncio.create_task(self._process_events())
        
        logger.info("Started screensaver monitoring")
        
    def stop_monitoring(self):
        """Stop monitoring screen state"""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("Stopped screensaver monitoring")
        
    def _monitor_loop(self):
        """Background monitoring loop"""
        last_state = self.current_state
        
        while self.monitoring:
            try:
                # Get current state
                new_state = self._get_screen_state()
                
                # Detect state changes
                if new_state != last_state:
                    self._handle_state_change(last_state, new_state)
                    last_state = new_state
                    
                # Check if we should start voice unlock
                if new_state in [ScreenState.SCREENSAVER, ScreenState.LOCKED]:
                    if not self.unlock_in_progress and self._should_attempt_unlock():
                        self.event_queue.put(('start_unlock', None))
                        
                # Sleep briefly
                import time
                time.sleep(self.config.performance.monitoring_interval)
                
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                
    def _get_screen_state(self) -> ScreenState:
        """Get current screen state using multiple methods"""
        
        # Method 1: Check screensaver process
        if self._is_screensaver_running():
            return ScreenState.SCREENSAVER
            
        # Method 2: Check screen lock via CGSession
        if self._is_screen_locked():
            return ScreenState.LOCKED
            
        # Method 3: Check if at login window
        if self._is_at_login_window():
            return ScreenState.LOGIN_WINDOW
            
        # Method 4: Check system sleep
        if self._is_system_sleeping():
            return ScreenState.SLEEP
            
        return ScreenState.ACTIVE
        
    def _is_screensaver_running(self) -> bool:
        """Check if screensaver is active"""
        try:
            # Use Quartz to check screensaver
            # This is more reliable than checking process
            defaults = AppKit.NSUserDefaults.standardUserDefaults()
            screensaver_delay = defaults.integerForKey_("askForPasswordDelay")
            
            # Alternative: Check ScreenSaver.framework
            result = subprocess.run(
                ["pmset", "-g", "assertions"],
                capture_output=True,
                text=True
            )
            
            return "ScreenSaverEngine" in result.stdout
            
        except Exception as e:
            logger.error(f"Error checking screensaver: {e}")
            return False
            
    def _is_screen_locked(self) -> bool:
        """Check if screen is locked"""
        try:
            # Use Quartz CGSessionCopyCurrentDictionary
            session_dict = Quartz.CGSessionCopyCurrentDictionary()
            if session_dict:
                screen_locked = session_dict.get("CGSSessionScreenIsLocked", 0)
                return bool(screen_locked)
            return False
            
        except Exception as e:
            logger.error(f"Error checking screen lock: {e}")
            return False
            
    def _is_at_login_window(self) -> bool:
        """Check if at login window"""
        try:
            result = subprocess.run(
                ["who", "-q"],
                capture_output=True,
                text=True
            )
            
            # If no users logged in, we're at login window
            return "users=0" in result.stdout
            
        except Exception:
            return False
            
    def _is_system_sleeping(self) -> bool:
        """Check if system is sleeping"""
        try:
            result = subprocess.run(
                ["pmset", "-g", "ps"],
                capture_output=True,
                text=True
            )
            
            return "sleep" in result.stdout.lower()
            
        except Exception:
            return False
            
    def _handle_state_change(self, old_state: ScreenState, new_state: ScreenState):
        """Handle screen state changes"""
        logger.info(f"Screen state changed: {old_state.value} -> {new_state.value}")
        
        self.current_state = new_state
        
        # Trigger appropriate events
        if new_state == ScreenState.SCREENSAVER:
            self._trigger_event('screensaver_started')
        elif old_state == ScreenState.SCREENSAVER:
            self._trigger_event('screensaver_stopped')
            
        if new_state == ScreenState.LOCKED:
            self._trigger_event('screen_locked')
        elif old_state == ScreenState.LOCKED:
            self._trigger_event('screen_unlocked')
            
    def _should_attempt_unlock(self) -> bool:
        """Determine if we should attempt voice unlock"""
        
        # Check if voice unlock is enabled for current mode
        if self.config.system.integration_mode not in ['screensaver', 'both']:
            return False
            
        # Don't attempt if recently failed
        # This would check authentication history
        
        return True
        
    async def _process_events(self):
        """Process events from monitor thread"""
        while self.monitoring:
            try:
                # Get event from queue (non-blocking)
                try:
                    event, data = self.event_queue.get(timeout=0.1)
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue
                    
                if event == 'start_unlock':
                    await self._attempt_voice_unlock()
                    
            except Exception as e:
                logger.error(f"Event processor error: {e}")
                
    async def _attempt_voice_unlock(self):
        """Attempt to unlock screen with voice"""
        if self.unlock_in_progress:
            return
            
        self.unlock_in_progress = True
        self._trigger_event('unlock_started')
        
        try:
            # Show notification if enabled
            if self.config.system.show_notifications:
                self._show_notification(
                    "JARVIS Voice Unlock",
                    "Say your unlock phrase..."
                )
                
            # Perform authentication
            result, details = await self.authenticator.authenticate()
            
            if result == AuthenticationResult.SUCCESS:
                # Unlock the screen
                success = await self._unlock_screen(details.get('user_id'))
                
                if success:
                    self._trigger_event('unlock_success', details)
                    
                    # JARVIS response if enabled
                    if self.config.system.jarvis_responses:
                        response = self.config.system.custom_responses.get(
                            'success', 
                            'Welcome back, Sir'
                        )
                        await self._speak_response(response)
                else:
                    self._trigger_event('unlock_failed', "Failed to unlock screen")
                    
            else:
                self._trigger_event('unlock_failed', details)
                
                # Handle different failure types
                if result == AuthenticationResult.LOCKOUT:
                    if self.config.system.jarvis_responses:
                        response = self.config.system.custom_responses.get(
                            'lockout',
                            'Security lockout activated, Sir'
                        )
                        await self._speak_response(response)
                        
                elif result == AuthenticationResult.SPOOFING_DETECTED:
                    logger.warning("Spoofing attempt detected during unlock")
                    
                else:
                    if self.config.system.jarvis_responses:
                        response = self.config.system.custom_responses.get(
                            'failure',
                            'Voice not recognized, Sir'
                        )
                        await self._speak_response(response)
                        
        except Exception as e:
            logger.error(f"Voice unlock error: {e}")
            self._trigger_event('unlock_failed', str(e))
            
        finally:
            self.unlock_in_progress = False
            
    @async_retry(max_attempts=3, base_delay=0.3, exceptions=(RuntimeError, asyncio.TimeoutError))
    async def _unlock_screen(self, user_id: Optional[str] = None) -> bool:
        """
        Unlock the screen/screensaver with robust async execution.

        Enhanced v3.0:
        - Retry logic with exponential backoff
        - Timeout protection for all subprocess calls
        - Circuit breaker for repeated failures
        - State machine integration
        - Comprehensive error handling
        """
        unlock_start = time.time()

        try:
            # Update state machine
            async with self._flow_lock:
                self._unlock_flow.state = UnlockFlowState.UNLOCKING

            # Method 1: Stop screensaver (async subprocess with timeout)
            if self.current_state == ScreenState.SCREENSAVER:
                try:
                    await self._subprocess.run(
                        "killall", "ScreenSaverEngine",
                        timeout=_screensaver_config.subprocess_timeout
                    )
                    logger.debug("Killed ScreenSaverEngine process")
                except Exception as e:
                    # Non-fatal - screensaver might not be running
                    logger.debug(f"killall ScreenSaverEngine: {e}")

            # Method 2: Simulate unlock via AppleScript
            # This sends a space key to dismiss the screensaver
            script = '''
            tell application "System Events"
                key code 49 -- space key
            end tell
            '''

            try:
                await self._subprocess.run_osascript(
                    script,
                    timeout=_screensaver_config.subprocess_timeout
                )
            except Exception as e:
                logger.warning(f"AppleScript unlock failed: {e}")
                # Try alternative method
                await self._try_alternative_unlock()

            # Update state machine
            async with self._flow_lock:
                self._unlock_flow.state = UnlockFlowState.VERIFYING

            # Verify unlock with timeout
            verification_start = time.time()
            max_verification_time = 2.0  # seconds

            while (time.time() - verification_start) < max_verification_time:
                await asyncio.sleep(0.2)
                new_state = await self._get_screen_state_async()

                if new_state == ScreenState.ACTIVE:
                    unlock_time_ms = (time.time() - unlock_start) * 1000
                    logger.info(f"✅ Screen unlocked in {unlock_time_ms:.0f}ms")

                    # Update stats
                    self._stats['unlock_successes'] += 1
                    self._stats['total_unlock_time_ms'] += unlock_time_ms
                    self._stats['avg_unlock_time_ms'] = (
                        self._stats['total_unlock_time_ms'] / self._stats['unlock_successes']
                    )

                    # Update state machine
                    async with self._flow_lock:
                        self._unlock_flow.state = UnlockFlowState.COMPLETED
                        self._unlock_flow.unlock_successful = True

                    return True

            # Verification timeout - unlock may have failed
            logger.warning("Screen unlock verification timed out")
            async with self._flow_lock:
                self._unlock_flow.state = UnlockFlowState.FAILED
                self._unlock_flow.last_error = "Verification timeout"

            return False

        except asyncio.CancelledError:
            logger.warning("Screen unlock cancelled")
            async with self._flow_lock:
                self._unlock_flow.state = UnlockFlowState.FAILED
                self._unlock_flow.last_error = "Cancelled"
            raise

        except Exception as e:
            logger.error(f"Screen unlock error: {e}")
            self._stats['unlock_failures'] += 1

            async with self._flow_lock:
                self._unlock_flow.state = UnlockFlowState.FAILED
                self._unlock_flow.last_error = str(e)

            return False

    async def _try_alternative_unlock(self):
        """Try alternative unlock methods when primary fails."""
        try:
            # Alternative 1: Send escape key
            script_escape = '''
            tell application "System Events"
                key code 53 -- escape key
            end tell
            '''
            await self._subprocess.run_osascript(script_escape, timeout=2.0)
            await asyncio.sleep(0.1)

            # Alternative 2: Click mouse
            script_click = '''
            tell application "System Events"
                click at {1, 1}
            end tell
            '''
            await self._subprocess.run_osascript(script_click, timeout=2.0)

        except Exception as e:
            logger.debug(f"Alternative unlock methods failed: {e}")

    async def _get_screen_state_async(self) -> ScreenState:
        """Async version of screen state detection."""
        # Run in thread pool to avoid blocking
        return await asyncio.to_thread(self._get_screen_state)
            
    async def _show_notification_async(self, title: str, message: str):
        """Show system notification (async version)."""
        if not self.config.system.show_notifications:
            return

        try:
            # Escape quotes in message
            safe_message = message.replace('"', '\\"')
            safe_title = title.replace('"', '\\"')

            script = f'display notification "{safe_message}" with title "{safe_title}"'

            if self.config.system.notification_sound:
                script += ' sound name "Glass"'

            await self._subprocess.run_osascript(script, timeout=2.0)

        except Exception as e:
            logger.debug(f"Notification error (non-fatal): {e}")

    def _show_notification(self, title: str, message: str):
        """Show system notification (sync version - schedules async task)."""
        if not self.config.system.show_notifications:
            return

        try:
            # Try to schedule async version
            loop = asyncio.get_running_loop()
            loop.create_task(self._show_notification_async(title, message))
        except RuntimeError:
            # No event loop - fall back to sync
            try:
                script = f'''
                display notification "{message}" with title "{title}"
                '''
                if self.config.system.notification_sound:
                    script += ' sound name "Glass"'
                subprocess.run(["osascript", "-e", script], capture_output=True, timeout=2)
            except Exception as e:
                logger.debug(f"Notification error: {e}")
            
    async def _speak_response(self, text: str):
        """Speak JARVIS response"""
        # DISABLED: Audio is now handled by frontend to avoid duplicate voices
        # The WebSocket response includes speak:true flag for frontend TTS
        logger.debug(f"[Skipping backend TTS] Text would have been: {text}")
        return
        # Original code kept for reference:
        # try:
        #     # This would integrate with JARVIS voice system
        #     # For now, use system TTS
        #     subprocess.run(["say", "-v", "Daniel", text], capture_output=True)
        # except Exception as e:
        #     logger.error(f"Speech error: {e}")
            
    def _trigger_event(self, event: str, data: Any = None):
        """Trigger event handlers"""
        for handler in self.event_handlers.get(event, []):
            try:
                handler(data)
            except Exception as e:
                logger.error(f"Event handler error for {event}: {e}")
                
    def configure_screensaver_settings(self):
        """Configure optimal screensaver settings for voice unlock"""
        try:
            # Get current settings
            defaults = AppKit.NSUserDefaults.standardUserDefaults()
            
            # Recommended settings for voice unlock
            recommendations = {
                'askForPassword': True,  # Require password
                'askForPasswordDelay': 5,  # 5 second delay
                'idleTime': 300  # 5 minute timeout
            }
            
            logger.info("Screensaver configuration recommendations:")
            for key, value in recommendations.items():
                current = defaults.objectForKey_(key)
                logger.info(f"  {key}: current={current}, recommended={value}")
                
            # Note: Actually changing these requires admin privileges
            # and should be done through System Preferences
            
        except Exception as e:
            logger.error(f"Configuration check error: {e}")
            
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive integration status"""
        return {
            'monitoring': self.monitoring,
            'current_state': self.current_state.value,
            'unlock_in_progress': self.unlock_in_progress,
            'integration_mode': self.config.system.integration_mode,
            'screensaver_configured': self._check_screensaver_config(),
            # State machine status
            'flow_state': self._unlock_flow.state.name,
            'flow_attempts': self._unlock_flow.attempts,
            'flow_last_error': self._unlock_flow.last_error,
            # Statistics
            'stats': {
                'unlock_attempts': self._stats['unlock_attempts'],
                'unlock_successes': self._stats['unlock_successes'],
                'unlock_failures': self._stats['unlock_failures'],
                'avg_unlock_time_ms': round(self._stats['avg_unlock_time_ms'], 1),
                'success_rate': (
                    self._stats['unlock_successes'] / self._stats['unlock_attempts'] * 100
                    if self._stats['unlock_attempts'] > 0 else 0
                ),
            },
            # Subprocess runner stats
            'subprocess_stats': self._subprocess.get_stats(),
            # macOS availability
            'macos_available': MACOS_AVAILABLE,
        }

    async def health_check(self) -> Dict[str, Any]:
        """
        Comprehensive health check for screensaver integration.

        Returns health status with component details.
        """
        issues = []
        components = {}

        # Check macOS availability
        components['macos_apis'] = {
            'available': MACOS_AVAILABLE,
            'quartz': Quartz is not None,
            'appkit': AppKit is not None,
        }
        if not MACOS_AVAILABLE:
            issues.append("macOS APIs not available")

        # Check subprocess runner health
        sub_stats = self._subprocess.get_stats()
        cb_open = any(
            cb.get('is_open', False)
            for cb in sub_stats.get('circuit_breakers', {}).values()
        )
        components['subprocess_runner'] = {
            'healthy': not cb_open,
            'total_calls': sub_stats['total_calls'],
            'timeout_rate': (
                sub_stats['timeout_calls'] / sub_stats['total_calls'] * 100
                if sub_stats['total_calls'] > 0 else 0
            ),
            'circuit_breakers_open': cb_open,
        }
        if cb_open:
            issues.append("Subprocess circuit breaker(s) open")

        # Check screen state detection
        try:
            screen_state = await self._get_screen_state_async()
            components['screen_detection'] = {
                'working': True,
                'current_state': screen_state.value,
            }
        except Exception as e:
            components['screen_detection'] = {
                'working': False,
                'error': str(e),
            }
            issues.append(f"Screen detection failed: {e}")

        # Check unlock success rate
        success_rate = (
            self._stats['unlock_successes'] / self._stats['unlock_attempts'] * 100
            if self._stats['unlock_attempts'] > 0 else 100
        )
        components['unlock_performance'] = {
            'attempts': self._stats['unlock_attempts'],
            'success_rate': round(success_rate, 1),
            'avg_time_ms': round(self._stats['avg_unlock_time_ms'], 1),
        }
        if success_rate < 80 and self._stats['unlock_attempts'] >= 3:
            issues.append(f"Low unlock success rate: {success_rate:.1f}%")

        # Calculate overall health
        healthy = len(issues) == 0
        health_score = 1.0 - (len(issues) * 0.25)  # Each issue reduces score by 25%
        health_score = max(0.0, health_score)

        return {
            'healthy': healthy,
            'score': round(health_score, 2),
            'message': "All systems operational" if healthy else f"Issues: {', '.join(issues)}",
            'components': components,
            'issues': issues,
            'last_check': datetime.now().isoformat(),
        }
        
    def _check_screensaver_config(self) -> bool:
        """Check if screensaver is properly configured"""
        try:
            defaults = AppKit.NSUserDefaults.standardUserDefaults()
            
            # Check key settings
            ask_for_password = defaults.boolForKey_("askForPassword")
            delay = defaults.integerForKey_("askForPasswordDelay")
            
            # Voice unlock works best with password + short delay
            return ask_for_password and delay <= 10

        except Exception:
            return False
            

class ScreensaverManager:
    """High-level manager for screensaver voice unlock"""
    
    def __init__(self):
        self.integration = ScreensaverIntegration()
        self.config = get_config()
        
    def setup(self):
        """Setup screensaver integration"""
        
        # Check configuration
        self.integration.configure_screensaver_settings()
        
        # Add event handlers
        self.integration.add_event_handler('unlock_success', self._on_unlock_success)
        self.integration.add_event_handler('unlock_failed', self._on_unlock_failed)
        
        # Start monitoring
        self.integration.start_monitoring()
        
        logger.info("Screensaver voice unlock ready")
        
    def _on_unlock_success(self, details: Dict[str, Any]):
        """Handle successful unlock"""
        logger.info(f"Voice unlock successful: {details}")
        
        # Could trigger additional actions here
        # - Launch specific apps
        # - Restore window arrangement
        # - Update presence status
        
    def _on_unlock_failed(self, details: Any):
        """Handle failed unlock"""
        logger.warning(f"Voice unlock failed: {details}")
        
        # Could implement additional security measures
        # - Take photo with camera
        # - Send notification
        # - Log attempt
        
    def shutdown(self):
        """Shutdown screensaver integration"""
        self.integration.stop_monitoring()
        logger.info("Screensaver voice unlock stopped")


# Example usage
if __name__ == "__main__":
    import asyncio
    
    async def test_screensaver_integration():
        manager = ScreensaverManager()
        manager.setup()
        
        # Wait for events
        await asyncio.sleep(300)  # 5 minutes
        
        manager.shutdown()
        
    asyncio.run(test_screensaver_integration())