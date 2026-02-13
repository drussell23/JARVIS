"""
JARVIS AGI OS - Proactive Event Stream

Event-driven architecture for autonomous notifications and actions.
Connects all JARVIS systems into a unified event stream that enables
proactive, intelligent behavior.

Features:
- Unified event bus for all AGI OS components
- Priority-based event handling
- Event filtering and routing
- Automatic voice narration of important events
- Event correlation and deduplication
- Integration with screen analyzer, decision engine, and action executor

Event Flow:
    Screen Analyzer -> Event -> Decision Engine -> Event -> Approval -> Event -> Execution

Usage:
    from agi_os import get_event_stream, AGIEvent, EventType

    stream = await get_event_stream()

    # Subscribe to events
    stream.subscribe(EventType.ERROR_DETECTED, my_handler)

    # Emit an event
    await stream.emit(AGIEvent(
        event_type=EventType.ERROR_DETECTED,
        source="screen_analyzer",
        data={"line": 42, "message": "SyntaxError"}
    ))
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Coroutine, Union

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of events in the AGI OS."""
    # Detection Events
    ERROR_DETECTED = "error_detected"
    WARNING_DETECTED = "warning_detected"
    NOTIFICATION_DETECTED = "notification_detected"
    MEETING_DETECTED = "meeting_detected"
    SECURITY_CONCERN = "security_concern"
    CONTENT_CHANGED = "content_changed"
    APP_CHANGED = "app_changed"

    # Decision Events
    ACTION_REQUESTED = "action_requested"
    ACTION_SCHEDULED = "action_scheduled"
    ACTION_PROPOSED = "action_proposed"
    ACTION_APPROVED = "action_approved"
    ACTION_DENIED = "action_denied"
    ACTION_AUTO_APPROVED = "action_auto_approved"

    # Execution Events
    ACTION_STARTED = "action_started"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    ACTION_ROLLED_BACK = "action_rolled_back"

    # System Events
    SYSTEM_STARTED = "system_started"
    SYSTEM_STOPPED = "system_stopped"
    HEALTH_CHECK = "health_check"
    MEMORY_WARNING = "memory_warning"

    # User Events
    USER_SPOKE = "user_spoke"
    USER_APPROVED = "user_approved"
    USER_DENIED = "user_denied"
    USER_COMMAND = "user_command"

    # Learning Events
    PATTERN_LEARNED = "pattern_learned"
    THRESHOLD_ADJUSTED = "threshold_adjusted"
    BEHAVIOR_UPDATED = "behavior_updated"


class EventPriority(Enum):
    """Priority levels for events."""
    DEBUG = 0       # Internal debugging only
    LOW = 1         # Background, informational
    NORMAL = 2      # Standard events
    HIGH = 3        # Important events
    URGENT = 4      # Requires attention
    CRITICAL = 5    # Immediate action needed


@dataclass
class AGIEvent:
    """Represents an event in the AGI OS."""
    event_type: EventType
    source: str                   # Component that generated the event
    data: Dict[str, Any] = field(default_factory=dict)
    priority: EventPriority = EventPriority.NORMAL
    timestamp: datetime = field(default_factory=datetime.now)
    event_id: str = field(default="")
    correlation_id: Optional[str] = None  # Link related events
    requires_narration: bool = False      # Should be spoken
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.event_id:
            self.event_id = hashlib.md5(
                f"{self.event_type.value}{self.source}{self.timestamp.isoformat()}".encode()
            ).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'event_id': self.event_id,
            'event_type': self.event_type.value,
            'source': self.source,
            'data': self.data,
            'priority': self.priority.value,
            'timestamp': self.timestamp.isoformat(),
            'correlation_id': self.correlation_id,
            'requires_narration': self.requires_narration,
            'metadata': self.metadata,
        }


EventHandler = Callable[[AGIEvent], Coroutine[Any, Any, None]]


@dataclass
class EventSubscription:
    """Represents a subscription to events."""
    handler: EventHandler
    event_types: Set[EventType]
    min_priority: EventPriority = EventPriority.DEBUG
    filter_func: Optional[Callable[[AGIEvent], bool]] = None
    subscription_id: str = field(default="")

    def __post_init__(self):
        if not self.subscription_id:
            self.subscription_id = hashlib.md5(
                f"{id(self.handler)}{datetime.now().isoformat()}".encode()
            ).hexdigest()[:8]


