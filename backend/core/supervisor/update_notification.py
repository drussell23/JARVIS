#!/usr/bin/env python3
"""
JARVIS Update Notification Orchestrator v2.0
==============================================

Intelligent, multi-modal update notification system that provides:
- Voice (TTS) announcements via the narrator
- Frontend WebSocket broadcasts for visual notifications
- Intelligent deduplication (don't spam the user)
- User activity awareness (don't interrupt active use)
- Changelog-aware summaries for meaningful notifications
- Configurable notification preferences
- LOCAL CHANGE AWARENESS (v2.0):
  - Detects local commits and pushes
  - Intelligent restart recommendations
  - Auto-restart option with user transparency
  - MaintenanceOverlay integration

This orchestrator ensures users receive timely, non-intrusive notifications
through multiple channels while maintaining a cohesive experience.

Author: JARVIS System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional, TYPE_CHECKING

import aiohttp

from .supervisor_config import SupervisorConfig, get_supervisor_config
from .update_detector import UpdateDetector, UpdateInfo, LocalChangeInfo, ChangeType

if TYPE_CHECKING:
    from .changelog_analyzer import ChangelogAnalyzer, ChangelogSummary
    from .narrator import SupervisorNarrator

logger = logging.getLogger(__name__)


class NotificationChannel(str, Enum):
    """Notification delivery channels."""
    VOICE = "voice"           # TTS announcement
    WEBSOCKET = "websocket"   # Frontend badge/modal
    CONSOLE = "console"       # Logging only (dev mode)
    ALL = "all"               # All channels


class NotificationPriority(str, Enum):
    """Notification urgency levels."""
    LOW = "low"               # Routine updates
    MEDIUM = "medium"         # Feature updates
    HIGH = "high"             # Security updates
    CRITICAL = "critical"     # Breaking changes / urgent fixes


class UserActivityState(str, Enum):
    """User activity detection states."""
    ACTIVE = "active"         # User is actively using JARVIS
    IDLE = "idle"             # User hasn't interacted recently
    AWAY = "away"             # User is away (long idle period)
    UNKNOWN = "unknown"       # Cannot determine activity


@dataclass
class NotificationState:
    """
    Tracks notification state to prevent spam and enable smart delivery.

    Attributes:
        last_notified_hash: Hash of the last update we notified about
        last_notified_at: When we last sent a notification
        notification_count: How many times we've notified about current update
        user_dismissed: User explicitly dismissed the notification
        user_acknowledged: User acknowledged (but didn't act on) the update
        pending_response: Waiting for user response
    """
    last_notified_hash: Optional[str] = None
    last_notified_at: Optional[datetime] = None
    notification_count: int = 0
    user_dismissed: bool = False
    user_acknowledged: bool = False
    pending_response: bool = False
    last_update_info: Optional[UpdateInfo] = None
    last_changelog: Optional[Any] = None  # ChangelogSummary

    # v2.0: Local change awareness state
    last_local_change_hash: Optional[str] = None
    last_local_notified_at: Optional[datetime] = None
    pending_restart: bool = False
    auto_restart_scheduled: bool = False
    last_local_changes: Optional[LocalChangeInfo] = None


@dataclass
class NotificationConfig:
    """
    Configuration for update notifications.

    Loaded from supervisor config with sensible defaults.
    """
    # Channel settings
    voice_enabled: bool = True
    websocket_enabled: bool = True
    console_enabled: bool = True

    # Timing
    min_interval_seconds: int = 300       # Minimum time between notifications
    reminder_interval_seconds: int = 3600  # Re-notify after this long
    max_reminders: int = 3                 # Max reminder notifications

    # Priority thresholds
    security_immediate: bool = True        # Always notify immediately for security

    # User activity awareness
    interrupt_active_user: bool = False    # Don't interrupt active use
    active_timeout_seconds: int = 120      # Consider user "active" if recent activity

    # Content
    announce_changes: bool = True          # Use changelog analyzer
    max_summary_words: int = 50            # TTS summary word limit

    # Frontend integration
    backend_url: str = "http://localhost:8010"
    websocket_timeout: float = 3.0

    # v2.0: Local change awareness settings
    local_change_enabled: bool = True      # Enable local change detection
    auto_restart_enabled: bool = True      # Auto-restart when recommended
    auto_restart_delay_seconds: int = 5    # Delay before auto-restart for user awareness
    local_change_debounce_seconds: int = 30  # Debounce multiple rapid changes


@dataclass
class NotificationResult:
    """Result of a notification attempt."""
    success: bool
    channels_delivered: list[NotificationChannel] = field(default_factory=list)
    error_message: Optional[str] = None
    should_retry: bool = False


class UpdateNotificationOrchestrator:
    """
    Intelligent multi-modal update notification system.
    
    Features:
    - Parallel delivery across voice and WebSocket channels
    - Smart deduplication (same update = one notification)
    - User activity awareness (don't interrupt active usage)
    - Changelog-powered summaries for meaningful notifications
    - Priority-based delivery (security updates bypass delays)
    - Configurable reminder system
    
    Example:
        >>> orchestrator = UpdateNotificationOrchestrator(config)
        >>> result = await orchestrator.notify_update_available(update_info)
        >>> if result.success:
        ...     print(f"Notified via: {result.channels_delivered}")
    """
    
    def __init__(
        self,
        config: Optional[SupervisorConfig] = None,
        narrator: Optional["SupervisorNarrator"] = None,
        changelog_analyzer: Optional["ChangelogAnalyzer"] = None,
        update_detector: Optional[UpdateDetector] = None,
    ):
        """
        Initialize the notification orchestrator.
        
        Args:
            config: Supervisor configuration
            narrator: TTS narrator instance (lazy loaded if not provided)
            changelog_analyzer: Changelog analyzer (lazy loaded if not provided)
            update_detector: Update detector for checking (lazy loaded)
        """
        self.config = config or get_supervisor_config()
        self._narrator = narrator
        self._changelog_analyzer = changelog_analyzer
        self._update_detector = update_detector
        
        # Build notification config from supervisor config
        self._notification_config = self._build_notification_config()
        
        # State tracking
        self._state = NotificationState()
        self._lock = asyncio.Lock()
        
        # Activity tracking
        self._last_user_activity: Optional[datetime] = None
        
        # Callbacks for external listeners
        self._on_notification_sent: list[Callable[[UpdateInfo, NotificationResult], None]] = []
        self._on_user_response: list[Callable[[str, UpdateInfo], None]] = []
        
        # HTTP session for WebSocket broadcasts
        self._session: Optional[aiohttp.ClientSession] = None
        
        logger.info("🔔 Update notification orchestrator initialized")
    
    def _build_notification_config(self) -> NotificationConfig:
        """Build notification config from supervisor config."""
        # Use config from supervisor if available, otherwise defaults
        if hasattr(self.config, 'notification'):
            cfg = self.config.notification
            return NotificationConfig(
                voice_enabled=cfg.voice_enabled,
                websocket_enabled=cfg.websocket_enabled,
                console_enabled=cfg.console_enabled,
                min_interval_seconds=cfg.min_interval_seconds,
                reminder_interval_seconds=cfg.reminder_interval_seconds,
                max_reminders=cfg.max_reminders,
                security_immediate=cfg.security_immediate,
                interrupt_active_user=cfg.interrupt_active_user,
                active_timeout_seconds=cfg.active_timeout_seconds,
                announce_changes=self.config.changelog.enabled,
                max_summary_words=self.config.changelog.max_length_words,
                backend_url=cfg.backend_url,
                websocket_timeout=cfg.websocket_timeout,
            )
        
        # Fallback to defaults derived from other config sections
        return NotificationConfig(
            voice_enabled=self.config.update.announce_changes,
            websocket_enabled=True,
            console_enabled=True,
            min_interval_seconds=60,
            reminder_interval_seconds=self.config.update.check.interval_seconds * 3,
            max_reminders=3,
            security_immediate=True,
            interrupt_active_user=False,
            active_timeout_seconds=120,
            announce_changes=self.config.changelog.enabled,
            max_summary_words=self.config.changelog.max_length_words,
        )
    
    @property
    def narrator(self) -> "SupervisorNarrator":
        """Lazy-load narrator."""
        if self._narrator is None:
            from .narrator import get_narrator
            self._narrator = get_narrator()
        return self._narrator
    
    @property  
    def changelog_analyzer(self) -> "ChangelogAnalyzer":
        """Lazy-load changelog analyzer."""
        if self._changelog_analyzer is None:
            from .changelog_analyzer import ChangelogAnalyzer
            self._changelog_analyzer = ChangelogAnalyzer(self.config)
        return self._changelog_analyzer
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=self._notification_config.websocket_timeout
                )
            )
        return self._session
    
    def _compute_update_hash(self, update_info: UpdateInfo) -> str:
        """Compute a unique hash for an update to detect duplicates."""
        content = f"{update_info.remote_sha}:{update_info.commits_behind}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def _determine_priority(self, update_info: UpdateInfo, changelog: Optional[Any] = None) -> NotificationPriority:
        """Determine notification priority based on update content."""
        # Check for security updates
        if changelog:
            if getattr(changelog, 'security_changes', False):
                return NotificationPriority.CRITICAL
            if getattr(changelog, 'breaking_changes', False):
                return NotificationPriority.HIGH
        
        # Check summary for keywords
        summary_lower = (update_info.summary or "").lower()
        if any(word in summary_lower for word in ["security", "critical", "urgent", "vulnerability"]):
            return NotificationPriority.CRITICAL
        if any(word in summary_lower for word in ["breaking", "important", "major"]):
            return NotificationPriority.HIGH
        if any(word in summary_lower for word in ["feature", "new", "add"]):
            return NotificationPriority.MEDIUM
        
        return NotificationPriority.LOW
    
    def _get_user_activity_state(self) -> UserActivityState:
        """Determine current user activity state."""
        if self._last_user_activity is None:
            return UserActivityState.UNKNOWN
        
        elapsed = (datetime.now() - self._last_user_activity).total_seconds()
        
        if elapsed < self._notification_config.active_timeout_seconds:
            return UserActivityState.ACTIVE
        elif elapsed < self._notification_config.active_timeout_seconds * 10:
            return UserActivityState.IDLE
        else:
            return UserActivityState.AWAY
    
    def record_user_activity(self) -> None:
        """Record that user activity was detected (called by external monitors)."""
        self._last_user_activity = datetime.now()
    
    def _should_notify(
        self,
        update_info: UpdateInfo,
        priority: NotificationPriority,
    ) -> tuple[bool, str]:
        """
        Determine if we should send a notification.
        
        Returns:
            Tuple of (should_notify, reason)
        """
        update_hash = self._compute_update_hash(update_info)
        
        # Critical priority always notifies (security)
        if priority == NotificationPriority.CRITICAL:
            return True, "Critical priority update"
        
        # User dismissed this update
        if self._state.user_dismissed and self._state.last_notified_hash == update_hash:
            return False, "User dismissed this update"
        
        # Check if this is a new update
        is_new_update = self._state.last_notified_hash != update_hash
        if is_new_update:
            return True, "New update detected"
        
        # Check minimum interval
        if self._state.last_notified_at:
            elapsed = (datetime.now() - self._state.last_notified_at).total_seconds()
            if elapsed < self._notification_config.min_interval_seconds:
                return False, f"Rate limited (notified {elapsed:.0f}s ago)"
        
        # Check reminder limits
        if self._state.notification_count >= self._notification_config.max_reminders:
            return False, "Max reminders reached"
        
        # Check if it's time for a reminder
        if self._state.last_notified_at:
            elapsed = (datetime.now() - self._state.last_notified_at).total_seconds()
            if elapsed >= self._notification_config.reminder_interval_seconds:
                return True, "Reminder interval reached"
        
        # Check user activity
        activity = self._get_user_activity_state()
        if activity == UserActivityState.ACTIVE and not self._notification_config.interrupt_active_user:
            return False, "User is active, deferring notification"
        
        return False, "No notification needed"
    
    async def _generate_voice_announcement(
        self,
        update_info: UpdateInfo,
        priority: NotificationPriority,
        is_reminder: bool = False,
    ) -> str:
        """Generate TTS-friendly announcement text."""
        # Try to get detailed changelog summary
        if self._notification_config.announce_changes and update_info.local_sha:
            try:
                announcement = await self.changelog_analyzer.get_voice_announcement(
                    since=update_info.local_sha,
                    max_words=self._notification_config.max_summary_words,
                )
                return announcement
            except Exception as e:
                logger.debug(f"Changelog analysis failed: {e}")
        
        # Fallback to basic summary
        if is_reminder:
            return f"Reminder: A system update is still available. {update_info.summary}"
        
        if priority == NotificationPriority.CRITICAL:
            return f"Sir, an important security update is available. {update_info.summary}. I recommend installing it immediately."
        elif priority == NotificationPriority.HIGH:
            return f"Sir, a significant update is available. {update_info.summary}. Would you like me to install it?"
        else:
            return f"Sir, a system update is available. {update_info.summary}. Shall I proceed with the update?"
    
    async def _broadcast_to_frontend(
        self,
        update_info: UpdateInfo,
        priority: NotificationPriority,
        changelog: Optional[Any] = None,
    ) -> bool:
        """
        Broadcast update available event to frontend via WebSocket.
        
        Returns:
            True if broadcast successful
        """
        # Build rich payload for frontend
        payload = {
            "type": "update_available",
            "data": {
                "available": True,
                "commits_behind": update_info.commits_behind,
                "summary": update_info.summary,
                "priority": priority.value,
                "remote_sha": update_info.remote_sha[:8] if update_info.remote_sha else None,
                "local_sha": update_info.local_sha[:8] if update_info.local_sha else None,
                "checked_at": update_info.checked_at.isoformat(),
                "highlights": [],
                "security_update": priority == NotificationPriority.CRITICAL,
                "breaking_changes": priority == NotificationPriority.HIGH,
            }
        }
        
        # Add changelog highlights if available
        if changelog and hasattr(changelog, 'highlights'):
            payload["data"]["highlights"] = changelog.highlights[:3]
        
        # Try to broadcast via backend API
        endpoints = [
            f"{self._notification_config.backend_url}/api/broadcast",
            f"{self._notification_config.backend_url}/api/system/broadcast",
            f"{self._notification_config.backend_url}/ws/broadcast",
        ]
        
        session = await self._get_session()
        
        for endpoint in endpoints:
            try:
                async with session.post(
                    endpoint,
                    json={"event": "update_available", "data": payload["data"]},
                ) as response:
                    if response.status in (200, 201, 202, 204):
                        logger.info(f"📡 Broadcast update_available to frontend")
                        return True
            except aiohttp.ClientConnectorError:
                # Backend not running - continue to next endpoint
                continue
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug(f"Broadcast to {endpoint} failed: {e}")
                continue
        
        logger.debug("Could not broadcast to frontend (backend may be starting)")
        return False
    
    async def _speak_notification(
        self,
        announcement: str,
        priority: NotificationPriority,
    ) -> bool:
        """
        Speak notification via TTS.
        
        Returns:
            True if TTS successful
        """
        try:
            # For critical updates, use a more urgent tone
            wait_for_completion = priority in (NotificationPriority.CRITICAL, NotificationPriority.HIGH)
            
            await self.narrator.speak(announcement, wait=wait_for_completion)
            logger.info(f"🔊 Spoke update notification")
            return True
        except Exception as e:
            logger.warning(f"TTS notification failed: {e}")
            return False
    
    async def notify_update_available(
        self,
        update_info: UpdateInfo,
        channels: NotificationChannel = NotificationChannel.ALL,
        force: bool = False,
    ) -> NotificationResult:
        """
        Send update notification through configured channels.
        
        This is the main entry point for notifying users about updates.
        It handles:
        - Deduplication (won't spam for same update)
        - Priority assessment (security updates are urgent)
        - Multi-channel delivery (voice + WebSocket)
        - Activity awareness (optional interrupt avoidance)
        
        Args:
            update_info: Information about the available update
            channels: Which channels to use (default: all configured)
            force: Force notification even if recently notified
            
        Returns:
            NotificationResult with delivery status
        """
        async with self._lock:
            # Analyze changelog for rich notifications
            changelog = None
            if self._notification_config.announce_changes and update_info.local_sha:
                try:
                    changelog = await self.changelog_analyzer.analyze_since(
                        since=update_info.local_sha
                    )
                    self._state.last_changelog = changelog
                except Exception as e:
                    logger.debug(f"Changelog analysis failed: {e}")
            
            # Determine priority
            priority = self._determine_priority(update_info, changelog)
            
            # Check if we should notify
            if not force:
                should_notify, reason = self._should_notify(update_info, priority)
                if not should_notify:
                    logger.debug(f"Skipping notification: {reason}")
                    return NotificationResult(
                        success=False,
                        error_message=reason,
                    )
            
            # Update state
            update_hash = self._compute_update_hash(update_info)
            is_reminder = self._state.last_notified_hash == update_hash
            
            self._state.last_notified_hash = update_hash
            self._state.last_notified_at = datetime.now()
            self._state.notification_count += 1 if is_reminder else 1
            self._state.last_update_info = update_info
            self._state.user_dismissed = False
            
            # If it's a new update, reset counter
            if not is_reminder:
                self._state.notification_count = 1
            
            # Prepare notification tasks
            channels_delivered: list[NotificationChannel] = []
            tasks = []
            
            # Voice notification
            if (channels in (NotificationChannel.ALL, NotificationChannel.VOICE) 
                and self._notification_config.voice_enabled):
                announcement = await self._generate_voice_announcement(
                    update_info, priority, is_reminder
                )
                tasks.append(("voice", self._speak_notification(announcement, priority)))
            
            # WebSocket broadcast
            if (channels in (NotificationChannel.ALL, NotificationChannel.WEBSOCKET)
                and self._notification_config.websocket_enabled):
                tasks.append(("websocket", self._broadcast_to_frontend(
                    update_info, priority, changelog
                )))
            
            # Console logging (always)
            if self._notification_config.console_enabled:
                log_prefix = "🔒" if priority == NotificationPriority.CRITICAL else "📦"
                logger.info(
                    f"{log_prefix} Update notification: {update_info.commits_behind} commit(s) - "
                    f"{update_info.summary} [priority={priority.value}]"
                )
                channels_delivered.append(NotificationChannel.CONSOLE)
            
            # Execute notifications in parallel
            if tasks:
                results = await asyncio.gather(
                    *[task for _, task in tasks],
                    return_exceptions=True
                )
                
                for (channel_name, _), result in zip(tasks, results):
                    if isinstance(result, Exception):
                        logger.warning(f"{channel_name} notification failed: {result}")
                    elif result:
                        if channel_name == "voice":
                            channels_delivered.append(NotificationChannel.VOICE)
                        elif channel_name == "websocket":
                            channels_delivered.append(NotificationChannel.WEBSOCKET)
            
            # Trigger callbacks
            result = NotificationResult(
                success=len(channels_delivered) > 0,
                channels_delivered=channels_delivered,
            )
            
            for callback in self._on_notification_sent:
                try:
                    callback(update_info, result)
                except Exception as e:
                    logger.error(f"Notification callback error: {e}")
            
            return result
    
    def register_notification_callback(
        self,
        callback: Callable[[UpdateInfo, NotificationResult], None],
    ) -> None:
        """Register a callback for when notifications are sent."""
        self._on_notification_sent.append(callback)
    
    def register_response_callback(
        self,
        callback: Callable[[str, UpdateInfo], None],
    ) -> None:
        """Register a callback for user responses (accept/dismiss)."""
        self._on_user_response.append(callback)
    
    def user_dismissed(self) -> None:
        """Record that user dismissed the update notification."""
        self._state.user_dismissed = True
        self._state.pending_response = False
        logger.info("User dismissed update notification")
    
    def user_acknowledged(self) -> None:
        """Record that user acknowledged (but didn't act on) the update."""
        self._state.user_acknowledged = True
        self._state.pending_response = False
        logger.info("User acknowledged update notification")
    
    async def check_and_notify(self, force: bool = False) -> Optional[NotificationResult]:
        """
        Check for updates and notify if available.
        
        Combines update detection with notification in a single call.
        
        Args:
            force: Force check and notification even if recently done
            
        Returns:
            NotificationResult if update found, None otherwise
        """
        if not self._update_detector:
            logger.warning("No update detector configured")
            return None
        
        update_info = await self._update_detector.check_for_updates(force=force)
        
        if update_info and update_info.available:
            return await self.notify_update_available(update_info, force=force)
        
        return None
    
    def get_pending_update(self) -> Optional[UpdateInfo]:
        """Get info about pending update (if any)."""
        return self._state.last_update_info
    
    def get_notification_state(self) -> dict[str, Any]:
        """Get current notification state for debugging/UI."""
        return {
            "has_pending_update": self._state.last_update_info is not None,
            "update_hash": self._state.last_notified_hash,
            "last_notified": self._state.last_notified_at.isoformat() if self._state.last_notified_at else None,
            "notification_count": self._state.notification_count,
            "user_dismissed": self._state.user_dismissed,
            "user_acknowledged": self._state.user_acknowledged,
            "user_activity": self._get_user_activity_state().value,
            # v2.0: Local change state
            "has_local_changes": self._state.last_local_changes is not None and self._state.last_local_changes.has_changes,
            "pending_restart": self._state.pending_restart,
            "auto_restart_scheduled": self._state.auto_restart_scheduled,
        }

    # =========================================================================
    # v2.0: LOCAL CHANGE AWARENESS METHODS
    # =========================================================================

    def _compute_local_change_hash(self, info: LocalChangeInfo) -> str:
        """Compute hash for local change to detect duplicates."""
        content = f"{info.started_on_commit}:{info.commits_since_start}:{info.uncommitted_files}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def notify_local_changes(
        self,
        info: LocalChangeInfo,
        force: bool = False,
    ) -> NotificationResult:
        """
        Notify about local repository changes.

        This handles:
        - Local commits detected
        - Code pushed to remote
        - Uncommitted changes
        - Restart recommendations

        Args:
            info: LocalChangeInfo from the update detector
            force: Force notification even if recently notified

        Returns:
            NotificationResult with delivery status
        """
        if not self._notification_config.local_change_enabled:
            return NotificationResult(success=False, error_message="Local change notifications disabled")

        if not info.has_changes:
            return NotificationResult(success=False, error_message="No changes to notify")

        async with self._lock:
            # Check debounce
            change_hash = self._compute_local_change_hash(info)
            if not force and self._state.last_local_change_hash == change_hash:
                if self._state.last_local_notified_at:
                    elapsed = (datetime.now() - self._state.last_local_notified_at).total_seconds()
                    if elapsed < self._notification_config.local_change_debounce_seconds:
                        return NotificationResult(
                            success=False,
                            error_message=f"Debounced (notified {elapsed:.0f}s ago)"
                        )

            # Update state
            self._state.last_local_change_hash = change_hash
            self._state.last_local_notified_at = datetime.now()
            self._state.last_local_changes = info
            self._state.pending_restart = info.restart_recommended

            # Prepare parallel notifications
            channels_delivered: list[NotificationChannel] = []
            tasks = []

            # Build notification message
            if info.change_type == ChangeType.LOCAL_PUSH:
                event_type = "local_push_detected"
                message = f"Code pushed to remote: {info.summary}"
            elif info.change_type == ChangeType.LOCAL_COMMIT:
                event_type = "local_commit_detected"
                message = f"New commit detected: {info.summary}"
            elif info.change_type == ChangeType.UNCOMMITTED:
                event_type = "code_changes_detected"
                message = f"Uncommitted changes: {info.summary}"
            else:
                event_type = "local_changes_detected"
                message = info.summary

            # WebSocket broadcast to frontend
            if self._notification_config.websocket_enabled:
                tasks.append(("websocket", self._broadcast_local_changes_to_frontend(info, event_type)))

            # Console logging
            if self._notification_config.console_enabled:
                log_emoji = "📤" if info.change_type == ChangeType.LOCAL_PUSH else "📝"
                logger.info(f"{log_emoji} Local changes: {message}")
                if info.restart_recommended:
                    logger.info(f"🔄 Restart recommended: {info.restart_reason}")
                channels_delivered.append(NotificationChannel.CONSOLE)

            # Execute parallel notifications
            if tasks:
                results = await asyncio.gather(
                    *[task for _, task in tasks],
                    return_exceptions=True
                )

                for (channel_name, _), result in zip(tasks, results):
                    if isinstance(result, Exception):
                        logger.warning(f"{channel_name} notification failed: {result}")
                    elif result:
                        if channel_name == "websocket":
                            channels_delivered.append(NotificationChannel.WEBSOCKET)

            # Handle auto-restart if recommended
            if info.restart_recommended and self._notification_config.auto_restart_enabled:
                if not self._state.auto_restart_scheduled:
                    self._state.auto_restart_scheduled = True
                    # Schedule auto-restart with delay for user awareness
                    asyncio.create_task(self._schedule_auto_restart(info))

            return NotificationResult(
                success=len(channels_delivered) > 0,
                channels_delivered=channels_delivered,
            )

    async def _broadcast_local_changes_to_frontend(
        self,
        info: LocalChangeInfo,
        event_type: str,
    ) -> bool:
        """Broadcast local changes to frontend via WebSocket."""
        payload = {
            "type": event_type,
            "data": {
                "has_changes": info.has_changes,
                "change_type": info.change_type.value if info.change_type else None,
                "summary": info.summary,
                "commits_since_start": info.commits_since_start,
                "uncommitted_files": info.uncommitted_files,
                "modified_files": info.modified_files[:10],  # Limit for payload size
                "current_branch": info.current_branch,
                "started_on_commit": info.started_on_commit,
                "restart_recommended": info.restart_recommended,
                "restart_reason": info.restart_reason,
                "detected_at": info.detected_at.isoformat(),
            }
        }

        # Try broadcast endpoints
        endpoints = [
            f"{self._notification_config.backend_url}/api/broadcast",
            f"{self._notification_config.backend_url}/api/system/broadcast",
        ]

        session = await self._get_session()

        for endpoint in endpoints:
            try:
                async with session.post(
                    endpoint,
                    json={"event": event_type, "data": payload["data"]},
                ) as response:
                    if response.status in (200, 201, 202, 204):
                        logger.info(f"📡 Broadcast {event_type} to frontend")
                        return True
            except Exception as e:
                logger.debug(f"Broadcast to {endpoint} failed: {e}")
                continue

        return False

    async def _schedule_auto_restart(self, info: LocalChangeInfo) -> None:
        """
        Schedule an automatic restart with user-transparent countdown.

        Shows MaintenanceOverlay on frontend before restarting.
        """
        delay = self._notification_config.auto_restart_delay_seconds

        # Broadcast restart notification to frontend
        await self._broadcast_restart_notification(
            message=f"Restarting in {delay} seconds to apply code changes",
            reason=info.restart_reason or "Code changes detected",
            estimated_time=delay,
        )

        # Voice announcement
        if self._notification_config.voice_enabled:
            try:
                await self.narrator.speak(
                    f"Sir, I'm applying your code changes. Restarting in {delay} seconds.",
                    wait=False
                )
            except Exception as e:
                logger.debug(f"TTS failed: {e}")

        # Wait before restart
        logger.info(f"⏱️ Auto-restart scheduled in {delay} seconds")
        await asyncio.sleep(delay)

        # Final broadcast before restart
        await self._broadcast_restart_notification(
            message="Restarting now...",
            reason="Applying code changes",
            estimated_time=15,
        )

        # Trigger restart via supervisor exit code
        logger.info("🔄 Triggering restart to apply code changes")
        self._state.auto_restart_scheduled = False
        self._state.pending_restart = False

        # Signal restart (exit code 102 = restart request)
        # This will be caught by the supervisor which handles the actual restart
        import sys
        sys.exit(102)

    async def _broadcast_restart_notification(
        self,
        message: str,
        reason: str,
        estimated_time: int,
    ) -> bool:
        """Broadcast restart notification to trigger MaintenanceOverlay."""
        payload = {
            "message": message,
            "reason": reason,
            "estimated_time": estimated_time,
        }

        endpoints = [
            f"{self._notification_config.backend_url}/api/broadcast",
            f"{self._notification_config.backend_url}/api/system/broadcast",
        ]

        session = await self._get_session()

        for endpoint in endpoints:
            try:
                async with session.post(
                    endpoint,
                    json={"event": "system_restarting", "data": payload},
                ) as response:
                    if response.status in (200, 201, 202, 204):
                        logger.info("📡 Broadcast system_restarting to frontend")
                        return True
            except Exception as e:
                logger.debug(f"Broadcast to {endpoint} failed: {e}")
                continue

        return False

    def cancel_auto_restart(self) -> None:
        """Cancel a scheduled auto-restart."""
        if self._state.auto_restart_scheduled:
            self._state.auto_restart_scheduled = False
            logger.info("🚫 Auto-restart cancelled by user")

    def get_local_change_state(self) -> dict[str, Any]:
        """Get current local change state for debugging/UI."""
        info = self._state.last_local_changes
        return {
            "has_local_changes": info is not None and info.has_changes if info else False,
            "change_type": info.change_type.value if info and info.change_type else None,
            "summary": info.summary if info else None,
            "restart_recommended": info.restart_recommended if info else False,
            "restart_reason": info.restart_reason if info else None,
            "pending_restart": self._state.pending_restart,
            "auto_restart_scheduled": self._state.auto_restart_scheduled,
        }

    async def close(self) -> None:
        """Clean up resources."""
        if self._session and not self._session.closed:
            await self._session.close()


# Module-level singleton
_notification_orchestrator: Optional[UpdateNotificationOrchestrator] = None


def get_notification_orchestrator(
    config: Optional[SupervisorConfig] = None,
) -> UpdateNotificationOrchestrator:
    """Get or create the notification orchestrator singleton."""
    global _notification_orchestrator
    if _notification_orchestrator is None:
        _notification_orchestrator = UpdateNotificationOrchestrator(config)
    return _notification_orchestrator

