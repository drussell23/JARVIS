#!/usr/bin/env python3
"""
Proactive Vision System Integration
Brings together all components for seamless proactive monitoring
"""

import logging
import asyncio
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime

from .proactive_vision_intelligence import ProactiveVisionIntelligence, ScreenChange, Priority
from .intelligent_notification_filter import NotificationFilter, NotificationContext
from .proactive_communication_module import ProactiveCommunicator

logger = logging.getLogger(__name__)


class ProactiveVisionSystem:
    """
    Integrated proactive vision system that combines:
    - Pure Claude Vision intelligence for change detection
    - Intelligent filtering to prevent notification fatigue
    - Natural communication with progressive disclosure
    """
    
    def __init__(self, vision_analyzer, voice_api=None):
        """
        Initialize the integrated proactive vision system
        
        Args:
            vision_analyzer: Claude Vision analyzer instance
            voice_api: Optional voice API for speaking notifications
        """
        self.vision_analyzer = vision_analyzer
        self.voice_api = voice_api
        
        # Initialize components
        self.vision_intelligence = ProactiveVisionIntelligence(
            vision_analyzer=vision_analyzer,
            notification_callback=self._handle_notification
        )
        
        self.notification_filter = NotificationFilter()
        
        self.communicator = ProactiveCommunicator(
            voice_callback=self._speak_message,
            text_callback=self._display_message
        )
        
        # System state
        self.system_state = {
            'monitoring_active': False,
            'start_time': None,
            'notifications_sent': 0,
            'notifications_filtered': 0,
            'user_context': self._initialize_user_context(),
            'last_activity_update': datetime.now()
        }
        
        # Configuration
        self.config = {
            'enable_voice': True,
            'enable_learning': True,
            'debug_mode': False,
            'test_mode': False
        }
        
    def _initialize_user_context(self) -> Dict[str, Any]:
        """Initialize user context tracking"""
        return {
            'activity': 'unknown',
            'focus_level': 0.5,
            'workflow_phase': 'normal',
            'active_applications': [],
            'recent_actions': []
        }
        
    async def start_proactive_monitoring(self, initial_context: Optional[Dict[str, Any]] = None):
        """
        Start the proactive vision monitoring system
        
        Args:
            initial_context: Optional initial context about user's activity
        """
        if self.system_state['monitoring_active']:
            logger.warning("Proactive monitoring already active")
            return
            
        logger.info("Starting integrated proactive vision system")
        
        # Update initial context if provided
        if initial_context:
            self.system_state['user_context'].update(initial_context)
            
        # Start monitoring
        self.system_state['monitoring_active'] = True
        self.system_state['start_time'] = datetime.now()
        
        # Start the vision intelligence monitoring
        await self.vision_intelligence.start_monitoring()
        
        # Start context tracking
        asyncio.create_task(self._context_tracking_loop())
        
        # Send welcome message
        await self._send_welcome_message()
        
    async def stop_proactive_monitoring(self):
        """Stop the proactive vision monitoring system"""
        if not self.system_state['monitoring_active']:
            return
            
        logger.info("Stopping integrated proactive vision system")
        
        # Stop monitoring
        await self.vision_intelligence.stop_monitoring()
        
        self.system_state['monitoring_active'] = False
        
        # Send summary
        await self._send_monitoring_summary()
        
    async def _handle_notification(self, notification_data: Dict[str, Any]):
        """
        Handle notification from vision intelligence system
        This is where filtering and communication happen
        """
        try:
            # Parse notification data
            change = self._parse_notification_data(notification_data)
            
            # Build notification context
            context = NotificationContext(
                user_activity=self.system_state['user_context']['activity'],
                focus_level=self.system_state['user_context']['focus_level'],
                recent_notifications=self._get_recent_notifications(),
                time_of_day=self._get_time_of_day(),
                workflow_phase=self.system_state['user_context']['workflow_phase'],
                interaction_history=[]
            )
            
            # Apply intelligent filtering
            should_notify, reason = self.notification_filter.should_notify(change, context)
            
            if not should_notify:
                self.system_state['notifications_filtered'] += 1
                logger.debug(f"Notification filtered: {reason}")
                
                if self.config['debug_mode']:
                    await self._display_message(f"[Filtered] {change.description}", "debug")
                return
                
            # Send through communication module
            message = await self.communicator.send_proactive_message(change, self.system_state['user_context'])
            
            # Record successful notification
            self.notification_filter.record_notification(change, context)
            self.system_state['notifications_sent'] += 1
            
            logger.info(f"Proactive notification sent: {message}")
            
        except Exception as e:
            logger.error(f"Error handling notification: {e}")
            
    def _parse_notification_data(self, data: Dict[str, Any]) -> ScreenChange:
        """Parse notification data into ScreenChange object"""
        # This would normally be more robust
        # For now, create from the notification data
        from datetime import datetime
        
        return ScreenChange(
            description=data.get('message', ''),
            importance=Priority(data.get('priority', 'medium')),
            confidence=data.get('confidence', 0.8),
            category=data.get('category', 'other'),
            suggested_message=data.get('message', ''),
            location=data.get('location', ''),
            timestamp=datetime.fromisoformat(data.get('timestamp', datetime.now().isoformat())),
            screenshot_hash=''
        )
        
    def _get_recent_notifications(self) -> List[Dict]:
        """Get recent notifications for context"""
        # Would retrieve from notification history
        return []
        
    def _get_time_of_day(self) -> str:
        """Get descriptive time of day"""
        hour = datetime.now().hour
        if hour < 6:
            return "night"
        elif hour < 12:
            return "morning"
        elif hour < 17:
            return "afternoon"
        elif hour < 21:
            return "evening"
        else:
            return "night"
            
    async def _speak_message(self, message: str, **kwargs):
        """Speak message through voice API"""
        if self.config['enable_voice'] and self.voice_api:
            try:
                await self.voice_api.speak({
                    'text': message,
                    'voice': 'jarvis',
                    **kwargs
                })
            except Exception as e:
                logger.error(f"Error speaking message: {e}")
                
    async def _display_message(self, message: str, priority: str = "normal"):
        """Display message (for now, just log)"""
        if priority == "debug" and not self.config['debug_mode']:
            return
            
        # In a real implementation, this would show a visual notification
        logger.info(f"[{priority.upper()}] {message}")
        
    async def _context_tracking_loop(self):
        """Continuously track and update user context"""
        while self.system_state['monitoring_active']:
            try:
                # Update context based on screen analysis
                await self._update_user_context()
                
                # Adaptive interval
                await asyncio.sleep(10)  # Check context every 10 seconds
                
            except Exception as e:
                logger.error(f"Error in context tracking: {e}")
                await asyncio.sleep(10)
                
    async def _update_user_context(self):
        """Update user context based on current screen"""
        # This would analyze the current screen to understand:
        # - What applications are active
        # - What the user is doing
        # - Their focus level
        # For now, placeholder
        pass
        
    async def _send_welcome_message(self):
        """Send initial welcome message"""
        message = ("I've started proactively monitoring your screen. "
                  "I'll intelligently notify you of important updates, errors, and changes "
                  "while respecting your focus. Just work naturally - I'll speak up when needed.")
                  
        await self._speak_message(message)
        await self._display_message(message, "info")
        
    async def _send_monitoring_summary(self):
        """Send summary when monitoring ends"""
        duration = (datetime.now() - self.system_state['start_time']).total_seconds() / 60
        
        summary = (f"Monitoring session complete. "
                  f"Duration: {duration:.0f} minutes. "
                  f"Notifications sent: {self.system_state['notifications_sent']}. "
                  f"Filtered: {self.system_state['notifications_filtered']}.")
                  
        await self._speak_message(summary)
        await self._display_message(summary, "info")
        
    async def handle_user_response(self, response: str):
        """
        Handle user response to continue conversation
        
        Args:
            response: User's response to a notification
        """
        return await self.communicator.handle_user_response(response)
        
    def update_config(self, config: Dict[str, Any]):
        """Update system configuration"""
        self.config.update(config)
        
        # Propagate relevant settings
        if 'importance_threshold' in config:
            self.notification_filter.update_config({
                'base_importance_threshold': config['importance_threshold']
            })
            
        if 'notification_style' in config:
            self.communicator.update_preferences({
                'style': config['notification_style']
            })
            
        logger.info(f"System configuration updated: {config}")
        
    def get_system_stats(self) -> Dict[str, Any]:
        """Get comprehensive system statistics"""
        monitoring_stats = self.vision_intelligence.get_monitoring_stats()
        filter_stats = self.notification_filter.get_filter_stats()
        communication_stats = self.communicator.get_conversation_stats()
        
        return {
            'system': {
                'active': self.system_state['monitoring_active'],
                'uptime': (datetime.now() - self.system_state['start_time']).total_seconds() 
                          if self.system_state['start_time'] else 0,
                'notifications_sent': self.system_state['notifications_sent'],
                'notifications_filtered': self.system_state['notifications_filtered'],
                'filter_rate': self.system_state['notifications_filtered'] / 
                               (self.system_state['notifications_sent'] + self.system_state['notifications_filtered'])
                               if (self.system_state['notifications_sent'] + self.system_state['notifications_filtered']) > 0 else 0
            },
            'monitoring': monitoring_stats,
            'filtering': filter_stats,
            'communication': communication_stats,
            'context': self.system_state['user_context']
        }
        
    async def test_cursor_update_scenario(self):
        """Test scenario: Cursor update notification"""
        logger.info("Running Cursor update test scenario")
        
        # Simulate a Cursor update notification
        test_notification = {
            'message': "I notice Cursor has a new update available.",
            'priority': 'medium',
            'category': 'update',
            'confidence': 0.95,
            'location': 'bottom status bar',
            'timestamp': datetime.now().isoformat()
        }
        
        # Set test context
        self.system_state['user_context'].update({
            'activity': 'coding',
            'focus_level': 0.6,
            'workflow_phase': 'normal'
        })
        
        # Process the notification
        await self._handle_notification(test_notification)
        
        # Simulate user asking for more info
        await asyncio.sleep(2)
        response = await self.handle_user_response("What's in the update?")
        logger.info(f"System response to user query: {response}")
        
        return True


# Factory function for easy initialization
async def create_proactive_vision_system(vision_analyzer, voice_api=None) -> ProactiveVisionSystem:
    """
    Create and configure a proactive vision system
    
    Args:
        vision_analyzer: Claude Vision analyzer instance
        voice_api: Optional voice API for notifications
        
    Returns:
        Configured ProactiveVisionSystem instance
    """
    system = ProactiveVisionSystem(vision_analyzer, voice_api)
    
    # Load any saved preferences or learning data
    # This would load from persistent storage in production
    
    logger.info("Proactive Vision System created and ready")
    return system