class ProactiveEventStream:
    """
    Central event stream for AGI OS.

    Provides a unified event bus that connects all components:
    - Screen analyzer emits detection events
    - Decision engine emits action proposals
    - Approval manager emits approval/denial events
    - Action executor emits execution events
    - Voice communicator narrates important events
    """

    def __init__(self):
        """Initialize the event stream."""
        # Event queue
        self._event_queue: asyncio.Queue[AGIEvent] = asyncio.Queue()

        # Subscriptions
        self._subscriptions: Dict[str, EventSubscription] = {}
        self._type_subscriptions: Dict[EventType, Set[str]] = {
            event_type: set() for event_type in EventType
        }
        self._global_subscriptions: Set[str] = set()

        # Event history for correlation
        self._event_history: OrderedDict[str, AGIEvent] = OrderedDict()
        self._max_history = 500
        self._correlation_window = timedelta(minutes=5)

        # Deduplication
        self._recent_events: OrderedDict[str, datetime] = OrderedDict()
        self._dedup_window = timedelta(seconds=5)
        self._max_dedup_entries = 200

        # Processing state
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None

        # Voice communicator (lazy loaded)
        self._voice: Optional[Any] = None

        # Statistics
        self._stats = {
            'events_emitted': 0,
            'events_processed': 0,
            'events_narrated': 0,
            'events_deduplicated': 0,
            'handlers_called': 0,
            'handler_errors': 0,
        }

        # Narration templates for natural speech
        self._narration_templates = self._load_narration_templates()

        logger.info("ProactiveEventStream initialized")

    def _load_narration_templates(self) -> Dict[EventType, str]:
        """Load templates for narrating events."""
        return {
            EventType.ERROR_DETECTED: "Sir, I've detected {error_type} in {location}.",
            EventType.WARNING_DETECTED: "Sir, warning detected: {message}",
            EventType.NOTIFICATION_DETECTED: "Sir, you have {count} new notifications from {source}.",
            EventType.MEETING_DETECTED: "Sir, you have a meeting in {minutes} minutes: {title}",
            EventType.SECURITY_CONCERN: "Sir, security concern detected: {description}",
            EventType.ACTION_REQUESTED: "Sir, AGI decision engine has requested execution of {action}.",
            EventType.ACTION_PROPOSED: "Sir, I'd like to {action}. {reason}",
            EventType.ACTION_APPROVED: "Understood. Proceeding with {action}.",
            EventType.ACTION_DENIED: "Very well. Cancelling {action}.",
            EventType.ACTION_COMPLETED: "Sir, I've completed {action}.",
            EventType.ACTION_FAILED: "Sir, I encountered an issue with {action}: {error}",
            EventType.PATTERN_LEARNED: "I've learned from that. I'll remember for next time.",
            EventType.MEMORY_WARNING: "Sir, system memory is running low.",
        }

    async def _get_voice(self):
        """Lazy load voice communicator."""
        if self._voice is None:
            try:
                from .realtime_voice_communicator import get_voice_communicator
                self._voice = await get_voice_communicator()
            except Exception as e:
                logger.warning("Voice communicator not available: %s", e)
        return self._voice

    async def start(self) -> None:
        """Start the event stream processor."""
        if self._running:
            return

        self._running = True
        self._processor_task = asyncio.create_task(
            self._event_processor(),
            name="agi_os_event_processor"
        )

        # Emit system started event
        await self.emit(AGIEvent(
            event_type=EventType.SYSTEM_STARTED,
            source="event_stream",
            data={'timestamp': datetime.now().isoformat()},
            priority=EventPriority.HIGH,
        ))

        logger.info("Event stream started")

    async def stop(self) -> None:
        """Stop the event stream processor."""
        if not self._running:
            return

        # Emit system stopped event
        await self.emit(AGIEvent(
            event_type=EventType.SYSTEM_STOPPED,
            source="event_stream",
            data={'timestamp': datetime.now().isoformat()},
            priority=EventPriority.HIGH,
        ))

        self._running = False

        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        logger.info("Event stream stopped")

    async def emit(
        self,
        event: AGIEvent,
        deduplicate: bool = True
    ) -> bool:
        """
        Emit an event to the stream.

        Args:
            event: The event to emit
            deduplicate: Whether to skip duplicate events

        Returns:
            True if event was emitted, False if deduplicated
        """
        # Check for duplicates
        if deduplicate:
            dedup_key = self._get_dedup_key(event)
            if dedup_key in self._recent_events:
                self._stats['events_deduplicated'] += 1
                logger.debug("Deduplicated event: %s", event.event_type.value)
                return False

            # Record for deduplication
            self._recent_events[dedup_key] = datetime.now()
            self._trim_dedup_cache()

        # Add to queue
        await self._event_queue.put(event)
        self._stats['events_emitted'] += 1

        # Add to history
        self._event_history[event.event_id] = event
        self._trim_history()

        logger.debug(
            "Event emitted: %s from %s (priority=%s)",
            event.event_type.value,
            event.source,
            event.priority.name
        )

        return True

    def subscribe(
        self,
        event_types: Union[EventType, List[EventType], None],
        handler: EventHandler,
        min_priority: EventPriority = EventPriority.DEBUG,
        filter_func: Optional[Callable[[AGIEvent], bool]] = None
    ) -> str:
        """
        Subscribe to events.

        Args:
            event_types: Event types to subscribe to (None for all)
            handler: Async handler function
            min_priority: Minimum priority to receive
            filter_func: Optional filter function

        Returns:
            Subscription ID for unsubscribing
        """
        if event_types is None:
            types_set = set()  # Empty means all
        elif isinstance(event_types, EventType):
            types_set = {event_types}
        else:
            types_set = set(event_types)

        subscription = EventSubscription(
            handler=handler,
            event_types=types_set,
            min_priority=min_priority,
            filter_func=filter_func,
        )

        self._subscriptions[subscription.subscription_id] = subscription

        # Register in type-specific maps
        if types_set:
            for event_type in types_set:
                self._type_subscriptions[event_type].add(subscription.subscription_id)
        else:
            self._global_subscriptions.add(subscription.subscription_id)

        logger.debug(
            "Subscription added: %s for %s",
            subscription.subscription_id,
            [t.value for t in types_set] if types_set else "all"
        )

        return subscription.subscription_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """
        Unsubscribe from events.

        Args:
            subscription_id: ID returned from subscribe()

        Returns:
            True if unsubscribed, False if not found
        """
        if subscription_id not in self._subscriptions:
            return False

        subscription = self._subscriptions.pop(subscription_id)

        # Remove from type maps
        for event_type in subscription.event_types:
            self._type_subscriptions[event_type].discard(subscription_id)
        self._global_subscriptions.discard(subscription_id)

        return True

    async def _event_processor(self) -> None:
        """Background task that processes events."""
        while self._running:
            try:
                # Wait for event with timeout
                try:
                    event = await asyncio.wait_for(
                        self._event_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Process the event
                await self._process_event(event)
                self._stats['events_processed'] += 1

                self._event_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Event processor error: %s", e)

    async def _process_event(self, event: AGIEvent) -> None:
        """
        Process a single event.

        Args:
            event: Event to process
        """
        # Find relevant subscriptions
        relevant_subs = self._get_relevant_subscriptions(event)

        # Narrate if needed
        if event.requires_narration or event.priority.value >= EventPriority.HIGH.value:
            await self._narrate_event(event)

        # Call handlers
        for sub_id in relevant_subs:
            subscription = self._subscriptions.get(sub_id)
            if not subscription:
                continue

            # Check priority
            if event.priority.value < subscription.min_priority.value:
                continue

            # Check filter
            if subscription.filter_func and not subscription.filter_func(event):
                continue

            # Call handler
            try:
                await subscription.handler(event)
                self._stats['handlers_called'] += 1
            except Exception as e:
                self._stats['handler_errors'] += 1
                logger.error(
                    "Handler error for %s: %s",
                    event.event_type.value,
                    e
                )

    def _get_relevant_subscriptions(self, event: AGIEvent) -> Set[str]:
        """Get subscriptions relevant to an event."""
        relevant = set()

        # Type-specific subscriptions
        relevant.update(self._type_subscriptions.get(event.event_type, set()))

        # Global subscriptions
        relevant.update(self._global_subscriptions)

        return relevant

    async def _narrate_event(self, event: AGIEvent) -> None:
        """
        Narrate an event via voice.

        Args:
            event: Event to narrate
        """
        voice = await self._get_voice()
        if not voice:
            return

        # Get template
        template = self._narration_templates.get(event.event_type)
        if not template:
            return

        # Build narration text
        try:
            text = template.format(**event.data)
        except KeyError:
            # Missing data, use generic
            text = f"Sir, {event.event_type.value.replace('_', ' ')} event occurred."

        # Determine voice mode and priority
        from .realtime_voice_communicator import VoiceMode, VoicePriority

        if event.priority == EventPriority.CRITICAL:
            voice_mode = VoiceMode.URGENT
            voice_priority = VoicePriority.CRITICAL
        elif event.priority == EventPriority.URGENT:
            voice_mode = VoiceMode.URGENT
            voice_priority = VoicePriority.URGENT
        elif event.priority == EventPriority.HIGH:
            voice_mode = VoiceMode.NOTIFICATION
            voice_priority = VoicePriority.HIGH
        else:
            voice_mode = VoiceMode.NOTIFICATION
            voice_priority = VoicePriority.NORMAL

        context = self._build_narration_context(event)
        await voice.speak(text, mode=voice_mode, priority=voice_priority, context=context)
        self._stats['events_narrated'] += 1

    def _build_narration_context(self, event: AGIEvent) -> Dict[str, Any]:
        """
        Build speech context for narration, including optional listen-window hints.

        Interactive events open a short post-speech listening window so users can
        reply without re-triggering a wake word.
        """
        context: Dict[str, Any] = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "event_source": event.source,
        }

        interactive_types = {
            EventType.MEETING_DETECTED,
            EventType.SECURITY_CONCERN,
            EventType.ACTION_REQUESTED,
            EventType.ACTION_PROPOSED,
        }
        interactive_override = event.metadata.get("expect_user_response")
        is_interactive = (
            bool(interactive_override)
            if interactive_override is not None
            else event.event_type in interactive_types
        )

        if is_interactive:
            high_priority_timeout = float(os.getenv("AGI_VOICE_LISTEN_TIMEOUT_HIGH", "18.0"))
            normal_timeout = float(os.getenv("AGI_VOICE_LISTEN_TIMEOUT_NORMAL", "12.0"))
            timeout_seconds = float(
                event.metadata.get(
                    "listen_timeout_seconds",
                    high_priority_timeout
                    if event.priority.value >= EventPriority.HIGH.value
                    else normal_timeout,
                )
            )
            context.update(
                {
                    "open_listen_window": True,
                    "listen_reason": f"event:{event.event_type.value}",
                    "listen_timeout_seconds": timeout_seconds,
                    "listen_close_on_utterance": True,
                    "listen_metadata": {
                        "event_id": event.event_id,
                        "event_type": event.event_type.value,
                        "priority": event.priority.value,
                    },
                }
            )

        return context

    def _get_dedup_key(self, event: AGIEvent) -> str:
        """Generate deduplication key for an event."""
        # Key based on type, source, and significant data
        significant_data = {
            k: v for k, v in sorted(event.data.items())
            if k not in ['timestamp', 'event_id']
        }
        return hashlib.md5(
            f"{event.event_type.value}{event.source}{significant_data}".encode()
        ).hexdigest()[:16]

    def _trim_dedup_cache(self) -> None:
        """Trim deduplication cache."""
        now = datetime.now()

        # Remove expired entries
        expired = [
            key for key, time in self._recent_events.items()
            if now - time > self._dedup_window
        ]
        for key in expired:
            del self._recent_events[key]

        # Trim to max size
        while len(self._recent_events) > self._max_dedup_entries:
            self._recent_events.popitem(last=False)

    def _trim_history(self) -> None:
        """Trim event history."""
        while len(self._event_history) > self._max_history:
            self._event_history.popitem(last=False)

    def get_correlated_events(
        self,
        correlation_id: str
    ) -> List[AGIEvent]:
        """
        Get events with the same correlation ID.

        Args:
            correlation_id: Correlation ID to search for

        Returns:
            List of correlated events
        """
        return [
            event for event in self._event_history.values()
            if event.correlation_id == correlation_id
        ]

    def get_recent_events(
        self,
        event_type: Optional[EventType] = None,
        source: Optional[str] = None,
        minutes: int = 5
    ) -> List[AGIEvent]:
        """
        Get recent events with optional filtering.

        Args:
            event_type: Filter by event type
            source: Filter by source
            minutes: How far back to look

        Returns:
            List of matching events
        """
        cutoff = datetime.now() - timedelta(minutes=minutes)
        events = []

        for event in reversed(self._event_history.values()):
            if event.timestamp < cutoff:
                break

            if event_type and event.event_type != event_type:
                continue
            if source and event.source != source:
                continue

            events.append(event)

        return events

    def create_correlation_id(self) -> str:
        """Create a new correlation ID for linking events."""
        return hashlib.md5(
            f"{datetime.now().isoformat()}{id(self)}".encode()
        ).hexdigest()[:12]

    def get_stats(self) -> Dict[str, Any]:
        """Get event stream statistics."""
        return {
            **self._stats,
            'queue_size': self._event_queue.qsize(),
            'subscriptions': len(self._subscriptions),
            'history_size': len(self._event_history),
            'dedup_cache_size': len(self._recent_events),
            'running': self._running,
        }

    # ============== Convenience Event Emitters ==============

    async def emit_error_detected(
        self,
        error_type: str,
        location: str,
        message: str,
        source: str = "screen_analyzer",
        **kwargs
    ) -> str:
        """Emit an error detection event."""
        event = AGIEvent(
            event_type=EventType.ERROR_DETECTED,
            source=source,
            data={
                'error_type': error_type,
                'location': location,
                'message': message,
                **kwargs
            },
            priority=EventPriority.HIGH,
            requires_narration=True,
        )
        await self.emit(event)
        return event.event_id

    async def emit_action_proposed(
        self,
        action: str,
        target: str,
        reason: str,
        confidence: float,
        correlation_id: Optional[str] = None,
        source: str = "decision_engine",
        **kwargs
    ) -> str:
        """Emit an action proposal event."""
        event = AGIEvent(
            event_type=EventType.ACTION_PROPOSED,
            source=source,
            data={
                'action': action,
                'target': target,
                'reason': reason,
                'confidence': confidence,
                **kwargs
            },
            priority=EventPriority.NORMAL,
            correlation_id=correlation_id,
        )
        await self.emit(event)
        return event.event_id

    async def emit_action_completed(
        self,
        action: str,
        result: str = "successfully",
        correlation_id: Optional[str] = None,
        source: str = "action_executor",
        **kwargs
    ) -> str:
        """Emit an action completion event."""
        event = AGIEvent(
            event_type=EventType.ACTION_COMPLETED,
            source=source,
            data={
                'action': action,
                'result': result,
                **kwargs
            },
            priority=EventPriority.NORMAL,
            correlation_id=correlation_id,
            requires_narration=True,
        )
        await self.emit(event)
        return event.event_id

    async def emit_learning_event(
        self,
        pattern_type: str,
        details: Dict[str, Any],
        source: str = "learning_system",
    ) -> str:
        """Emit a learning event."""
        event = AGIEvent(
            event_type=EventType.PATTERN_LEARNED,
            source=source,
            data={
                'pattern_type': pattern_type,
                **details
            },
            priority=EventPriority.LOW,
        )
        await self.emit(event)
        return event.event_id


