"""
JARVIS AGI OS - Integration with Existing JARVIS Systems

This module provides seamless integration between the new AGI OS
components and the existing JARVIS infrastructure:

- Screen Analyzer Integration: Connect continuous monitoring to event stream
- Decision Engine Integration: Route autonomous decisions through AGI OS
- Voice System Integration: Unify voice output through Daniel TTS
- Permission System Integration: Bridge approval systems
- Neural Mesh Integration: Connect agents to AGI OS events
- Claude Vision Integration: Intelligent screen analysis with AI understanding
- Proactive Detection: Pattern-based detection for user assistance

Features:
- Dynamic owner identification via voice biometrics
- Claude Vision integration for intelligent screen analysis
- Proactive detection patterns (idle, workflow interruption, opportunities)
- Unified vision interface for all visual components
- Advanced event routing with context enrichment

Usage:
    from agi_os.jarvis_integration import (
        connect_screen_analyzer,
        connect_decision_engine,
        integrate_voice_systems,
        integrate_approval_systems,
        get_unified_vision,
    )

    # Connect screen analyzer to AGI OS with Claude Vision
    bridge = await connect_screen_analyzer(vision_handler, enable_claude_vision=True)

    # Get unified vision interface
    vision = await get_unified_vision()
    analysis = await vision.analyze_current_screen()
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import time
import weakref
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    TYPE_CHECKING,
)

if TYPE_CHECKING:
    from .proactive_event_stream import ProactiveEventStream, AGIEvent, EventType, EventPriority
    from .realtime_voice_communicator import RealTimeVoiceCommunicator
    from .voice_approval_manager import VoiceApprovalManager
    from .owner_identity_service import OwnerIdentityService

logger = logging.getLogger(__name__)


# ============== Event Type Extensions ==============

class VisionEventType(Enum):
    """Extended event types for vision/screen analysis."""
    # Detection Events
    ERROR_DIALOG_DETECTED = "error_dialog_detected"
    WARNING_DIALOG_DETECTED = "warning_dialog_detected"
    NOTIFICATION_POPUP = "notification_popup"
    PERMISSION_REQUEST = "permission_request"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA_DETECTED = "captcha_detected"
    DOWNLOAD_COMPLETE = "download_complete"
    UPDATE_AVAILABLE = "update_available"

    # Meeting/Calendar Events
    MEETING_STARTING = "meeting_starting"
    MEETING_INVITE = "meeting_invite"
    CALENDAR_REMINDER = "calendar_reminder"

    # Security Events
    SECURITY_PROMPT = "security_prompt"
    AUTHENTICATION_REQUIRED = "authentication_required"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    MALWARE_WARNING = "malware_warning"

    # Workflow Events
    LONG_RUNNING_TASK = "long_running_task"
    TASK_COMPLETED = "task_completed"
    USER_IDLE = "user_idle"
    WORKFLOW_BLOCKED = "workflow_blocked"
    WORKFLOW_OPPORTUNITY = "workflow_opportunity"

    # Application Events
    APP_CRASHED = "app_crashed"
    APP_NOT_RESPONDING = "app_not_responding"
    BROWSER_TAB_ALERT = "browser_tab_alert"

    # Content Events
    NEW_MESSAGE = "new_message"
    EMAIL_RECEIVED = "email_received"
    SOCIAL_NOTIFICATION = "social_notification"


@dataclass
class ScreenAnalysisResult:
    """Result of screen analysis via Claude Vision or other analyzers."""
    timestamp: datetime = field(default_factory=datetime.now)
    analysis_id: str = ""

    # Visual analysis
    detected_elements: List[Dict[str, Any]] = field(default_factory=list)
    detected_text: List[str] = field(default_factory=list)
    detected_events: List[VisionEventType] = field(default_factory=list)

    # Context
    active_app: str = ""
    active_window: str = ""
    screen_region: Optional[Tuple[int, int, int, int]] = None

    # AI analysis
    ai_summary: str = ""
    ai_suggestions: List[str] = field(default_factory=list)
    ai_confidence: float = 0.0

    # Owner context
    owner_name: str = ""
    owner_verified: bool = False

    # Metadata
    processing_time_ms: float = 0.0
    model_used: str = ""
    tokens_used: int = 0

    def __post_init__(self):
        if not self.analysis_id:
            self.analysis_id = hashlib.md5(
                f"{self.timestamp.isoformat()}{id(self)}".encode()
            ).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'analysis_id': self.analysis_id,
            'timestamp': self.timestamp.isoformat(),
            'detected_elements': self.detected_elements,
            'detected_text': self.detected_text,
            'detected_events': [e.value for e in self.detected_events],
            'active_app': self.active_app,
            'active_window': self.active_window,
            'screen_region': self.screen_region,
            'ai_summary': self.ai_summary,
            'ai_suggestions': self.ai_suggestions,
            'ai_confidence': self.ai_confidence,
            'owner_name': self.owner_name,
            'owner_verified': self.owner_verified,
            'processing_time_ms': self.processing_time_ms,
            'model_used': self.model_used,
            'tokens_used': self.tokens_used,
        }


@dataclass
class ProactiveDetectionPattern:
    """Pattern for proactive detection of user needs."""
    pattern_id: str
    name: str
    description: str

    # Detection criteria
    visual_triggers: List[str] = field(default_factory=list)  # Visual patterns to match
    text_triggers: List[str] = field(default_factory=list)    # Text patterns to match
    app_triggers: List[str] = field(default_factory=list)     # App names to trigger on
    time_triggers: List[Dict[str, Any]] = field(default_factory=list)  # Time-based triggers

    # Actions
    event_type: Optional[VisionEventType] = None
    suggested_action: str = ""
    voice_notification: str = ""

    # Configuration
    enabled: bool = True
    cooldown_seconds: int = 60
    min_confidence: float = 0.7

    # State
    last_triggered: Optional[datetime] = None
    trigger_count: int = 0


# ============== Screen Analyzer Bridge (Enhanced) ==============

class ScreenAnalyzerBridge:
    """
    Enhanced bridge between continuous screen analyzer and AGI OS event stream.

    Features:
    - More event types and better event handling
    - Claude Vision integration for intelligent analysis
    - Dynamic owner identification
    - Proactive detection patterns
    - Unified interface for all vision components
    """

    def __init__(self):
        """Initialize the enhanced bridge."""
        # Core components
        self._event_stream: Optional[Any] = None
        self._voice: Optional[Any] = None
        self._analyzer: Optional[Any] = None
        self._owner_identity: Optional[Any] = None

        # Claude Vision integration
        self._claude_client: Optional[Any] = None
        self._vision_enabled: bool = False
        self._vision_model: str = "claude-sonnet-4-20250514"
        self._vision_analysis_interval: float = 5.0  # seconds
        self._last_vision_analysis: Optional[datetime] = None

        # Analysis cache
        self._analysis_cache: OrderedDict[str, ScreenAnalysisResult] = OrderedDict()
        self._max_cache_size: int = 100
        self._analysis_history: List[ScreenAnalysisResult] = []
        self._max_history: int = 500

        # Proactive detection
        self._detection_patterns: Dict[str, ProactiveDetectionPattern] = {}
        self._load_default_patterns()

        # Screen state tracking
        self._current_app: str = ""
        self._current_window: str = ""
        self._idle_start: Optional[datetime] = None
        self._idle_threshold: timedelta = timedelta(minutes=5)
        self._last_activity: datetime = datetime.now()

        # Processing state
        self._connected = False
        self._processing_lock = asyncio.Lock()
        self._vision_task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = {
            'events_processed': 0,
            'vision_analyses': 0,
            'patterns_triggered': 0,
            'owner_verifications': 0,
            'errors': 0,
        }

        logger.info("Enhanced ScreenAnalyzerBridge initialized")

    def _load_default_patterns(self) -> None:
        """Load default proactive detection patterns."""
        default_patterns = [
            ProactiveDetectionPattern(
                pattern_id="error_dialog",
                name="Error Dialog Detection",
                description="Detect error dialogs and offer assistance",
                visual_triggers=["error icon", "warning icon", "red x", "exclamation mark"],
                text_triggers=["error", "failed", "exception", "crash", "not responding"],
                event_type=VisionEventType.ERROR_DIALOG_DETECTED,
                suggested_action="analyze_error_and_suggest_fix",
                voice_notification="I've detected an error dialog. Would you like me to help?",
                cooldown_seconds=30,
            ),
            ProactiveDetectionPattern(
                pattern_id="meeting_reminder",
                name="Meeting Reminder",
                description="Detect upcoming meetings and remind user",
                app_triggers=["Calendar", "Outlook", "Google Calendar"],
                text_triggers=["meeting in", "starts in", "reminder", "join meeting"],
                event_type=VisionEventType.MEETING_STARTING,
                voice_notification="You have a meeting coming up soon.",
                cooldown_seconds=300,
            ),
            ProactiveDetectionPattern(
                pattern_id="download_complete",
                name="Download Complete",
                description="Detect completed downloads",
                text_triggers=["download complete", "download finished", "saved to downloads"],
                visual_triggers=["download icon", "checkmark"],
                event_type=VisionEventType.DOWNLOAD_COMPLETE,
                voice_notification="Your download has completed.",
                cooldown_seconds=10,
            ),
            ProactiveDetectionPattern(
                pattern_id="update_available",
                name="Update Available",
                description="Detect software update prompts",
                text_triggers=["update available", "new version", "restart to update", "update now"],
                event_type=VisionEventType.UPDATE_AVAILABLE,
                suggested_action="schedule_update",
                voice_notification="There's a software update available. Should I handle it?",
                cooldown_seconds=3600,
            ),
            ProactiveDetectionPattern(
                pattern_id="permission_request",
                name="Permission Request",
                description="Detect permission request dialogs",
                text_triggers=["allow", "grant access", "permission", "wants to access"],
                visual_triggers=["permission dialog", "system dialog"],
                event_type=VisionEventType.PERMISSION_REQUEST,
                voice_notification="An application is requesting permission.",
                cooldown_seconds=5,
            ),
            ProactiveDetectionPattern(
                pattern_id="user_idle",
                name="User Idle Detection",
                description="Detect when user has been idle",
                time_triggers=[{"idle_minutes": 5}],
                event_type=VisionEventType.USER_IDLE,
                suggested_action="suggest_break_or_tasks",
                cooldown_seconds=600,
            ),
            ProactiveDetectionPattern(
                pattern_id="workflow_blocked",
                name="Workflow Blocked",
                description="Detect when user is stuck on a task",
                time_triggers=[{"same_screen_minutes": 3}],
                text_triggers=["loading", "processing", "please wait"],
                event_type=VisionEventType.WORKFLOW_BLOCKED,
                voice_notification="It looks like you might be waiting on something. Need any help?",
                cooldown_seconds=180,
            ),
            ProactiveDetectionPattern(
                pattern_id="security_prompt",
                name="Security Prompt",
                description="Detect security-related prompts",
                text_triggers=["password", "authenticate", "verify", "suspicious", "malware"],
                visual_triggers=["lock icon", "shield icon", "security warning"],
                event_type=VisionEventType.SECURITY_PROMPT,
                voice_notification="I've detected a security prompt. Please review it carefully.",
                cooldown_seconds=30,
            ),
            ProactiveDetectionPattern(
                pattern_id="new_message",
                name="New Message",
                description="Detect new messages in communication apps",
                app_triggers=["Messages", "Slack", "Discord", "Teams", "Mail"],
                text_triggers=["new message", "unread", "notification badge"],
                visual_triggers=["notification badge", "red dot"],
                event_type=VisionEventType.NEW_MESSAGE,
                cooldown_seconds=60,
            ),
        ]

        for pattern in default_patterns:
            self._detection_patterns[pattern.pattern_id] = pattern

    async def connect(
        self,
        analyzer: Any,
        event_stream: Optional[Any] = None,
        voice: Optional[Any] = None,
        owner_identity: Optional[Any] = None,
        enable_claude_vision: bool = False,
        vision_model: str = "claude-sonnet-4-20250514",
    ) -> None:
        """
        Connect screen analyzer to AGI OS with enhanced features.

        Args:
            analyzer: MemoryAwareScreenAnalyzer instance
            event_stream: ProactiveEventStream (or fetched automatically)
            voice: RealTimeVoiceCommunicator (or fetched automatically)
            owner_identity: OwnerIdentityService (or fetched automatically)
            enable_claude_vision: Enable Claude Vision for intelligent analysis
            vision_model: Claude model to use for vision analysis
        """
        if self._connected:
            logger.warning("Screen analyzer already connected")
            return

        self._analyzer = analyzer
        self._vision_enabled = enable_claude_vision
        self._vision_model = vision_model

        # Get or fetch components
        await self._initialize_components(event_stream, voice, owner_identity)

        # Initialize Claude Vision if enabled
        if self._vision_enabled:
            await self._initialize_claude_vision()

        # Register callbacks
        await self._register_callbacks()

        # Start background vision analysis if enabled
        if self._vision_enabled and self._claude_client:
            self._vision_task = asyncio.create_task(
                self._continuous_vision_analysis(),
                name="vision_analysis"
            )

        self._connected = True
        logger.info(
            "Screen analyzer connected to AGI OS (vision=%s, model=%s)",
            self._vision_enabled,
            self._vision_model
        )

    async def _initialize_components(
        self,
        event_stream: Optional[Any],
        voice: Optional[Any],
        owner_identity: Optional[Any],
    ) -> None:
        """Initialize required components."""
        # Event stream
        if event_stream:
            self._event_stream = event_stream
        else:
            try:
                from .proactive_event_stream import get_event_stream
                self._event_stream = await get_event_stream()
            except Exception as e:
                logger.warning("Could not get event stream: %s", e)

        # Voice communicator
        if voice:
            self._voice = voice
        else:
            try:
                from .realtime_voice_communicator import get_voice_communicator
                self._voice = await get_voice_communicator()
            except Exception as e:
                logger.warning("Could not get voice communicator: %s", e)

        # Owner identity service
        if owner_identity:
            self._owner_identity = owner_identity
        else:
            try:
                from .owner_identity_service import get_owner_identity
                self._owner_identity = await get_owner_identity()
            except Exception as e:
                logger.warning("Could not get owner identity service: %s", e)

    async def _initialize_claude_vision(self) -> None:
        """Initialize Claude Vision client."""
        try:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if api_key:
                self._claude_client = anthropic.AsyncAnthropic(api_key=api_key)
                logger.info("Claude Vision initialized with model %s", self._vision_model)
            else:
                logger.warning("ANTHROPIC_API_KEY not set, Claude Vision disabled")
                self._vision_enabled = False
        except ImportError:
            logger.warning("anthropic package not installed, Claude Vision disabled")
            self._vision_enabled = False
        except Exception as e:
            logger.error("Failed to initialize Claude Vision: %s", e)
            self._vision_enabled = False

    async def _register_callbacks(self) -> None:
        """Register screen analyzer callbacks with enhanced handling."""
        if not self._analyzer:
            return

        # Check if analyzer has event_callbacks attribute
        if hasattr(self._analyzer, 'event_callbacks'):
            callbacks = self._analyzer.event_callbacks

            # Core callbacks
            callback_mapping = {
                'error_detected': self._on_error_detected,
                'content_changed': self._on_content_changed,
                'app_changed': self._on_app_changed,
                'user_needs_help': self._on_user_needs_help,
                'memory_warning': self._on_memory_warning,
                # Extended callbacks
                'notification_detected': self._on_notification_detected,
                'meeting_detected': self._on_meeting_detected,
                'security_concern': self._on_security_concern,
                'screen_captured': self._on_screen_captured,
            }

            for callback_name, handler in callback_mapping.items():
                if callback_name in callbacks:
                    callbacks[callback_name].add(handler)
                    logger.debug("Registered callback: %s", callback_name)

    async def disconnect(self) -> None:
        """Disconnect and cleanup."""
        if self._vision_task:
            self._vision_task.cancel()
            try:
                await self._vision_task
            except asyncio.CancelledError:
                pass

        self._connected = False
        logger.info("Screen analyzer disconnected from AGI OS")

    # ============== Core Event Handlers ==============

    async def _on_error_detected(self, state: Dict[str, Any]) -> None:
        """Handle error detection from screen analyzer."""
        self._stats['events_processed'] += 1

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            # Enrich with owner context
            owner_name = await self._get_owner_name()

            # Determine error severity
            error_type = state.get('error_type', 'unknown')
            severity = self._determine_error_severity(error_type, state)

            event = AGIEvent(
                event_type=EventType.ERROR_DETECTED,
                source="screen_analyzer",
                data={
                    'error_type': error_type,
                    'location': state.get('location', 'screen'),
                    'message': state.get('message', str(state)),
                    'severity': severity,
                    'owner_name': owner_name,
                    'app': state.get('app', self._current_app),
                    'window': state.get('window', self._current_window),
                    'timestamp': datetime.now().isoformat(),
                },
                priority=EventPriority.HIGH if severity == 'critical' else EventPriority.NORMAL,
                requires_narration=True,
            )

            await self._event_stream.emit(event)

            # If Claude Vision is enabled, get intelligent analysis
            if self._vision_enabled and state.get('screenshot'):
                await self._analyze_error_with_vision(state)

    async def _on_content_changed(self, state: Dict[str, Any]) -> None:
        """Handle content change with proactive pattern detection."""
        self._stats['events_processed'] += 1
        self._last_activity = datetime.now()
        self._idle_start = None

        # Check for proactive patterns
        await self._check_proactive_patterns(state)

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            await self._event_stream.emit(AGIEvent(
                event_type=EventType.CONTENT_CHANGED,
                source="screen_analyzer",
                data={
                    **state,
                    'owner_name': await self._get_owner_name(),
                },
                priority=EventPriority.LOW,
            ))

    async def _on_app_changed(self, state: Dict[str, Any]) -> None:
        """Handle app change with context tracking."""
        self._stats['events_processed'] += 1
        self._last_activity = datetime.now()

        old_app = self._current_app
        self._current_app = state.get('app_name', '')
        self._current_window = state.get('window_title', '')

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            await self._event_stream.emit(AGIEvent(
                event_type=EventType.APP_CHANGED,
                source="screen_analyzer",
                data={
                    'previous_app': old_app,
                    'new_app': self._current_app,
                    'window_title': self._current_window,
                    'owner_name': await self._get_owner_name(),
                },
                priority=EventPriority.LOW,
            ))

        # Check if this app change matches any patterns
        await self._check_app_based_patterns()

    async def _on_user_needs_help(self, state: Dict[str, Any]) -> None:
        """Handle user needs help detection with dynamic addressing."""
        self._stats['events_processed'] += 1

        owner_name = await self._get_owner_name()

        if self._voice:
            from .realtime_voice_communicator import VoiceMode

            # Personalized help offer
            message = f"{owner_name}, it looks like you might need some help. "

            # Add context-aware suggestions if available
            if state.get('context'):
                message += f"I noticed you're working on {state.get('context')}. "

            message += "Let me know if I can assist."

            await self._voice.speak(
                message,
                mode=VoiceMode.CONVERSATIONAL,
                context={
                    "open_listen_window": True,
                    "listen_reason": "user_help_offer",
                    "listen_timeout_seconds": float(
                        os.getenv("JARVIS_HELP_LISTEN_TIMEOUT_SECONDS", "20.0")
                    ),
                    "listen_close_on_utterance": True,
                    "listen_metadata": {
                        "trigger": "user_needs_help",
                        "context": state.get("context"),
                    },
                },
            )

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            await self._event_stream.emit(AGIEvent(
                event_type=EventType.USER_COMMAND,  # Using as proxy for help request
                source="screen_analyzer",
                data={
                    'request_type': 'help_needed',
                    'context': state,
                    'owner_name': owner_name,
                },
                priority=EventPriority.HIGH,
                requires_narration=False,  # Already spoke
            ))

    async def _on_memory_warning(self, state: Dict[str, Any]) -> None:
        """Handle memory warning with enhanced reporting."""
        self._stats['events_processed'] += 1

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            await self._event_stream.emit(AGIEvent(
                event_type=EventType.MEMORY_WARNING,
                source="screen_analyzer",
                data={
                    **state,
                    'owner_name': await self._get_owner_name(),
                    'current_app': self._current_app,
                },
                priority=EventPriority.HIGH,
                requires_narration=True,
            ))

    # ============== Extended Event Handlers ==============

    async def _on_notification_detected(self, state: Dict[str, Any]) -> None:
        """Handle notification detection."""
        self._stats['events_processed'] += 1

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            await self._event_stream.emit(AGIEvent(
                event_type=EventType.NOTIFICATION_DETECTED,
                source="screen_analyzer",
                data={
                    'notification_type': state.get('type', 'unknown'),
                    'source_app': state.get('source_app', ''),
                    'title': state.get('title', ''),
                    'content': state.get('content', ''),
                    'owner_name': await self._get_owner_name(),
                },
                priority=EventPriority.NORMAL,
            ))

    async def _on_meeting_detected(self, state: Dict[str, Any]) -> None:
        """Handle meeting detection."""
        self._stats['events_processed'] += 1

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            owner_name = await self._get_owner_name()

            await self._event_stream.emit(AGIEvent(
                event_type=EventType.MEETING_DETECTED,
                source="screen_analyzer",
                data={
                    'meeting_title': state.get('title', ''),
                    'start_time': state.get('start_time', ''),
                    'minutes_until': state.get('minutes_until', 0),
                    'platform': state.get('platform', ''),
                    'owner_name': owner_name,
                },
                priority=EventPriority.HIGH,
                requires_narration=True,
            ))

    async def _on_security_concern(self, state: Dict[str, Any]) -> None:
        """Handle security concern detection."""
        self._stats['events_processed'] += 1

        if self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            await self._event_stream.emit(AGIEvent(
                event_type=EventType.SECURITY_CONCERN,
                source="screen_analyzer",
                data={
                    'concern_type': state.get('type', 'unknown'),
                    'description': state.get('description', ''),
                    'severity': state.get('severity', 'medium'),
                    'recommended_action': state.get('recommended_action', ''),
                    'owner_name': await self._get_owner_name(),
                },
                priority=EventPriority.URGENT,
                requires_narration=True,
            ))

    async def _on_screen_captured(self, state: Dict[str, Any]) -> None:
        """Handle screen capture for vision analysis."""
        self._stats['events_processed'] += 1

        # Update last activity
        self._last_activity = datetime.now()

        # If vision is enabled, queue for analysis
        if self._vision_enabled and state.get('screenshot'):
            # Non-blocking - the continuous analysis loop will handle it
            pass

    # ============== Claude Vision Integration ==============

    async def analyze_screen(
        self,
        screenshot: Optional[bytes] = None,
        context: Optional[str] = None,
        focus_area: Optional[str] = None,
    ) -> ScreenAnalysisResult:
        """
        Analyze screen using Claude Vision.

        Args:
            screenshot: Screenshot bytes (PNG/JPEG), or capture current if None
            context: Additional context for the analysis
            focus_area: Specific area to focus on (e.g., "error dialogs", "notifications")

        Returns:
            ScreenAnalysisResult with AI analysis
        """
        start_time = datetime.now()
        result = ScreenAnalysisResult(
            active_app=self._current_app,
            active_window=self._current_window,
            owner_name=await self._get_owner_name(),
        )

        if not self._claude_client:
            logger.warning("Claude Vision not available")
            return result

        try:
            # Capture screenshot if not provided
            if screenshot is None:
                screenshot = await self._capture_screen()

            if screenshot is None:
                logger.warning("No screenshot available for analysis")
                return result

            # Build the analysis prompt
            prompt = self._build_vision_prompt(context, focus_area)

            # Encode image
            image_base64 = base64.b64encode(screenshot).decode('utf-8')

            # Call Claude Vision
            response = await self._claude_client.messages.create(
                model=self._vision_model,
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image_base64,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt,
                            },
                        ],
                    }
                ],
            )

            # Parse response
            analysis_text = response.content[0].text if response.content else ""
            result = self._parse_vision_response(analysis_text, result)
            result.model_used = self._vision_model
            result.tokens_used = response.usage.input_tokens + response.usage.output_tokens

            self._stats['vision_analyses'] += 1

        except Exception as e:
            logger.error("Vision analysis failed: %s", e)
            self._stats['errors'] += 1

        # Calculate processing time
        result.processing_time_ms = (datetime.now() - start_time).total_seconds() * 1000

        # Cache result
        self._cache_analysis(result)

        return result

    async def _continuous_vision_analysis(self) -> None:
        """Background task for continuous vision analysis."""
        while True:
            try:
                await asyncio.sleep(self._vision_analysis_interval)

                # Skip if recently analyzed
                if self._last_vision_analysis:
                    elapsed = (datetime.now() - self._last_vision_analysis).total_seconds()
                    if elapsed < self._vision_analysis_interval:
                        continue

                # Perform analysis
                result = await self.analyze_screen()
                self._last_vision_analysis = datetime.now()

                # Emit events for detected items
                await self._emit_vision_events(result)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Continuous vision analysis error: %s", e)
                await asyncio.sleep(10)  # Back off on error

    async def _analyze_error_with_vision(self, state: Dict[str, Any]) -> None:
        """Analyze an error with Claude Vision for intelligent suggestions."""
        screenshot = state.get('screenshot')
        if not screenshot:
            return

        result = await self.analyze_screen(
            screenshot=screenshot,
            context="error analysis",
            focus_area="error dialogs and messages"
        )

        if result.ai_suggestions and self._voice:
            from .realtime_voice_communicator import VoiceMode

            # Offer intelligent suggestion
            suggestion = result.ai_suggestions[0] if result.ai_suggestions else "analyze the error"
            await self._voice.speak(
                f"I've analyzed the error. {result.ai_summary}. "
                f"Would you like me to {suggestion}?",
                mode=VoiceMode.THOUGHTFUL,
                context={
                    "open_listen_window": True,
                    "listen_reason": "vision_error_followup",
                    "listen_timeout_seconds": float(
                        os.getenv("JARVIS_VISION_FOLLOWUP_LISTEN_TIMEOUT_SECONDS", "25.0")
                    ),
                    "listen_close_on_utterance": True,
                    "listen_metadata": {
                        "suggestion": suggestion,
                    },
                },
            )

    def _build_vision_prompt(
        self,
        context: Optional[str] = None,
        focus_area: Optional[str] = None,
    ) -> str:
        """Build the prompt for vision analysis."""
        base_prompt = """Analyze this macOS screen capture and provide:
