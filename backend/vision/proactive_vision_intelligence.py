#!/usr/bin/env python3
"""
Proactive Vision Intelligence System for JARVIS
Pure Claude Vision-based continuous monitoring with zero hardcoded rules
"""

import asyncio
import logging
import time
import json
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass
from enum import Enum
import hashlib
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


class Priority(Enum):
    """Notification priority levels"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ChangeCategory(Enum):
    """Categories of screen changes"""
    UPDATE = "update"
    ERROR = "error"
    NOTIFICATION = "notification"
    STATUS = "status"
    DIALOG = "dialog"
    COMPLETION = "completion"
    WARNING = "warning"
    OTHER = "other"


@dataclass
class ScreenChange:
    """Represents a detected screen change"""
    description: str
    importance: Priority
    confidence: float
    category: ChangeCategory
    suggested_message: str
    location: str
    timestamp: datetime
    screenshot_hash: str
    
    def should_notify(self, threshold: float = 0.6) -> bool:
        """Determine if this change warrants notification"""
        importance_scores = {Priority.HIGH: 0.9, Priority.MEDIUM: 0.7, Priority.LOW: 0.5}
        score = importance_scores[self.importance] * self.confidence
        return score >= threshold


class ProactiveVisionIntelligence:
    """
    Proactive vision monitoring system using pure Claude Vision intelligence
    No hardcoded detection rules - all understanding comes from Claude
    """
    
    def __init__(self, vision_analyzer, notification_callback=None):
        """
        Initialize proactive vision intelligence
        
        Args:
            vision_analyzer: Claude Vision analyzer instance
            notification_callback: Callback to send notifications to user
        """
        self.vision_analyzer = vision_analyzer
        self.notification_callback = notification_callback
        
        # Configuration
        self.config = {
            'analysis_interval': 3.0,  # seconds between analyses
            'importance_threshold': 0.6,  # minimum score to notify
            'confidence_threshold': 0.7,  # minimum confidence to notify
            'max_notifications_per_minute': 3,
            'cooldown_seconds': 30,  # between similar notifications
            'comparison_mode': 'differential',  # or 'full'
            'enable_context_awareness': True,
            'enable_learning': True,
            'voice_enabled': True,
            'verbose_mode': False
        }
        
        # State tracking
        self.monitoring_state = {
            'active': False,
            'start_time': None,
            'last_screenshot': None,
            'last_screenshot_hash': None,
            'last_analysis_time': None,
            'current_context': None,
            'recent_changes': deque(maxlen=50),
            'notification_history': deque(maxlen=100),
            'notification_timestamps': deque(maxlen=self.config['max_notifications_per_minute']),
            'similar_notifications': {},  # hash -> last_notified_time
            'user_activity': None,
            'workflow_context': None
        }
        
        # Learning state
        self.learning_state = {
            'user_responses': {},  # notification -> response mapping
            'importance_feedback': {},  # adjust importance based on user interaction
            'timing_patterns': {},  # best times to notify
            'ignored_patterns': set(),  # patterns user consistently ignores
            'valued_patterns': set(),  # patterns user engages with
        }
        
        # Monitoring task
        self._monitoring_task = None
        
    async def start_monitoring(self):
        """Start proactive monitoring"""
        if self.monitoring_state['active']:
            logger.warning("Monitoring already active")
            return
            
        logger.info("Starting proactive vision monitoring")
        self.monitoring_state['active'] = True
        self.monitoring_state['start_time'] = datetime.now()
        
        # Send initial greeting
        await self._send_proactive_greeting()
        
        # Start monitoring loop
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        
    async def stop_monitoring(self):
        """Stop proactive monitoring"""
        if not self.monitoring_state['active']:
            return
            
        logger.info("Stopping proactive vision monitoring")
        self.monitoring_state['active'] = False
        
        # Cancel monitoring task
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
                
        # Send farewell with summary
        await self._send_monitoring_summary()
        
    async def _monitoring_loop(self):
        """Main monitoring loop - continuously analyze screen for changes"""
        while self.monitoring_state['active']:
            try:
                # Capture current screen
                screenshot = await self._capture_screen()
                if not screenshot:
                    await asyncio.sleep(self.config['analysis_interval'])
                    continue
                    
                # Analyze for changes
                changes = await self._analyze_screen_changes(screenshot)
                
                # Process detected changes
                for change in changes:
                    await self._process_change(change)
                    
                # Update state
                self._update_monitoring_state(screenshot)
                
                # Adaptive interval based on activity
                interval = self._calculate_adaptive_interval()
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(self.config['analysis_interval'])
                
    async def _capture_screen(self) -> Optional[Image.Image]:
        """Capture current screen"""
        try:
            # Use vision analyzer's capture method
            if hasattr(self.vision_analyzer, 'capture_screenshot'):
                screenshot = await self.vision_analyzer.capture_screenshot()
            else:
                # Fallback to basic capture
                import pyautogui
                screenshot = pyautogui.screenshot()
                
            return screenshot
        except Exception as e:
            logger.error(f"Failed to capture screen: {e}")
            return None
            
    async def _analyze_screen_changes(self, current_screenshot: Image.Image) -> List[ScreenChange]:
        """
        Analyze screen for changes using Claude Vision
        This is where the magic happens - pure Claude intelligence, no hardcoding
        """
        changes = []
        
        # Get previous screenshot for comparison
        previous_screenshot = self.monitoring_state['last_screenshot']
        
        # Build the analysis prompt
        if previous_screenshot:
            # Comparative analysis
            prompt = self._build_comparative_prompt()
        else:
            # Initial analysis
            prompt = self._build_initial_analysis_prompt()
            
        try:
            # Send to Claude for analysis
            if previous_screenshot:
                # Send both screenshots for comparison
                result = await self._analyze_with_comparison(previous_screenshot, current_screenshot, prompt)
            else:
                # Single screenshot analysis
                result = await self._analyze_single_screenshot(current_screenshot, prompt)
                
            # Parse Claude's response into ScreenChange objects
            if result and result.get('changes_detected'):
                for observation in result.get('observations', []):
                    change = self._parse_observation(observation, current_screenshot)
                    if change:
                        changes.append(change)
                        
        except Exception as e:
            logger.error(f"Error analyzing screen changes: {e}")
            
        return changes
        
    def _build_comparative_prompt(self) -> str:
        """Build prompt for comparing two screenshots"""
        return '''You are JARVIS, an intelligent AI assistant monitoring the user's screen.
Compare these two screenshots (previous and current).

Look for important changes including:
1. New notifications, badges, or alerts
2. Update notifications (like "New update available")
3. Error messages or warnings
4. Status changes in applications
5. Dialog boxes or popups
6. Important text that appeared
7. Completion of processes
8. Time-sensitive information

Analyze what you see and determine if the user should be notified.

Respond in this JSON format:
{
    "changes_detected": true/false,
    "observations": [
        {
            "description": "Specific description of what changed",
            "importance": "high/medium/low",
            "confidence": 0.0-1.0,
            "category": "update/error/notification/status/dialog/completion/warning/other",
            "suggested_message": "What JARVIS should say to the user",
            "location": "Where on screen this appeared"
        }
    ],
    "should_notify": true/false,
    "context": "What the user appears to be doing"
}

Be specific about what you observe. Don't use generic descriptions.
Only flag things genuinely worth interrupting the user for.
Focus on actionable or important information.'''

    def _build_initial_analysis_prompt(self) -> str:
        """Build prompt for initial screen analysis"""
        return '''You are JARVIS, an intelligent AI assistant starting to monitor the user's screen.
Analyze this screenshot to understand the current state.

Identify:
1. What applications are open
2. What the user appears to be working on
3. Any existing notifications or alerts
4. The general context of their work
5. Any opportunities where you might help

Respond in this JSON format:
{
    "changes_detected": false,
    "observations": [],
    "should_notify": false,
    "context": "Detailed description of what the user is doing",
    "initial_state": {
        "open_applications": [],
        "primary_activity": "",
        "potential_workflows": [],
        "existing_notifications": []
    }
}'''
        
    async def _analyze_with_comparison(self, previous: Image.Image, current: Image.Image, prompt: str) -> Dict:
        """Send two screenshots to Claude for comparison"""
        try:
            # This method would need to be implemented to send both images
            # For now, we'll use a simplified approach
            result = await self.vision_analyzer.analyze_screenshot(current, prompt)
            
            # Try to parse JSON from response
            if isinstance(result, dict) and 'analysis' in result:
                response_text = result['analysis']
                # Extract JSON from response
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                    
            return {'changes_detected': False, 'observations': []}
            
        except Exception as e:
            logger.error(f"Error in comparative analysis: {e}")
            return {'changes_detected': False, 'observations': []}
            
    async def _analyze_single_screenshot(self, screenshot: Image.Image, prompt: str) -> Dict:
        """Analyze a single screenshot"""
        try:
            result = await self.vision_analyzer.analyze_screenshot(screenshot, prompt)
            
            # Parse response
            if isinstance(result, dict) and 'analysis' in result:
                response_text = result['analysis']
                # Extract JSON from response
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                    
            return {'changes_detected': False, 'observations': []}
            
        except Exception as e:
            logger.error(f"Error analyzing screenshot: {e}")
            return {'changes_detected': False, 'observations': []}
            
    def _parse_observation(self, observation: Dict, screenshot: Image.Image) -> Optional[ScreenChange]:
        """Parse observation into ScreenChange object"""
        try:
            # Create hash for deduplication
            screenshot_hash = self._hash_image_region(screenshot, observation.get('location', ''))
            
            change = ScreenChange(
                description=observation.get('description', ''),
                importance=Priority[observation.get('importance', 'low').upper()],
                confidence=float(observation.get('confidence', 0.0)),
                category=ChangeCategory[observation.get('category', 'other').upper()],
                suggested_message=observation.get('suggested_message', ''),
                location=observation.get('location', ''),
                timestamp=datetime.now(),
                screenshot_hash=screenshot_hash
            )
            
            return change
            
        except Exception as e:
            logger.error(f"Error parsing observation: {e}")
            return None
            
    def _hash_image_region(self, image: Image.Image, location: str) -> str:
        """Create hash of image region for deduplication"""
        # For now, hash the entire image
        # Could be enhanced to hash specific regions based on location
        if isinstance(image, Image.Image):
            return hashlib.md5(image.tobytes()).hexdigest()
        return hashlib.md5(str(image).encode()).hexdigest()
        
    async def _process_change(self, change: ScreenChange):
        """Process a detected change and decide whether to notify"""
        # Check if we should notify
        if not change.should_notify(self.config['importance_threshold']):
            logger.debug(f"Change below threshold: {change.description}")
            return
            
        # Check confidence
        if change.confidence < self.config['confidence_threshold']:
            logger.debug(f"Change confidence too low: {change.description}")
            return
            
        # Check for spam
        if self._is_spam(change):
            logger.debug(f"Change filtered as spam: {change.description}")
            return
            
        # Check rate limiting
        if not self._check_rate_limit():
            logger.debug("Rate limit exceeded, skipping notification")
            return
            
        # Send notification
        await self._send_notification(change)
        
        # Record for learning
        self._record_notification(change)
        
    def _is_spam(self, change: ScreenChange) -> bool:
        """Check if notification would be spam"""
        # Check if similar notification was sent recently
        if change.screenshot_hash in self.monitoring_state['similar_notifications']:
            last_notified = self.monitoring_state['similar_notifications'][change.screenshot_hash]
            if datetime.now() - last_notified < timedelta(seconds=self.config['cooldown_seconds']):
                return True
                
        # Check if this pattern is ignored
        if change.description in self.learning_state['ignored_patterns']:
            return True
            
        return False
        
    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits"""
        now = datetime.now()
        # Remove old timestamps
        while self.monitoring_state['notification_timestamps']:
            if now - self.monitoring_state['notification_timestamps'][0] > timedelta(minutes=1):
                self.monitoring_state['notification_timestamps'].popleft()
            else:
                break
                
        # Check limit
        return len(self.monitoring_state['notification_timestamps']) < self.config['max_notifications_per_minute']
        
    async def _send_notification(self, change: ScreenChange):
        """Send notification to user"""
        message = change.suggested_message
        
        # Enhance message based on context
        if self.config['enable_context_awareness'] and self.monitoring_state['current_context']:
            message = self._enhance_message_with_context(message, change)
            
        # Send via callback
        if self.notification_callback:
            notification_data = {
                'message': message,
                'priority': change.importance.value,
                'category': change.category.value,
                'confidence': change.confidence,
                'timestamp': change.timestamp.isoformat()
            }
            await self.notification_callback(notification_data)
            
        # Update state
        self.monitoring_state['notification_timestamps'].append(datetime.now())
        self.monitoring_state['similar_notifications'][change.screenshot_hash] = datetime.now()
        
        logger.info(f"Sent notification: {message}")
        
    def _enhance_message_with_context(self, message: str, change: ScreenChange) -> str:
        """Enhance message based on current context"""
        context = self.monitoring_state['current_context']
        
        # Add contextual prefixes based on workflow
        if 'coding' in context.lower():
            if change.category == ChangeCategory.ERROR:
                message = f"While you're coding, {message}"
            elif change.category == ChangeCategory.UPDATE:
                message = f"{message} I can help you update after you finish this function."
                
        return message
        
    def _record_notification(self, change: ScreenChange):
        """Record notification for learning"""
        self.monitoring_state['recent_changes'].append(change)
        self.monitoring_state['notification_history'].append({
            'change': change,
            'timestamp': datetime.now(),
            'user_response': None  # To be filled if user responds
        })
        
    def _update_monitoring_state(self, screenshot: Image.Image):
        """Update monitoring state after analysis"""
        self.monitoring_state['last_screenshot'] = screenshot
        self.monitoring_state['last_screenshot_hash'] = self._hash_image_region(screenshot, '')
        self.monitoring_state['last_analysis_time'] = datetime.now()
        
    def _calculate_adaptive_interval(self) -> float:
        """Calculate adaptive monitoring interval based on activity"""
        base_interval = self.config['analysis_interval']
        
        # If many recent changes, monitor more frequently
        recent_change_count = len([c for c in self.monitoring_state['recent_changes'] 
                                  if datetime.now() - c.timestamp < timedelta(minutes=5)])
        
        if recent_change_count > 5:
            return base_interval * 0.5  # More frequent
        elif recent_change_count == 0:
            return base_interval * 2.0  # Less frequent
        else:
            return base_interval
            
    async def _send_proactive_greeting(self):
        """Send initial greeting when monitoring starts"""
        greeting = {
            'message': "I've started monitoring your screen intelligently. I'll let you know if I notice anything important like updates, errors, or notifications. Just continue working as usual.",
            'priority': 'info',
            'category': 'system'
        }
        
        if self.notification_callback:
            await self.notification_callback(greeting)
            
    async def _send_monitoring_summary(self):
        """Send summary when monitoring stops"""
        duration = datetime.now() - self.monitoring_state['start_time']
        notification_count = len(self.monitoring_state['notification_history'])
        
        summary = {
            'message': f"Monitoring session ended. I tracked your screen for {duration.total_seconds()//60:.0f} minutes and sent {notification_count} notifications.",
            'priority': 'info',
            'category': 'system'
        }
        
        if self.notification_callback:
            await self.notification_callback(summary)
            
    def record_user_feedback(self, notification_id: str, feedback: str):
        """Record user feedback on notifications for learning"""
        # Update learning state based on feedback
        if feedback == 'helpful':
            # Increase importance of similar notifications
            pass
        elif feedback == 'ignore':
            # Decrease importance or add to ignored patterns
            pass
            
    def get_monitoring_stats(self) -> Dict[str, Any]:
        """Get current monitoring statistics"""
        return {
            'active': self.monitoring_state['active'],
            'duration': (datetime.now() - self.monitoring_state['start_time']).total_seconds() if self.monitoring_state['start_time'] else 0,
            'notifications_sent': len(self.monitoring_state['notification_history']),
            'recent_changes': len(self.monitoring_state['recent_changes']),
            'current_context': self.monitoring_state['current_context']
        }

    # -------------------------------------------------------------------------
    # Lifecycle API — thin wrappers used by UnifiedCommandProcessor
    # -------------------------------------------------------------------------

    def is_running(self) -> bool:
        """Return True while the ambient monitor is active."""
        return bool(self.monitoring_state.get('active', False))

    async def start(
        self,
        on_narration: Optional[Any] = None,
        interval_s: Optional[float] = None,
    ) -> None:
        """Start proactive ambient monitoring.

        Args:
            on_narration: Optional async callback ``(notification_dict) -> None``
                that receives each proactive narration.  When provided, it
                overrides the callback set at construction time.
            interval_s: Override the default analysis interval (seconds).
                Honoured only when the monitor is not already running.
        """
        if on_narration is not None:
            self.notification_callback = on_narration
        if interval_s is not None:
            self.config['analysis_interval'] = float(interval_s)
        await self.start_monitoring()

    async def stop(self) -> None:
        """Stop the ambient monitor and send a summary."""
        await self.stop_monitoring()