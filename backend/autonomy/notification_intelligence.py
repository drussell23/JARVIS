#!/usr/bin/env python3
"""
Notification Intelligence System for JARVIS
Uses vision system to detect and intelligently announce notifications
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Import existing vision and autonomy components
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vision.ocr_processor import OCRProcessor, TextRegion
from vision.window_analysis import WindowAnalyzer, ApplicationCategory
from vision.enhanced_monitoring import EnhancedWorkspaceMonitor
from autonomy.autonomous_decision_engine import AutonomousDecisionEngine, AutonomousAction, ActionPriority
from autonomy.vision_decision_pipeline import VisionDecisionPipeline
from modules.voice_module import VoiceModule

logger = logging.getLogger(__name__)


class NotificationContext(Enum):
    """Context of detected notifications"""
    MESSAGE_RECEIVED = "message_received"
    MEETING_REMINDER = "meeting_reminder"
    SYSTEM_ALERT = "system_alert"
    SOCIAL_UPDATE = "social_update"
    WORK_UPDATE = "work_update"
    URGENT_ALERT = "urgent_alert"


@dataclass
class IntelligentNotification:
    """Notification detected through vision system"""
    source_window_id: str
    app_name: str
    detected_text: List[str]
    visual_elements: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    context: NotificationContext = NotificationContext.MESSAGE_RECEIVED
    confidence: float = 0.0
    urgency_score: float = 0.0
    
    def to_natural_speech(self) -> str:
        """Convert to natural speech using AI understanding"""
        # This will be dynamically generated based on content
        return ""


class NotificationIntelligence:
    """
    Intelligent notification system that uses vision to detect and understand notifications
    without hardcoding specific app behaviors
    """
    
    def __init__(self):
        # Vision components
        self.ocr_processor = OCRProcessor()
        self.window_analyzer = WindowAnalyzer()
        self.enhanced_monitor = EnhancedWorkspaceMonitor()
        self.decision_engine = AutonomousDecisionEngine()
        
        # Voice system
        self.voice_module = VoiceModule()
        
        # Learning system
        self.notification_patterns = {}
        self.learned_behaviors = {}
        self.announcement_history = []
        
        # Active monitoring
        self.is_monitoring = False
        self.monitoring_task = None
        
        # Intelligent detection patterns (learned, not hardcoded)
        self.detection_patterns = {
            'badge_indicators': [],  # Will learn badge patterns
            'notification_regions': {},  # Will learn where notifications appear
            'urgency_indicators': [],  # Will learn what indicates urgency
            'sender_patterns': []  # Will learn how to identify senders
        }
        
    async def start_intelligent_monitoring(self):
        """Start intelligent notification monitoring"""
        if self.is_monitoring:
            return
            
        self.is_monitoring = True
        self.monitoring_task = asyncio.create_task(self._intelligent_monitoring_loop())
        logger.info("Started intelligent notification monitoring")
        
    async def stop_monitoring(self):
        """Stop monitoring"""
        self.is_monitoring = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
        logger.info("Stopped intelligent notification monitoring")
        
    async def _intelligent_monitoring_loop(self):
        """Main intelligent monitoring loop"""
        while self.is_monitoring:
            try:
                # Get current workspace state from vision system
                workspace_state = await self.enhanced_monitor.get_complete_workspace_state()
                
                # Analyze for notifications using AI
                notifications = await self._detect_notifications_intelligently(workspace_state)
                
                # Process each notification
                for notification in notifications:
                    await self._process_intelligent_notification(notification)
                    
                await asyncio.sleep(2)  # 2-second monitoring cycle
                
            except Exception as e:
                logger.error(f"Error in intelligent monitoring: {e}")
                await asyncio.sleep(5)
    
    async def _detect_notifications_intelligently(self, workspace_state: Dict[str, Any]) -> List[IntelligentNotification]:
        """
        Use vision and AI to detect notifications without hardcoding
        This learns from patterns rather than having preset rules
        """
        notifications = []
        
        # Get all windows and their visual state
        windows = workspace_state.get('windows', [])
        ui_elements = workspace_state.get('ui_elements', [])
        state_changes = workspace_state.get('state_changes', [])
        
        for window in windows:
            # Use OCR to read window content
            window_notifications = await self._analyze_window_for_notifications(
                window, 
                ui_elements,
                state_changes
            )
            notifications.extend(window_notifications)
            
        return notifications
    
    async def _analyze_window_for_notifications(self, window, ui_elements, state_changes) -> List[IntelligentNotification]:
        """
        Intelligently analyze a window for notifications
        Uses pattern recognition and AI understanding
        """
        notifications = []
        
        # Look for visual indicators of notifications
        notification_indicators = await self._find_notification_indicators(window, ui_elements)
        
        # If indicators found, analyze the content
        if notification_indicators:
            # Extract relevant text regions
            text_regions = await self._extract_notification_text(window, notification_indicators)
            
            # Use AI to understand the notification
            for indicator in notification_indicators:
                notification = await self._understand_notification(
                    window,
                    indicator,
                    text_regions
                )
                if notification:
                    notifications.append(notification)
                    
        # Also check for state changes that might indicate notifications
        for change in state_changes:
            if change.get('window_id') == window.window_id:
                if await self._is_notification_change(change):
                    notification = await self._create_notification_from_change(
                        window, change
                    )
                    if notification:
                        notifications.append(notification)
                        
        return notifications
    
    async def _find_notification_indicators(self, window, ui_elements) -> List[Dict[str, Any]]:
        """
        Find visual indicators of notifications using pattern recognition
        This learns what notifications look like rather than hardcoding
        """
        indicators = []
        
        # Look for badge-like elements (small colored regions with numbers/dots)
        for element in ui_elements:
            if self._looks_like_badge(element):
                indicators.append({
                    'type': 'badge',
                    'element': element,
                    'confidence': self._calculate_badge_confidence(element)
                })
                
        # Look for notification banners/toasts
        for element in ui_elements:
            if self._looks_like_notification_banner(element):
                indicators.append({
                    'type': 'banner',
                    'element': element,
                    'confidence': self._calculate_banner_confidence(element)
                })
                
        # Learn from patterns
        self._update_detection_patterns(indicators)
        
        return indicators
    
    def _looks_like_badge(self, element: Dict[str, Any]) -> bool:
        """
        Intelligently determine if element looks like a notification badge
        Uses visual characteristics rather than hardcoding
        """
        # Check size - badges are typically small
        if element.get('width', 0) > 50 or element.get('height', 0) > 50:
            return False
            
        # Check if contains number or dot
        text = element.get('text', '').strip()
        if text.isdigit() or text in ['•', '●', '○']:
            return True
            
        # Check color - badges often have bright colors
        color = element.get('color')
        if color and self._is_notification_color(color):
            return True
            
        # Check learned patterns
        return self._matches_learned_badge_pattern(element)
    
    def _looks_like_notification_banner(self, element: Dict[str, Any]) -> bool:
        """
        Determine if element looks like a notification banner
        """
        # Check position - banners often appear at top or corners
        y_pos = element.get('y', 0)
        window_height = element.get('window_height', 800)
        
        if y_pos < window_height * 0.2:  # Top 20% of window
            # Check if has notification-like text
            text = element.get('text', '').lower()
            notification_keywords = ['new', 'message', 'notification', 'alert', 'update']
            
            if any(keyword in text for keyword in notification_keywords):
                return True
                
        return self._matches_learned_banner_pattern(element)
    
    async def _extract_notification_text(self, window, indicators) -> List[TextRegion]:
        """
        Extract text related to notifications using OCR
        """
        text_regions = []
        
        for indicator in indicators:
            element = indicator['element']
            
            # Define region around indicator to capture related text
            region = self._expand_region_for_context(element)
            
            # Use OCR to extract text
            # This would integrate with the actual screen capture
            # For now, we'll use the text already in the element
            if 'text' in element:
                text_regions.append(TextRegion(
                    text=element['text'],
                    confidence=indicator['confidence'],
                    bounding_box=(element.get('x', 0), element.get('y', 0), 
                                element.get('width', 100), element.get('height', 50)),
                    center_point=(element.get('x', 0) + element.get('width', 100)//2,
                                element.get('y', 0) + element.get('height', 50)//2),
                    area_type='notification'
                ))
                
        return text_regions
    
    async def _understand_notification(self, window, indicator, text_regions) -> Optional[IntelligentNotification]:
        """
        Use AI to understand what the notification is about
        """
        # Gather context
        app_name = window.app_name
        window_title = window.window_title
        
        # Extract relevant text
        notification_text = []
        for region in text_regions:
            if self._is_near_indicator(region, indicator['element']):
                notification_text.append(region.text)
                
        if not notification_text:
            return None
            
        # Determine context using AI understanding
        context = await self._determine_notification_context(
            app_name, window_title, notification_text
        )
        
        # Calculate urgency
        urgency = await self._calculate_urgency(notification_text, context)
        
        return IntelligentNotification(
            source_window_id=window.window_id,
            app_name=app_name,
            detected_text=notification_text,
            visual_elements={'indicator': indicator},
            context=context,
            confidence=indicator['confidence'],
            urgency_score=urgency
        )
    
    async def _determine_notification_context(self, app_name: str, window_title: str, 
                                            text_content: List[str]) -> NotificationContext:
        """
        Use AI to determine the context of the notification
        """
        # Combine all text for analysis
        full_text = ' '.join(text_content).lower()
        
        # Use pattern matching and AI to determine context
        if any(word in full_text for word in ['meeting', 'calendar', 'scheduled', 'starts in']):
            return NotificationContext.MEETING_REMINDER
        elif any(word in full_text for word in ['urgent', 'emergency', 'critical', 'asap']):
            return NotificationContext.URGENT_ALERT
        elif any(word in full_text for word in ['message', 'chat', 'replied', 'sent']):
            return NotificationContext.MESSAGE_RECEIVED
        elif any(word in full_text for word in ['system', 'update', 'restart', 'error']):
            return NotificationContext.SYSTEM_ALERT
        elif any(word in app_name.lower() for word in ['slack', 'teams', 'zoom']):
            return NotificationContext.WORK_UPDATE
        else:
            return NotificationContext.SOCIAL_UPDATE
    
    async def _calculate_urgency(self, text_content: List[str], 
                               context: NotificationContext) -> float:
        """
        Calculate urgency score using AI analysis
        """
        urgency = 0.5  # Base urgency
        
        # Context-based urgency
        if context == NotificationContext.URGENT_ALERT:
            urgency = 0.9
        elif context == NotificationContext.MEETING_REMINDER:
            urgency = 0.8
        elif context == NotificationContext.WORK_UPDATE:
            urgency = 0.7
            
        # Text analysis for urgency indicators
        full_text = ' '.join(text_content).lower()
        urgency_words = ['urgent', 'asap', 'emergency', 'critical', 'now', 'immediately']
        
        for word in urgency_words:
            if word in full_text:
                urgency = max(urgency, 0.85)
                
        # Time-based urgency
        time_indicators = ['in 5 minutes', 'in 10 minutes', 'starting soon']
        for indicator in time_indicators:
            if indicator in full_text:
                urgency = max(urgency, 0.85)
                
        return urgency
    
    async def _process_intelligent_notification(self, notification: IntelligentNotification):
        """
        Process and potentially announce the notification
        """
        # Check if should announce based on learned preferences
        if await self._should_announce(notification):
            # Generate natural speech
            announcement = await self._generate_natural_announcement(notification)
            
            # Announce via voice
            await self.voice_module.speak(announcement)
            
            # Learn from this interaction
            self._record_announcement(notification, announcement)
    
    async def _should_announce(self, notification: IntelligentNotification) -> bool:
        """
        Intelligently decide if notification should be announced
        """
        # High urgency always announced
        if notification.urgency_score > 0.8:
            return True
            
        # Check if similar notifications were announced before
        if self._was_similar_announced_recently(notification):
            return False
            
        # Use learned preferences
        return self._matches_announcement_preferences(notification)
    
    async def _generate_natural_announcement(self, notification: IntelligentNotification) -> str:
        """
        Generate natural speech for the notification using AI
        """
        # Build context-aware announcement
        parts = []
        
        # Urgency prefix
        if notification.urgency_score > 0.8:
            parts.append("Sir, urgent notification")
        elif notification.urgency_score > 0.6:
            parts.append("Sir, you have a notification")
            
        # App context
        parts.append(f"from {notification.app_name}")
        
        # Content summary
        if notification.detected_text:
            # Use AI to summarize the text naturally
            summary = self._summarize_notification_text(notification.detected_text)
            parts.append(summary)
            
        # Context-specific additions
        if notification.context == NotificationContext.MEETING_REMINDER:
            parts.append("Would you like me to prepare your workspace?")
        elif notification.context == NotificationContext.URGENT_ALERT:
            parts.append("This requires your immediate attention")
            
        return ". ".join(parts)
    
    def _summarize_notification_text(self, text_list: List[str]) -> str:
        """
        Create a natural summary of notification text
        """
        # Join and clean text
        full_text = ' '.join(text_list)
        
        # Remove redundant information
        cleaned = full_text.strip()
        
        # Truncate if too long
        if len(cleaned) > 100:
            cleaned = cleaned[:97] + "..."
            
        return f"The message says: {cleaned}"
    
    def _update_detection_patterns(self, indicators: List[Dict[str, Any]]):
        """
        Learn from detected indicators to improve future detection
        """
        for indicator in indicators:
            pattern_type = indicator['type']
            element = indicator['element']
            
            # Store successful detection patterns
            if pattern_type not in self.detection_patterns:
                self.detection_patterns[pattern_type] = []
                
            self.detection_patterns[pattern_type].append({
                'visual_characteristics': self._extract_visual_characteristics(element),
                'confidence': indicator['confidence'],
                'timestamp': datetime.now()
            })
            
    def _extract_visual_characteristics(self, element: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract visual characteristics for pattern learning
        """
        return {
            'size': (element.get('width', 0), element.get('height', 0)),
            'position_relative': self._calculate_relative_position(element),
            'color_category': self._categorize_color(element.get('color')),
            'text_pattern': self._extract_text_pattern(element.get('text', '')),
            'shape': self._determine_shape(element)
        }
    
    def _record_announcement(self, notification: IntelligentNotification, announcement: str):
        """
        Record announcement for learning
        """
        self.announcement_history.append({
            'notification': notification,
            'announcement': announcement,
            'timestamp': datetime.now(),
            'user_feedback': None  # Will be updated based on user response
        })
        
        # Keep history manageable
        if len(self.announcement_history) > 1000:
            self.announcement_history = self.announcement_history[-1000:]