1. A brief summary of what's currently displayed (1-2 sentences)
2. Any detected UI elements that need attention (errors, notifications, prompts)
3. The application context and current task the user appears to be doing
4. Any actionable suggestions for the user

Format your response as:
SUMMARY: <brief summary>
DETECTED: <comma-separated list of detected elements>
APP: <application name>
WINDOW: <window title if visible>
EVENTS: <comma-separated event types from: error_dialog, notification, meeting, security, download, update, permission, idle, blocked>
SUGGESTIONS: <numbered list of actionable suggestions>
CONFIDENCE: <0.0-1.0 confidence score>
"""

        if context:
            base_prompt += f"\nContext: {context}"

        if focus_area:
            base_prompt += f"\nFocus especially on: {focus_area}"

        return base_prompt

    def _parse_vision_response(
        self,
        response_text: str,
        result: ScreenAnalysisResult,
    ) -> ScreenAnalysisResult:
        """Parse Claude Vision response into structured result."""
        lines = response_text.strip().split('\n')

        for line in lines:
            line = line.strip()

            if line.startswith('SUMMARY:'):
                result.ai_summary = line.replace('SUMMARY:', '').strip()

            elif line.startswith('DETECTED:'):
                detected = line.replace('DETECTED:', '').strip()
                if detected:
                    result.detected_elements = [
                        {'type': d.strip()} for d in detected.split(',')
                    ]

            elif line.startswith('APP:'):
                result.active_app = line.replace('APP:', '').strip()

            elif line.startswith('WINDOW:'):
                result.active_window = line.replace('WINDOW:', '').strip()

            elif line.startswith('EVENTS:'):
                events_str = line.replace('EVENTS:', '').strip()
                if events_str:
                    for event_name in events_str.split(','):
                        event_name = event_name.strip().upper()
                        try:
                            # Map to VisionEventType
                            event_map = {
                                'ERROR_DIALOG': VisionEventType.ERROR_DIALOG_DETECTED,
                                'NOTIFICATION': VisionEventType.NOTIFICATION_POPUP,
                                'MEETING': VisionEventType.MEETING_STARTING,
                                'SECURITY': VisionEventType.SECURITY_PROMPT,
                                'DOWNLOAD': VisionEventType.DOWNLOAD_COMPLETE,
                                'UPDATE': VisionEventType.UPDATE_AVAILABLE,
                                'PERMISSION': VisionEventType.PERMISSION_REQUEST,
                                'IDLE': VisionEventType.USER_IDLE,
                                'BLOCKED': VisionEventType.WORKFLOW_BLOCKED,
                            }
                            if event_name in event_map:
                                result.detected_events.append(event_map[event_name])
                        except (KeyError, ValueError):
                            pass

            elif line.startswith('SUGGESTIONS:'):
                continue  # Next lines are suggestions

            elif line and line[0].isdigit() and '.' in line[:3]:
                # Numbered suggestion
                suggestion = line.split('.', 1)[1].strip() if '.' in line else line
                result.ai_suggestions.append(suggestion)

            elif line.startswith('CONFIDENCE:'):
                try:
                    result.ai_confidence = float(line.replace('CONFIDENCE:', '').strip())
                except ValueError:
                    result.ai_confidence = 0.5

        return result

    async def _capture_screen(self) -> Optional[bytes]:
        """Capture current screen."""
        try:
            # Try using the analyzer's capture method
            if self._analyzer and hasattr(self._analyzer, 'capture_screen'):
                return await self._analyzer.capture_screen()

            # Fallback to screencapture command
            import subprocess
            import tempfile

            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
                temp_path = f.name

            subprocess.run(
                ['screencapture', '-x', '-t', 'png', temp_path],
                check=True,
                capture_output=True
            )

            with open(temp_path, 'rb') as f:
                data = f.read()

            os.unlink(temp_path)
            return data

        except Exception as e:
            logger.error("Screen capture failed: %s", e)
            return None

    async def _emit_vision_events(self, result: ScreenAnalysisResult) -> None:
        """Emit AGI events for vision analysis results."""
        if not self._event_stream:
            return

        from .proactive_event_stream import AGIEvent, EventType, EventPriority

        for vision_event in result.detected_events:
            # Map VisionEventType to standard EventType where possible
            event_type_map = {
                VisionEventType.ERROR_DIALOG_DETECTED: EventType.ERROR_DETECTED,
                VisionEventType.WARNING_DIALOG_DETECTED: EventType.WARNING_DETECTED,
                VisionEventType.NOTIFICATION_POPUP: EventType.NOTIFICATION_DETECTED,
                VisionEventType.MEETING_STARTING: EventType.MEETING_DETECTED,
                VisionEventType.SECURITY_PROMPT: EventType.SECURITY_CONCERN,
            }

            agi_event_type = event_type_map.get(vision_event, EventType.CONTENT_CHANGED)

            priority = EventPriority.NORMAL
            if vision_event in [VisionEventType.ERROR_DIALOG_DETECTED,
                               VisionEventType.SECURITY_PROMPT]:
                priority = EventPriority.HIGH

            await self._event_stream.emit(AGIEvent(
                event_type=agi_event_type,
                source="vision_analyzer",
                data={
                    'vision_event': vision_event.value,
                    'summary': result.ai_summary,
                    'suggestions': result.ai_suggestions,
                    'confidence': result.ai_confidence,
                    'owner_name': result.owner_name,
                },
                priority=priority,
                requires_narration=priority.value >= EventPriority.HIGH.value,
            ))

    def _cache_analysis(self, result: ScreenAnalysisResult) -> None:
        """Cache analysis result."""
        self._analysis_cache[result.analysis_id] = result
        self._analysis_history.append(result)

        # Trim cache
        while len(self._analysis_cache) > self._max_cache_size:
            self._analysis_cache.popitem(last=False)

        while len(self._analysis_history) > self._max_history:
            self._analysis_history.pop(0)

    # ============== Proactive Detection ==============

    async def _check_proactive_patterns(self, state: Dict[str, Any]) -> None:
        """Check state against proactive detection patterns."""
        text_content = state.get('text', '')
        visual_elements = state.get('visual_elements', [])

        for pattern in self._detection_patterns.values():
            if not pattern.enabled:
                continue

            # Check cooldown
            if pattern.last_triggered:
                elapsed = (datetime.now() - pattern.last_triggered).total_seconds()
                if elapsed < pattern.cooldown_seconds:
                    continue

            # Check triggers
            triggered = False

            # Text triggers
            if pattern.text_triggers and text_content:
                for trigger in pattern.text_triggers:
                    if trigger.lower() in text_content.lower():
                        triggered = True
                        break

            # Visual triggers
            if not triggered and pattern.visual_triggers and visual_elements:
                for trigger in pattern.visual_triggers:
                    if any(trigger.lower() in str(elem).lower() for elem in visual_elements):
                        triggered = True
                        break

            if triggered:
                await self._trigger_pattern(pattern, state)

    async def _check_app_based_patterns(self) -> None:
        """Check app-based patterns after app change."""
        for pattern in self._detection_patterns.values():
            if not pattern.enabled or not pattern.app_triggers:
                continue

            # Check cooldown
            if pattern.last_triggered:
                elapsed = (datetime.now() - pattern.last_triggered).total_seconds()
                if elapsed < pattern.cooldown_seconds:
                    continue

            # Check if current app matches
            for trigger_app in pattern.app_triggers:
                if trigger_app.lower() in self._current_app.lower():
                    await self._trigger_pattern(pattern, {'app': self._current_app})
                    break

    async def _trigger_pattern(
        self,
        pattern: ProactiveDetectionPattern,
        state: Dict[str, Any],
    ) -> None:
        """Trigger a proactive detection pattern."""
        pattern.last_triggered = datetime.now()
        pattern.trigger_count += 1
        self._stats['patterns_triggered'] += 1

        logger.info("Pattern triggered: %s", pattern.name)

        # Voice notification
        if pattern.voice_notification and self._voice:
            from .realtime_voice_communicator import VoiceMode

            owner_name = await self._get_owner_name()
            notification = pattern.voice_notification.replace("{owner}", owner_name)

            await self._voice.speak(
                notification,
                mode=VoiceMode.NOTIFICATION,
                context={
                    "open_listen_window": bool(pattern.suggested_action),
                    "listen_reason": "pattern_triggered",
                    "listen_timeout_seconds": float(
                        os.getenv("JARVIS_PATTERN_LISTEN_TIMEOUT_SECONDS", "15.0")
                    ),
                    "listen_close_on_utterance": True,
                    "listen_metadata": {
                        "pattern_id": pattern.pattern_id,
                        "pattern_name": pattern.name,
                        "suggested_action": pattern.suggested_action,
                    },
                },
            )

        # Emit event
        if pattern.event_type and self._event_stream:
            from .proactive_event_stream import AGIEvent, EventType, EventPriority

            # Map VisionEventType to base EventType
            type_map = {
                VisionEventType.ERROR_DIALOG_DETECTED: EventType.ERROR_DETECTED,
                VisionEventType.WARNING_DIALOG_DETECTED: EventType.WARNING_DETECTED,
                VisionEventType.NOTIFICATION_POPUP: EventType.NOTIFICATION_DETECTED,
                VisionEventType.MEETING_STARTING: EventType.MEETING_DETECTED,
                VisionEventType.SECURITY_PROMPT: EventType.SECURITY_CONCERN,
                VisionEventType.USER_IDLE: EventType.CONTENT_CHANGED,
                VisionEventType.WORKFLOW_BLOCKED: EventType.WARNING_DETECTED,
            }

            event_type = type_map.get(pattern.event_type, EventType.CONTENT_CHANGED)

            await self._event_stream.emit(AGIEvent(
                event_type=event_type,
                source="proactive_detection",
                data={
                    'pattern_id': pattern.pattern_id,
                    'pattern_name': pattern.name,
                    'suggested_action': pattern.suggested_action,
                    'trigger_state': state,
                    'owner_name': await self._get_owner_name(),
                },
                priority=EventPriority.NORMAL,
            ))

    # ============== Dynamic Owner Identification ==============

    async def _get_owner_name(self) -> str:
        """Get owner name dynamically via voice biometrics or fallback."""
        if self._owner_identity:
            try:
                return await self._owner_identity.get_owner_name(use_first_name=True)
            except Exception as e:
                logger.debug("Owner identity lookup failed: %s", e)

        return "sir"

    async def verify_owner(self, audio_data: Optional[bytes] = None) -> Tuple[bool, float]:
        """
        Verify if the current user is the owner.

        Args:
            audio_data: Optional audio data for voice verification

        Returns:
            Tuple of (is_owner, confidence)
        """
        self._stats['owner_verifications'] += 1

        if self._owner_identity:
            try:
                return await self._owner_identity.verify_owner_voice(audio_data)
            except Exception as e:
                logger.warning("Owner verification failed: %s", e)

        return False, 0.0

    # ============== Helper Methods ==============

    def _determine_error_severity(
        self,
        error_type: str,
        state: Dict[str, Any],
    ) -> str:
        """Determine error severity based on type and context."""
        critical_types = ['crash', 'fatal', 'kernel', 'system', 'security']
        high_types = ['exception', 'failure', 'error']

        error_lower = error_type.lower()

        if any(ct in error_lower for ct in critical_types):
            return 'critical'
        elif any(ht in error_lower for ht in high_types):
            return 'high'
        else:
            return 'normal'

    # ============== Pattern Management ==============

    def add_detection_pattern(self, pattern: ProactiveDetectionPattern) -> None:
        """Add a custom detection pattern."""
        self._detection_patterns[pattern.pattern_id] = pattern
        logger.info("Added detection pattern: %s", pattern.name)

    def remove_detection_pattern(self, pattern_id: str) -> bool:
        """Remove a detection pattern."""
        if pattern_id in self._detection_patterns:
            del self._detection_patterns[pattern_id]
            return True
        return False

    def enable_pattern(self, pattern_id: str) -> bool:
        """Enable a detection pattern."""
        if pattern_id in self._detection_patterns:
            self._detection_patterns[pattern_id].enabled = True
            return True
        return False

    def disable_pattern(self, pattern_id: str) -> bool:
        """Disable a detection pattern."""
        if pattern_id in self._detection_patterns:
            self._detection_patterns[pattern_id].enabled = False
            return True
        return False

    def get_patterns(self) -> Dict[str, ProactiveDetectionPattern]:
        """Get all detection patterns."""
        return self._detection_patterns.copy()

    # ============== Analysis History ==============

    def get_recent_analyses(self, count: int = 10) -> List[ScreenAnalysisResult]:
        """Get recent screen analyses."""
        return self._analysis_history[-count:]

    def get_analysis_by_id(self, analysis_id: str) -> Optional[ScreenAnalysisResult]:
        """Get a specific analysis by ID."""
        return self._analysis_cache.get(analysis_id)

    # ============== Statistics ==============

    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics."""
        return {
            **self._stats,
            'connected': self._connected,
            'vision_enabled': self._vision_enabled,
            'vision_model': self._vision_model,
            'patterns_count': len(self._detection_patterns),
            'patterns_enabled': sum(1 for p in self._detection_patterns.values() if p.enabled),
            'cache_size': len(self._analysis_cache),
            'history_size': len(self._analysis_history),
            'current_app': self._current_app,
            'current_window': self._current_window,
            'last_activity': self._last_activity.isoformat() if self._last_activity else None,
        }


