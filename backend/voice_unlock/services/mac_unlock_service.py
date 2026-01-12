"""
Mac Unlock Service
==================

High-level service for voice-based Mac unlocking.
"""

import logging
import asyncio
from typing import Optional, Callable, Dict, Any
from datetime import datetime

from ..core.authentication import VoiceAuthenticator, AuthenticationResult
from .screensaver_integration import ScreensaverIntegration, ScreenState
from .keychain_service import KeychainService
from ..config import get_config

logger = logging.getLogger(__name__)


class MacUnlockService:
    """Main service for voice-based Mac unlocking"""
    
    def __init__(self, authenticator: Optional[VoiceAuthenticator] = None):
        self.config = get_config()
        self.authenticator = authenticator or VoiceAuthenticator()
        self.screensaver = ScreensaverIntegration(self.authenticator)
        self.keychain = KeychainService()
        
        # Service state
        self.enabled = False
        self.monitoring_task = None
        
        # Statistics
        self.stats = {
            'unlock_attempts': 0,
            'successful_unlocks': 0,
            'failed_unlocks': 0,
            'last_unlock_attempt': None,
            'service_start_time': None
        }
        
        # Event callbacks
        self.event_callbacks = {
            'unlock_started': [],
            'unlock_success': [],
            'unlock_failed': [],
            'service_started': [],
            'service_stopped': []
        }
        
    def add_event_callback(self, event: str, callback: Callable):
        """Add callback for service events"""
        if event in self.event_callbacks:
            self.event_callbacks[event].append(callback)
            
    async def start_service(self):
        """Start the voice unlock service"""
        if self.enabled:
            logger.warning("Voice unlock service already running")
            return
            
        logger.info("Starting Mac Voice Unlock Service...")
        
        # Configure screensaver settings check
        self.screensaver.configure_screensaver_settings()
        
        # Set up event handlers
        self._setup_event_handlers()
        
        # Start screensaver monitoring
        self.screensaver.start_monitoring()
        
        # Start continuous monitoring if configured
        if self.config.performance.background_monitoring:
            self.monitoring_task = asyncio.create_task(self._continuous_monitoring())
            
        self.enabled = True
        self.stats['service_start_time'] = datetime.now()
        
        # Trigger callbacks
        self._trigger_event('service_started')
        
        # JARVIS announcement
        if self.config.system.jarvis_responses:
            await self._speak("Voice unlock service activated, Sir")
            
        logger.info("✅ Mac Voice Unlock Service started")
        
    async def stop_service(self):
        """Stop the voice unlock service"""
        if not self.enabled:
            return
            
        logger.info("Stopping Mac Voice Unlock Service...")
        
        # Stop monitoring
        self.screensaver.stop_monitoring()
        
        # Cancel background task
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
                
        self.enabled = False
        
        # Trigger callbacks
        self._trigger_event('service_stopped')
        
        # JARVIS announcement
        if self.config.system.jarvis_responses:
            await self._speak("Voice unlock service deactivated, Sir")
            
        logger.info("✅ Mac Voice Unlock Service stopped")
        
    def _setup_event_handlers(self):
        """Set up screensaver event handlers"""
        
        def on_screen_locked(data):
            """Handle screen lock event"""
            logger.info("Screen locked - voice unlock ready")
            if self.config.system.show_notifications:
                self._show_notification("JARVIS Voice Unlock", "Say your phrase to unlock")
                
        def on_unlock_success(details):
            """Handle successful unlock"""
            self.stats['successful_unlocks'] += 1
            self._trigger_event('unlock_success', details)
            
            # Log to audit
            self.keychain.audit_log('voice_unlock_success', {
                'user_id': details.get('user_id'),
                'auth_score': details.get('auth_score'),
                'timestamp': datetime.now().isoformat()
            })
            
        def on_unlock_failed(details):
            """Handle failed unlock"""
            self.stats['failed_unlocks'] += 1
            self._trigger_event('unlock_failed', details)
            
            # Log to audit
            self.keychain.audit_log('voice_unlock_failed', {
                'reason': str(details),
                'timestamp': datetime.now().isoformat()
            })
            
        # Register handlers
        self.screensaver.add_event_handler('screen_locked', on_screen_locked)
        self.screensaver.add_event_handler('unlock_success', on_unlock_success)
        self.screensaver.add_event_handler('unlock_failed', on_unlock_failed)
        
    async def _continuous_monitoring(self):
        """Background monitoring for additional features"""
        
        while self.enabled:
            try:
                # Check if we should perform continuous authentication
                if self.screensaver.current_state == ScreenState.ACTIVE:
                    # Get list of enrolled users
                    enrolled_users = self.keychain.list_voiceprints()
                    
                    if enrolled_users and self.config.system.continuous_auth_enabled:
                        # Perform passive authentication
                        # This could be used for presence detection
                        pass
                        
                # Sleep for monitoring interval
                await asyncio.sleep(self.config.performance.monitoring_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Continuous monitoring error: {e}")
                await asyncio.sleep(5)  # Error backoff
                
    async def manual_unlock(self, user_id: Optional[str] = None):
        """Manually trigger voice unlock attempt"""
        
        self.stats['unlock_attempts'] += 1
        self.stats['last_unlock_attempt'] = datetime.now()
        
        # Trigger unlock started event
        self._trigger_event('unlock_started', {'user_id': user_id})
        
        try:
            # Perform authentication
            result, details = await self.authenticator.authenticate(user_id=user_id)
            
            if result == AuthenticationResult.SUCCESS:
                # Attempt to unlock
                success = await self.screensaver._unlock_screen(details.get('user_id'))
                
                if success:
                    self.stats['successful_unlocks'] += 1
                    self._trigger_event('unlock_success', details)
                    
                    if self.config.system.jarvis_responses:
                        await self._speak(
                            self.config.system.custom_responses.get(
                                'success',
                                'Welcome back, Sir'
                            )
                        )
                        
                    return True, "Screen unlocked successfully"
                else:
                    self.stats['failed_unlocks'] += 1
                    return False, "Failed to unlock screen"
                    
            else:
                self.stats['failed_unlocks'] += 1
                self._trigger_event('unlock_failed', details)
                
                # Handle different failure types
                if result == AuthenticationResult.LOCKOUT:
                    message = self.config.system.custom_responses.get(
                        'lockout',
                        'Security lockout activated'
                    )
                elif result == AuthenticationResult.SPOOFING_DETECTED:
                    message = "Security alert: Spoofing attempt detected"
                else:
                    message = self.config.system.custom_responses.get(
                        'failure',
                        'Voice not recognized'
                    )
                    
                if self.config.system.jarvis_responses:
                    await self._speak(message)
                    
                return False, message
                
        except Exception as e:
            logger.error(f"Manual unlock error: {e}")
            self.stats['failed_unlocks'] += 1
            return False, f"Unlock error: {e}"
            
    def get_service_status(self) -> Dict[str, Any]:
        """Get current service status and statistics"""
        
        status = {
            'enabled': self.enabled,
            'monitoring': self.screensaver.monitoring,
            'screen_state': self.screensaver.current_state.value,
            'stats': self.stats.copy(),
            'config': {
                'mode': self.config.system.integration_mode,
                'anti_spoofing': self.config.security.anti_spoofing_level,
                'continuous_auth': getattr(self.config.system, 'continuous_auth_enabled', False)
            }
        }
        
        # Add uptime
        if self.stats['service_start_time']:
            uptime = (datetime.now() - self.stats['service_start_time']).total_seconds()
            status['uptime_seconds'] = uptime
            
        # Add success rate
        total_attempts = self.stats['unlock_attempts']
        if total_attempts > 0:
            status['success_rate'] = self.stats['successful_unlocks'] / total_attempts
        else:
            status['success_rate'] = 0.0
            
        # Get enrolled users
        try:
            voiceprints = self.keychain.list_voiceprints()
            status['enrolled_users_count'] = len(voiceprints)
        except Exception:
            status['enrolled_users_count'] = 0
            
        return status
        
    async def test_audio_system(self) -> Dict[str, Any]:
        """Test audio capture and quality"""
        
        try:
            # Test audio capture
            from ..utils.audio_capture import AudioCapture
            capture = AudioCapture()
            
            # Calibrate noise
            noise_level = capture.calibrate_noise_floor()
            
            # Capture test audio
            logger.info("Testing audio capture (speak now)...")
            audio, detected = capture.capture_with_vad(max_duration=3.0)
            
            if detected:
                # Validate audio
                valid, message = self.authenticator.feature_extractor.validate_audio(audio)
                
                return {
                    'success': True,
                    'audio_detected': True,
                    'duration': len(audio) / capture.config.sample_rate,
                    'noise_level': float(noise_level),
                    'quality_valid': valid,
                    'quality_message': message
                }
            else:
                return {
                    'success': False,
                    'audio_detected': False,
                    'message': 'No voice detected'
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
            
    def _trigger_event(self, event: str, data: Any = None):
        """Trigger event callbacks"""
        for callback in self.event_callbacks.get(event, []):
            try:
                callback(data)
            except Exception as e:
                logger.error(f"Event callback error for {event}: {e}")
                
    def _show_notification(self, title: str, message: str):
        """Show system notification"""
        try:
            import subprocess
            script = f'''
            display notification "{message}" with title "{title}"
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True)
        except Exception as e:
            logger.error(f"Notification error: {e}")
            
    async def _speak(self, text: str):
        """Speak using JARVIS voice"""
        try:
            # DISABLED: Audio is now handled by frontend to avoid duplicate voices
            # The WebSocket response includes speak:true flag for frontend TTS
            import subprocess
            logger.debug(f"[Skipping backend TTS] Text would have been: {text}")
            # Original code kept for reference:
            # subprocess.run(["say", "-v", "Daniel", text], capture_output=True)
        except Exception as e:
            logger.error(f"Speech error: {e}")