"""
JARVIS macOS Helper - Notification Monitor

Monitors system and app notifications post-delivery using UserNotifications framework.
Provides intelligent notification summarization and AGI OS integration.

Features:
- Read notifications after they appear (Apple-compliant)
- Categorize notifications by type and priority
- Intelligent filtering and deduplication
- Integration with calendar events
- Voice narration for important notifications
- Learning from user interactions

Apple Compliance:
- Reads notifications AFTER delivery only
- Uses public UserNotifications API
- Respects user notification preferences
- No pre-delivery interception (not possible)
- Cannot modify or suppress notifications

Architecture:
    Notification Center → Post-Delivery Reading → Categorization → Event Emission
                                                         ↓
                                                 AGI OS Processing
                                                         ↓
                                              Smart Summarization / Voice
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# Notification Categories
# =============================================================================

class NotificationCategory(str, Enum):
    """Categories of notifications for intelligent handling."""
    # Communication
    MESSAGE = "message"  # iMessage, WhatsApp, Slack, etc.
    EMAIL = "email"
    SOCIAL = "social"  # Twitter, LinkedIn, etc.
    CALL = "call"  # Phone, FaceTime, Zoom

    # Calendar & Tasks
    CALENDAR = "calendar"
    REMINDER = "reminder"
    MEETING = "meeting"

    # System
    SYSTEM = "system"  # macOS updates, alerts
    SECURITY = "security"  # Password prompts, auth requests
    BATTERY = "battery"
    STORAGE = "storage"

    # Development
    IDE = "ide"  # VS Code, Xcode notifications
    BUILD = "build"  # CI/CD notifications
    GIT = "git"

    # Finance
    FINANCE = "finance"  # Banking, payments
    SHOPPING = "shopping"

    # Other
    NEWS = "news"
    ENTERTAINMENT = "entertainment"
    UNKNOWN = "unknown"


class NotificationPriority(str, Enum):
    """Priority levels for notifications."""
    CRITICAL = "critical"  # Security, urgent system
    HIGH = "high"  # Direct messages, upcoming meetings
    NORMAL = "normal"  # General notifications
    LOW = "low"  # Promotional, news
    MUTED = "muted"  # Muted apps


# =============================================================================
# Notification Data Classes
# =============================================================================

@dataclass
class ParsedNotification:
    """Parsed notification with categorization and metadata."""
    notification_id: str
    title: str
    body: str
    subtitle: str = ""
    app_name: str = ""
    bundle_id: str = ""

    # Categorization
    category: NotificationCategory = NotificationCategory.UNKNOWN
    priority: NotificationPriority = NotificationPriority.NORMAL

    # Metadata
    timestamp: datetime = field(default_factory=datetime.now)
    thread_id: str = ""
    actions: List[str] = field(default_factory=list)
    user_info: Dict[str, Any] = field(default_factory=dict)

    # Analysis
    sender: str = ""  # Extracted sender name
    is_reply: bool = False
    sentiment: str = ""  # positive, negative, neutral
    urgency_score: float = 0.0  # 0-1
    actionable: bool = False

    # Processing flags
    was_narrated: bool = False
    was_summarized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "title": self.title,
            "body": self.body,
            "subtitle": self.subtitle,
            "app_name": self.app_name,
            "bundle_id": self.bundle_id,
            "category": self.category.value,
            "priority": self.priority.value,
            "timestamp": self.timestamp.isoformat(),
            "thread_id": self.thread_id,
            "sender": self.sender,
            "is_reply": self.is_reply,
            "urgency_score": self.urgency_score,
            "actionable": self.actionable,
        }


@dataclass
class NotificationSummary:
    """Summary of multiple notifications."""
    total_count: int = 0
    by_category: Dict[NotificationCategory, int] = field(default_factory=dict)
    by_app: Dict[str, int] = field(default_factory=dict)
    high_priority_count: int = 0
    unread_messages: int = 0
    upcoming_meetings: int = 0
    period_start: datetime = field(default_factory=datetime.now)
    period_end: datetime = field(default_factory=datetime.now)
    summary_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_count": self.total_count,
            "by_category": {k.value: v for k, v in self.by_category.items()},
            "by_app": self.by_app,
            "high_priority_count": self.high_priority_count,
            "unread_messages": self.unread_messages,
            "upcoming_meetings": self.upcoming_meetings,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "summary_text": self.summary_text,
        }


# =============================================================================
# App Classification Rules
# =============================================================================

# Bundle IDs to notification categories
APP_CATEGORY_MAP: Dict[str, NotificationCategory] = {
    # Messages
    "com.apple.MobileSMS": NotificationCategory.MESSAGE,
    "com.apple.iChat": NotificationCategory.MESSAGE,
    "net.whatsapp.WhatsApp": NotificationCategory.MESSAGE,
    "com.tinyspeck.slackmacgap": NotificationCategory.MESSAGE,
    "com.microsoft.teams": NotificationCategory.MESSAGE,
    "org.telegram.desktop": NotificationCategory.MESSAGE,
    "com.facebook.Messenger": NotificationCategory.MESSAGE,
    "com.discord.Discord": NotificationCategory.MESSAGE,

    # Email
    "com.apple.mail": NotificationCategory.EMAIL,
    "com.microsoft.Outlook": NotificationCategory.EMAIL,
    "com.google.Gmail": NotificationCategory.EMAIL,
    "com.readdle.smartemail-Mac": NotificationCategory.EMAIL,

    # Calendar
    "com.apple.iCal": NotificationCategory.CALENDAR,
    "com.microsoft.Outlook.Calendar": NotificationCategory.CALENDAR,
    "com.google.Chrome.app.calendar": NotificationCategory.CALENDAR,

    # Reminders
    "com.apple.reminders": NotificationCategory.REMINDER,
    "com.culturedcode.ThingsMac": NotificationCategory.REMINDER,
    "com.todoist.mac.Todoist": NotificationCategory.REMINDER,

    # Calls
    "com.apple.FaceTime": NotificationCategory.CALL,
    "us.zoom.xos": NotificationCategory.MEETING,

    # IDE
    "com.microsoft.VSCode": NotificationCategory.IDE,
    "com.apple.dt.Xcode": NotificationCategory.IDE,
    "com.jetbrains.intellij": NotificationCategory.IDE,

    # System
    "com.apple.systempreferences": NotificationCategory.SYSTEM,
    "com.apple.finder": NotificationCategory.SYSTEM,
    "com.apple.AppStore": NotificationCategory.SYSTEM,
}

# Keywords for urgency detection
URGENCY_KEYWORDS = {
    "high": [
        "urgent", "asap", "immediately", "critical", "emergency",
        "deadline", "overdue", "failing", "error", "alert",
        "important", "action required", "security",
    ],
    "low": [
        "newsletter", "weekly", "digest", "promotional", "sale",
        "offer", "subscribe", "unsubscribe", "advertisement",
    ],
}


# =============================================================================
# Notification Monitor Configuration
# =============================================================================

@dataclass
class NotificationMonitorConfig:
    """Configuration for the notification monitor."""
    # Polling
    poll_interval_seconds: float = 2.0  # How often to check for new notifications

    # Filtering
    muted_apps: Set[str] = field(default_factory=set)  # Bundle IDs to ignore
    muted_categories: Set[NotificationCategory] = field(default_factory=set)

    # Narration
    enable_voice_narration: bool = True
    narrate_high_priority_only: bool = False
    narration_cooldown_seconds: float = 30.0  # Don't narrate same app twice in this period

    # Summarization
    enable_summarization: bool = True
    summary_interval_minutes: int = 30  # Generate summary every N minutes

    # Learning
    enable_learning: bool = True
    learning_db_path: str = ""

    # Calendar integration
    enable_calendar_integration: bool = True
    meeting_reminder_minutes: List[int] = field(default_factory=lambda: [15, 5, 1])


# =============================================================================
# Notification Monitor
# =============================================================================

class NotificationMonitor:
    """
    Monitors macOS notifications post-delivery.

    Apple Compliance:
    - ONLY reads notifications after they appear
    - Cannot intercept or modify notifications
    - Uses UserNotifications framework indirectly

    Implementation:
    - Polls notification database (sqlite3 in ~/Library/...)
    - Parses Notification Center state
    - Falls back to AppleScript queries
    """

    def __init__(self, config: Optional[NotificationMonitorConfig] = None):
        """
        Initialize the notification monitor.

        Args:
            config: Monitor configuration
        """
        self.config = config or NotificationMonitorConfig()

        # State
        self._running = False
        self._tasks: List[asyncio.Task] = []

        # Notification tracking
        self._seen_notifications: Dict[str, datetime] = {}  # id -> timestamp
        self._recent_notifications: List[ParsedNotification] = []
        self._max_recent = 100

        # Narration tracking
        self._last_narration: Dict[str, datetime] = {}  # app_name -> timestamp

        # Event bus (lazy loaded)
        self._event_bus = None

        # Learning database (lazy loaded)
        self._learning_db = None

        # Resolved `osascript` path, or None where it does not exist. Looked up
        # once in `start()` (off the loop), never per poll.
        self._osascript: Optional[str] = None

        logger.info("NotificationMonitor initialized")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start the notification monitor."""
        if self._running:
            logger.warning("NotificationMonitor already running")
            return

        self._running = True

        # Initialize event bus
        try:
            from .event_bus import get_macos_event_bus
            self._event_bus = await get_macos_event_bus()
        except Exception as e:
            logger.error(f"Failed to initialize event bus: {e}")

        # Initialize learning database if enabled
        if self.config.enable_learning and self.config.learning_db_path:
            await self._init_learning_db()

        # Notification Center is read through `osascript`. Resolve it ONCE,
        # off the loop. On a host without it (Windows) the poll spawned a
        # process every `poll_interval_seconds` only to fail: asyncio's
        # subprocess transport runs CreateProcess synchronously on the loop
        # thread, and StallSampler dumps 4/8 and 7/8 (2026-10-15 08:57:31,
        # 09:09:01) caught the loop wedged 0.55s / 0.50s inside it — for a
        # call that can never return a notification there.
        try:
            from backend.core.async_offload import call_off_loop
            self._osascript = await call_off_loop(shutil.which, "osascript")
        except Exception:  # noqa: BLE001 — fail-open to inline
            self._osascript = shutil.which("osascript")

        # Start monitoring tasks
        if self._osascript:
            self._tasks.append(
                asyncio.create_task(
                    self._notification_polling_loop(),
                    name="notification_poll"
                )
            )
        else:
            logger.info(
                "NotificationMonitor: osascript not available on this host — "
                "Notification Center polling not started")

        if self.config.enable_summarization:
            self._tasks.append(
                asyncio.create_task(
                    self._summarization_loop(),
                    name="notification_summary"
                )
            )

        if self.config.enable_calendar_integration:
            self._tasks.append(
                asyncio.create_task(
                    self._calendar_monitoring_loop(),
                    name="calendar_monitor"
                )
            )

        logger.info("NotificationMonitor started")

    async def stop(self) -> None:
        """Stop the notification monitor."""
        if not self._running:
            return

        self._running = False

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()
        logger.info("NotificationMonitor stopped")

    async def _init_learning_db(self) -> None:
        """Initialize learning database connection."""
        try:
            from intelligence.learning_database import JARVISLearningDatabase
            self._learning_db = JARVISLearningDatabase(
                db_path=self.config.learning_db_path
            )
            await self._learning_db.initialize()
        except Exception as e:
            logger.warning(f"Could not initialize learning DB: {e}")
            self._learning_db = None

    # =========================================================================
    # Notification Polling
    # =========================================================================

    async def _notification_polling_loop(self) -> None:
        """Poll for new notifications."""
        while self._running:
            try:
                await asyncio.sleep(self.config.poll_interval_seconds)
                await self._check_notifications()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Notification polling error: {e}")
                await asyncio.sleep(5)

    async def _check_notifications(self) -> None:
        """Check for new notifications."""
        try:
            # Method 1: Try to read from Notification Center via AppleScript
            notifications = await self._get_notification_center_items()

            # Process each notification
            for notif in notifications:
                notif_id = self._generate_notification_id(notif)

                # Skip already seen
                if notif_id in self._seen_notifications:
                    continue

                # Parse and categorize
                parsed = self._parse_notification(notif)
                parsed.notification_id = notif_id

                # Store as seen
                self._seen_notifications[notif_id] = datetime.now()

                # Add to recent list
                self._recent_notifications.append(parsed)
                if len(self._recent_notifications) > self._max_recent:
                    self._recent_notifications = self._recent_notifications[-self._max_recent:]

                # Check if should be processed
                if self._should_process_notification(parsed):
                    await self._process_notification(parsed)

            # Clean up old seen notifications (keep last hour)
            cutoff = datetime.now() - timedelta(hours=1)
            self._seen_notifications = {
                k: v for k, v in self._seen_notifications.items()
                if v > cutoff
            }

        except Exception as e:
            logger.debug(f"Error checking notifications: {e}")

    async def _get_notification_center_items(self) -> List[Dict[str, Any]]:
        """Get notification center items via AppleScript."""
        if not self._osascript:
            return []           # no AppleScript host: nothing to read, no spawn
        try:
            # Note: This requires Accessibility permission
            script = """
            tell application "System Events"
                try
                    tell process "NotificationCenter"
                        set notifList to {}
                        repeat with w in windows
                            try
                                set notifInfo to {name:name of w}
                                set end of notifList to notifInfo
                            end try
                        end repeat
                        return notifList
                    end tell
                on error
                    return {}
                end try
            end tell
            """
            result = await asyncio.create_subprocess_exec(
                self._osascript, "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                return []

            # Parse output - this is a simplified approach
            # Real implementation would need more sophisticated parsing
            output = stdout.decode().strip()
            notifications = []

            # For now, return empty - actual implementation would parse
            # the AppleScript output format

            return notifications

        except Exception as e:
            logger.debug(f"Error getting notification center items: {e}")
            return []

    def _generate_notification_id(self, notif: Dict[str, Any]) -> str:
        """Generate a unique ID for a notification."""
        data = f"{notif.get('app', '')}{notif.get('title', '')}{notif.get('body', '')}"
        return hashlib.md5(data.encode()).hexdigest()[:16]

    def _parse_notification(self, notif: Dict[str, Any]) -> ParsedNotification:
        """Parse and categorize a notification."""
        title = notif.get("title", "")
        body = notif.get("body", "")
        app_name = notif.get("app", "")
        bundle_id = notif.get("bundle_id", "")

        # Determine category
        category = self._categorize_notification(bundle_id, title, body)

        # Determine priority
        priority = self._determine_priority(category, title, body)

        # Extract sender (for messages)
        sender = self._extract_sender(title, body, category)

        # Calculate urgency score
        urgency = self._calculate_urgency(title, body)

        return ParsedNotification(
            notification_id="",
            title=title,
            body=body,
            app_name=app_name,
            bundle_id=bundle_id,
            category=category,
            priority=priority,
            sender=sender,
            urgency_score=urgency,
            actionable=self._is_actionable(category, title, body),
        )

    def _categorize_notification(
        self,
        bundle_id: str,
        title: str,
        body: str
    ) -> NotificationCategory:
        """Categorize a notification based on app and content."""
        # Check bundle ID mapping
        if bundle_id in APP_CATEGORY_MAP:
            return APP_CATEGORY_MAP[bundle_id]

        # Content-based categorization
        combined = f"{title} {body}".lower()

        if any(kw in combined for kw in ["meeting", "calendar", "event"]):
            return NotificationCategory.CALENDAR
        if any(kw in combined for kw in ["reminder", "task", "todo"]):
            return NotificationCategory.REMINDER
        if any(kw in combined for kw in ["mail", "email"]):
            return NotificationCategory.EMAIL
        if any(kw in combined for kw in ["security", "password", "authentication"]):
            return NotificationCategory.SECURITY
        if any(kw in combined for kw in ["battery", "charging"]):
            return NotificationCategory.BATTERY
        if any(kw in combined for kw in ["build", "deploy", "pipeline"]):
            return NotificationCategory.BUILD

        return NotificationCategory.UNKNOWN

    def _determine_priority(
        self,
        category: NotificationCategory,
        title: str,
        body: str
    ) -> NotificationPriority:
        """Determine notification priority."""
        combined = f"{title} {body}".lower()

        # High priority categories
        if category in [
            NotificationCategory.SECURITY,
            NotificationCategory.CALL,
        ]:
            return NotificationPriority.CRITICAL

        if category in [
            NotificationCategory.MESSAGE,
            NotificationCategory.MEETING,
        ]:
            return NotificationPriority.HIGH

        # Keyword-based priority
        if any(kw in combined for kw in URGENCY_KEYWORDS["high"]):
            return NotificationPriority.HIGH
        if any(kw in combined for kw in URGENCY_KEYWORDS["low"]):
            return NotificationPriority.LOW

        return NotificationPriority.NORMAL

    def _extract_sender(
        self,
        title: str,
        body: str,
        category: NotificationCategory
    ) -> str:
        """Extract sender name from notification."""
        if category in [NotificationCategory.MESSAGE, NotificationCategory.EMAIL]:
            # Common patterns: "Name: message" or "Name sent..." or just title is name
            if ":" in title:
                return title.split(":")[0].strip()
            if " sent " in title:
                return title.split(" sent ")[0].strip()
            return title.strip()
        return ""

    def _calculate_urgency(self, title: str, body: str) -> float:
        """Calculate urgency score (0-1)."""
        combined = f"{title} {body}".lower()
        score = 0.5  # Base score

        # Check high urgency keywords
        for keyword in URGENCY_KEYWORDS["high"]:
            if keyword in combined:
                score += 0.1

        # Check low urgency keywords
        for keyword in URGENCY_KEYWORDS["low"]:
            if keyword in combined:
                score -= 0.1

        return max(0.0, min(1.0, score))

    def _is_actionable(
        self,
        category: NotificationCategory,
        title: str,
        body: str
    ) -> bool:
        """Determine if notification requires action."""
        combined = f"{title} {body}".lower()

        if category in [
            NotificationCategory.SECURITY,
            NotificationCategory.CALL,
            NotificationCategory.MEETING,
        ]:
            return True

        action_keywords = ["reply", "respond", "approve", "confirm", "accept", "decline"]
        return any(kw in combined for kw in action_keywords)

    def _should_process_notification(self, notif: ParsedNotification) -> bool:
        """Determine if notification should be processed."""
        # Check muted apps
        if notif.bundle_id in self.config.muted_apps:
            return False

        # Check muted categories
        if notif.category in self.config.muted_categories:
            return False

        return True

    # =========================================================================
    # Notification Processing
    # =========================================================================

    async def _process_notification(self, notif: ParsedNotification) -> None:
        """Process a notification (emit event, narrate, learn)."""
        # Emit event
        await self._emit_notification_event(notif)

        # Voice narration
        if self._should_narrate(notif):
            await self._narrate_notification(notif)

        # Learn from notification
        if self.config.enable_learning and self._learning_db:
            await self._learn_from_notification(notif)

    async def _emit_notification_event(self, notif: ParsedNotification) -> None:
        """Emit notification event to event bus."""
        if not self._event_bus:
            return

        from .event_types import MacOSEventFactory
        event = MacOSEventFactory.create_notification_received(
            title=notif.title,
            body=notif.body,
            app_name=notif.app_name,
            bundle_id=notif.bundle_id,
        )
        event.data.update({
            "category": notif.category.value,
            "priority": notif.priority.value,
            "sender": notif.sender,
            "urgency_score": notif.urgency_score,
            "actionable": notif.actionable,
        })

        await self._event_bus.emit(event)

    def _should_narrate(self, notif: ParsedNotification) -> bool:
        """Determine if notification should be narrated."""
        if not self.config.enable_voice_narration:
            return False

        if self.config.narrate_high_priority_only:
            if notif.priority not in [
                NotificationPriority.CRITICAL,
                NotificationPriority.HIGH
            ]:
                return False

        # Check cooldown
        if notif.app_name in self._last_narration:
            last_time = self._last_narration[notif.app_name]
            if (datetime.now() - last_time).seconds < self.config.narration_cooldown_seconds:
                return False

        return True

    async def _narrate_notification(self, notif: ParsedNotification) -> None:
        """Narrate notification via voice."""
        try:
            from agi_os.realtime_voice_communicator import get_voice_communicator, VoiceMode

            voice = await get_voice_communicator()
            if not voice:
                return

            # Build narration text
            if notif.category == NotificationCategory.MESSAGE:
                if notif.sender:
                    text = f"Sir, you have a message from {notif.sender}."
                else:
                    text = f"Sir, you have a new message from {notif.app_name}."
            elif notif.category == NotificationCategory.MEETING:
                text = f"Sir, {notif.title}"
            elif notif.category == NotificationCategory.SECURITY:
                text = f"Sir, security alert: {notif.title}"
            else:
                text = f"Sir, notification from {notif.app_name}: {notif.title}"

            # Determine voice mode
            if notif.priority == NotificationPriority.CRITICAL:
                mode = VoiceMode.URGENT
            elif notif.priority == NotificationPriority.HIGH:
                mode = VoiceMode.NOTIFICATION
            else:
                mode = VoiceMode.QUIET

            await voice.speak(text, mode=mode)
            notif.was_narrated = True
            self._last_narration[notif.app_name] = datetime.now()

        except Exception as e:
            logger.debug(f"Narration error: {e}")

    async def _learn_from_notification(self, notif: ParsedNotification) -> None:
        """Store notification data for learning."""
        if not self._learning_db:
            return

        try:
            await self._learning_db.store_notification_pattern(
                app_name=notif.app_name,
                category=notif.category.value,
                priority=notif.priority.value,
                was_actioned=False,  # Will be updated if user interacts
                timestamp=notif.timestamp,
            )
        except Exception as e:
            logger.debug(f"Learning error: {e}")

    # =========================================================================
    # Summarization
    # =========================================================================

    async def _summarization_loop(self) -> None:
        """Generate periodic notification summaries."""
        while self._running:
            try:
                await asyncio.sleep(self.config.summary_interval_minutes * 60)
                summary = await self.generate_summary()

                if summary.total_count > 0:
                    await self._emit_summary_event(summary)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Summarization error: {e}")

    async def generate_summary(
        self,
        minutes: int = 30
    ) -> NotificationSummary:
        """Generate a summary of recent notifications."""
        cutoff = datetime.now() - timedelta(minutes=minutes)
        recent = [
            n for n in self._recent_notifications
            if n.timestamp > cutoff
        ]

        summary = NotificationSummary(
            total_count=len(recent),
            period_start=cutoff,
            period_end=datetime.now(),
        )

        # Count by category
        for notif in recent:
            summary.by_category[notif.category] = summary.by_category.get(notif.category, 0) + 1
            summary.by_app[notif.app_name] = summary.by_app.get(notif.app_name, 0) + 1

            if notif.priority in [NotificationPriority.CRITICAL, NotificationPriority.HIGH]:
                summary.high_priority_count += 1

            if notif.category == NotificationCategory.MESSAGE:
                summary.unread_messages += 1

            if notif.category == NotificationCategory.MEETING:
                summary.upcoming_meetings += 1

        # Generate summary text
        if summary.total_count == 0:
            summary.summary_text = "No new notifications."
        else:
            parts = []
            if summary.unread_messages > 0:
                parts.append(f"{summary.unread_messages} unread messages")
            if summary.upcoming_meetings > 0:
                parts.append(f"{summary.upcoming_meetings} upcoming meetings")
            if summary.high_priority_count > 0:
                parts.append(f"{summary.high_priority_count} high priority")

            if parts:
                summary.summary_text = f"You have {summary.total_count} notifications: {', '.join(parts)}."
            else:
                summary.summary_text = f"You have {summary.total_count} notifications."

        return summary

    async def _emit_summary_event(self, summary: NotificationSummary) -> None:
        """Emit summary event."""
        if not self._event_bus:
            return

        from .event_types import MacOSEvent, MacOSEventType, MacOSEventPriority, EventCategory

        event = MacOSEvent(
            event_type=MacOSEventType.NOTIFICATION_RECEIVED,
            source="notification_monitor",
            category=EventCategory.NOTIFICATION,
            priority=MacOSEventPriority.LOW,
            requires_voice_narration=True,
            data=summary.to_dict(),
            metadata={"is_summary": True},
        )
        await self._event_bus.emit(event)

    # =========================================================================
    # Calendar Integration
    # =========================================================================

    async def _calendar_monitoring_loop(self) -> None:
        """Monitor calendar for upcoming meetings."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._check_upcoming_meetings()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Calendar monitoring error: {e}")

    async def _check_upcoming_meetings(self) -> None:
        """Check for upcoming calendar events."""
        try:
            # Get events starting in the next hour via AppleScript/EventKit
            script = """
            use framework "EventKit"
            set eventStore to current application's EKEventStore's alloc()'s init()

            -- This would need proper implementation with EventKit
            -- For now, return empty
            return ""
            """
            # Actual calendar integration would use EventKit properly
            pass

        except Exception as e:
            logger.debug(f"Calendar check error: {e}")

    # =========================================================================
    # Public API
    # =========================================================================

    def get_recent_notifications(
        self,
        count: int = 10,
        category: Optional[NotificationCategory] = None
    ) -> List[ParsedNotification]:
        """Get recent notifications, optionally filtered."""
        notifications = self._recent_notifications

        if category:
            notifications = [n for n in notifications if n.category == category]

        return notifications[-count:]

    def mute_app(self, bundle_id: str) -> None:
        """Mute notifications from an app."""
        self.config.muted_apps.add(bundle_id)

    def unmute_app(self, bundle_id: str) -> None:
        """Unmute notifications from an app."""
        self.config.muted_apps.discard(bundle_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get notification monitor statistics."""
        return {
            "running": self._running,
            "total_seen": len(self._seen_notifications),
            "recent_count": len(self._recent_notifications),
            "muted_apps": list(self.config.muted_apps),
            "muted_categories": [c.value for c in self.config.muted_categories],
        }


# =============================================================================
# Singleton Pattern
# =============================================================================

_notification_monitor: Optional[NotificationMonitor] = None


async def get_notification_monitor(
    config: Optional[NotificationMonitorConfig] = None,
    auto_start: bool = True,
) -> NotificationMonitor:
    """
    Get the global notification monitor instance.

    Args:
        config: Monitor configuration
        auto_start: Automatically start monitoring

    Returns:
        The NotificationMonitor singleton
    """
    global _notification_monitor

    if _notification_monitor is None:
        _notification_monitor = NotificationMonitor(config)

    if auto_start and not _notification_monitor._running:
        await _notification_monitor.start()

    return _notification_monitor


async def stop_notification_monitor() -> None:
    """Stop the global notification monitor."""
    global _notification_monitor

    if _notification_monitor is not None:
        await _notification_monitor.stop()
        _notification_monitor = None