# ============== Unified Vision Interface ==============

class UnifiedVisionInterface:
    """
    Unified interface for all vision-related components.

    Provides a single access point for:
    - Screen analysis (continuous monitoring)
    - Claude Vision (AI-powered analysis)
    - Proactive detection patterns
    - Owner identification
    """

    def __init__(self):
        """Initialize unified vision interface."""
        self._screen_bridge: Optional[ScreenAnalyzerBridge] = None
        self._initialized = False

    async def initialize(
        self,
        screen_bridge: Optional[ScreenAnalyzerBridge] = None,
    ) -> None:
        """Initialize the unified interface."""
        if screen_bridge:
            self._screen_bridge = screen_bridge
        else:
            global _screen_bridge
            if _screen_bridge is None:
                _screen_bridge = ScreenAnalyzerBridge()
            self._screen_bridge = _screen_bridge

        self._initialized = True

    async def analyze_current_screen(
        self,
        context: Optional[str] = None,
        focus_area: Optional[str] = None,
    ) -> ScreenAnalysisResult:
        """
        Analyze the current screen.

        Args:
            context: Additional context for analysis
            focus_area: Specific area to focus on

        Returns:
            ScreenAnalysisResult with analysis
        """
        if not self._screen_bridge:
            return ScreenAnalysisResult()

        return await self._screen_bridge.analyze_screen(
            context=context,
            focus_area=focus_area,
        )

    async def get_current_context(self) -> Dict[str, Any]:
        """Get current screen context without full analysis."""
        if not self._screen_bridge:
            return {}

        return {
            'app': self._screen_bridge._current_app,
            'window': self._screen_bridge._current_window,
            'last_activity': self._screen_bridge._last_activity.isoformat() if self._screen_bridge._last_activity else None,
            'owner': await self._screen_bridge._get_owner_name() if self._screen_bridge else 'unknown',
        }

    async def verify_owner(self, audio_data: Optional[bytes] = None) -> Tuple[bool, float]:
        """Verify current user is owner."""
        if not self._screen_bridge:
            return False, 0.0
        return await self._screen_bridge.verify_owner(audio_data)

    def get_detection_patterns(self) -> Dict[str, ProactiveDetectionPattern]:
        """Get all detection patterns."""
        if not self._screen_bridge:
            return {}
        return self._screen_bridge.get_patterns()

    def add_pattern(self, pattern: ProactiveDetectionPattern) -> None:
        """Add a custom detection pattern."""
        if self._screen_bridge:
            self._screen_bridge.add_detection_pattern(pattern)

    def get_recent_analyses(self, count: int = 10) -> List[ScreenAnalysisResult]:
        """Get recent analyses."""
        if not self._screen_bridge:
            return []
        return self._screen_bridge.get_recent_analyses(count)

    def get_stats(self) -> Dict[str, Any]:
        """Get unified stats."""
        if not self._screen_bridge:
            return {'initialized': False}

        stats = self._screen_bridge.get_stats()
        stats['unified_interface'] = True
        return stats