# ============== Singleton Pattern ==============

_event_stream: Optional[ProactiveEventStream] = None


async def get_event_stream() -> ProactiveEventStream:
    """
    Get the global event stream instance.

    Returns:
        The ProactiveEventStream singleton
    """
    global _event_stream

    if _event_stream is None:
        _event_stream = ProactiveEventStream()

    if not _event_stream._running:
        await _event_stream.start()

    return _event_stream


async def stop_event_stream() -> None:
    """Stop the global event stream."""
    global _event_stream

    if _event_stream is not None:
        await _event_stream.stop()
        _event_stream = None


if __name__ == "__main__":
    async def test():
        """Test the event stream."""
        stream = await get_event_stream()

        print("Testing ProactiveEventStream...")

        # Subscribe to events
        async def error_handler(event: AGIEvent):
            print(f"  Handler received: {event.event_type.value}")

        sub_id = stream.subscribe(EventType.ERROR_DETECTED, error_handler)

        # Emit events
        await stream.emit_error_detected(
            error_type="SyntaxError",
            location="app.py line 42",
            message="Unexpected token"
        )

        await stream.emit_action_proposed(
            action="fix the error",
            target="app.py",
            reason="Detected a syntax error I can fix",
            confidence=0.85
        )

        await asyncio.sleep(2)

        print(f"\nStats: {stream.get_stats()}")

        # Cleanup
        stream.unsubscribe(sub_id)
        await stop_event_stream()

        print("\nTest complete!")

    asyncio.run(test())
