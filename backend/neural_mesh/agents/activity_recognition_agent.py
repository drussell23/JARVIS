"""
JARVIS Neural Mesh - Activity Recognition Agent (v2.7)

Advanced user activity recognition and context understanding system.
Monitors user behavior patterns, application usage, and work context
to provide intelligent context-aware assistance.

This agent was documented but DORMANT (part of the 27% inactive agents).
v2.7: Now fully implemented and activated in the Neural Mesh.

Key Capabilities:
1. Activity Classification - Identify current user activity type
2. Application Context - Track application usage patterns
3. Work Session Detection - Identify work vs leisure sessions
4. Focus State Recognition - Detect deep work vs multitasking
5. Activity Transitions - Detect when user switches activities
6. Behavioral Patterns - Learn user's typical activity flows
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import AgentMessage, KnowledgeType, MessageType

logger = logging.getLogger(__name__)


class ActivityType(Enum):
    """Types of user activities."""
    CODING = "coding"
    WRITING = "writing"
    BROWSING = "browsing"
    COMMUNICATION = "communication"
    MEETING = "meeting"
    MEDIA = "media"
    DESIGN = "design"
    RESEARCH = "research"
    ADMINISTRATIVE = "administrative"
    GAMING = "gaming"
    IDLE = "idle"
    UNKNOWN = "unknown"


class FocusState(Enum):
    """User focus states."""
    DEEP_WORK = "deep_work"  # Single task, minimal switching
    FOCUSED = "focused"  # Moderate concentration
    MULTITASKING = "multitasking"  # Frequent task switching
    DISTRACTED = "distracted"  # High switching, no clear focus
    IDLE = "idle"  # No activity detected
    BREAK = "break"  # Intentional rest period
    UNKNOWN = "unknown"  # v93.0: Unknown/uninitialized state


class SessionType(Enum):
    """Work session types."""
    WORK = "work"
    CREATIVE = "creative"
    COMMUNICATION = "communication"
    LEARNING = "learning"
    LEISURE = "leisure"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


@dataclass
class DetectedActivity:
    """Represents a detected user activity."""
    activity_id: str
    activity_type: ActivityType
    confidence: float
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    applications: List[str] = field(default_factory=list)
    context_signals: Dict[str, Any] = field(default_factory=dict)
    focus_state: FocusState = FocusState.UNKNOWN


@dataclass
class ActivityTransition:
    """Represents a transition between activities."""
    from_activity: ActivityType
    to_activity: ActivityType
    timestamp: datetime
    trigger_signal: str
    confidence: float


@dataclass
class UserSession:
    """Represents a user work session."""
    session_id: str
    session_type: SessionType
    started_at: datetime
    ended_at: Optional[datetime] = None
    activities: List[str] = field(default_factory=list)
    total_focus_time: float = 0.0
    total_distracted_time: float = 0.0
    app_usage: Dict[str, float] = field(default_factory=dict)


# Application to activity mapping
APP_ACTIVITY_MAPPING: Dict[str, ActivityType] = {
    # Coding
    "visual studio code": ActivityType.CODING,
    "vscode": ActivityType.CODING,
    "pycharm": ActivityType.CODING,
    "xcode": ActivityType.CODING,
    "intellij": ActivityType.CODING,
    "sublime text": ActivityType.CODING,
    "terminal": ActivityType.CODING,
    "iterm": ActivityType.CODING,
    "cursor": ActivityType.CODING,
    # Writing
    "word": ActivityType.WRITING,
    "pages": ActivityType.WRITING,
    "google docs": ActivityType.WRITING,
    "notes": ActivityType.WRITING,
    "notion": ActivityType.WRITING,
    "obsidian": ActivityType.WRITING,
    "bear": ActivityType.WRITING,
    # Browsing
    "safari": ActivityType.BROWSING,
    "chrome": ActivityType.BROWSING,
    "firefox": ActivityType.BROWSING,
    "arc": ActivityType.BROWSING,
    "brave": ActivityType.BROWSING,
    # Communication
    "slack": ActivityType.COMMUNICATION,
    "discord": ActivityType.COMMUNICATION,
    "messages": ActivityType.COMMUNICATION,
    "mail": ActivityType.COMMUNICATION,
    "outlook": ActivityType.COMMUNICATION,
    "teams": ActivityType.COMMUNICATION,
    # Meetings
    "zoom": ActivityType.MEETING,
    "meet": ActivityType.MEETING,
    "facetime": ActivityType.MEETING,
    "webex": ActivityType.MEETING,
    # Design
    "figma": ActivityType.DESIGN,
    "sketch": ActivityType.DESIGN,
    "photoshop": ActivityType.DESIGN,
    "illustrator": ActivityType.DESIGN,
    "canva": ActivityType.DESIGN,
    # Media
    "spotify": ActivityType.MEDIA,
    "music": ActivityType.MEDIA,
    "vlc": ActivityType.MEDIA,
    "youtube": ActivityType.MEDIA,
    "netflix": ActivityType.MEDIA,
    # Research
    "arxiv": ActivityType.RESEARCH,
    "scholar": ActivityType.RESEARCH,
    "zotero": ActivityType.RESEARCH,
    "mendeley": ActivityType.RESEARCH,
    # Administrative
    "calendar": ActivityType.ADMINISTRATIVE,
    "reminders": ActivityType.ADMINISTRATIVE,
    "finder": ActivityType.ADMINISTRATIVE,
    "system preferences": ActivityType.ADMINISTRATIVE,
}


class ActivityRecognitionAgent(BaseNeuralMeshAgent):
    """
    Activity Recognition Agent - User activity and context understanding.

    v2.7: Previously dormant agent now activated for the Neural Mesh.

    Capabilities:
    - recognize_activity: Identify current user activity
    - get_focus_state: Determine user's focus level
    - track_session: Track work session progress
    - detect_transitions: Identify activity transitions
    - get_activity_history: Retrieve activity history
    - predict_next_activity: Anticipate likely next activity
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="ActivityRecognitionAgent",
            agent_type="intelligence",
            capabilities={
                "recognize_activity",
                "get_focus_state",
                "track_session",
                "detect_transitions",
                "get_activity_history",
                "predict_next_activity",
                "analyze_productivity",
                "get_app_context",
            },
            description="Recognizes user activities, tracks focus states, and provides context-aware intelligence",
        )

        # Activity tracking
        self._current_activity: Optional[DetectedActivity] = None
        self._activity_history: deque[DetectedActivity] = deque(
            maxlen=int(os.getenv("ACTIVITY_HISTORY_SIZE", "500"))
        )
        self._transitions: deque[ActivityTransition] = deque(
            maxlen=int(os.getenv("ACTIVITY_TRANSITIONS_SIZE", "200"))
        )

        # Session tracking
        self._current_session: Optional[UserSession] = None
        self._session_history: deque[UserSession] = deque(
            maxlen=int(os.getenv("SESSION_HISTORY_SIZE", "50"))
        )

        # App usage tracking
        self._app_usage_today: Dict[str, float] = defaultdict(float)
        self._app_switch_count: int = 0
        self._last_app: Optional[str] = None
        self._last_app_change: Optional[datetime] = None

        # Focus metrics
        self._focus_window_seconds = float(
            os.getenv("FOCUS_WINDOW_SECONDS", "300")  # 5 min
        )
        self._recent_switches: deque[datetime] = deque(
            maxlen=int(os.getenv("FOCUS_SWITCH_TRACKING", "50"))
        )

        # Pattern learning
        self._activity_patterns: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )  # activity -> {next_activity: count}
        self._time_patterns: Dict[int, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )  # hour -> {activity: count}

        # Configurable thresholds
        self._deep_work_threshold = float(
            os.getenv("DEEP_WORK_THRESHOLD", "0.1")  # switches per minute
        )
        self._distracted_threshold = float(
            os.getenv("DISTRACTED_THRESHOLD", "1.0")  # switches per minute
        )
        self._min_activity_duration = float(
            os.getenv("MIN_ACTIVITY_DURATION", "5.0")  # seconds
        )

        logger.info(
            f"[ACTIVITY-RECOGNITION] Initialized with "
            f"focus_window={self._focus_window_seconds}s, "
            f"deep_work_threshold={self._deep_work_threshold}/min"
        )

    async def handle_message(self, message: AgentMessage) -> Optional[Dict[str, Any]]:
        """Handle incoming messages requesting activity recognition."""
        try:
            content = message.content
            action = content.get("action", "")

            if action == "recognize_activity":
                return await self._recognize_activity(content)
            elif action == "get_focus_state":
                return await self._get_focus_state()
            elif action == "track_app_switch":
                return await self._track_app_switch(content)
            elif action == "start_session":
                return await self._start_session(content)
            elif action == "end_session":
                return await self._end_session()
            elif action == "get_activity_history":
                return await self._get_activity_history(content)
            elif action == "get_session_stats":
                return await self._get_session_stats()
            elif action == "predict_next_activity":
                return await self._predict_next_activity()
            elif action == "analyze_productivity":
                return await self._analyze_productivity(content)
            elif action == "get_current_context":
                return await self._get_current_context()
            else:
                return {"error": f"Unknown action: {action}"}

        except Exception as e:
            logger.exception(f"[ACTIVITY-RECOGNITION] Error handling message: {e}")
            return {"error": str(e)}

    async def _recognize_activity(
        self, content: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Recognize the current user activity from context signals.

        Analyzes multiple signals:
        - Active application
        - Window title
        - Recent interactions
        - Time of day patterns
        """
        app_name = content.get("app_name", "").lower()
        window_title = content.get("window_title", "")
        timestamp = datetime.fromisoformat(
            content.get("timestamp", datetime.now().isoformat())
        )

        # Determine activity type from app
        activity_type = self._classify_activity(app_name, window_title)

        # Calculate confidence based on multiple signals
        confidence = self._calculate_activity_confidence(
            app_name, window_title, activity_type
        )

        # Get current focus state
        focus_state = await self._calculate_focus_state()

        # Create activity record
        activity_id = hashlib.sha256(
            f"{activity_type.value}:{timestamp.isoformat()}:{app_name}".encode()
        ).hexdigest()[:16]

        # Handle activity transition
        if self._current_activity:
            if self._current_activity.activity_type != activity_type:
                # Record transition
                transition = ActivityTransition(
                    from_activity=self._current_activity.activity_type,
                    to_activity=activity_type,
                    timestamp=timestamp,
                    trigger_signal=f"app:{app_name}",
                    confidence=confidence,
                )
                self._transitions.append(transition)

                # Update pattern learning
                from_key = self._current_activity.activity_type.value
                to_key = activity_type.value
                self._activity_patterns[from_key][to_key] += 1

                # End previous activity
                self._current_activity.ended_at = timestamp
                self._current_activity.duration_seconds = (
                    timestamp - self._current_activity.started_at
                ).total_seconds()

                if self._current_activity.duration_seconds >= self._min_activity_duration:
                    self._activity_history.append(self._current_activity)

                # Start new activity
                self._current_activity = DetectedActivity(
                    activity_id=activity_id,
                    activity_type=activity_type,
                    confidence=confidence,
                    started_at=timestamp,
                    applications=[app_name],
                    focus_state=focus_state,
                )
            else:
                # Same activity, update
                if app_name not in self._current_activity.applications:
                    self._current_activity.applications.append(app_name)
                self._current_activity.focus_state = focus_state
        else:
            # First activity
            self._current_activity = DetectedActivity(
                activity_id=activity_id,
                activity_type=activity_type,
                confidence=confidence,
                started_at=timestamp,
                applications=[app_name],
                focus_state=focus_state,
            )

        # Update time-of-day patterns
        hour = timestamp.hour
        self._time_patterns[hour][activity_type.value] += 1

        # Track app switch
        if self._last_app and self._last_app != app_name:
            self._app_switch_count += 1
            self._recent_switches.append(timestamp)

        self._last_app = app_name
        self._last_app_change = timestamp

        # Update app usage
        if self._last_app_change:
            # Record time spent in previous app
            pass  # Handled in session tracking

        return {
            "activity_id": activity_id,
            "activity_type": activity_type.value,
            "confidence": confidence,
            "focus_state": focus_state.value,
            "duration_so_far": (
                datetime.now() - self._current_activity.started_at
            ).total_seconds() if self._current_activity else 0,
            "recent_transitions": len([
                t for t in self._transitions
                if (timestamp - t.timestamp).total_seconds() < 300
            ]),
        }

    def _classify_activity(
        self, app_name: str, window_title: str
    ) -> ActivityType:
        """Classify activity from app name and window title."""
        app_lower = app_name.lower()

        # Direct app mapping
        for app_key, activity in APP_ACTIVITY_MAPPING.items():
            if app_key in app_lower:
                return activity

        # Window title analysis for browsers
        if any(browser in app_lower for browser in ["safari", "chrome", "firefox", "arc"]):
            title_lower = window_title.lower()
            if any(kw in title_lower for kw in ["github", "stackoverflow", "docs"]):
                return ActivityType.CODING
            if any(kw in title_lower for kw in ["youtube", "netflix", "twitch"]):
                return ActivityType.MEDIA
            if any(kw in title_lower for kw in ["mail", "gmail", "inbox"]):
                return ActivityType.COMMUNICATION
            if any(kw in title_lower for kw in ["scholar", "arxiv", "paper"]):
                return ActivityType.RESEARCH

        return ActivityType.UNKNOWN

    def _calculate_activity_confidence(
        self, app_name: str, window_title: str, activity_type: ActivityType
    ) -> float:
        """Calculate confidence score for activity classification."""
        confidence = 0.5  # Base confidence

        # App name match
        app_lower = app_name.lower()
        for app_key, activity in APP_ACTIVITY_MAPPING.items():
            if app_key in app_lower and activity == activity_type:
                confidence += 0.3
                break

        # Window title context
        if window_title:
            confidence += 0.1

        # Historical pattern match
        hour = datetime.now().hour
        if activity_type.value in self._time_patterns[hour]:
            pattern_count = self._time_patterns[hour][activity_type.value]
            total = sum(self._time_patterns[hour].values())
            if total > 0:
                pattern_match = pattern_count / total
                confidence += pattern_match * 0.1

        return min(confidence, 1.0)

    async def _calculate_focus_state(self) -> FocusState:
        """Calculate current focus state from switch frequency."""
        if not self._recent_switches:
            return FocusState.IDLE

        now = datetime.now()
        window_start = now - timedelta(seconds=self._focus_window_seconds)

        # Count switches in window
        recent = [s for s in self._recent_switches if s > window_start]
        switches_per_minute = (
            len(recent) / (self._focus_window_seconds / 60)
            if self._focus_window_seconds > 0 else 0
        )

        if switches_per_minute <= self._deep_work_threshold:
            return FocusState.DEEP_WORK
        elif switches_per_minute <= self._deep_work_threshold * 3:
            return FocusState.FOCUSED
        elif switches_per_minute <= self._distracted_threshold:
            return FocusState.MULTITASKING
        else:
            return FocusState.DISTRACTED

    async def _get_focus_state(self) -> Dict[str, Any]:
        """Get current focus state with details."""
        focus_state = await self._calculate_focus_state()

        now = datetime.now()
        window_start = now - timedelta(seconds=self._focus_window_seconds)
        recent_switches = [s for s in self._recent_switches if s > window_start]

        return {
            "focus_state": focus_state.value,
            "switches_in_window": len(recent_switches),
            "window_seconds": self._focus_window_seconds,
            "switches_per_minute": (
                len(recent_switches) / (self._focus_window_seconds / 60)
                if self._focus_window_seconds > 0 else 0
            ),
            "current_activity": (
                self._current_activity.activity_type.value
                if self._current_activity else None
            ),
            "activity_duration": (
                (now - self._current_activity.started_at).total_seconds()
                if self._current_activity else 0
            ),
        }

    async def _track_app_switch(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """Track an application switch event."""
        from_app = content.get("from_app", "")
        to_app = content.get("to_app", "")
        timestamp = datetime.fromisoformat(
            content.get("timestamp", datetime.now().isoformat())
        )

        # Update switch tracking
        self._app_switch_count += 1
        self._recent_switches.append(timestamp)

        # Track time in previous app
        if self._last_app_change and from_app:
            duration = (timestamp - self._last_app_change).total_seconds()
            self._app_usage_today[from_app] += duration

            # Update session app usage
            if self._current_session:
                self._current_session.app_usage[from_app] = (
                    self._current_session.app_usage.get(from_app, 0) + duration
                )

        self._last_app = to_app
        self._last_app_change = timestamp

        # Recognize new activity
        return await self._recognize_activity({
            "app_name": to_app,
            "window_title": content.get("window_title", ""),
            "timestamp": timestamp.isoformat(),
        })

    async def _start_session(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """Start a new work session."""
        session_type_str = content.get("session_type", "unknown")
        try:
            session_type = SessionType(session_type_str)
        except ValueError:
            session_type = SessionType.UNKNOWN

        session_id = hashlib.sha256(
            f"{datetime.now().isoformat()}:{session_type.value}".encode()
        ).hexdigest()[:16]

        self._current_session = UserSession(
            session_id=session_id,
            session_type=session_type,
            started_at=datetime.now(),
        )

        logger.info(f"[ACTIVITY-RECOGNITION] Started session: {session_id} ({session_type.value})")

        return {
            "session_id": session_id,
            "session_type": session_type.value,
            "started_at": self._current_session.started_at.isoformat(),
        }

    async def _end_session(self) -> Dict[str, Any]:
        """End the current work session."""
        if not self._current_session:
            return {"error": "No active session"}

        self._current_session.ended_at = datetime.now()

        # Calculate session stats
        duration = (
            self._current_session.ended_at - self._current_session.started_at
        ).total_seconds()

        # Analyze focus time
        focus_activities = [
            a for a in self._activity_history
            if a.started_at >= self._current_session.started_at
            and a.focus_state in [FocusState.DEEP_WORK, FocusState.FOCUSED]
        ]
        self._current_session.total_focus_time = sum(
            a.duration_seconds for a in focus_activities
        )

        distracted_activities = [
            a for a in self._activity_history
            if a.started_at >= self._current_session.started_at
            and a.focus_state == FocusState.DISTRACTED
        ]
        self._current_session.total_distracted_time = sum(
            a.duration_seconds for a in distracted_activities
        )

        # Store session
        self._session_history.append(self._current_session)

        result = {
            "session_id": self._current_session.session_id,
            "session_type": self._current_session.session_type.value,
            "duration_seconds": duration,
            "focus_time": self._current_session.total_focus_time,
            "distracted_time": self._current_session.total_distracted_time,
            "focus_percentage": (
                self._current_session.total_focus_time / duration * 100
                if duration > 0 else 0
            ),
            "app_usage": dict(self._current_session.app_usage),
        }

        logger.info(
            f"[ACTIVITY-RECOGNITION] Ended session: {self._current_session.session_id} "
            f"(duration: {duration:.0f}s, focus: {result['focus_percentage']:.1f}%)"
        )

        self._current_session = None
        return result

    async def _get_activity_history(
        self, content: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Get activity history."""
        limit = content.get("limit", 50)
        since = content.get("since")

        activities = list(self._activity_history)

        if since:
            since_dt = datetime.fromisoformat(since)
            activities = [a for a in activities if a.started_at >= since_dt]

        activities = activities[-limit:]

        return {
            "activities": [
                {
                    "activity_id": a.activity_id,
                    "activity_type": a.activity_type.value,
                    "confidence": a.confidence,
                    "started_at": a.started_at.isoformat(),
                    "ended_at": a.ended_at.isoformat() if a.ended_at else None,
                    "duration_seconds": a.duration_seconds,
                    "applications": a.applications,
                    "focus_state": a.focus_state.value,
                }
                for a in activities
            ],
            "total_count": len(self._activity_history),
            "returned_count": len(activities),
        }

    async def _get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics."""
        if not self._session_history:
            return {
                "total_sessions": 0,
                "current_session": None,
            }

        sessions = list(self._session_history)
        total_duration = sum(
            (s.ended_at - s.started_at).total_seconds()
            for s in sessions if s.ended_at
        )
        total_focus = sum(s.total_focus_time for s in sessions)

        current = None
        if self._current_session:
            current = {
                "session_id": self._current_session.session_id,
                "session_type": self._current_session.session_type.value,
                "started_at": self._current_session.started_at.isoformat(),
                "duration_so_far": (
                    datetime.now() - self._current_session.started_at
                ).total_seconds(),
            }

        return {
            "total_sessions": len(sessions),
            "total_duration_seconds": total_duration,
            "total_focus_time": total_focus,
            "average_focus_percentage": (
                total_focus / total_duration * 100
                if total_duration > 0 else 0
            ),
            "current_session": current,
            "session_types": {
                st.value: len([s for s in sessions if s.session_type == st])
                for st in SessionType
            },
        }

    async def _predict_next_activity(self) -> Dict[str, Any]:
        """Predict the most likely next activity."""
        predictions = []

        # Pattern-based prediction
        if self._current_activity:
            current_key = self._current_activity.activity_type.value
            if current_key in self._activity_patterns:
                transitions = self._activity_patterns[current_key]
                total = sum(transitions.values())
                if total > 0:
                    for activity, count in sorted(
                        transitions.items(), key=lambda x: -x[1]
                    )[:3]:
                        predictions.append({
                            "activity": activity,
                            "probability": count / total,
                            "source": "transition_pattern",
                        })

        # Time-based prediction
        hour = datetime.now().hour
        if hour in self._time_patterns:
            time_activities = self._time_patterns[hour]
            total = sum(time_activities.values())
            if total > 0:
                top_activity = max(time_activities.items(), key=lambda x: x[1])
                predictions.append({
                    "activity": top_activity[0],
                    "probability": top_activity[1] / total,
                    "source": "time_pattern",
                })

        # Merge predictions
        merged: Dict[str, float] = defaultdict(float)
        for pred in predictions:
            merged[pred["activity"]] += pred["probability"] * 0.5

        sorted_predictions = sorted(merged.items(), key=lambda x: -x[1])

        return {
            "predictions": [
                {"activity": act, "probability": prob}
                for act, prob in sorted_predictions[:3]
            ],
            "current_activity": (
                self._current_activity.activity_type.value
                if self._current_activity else None
            ),
            "confidence": max(merged.values()) if merged else 0,
        }

    async def _analyze_productivity(
        self, content: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Analyze productivity metrics."""
        period_hours = content.get("period_hours", 24)
        cutoff = datetime.now() - timedelta(hours=period_hours)

        # Filter activities in period
        activities = [
            a for a in self._activity_history
            if a.started_at >= cutoff
        ]

        # Calculate metrics
        productive_types = {
            ActivityType.CODING,
            ActivityType.WRITING,
            ActivityType.RESEARCH,
            ActivityType.DESIGN,
        }

        productive_time = sum(
            a.duration_seconds for a in activities
            if a.activity_type in productive_types
        )
        total_time = sum(a.duration_seconds for a in activities)

        focus_time = sum(
            a.duration_seconds for a in activities
            if a.focus_state in [FocusState.DEEP_WORK, FocusState.FOCUSED]
        )

        # Activity breakdown
        activity_breakdown: Dict[str, float] = defaultdict(float)
        for a in activities:
            activity_breakdown[a.activity_type.value] += a.duration_seconds

        return {
            "period_hours": period_hours,
            "total_tracked_time": total_time,
            "productive_time": productive_time,
            "productivity_percentage": (
                productive_time / total_time * 100
                if total_time > 0 else 0
            ),
            "focus_time": focus_time,
            "focus_percentage": (
                focus_time / total_time * 100
                if total_time > 0 else 0
            ),
            "activity_breakdown": dict(activity_breakdown),
            "total_switches": self._app_switch_count,
            "activities_count": len(activities),
        }

    async def _get_current_context(self) -> Dict[str, Any]:
        """Get comprehensive current context for other agents."""
        focus_state = await self._calculate_focus_state()

        return {
            "current_activity": {
                "type": (
                    self._current_activity.activity_type.value
                    if self._current_activity else None
                ),
                "confidence": (
                    self._current_activity.confidence
                    if self._current_activity else 0
                ),
                "duration_seconds": (
                    (datetime.now() - self._current_activity.started_at).total_seconds()
                    if self._current_activity else 0
                ),
                "applications": (
                    self._current_activity.applications
                    if self._current_activity else []
                ),
            },
            "focus_state": focus_state.value,
            "session": {
                "active": self._current_session is not None,
                "type": (
                    self._current_session.session_type.value
                    if self._current_session else None
                ),
                "duration": (
                    (datetime.now() - self._current_session.started_at).total_seconds()
                    if self._current_session else 0
                ),
            },
            "today": {
                "app_switches": self._app_switch_count,
                "app_usage": dict(self._app_usage_today),
            },
        }

    async def perform_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Perform activity recognition task."""
        task_type = task.get("type", "")

        if task_type == "recognize":
            return await self._recognize_activity(task)
        elif task_type == "get_context":
            return await self._get_current_context()
        elif task_type == "analyze":
            return await self._analyze_productivity(task)
        else:
            return {"error": f"Unknown task type: {task_type}"}

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info(
            "[ACTIVITY-RECOGNITION] Agent started - "
            "ready to track user activities and focus states"
        )

    async def on_stop(self) -> None:
        """Called when agent stops."""
        # End current session if active
        if self._current_session:
            await self._end_session()

        logger.info(
            f"[ACTIVITY-RECOGNITION] Agent stopped - "
            f"tracked {len(self._activity_history)} activities, "
            f"{len(self._session_history)} sessions"
        )