# ============== Other Bridges (kept from original) ==============

class DecisionEngineBridge:
    """
    Bridge between autonomous decision engine and AGI OS.

    Routes decisions through the AGI OS approval system.
    """

    def __init__(self):
        """Initialize the bridge."""
        self._decision_engine: Optional[Any] = None
        self._approval_manager: Optional[Any] = None
        self._event_stream: Optional[Any] = None
        self._owner_identity: Optional[Any] = None
        self._connected = False

    async def connect(
        self,
        decision_engine: Any,
        approval_manager: Optional[Any] = None,
        event_stream: Optional[Any] = None,
        owner_identity: Optional[Any] = None,
    ) -> None:
        """
        Connect decision engine to AGI OS.

        Args:
            decision_engine: AutonomousDecisionEngine instance
            approval_manager: VoiceApprovalManager (or fetched automatically)
            event_stream: ProactiveEventStream (or fetched automatically)
            owner_identity: OwnerIdentityService (or fetched automatically)
        """
        if self._connected:
            logger.warning("Decision engine already connected")
            return

        self._decision_engine = decision_engine

        # Get or fetch approval manager
        if approval_manager:
            self._approval_manager = approval_manager
        else:
            try:
                from .voice_approval_manager import get_approval_manager
                self._approval_manager = await get_approval_manager()
            except Exception as e:
                logger.warning("Could not get approval manager: %s", e)

        # Get or fetch event stream
        if event_stream:
            self._event_stream = event_stream
        else:
            try:
                from .proactive_event_stream import get_event_stream
                self._event_stream = await get_event_stream()
            except Exception as e:
                logger.warning("Could not get event stream: %s", e)

        # Get or fetch owner identity
        if owner_identity:
            self._owner_identity = owner_identity
        else:
            try:
                from .owner_identity_service import get_owner_identity
                self._owner_identity = await get_owner_identity()
            except Exception as e:
                logger.warning("Could not get owner identity: %s", e)

        # Register decision handler
        if hasattr(self._decision_engine, 'register_decision_handler'):
            self._decision_engine.register_decision_handler(
                'agi_os_approval',
                self._handle_decision
            )

        self._connected = True
        logger.info("Decision engine connected to AGI OS")

    async def _handle_decision(self, context: Dict[str, Any]) -> List[Any]:
        """
        Handle decisions from the decision engine.

        Routes through AGI OS approval system.
        """
        if not self._approval_manager:
            return []

        # This would be called by the decision engine with proposed actions
        # The actions would then be routed through approval
        return []

    async def _get_owner_name(self) -> str:
        """Get owner name dynamically."""
        if self._owner_identity:
            try:
                return await self._owner_identity.get_owner_name(use_first_name=True)
            except Exception:
                pass
        return "sir"


