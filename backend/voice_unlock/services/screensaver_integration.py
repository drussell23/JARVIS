"""
macOS Screensaver Integration
============================

Integrates voice unlock with macOS screensaver using
dynamic detection and native APIs.
"""

import subprocess
import logging
import asyncio
from typing import Optional, Dict, Any, Callable, List
from datetime import datetime
from enum import Enum
import plistlib
from pathlib import Path
import Quartz
import AppKit
import objc
from threading import Thread
import queue

from ..core.authentication import VoiceAuthenticator, AuthenticationResult
from ..config import get_config

logger = logging.getLogger(__name__)


class ScreenState(Enum):
    """Screen/system states"""
    ACTIVE = "active"
    SCREENSAVER = "screensaver"
    LOCKED = "locked"
    SLEEP = "sleep"
    LOGIN_WINDOW = "login_window"


class ScreensaverIntegration:
    """Integrates voice unlock with macOS screensaver"""
    
    def __init__(self, authenticator: Optional[VoiceAuthenticator] = None):
        self.config = get_config()
        self.authenticator = authenticator or VoiceAuthenticator()
        
        # State tracking
        self.current_state = ScreenState.ACTIVE
        self.monitoring = False
        self.unlock_in_progress = False
        
        # Event handlers
        self.event_handlers: Dict[str, List[Callable]] = {
            'screen_locked': [],
            'screen_unlocked': [],
            'screensaver_started': [],
            'screensaver_stopped': [],
            'unlock_started': [],
            'unlock_success': [],
            'unlock_failed': []
        }
        
        # Background monitoring
        self.monitor_thread: Optional[Thread] = None
        self.event_queue = queue.Queue()
        
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
            
    async def _unlock_screen(self, user_id: Optional[str] = None) -> bool:
        """Unlock the screen/screensaver"""
        try:
            # Method 1: Stop screensaver (async subprocess)
            if self.current_state == ScreenState.SCREENSAVER:
                # Kill screensaver process - use async subprocess
                proc = await asyncio.create_subprocess_exec(
                    "killall", "ScreenSaverEngine",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()

            # Method 2: Simulate unlock (requires password/TouchID normally)
            # This is where PAM integration would help

            # For screensaver without password requirement
            script = """
            tell application "System Events"
                key code 49 -- space key
            end tell
            """

            # Use async subprocess instead of blocking subprocess.run
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            
            # Verify unlock
            await asyncio.sleep(0.5)
            new_state = self._get_screen_state()
            
            return new_state == ScreenState.ACTIVE
            
        except Exception as e:
            logger.error(f"Screen unlock error: {e}")
            return False
            
    def _show_notification(self, title: str, message: str):
        """Show system notification"""
        if not self.config.system.show_notifications:
            return
            
        try:
            script = f'''
            display notification "{message}" with title "{title}"
            '''
            
            if self.config.system.notification_sound:
                script += ' sound name "Glass"'
                
            subprocess.run(["osascript", "-e", script], capture_output=True)
            
        except Exception as e:
            logger.error(f"Notification error: {e}")
            
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
        """Get current integration status"""
        return {
            'monitoring': self.monitoring,
            'current_state': self.current_state.value,
            'unlock_in_progress': self.unlock_in_progress,
            'integration_mode': self.config.system.integration_mode,
            'screensaver_configured': self._check_screensaver_config()
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
            
        except:
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