"""
Feedback-Aware Vision Intelligence Integration
Connects ProactiveVisionIntelligence with FeedbackLearningLoop for adaptive notifications.

This module demonstrates the "Invisible Assistant" UX philosophy:
- Learns from user engagement/dismissals
- Adapts notification importance dynamically
- Respects user's time and attention patterns
- Gets smarter over time without being intrusive
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Any, Optional, List
from datetime import datetime
from dataclasses import dataclass

from .boundary_adapters import safe_text

logger = logging.getLogger(__name__)


@dataclass
class FeedbackAwareNotification:
    """Notification enhanced with feedback learning."""
    original_change: Any  # ScreenChange from proactive_vision_intelligence
    pattern_type: str  # NotificationPattern enum value
    base_importance: float  # 0.0-1.0
    adjusted_importance: float  # After feedback learning
    should_send: bool
    learning_context: Dict[str, Any]
    notification_id: str
    timestamp: datetime


class FeedbackAwareVisionIntelligence:
    """
    Enhances ProactiveVisionIntelligence with feedback learning.

    Architecture:
        ProactiveVisionIntelligence (detects changes)
            ↓
        FeedbackLearningLoop (filters & adjusts importance)
            ↓
        User sees only relevant notifications
            ↓
        User responds (engage/dismiss/negative)
            ↓
        FeedbackLearningLoop learns & adapts
    """

    def __init__(
        self,
        proactive_vision: Any,  # ProactiveVisionIntelligence instance
        feedback_loop: Any,  # FeedbackLearningLoop instance
    ):
        """
        Initialize feedback-aware vision intelligence.

        Args:
            proactive_vision: ProactiveVisionIntelligence instance
            feedback_loop: FeedbackLearningLoop instance
        """
        self.proactive_vision = proactive_vision
        self.feedback_loop = feedback_loop

        # Track notifications waiting for user response
        self.pending_notifications: Dict[str, FeedbackAwareNotification] = {}

        # Notification timing
        self.last_notification_time: Optional[datetime] = None

        # Integration hooks
        self._setup_hooks()

        logger.info("[FEEDBACK-VISION] Initialized feedback-aware vision intelligence")

    def _setup_hooks(self) -> None:
        """Set up hooks to intercept proactive vision notifications."""
        # Replace notification callback with our feedback-aware version
        original_callback = self.proactive_vision.notification_callback
        self.proactive_vision.notification_callback = self._feedback_aware_callback(original_callback)

        logger.debug("[FEEDBACK-VISION] Installed notification hooks")

    def _feedback_aware_callback(self, original_callback):
        """
        Create feedback-aware wrapper for notification callback.

        This intercepts notifications from ProactiveVisionIntelligence,
        applies feedback learning, and decides whether to show them.
        """
        async def wrapper(change) -> str:
            """
            Process notification through feedback learning.

            Args:
                change: ScreenChange from ProactiveVisionIntelligence

            Returns:
                User response: 'engaged', 'dismissed', 'deferred', 'negative_feedback'
            """
            # Map ScreenChange category to NotificationPattern
            pattern = self._map_category_to_pattern(change.category)

            # Get base importance from change
            base_importance = self._calculate_base_importance(change)

            # Build context for pattern matching
            context = {
                'window_type': change.location,
                'category': change.category.value,
                'confidence': change.confidence,
            }

            # Check timing - is now a good time to notify?
            current_hour = datetime.now().hour
            if not self.feedback_loop.is_good_time_to_notify(current_hour):
                logger.debug(
                    f"[FEEDBACK-VISION] Deferring notification - hour {current_hour} "
                    "has low historical engagement"
                )
                # Could queue for later, but for now just skip
                return 'deferred'

            # Apply feedback learning
            should_send, adjusted_importance = self.feedback_loop.should_show_notification(
                pattern=pattern,
                base_importance=base_importance,
                context=context,
            )

            if not should_send:
                logger.info(
                    f"[FEEDBACK-VISION] Suppressed notification: {change.description} "
                    f"(pattern={pattern.value}, learned suppression)"
                )
                # Record as auto-dismissed
                await self.feedback_loop.record_feedback(
                    pattern=pattern,
                    response='dismissed',
                    notification_text=change.suggested_message,
                    context=context,
                    time_to_respond=0.0,
                )
                return 'dismissed'

            # Create feedback-aware notification
            notification_id = f"notif_{int(time.time() * 1000)}"
            notification = FeedbackAwareNotification(
                original_change=change,
                pattern_type=pattern.value,
                base_importance=base_importance,
                adjusted_importance=adjusted_importance,
                should_send=should_send,
                learning_context=context,
                notification_id=notification_id,
                timestamp=datetime.now(),
            )

            # Track pending notification
            self.pending_notifications[notification_id] = notification
            notification_start_time = time.time()

            # Send to user via original callback
            logger.info(
                f"[FEEDBACK-VISION] Sending notification: {change.suggested_message} "
                f"(base_importance={base_importance:.2f}, "
                f"adjusted={adjusted_importance:.2f})"
            )

            user_response = safe_text(await original_callback(change))

            # Calculate response time
            time_to_respond = time.time() - notification_start_time

            # Map response to UserResponse enum
            from backend.core.learning.feedback_loop import UserResponse
            user_response_lower = user_response.lower()
            if 'detail' in user_response_lower or 'yes' in user_response_lower or 'tell me' in user_response_lower:
                response_type = UserResponse.ENGAGED
            elif 'no' in user_response_lower or 'dismiss' in user_response_lower:
                response_type = UserResponse.DISMISSED
            elif 'later' in user_response_lower or 'not now' in user_response_lower:
                response_type = UserResponse.DEFERRED
            elif 'stop' in user_response_lower or 'never' in user_response_lower:
                response_type = UserResponse.NEGATIVE_FEEDBACK
            else:
                # Default to dismissal if unclear
                response_type = UserResponse.DISMISSED

            # Record feedback
            await self.feedback_loop.record_feedback(
                pattern=pattern,
                response=response_type,
                notification_text=change.suggested_message,
                context=context,
                time_to_respond=time_to_respond,
            )

            # Remove from pending
            self.pending_notifications.pop(notification_id, None)
            self.last_notification_time = datetime.now()

            logger.info(
                f"[FEEDBACK-VISION] Recorded user response: {response_type.value} "
                f"(time_to_respond={time_to_respond:.1f}s)"
            )

            return response_type.value

        return wrapper

    def _map_category_to_pattern(self, category) -> Any:
        """
        Map ScreenChange category to NotificationPattern.

        Args:
            category: ChangeCategory enum from proactive_vision_intelligence

        Returns:
            NotificationPattern enum from feedback_loop
        """
        from backend.core.learning.feedback_loop import NotificationPattern

        # Map ChangeCategory -> NotificationPattern
        mapping = {
            'error': NotificationPattern.TERMINAL_ERROR,
            'warning': NotificationPattern.TERMINAL_WARNING,
            'completion': NotificationPattern.TERMINAL_COMPLETION,
            'update': NotificationPattern.BROWSER_UPDATE,
            'notification': NotificationPattern.WORKFLOW_SUGGESTION,
            'dialog': NotificationPattern.WORKFLOW_SUGGESTION,
            'status': NotificationPattern.RESOURCE_WARNING,
        }

        category_value = category.value if hasattr(category, 'value') else str(category)
        return mapping.get(category_value, NotificationPattern.OTHER)

    def _calculate_base_importance(self, change) -> float:
        """
        Calculate base importance from ScreenChange.

        Args:
            change: ScreenChange object

        Returns:
            Base importance score (0.0-1.0)
        """
        # Map Priority to importance score
        importance_map = {
            'high': 0.9,
            'medium': 0.7,
            'low': 0.5,
        }

        priority_value = change.importance.value if hasattr(change.importance, 'value') else str(change.importance)
        base = importance_map.get(priority_value, 0.5)

        # Adjust by confidence
        return base * change.confidence

    async def get_learning_insights(self) -> Dict[str, Any]:
        """
        Get insights about what JARVIS has learned.

        Returns:
            Dictionary with learning statistics and insights
        """
        from backend.core.learning.feedback_loop import NotificationPattern

        # Get insights for each pattern type
        pattern_insights = {}
        for pattern in NotificationPattern:
            insights = self.feedback_loop.get_pattern_insights(pattern)
            if insights.get('has_data'):
                pattern_insights[pattern.value] = insights

        # Overall statistics
        exported_data = self.feedback_loop.export_learned_data()

        return {
            'pattern_insights': pattern_insights,
            'total_feedback_events': exported_data['total_feedback_events'],
            'timing_insights': exported_data['timing_insights'],
            'suppressed_patterns_count': len(exported_data['suppressed_patterns']),
            'last_notification': self.last_notification_time.isoformat() if self.last_notification_time else None,
            'pending_notifications': len(self.pending_notifications),
        }

    async def reset_learning(self, pattern_type: Optional[str] = None) -> None:
        """
        Reset learned data (user control).

        Args:
            pattern_type: Specific pattern to reset, or None for all
        """
        from backend.core.learning.feedback_loop import NotificationPattern

        if pattern_type:
            try:
                pattern = NotificationPattern(pattern_type)
                await self.feedback_loop.reset_learning(pattern)
                logger.info(f"[FEEDBACK-VISION] Reset learning for {pattern_type}")
            except ValueError:
                logger.error(f"[FEEDBACK-VISION] Invalid pattern type: {pattern_type}")
        else:
            await self.feedback_loop.reset_learning()
            logger.info("[FEEDBACK-VISION] Reset all learning data")

    async def start_monitoring(self) -> None:
        """Start feedback-aware monitoring."""
        logger.info("[FEEDBACK-VISION] Starting feedback-aware vision monitoring")
        await self.proactive_vision.start_monitoring()

    async def stop_monitoring(self) -> None:
        """Stop feedback-aware monitoring."""
        logger.info("[FEEDBACK-VISION] Stopping feedback-aware vision monitoring")
        await self.proactive_vision.stop_monitoring()


# Factory function
async def create_feedback_aware_vision(
    vision_analyzer: Any,
    notification_callback: Any,
    storage_path: Optional[Any] = None,
) -> FeedbackAwareVisionIntelligence:
    """
    Factory function to create fully integrated feedback-aware vision system.

    Args:
        vision_analyzer: Claude Vision analyzer instance
        notification_callback: Callback for sending notifications
        storage_path: Path to persist learned data

    Returns:
        FeedbackAwareVisionIntelligence instance
    """
    from backend.vision.proactive_vision_intelligence import ProactiveVisionIntelligence
    from backend.core.learning.feedback_loop import FeedbackLearningLoop

    # Create components
    proactive_vision = ProactiveVisionIntelligence(
        vision_analyzer=vision_analyzer,
        notification_callback=notification_callback,
    )

    feedback_loop = FeedbackLearningLoop(storage_path=storage_path)

    # Create integrated system
    feedback_aware = FeedbackAwareVisionIntelligence(
        proactive_vision=proactive_vision,
        feedback_loop=feedback_loop,
    )

    logger.info("[FEEDBACK-VISION] Created feedback-aware vision system")
    return feedback_aware