class VoiceSystemBridge:
    """
    Bridge to unify voice output through AGI OS.

    Redirects existing voice calls to the RealTimeVoiceCommunicator.
    """

    def __init__(self):
        """Initialize the bridge."""
        self._voice: Optional[Any] = None
        self._original_voices: Dict[str, Any] = {}
        self._connected = False

    async def connect(self, voice: Optional[Any] = None) -> None:
        """
        Connect voice systems to AGI OS.

        Args:
            voice: RealTimeVoiceCommunicator (or fetched automatically)
        """
        if self._connected:
            return

        if voice:
            self._voice = voice
        else:
            try:
                from .realtime_voice_communicator import get_voice_communicator
                self._voice = await get_voice_communicator()
            except Exception as e:
                logger.warning("Could not get voice communicator: %s", e)
                return

        self._connected = True
        logger.info("Voice systems connected to AGI OS")

    async def speak(
        self,
        text: str,
        mode: str = "normal",
        **kwargs
    ) -> Optional[str]:
        """
        Speak through AGI OS voice system.

        Args:
            text: Text to speak
            mode: Voice mode
            **kwargs: Additional parameters

        Returns:
            Message ID or None
        """
        if not self._voice:
            return None

        from .realtime_voice_communicator import VoiceMode

        voice_modes = {
            'normal': VoiceMode.NORMAL,
            'urgent': VoiceMode.URGENT,
            'thoughtful': VoiceMode.THOUGHTFUL,
            'quiet': VoiceMode.QUIET,
            'notification': VoiceMode.NOTIFICATION,
        }

        voice_mode = voice_modes.get(mode, VoiceMode.NORMAL)
        allowed_kwargs = {}
        for key in ("priority", "callback", "context", "allow_repetition"):
            if key in kwargs:
                allowed_kwargs[key] = kwargs[key]
        return await self._voice.speak(text, mode=voice_mode, **allowed_kwargs)


