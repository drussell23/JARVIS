#!/usr/bin/env python3
"""
Voice Integration Handler for JARVIS Proactive Monitoring
Handles FR-7: Voice announcements, sound cues, and audio feedback
Fully dynamic with no hardcoding
"""

import asyncio
import logging
import os
from typing import Optional, Dict, Any, Callable
from datetime import datetime
import json

logger = logging.getLogger(__name__)

class VoiceIntegrationHandler:
    """Handle voice announcements and sound cues for proactive monitoring"""
    
    def __init__(self, jarvis_api=None):
        """
        Initialize voice integration handler
        
        Args:
            jarvis_api: JARVIS voice API instance for TTS
        """
        self.jarvis_api = jarvis_api
        self.voice_queue = asyncio.Queue()
        self._voice_task = None
        self._is_active = False
        
        # Dynamic configuration
        self.config = {
            'voice_enabled': os.getenv('JARVIS_VOICE_ENABLED', 'true').lower() == 'true',
            'sound_cues_enabled': os.getenv('JARVIS_SOUND_CUES', 'true').lower() == 'true',
            'voice_priority_threshold': os.getenv('JARVIS_VOICE_PRIORITY', 'normal'),
            'max_voice_queue': int(os.getenv('JARVIS_MAX_VOICE_QUEUE', '5')),
            'voice_speed': float(os.getenv('JARVIS_VOICE_SPEED', '1.1')),
            'voice_volume': float(os.getenv('JARVIS_VOICE_VOLUME', '0.8'))
        }
        
        # Sound cue mappings (can be customized via environment)
        self.sound_cues = self._load_sound_cues()
        
    def _load_sound_cues(self) -> Dict[str, str]:
        """Load sound cue mappings from environment or defaults"""
        default_cues = {
            'error_chime': 'sounds/error_notification.mp3',
            'info_ding': 'sounds/info_ding.mp3',
            'suggestion_pop': 'sounds/suggestion.mp3',
            'assist_beep': 'sounds/assist_ready.mp3',
            'urgent_alert': 'sounds/urgent_alert.mp3',
            'default_notification': 'sounds/notification.mp3',
            'subtle_ping': 'sounds/subtle_ping.mp3'
        }
        
        # Allow environment override
        cues_json = os.getenv('JARVIS_SOUND_CUES_MAP')
        if cues_json:
            try:
                return json.loads(cues_json)
            except Exception:
                pass

        return default_cues
        
    async def start(self):
        """Start voice processing"""
        if self._is_active:
            return
            
        self._is_active = True
        self._voice_task = asyncio.create_task(self._voice_processor())
        logger.info("Voice integration handler started")
        
    async def stop(self):
        """Stop voice processing"""
        self._is_active = False
        
        if self._voice_task:
            self._voice_task.cancel()
            try:
                await self._voice_task
            except asyncio.CancelledError:
                pass
                
        logger.info("Voice integration handler stopped")
        
    async def handle_notification(self, notification: Dict[str, Any]):
        """
        Handle incoming notification for voice announcement
        
        Args:
            notification: Notification dict with message, priority, etc.
        """
        if not self.config['voice_enabled']:
            return
            
        # Check priority threshold
        priority = notification.get('priority', 'normal')
        if not self._should_announce(priority):
            return
            
        # Queue for voice announcement
        if self.voice_queue.qsize() < self.config['max_voice_queue']:
            await self.voice_queue.put(notification)
        else:
            logger.warning("Voice queue full, dropping notification")
            
    def _should_announce(self, priority: str) -> bool:
        """Check if priority meets threshold for voice announcement"""
        priority_levels = {
            'critical': 4,
            'high': 3,
            'normal': 2,
            'low': 1,
            'info': 0
        }
        
        threshold = priority_levels.get(self.config['voice_priority_threshold'], 2)
        current = priority_levels.get(priority, 2)
        
        return current >= threshold
        
    async def _voice_processor(self):
        """Process voice announcement queue"""
        while self._is_active:
            try:
                # Get next notification
                notification = await asyncio.wait_for(
                    self.voice_queue.get(),
                    timeout=1.0
                )
                
                # Play sound cue if enabled
                if self.config['sound_cues_enabled']:
                    await self._play_sound_cue(notification)
                    
                # Speak the message
                await self._speak_message(notification)
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error in voice processor: {e}")
                
    async def _play_sound_cue(self, notification: Dict[str, Any]):
        """Play appropriate sound cue for notification"""
        sound_cue = notification.get('sound_cue', 'default_notification')
        sound_file = self.sound_cues.get(sound_cue)
        
        if not sound_file or not os.path.exists(sound_file):
            return
            
        try:
            # Use system command to play sound (macOS)
            if os.name == 'posix':  # macOS/Linux
                os.system(f"afplay '{sound_file}' &")
            # Add Windows support if needed
        except Exception as e:
            logger.error(f"Error playing sound cue: {e}")
            
    async def _speak_message(self, notification: Dict[str, Any]):
        """Speak notification message using JARVIS voice"""
        if not self.jarvis_api:
            return
            
        message = notification.get('message', '')
        if not message:
            return
            
        try:
            # Prepare voice parameters
            voice_params = {
                'text': message,
                'speed': self.config['voice_speed'],
                'volume': self.config['voice_volume']
            }
            
            # Add emotion/tone based on notification type
            if notification.get('priority') == 'high':
                voice_params['emotion'] = 'urgent'
            elif 'error' in notification.get('data', {}).get('opportunity', {}).get('type', ''):
                voice_params['emotion'] = 'concerned'
            elif 'suggestion' in message.lower():
                voice_params['emotion'] = 'helpful'
                
            # Call JARVIS voice API
            await self.jarvis_api.speak_async(voice_params)
            
        except Exception as e:
            logger.error(f"Error speaking message: {e}")
            
    async def announce_monitoring_state(self, state: str, context: Optional[Dict[str, Any]] = None):
        """
        Announce monitoring state changes
        
        Args:
            state: 'started', 'stopped', 'paused'
            context: Additional context for the announcement
        """
        messages = {
            'started': "Screen monitoring activated. I'll watch and help as you work.",
            'stopped': "Screen monitoring deactivated. Call me when you need assistance.",
            'paused': "Monitoring paused for privacy. I'll resume when you're ready."
        }
        
        message = messages.get(state, f"Monitoring state: {state}")
        
        # Customize based on context
        if context and state == 'started':
            if context.get('current_app'):
                message = f"I see you're in {context['current_app']}. I'll monitor and assist as you work."
                
        notification = {
            'message': message,
            'priority': 'info',
            'sound_cue': 'default_notification',
            'type': 'state_change',
            'state': state
        }
        
        await self.handle_notification(notification)
        
    async def provide_voice_feedback(self, feedback_type: str, data: Optional[Dict[str, Any]] = None):
        """
        Provide specific voice feedback for user actions
        
        Args:
            feedback_type: Type of feedback to provide
            data: Additional data for context
        """
        feedback_messages = {
            'assistance_accepted': "Great! Let me help you with that.",
            'assistance_declined': "No problem. I'll keep watching.",
            'task_completed': "Excellent work! That's now complete.",
            'error_resolved': "Good job fixing that error.",
            'workflow_improved': "That's a much more efficient approach!"
        }
        
        message = feedback_messages.get(feedback_type)
        if not message and data:
            # Generate dynamic feedback
            message = f"Acknowledged. {data.get('context', '')}"
            
        if message:
            notification = {
                'message': message,
                'priority': 'low',
                'sound_cue': 'subtle_ping',
                'type': 'feedback',
                'feedback_type': feedback_type
            }
            
            await self.handle_notification(notification)