# Integration with existing vision pipeline
class NotificationVisionIntegration:
    """
    Integrates notification intelligence with the vision decision pipeline
    """
    
    def __init__(self, vision_pipeline: VisionDecisionPipeline):
        self.vision_pipeline = vision_pipeline
        self.notification_intelligence = NotificationIntelligence()
        
        # Register notification detection as part of pipeline
        self._register_with_pipeline()
        
    def _register_with_pipeline(self):
        """
        Register notification detection with vision pipeline
        """
        # Add notification detection to decision engine
        self.vision_pipeline.decision_engine.register_decision_handler(
            'notification_detection',
            self._handle_notification_decision
        )
        
    async def _handle_notification_decision(self, context: Dict[str, Any]) -> List[AutonomousAction]:
        """
        Generate notification-related actions for the decision engine
        """
        actions = []
        
        # Get detected notifications from context
        notifications = context.get('detected_notifications', [])
        
        for notification in notifications:
            if notification.urgency_score > 0.7:
                # Create autonomous action to announce
                action = AutonomousAction(
                    action_type="announce_notification",
                    target="voice_system",
                    params={
                        'notification': notification,
                        'announcement': await self.notification_intelligence._generate_natural_announcement(notification)
                    },
                    priority=ActionPriority.HIGH if notification.urgency_score > 0.8 else ActionPriority.MEDIUM,
                    confidence=notification.confidence,
                    category="notification",
                    reasoning=f"Detected {notification.context.value} from {notification.app_name}",
                    requires_permission=False  # Notifications don't need permission
                )
                actions.append(action)
                
        return actions


async def test_intelligent_notifications():
    """Test the intelligent notification system"""
    print("🧠 Testing Intelligent Notification System")
    print("=" * 50)
    
    # Create notification intelligence
    notif_intel = NotificationIntelligence()
    
    # Simulate some test scenarios
    print("\n📊 Starting intelligent monitoring...")
    await notif_intel.start_intelligent_monitoring()
    
    # Let it run for a bit
    await asyncio.sleep(10)
    
    # Stop monitoring
    await notif_intel.stop_monitoring()
    
    print("\n✅ Intelligent notification test complete!")


if __name__ == "__main__":
    asyncio.run(test_intelligent_notifications())