class PermissionSystemBridge:
    """
    Bridge between existing permission manager and AGI OS approval system.

    Syncs approval decisions and learned patterns.
    """

    def __init__(self):
        """Initialize the bridge."""
        self._permission_manager: Optional[Any] = None
        self._approval_manager: Optional[Any] = None
        self._connected = False

    async def connect(
        self,
        permission_manager: Optional[Any] = None,
        approval_manager: Optional[Any] = None
    ) -> None:
        """
        Connect permission systems.

        Args:
            permission_manager: Existing PermissionManager
            approval_manager: VoiceApprovalManager (or fetched automatically)
        """
        if self._connected:
            return

        self._permission_manager = permission_manager

        if approval_manager:
            self._approval_manager = approval_manager
        else:
            try:
                from .voice_approval_manager import get_approval_manager
                self._approval_manager = await get_approval_manager()
            except Exception as e:
                logger.warning("Could not get approval manager: %s", e)
                return

        # Sync patterns from permission manager to approval manager
        if self._permission_manager and self._approval_manager:
            await self._sync_patterns()

        self._connected = True
        logger.info("Permission systems connected to AGI OS")

    async def _sync_patterns(self) -> None:
        """Sync learned patterns between systems."""
        # Get stats from permission manager
        if hasattr(self._permission_manager, 'get_permission_stats'):
            stats = self._permission_manager.get_permission_stats()
            logger.debug("Synced %d permission patterns", stats.get('unique_actions', 0))


class NeuralMeshBridge:
    """
    Bidirectional bridge between Neural Mesh AgentCommunicationBus and AGI OS ProactiveEventStream.

    v237.2: Evolved from one-way emit helper into full duplex event bridge.

    Forward (Bus → EventStream): Agent broadcasts → AGIEvents → IntelligentActionOrchestrator
    Reverse (EventStream → Bus): Orchestrator decisions → Bus broadcasts → All agents

    Features:
    - Loop prevention via source tagging + metadata flags
    - Per-MessageType rate limiting to prevent event floods
    - Translation tables for type/priority mapping
    - Stats tracking for observability
    """

    # Forward translation: MessageType → (EventType, EventPriority, requires_narration, rate_limit_seconds)
    _FORWARD_MAP: Dict[str, Tuple[str, str, bool, float]] = {
        'error_detected':       ('error_detected',        'HIGH',   True,  2.0),
        'alert_raised':         ('warning_detected',      'HIGH',   True,  2.0),
        'task_failed':          ('action_failed',         'HIGH',   True,  2.0),
        'task_completed':       ('action_completed',      'NORMAL', False, 1.0),
        'knowledge_shared':     ('pattern_learned',       'LOW',    False, 5.0),
        'agent_status_changed': ('health_check',          'LOW',    False, 10.0),
        'notification':         ('notification_detected',  'NORMAL', True,  1.0),
        'context_update':       ('content_changed',       'LOW',    False, 2.0),
    }

    # Reverse translation: EventType → (MessageType, broadcast?)
    _REVERSE_MAP: Dict[str, Tuple[str, bool]] = {
        'action_proposed':  ('announcement',     True),
        'action_completed': ('announcement',     True),
        'action_failed':    ('alert_raised',     True),
        'pattern_learned':  ('knowledge_shared', True),
        'user_command':     ('task_assigned',     False),  # targeted to coordinator
        # v238.0: Complete reverse mapping coverage
        'error_detected':        ('error_detected',     True),
        'warning_detected':      ('alert_raised',       True),
        'notification_detected': ('notification',       True),
        'health_check':          ('agent_health_check', True),
        'content_changed':       ('context_update',     True),
        'security_concern':      ('alert_raised',       True),
    }

    # CUSTOM message payload keys that indicate bridgeable intent
    _CUSTOM_BRIDGE_KEYS = ('event_category', 'severity', 'agi_event_type')

    def __init__(self):
        """Initialize the bridge."""
        self._neural_mesh: Optional[Any] = None
        self._event_stream: Optional[Any] = None
        self._owner_identity: Optional[Any] = None
        self._bus: Optional[Any] = None
        self._connected = False
        self._forward_subscribed = False
        self._reverse_sub_ids: List[str] = []
        self._rate_limits: Dict[str, float] = {}  # MessageType.value → last_emit monotonic time
        self._stats = {'forward': 0, 'reverse': 0, 'rate_limited': 0, 'loop_prevented': 0}

    async def connect(
        self,
        neural_mesh: Optional[Any] = None,
        event_stream: Optional[Any] = None,
        owner_identity: Optional[Any] = None,
    ) -> None:
        """
        Connect Neural Mesh to AGI OS with bidirectional event bridging.

        Args:
            neural_mesh: NeuralMeshCoordinator (has .bus property)
            event_stream: ProactiveEventStream (optional, auto-resolved)
            owner_identity: OwnerIdentityService (optional, auto-resolved)
        """
        if self._connected:
            return

        self._neural_mesh = neural_mesh

        # Resolve event stream
        if event_stream:
            self._event_stream = event_stream
        else:
            try:
                from .proactive_event_stream import get_event_stream
                self._event_stream = await get_event_stream()
            except Exception as e:
                logger.warning("Could not get event stream: %s", e)
                return

        # Resolve owner identity
        if owner_identity:
            self._owner_identity = owner_identity
        else:
            try:
                from .owner_identity_service import get_owner_identity
                self._owner_identity = await get_owner_identity()
            except Exception as e:
                logger.warning("Could not get owner identity: %s", e)

        # Get bus from coordinator for bidirectional bridging
        if neural_mesh and hasattr(neural_mesh, 'bus'):
            try:
                self._bus = neural_mesh.bus
            except RuntimeError:
                logger.warning("Neural Mesh bus not initialized yet")
                self._bus = None

        # Subscribe forward: Bus → EventStream
        if self._bus and not self._forward_subscribed:
            await self._subscribe_forward()

        # Subscribe reverse: EventStream → Bus
        if self._event_stream and self._bus:
            self._subscribe_reverse()

        self._connected = True
        logger.info(
            "Neural Mesh ↔ Event Stream bridge connected (forward=%s, reverse_subs=%d)",
            self._forward_subscribed, len(self._reverse_sub_ids)
        )

    async def _subscribe_forward(self) -> None:
        """Subscribe to bus broadcasts for forward bridging (Bus → EventStream)."""
        from neural_mesh.data_models import MessageType

        forward_types = set()
        for msg_type_val in self._FORWARD_MAP:
            try:
                forward_types.add(MessageType(msg_type_val))
            except ValueError:
                pass

        # Also subscribe to CUSTOM for selective bridging
        forward_types.add(MessageType.CUSTOM)

        for msg_type in forward_types:
            try:
                await self._bus.subscribe_broadcast(msg_type, self._forward_handler)
            except Exception as e:
                logger.warning("Failed to subscribe forward for %s: %s", msg_type.value, e)

        self._forward_subscribed = True
        logger.debug("Forward subscriptions: %d message types", len(forward_types))

    def _subscribe_reverse(self) -> None:
        """Subscribe to event stream for reverse bridging (EventStream → Bus)."""
        from .proactive_event_stream import EventType

        reverse_types = []
        for evt_type_val in self._REVERSE_MAP:
            try:
                reverse_types.append(EventType(evt_type_val))
            except ValueError:
                pass

        if reverse_types:
            sub_id = self._event_stream.subscribe(
                event_types=reverse_types,
                handler=self._reverse_handler,
            )
            self._reverse_sub_ids.append(sub_id)
            logger.debug("Reverse subscription: %s for %d event types", sub_id, len(reverse_types))

    # ---- Forward Bridge: Bus → EventStream ----

    async def _forward_handler(self, message: Any) -> None:
        """Handle bus broadcast → translate → emit to EventStream."""
        try:
            # Loop prevention: skip messages originating from reverse bridge
            from_agent = getattr(message, 'from_agent', '')
            if from_agent == 'agi_os_bridge':
                self._stats['loop_prevented'] += 1
                return

            metadata = getattr(message, 'metadata', {}) or {}
            if metadata.get('bridged'):
                self._stats['loop_prevented'] += 1
                return

            msg_type = getattr(message, 'message_type', None)
            if msg_type is None:
                return

            msg_type_val = msg_type.value if hasattr(msg_type, 'value') else str(msg_type)

            # Rate limit per MessageType
            rate_limit = self._get_forward_rate_limit(msg_type_val)
            now = time.monotonic()
            if msg_type_val in self._rate_limits and (now - self._rate_limits[msg_type_val]) < rate_limit:
                self._stats['rate_limited'] += 1
                return

            # Translate and emit (record rate limit ONLY on successful emission,
            # so failed translations like CUSTOM without intent don't consume it)
            agi_event = self._translate_to_agi_event(message, msg_type_val)
            if agi_event and self._event_stream:
                self._rate_limits[msg_type_val] = now
                await self._event_stream.emit(agi_event)
                self._stats['forward'] += 1

        except Exception as e:
            logger.warning("Forward bridge error: %s", e, exc_info=True)

    def _get_forward_rate_limit(self, msg_type_val: str) -> float:
        """Get rate limit in seconds for a MessageType value."""
        mapping = self._FORWARD_MAP.get(msg_type_val)
        if mapping:
            return mapping[3]
        return 2.0  # default for CUSTOM / unmapped

    def _translate_to_agi_event(self, message: Any, msg_type_val: str) -> Optional[Any]:
        """Translate a bus AgentMessage to an AGIEvent."""
        from .proactive_event_stream import AGIEvent, EventType, EventPriority

        mapping = self._FORWARD_MAP.get(msg_type_val)
        payload = getattr(message, 'payload', {}) or {}
        from_agent = getattr(message, 'from_agent', 'unknown')

        if mapping:
            evt_type_val, priority_name, narrate, _ = mapping
            try:
                event_type = EventType(evt_type_val)
            except ValueError:
                return None
            priority = getattr(EventPriority, priority_name, EventPriority.NORMAL)
        elif msg_type_val == 'custom':
            # Only bridge CUSTOM messages that explicitly declare intent
            event_type = self._resolve_custom_event_type(payload)
            if event_type is None:
                return None
            priority = self._resolve_custom_priority(payload)
            narrate = payload.get('requires_narration', False)
        else:
            return None

        return AGIEvent(
            event_type=event_type,
            source=f"neural_mesh_bridge.{from_agent}",
            data=payload,
            priority=priority,
            requires_narration=narrate,
            correlation_id=getattr(message, 'correlation_id', None),
            metadata={'bridged': True, 'original_msg_type': msg_type_val},
        )

    def _resolve_custom_event_type(self, payload: Dict[str, Any]) -> Optional[Any]:
        """Resolve EventType from CUSTOM message payload keys."""
        from .proactive_event_stream import EventType

        # Check for explicit event type declaration
        agi_type = payload.get('agi_event_type') or payload.get('event_category')
        if agi_type:
            try:
                return EventType(agi_type)
            except ValueError:
                pass

        # Map severity to detection events
        severity = payload.get('severity', '').lower()
        severity_map = {
            'error': EventType.ERROR_DETECTED,
            'critical': EventType.ERROR_DETECTED,
            'warning': EventType.WARNING_DETECTED,
            'security': EventType.SECURITY_CONCERN,
        }
        return severity_map.get(severity)

    def _resolve_custom_priority(self, payload: Dict[str, Any]) -> Any:
        """Resolve EventPriority from CUSTOM message payload."""
        from .proactive_event_stream import EventPriority

        severity = payload.get('severity', '').lower()
        priority_map = {
            'critical': EventPriority.CRITICAL,
            'error': EventPriority.HIGH,
            'warning': EventPriority.HIGH,
            'security': EventPriority.URGENT,
        }
        return priority_map.get(severity, EventPriority.NORMAL)

    # ---- Reverse Bridge: EventStream → Bus ----

    async def _reverse_handler(self, event: Any) -> None:
        """Handle EventStream event → translate → broadcast to bus."""
        try:
            # Loop prevention: skip events originating from forward bridge
            source = getattr(event, 'source', '')
            if source.startswith('neural_mesh_bridge.'):
                self._stats['loop_prevented'] += 1
                return

            metadata = getattr(event, 'metadata', {}) or {}
            if metadata.get('bridged'):
                self._stats['loop_prevented'] += 1
                return

            if not self._bus:
                return

            event_type = getattr(event, 'event_type', None)
            if event_type is None:
                return

            evt_type_val = event_type.value if hasattr(event_type, 'value') else str(event_type)
            mapping = self._REVERSE_MAP.get(evt_type_val)
            if not mapping:
                return

            msg_type_val, is_broadcast = mapping

            from neural_mesh.data_models import MessageType
            try:
                msg_type = MessageType(msg_type_val)
            except ValueError:
                return

            payload = getattr(event, 'data', {}) or {}
            payload_with_meta = {
                **payload,
                'bridged': True,
                'agi_event_type': evt_type_val,
                'agi_event_id': getattr(event, 'event_id', ''),
                'agi_source': source,
            }

            await self._bus.broadcast(
                from_agent='agi_os_bridge',
                message_type=msg_type,
                payload=payload_with_meta,
            )
            self._stats['reverse'] += 1

        except Exception as e:
            logger.warning("Reverse bridge error: %s", e, exc_info=True)

    # ---- Public API (backward compatible) ----

    async def emit_agent_event(
        self,
        agent_name: str,
        event_type: str,
        data: Dict[str, Any]
    ) -> Optional[str]:
        """
        Emit an event from a Neural Mesh agent (legacy one-shot API).

        Args:
            agent_name: Name of the agent
            event_type: Type of event ('error', 'warning', 'action_proposed', etc.)
            data: Event data

        Returns:
            Event ID or None
        """
        if not self._event_stream:
            return None

        from .proactive_event_stream import AGIEvent, EventType, EventPriority

        event_type_map = {
            'error': EventType.ERROR_DETECTED,
            'warning': EventType.WARNING_DETECTED,
            'action_proposed': EventType.ACTION_PROPOSED,
            'action_completed': EventType.ACTION_COMPLETED,
            'action_failed': EventType.ACTION_FAILED,
        }

        agi_event_type = event_type_map.get(event_type, EventType.CONTENT_CHANGED)

        owner_name = "sir"
        if self._owner_identity:
            try:
                owner_name = await self._owner_identity.get_owner_name(use_first_name=True)
            except Exception:
                pass

        event = AGIEvent(
            event_type=agi_event_type,
            source=f"neural_mesh.{agent_name}",
            data={**data, 'owner_name': owner_name},
            priority=EventPriority.NORMAL,
        )

        await self._event_stream.emit(event)
        return event.event_id

    async def stop(self) -> None:
        """Stop the bridge: unsubscribe from event stream, clear state."""
        # Unsubscribe reverse (event stream subscriptions)
        if self._event_stream:
            for sub_id in self._reverse_sub_ids:
                try:
                    self._event_stream.unsubscribe(sub_id)
                except Exception:
                    pass
        self._reverse_sub_ids.clear()

        # Note: bus broadcast subscriptions don't have an unsubscribe API,
        # but the bus itself will be stopped by the coordinator shortly after.
        self._forward_subscribed = False
        self._connected = False
        self._rate_limits.clear()

        logger.info(
            "Neural Mesh bridge stopped (fwd=%d, rev=%d, rate_limited=%d, loops=%d)",
            self._stats['forward'], self._stats['reverse'],
            self._stats['rate_limited'], self._stats['loop_prevented'],
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics for observability."""
        return {
            **self._stats,
            'connected': self._connected,
            'forward_subscribed': self._forward_subscribed,
            'reverse_subscriptions': len(self._reverse_sub_ids),
        }


# ============== Singleton Instances ==============

_screen_bridge: Optional[ScreenAnalyzerBridge] = None
_decision_bridge: Optional[DecisionEngineBridge] = None
_voice_bridge: Optional[VoiceSystemBridge] = None
_permission_bridge: Optional[PermissionSystemBridge] = None
_mesh_bridge: Optional[NeuralMeshBridge] = None
_unified_vision: Optional[UnifiedVisionInterface] = None


# ============== Convenience Functions ==============

async def connect_screen_analyzer(
    analyzer: Any,
    enable_claude_vision: bool = False,
    vision_model: str = "claude-sonnet-4-20250514",
) -> ScreenAnalyzerBridge:
    """
    Connect a screen analyzer to AGI OS with enhanced features.

    Args:
        analyzer: MemoryAwareScreenAnalyzer instance
        enable_claude_vision: Enable Claude Vision for intelligent analysis
        vision_model: Claude model to use for vision analysis

    Returns:
        ScreenAnalyzerBridge instance
    """
    global _screen_bridge

    if _screen_bridge is None:
        _screen_bridge = ScreenAnalyzerBridge()

    await _screen_bridge.connect(
        analyzer,
        enable_claude_vision=enable_claude_vision,
        vision_model=vision_model,
    )
    return _screen_bridge


async def get_screen_bridge() -> Optional[ScreenAnalyzerBridge]:
    """Get the current screen analyzer bridge."""
    return _screen_bridge


async def connect_decision_engine(decision_engine: Any) -> DecisionEngineBridge:
    """
    Connect a decision engine to AGI OS.

    Args:
        decision_engine: AutonomousDecisionEngine instance

    Returns:
        DecisionEngineBridge instance
    """
    global _decision_bridge

    if _decision_bridge is None:
        _decision_bridge = DecisionEngineBridge()

    await _decision_bridge.connect(decision_engine)
    return _decision_bridge


async def integrate_voice_systems() -> VoiceSystemBridge:
    """
    Integrate voice systems with AGI OS.

    Returns:
        VoiceSystemBridge instance
    """
    global _voice_bridge

    if _voice_bridge is None:
        _voice_bridge = VoiceSystemBridge()

    await _voice_bridge.connect()
    return _voice_bridge


async def integrate_approval_systems(
    permission_manager: Optional[Any] = None
) -> PermissionSystemBridge:
    """
    Integrate approval systems with AGI OS.

    Args:
        permission_manager: Optional existing PermissionManager

    Returns:
        PermissionSystemBridge instance
    """
    global _permission_bridge

    if _permission_bridge is None:
        _permission_bridge = PermissionSystemBridge()

    await _permission_bridge.connect(permission_manager)
    return _permission_bridge


async def connect_neural_mesh(neural_mesh: Any) -> NeuralMeshBridge:
    """
    Connect Neural Mesh to AGI OS.

    Args:
        neural_mesh: NeuralMeshCoordinator instance

    Returns:
        NeuralMeshBridge instance
    """
    global _mesh_bridge

    if _mesh_bridge is None:
        _mesh_bridge = NeuralMeshBridge()

    await _mesh_bridge.connect(neural_mesh)
    return _mesh_bridge


async def get_event_bridge() -> Optional[NeuralMeshBridge]:
    """Get the current event bridge instance (v237.2)."""
    return _mesh_bridge


async def get_unified_vision() -> UnifiedVisionInterface:
    """
    Get the unified vision interface.

    Returns:
        UnifiedVisionInterface instance
    """
    global _unified_vision

    if _unified_vision is None:
        _unified_vision = UnifiedVisionInterface()
        await _unified_vision.initialize()

    return _unified_vision


async def integrate_all(
    screen_analyzer: Optional[Any] = None,
    decision_engine: Optional[Any] = None,
    permission_manager: Optional[Any] = None,
    neural_mesh: Optional[Any] = None,
    enable_claude_vision: bool = False,
) -> Dict[str, Any]:
    """
    Integrate all available systems with AGI OS.

    Args:
        screen_analyzer: Optional MemoryAwareScreenAnalyzer
        decision_engine: Optional AutonomousDecisionEngine
        permission_manager: Optional PermissionManager
        neural_mesh: Optional NeuralMeshCoordinator
        enable_claude_vision: Enable Claude Vision for screen analysis

    Returns:
        Dictionary of bridge instances
    """
    bridges = {}

    # Voice (always integrate)
    bridges['voice'] = await integrate_voice_systems()

    # Approval (always integrate)
    bridges['approval'] = await integrate_approval_systems(permission_manager)

    # Screen analyzer with vision
    if screen_analyzer:
        bridges['screen'] = await connect_screen_analyzer(
            screen_analyzer,
            enable_claude_vision=enable_claude_vision,
        )

    # Decision engine
    if decision_engine:
        bridges['decision'] = await connect_decision_engine(decision_engine)

    # Neural Mesh
    if neural_mesh:
        bridges['mesh'] = await connect_neural_mesh(neural_mesh)

    # Unified vision interface
    bridges['vision'] = await get_unified_vision()

    logger.info("Integrated %d systems with AGI OS", len(bridges))
    return bridges


# ============== Testing ==============

if __name__ == "__main__":
    async def test():
        """Test the enhanced integration."""
        print("Testing Enhanced JARVIS Integration...")

        # Test screen bridge
        bridge = ScreenAnalyzerBridge()
        print(f"Screen bridge created with {len(bridge._detection_patterns)} patterns")

        # List patterns
        print("\nDefault detection patterns:")
        for pattern_id, pattern in bridge.get_patterns().items():
            print(f"  - {pattern.name}: {pattern.description}")

        # Test unified vision
        vision = UnifiedVisionInterface()
        await vision.initialize(bridge)

        context = await vision.get_current_context()
        print(f"\nCurrent context: {context}")

        stats = vision.get_stats()
        print(f"\nStats: {stats}")

        print("\nTest complete!")

    asyncio.run(test())
