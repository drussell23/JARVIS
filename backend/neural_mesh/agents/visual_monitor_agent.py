"""
JARVIS Neural Mesh - Visual Monitor Agent v14.0
===============================================

The "Watcher & Actor" of Video Multi-Space Intelligence (VMSI) - Ferrari Engine Edition.

This agent provides GPU-ACCELERATED visual surveillance capabilities:
- 🏎️  Ferrari Engine: 60 FPS ScreenCaptureKit (adaptive, GPU-accelerated)
- Watch background windows for specific events (OCR-based detection)
- AUTOMATICALLY ACT when events are detected (v11.0 Watch & Act)
- Monitor multiple windows in parallel ("God Mode")
- Alert when visual events are detected
- Execute Computer Use actions in response to visual triggers
- Support conditional branching (if Error -> Retry, if Success -> Deploy)
- Integrate with SpatialAwarenessAgent for window location
- Share state across repos (JARVIS ↔ JARVIS Prime ↔ Reactor Core)
- 👻 Ghost Hands: Cross-space actions without focus stealing (v13.0)
- 🗣️ Working Out Loud: Real-time narration during monitoring (v14.0)

v14.0 WORKING OUT LOUD - TRANSPARENT CO-PILOT MODE:
- Heartbeat narration: Regular status updates during monitoring
- Near-miss narration: Alert when interesting but non-matching text is detected
- Activity narration: Announce significant screen changes
- Intelligent debouncing: Prevents spam with rate limiting and content similarity checks
- Configurable verbosity: minimal, normal, verbose, debug
- Environment variable control: JARVIS_WORKING_OUT_LOUD, JARVIS_HEARTBEAT_INTERVAL, etc.

v13.0 GHOST HANDS INTEGRATION:
- Cross-space action execution via YabaiAwareActuator
- Zero focus stealing: User stays on their current space
- Surgical window targeting via window_id from vision detection

v12.0 FERRARI ENGINE INTEGRATION:
- Direct VideoWatcher usage (bypasses legacy VideoWatcherManager)
- Automatic ScreenCaptureKit selection for window-specific capture
- Adaptive FPS (10-15 FPS for static content, scales to 60 FPS for dynamic)
- GPU-accelerated Metal texture handling (zero-copy)
- Intelligent window discovery via fast_capture (Ferrari Engine)
- Fallback chain: Ferrari Engine → SpatialAwareness → Legacy

Capabilities:
- watch_and_alert: Monitor a window for text/event and alert
- watch_and_act: Monitor AND execute actions when event detected
- watch_multiple: Monitor multiple windows in parallel (God Mode)
- stop_watching: Cancel active watchers
- list_watchers: Get status of all active watchers

Usage from voice:
    Passive:
    "Watch the Terminal for 'Build Successful'"

    Active (Watch & Act):
    "Watch the Terminal for 'Build Complete', then click Deploy"
    "When you see 'Error' in Terminal, click Retry button"

    God Mode (Multi-Watch):
    "Watch Chrome for 'Application Submitted' and Terminal for 'Error'"

    Working Out Loud (v14.0):
    JARVIS will periodically narrate: "Still watching Chrome for 'Build Complete'. 45 seconds in."
    JARVIS will announce near-misses: "I see 'Build Started' but waiting for 'Build Complete'."

This is JARVIS's "60 FPS eyes AND autonomous hands" - GPU-accelerated surveillance
with intelligent action execution and transparent co-pilot narration. Clinical-grade engineering.

Author: JARVIS AI System
Version: 14.0 - Working Out Loud (Transparent Co-Pilot Mode)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeType,
    MessageType,
    MessagePriority,
)

logger = logging.getLogger(__name__)

# =====================================================================
# ROOT CAUSE FIX: Robust Real-Time Detection Notifications v2.0.0
# =====================================================================
# Import UnifiedVoiceOrchestrator for consistent TTS across all components
# Import WebSocket manager for real-time frontend notifications
# =====================================================================
try:
    from backend.core.supervisor.unified_voice_orchestrator import (
        get_voice_orchestrator,
        VoicePriority,
        VoiceSource,
        SpeechTopic
    )
    VOICE_ORCHESTRATOR_AVAILABLE = True
    logger.info("[VisualMonitor] ✅ UnifiedVoiceOrchestrator available")
except ImportError as e:
    VOICE_ORCHESTRATOR_AVAILABLE = False
    logger.warning(f"[VisualMonitor] UnifiedVoiceOrchestrator not available: {e}")

try:
    from backend.api.unified_websocket import get_ws_manager
    WEBSOCKET_MANAGER_AVAILABLE = True
    logger.info("[VisualMonitor] ✅ WebSocket Manager available for real-time notifications")
except ImportError as e:
    WEBSOCKET_MANAGER_AVAILABLE = False
    logger.warning(f"[VisualMonitor] WebSocket Manager not available: {e}")

# Multi-space window detection for "God Mode" parallel watching
try:
    from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector
    MULTI_SPACE_AVAILABLE = True
except ImportError:
    MULTI_SPACE_AVAILABLE = False
    logger.warning("MultiSpaceWindowDetector not available - multi-space watching disabled")


# =============================================================================
# Action Configuration - "Watch & Act" Capabilities (v11.0)
# =============================================================================

class ActionType(str, Enum):
    """Types of actions that can be executed in response to visual events."""
    # Core action types
    SIMPLE_GOAL = "simple_goal"  # Natural language goal for Computer Use
    CONDITIONAL = "conditional"  # Conditional branching (if X -> do Y)
    WORKFLOW = "workflow"  # Complex multi-step workflow via AgenticTaskRunner
    NOTIFICATION = "notification"  # Just notify (passive mode)
    VOICE_ALERT = "voice_alert"  # Voice alert only
    GHOST_HANDS = "ghost_hands"  # Cross-space action via Ghost Hands (ZERO focus stealing!)

    # Extended action types (for action_config dict compatibility)
    COMPUTER_USE = "computer_use"  # Generic computer use action
    CLICK = "click"  # Click action
    TYPE = "type"  # Type/keyboard action
    EXECUTE = "execute"  # Execute command/script


@dataclass
class ActionConfig:
    """
    Configuration for actions to execute when visual event detected.

    Supports multiple action types from simple to complex:
    - Simple: Natural language goal executed via Computer Use
    - Conditional: If-then-else branching based on trigger
    - Workflow: Complex multi-step via AgenticTaskRunner
    """
    action_type: ActionType = ActionType.NOTIFICATION

    # Simple goal (for SIMPLE_GOAL)
    goal: Optional[str] = None  # e.g., "Click the Deploy button"

    # Conditional branching (for CONDITIONAL)
    conditions: List['ConditionalAction'] = field(default_factory=list)
    default_action: Optional[str] = None  # Fallback if no conditions match

    # Workflow (for WORKFLOW)
    workflow_goal: Optional[str] = None  # Complex goal for AgenticTaskRunner
    workflow_context: Dict[str, Any] = field(default_factory=dict)

    # Ghost Hands (for GHOST_HANDS) - ZERO FOCUS STEALING!
    ghost_hands_actions: List[Dict[str, Any]] = field(default_factory=list)  # List of GhostAction configs
    ghost_hands_coordinates: Optional[tuple] = None  # (x, y) for click actions
    ghost_hands_element: Optional[str] = None  # Element selector/title to click
    preserve_user_focus: bool = True  # Always preserve user's current focus

    # Common settings
    switch_to_window: bool = True  # Switch to window before acting (ignored for GHOST_HANDS)
    narrate: bool = True  # Voice narration during execution
    require_confirmation: bool = False  # Ask user before acting
    timeout_seconds: float = 30.0  # Max time for action execution


@dataclass
class ConditionalAction:
    """
    Conditional action: If trigger matches pattern, execute action.

    Example:
        ConditionalAction(
            trigger_pattern="Error",
            action_goal="Click the Retry button",
            description="Retry on error"
        )
    """
    trigger_pattern: str  # Text pattern to match (supports regex)
    action_goal: str  # What to do if pattern matches
    description: str = ""  # Human-readable description
    confidence_threshold: float = 0.75  # Min confidence to trigger
    case_sensitive: bool = False
    use_regex: bool = False


@dataclass
class WatchAndActRequest:
    """
    Complete request for watch-and-act operation.

    Combines visual monitoring with automated response.
    """
    # Monitoring config
    app_name: str
    trigger_text: str
    space_id: Optional[int] = None

    # Action config
    action_config: Optional[ActionConfig] = None

    # Monitoring settings
    fps: int = 5
    timeout: float = 300.0  # 5 minutes default

    # Advanced
    multiple_triggers: List[str] = field(default_factory=list)  # Watch for any of these
    stop_on_first: bool = True  # Stop after first trigger


@dataclass
class ActionExecutionResult:
    """Result of action execution after visual event detected."""
    success: bool
    action_type: ActionType
    goal_executed: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    computer_use_result: Optional[Dict[str, Any]] = None  # Full Computer Use result
    narration: List[str] = field(default_factory=list)


@dataclass
class VisualMonitorConfig:
    """
    Configuration for Visual Monitor Agent.

    Inherits base agent configuration from BaseAgentConfig via composition.
    """
    # Base agent configuration
    heartbeat_interval_seconds: float = 10.0
    message_queue_size: int = 1000
    message_handler_timeout_seconds: float = 10.0
    enable_knowledge_access: bool = True
    knowledge_cache_size: int = 100
    log_messages: bool = True
    log_level: str = "INFO"

    # Visual monitoring specific
    default_fps: int = 5  # Default FPS for watchers
    default_timeout: float = 300.0  # Default timeout (5 minutes)
    max_parallel_watchers: int = 3  # Max simultaneous watchers (legacy)
    enable_voice_alerts: bool = True  # Speak when events detected
    enable_notifications: bool = True  # macOS notifications
    enable_cross_repo_sync: bool = True  # Share state across repos

    # v13.0: God Mode - Multi-Space Parallel Watching
    multi_space_enabled: bool = True  # Enable multi-space detection
    max_multi_space_watchers: int = 20  # Safety limit for God Mode
    auto_space_switch: bool = True  # Automatically switch to detected space
    watcher_coordination_timeout: float = 300.0  # Max time for parallel watchers
    ferrari_fps: int = 60  # Ferrari Engine target FPS (GPU-accelerated ScreenCaptureKit)

    # Cross-repo paths
    cross_repo_dir: str = "~/.jarvis/cross_repo"
    vmsi_state_file: str = "vmsi_state.json"
    sync_interval_seconds: float = 5.0  # How often to sync state

    # v11.0: Action execution ("Watch & Act")
    enable_action_execution: bool = True  # Execute actions on events
    enable_computer_use: bool = True  # Use Computer Use for actions
    enable_agentic_runner: bool = True  # Use AgenticTaskRunner for workflows
    action_timeout_seconds: float = 60.0  # Max time for action execution
    require_confirmation: bool = False  # Ask before executing actions
    auto_switch_to_window: bool = True  # Automatically switch to target window

    # v14.0: "Working Out Loud" - Transparent Co-Pilot Mode
    # Turns JARVIS from a silent sentinel into an active co-pilot
    working_out_loud_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_WORKING_OUT_LOUD", "true").lower() == "true"
    )
    # Heartbeat: Regular status updates during monitoring
    heartbeat_narration_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_HEARTBEAT_INTERVAL", "30"))
    )
    # v15.0: First heartbeat comes earlier for immediate feedback
    first_heartbeat_delay_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_FIRST_HEARTBEAT_DELAY", "10"))
    )
    # Near-miss: When we see interesting text but not the trigger
    near_miss_narration_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NEAR_MISS_NARRATION", "true").lower() == "true"
    )
    near_miss_cooldown_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_NEAR_MISS_COOLDOWN", "60"))
    )
    # Activity: When significant screen changes are detected
    activity_narration_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_ACTIVITY_NARRATION", "true").lower() == "true"
    )
    activity_cooldown_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_ACTIVITY_COOLDOWN", "15"))
    )
    # Verbosity levels: minimal, normal, verbose, debug
    narration_verbosity: str = field(
        default_factory=lambda: os.getenv("JARVIS_NARRATION_VERBOSITY", "normal")
    )
    # Max narrations per minute (spam protection)
    max_narrations_per_minute: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MAX_NARRATIONS_PER_MIN", "6"))
    )


# =============================================================================
# v14.0: "Working Out Loud" - Narration State Management
# =============================================================================

@dataclass
class NarrationState:
    """
    Intelligent narration state tracker for "Working Out Loud" feature.

    Prevents spam while ensuring meaningful updates reach the user.
    Uses adaptive debouncing based on:
    - Time since last narration
    - Narration type (heartbeat vs near-miss vs activity vs startup)
    - Content similarity (don't repeat similar messages)
    - User activity patterns

    v15.0: Enhanced transparency - startup confirmation and early heartbeats
    """
    # Timing state
    last_heartbeat_time: float = 0.0
    last_near_miss_time: float = 0.0
    last_activity_time: float = 0.0
    last_any_narration_time: float = 0.0
    last_startup_time: float = 0.0  # v15.0: Track startup narration

    # v15.0: Startup confirmation state
    startup_confirmed: bool = False  # Whether initial startup was narrated
    monitoring_start_time: float = 0.0  # When monitoring started

    # Content tracking (to avoid repetition)
    last_near_miss_text: str = ""
    last_activity_description: str = ""

    # Rate limiting
    narrations_this_minute: int = 0
    minute_start_time: float = 0.0

    # Consecutive tracking (to detect when user might want silence)
    consecutive_heartbeats: int = 0
    consecutive_near_misses: int = 0

    # Detection state for intelligent narration
    frames_since_last_change: int = 0
    last_ocr_text_hash: int = 0
    interesting_keywords_seen: List[str] = field(default_factory=list)

    def can_narrate(self, narration_type: str, config: VisualMonitorConfig) -> bool:
        """
        Check if narration is allowed based on rate limiting and cooldowns.

        Args:
            narration_type: "heartbeat", "near_miss", "activity", "startup", or "error"
            config: VisualMonitorConfig with timing parameters

        Returns:
            True if narration is allowed

        v15.0: Added "startup" and "error" types that bypass MIN_GAP for transparency
        """
        import time
        now = time.time()

        # Reset minute counter if needed
        if now - self.minute_start_time >= 60:
            self.narrations_this_minute = 0
            self.minute_start_time = now

        # Global rate limit (higher limit for startup/error)
        if narration_type in ("startup", "error"):
            # Critical narrations allowed even near rate limit
            if self.narrations_this_minute >= config.max_narrations_per_minute + 2:
                return False
        elif self.narrations_this_minute >= config.max_narrations_per_minute:
            return False

        # v15.0: Startup and error types bypass MIN_GAP - these are critical for transparency
        if narration_type in ("startup", "error"):
            # Only check cooldown since last startup/error, not general MIN_GAP
            if narration_type == "startup":
                return now - self.last_startup_time >= 2.0  # Allow after 2s cooldown
            else:  # error
                return True  # Errors always allowed (critical feedback)

        # Minimum gap between regular narrations (prevents overwhelming user)
        MIN_GAP_SECONDS = 5.0
        if now - self.last_any_narration_time < MIN_GAP_SECONDS:
            return False

        # Type-specific cooldowns
        if narration_type == "heartbeat":
            # v15.0: First heartbeat comes earlier (10s), subsequent at normal interval (30s)
            if not self.startup_confirmed:
                # Before startup confirmation, allow heartbeat after 10s
                first_heartbeat_delay = getattr(config, 'first_heartbeat_delay_seconds', 10.0)
                return now - self.monitoring_start_time >= first_heartbeat_delay
            else:
                return now - self.last_heartbeat_time >= config.heartbeat_narration_interval
        elif narration_type == "near_miss":
            return (config.near_miss_narration_enabled and
                    now - self.last_near_miss_time >= config.near_miss_cooldown_seconds)
        elif narration_type == "activity":
            return (config.activity_narration_enabled and
                    now - self.last_activity_time >= config.activity_cooldown_seconds)

        return True

    def record_narration(self, narration_type: str, content: str = "") -> None:
        """Record that a narration was made (for rate limiting).

        v15.0: Added startup and error types for transparency
        """
        import time
        now = time.time()

        self.last_any_narration_time = now
        self.narrations_this_minute += 1

        if narration_type == "heartbeat":
            self.last_heartbeat_time = now
            self.consecutive_heartbeats += 1
            self.consecutive_near_misses = 0
        elif narration_type == "near_miss":
            self.last_near_miss_time = now
            self.last_near_miss_text = content
            self.consecutive_near_misses += 1
            self.consecutive_heartbeats = 0
        elif narration_type == "activity":
            self.last_activity_time = now
            self.last_activity_description = content
            self.consecutive_heartbeats = 0
            self.consecutive_near_misses = 0
        elif narration_type == "startup":
            # v15.0: Startup confirmation
            self.last_startup_time = now
            self.startup_confirmed = True
            self.consecutive_heartbeats = 0
            self.consecutive_near_misses = 0
        elif narration_type == "error":
            # v15.0: Error narration - doesn't affect other counters
            pass

    def is_similar_content(self, content: str, narration_type: str) -> bool:
        """Check if content is too similar to recent narrations (avoid repetition)."""
        if narration_type == "near_miss":
            # Simple similarity check - could be enhanced with embeddings
            return content.lower().strip() == self.last_near_miss_text.lower().strip()
        elif narration_type == "activity":
            return content.lower().strip() == self.last_activity_description.lower().strip()
        return False


class VisualMonitorAgent(BaseNeuralMeshAgent):
    """
    Visual Monitor Agent - The "Watcher" of VMSI.

    Provides background visual surveillance for specific windows.

    Capabilities:
    - watch_and_alert: Monitor a window and alert on event
    - watch_multiple: Monitor multiple windows in parallel
    - stop_watching: Stop specific watcher
    - list_watchers: List all active watchers

    Example:
        result = await coordinator.request(
            to_agent="visual_monitor_agent",
            payload={
                "action": "watch_and_alert",
                "app_name": "Terminal",
                "trigger_text": "Build Successful"
            }
        )
    """

    def __init__(self, config: Optional[VisualMonitorConfig] = None) -> None:
        """Initialize the Visual Monitor Agent."""
        super().__init__(
            agent_name="visual_monitor_agent",
            agent_type="visual_monitor",
            capabilities={
                "watch_and_alert",
                "watch_and_act",  # v11.0: Active response
                "watch_multiple",
                "stop_watching",
                "list_watchers",
                "get_watcher_stats",
                "background_surveillance",  # Meta capability
                "autonomous_response",  # v11.0: Act on visual events
                "ferrari_engine",  # v12.0: GPU-accelerated 60 FPS capture
                "god_mode_surveillance",  # v12.0: Multi-window parallel monitoring
                "ghost_hands",  # v13.0: Cross-space actions without focus stealing
                "working_out_loud",  # v14.0: Real-time narration during monitoring
            },
            version="14.0",
        )

        self.config = config or VisualMonitorConfig()

        # Lazy-loaded components - Visual
        self._watcher_manager = None  # Legacy - deprecated in favor of direct VideoWatcher
        self._detector = None
        self._spatial_agent = None

        # v12.0: Direct VideoWatcher management (Ferrari Engine)
        self._active_video_watchers: Dict[str, Any] = {}  # watcher_id -> VideoWatcher instance
        self._fast_capture_engine = None  # For window discovery

        # Lazy-loaded components - Action Execution (v11.0)
        self._computer_use_connector = None  # For executing actions
        self._agentic_task_runner = None  # For complex workflows
        self._tts_callback = None  # For voice narration during monitoring

        # Active monitoring tasks
        self._watch_tasks: Dict[str, asyncio.Task] = {}
        self._active_watchers: Dict[str, Any] = {}  # watcher_id -> watcher_info

        # Stats
        self._total_watches_started = 0
        self._total_events_detected = 0
        self._total_alerts_sent = 0
        self._total_actions_executed = 0  # NEW v11.0
        self._total_actions_succeeded = 0  # NEW v11.0
        self._total_actions_failed = 0  # NEW v11.0

        # Cross-repo state
        self._state_sync_task: Optional[asyncio.Task] = None

        # Purple indicator session (for visual feedback that JARVIS is watching)
        self._indicator_session = None

        # v14.0: "Working Out Loud" - Narration state per watcher
        self._narration_states: Dict[str, NarrationState] = {}

    # =========================================================================
    # Helper Methods for Non-Blocking Initialization
    # =========================================================================

    def _create_spatial_agent_sync(self):
        """
        Synchronously create SpatialAwarenessAgent instance.

        This runs in a thread executor to prevent Yabai from blocking
        the main event loop during import/instantiation.

        Returns:
            SpatialAwarenessAgent instance or None if failed
        """
        try:
            from backend.neural_mesh.agents.spatial_awareness_agent import SpatialAwarenessAgent
            return SpatialAwarenessAgent()
        except Exception as e:
            logger.warning(f"Failed to create SpatialAwarenessAgent: {e}")
            return None

    async def _ensure_purple_indicator(self) -> bool:
        """
        Force macOS to show the purple recording indicator in the menu bar.

        WHY THIS MATTERS:
        ScreenCaptureKit (Ferrari Engine) does NOT trigger the purple icon by default
        because it captures window content directly without going through the
        traditional screen recording APIs.

        This creates a UX problem: the user says "Watch Chrome" but sees no visual
        confirmation that JARVIS is actually watching.

        CRITICAL INSIGHT:
        An empty AVCaptureSession WITHOUT input won't trigger the purple icon!
        macOS is smart enough to know nothing is being captured. We MUST attach
        a valid screen input to force the indicator to appear.

        SOLUTION:
        1. Create AVCaptureSession with minimal preset
        2. Attach AVCaptureScreenInput for the main display
        3. Set extremely low framerate (1 FPS) to minimize resource usage
        4. We only need the purple light, not the actual video frames

        Returns:
            True if indicator activated, False otherwise
        """
        try:
            # Check if already active
            if self._indicator_session is not None:
                logger.debug("🟣 Purple indicator already active")
                return True

            loop = asyncio.get_event_loop()

            def _start_indicator_session_with_input():
                """
                Start AVCaptureSession WITH screen input (required for purple light).

                An empty session won't trigger the indicator - macOS optimizes it away.
                We must attach actual screen capture input, but at minimal framerate.
                """
                try:
                    import AVFoundation
                    from Quartz import CGMainDisplayID
                    from CoreMedia import CMTimeMake

                    # 1. Create Session with minimal preset
                    session = AVFoundation.AVCaptureSession.alloc().init()

                    # Use low preset if available (reduces memory/CPU)
                    if hasattr(AVFoundation, 'AVCaptureSessionPresetLow'):
                        session.setSessionPreset_(AVFoundation.AVCaptureSessionPresetLow)

                    # 2. Create Screen Input for main display
                    # This is REQUIRED - without input, no purple light!
                    display_id = CGMainDisplayID()
                    screen_input = AVFoundation.AVCaptureScreenInput.alloc().initWithDisplayID_(display_id)

                    if screen_input is None:
                        logger.warning("Failed to create AVCaptureScreenInput")
                        return None

                    # 3. Configure for MINIMAL resource usage
                    # We only want the purple light, not actual video capture
                    # Set to 1 FPS (1 frame per second) - absolute minimum
                    try:
                        # CMTimeMake(value, timescale) = value/timescale seconds per frame
                        # CMTimeMake(1, 1) = 1 second per frame = 1 FPS
                        min_frame_duration = CMTimeMake(1, 1)
                        screen_input.setMinFrameDuration_(min_frame_duration)
                    except Exception as e:
                        logger.debug(f"Could not set min frame duration: {e}")
                        # Continue anyway - framerate is just an optimization

                    # Disable cursor capture to reduce overhead
                    try:
                        screen_input.setCapturesCursor_(False)
                    except Exception:
                        pass  # Not critical

                    # Disable mouse clicks capture
                    try:
                        screen_input.setCapturesMouseClicks_(False)
                    except Exception:
                        pass  # Not critical

                    # 4. Add input to session (THIS triggers the purple light)
                    if session.canAddInput_(screen_input):
                        session.addInput_(screen_input)
                    else:
                        logger.warning("Cannot add screen input to indicator session")
                        return None

                    # 5. Start the session - purple light should appear NOW
                    session.startRunning()

                    # Verify session is actually running
                    if session.isRunning():
                        return session
                    else:
                        logger.warning("Indicator session failed to start running")
                        return None

                except ImportError as e:
                    logger.debug(f"AVFoundation/Quartz not available: {e}")
                    return None
                except Exception as e:
                    logger.debug(f"Purple indicator session failed: {e}")
                    return None

            # Run in executor with timeout (AVFoundation init can be slow)
            indicator_timeout = float(os.getenv('JARVIS_INDICATOR_TIMEOUT', '5.0'))

            try:
                self._indicator_session = await asyncio.wait_for(
                    loop.run_in_executor(None, _start_indicator_session_with_input),
                    timeout=indicator_timeout
                )

                if self._indicator_session:
                    logger.info("🟣 Purple Video Indicator ACTIVATED - JARVIS is watching!")
                    return True
                else:
                    logger.debug("Purple indicator not available (AVFoundation/permissions may be missing)")
                    return False

            except asyncio.TimeoutError:
                logger.debug(f"Purple indicator timed out after {indicator_timeout}s")
                return False

        except Exception as e:
            logger.debug(f"Purple indicator error: {e}")
            return False

    async def _stop_purple_indicator(self):
        """Stop the purple indicator session when monitoring ends."""
        if self._indicator_session:
            try:
                loop = asyncio.get_event_loop()

                def _stop_session():
                    try:
                        self._indicator_session.stopRunning()
                    except Exception:
                        pass

                await loop.run_in_executor(None, _stop_session)
                self._indicator_session = None
                logger.debug("🟣 Purple indicator deactivated")
            except Exception as e:
                logger.debug(f"Error stopping purple indicator: {e}")

    async def on_initialize(self) -> None:
        """
        Initialize agent resources with PARALLEL NON-BLOCKING execution.

        v15.0 PARALLEL INITIALIZATION:
        ==============================
        All components now initialize CONCURRENTLY instead of sequentially.
        This reduces startup time from ~30s (sum of all) to ~5s (max of any single).

        Each component:
        - Runs in a thread executor (for C++/blocking calls)
        - Has individual timeout protection
        - Gracefully degrades on failure (partial success is acceptable)
        - Logs detailed status for debugging

        Architecture:
        1. Define wrapper tasks that catch individual failures
        2. Launch ALL tasks concurrently with asyncio.create_task()
        3. Use asyncio.gather() with return_exceptions=True
        4. Report individual component status after parallel completion
        """
        import time as time_module
        start_time = time_module.time()

        logger.info("🚀 [VisualMonitor] Starting PARALLEL initialization v15.0...")

        # Get event loop for executor calls
        loop = asyncio.get_event_loop()

        # Configurable timeouts from environment (no hardcoding)
        ferrari_init_timeout = float(os.getenv('JARVIS_FERRARI_INIT_TIMEOUT', '5.0'))
        watcher_mgr_init_timeout = float(os.getenv('JARVIS_WATCHER_MGR_INIT_TIMEOUT', '3.0'))
        detector_init_timeout = float(os.getenv('JARVIS_DETECTOR_INIT_TIMEOUT', '5.0'))
        computer_use_init_timeout = float(os.getenv('JARVIS_COMPUTER_USE_INIT_TIMEOUT', '3.0'))
        agentic_runner_init_timeout = float(os.getenv('JARVIS_AGENTIC_RUNNER_INIT_TIMEOUT', '3.0'))
        spatial_agent_init_timeout = float(os.getenv('JARVIS_SPATIAL_AGENT_INIT_TIMEOUT', '5.0'))

        # Overall parallel timeout - max of individual + buffer
        parallel_timeout = float(os.getenv('JARVIS_PARALLEL_INIT_TIMEOUT', '10.0'))

        # Track component status for final report
        component_status = {
            "ferrari_engine": {"success": False, "error": None, "duration": 0.0},
            "watcher_manager": {"success": False, "error": None, "duration": 0.0},
            "detector": {"success": False, "error": None, "duration": 0.0},
            "computer_use": {"success": False, "error": None, "duration": 0.0, "skipped": not self.config.enable_computer_use},
            "agentic_runner": {"success": False, "error": None, "duration": 0.0, "skipped": not self.config.enable_agentic_runner},
            "spatial_agent": {"success": False, "error": None, "duration": 0.0},
        }

        # =====================================================================
        # PARALLEL INITIALIZATION TASKS - Each wrapped with error handling
        # =====================================================================

        async def init_ferrari_engine():
            """Ferrari Engine (GPU-accelerated window capture) - Non-blocking."""
            comp_start = time_module.time()
            try:
                import sys
                from pathlib import Path
                native_ext_path = Path(__file__).parent.parent.parent / "native_extensions"
                if str(native_ext_path) not in sys.path:
                    sys.path.insert(0, str(native_ext_path))

                def _init_fast_capture():
                    import fast_capture
                    return fast_capture.FastCaptureEngine()

                self._fast_capture_engine = await asyncio.wait_for(
                    loop.run_in_executor(None, _init_fast_capture),
                    timeout=ferrari_init_timeout
                )
                component_status["ferrari_engine"]["success"] = True
                component_status["ferrari_engine"]["duration"] = time_module.time() - comp_start
                logger.info("✅ Ferrari Engine Ready")

            except asyncio.TimeoutError:
                component_status["ferrari_engine"]["error"] = f"timeout ({ferrari_init_timeout}s)"
                component_status["ferrari_engine"]["duration"] = time_module.time() - comp_start
                logger.warning(f"⚠️ Ferrari Engine timeout after {ferrari_init_timeout}s - using fallback")
                self._fast_capture_engine = None
            except Exception as e:
                component_status["ferrari_engine"]["error"] = str(e)
                component_status["ferrari_engine"]["duration"] = time_module.time() - comp_start
                logger.warning(f"⚠️ Ferrari Engine failed: {e}")
                self._fast_capture_engine = None

        async def init_watcher_manager():
            """Legacy VideoWatcherManager - Non-blocking fallback."""
            comp_start = time_module.time()
            try:
                def _init_watcher_manager():
                    from backend.vision.macos_video_capture_advanced import get_watcher_manager
                    return get_watcher_manager()

                self._watcher_manager = await asyncio.wait_for(
                    loop.run_in_executor(None, _init_watcher_manager),
                    timeout=watcher_mgr_init_timeout
                )
                component_status["watcher_manager"]["success"] = True
                component_status["watcher_manager"]["duration"] = time_module.time() - comp_start
                logger.info("✅ Watcher Manager Ready")

            except asyncio.TimeoutError:
                component_status["watcher_manager"]["error"] = f"timeout ({watcher_mgr_init_timeout}s)"
                component_status["watcher_manager"]["duration"] = time_module.time() - comp_start
                logger.debug(f"Legacy VideoWatcherManager timeout - not critical")
                self._watcher_manager = None
            except Exception as e:
                component_status["watcher_manager"]["error"] = str(e)
                component_status["watcher_manager"]["duration"] = time_module.time() - comp_start
                logger.debug(f"Legacy VideoWatcherManager failed: {e}")
                self._watcher_manager = None

        async def init_detector():
            """OCR Detector - Non-blocking (can be slow due to model loading)."""
            comp_start = time_module.time()
            try:
                def _init_detector():
                    from backend.vision.visual_event_detector import create_detector
                    return create_detector()

                self._detector = await asyncio.wait_for(
                    loop.run_in_executor(None, _init_detector),
                    timeout=detector_init_timeout
                )
                component_status["detector"]["success"] = True
                component_status["detector"]["duration"] = time_module.time() - comp_start
                logger.info("✅ OCR Detector Ready")

            except asyncio.TimeoutError:
                component_status["detector"]["error"] = f"timeout ({detector_init_timeout}s)"
                component_status["detector"]["duration"] = time_module.time() - comp_start
                logger.warning(f"VisualEventDetector timeout after {detector_init_timeout}s")
                self._detector = None
            except Exception as e:
                component_status["detector"]["error"] = str(e)
                component_status["detector"]["duration"] = time_module.time() - comp_start
                logger.warning(f"VisualEventDetector failed: {e}")
                self._detector = None

        async def init_computer_use():
            """Computer Use Connector - Non-blocking (enables Watch & Act)."""
            if not self.config.enable_computer_use:
                return  # Skip if disabled

            comp_start = time_module.time()
            try:
                def _init_computer_use():
                    from backend.display.computer_use_connector import get_computer_use_connector
                    return get_computer_use_connector()

                self._computer_use_connector = await asyncio.wait_for(
                    loop.run_in_executor(None, _init_computer_use),
                    timeout=computer_use_init_timeout
                )

                # Extract TTS callback for voice narration
                if hasattr(self._computer_use_connector, 'narrator') and hasattr(self._computer_use_connector.narrator, 'tts_callback'):
                    self._tts_callback = self._computer_use_connector.narrator.tts_callback

                component_status["computer_use"]["success"] = True
                component_status["computer_use"]["duration"] = time_module.time() - comp_start
                logger.info("✅ Computer Use Ready")

            except asyncio.TimeoutError:
                component_status["computer_use"]["error"] = f"timeout ({computer_use_init_timeout}s)"
                component_status["computer_use"]["duration"] = time_module.time() - comp_start
                logger.warning(f"ComputerUseConnector timeout - Watch & Act limited to passive mode")
                self._computer_use_connector = None
            except Exception as e:
                component_status["computer_use"]["error"] = str(e)
                component_status["computer_use"]["duration"] = time_module.time() - comp_start
                logger.warning(f"ComputerUseConnector failed: {e}")
                self._computer_use_connector = None

        async def init_agentic_runner():
            """AgenticTaskRunner - Non-blocking (enables complex workflows)."""
            if not self.config.enable_agentic_runner:
                return  # Skip if disabled

            comp_start = time_module.time()
            try:
                def _init_agentic_runner():
                    from backend.core.agentic_task_runner import get_agentic_runner
                    return get_agentic_runner()

                self._agentic_task_runner = await asyncio.wait_for(
                    loop.run_in_executor(None, _init_agentic_runner),
                    timeout=agentic_runner_init_timeout
                )

                if self._agentic_task_runner:
                    component_status["agentic_runner"]["success"] = True
                    component_status["agentic_runner"]["duration"] = time_module.time() - comp_start
                    logger.info("✅ Agentic Runner Ready")
                else:
                    component_status["agentic_runner"]["error"] = "returned None"
                    component_status["agentic_runner"]["duration"] = time_module.time() - comp_start
                    logger.warning("AgenticTaskRunner returned None - using Computer Use fallback")

            except asyncio.TimeoutError:
                component_status["agentic_runner"]["error"] = f"timeout ({agentic_runner_init_timeout}s)"
                component_status["agentic_runner"]["duration"] = time_module.time() - comp_start
                logger.warning(f"AgenticTaskRunner timeout - using Computer Use fallback")
                self._agentic_task_runner = None
            except Exception as e:
                component_status["agentic_runner"]["error"] = str(e)
                component_status["agentic_runner"]["duration"] = time_module.time() - comp_start
                logger.warning(f"AgenticTaskRunner failed: {e}")
                self._agentic_task_runner = None

        async def init_spatial_agent():
            """SpatialAwarenessAgent - Non-blocking (Yabai can hard-block)."""
            comp_start = time_module.time()
            try:
                # Phase 1: Create agent (sync, in executor)
                self.spatial_agent = await asyncio.wait_for(
                    loop.run_in_executor(None, self._create_spatial_agent_sync),
                    timeout=spatial_agent_init_timeout
                )

                if self.spatial_agent:
                    # Phase 2: Initialize agent (async with timeout)
                    try:
                        await asyncio.wait_for(
                            self.spatial_agent.on_initialize(),
                            timeout=spatial_agent_init_timeout
                        )
                        component_status["spatial_agent"]["success"] = True
                        component_status["spatial_agent"]["duration"] = time_module.time() - comp_start
                        logger.info("✅ Spatial Awareness Ready")
                    except asyncio.TimeoutError:
                        # Partial success - agent created but not fully initialized
                        component_status["spatial_agent"]["success"] = True  # Partial
                        component_status["spatial_agent"]["error"] = "on_initialize timeout (Yabai blocking)"
                        component_status["spatial_agent"]["duration"] = time_module.time() - comp_start
                        logger.warning("SpatialAgent partially initialized - Yabai may be blocking")
                else:
                    component_status["spatial_agent"]["error"] = "creation returned None"
                    component_status["spatial_agent"]["duration"] = time_module.time() - comp_start
                    self.spatial_agent = None

            except asyncio.TimeoutError:
                component_status["spatial_agent"]["error"] = f"timeout ({spatial_agent_init_timeout}s)"
                component_status["spatial_agent"]["duration"] = time_module.time() - comp_start
                logger.warning(f"SpatialAwarenessAgent timeout - God Mode without space switching")
                self.spatial_agent = None
            except Exception as e:
                component_status["spatial_agent"]["error"] = str(e)
                component_status["spatial_agent"]["duration"] = time_module.time() - comp_start
                logger.warning(f"SpatialAwarenessAgent failed: {e}")
                self.spatial_agent = None

        # =====================================================================
        # LAUNCH ALL TASKS IN PARALLEL
        # =====================================================================
        tasks = [
            asyncio.create_task(init_ferrari_engine(), name="ferrari"),
            asyncio.create_task(init_watcher_manager(), name="watcher"),
            asyncio.create_task(init_detector(), name="detector"),
            asyncio.create_task(init_computer_use(), name="computer_use"),
            asyncio.create_task(init_agentic_runner(), name="agentic"),
            asyncio.create_task(init_spatial_agent(), name="spatial"),
        ]

        # Wait for all with global timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=parallel_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ Parallel initialization timed out after {parallel_timeout}s")
            logger.warning("   Some components may still be initializing in background")
            # Cancel remaining tasks
            for task in tasks:
                if not task.done():
                    task.cancel()

        # =====================================================================
        # INITIALIZATION COMPLETE - Report Status
        # =====================================================================
        total_time = time_module.time() - start_time

        # Count successes and failures
        successes = sum(1 for s in component_status.values() if s.get("success") and not s.get("skipped"))
        failures = sum(1 for s in component_status.values() if not s.get("success") and not s.get("skipped"))
        skipped = sum(1 for s in component_status.values() if s.get("skipped"))

        # Log detailed component status
        logger.info("")
        logger.info("=" * 60)
        logger.info(f"✨ [VisualMonitor] Parallel Init Complete in {total_time:.2f}s")
        logger.info(f"   Components: {successes} ready, {failures} degraded, {skipped} skipped")
        logger.info("-" * 60)

        for comp_name, status in component_status.items():
            if status.get("skipped"):
                logger.info(f"   ⏭️  {comp_name}: SKIPPED (disabled)")
            elif status.get("success"):
                logger.info(f"   ✅ {comp_name}: READY ({status['duration']:.2f}s)")
            else:
                logger.info(f"   ⚠️  {comp_name}: DEGRADED - {status.get('error', 'unknown')} ({status['duration']:.2f}s)")

        logger.info("=" * 60)
        logger.info("")

        # Ensure cross-repo directory exists
        if self.config.enable_cross_repo_sync:
            cross_repo_path = Path(self.config.cross_repo_dir).expanduser()
            cross_repo_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Cross-repo directory: {cross_repo_path}")

        # Subscribe to visual monitoring messages (only if connected)
        if self.message_bus:
            try:
                await self.subscribe(
                    MessageType.CUSTOM,
                    self._handle_visual_message,
                )
            except RuntimeError:
                logger.debug("Message bus not available for subscription")

        # Announce availability (only if connected)
        if self.message_bus:
            try:
                await self.broadcast(
                    message_type=MessageType.ANNOUNCEMENT,
                    payload={
                        "agent": self.agent_name,
                        "event": "agent_ready",
                        "capabilities": list(self.capabilities),
                        "watcher_available": self._watcher_manager is not None,
                        "detector_available": self._detector is not None,
                    },
                )
            except RuntimeError:
                logger.debug("Message bus not available for broadcast")

        logger.info(
            f"VisualMonitorAgent initialized - "
            f"Watcher: {'ACTIVE' if self._watcher_manager else 'INACTIVE'}, "
            f"Detector: {'ACTIVE' if self._detector else 'INACTIVE'}"
        )

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("VisualMonitorAgent started - ready for visual monitoring")

        # Start cross-repo state sync
        if self.config.enable_cross_repo_sync:
            self._state_sync_task = asyncio.create_task(self._sync_state_loop())
            logger.info("Cross-repo state sync started")

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"VisualMonitorAgent stopping - "
            f"Watches: {self._total_watches_started}, "
            f"Events: {self._total_events_detected}, "
            f"Alerts: {self._total_alerts_sent}, "
            f"Actions: {self._total_actions_executed} ({self._total_actions_succeeded} succeeded)"
        )

        # v12.0: Stop all Ferrari Engine watchers
        if self._active_video_watchers:
            logger.info(f"Stopping {len(self._active_video_watchers)} Ferrari Engine watchers...")
            for watcher_id, watcher_info in list(self._active_video_watchers.items()):
                try:
                    await watcher_info['watcher'].stop()
                    logger.debug(f"Stopped Ferrari Engine watcher: {watcher_id}")
                except Exception as e:
                    logger.error(f"Error stopping watcher {watcher_id}: {e}")

        # Legacy: Stop all active watchers in old manager
        if self._watcher_manager:
            await self._watcher_manager.stop_all_watchers()

        # Cancel watch tasks
        for task_id, task in self._watch_tasks.items():
            task.cancel()
            logger.debug(f"Cancelled watch task: {task_id}")

        # Stop state sync
        if self._state_sync_task:
            self._state_sync_task.cancel()

        logger.info("All watchers stopped")

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """
        Execute a visual monitoring task.

        Supported actions:
        - watch_and_alert: Monitor a window and alert on event
        - watch_multiple: Monitor multiple windows in parallel
        - stop_watching: Stop a specific watcher
        - list_watchers: List all active watchers
        - get_watcher_stats: Get detailed watcher statistics
        """
        action = payload.get("action", "")

        logger.debug(f"VisualMonitorAgent executing: {action}")

        if action == "watch_and_alert":
            app_name = payload.get("app_name", "")
            trigger_text = payload.get("trigger_text", "")
            space_id = payload.get("space_id")
            return await self.watch_and_alert(app_name, trigger_text, space_id)

        elif action == "watch_multiple":
            watch_specs = payload.get("watch_specs", [])
            return await self.watch_multiple(watch_specs)

        elif action == "stop_watching":
            watcher_id = payload.get("watcher_id", "")
            app_name = payload.get("app_name", "")
            return await self.stop_watching(watcher_id=watcher_id, app_name=app_name)

        elif action == "list_watchers":
            return await self.list_watchers()

        elif action == "get_watcher_stats":
            return self.get_stats()

        else:
            raise ValueError(f"Unknown visual monitoring action: {action}")

    async def watch_and_alert(
        self,
        app_name: str,
        trigger_text: str,
        space_id: Optional[int] = None,
        action_config: Optional[ActionConfig] = None,
        workflow_goal: Optional[str] = None,
        wait_for_completion: bool = False,
        max_duration: Optional[float] = None,
        **kwargs  # Accept additional kwargs for forward compatibility
    ) -> Dict[str, Any]:
        """
        Watch an app for specific text/event and alert (or ACT!) when found.

        This is the main capability - enables voice commands like:
        - Passive (v10.6): "Watch the Terminal for 'Build Successful'"
        - ACTIVE (v11.0): "Watch the Terminal for 'Build Complete', then click Deploy"

        Args:
            app_name: App to monitor (e.g., "Terminal", "Chrome")
            trigger_text: Text to wait for (e.g., "Build Successful")
            space_id: Optional specific space (auto-detect if None)
            action_config: Optional action to execute when event detected (v11.0)
            workflow_goal: Optional complex workflow goal (v11.0)
            wait_for_completion: If True, wait for event detection and action execution
                                If False, return immediately after starting watcher (default)
            max_duration: Optional max monitoring time in seconds (None = indefinite)

        Returns:
            Result with watcher_id, monitoring status, and action result if executed
        """
        if not app_name or not trigger_text:
            return {
                "success": False,
                "error": "app_name and trigger_text are required"
            }

        if not self._watcher_manager or not self._detector:
            return {
                "success": False,
                "error": "Watcher or detector not available"
            }

        try:
            logger.info(f"Starting watch: {app_name} for '{trigger_text}'")
            self._total_watches_started += 1

            # =====================================================================
            # ROOT CAUSE FIX: Timeout Protection for _find_window v6.0.0
            # =====================================================================
            # PROBLEM: _find_window can block forever if Yabai/MultiSpaceDetector hangs
            # - Blocks voice thread indefinitely
            # - User sees "Processing..." forever
            #
            # SOLUTION: Wrap in asyncio.wait_for with configurable timeout
            # =====================================================================

            # Step 1: Find window using SpatialAwarenessAgent (with timeout protection)
            find_window_timeout = float(os.getenv("JARVIS_FIND_WINDOW_TIMEOUT", "5"))

            try:
                window_info = await asyncio.wait_for(
                    self._find_window(app_name, space_id),
                    timeout=find_window_timeout
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"_find_window timed out after {find_window_timeout}s for '{app_name}'. "
                    f"Yabai or MultiSpaceDetector may be unresponsive."
                )
                return {
                    "success": False,
                    "error": f"Could not find {app_name} (window search timed out)",
                    "timeout": True
                }

            if not window_info['found']:
                return {
                    "success": False,
                    "error": f"Could not find {app_name}",
                    "window_info": window_info
                }

            window_id = window_info['window_id']
            detected_space_id = window_info.get('space_id', 0)

            logger.info(
                f"Found {app_name}: Window {window_id}, Space {detected_space_id}, "
                f"Method: {window_info.get('method', 'unknown')}"
            )

            # Step 2: Spawn Ferrari Engine VideoWatcher (v12.0)
            watcher = await self._spawn_ferrari_watcher(
                window_id=window_id,
                fps=self.config.default_fps,
                app_name=app_name,
                space_id=detected_space_id
            )

            if not watcher:
                return {
                    "success": False,
                    "error": f"Failed to create Ferrari Engine watcher for {app_name}",
                    "window_id": window_id
                }

            # v11.0: Auto-create action_config from workflow_goal if provided
            if workflow_goal and not action_config:
                action_config = ActionConfig(
                    action_type=ActionType.WORKFLOW,
                    workflow_goal=workflow_goal,
                    narrate=self.config.enable_voice_alerts,
                    switch_to_window=self.config.auto_switch_to_window
                )

            # Step 3: Start monitoring task
            task = asyncio.create_task(
                self._monitor_and_alert(
                    watcher=watcher,
                    trigger_text=trigger_text,
                    app_name=app_name,
                    space_id=detected_space_id,
                    action_config=action_config  # v11.0: Pass action config
                )
            )

            self._watch_tasks[watcher.watcher_id] = task
            self._active_watchers[watcher.watcher_id] = {
                'watcher': watcher,
                'app_name': app_name,
                'trigger_text': trigger_text,
                'space_id': detected_space_id,
                'started_at': datetime.now().isoformat(),
                'action_config': action_config,  # v11.0: Store action config
                'will_act': action_config is not None  # v11.0: Flag for active monitoring
            }

            # Intelligent mode selection: wait or return immediately
            if wait_for_completion:
                # BLOCKING MODE: Wait for event detection and action execution
                logger.info(f"[{watcher.watcher_id}] Blocking mode: waiting for completion...")

                try:
                    # Wait for monitoring task to complete (with timeout protection)
                    monitoring_result = await asyncio.wait_for(
                        task,
                        timeout=self.config.default_timeout
                    )

                    # Return comprehensive result with action execution details
                    return {
                        "success": True,
                        "watcher_id": watcher.watcher_id,
                        "window_id": window_id,
                        "app_name": app_name,
                        "space_id": detected_space_id,
                        "trigger_text": trigger_text,
                        "action_result": monitoring_result,  # Full action execution result
                        "message": f"Completed watch on {app_name}"
                    }

                except asyncio.TimeoutError:
                    logger.warning(f"[{watcher.watcher_id}] Timed out waiting for event")
                    # Clean up
                    await self._watcher_manager.stop_watcher(watcher.watcher_id)
                    return {
                        "success": False,
                        "error": f"Timeout waiting for '{trigger_text}' (>{self.config.default_timeout}s)",
                        "watcher_id": watcher.watcher_id,
                        "timeout": True
                    }

                except Exception as e:
                    logger.exception(f"[{watcher.watcher_id}] Error during blocking wait: {e}")
                    return {
                        "success": False,
                        "error": f"Monitoring failed: {str(e)}",
                        "watcher_id": watcher.watcher_id
                    }
            else:
                # BACKGROUND MODE: Return immediately (original behavior)
                logger.info(f"[{watcher.watcher_id}] Background mode: watcher running asynchronously")
                return {
                    "success": True,
                    "watcher_id": watcher.watcher_id,
                    "window_id": window_id,
                    "app_name": app_name,
                    "space_id": detected_space_id,
                    "trigger_text": trigger_text,
                    "message": f"Watching {app_name} on Space {detected_space_id}"
                }

        except Exception as e:
            logger.exception(f"Error in watch_and_alert: {e}")
            return {
                "success": False,
                "error": str(e),
                "app_name": app_name,
                "trigger_text": trigger_text
            }

    # =========================================================================
    # GOD MODE: Multi-Space Parallel Watching (v13.0)
    # =========================================================================

    async def watch_app_across_all_spaces(
        self,
        app_name: str,
        trigger_text: str,
        action_config: Optional[Dict[str, Any]] = None,
        alert_config: Optional[Dict[str, Any]] = None,
        max_duration: Optional[float] = None,
        wait_for_completion: bool = False
    ) -> Dict[str, Any]:
        """
        God Mode: Watch ALL instances of an app across ALL macOS spaces simultaneously.

        Spawns parallel Ferrari Engine watchers for every matching window.
        When ANY watcher detects trigger, automatically switches to that space
        and executes action.

        Args:
            app_name: Application to monitor (e.g., "Terminal", "Safari")
            trigger_text: Text to watch for (appears on ANY instance)
            action_config: Actions to execute upon detection
            alert_config: Alert settings (notification, logging)
            max_duration: Max monitoring time in seconds (None = indefinite)
            wait_for_completion: If True, wait for event detection (blocking)
                                If False, return immediately after starting watchers (default)

        Returns:
            {
                'status': 'triggered' | 'timeout' | 'error',
                'triggered_window': {...},  # Which window detected
                'triggered_space': int,     # Which space
                'detection_time': float,
                'total_watchers': int,
                'results': {...}
            }

        Example:
            # Watch ALL Terminal windows across all spaces for "BUILD SUCCESS"
            result = await agent.watch_app_across_all_spaces(
                app_name="Terminal",
                trigger_text="BUILD SUCCESS",
                action_config={
                    'type': 'notification',
                    'message': 'Build completed in Terminal on Space {space_id}'
                }
            )
        """
        logger.info(f"🚀 God Mode: Initiating parallel watch for '{app_name}' - '{trigger_text}'")

        # =====================================================================
        # ROOT CAUSE FIX: Timeout Protection for God Mode Window Discovery v6.0.0
        # =====================================================================
        # PROBLEM: _find_window(find_all=True) can block forever
        # - MultiSpaceWindowDetector may hang on Yabai
        # - Voice thread blocks indefinitely
        # - User stuck at "Processing..."
        #
        # SOLUTION: Wrap in asyncio.wait_for with timeout
        # =====================================================================

        # ===== STEP 1: Discover All Windows Across All Spaces (with timeout) =====
        logger.info(f"🔍 Discovering all {app_name} windows across spaces...")

        find_window_timeout = float(os.getenv("JARVIS_FIND_WINDOW_TIMEOUT", "5"))

        try:
            windows = await asyncio.wait_for(
                self._find_window(app_name, find_all=True),
                timeout=find_window_timeout
            )
        except asyncio.TimeoutError:
            logger.error(
                f"🚨 God Mode window discovery timed out after {find_window_timeout}s for '{app_name}'. "
                f"MultiSpaceWindowDetector or Yabai may be unresponsive."
            )

            # v15.0: ERROR NARRATION - User should know when window discovery fails
            if self.config.working_out_loud_enabled:
                try:
                    await self._narrate_working_out_loud(
                        message=f"I couldn't find {app_name} windows. The window system took too long to respond.",
                        narration_type="error",
                        watcher_id=f"discovery_{app_name}",
                        priority="high"
                    )
                except Exception as e:
                    logger.warning(f"Error narration failed: {e}")

            return {
                'status': 'error',
                'error': f"Window discovery timed out after {find_window_timeout}s (Yabai/MultiSpaceDetector unresponsive)",
                'total_watchers': 0,
                'timeout': True
            }

        if not windows or len(windows) == 0:
            logger.warning(f"⚠️  No windows found for {app_name}")

            # v15.0: ERROR NARRATION - User should know when no windows found
            if self.config.working_out_loud_enabled:
                try:
                    await self._narrate_working_out_loud(
                        message=f"I don't see any {app_name} windows open. Please open {app_name} and try again.",
                        narration_type="error",
                        watcher_id=f"discovery_{app_name}",
                        priority="high"
                    )
                except Exception as e:
                    logger.warning(f"Error narration failed: {e}")

            return {
                'status': 'error',
                'error': f"No windows found for {app_name}",
                'total_watchers': 0
            }

        logger.info(f"✅ Found {len(windows)} {app_name} windows:")
        for w in windows:
            logger.info(f"  - Window {w['window_id']} on Space {w['space_id']} ({w['confidence']}% match)")

        # Validate max watchers
        max_watchers = self.config.max_multi_space_watchers
        if len(windows) > max_watchers:
            logger.warning(f"⚠️  Found {len(windows)} windows, limiting to {max_watchers} for safety")
            windows = windows[:max_watchers]

        # ===== STEP 2: Spawn Parallel Ferrari Watchers =====
        watcher_tasks = []
        watcher_metadata = []

        for window in windows:
            # Create unique watcher ID
            watcher_id = f"{app_name}_space{window['space_id']}_win{window['window_id']}"

            # Store metadata for later correlation
            watcher_metadata.append({
                'watcher_id': watcher_id,
                'window_id': window['window_id'],
                'space_id': window['space_id'],
                'app_name': window['app_name']
            })

            # Spawn Ferrari Engine watcher
            watcher_task = asyncio.create_task(
                self._spawn_multi_space_watcher(
                    watcher_id=watcher_id,
                    window_id=window['window_id'],
                    space_id=window['space_id'],
                    trigger_text=trigger_text,
                    action_config=action_config,
                    alert_config=alert_config,
                    app_name=window.get('app_name', app_name)  # v13.0: Pass app_name for Ferrari Engine
                )
            )

            watcher_tasks.append(watcher_task)

        logger.info(f"🚀 Spawned {len(watcher_tasks)} parallel Ferrari Engine watchers")

        # =====================================================================
        # ROOT CAUSE FIX: Non-Blocking Execution v2.0.0
        # =====================================================================
        # Intelligent mode selection: wait or return immediately
        # =====================================================================

        if wait_for_completion:
            # BLOCKING MODE: Wait for event detection across all spaces
            logger.info(f"[God Mode] Blocking mode: waiting for trigger across {len(watcher_tasks)} spaces...")

            # ===== STEP 3A: Race Condition - First Trigger Wins (BLOCKING) =====
            try:
                # Run all watchers in parallel with timeout
                if max_duration:
                    results = await asyncio.wait_for(
                        self._coordinate_watchers(watcher_tasks, watcher_metadata),
                        timeout=max_duration
                    )
                else:
                    results = await self._coordinate_watchers(watcher_tasks, watcher_metadata)

                return results

            except asyncio.TimeoutError:
                # Timeout - stop all watchers
                logger.warning(f"⏱️  God Mode timeout after {max_duration}s")
                await self._stop_all_watchers()
                return {
                    'status': 'timeout',
                    'total_watchers': len(watcher_tasks),
                    'duration': max_duration
                }

            except Exception as e:
                logger.error(f"❌ Error in God Mode watch: {e}")
                await self._stop_all_watchers()
                return {
                    'status': 'error',
                    'error': str(e),
                    'total_watchers': len(watcher_tasks)
                }

        else:
            # BACKGROUND MODE: Return immediately, watchers run asynchronously
            logger.info(f"[God Mode] Background mode: {len(watcher_tasks)} watchers running asynchronously")

            # =====================================================================
            # ROOT CAUSE FIX v16.0: Verify Watchers Actually Start Before Returning
            # =====================================================================
            # PROBLEM: asyncio.create_task() only SCHEDULES tasks - they don't run
            # until we yield control. We were returning "success" before tasks ran!
            #
            # SOLUTION:
            # 1. Yield control to let watcher tasks start executing
            # 2. Wait briefly for watchers to register in _active_video_watchers
            # 3. Trigger purple indicator BEFORE returning (don't rely on tasks)
            # 4. Return error if ALL watchers failed to start
            # =====================================================================

            # ===== STEP 3B.0: Wait for Watchers to Actually Start =====
            watcher_startup_timeout = float(os.getenv('JARVIS_WATCHER_STARTUP_TIMEOUT', '10.0'))
            startup_check_interval = 0.2  # Check every 200ms
            initial_watcher_count = len(self._active_video_watchers)
            expected_watchers = len(watcher_tasks)

            logger.info(
                f"[God Mode] Waiting up to {watcher_startup_timeout}s for {expected_watchers} "
                f"watchers to start (current: {initial_watcher_count})..."
            )

            # Yield control and wait for watchers to register
            start_wait_time = datetime.now()
            new_watchers_started = 0

            while (datetime.now() - start_wait_time).total_seconds() < watcher_startup_timeout:
                # Yield to let tasks execute
                await asyncio.sleep(startup_check_interval)

                # Check how many new watchers registered
                current_watcher_count = len(self._active_video_watchers)
                new_watchers_started = current_watcher_count - initial_watcher_count

                if new_watchers_started >= expected_watchers:
                    # All watchers started
                    logger.info(
                        f"[God Mode] ✅ All {expected_watchers} watchers started "
                        f"in {(datetime.now() - start_wait_time).total_seconds():.2f}s"
                    )
                    break

                if new_watchers_started > 0 and (datetime.now() - start_wait_time).total_seconds() >= 3.0:
                    # At least some watchers started and we've waited 3+ seconds
                    # Don't wait for stragglers - proceed with what we have
                    logger.info(
                        f"[God Mode] ⚡ {new_watchers_started}/{expected_watchers} watchers started "
                        f"after {(datetime.now() - start_wait_time).total_seconds():.2f}s - proceeding"
                    )
                    break

            # ===== STEP 3B.0.1: Verify At Least One Watcher Started =====
            if new_watchers_started == 0:
                logger.error(
                    f"[God Mode] ❌ No watchers started after {watcher_startup_timeout}s! "
                    f"All {expected_watchers} tasks may have failed."
                )

                # Narrate error to user
                if self.config.working_out_loud_enabled:
                    try:
                        await self._narrate_working_out_loud(
                            message=f"I couldn't connect to any {app_name} windows. "
                                    f"Please check if {app_name} is open and try again.",
                            narration_type="error",
                            watcher_id=f"startup_failed_{app_name}",
                            priority="high"
                        )
                    except Exception as e:
                        logger.warning(f"[God Mode] Error narration failed: {e}")

                return {
                    'success': False,
                    'status': 'error',
                    'error': f'No watchers started after {watcher_startup_timeout}s timeout',
                    'total_watchers': 0,
                    'expected_watchers': expected_watchers,
                    'app_name': app_name,
                    'trigger_text': trigger_text,
                    'message': f"Failed to connect to any {app_name} windows"
                }

            # ===== STEP 3B.0.2: Ensure Purple Indicator Active =====
            # Call directly here - don't rely on watcher tasks to trigger it
            logger.info("[God Mode] 🟣 Ensuring purple indicator is active...")
            try:
                indicator_success = await self._ensure_purple_indicator()
                if indicator_success:
                    logger.info("[God Mode] 🟣 Purple recording indicator activated")
                else:
                    logger.warning("[God Mode] ⚠️ Purple indicator activation failed (non-critical)")
            except Exception as e:
                logger.warning(f"[God Mode] Purple indicator failed: {e} (non-critical)")

            # ===== STEP 3B: Non-Blocking Execution - Return Immediately =====
            # Create unique coordination task ID
            god_mode_task_id = f"god_mode_{app_name}_{int(datetime.now().timestamp() * 1000)}"

            logger.info(
                f"[God Mode] ✅ Background monitoring active: {new_watchers_started} watchers, "
                f"watching for '{trigger_text}'"
            )

            # =====================================================================
            # ROOT CAUSE FIX: Safety Harness for Background Tasks v5.0.0
            # =====================================================================
            # PROBLEM: Background tasks crash silently ("Future exception never retrieved")
            # - ConnectionError from network drops not caught
            # - Resources (video streams) not cleaned up on crash
            # - Zombie processes left running
            #
            # SOLUTION: Comprehensive exception handling + done callback safety net
            # - Catch specific exceptions (TimeoutError, ConnectionError, etc.)
            # - Always clean up resources via _stop_all_watchers()
            # - Done callback as final safety net
            # =====================================================================

            async def _safe_background_coordination():
                """
                Safety harness for background surveillance task.

                Handles all exceptions gracefully and ensures resources are cleaned up:
                - TimeoutError: Max duration exceeded
                - ConnectionError: Network/Ferrari Engine connection lost
                - asyncio.CancelledError: Task cancelled by user
                - Exception: Any other unexpected error

                Returns:
                    Result dict with status (triggered/timeout/error/cancelled)
                """
                watcher_count = len(watcher_tasks)
                logger.debug(f"[God Mode {god_mode_task_id}] Safety harness active for {watcher_count} watchers")

                try:
                    # ===== MAIN COORDINATION LOGIC =====
                    if max_duration:
                        results = await asyncio.wait_for(
                            self._coordinate_watchers(watcher_tasks, watcher_metadata),
                            timeout=max_duration
                        )
                    else:
                        results = await self._coordinate_watchers(watcher_tasks, watcher_metadata)

                    logger.info(f"✅ [God Mode {god_mode_task_id}] Detection completed: {results.get('status')}")
                    return results

                except asyncio.TimeoutError:
                    # Max duration exceeded - normal timeout condition
                    logger.warning(
                        f"⏱️  [God Mode {god_mode_task_id}] Timeout after {max_duration}s. "
                        f"Stopping {watcher_count} watchers..."
                    )
                    await self._stop_all_watchers()
                    return {
                        'status': 'timeout',
                        'total_watchers': watcher_count,
                        'duration': max_duration,
                        'god_mode_task_id': god_mode_task_id
                    }

                except ConnectionError as e:
                    # Network drop or Ferrari Engine disconnection
                    logger.error(
                        f"🔌 [God Mode {god_mode_task_id}] Connection lost: {e}. "
                        f"Stopping {watcher_count} watchers..."
                    )
                    await self._stop_all_watchers()
                    return {
                        'status': 'connection_error',
                        'error': f"Connection lost: {str(e)}",
                        'total_watchers': watcher_count,
                        'god_mode_task_id': god_mode_task_id
                    }

                except asyncio.CancelledError:
                    # Task was cancelled (user stopped monitoring)
                    logger.info(
                        f"🛑 [God Mode {god_mode_task_id}] Task cancelled by user. "
                        f"Stopping {watcher_count} watchers..."
                    )
                    await self._stop_all_watchers()
                    # Re-raise to properly propagate cancellation
                    raise

                except OSError as e:
                    # OS-level errors (file descriptor issues, etc.)
                    logger.error(
                        f"💥 [God Mode {god_mode_task_id}] OS error: {e}. "
                        f"Stopping {watcher_count} watchers...",
                        exc_info=True
                    )
                    await self._stop_all_watchers()
                    return {
                        'status': 'os_error',
                        'error': f"OS error: {str(e)}",
                        'total_watchers': watcher_count,
                        'god_mode_task_id': god_mode_task_id
                    }

                except Exception as e:
                    # Catch-all for any other unexpected errors
                    logger.critical(
                        f"❌ [God Mode {god_mode_task_id}] Critical failure: {e}. "
                        f"Stopping {watcher_count} watchers...",
                        exc_info=True  # Full stack trace for debugging
                    )
                    await self._stop_all_watchers()
                    return {
                        'status': 'error',
                        'error': str(e),
                        'error_type': type(e).__name__,
                        'total_watchers': watcher_count,
                        'god_mode_task_id': god_mode_task_id
                    }

                finally:
                    # ===== FINAL SAFETY NET: Always clean up task reference =====
                    logger.debug(f"[God Mode {god_mode_task_id}] Cleaning up task reference...")
                    if hasattr(self, '_god_mode_tasks') and god_mode_task_id in self._god_mode_tasks:
                        del self._god_mode_tasks[god_mode_task_id]

            # =====================================================================
            # Spawn background task with safety harness
            # =====================================================================
            coordination_task = asyncio.create_task(_safe_background_coordination())

            # =====================================================================
            # Done Callback: Final safety net for uncaught exceptions
            # =====================================================================
            def _handle_task_completion(task: asyncio.Task):
                """
                Done callback - catches any exceptions that slip through try/except.
                This is the FINAL safety net to prevent "Future exception never retrieved".
                """
                try:
                    # Retrieve result to surface any uncaught exceptions
                    result = task.result()
                    if result and result.get('status') in ['error', 'connection_error', 'os_error']:
                        logger.warning(
                            f"[God Mode {god_mode_task_id}] Task completed with error status: "
                            f"{result.get('status')}"
                        )
                except asyncio.CancelledError:
                    # Expected cancellation - don't log as error
                    logger.debug(f"[God Mode {god_mode_task_id}] Task cancelled successfully")
                except Exception as e:
                    # This should NEVER happen if _safe_background_coordination is correct
                    # But if it does, we catch it here as absolute final safety net
                    logger.critical(
                        f"🚨 [God Mode {god_mode_task_id}] UNCAUGHT EXCEPTION in background task: {e}",
                        exc_info=True
                    )
                    # Try one last cleanup attempt
                    try:
                        # Can't use await in callback, so create a task
                        asyncio.create_task(self._stop_all_watchers())
                    except Exception as cleanup_error:
                        logger.error(f"Failed final cleanup attempt: {cleanup_error}")

            # Attach done callback for final safety net
            coordination_task.add_done_callback(_handle_task_completion)

            # Store task for potential cancellation/monitoring
            if not hasattr(self, '_god_mode_tasks'):
                self._god_mode_tasks = {}
            self._god_mode_tasks[god_mode_task_id] = coordination_task

            # =====================================================================
            # v15.0: IMMEDIATE STARTUP CONFIRMATION - "Working Out Loud" transparency
            # =====================================================================
            # CRITICAL: Narrate to user immediately so they know watchers started
            # This fixes the "silent background" problem where user gets no feedback
            # =====================================================================
            if self.config.working_out_loud_enabled:
                try:
                    # Get or create narration state for startup tracking
                    startup_state = self._get_narration_state(god_mode_task_id)
                    import time
                    startup_state.monitoring_start_time = time.time()

                    # Build confirmation message - use ACTUAL watcher count, not expected
                    actual_watchers = new_watchers_started
                    spaces_list = [meta['space_id'] for meta in watcher_metadata[:actual_watchers]]

                    if actual_watchers == 1:
                        startup_msg = f"I've connected to {app_name}. Monitoring for '{trigger_text}' now."
                    elif actual_watchers < expected_watchers:
                        # Partial success - some watchers didn't start
                        startup_msg = (
                            f"I've connected to {actual_watchers} of {expected_watchers} {app_name} windows. "
                            f"Monitoring for '{trigger_text}'."
                        )
                    else:
                        startup_msg = (
                            f"I've connected to {actual_watchers} {app_name} windows across spaces "
                            f"{', '.join(map(str, spaces_list))}. Monitoring for '{trigger_text}'."
                        )

                    # Send startup confirmation via TTS
                    await self._narrate_working_out_loud(
                        message=startup_msg,
                        narration_type="startup",
                        watcher_id=god_mode_task_id,
                        priority="normal"
                    )
                    logger.info(f"[God Mode] 🎙️ Startup confirmation narrated: {startup_msg}")
                except Exception as e:
                    logger.warning(f"[God Mode] Startup narration failed: {e}")

            # Return immediately with acknowledgment
            # Use ACTUAL watcher count - not expected count
            return {
                'success': True,
                'status': 'monitoring',
                'god_mode_task_id': god_mode_task_id,
                'total_watchers': new_watchers_started,  # v16.0: Actual count
                'expected_watchers': expected_watchers,  # v16.0: For transparency
                'app_name': app_name,
                'trigger_text': trigger_text,
                'spaces_monitored': [meta['space_id'] for meta in watcher_metadata[:new_watchers_started]],
                'startup_time_seconds': (datetime.now() - start_wait_time).total_seconds(),
                'message': f"Monitoring {new_watchers_started} {app_name} windows for '{trigger_text}'"
            }

    async def _coordinate_watchers(
        self,
        watcher_tasks: List[asyncio.Task],
        watcher_metadata: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Coordinate parallel watchers - first trigger wins.

        When ANY watcher detects trigger:
        1. Cancel all other watchers
        2. Identify which window/space detected
        3. Switch to that space
        4. Execute action
        5. Return results
        """
        logger.info(f"⚡ Racing {len(watcher_tasks)} watchers - first detection wins")

        # Race all watchers - first to complete wins
        done, pending = await asyncio.wait(
            watcher_tasks,
            return_when=asyncio.FIRST_COMPLETED
        )

        # ===== STEP 1: Cancel All Remaining Watchers =====
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info(f"⚡ Trigger detected! Cancelled {len(pending)} other watchers")

        # ===== STEP 2: Get Trigger Details =====
        triggered_task = done.pop()
        trigger_result = await triggered_task

        # Find which watcher triggered
        triggered_watcher = None
        for meta in watcher_metadata:
            if meta['watcher_id'] == trigger_result.get('watcher_id'):
                triggered_watcher = meta
                break

        if not triggered_watcher:
            logger.error("❌ Could not identify which watcher triggered")
            return {
                'status': 'error',
                'error': 'Watcher identification failed',
                'trigger_result': trigger_result
            }

        # ===== STEP 3: Switch to Triggered Space =====
        target_space = triggered_watcher['space_id']

        logger.info(
            f"🎯 Trigger detected on Space {target_space}, "
            f"Window {triggered_watcher['window_id']} ({triggered_watcher['app_name']})"
        )

        # Use SpatialAwarenessAgent for safe space switching
        if hasattr(self, 'spatial_agent') and self.spatial_agent:
            try:
                logger.info(f"🔄 Switching to Space {target_space}...")
                # Use switch_to method from SpatialAwarenessAgent
                switch_result = await self.spatial_agent.switch_to(
                    app_name=triggered_watcher['app_name'],
                    narrate=True
                )
                if switch_result.get('success'):
                    logger.info(f"✅ Switched to Space {target_space}")
                else:
                    logger.warning(f"⚠️  Space switch returned: {switch_result}")
            except Exception as e:
                logger.error(f"❌ Failed to switch space: {e}")
        else:
            logger.warning("⚠️  SpatialAwarenessAgent not available - skipping space switch")

        # ===== STEP 4: Execute Action =====
        action_result = await self._execute_trigger_action(
            trigger_result,
            triggered_watcher
        )

        # ===== STEP 5: Return Comprehensive Results =====
        return {
            'status': 'triggered',
            'triggered_window': triggered_watcher,
            'triggered_space': target_space,
            'detection_time': trigger_result.get('timestamp'),
            'total_watchers': len(watcher_tasks),
            'trigger_details': trigger_result,
            'action_result': action_result
        }

    async def _spawn_multi_space_watcher(
        self,
        watcher_id: str,
        window_id: int,
        space_id: int,
        trigger_text: str,
        action_config: Optional[Dict[str, Any]] = None,
        alert_config: Optional[Dict[str, Any]] = None,
        app_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Spawn a single Ferrari Engine watcher for a specific window.

        v13.0 GOD MODE: Real Ferrari Engine integration with 60 FPS ScreenCaptureKit.
        This is the critical connection between multi-space discovery and actual video capture.

        Args:
            watcher_id: Unique watcher identifier
            window_id: Window ID to monitor (from Yabai/MultiSpaceWindowDetector)
            space_id: macOS space ID where window resides
            trigger_text: Text to detect via OCR
            action_config: Optional action to execute on detection
            alert_config: Optional alert configuration
            app_name: Application name (extracted from watcher_id if not provided)

        Returns:
            Detection result dict with trigger status and metadata
        """
        logger.debug(
            f"🏁 Spawning Ferrari watcher {watcher_id} for window {window_id} "
            f"on space {space_id}"
        )

        # Extract app name from watcher_id if not provided
        # watcher_id format: "Terminal_space1_win12345"
        if not app_name:
            app_name = watcher_id.split('_space')[0] if '_space' in watcher_id else 'Unknown'

        start_time = datetime.now()

        try:
            # ===== STEP 1: Spawn Real Ferrari Engine VideoWatcher =====
            logger.info(
                f"🏎️  [{watcher_id}] Spawning Ferrari Engine for window {window_id} ({app_name})"
            )

            # Use adaptive FPS based on config
            fps = self.config.ferrari_fps

            # Create Ferrari Engine VideoWatcher
            watcher = await self._spawn_ferrari_watcher(
                window_id=window_id,
                fps=fps,
                app_name=app_name,
                space_id=space_id
            )

            if not watcher:
                # Ferrari Engine unavailable - fallback to error
                logger.error(f"❌ [{watcher_id}] Ferrari Engine watcher creation failed")

                # v15.0: ERROR NARRATION - User should know when watchers fail
                if self.config.working_out_loud_enabled:
                    try:
                        await self._narrate_working_out_loud(
                            message=f"I couldn't connect to {app_name} on space {space_id}. Ferrari Engine is unavailable.",
                            narration_type="error",
                            watcher_id=watcher_id,
                            priority="high"
                        )
                    except Exception as e:
                        logger.warning(f"Error narration failed: {e}")

                return {
                    'status': 'error',
                    'error': 'Ferrari Engine unavailable',
                    'watcher_id': watcher_id,
                    'space_id': space_id,
                    'window_id': window_id,
                    'trigger_detected': False
                }

            logger.info(f"✅ [{watcher_id}] Ferrari Engine watcher active (60 FPS GPU capture)")

            # ===== STEP 2: Convert action_config dict to ActionConfig if provided =====
            # Uses local ActionConfig and ActionType classes defined at module top
            action_config_obj = None
            if action_config and self.config.enable_action_execution:
                try:
                    # Parse action type using local ActionType enum
                    action_type_str = action_config.get('type', 'notification')
                    action_type_map = {
                        'notification': ActionType.NOTIFICATION,
                        'computer_use': ActionType.COMPUTER_USE,
                        'click': ActionType.CLICK,
                        'type': ActionType.TYPE,
                        'execute': ActionType.EXECUTE,
                        'simple_goal': ActionType.SIMPLE_GOAL,
                        'workflow': ActionType.WORKFLOW,
                        'ghost_hands': ActionType.GHOST_HANDS,
                        'voice_alert': ActionType.VOICE_ALERT,
                    }
                    action_type = action_type_map.get(action_type_str, ActionType.NOTIFICATION)

                    # Create ActionConfig object using local class
                    action_config_obj = ActionConfig(
                        action_type=action_type,
                        goal=action_config.get('goal', f"Respond to '{trigger_text}' in {app_name}"),
                        workflow_context=action_config.get('context', {}),
                        timeout_seconds=float(action_config.get('timeout_seconds', 30)),
                        require_confirmation=action_config.get('require_confirmation', False)
                    )

                    logger.info(
                        f"🎯 [{watcher_id}] Action configured: {action_type.value} - {action_config_obj.goal}"
                    )
                except Exception as e:
                    logger.warning(f"⚠️  [{watcher_id}] Could not create ActionConfig: {e}")
                    action_config_obj = None

            # ===== STEP 3: Monitor Ferrari Stream for Trigger =====
            logger.info(
                f"👁️  [{watcher_id}] Monitoring 60 FPS stream for trigger: '{trigger_text}'"
            )

            # Run Ferrari Engine visual detection
            detection_result = await self._monitor_and_alert(
                watcher=watcher,
                trigger_text=trigger_text,
                app_name=app_name,
                space_id=space_id,
                action_config=action_config_obj
            )

            # ===== STEP 4: Process Detection Result =====
            # Check if trigger was actually detected (detection_result can be dict with 'detected' key)
            trigger_detected = False
            if detection_result:
                # Check for explicit 'detected' key (from _ferrari_visual_detection)
                if isinstance(detection_result, dict):
                    trigger_detected = detection_result.get('detected', False)
                # Or check for 'trigger_detected' key (from _monitor_and_alert)
                if not trigger_detected and 'trigger_detected' in detection_result:
                    trigger_detected = detection_result.get('trigger_detected', False)

            if trigger_detected:
                # Trigger detected!
                confidence = detection_result.get('confidence', 0.0)
                logger.info(
                    f"🎯 [{watcher_id}] TRIGGER DETECTED! "
                    f"Confidence: {confidence:.2f}"
                )

                # Stop watcher after detection (God Mode: first trigger wins)
                await watcher.stop()

                return {
                    'watcher_id': watcher_id,
                    'space_id': space_id,
                    'window_id': window_id,
                    'app_name': app_name,
                    'trigger_detected': True,
                    'trigger_text': trigger_text,
                    'confidence': confidence,
                    'detection_time': detection_result.get('detection_time', 0.0),
                    'timestamp': start_time.isoformat(),
                    'action_result': detection_result.get('action_result'),
                    'status': 'success'
                }
            else:
                # No trigger detected (timeout or error)
                logger.info(f"⏱️  [{watcher_id}] No trigger detected (timeout or stopped)")

                # Stop watcher
                await watcher.stop()

                return {
                    'watcher_id': watcher_id,
                    'space_id': space_id,
                    'window_id': window_id,
                    'trigger_detected': False,
                    'timestamp': start_time.isoformat(),
                    'status': 'no_trigger'
                }

        except asyncio.CancelledError:
            # Watcher was cancelled (another watcher won the race)
            logger.info(f"🛑 [{watcher_id}] Cancelled - another watcher detected trigger first")

            # Stop watcher if it exists
            if 'watcher' in locals() and watcher:
                try:
                    await watcher.stop()
                except:
                    pass

            return {
                'watcher_id': watcher_id,
                'space_id': space_id,
                'window_id': window_id,
                'trigger_detected': False,
                'cancelled': True,
                'status': 'cancelled'
            }

        except Exception as e:
            logger.error(f"❌ [{watcher_id}] Failed to spawn/monitor watcher: {e}")

            # Stop watcher if it exists
            if 'watcher' in locals() and watcher:
                try:
                    await watcher.stop()
                except:
                    pass

            return {
                'status': 'error',
                'error': str(e),
                'watcher_id': watcher_id,
                'space_id': space_id,
                'window_id': window_id,
                'trigger_detected': False
            }

    async def _execute_trigger_action(
        self,
        trigger_result: Dict[str, Any],
        window_metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute configured action when trigger is detected.

        Uses ComputerUseConnector for actions on the detected window.
        """
        action_config = trigger_result.get('config', {}).get('action_config', {})

        if not action_config:
            logger.info("ℹ️  No action configured - detection only")
            return {'status': 'no_action'}

        action_type = action_config.get('type')

        try:
            if action_type == 'notification':
                # Send macOS notification
                message = action_config.get('message', 'Trigger detected')
                # Format message with space/window info
                formatted_message = message.format(
                    space_id=window_metadata['space_id'],
                    window_id=window_metadata['window_id'],
                    app_name=window_metadata['app_name']
                )

                logger.info(f"📢 Notification: {formatted_message}")

                return {
                    'status': 'success',
                    'action': 'notification',
                    'message': formatted_message
                }

            elif action_type == 'computer_use':
                # Execute Computer Use action on detected window
                if not self.computer_use:
                    return {
                        'status': 'error',
                        'error': 'ComputerUseConnector not available'
                    }

                # Execute action (click, type, etc.)
                cu_action = action_config.get('action_data', {})
                logger.info(f"🖱️  Executing Computer Use action: {cu_action}")

                # Placeholder for actual Computer Use execution
                result = {'executed': True, 'action': cu_action}

                return {
                    'status': 'success',
                    'action': 'computer_use',
                    'result': result
                }

            elif action_type == 'custom':
                # Call custom callback
                callback = action_config.get('callback')
                if callable(callback):
                    await callback(trigger_result, window_metadata)
                    return {
                        'status': 'success',
                        'action': 'custom_callback'
                    }

            else:
                return {
                    'status': 'error',
                    'error': f"Unknown action type: {action_type}"
                }

        except Exception as e:
            logger.error(f"❌ Action execution failed: {e}")
            return {
                'status': 'error',
                'error': str(e)
            }

    async def _stop_all_watchers(self) -> None:
        """
        Emergency stop for all active watchers.

        Called on timeout or error.
        """
        logger.info("🛑 Stopping all active watchers...")

        # Stop all VideoWatchers
        if hasattr(self, '_active_video_watchers'):
            for watcher_id, watcher in self._active_video_watchers.items():
                try:
                    if hasattr(watcher, 'stop'):
                        await watcher.stop()
                    logger.debug(f"✓ Stopped watcher: {watcher_id}")
                except Exception as e:
                    logger.error(f"❌ Error stopping watcher {watcher_id}: {e}")

            self._active_video_watchers.clear()

        logger.info("✅ All watchers stopped")

    async def watch(
        self,
        app_name: str,
        trigger_text: str,
        all_spaces: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Unified watch interface with mode selection.

        Args:
            app_name: Application to monitor
            trigger_text: Text to watch for
            all_spaces: If True, watch ALL instances across ALL spaces (God Mode)
                       If False, watch only current/first window (legacy single-window mode)
            **kwargs: Additional config (action_config, alert_config, space_id, max_duration)

        Returns:
            Watch results dict

        Examples:
            # Single window mode (legacy)
            await agent.watch("Terminal", "DONE")

            # God Mode - All spaces
            await agent.watch("Terminal", "DONE", all_spaces=True)

            # God Mode with action
            await agent.watch(
                "Terminal",
                "BUILD COMPLETE",
                all_spaces=True,
                action_config={'type': 'notification', 'message': 'Build done on Space {space_id}'}
            )
        """
        if all_spaces and self.config.multi_space_enabled:
            logger.info(f"🌌 God Mode: Watching ALL {app_name} windows across all spaces")
            return await self.watch_app_across_all_spaces(
                app_name=app_name,
                trigger_text=trigger_text,
                **kwargs
            )
        else:
            if all_spaces and not self.config.multi_space_enabled:
                logger.warning("God Mode disabled in config - using single-window mode")

            logger.info(f"📺 Standard Mode: Watching first {app_name} window")
            return await self.watch_and_alert(
                app_name=app_name,
                trigger_text=trigger_text,
                **kwargs
            )

    async def watch_multiple(
        self,
        watch_specs: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Monitor multiple windows in parallel.

        Args:
            watch_specs: List of watch specifications
                [
                    {"app": "Terminal", "trigger": "Build Successful"},
                    {"app": "Chrome", "trigger": "Application Submitted"}
                ]

        Returns:
            Results for all watchers
        """
        if not watch_specs:
            return {"success": False, "error": "No watch specs provided"}

        logger.info(f"Starting {len(watch_specs)} parallel watchers")

        # Start all watchers in parallel
        tasks = []
        for spec in watch_specs:
            app_name = spec.get('app', '')
            trigger_text = spec.get('trigger', '')
            space_id = spec.get('space_id')

            task = self.watch_and_alert(app_name, trigger_text, space_id)
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        successful = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
        failed = len(results) - successful

        return {
            "success": successful > 0,
            "total_watchers": len(watch_specs),
            "successful": successful,
            "failed": failed,
            "results": results
        }

    async def stop_watching(
        self,
        watcher_id: Optional[str] = None,
        app_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Stop a specific watcher.

        Args:
            watcher_id: Specific watcher ID (or)
            app_name: App name (stops all watchers for this app)

        Returns:
            Stop result
        """
        if not watcher_id and not app_name:
            return {"success": False, "error": "watcher_id or app_name required"}

        stopped_count = 0

        if watcher_id:
            # Stop specific watcher
            if watcher_id in self._active_watchers:
                await self._stop_watcher_by_id(watcher_id)
                stopped_count = 1
            else:
                return {
                    "success": False,
                    "error": f"Watcher {watcher_id} not found"
                }

        elif app_name:
            # Stop all watchers for this app
            watcher_ids = [
                wid for wid, info in self._active_watchers.items()
                if info['app_name'].lower() == app_name.lower()
            ]

            for wid in watcher_ids:
                await self._stop_watcher_by_id(wid)
                stopped_count += 1

            if stopped_count == 0:
                return {
                    "success": False,
                    "error": f"No watchers found for {app_name}"
                }

        # =====================================================================
        # v6.1.0: Deactivate Purple Indicator when all watchers stopped
        # =====================================================================
        # Only stop the indicator when NO active watchers remain
        total_active = len(self._active_watchers) + len(self._active_video_watchers)
        if total_active == 0:
            await self._stop_purple_indicator()

        return {
            "success": True,
            "stopped_count": stopped_count,
            "message": f"Stopped {stopped_count} watcher(s)"
        }

    async def list_watchers(self) -> Dict[str, Any]:
        """List all active watchers."""
        watchers = []

        for watcher_id, info in self._active_watchers.items():
            watcher = info['watcher']
            watchers.append({
                'watcher_id': watcher_id,
                'app_name': info['app_name'],
                'trigger_text': info['trigger_text'],
                'space_id': info['space_id'],
                'started_at': info['started_at'],
                'stats': watcher.get_stats() if hasattr(watcher, 'get_stats') else {}
            })

        return {
            "active_watchers": len(watchers),
            "watchers": watchers,
            "max_parallel": self.config.max_parallel_watchers
        }

    # =========================================================================
    # Internal Methods
    # =========================================================================

    async def _find_window(
        self,
        app_name: str,
        space_id: Optional[int] = None,
        find_all: bool = False
    ) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Find window(s) using Ferrari Engine (fast_capture) or fallbacks.

        Args:
            app_name: Application name to search for
            space_id: Optional specific space ID to search in
            find_all: If True, return ALL matching windows across ALL spaces
                     If False, return first/best match only (backward compatible)

        Returns:
            If find_all=False: Single window dict or None (backward compatible)
            If find_all=True: List of window dicts with space metadata

        Priority:
        1. Ferrari Engine (fast_capture) - Accurate window enumeration
        2. MultiSpaceWindowDetector - Cross-space enumeration (for find_all=True)
        3. SpatialAwarenessAgent - Yabai integration
        4. Legacy estimation - Hash-based fallback
        """
        # =====================================================================
        # GOD MODE: Find ALL windows across ALL spaces
        # =====================================================================
        if find_all and MULTI_SPACE_AVAILABLE:
            try:
                logger.info(f"🔍 God Mode: Searching for ALL '{app_name}' windows across ALL spaces...")

                # =====================================================================
                # ROOT CAUSE FIX v11.0.0: Non-Blocking Yabai Queries with Cache
                # =====================================================================
                # PROBLEM: MultiSpaceWindowDetector makes synchronous subprocess.run() calls
                # - Blocks entire event loop while waiting for Yabai
                # - asyncio.wait_for timeout doesn't work on blocking calls
                # - User stuck at "Processing..." forever
                #
                # SOLUTION: Use module-level cached check + thread pool execution
                # - Cache avoids repeated blocking checks
                # - Short timeout (1s) prevents hang
                # - Thread pool execution for detector queries
                # =====================================================================

                # Fast-fail check: Use cached yabai availability
                yabai_check_timeout = float(os.getenv("JARVIS_YABAI_CHECK_TIMEOUT", "2"))

                try:
                    # Import the async cached check from yabai_space_detector
                    from vision.yabai_space_detector import async_quick_yabai_check

                    yabai_available, yabai_path = await asyncio.wait_for(
                        async_quick_yabai_check(),
                        timeout=yabai_check_timeout
                    )

                    if not yabai_available:
                        logger.warning(
                            f"⚠️  Yabai not available (cached check) - skipping multi-space detection for '{app_name}'"
                        )
                        # Fall through to single-window detection
                        raise Exception("Yabai not available")

                    logger.debug(f"[GOD MODE] Yabai available at {yabai_path}")

                except asyncio.TimeoutError:
                    logger.warning(f"⚠️  Yabai availability check timed out after {yabai_check_timeout}s")
                    raise Exception("Yabai check timeout")
                except ImportError:
                    # Fallback to inline check if module not available
                    logger.warning("⚠️  async_quick_yabai_check not available, using fallback")
                    raise Exception("Yabai check module not available")

                detector = MultiSpaceWindowDetector()

                # Run blocking Yabai query in thread pool (non-blocking)
                result = await asyncio.to_thread(
                    detector.get_all_windows_across_spaces
                )

                # Extract windows list from result dict
                all_windows = result.get('windows', [])

                if not all_windows:
                    logger.warning(f"⚠️  God Mode: No windows found in multi-space detection result")
                    return []

                # Filter for matching app across ALL spaces
                app_name_lower = app_name.lower()
                matching_windows = []

                for window_obj in all_windows:
                    # Access EnhancedWindowInfo object attributes
                    window_app = window_obj.app_name if hasattr(window_obj, 'app_name') else ''
                    window_app_lower = window_app.lower()

                    # Match strategies
                    confidence = 0
                    if window_app_lower == app_name_lower:
                        confidence = 100
                    elif app_name_lower in window_app_lower:
                        confidence = 90
                    elif window_app_lower in app_name_lower:
                        confidence = 80
                    elif self._fuzzy_match(app_name_lower, window_app_lower):
                        confidence = 70

                    if confidence > 0:
                        matching_windows.append({
                            'found': True,
                            'window_id': window_obj.window_id,
                            'space_id': window_obj.space_id if window_obj.space_id else 1,
                            'app_name': window_app,
                            'window_title': window_obj.window_title if hasattr(window_obj, 'window_title') else '',
                            'bounds': window_obj.bounds if hasattr(window_obj, 'bounds') else {},
                            'is_visible': True,  # All windows from multi-space detector are visible
                            'confidence': confidence,
                            'method': 'multi_space_detector'
                        })

                if matching_windows:
                    # Sort by confidence
                    matching_windows.sort(key=lambda x: x['confidence'], reverse=True)

                    spaces_list = [f"Space {w['space_id']}" for w in matching_windows]
                    logger.info(
                        f"✅ God Mode found {len(matching_windows)} '{app_name}' windows across spaces: "
                        f"{spaces_list}"
                    )

                    return matching_windows
                else:
                    logger.warning(f"⚠️  God Mode: No windows found matching '{app_name}' across any space")
                    return []

            except Exception as e:
                logger.warning(f"⚠️  Multi-space detection failed: {e}, falling back to single-window search")

        # =====================================================================
        # STANDARD MODE: Find first/best window (backward compatible)
        # =====================================================================
        # Priority 1: Ferrari Engine (fast_capture) - ACCURATE
        if self._fast_capture_engine:
            try:
                logger.debug(f"🔍 Searching for '{app_name}' using Ferrari Engine...")

                # Get all visible windows
                windows = await asyncio.to_thread(
                    self._fast_capture_engine.get_visible_windows
                )

                # Fuzzy search for app name (case-insensitive, partial match)
                app_name_lower = app_name.lower()
                matching_windows = []

                for window in windows:
                    window_app_lower = window.app_name.lower()

                    # Match strategies (in priority order):
                    # 1. Exact match
                    if window_app_lower == app_name_lower:
                        matching_windows.append((window, 100))  # 100% confidence
                    # 2. App name contains search term
                    elif app_name_lower in window_app_lower:
                        matching_windows.append((window, 90))
                    # 3. Search term contains app name
                    elif window_app_lower in app_name_lower:
                        matching_windows.append((window, 80))
                    # 4. Fuzzy match (similar terms)
                    elif self._fuzzy_match(app_name_lower, window_app_lower):
                        matching_windows.append((window, 70))

                if matching_windows:
                    # Sort by confidence, then by window size (prefer larger windows)
                    matching_windows.sort(key=lambda x: (x[1], x[0].width * x[0].height), reverse=True)

                    best_window, confidence = matching_windows[0]

                    logger.info(
                        f"✅ Ferrari Engine found '{app_name}': "
                        f"Window {best_window.window_id} ({best_window.width}x{best_window.height}), "
                        f"Confidence: {confidence}%"
                    )

                    return {
                        'found': True,
                        'window_id': best_window.window_id,
                        'space_id': space_id or 1,  # Space ID from yabai if available
                        'app_name': best_window.app_name,
                        'window_title': best_window.window_title,
                        'width': best_window.width,
                        'height': best_window.height,
                        'confidence': confidence,
                        'method': 'ferrari_engine'
                    }
                else:
                    logger.warning(f"⚠️  Ferrari Engine: No windows found matching '{app_name}'")
                    logger.debug(f"   Available apps: {[w.app_name for w in windows[:10]]}")

            except Exception as e:
                logger.warning(f"⚠️  Ferrari Engine window search failed: {e}")

        # Priority 2: SpatialAwarenessAgent (yabai integration)
        if hasattr(self, 'coordinator') and self.coordinator:
            try:
                logger.debug(f"🔍 Trying SpatialAwarenessAgent for '{app_name}'...")

                result = await self.coordinator.request(
                    to_agent="spatial_awareness_agent",
                    payload={
                        "action": "find_window",
                        "app_name": app_name
                    },
                    timeout=5.0
                )

                if result and result.get('found'):
                    spaces = result.get('spaces', [])
                    if spaces:
                        target_space = spaces[0] if not space_id else space_id

                        # Try to get window ID from Ferrari Engine for this app
                        if self._fast_capture_engine:
                            windows = await asyncio.to_thread(
                                self._fast_capture_engine.get_visible_windows
                            )
                            for window in windows:
                                if app_name.lower() in window.app_name.lower():
                                    return {
                                        'found': True,
                                        'window_id': window.window_id,
                                        'space_id': target_space,
                                        'app_name': window.app_name,
                                        'method': 'spatial_awareness + ferrari'
                                    }

                        logger.info(f"✓ SpatialAwarenessAgent found '{app_name}' on space {target_space}")

            except Exception as e:
                logger.warning(f"SpatialAwarenessAgent query failed: {e}")

        # Priority 3: Legacy estimation (hash-based)
        logger.warning(f"⚠️  Using legacy window estimation for '{app_name}'")
        window_id = self._estimate_window_id(app_name)

        return {
            'found': True,  # Optimistic - may fail later
            'window_id': window_id,
            'space_id': space_id or 1,
            'app_name': app_name,
            'method': 'legacy_estimation',
            'warning': 'Using estimated window ID - may be inaccurate'
        }

    def _fuzzy_match(self, term1: str, term2: str, threshold: float = 0.6) -> bool:
        """
        Fuzzy string matching for app names.

        Returns True if strings are similar enough.
        """
        # Simple Levenshtein-like similarity
        if not term1 or not term2:
            return False

        # Check for common substrings
        longer = term1 if len(term1) >= len(term2) else term2
        shorter = term2 if len(term1) >= len(term2) else term1

        # Count matching characters
        matches = sum(1 for c in shorter if c in longer)
        similarity = matches / len(longer)

        return similarity >= threshold

    async def _ferrari_visual_detection(
        self,
        watcher: Any,
        trigger_text: str,
        timeout: float,
        app_name: str = "Unknown"
    ) -> Dict[str, Any]:
        """
        Ferrari Engine visual detection loop with "Working Out Loud" narration.

        Continuously pulls frames from VideoWatcher (Ferrari Engine) and
        runs OCR detection to find trigger text.

        This is the v12.0 CORE - where 60 FPS GPU frames meet intelligent OCR.

        v14.0 Enhancement: "Working Out Loud" provides real-time narration:
        - Heartbeat: Regular status updates ("Still watching Chrome...")
        - Near-miss: When interesting text is detected ("I see 'Build Started'...")
        - Activity: When significant screen changes occur

        Args:
            watcher: VideoWatcher instance (Ferrari Engine)
            trigger_text: Text to search for (supports case-insensitive fuzzy matching)
            timeout: Max time to wait for detection
            app_name: Application name for narration

        Returns:
            Detection result with confidence and timing
        """
        import time
        start_time = time.time()
        frame_count = 0
        ocr_checks = 0

        # v14.0: Get watcher ID for narration state tracking
        watcher_id = getattr(watcher, 'watcher_id', f"watcher_{id(watcher)}")

        # v14.0: Track last OCR text hash for activity detection
        narration_state = self._get_narration_state(watcher_id)
        last_ocr_hash = 0
        last_all_text = ""

        logger.info(
            f"[Ferrari Detection] Starting visual search for '{trigger_text}' "
            f"(timeout: {timeout}s, Working Out Loud: {self.config.working_out_loud_enabled})"
        )

        # v15.0: Initialize monitoring start time for first heartbeat timing
        narration_state.monitoring_start_time = start_time

        # v14.0: Initial narration - announce monitoring start (now uses "startup" type)
        if self.config.working_out_loud_enabled:
            await self._narrate_working_out_loud(
                message=f"I'm now watching {app_name} for '{trigger_text}'.",
                narration_type="startup",  # v15.0: Changed from "activity" to "startup"
                watcher_id=watcher_id,
                priority="normal"
            )

        try:
            while True:
                # Check timeout
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    logger.info(
                        f"[Ferrari Detection] Timeout after {elapsed:.1f}s "
                        f"({frame_count} frames, {ocr_checks} OCR checks)"
                    )

                    # v14.0: Cleanup narration state
                    self._cleanup_narration_state(watcher_id)

                    return {
                        'detected': False,
                        'confidence': 0.0,
                        'trigger': None,
                        'detection_time': elapsed,
                        'frames_checked': frame_count,
                        'ocr_checks': ocr_checks,
                        'timeout': True
                    }

                # v14.0: Heartbeat narration at configured intervals
                if self.config.working_out_loud_enabled:
                    await self._narrate_heartbeat(
                        watcher_id=watcher_id,
                        trigger_text=trigger_text,
                        app_name=app_name,
                        elapsed_seconds=elapsed,
                        frames_checked=frame_count,
                        ocr_checks=ocr_checks
                    )

                # Get latest frame from Ferrari Engine
                frame_data = await watcher.get_latest_frame(timeout=1.0)

                if not frame_data:
                    # No frame available - watcher may have stopped or no new frames
                    await asyncio.sleep(0.1)
                    continue

                frame_count += 1
                frame = frame_data.get('frame')

                if frame is None:
                    continue

                # OCR detection every N frames (adaptive based on FPS)
                # For 5 FPS: check every frame
                # For 30 FPS: check every 3-5 frames to reduce OCR load
                fps = frame_data.get('fps', 5)
                check_interval = max(1, int(fps / 5))  # ~5 OCR checks per second

                if frame_count % check_interval == 0:
                    ocr_checks += 1

                    # Run OCR detection with extended results for near-miss detection
                    detected, confidence, detected_text = await self._ocr_detect(
                        frame=frame,
                        trigger_text=trigger_text
                    )

                    # v14.0: Get all OCR text for activity/near-miss detection
                    all_text = await self._ocr_get_all_text(frame)

                    if detected:
                        detection_time = time.time() - start_time
                        logger.info(
                            f"[Ferrari Detection] ✅ FOUND '{trigger_text}'! "
                            f"Time: {detection_time:.2f}s, Confidence: {confidence:.2f}, "
                            f"Frames: {frame_count}, OCR checks: {ocr_checks}"
                        )

                        # v14.0: Cleanup narration state
                        self._cleanup_narration_state(watcher_id)

                        return {
                            'detected': True,
                            'confidence': confidence,
                            'trigger': detected_text,
                            'detection_time': detection_time,
                            'frames_checked': frame_count,
                            'ocr_checks': ocr_checks,
                            'method': frame_data.get('method', 'screencapturekit')
                        }

                    # v14.0: Near-miss detection - found interesting text but not the trigger
                    if self.config.working_out_loud_enabled and all_text:
                        # Check for near-miss (similar but not matching text)
                        similarity = self._calculate_text_similarity(trigger_text, all_text)
                        if 0.3 <= similarity < 0.8:  # Similar but not a match
                            # Extract the most relevant portion of detected text
                            near_miss_text = self._extract_near_miss_text(all_text, trigger_text)
                            if near_miss_text:
                                await self._narrate_near_miss(
                                    watcher_id=watcher_id,
                                    trigger_text=trigger_text,
                                    detected_text=near_miss_text,
                                    app_name=app_name,
                                    similarity=similarity
                                )

                        # v14.0: Activity detection - significant screen change
                        current_hash = hash(all_text)
                        if last_ocr_hash != 0 and current_hash != last_ocr_hash:
                            # Text changed - detect type of change
                            text_change_ratio = self._calculate_text_change_ratio(
                                last_all_text, all_text
                            )
                            if text_change_ratio > 0.5:  # More than 50% change
                                activity_desc = self._describe_activity(
                                    last_all_text, all_text, text_change_ratio
                                )
                                if activity_desc:
                                    await self._narrate_activity(
                                        watcher_id=watcher_id,
                                        activity_description=activity_desc,
                                        app_name=app_name
                                    )

                        last_ocr_hash = current_hash
                        last_all_text = all_text

                    # Log periodic progress
                    if ocr_checks % 10 == 0:
                        logger.debug(
                            f"[Ferrari Detection] Progress: {ocr_checks} OCR checks, "
                            f"{frame_count} frames, {elapsed:.1f}s elapsed"
                        )

                # Small sleep to prevent busy-wait
                await asyncio.sleep(0.05)

        except Exception as e:
            logger.exception(f"[Ferrari Detection] Error: {e}")

            # v14.0: Cleanup narration state
            self._cleanup_narration_state(watcher_id)

            return {
                'detected': False,
                'confidence': 0.0,
                'error': str(e),
                'frames_checked': frame_count,
                'ocr_checks': ocr_checks
            }

    async def _ocr_get_all_text(self, frame: Any) -> str:
        """
        Extract all text from a frame for activity/near-miss detection.

        Args:
            frame: Numpy array (RGB)

        Returns:
            All detected text concatenated, or empty string if failed
        """
        try:
            if not self._detector:
                return ""

            # Try to get all text from the detector
            if hasattr(self._detector, 'get_all_text'):
                return await self._detector.get_all_text(frame)
            elif hasattr(self._detector, 'detect_all_text'):
                result = await self._detector.detect_all_text(frame)
                return result.get('text', '') if result else ''

            return ""
        except Exception as e:
            logger.debug(f"[OCR] All text extraction failed: {e}")
            return ""

    def _extract_near_miss_text(self, all_text: str, trigger_text: str) -> Optional[str]:
        """
        Extract the portion of all_text most similar to trigger_text.

        Returns the most relevant near-miss phrase, or None if not found.
        """
        if not all_text or not trigger_text:
            return None

        # Split into lines/sentences and find the most similar
        lines = [line.strip() for line in all_text.split('\n') if line.strip()]

        best_match = None
        best_similarity = 0.0

        for line in lines[:20]:  # Limit to first 20 lines for performance
            similarity = self._calculate_text_similarity(trigger_text, line)
            if similarity > best_similarity and similarity < 0.8:  # Near miss, not match
                best_similarity = similarity
                best_match = line

        return best_match if best_similarity > 0.3 else None

    def _calculate_text_change_ratio(self, old_text: str, new_text: str) -> float:
        """
        Calculate how much the text has changed between frames.

        Returns value between 0.0 (no change) and 1.0 (completely different).
        """
        if not old_text and not new_text:
            return 0.0
        if not old_text or not new_text:
            return 1.0

        old_words = set(old_text.lower().split())
        new_words = set(new_text.lower().split())

        if not old_words and not new_words:
            return 0.0

        union = old_words | new_words
        if not union:
            return 0.0

        changed = len(old_words ^ new_words)  # Symmetric difference
        return changed / len(union)

    def _describe_activity(
        self,
        old_text: str,
        new_text: str,
        change_ratio: float
    ) -> Optional[str]:
        """
        Generate a human-readable description of detected activity.

        Returns a description like "Content is changing rapidly" or None.
        """
        if change_ratio > 0.8:
            return "Content changed dramatically"
        elif change_ratio > 0.5:
            return "Significant activity detected"
        elif change_ratio > 0.3:
            return "Screen content is updating"

        # Try to detect specific patterns
        new_lower = new_text.lower()
        old_lower = old_text.lower()

        if 'loading' in new_lower and 'loading' not in old_lower:
            return "Loading indicator appeared"
        elif 'error' in new_lower and 'error' not in old_lower:
            return "An error message appeared"
        elif 'success' in new_lower and 'success' not in old_lower:
            return "A success message appeared"
        elif 'complete' in new_lower and 'complete' not in old_lower:
            return "Something completed"

        return None

    async def _ocr_detect(
        self,
        frame: Any,
        trigger_text: str
    ) -> tuple[bool, float, Optional[str]]:
        """
        Run OCR on frame and check for trigger text.

        Args:
            frame: Numpy array (RGB)
            trigger_text: Text to search for

        Returns:
            (detected, confidence, detected_text)
        """
        try:
            if not self._detector:
                # No detector available - fallback to simple presence check
                return False, 0.0, None

            # Run OCR detection via visual event detector (async method)
            result = await self._detector.detect_text(
                frame=frame,
                target_text=trigger_text,
                case_sensitive=False,
                fuzzy=True
            )

            if result and result.get('found', False):
                return True, result.get('confidence', 0.9), result.get('text', trigger_text)

            return False, 0.0, None

        except Exception as e:
            logger.debug(f"[OCR] Detection error: {e}")
            return False, 0.0, None

    async def _spawn_ferrari_watcher(
        self,
        window_id: int,
        fps: int,
        app_name: str,
        space_id: int
    ) -> Optional[Any]:
        """
        Spawn a Ferrari Engine VideoWatcher for window-specific surveillance.

        This is the v12.0 core - direct VideoWatcher creation that automatically
        uses the ScreenCaptureKit Ferrari Engine for 60 FPS GPU-accelerated capture.

        Args:
            window_id: Window ID to monitor
            fps: Target FPS (adaptive - will throttle for static content)
            app_name: Application name (for logging)
            space_id: macOS space ID

        Returns:
            VideoWatcher instance or None if failed
        """
        try:
            from backend.vision.macos_video_capture_advanced import VideoWatcher, WatcherConfig

            logger.info(
                f"🏎️  Spawning Ferrari Engine watcher: "
                f"Window {window_id} ({app_name}), {fps} FPS"
            )

            # Create watcher configuration
            watcher_config = WatcherConfig(
                window_id=window_id,
                fps=fps,
                max_buffer_size=10  # Adaptive buffer
            )

            # Create VideoWatcher (will auto-select Ferrari Engine if available)
            watcher = VideoWatcher(watcher_config)

            # =====================================================================
            # ROOT CAUSE FIX: Timeout Protection for watcher.start() v6.0.1
            # =====================================================================
            # PROBLEM: watcher.start() can hang forever if:
            # - ScreenCaptureKit permission dialog is pending
            # - Window ID is invalid or stale
            # - macOS privacy/accessibility blocks the capture
            # - GPU/Metal initialization hangs
            #
            # SOLUTION: Wrap in asyncio.wait_for with configurable timeout
            # CONFIGURABLE: JARVIS_WATCHER_START_TIMEOUT env var (default 10s)
            # =====================================================================
            watcher_start_timeout = float(os.getenv('JARVIS_WATCHER_START_TIMEOUT', '10.0'))

            try:
                success = await asyncio.wait_for(
                    watcher.start(),
                    timeout=watcher_start_timeout
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"❌ Ferrari Engine watcher.start() timed out after {watcher_start_timeout}s "
                    f"for window {window_id} ({app_name}). "
                    f"Possible causes: permission dialog, invalid window, GPU hang."
                )
                # Try to clean up the watcher
                try:
                    await watcher.stop()
                except Exception:
                    pass
                return None

            if not success:
                logger.error(f"❌ Ferrari Engine watcher failed to start for window {window_id}")
                return None

            # =====================================================================
            # v6.1.0: Activate Purple Indicator for Visual Confirmation
            # =====================================================================
            # WHY: ScreenCaptureKit doesn't trigger the macOS purple indicator.
            # Users need visual confirmation that JARVIS is actively watching.
            # This starts a lightweight AVCaptureSession purely for the icon.
            # =====================================================================
            await self._ensure_purple_indicator()

            # Store watcher info
            watcher_id = watcher.watcher_id
            self._active_video_watchers[watcher_id] = {
                'watcher': watcher,
                'window_id': window_id,
                'app_name': app_name,
                'space_id': space_id,
                'fps': fps,
                'started_at': datetime.now().isoformat(),
                'config': watcher_config
            }

            logger.info(
                f"✅ Ferrari Engine watcher started: {watcher_id} "
                f"(Window {window_id}, {app_name})"
            )

            return watcher

        except ImportError as e:
            logger.error(f"❌ Ferrari Engine not available: {e}")
            logger.error("   VideoWatcher/WatcherConfig import failed")
            return None
        except Exception as e:
            logger.exception(f"❌ Failed to spawn Ferrari Engine watcher: {e}")
            return None

    def _estimate_window_id(self, app_name: str) -> int:
        """
        Estimate window ID for an app (fallback method).

        In production, this integrates with yabai or spatial awareness.
        For now, returns a placeholder ID.
        """
        # TODO: Integrate with yabai to get actual window ID
        # For now, return a placeholder
        import hashlib
        hash_val = int(hashlib.md5(app_name.encode()).hexdigest()[:8], 16)
        return hash_val % 10000 + 1000  # ID in range 1000-11000

    async def _monitor_and_alert(
        self,
        watcher: Any,
        trigger_text: str,
        app_name: str,
        space_id: int,
        action_config: Optional[ActionConfig] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Monitor watcher and send alert (or execute action!) when event detected.

        v11.0: Now supports autonomous action execution!

        This runs as a background task.

        Returns:
            Dict with detection and action results, or None if event not detected
        """
        action_result = None
        detected = False
        detection_confidence = 0.0

        try:
            logger.info(
                f"[{watcher.watcher_id}] Monitoring {app_name} for '{trigger_text}'"
            )

            if action_config:
                logger.info(
                    f"[{watcher.watcher_id}] 🎯 ACTIVE MODE: Will execute action when detected!"
                )

            # v12.0: Ferrari Engine frame monitoring with OCR detection
            # v14.0: Pass app_name for "Working Out Loud" narration
            result = await self._ferrari_visual_detection(
                watcher=watcher,
                trigger_text=trigger_text,
                timeout=self.config.default_timeout,
                app_name=app_name
            )

            if result.get('detected', False):
                detected = True
                detection_confidence = result.get('confidence', 0.0)
                self._total_events_detected += 1

                logger.info(
                    f"[{watcher.watcher_id}] ✅ Event detected! "
                    f"Trigger: '{trigger_text}', Confidence: {detection_confidence:.2f}"
                )

                # =====================================================================
                # ROOT CAUSE FIX: Robust Multi-Channel Detection Notification v2.0.0
                # =====================================================================
                # Send notifications via ALL available channels:
                # 1. TTS Voice (UnifiedVoiceOrchestrator)
                # 2. WebSocket (real-time frontend updates)
                # 3. Legacy TTS callback (fallback compatibility)
                # =====================================================================
                await self._send_detection_notification(
                    trigger_text=trigger_text,
                    app_name=app_name,
                    space_id=space_id,
                    confidence=detection_confidence,
                    window_id=watcher.window_id if hasattr(watcher, 'window_id') else None
                )

                # Send alert
                await self._send_alert(
                    app_name=app_name,
                    trigger_text=trigger_text,
                    space_id=space_id,
                    confidence=detection_confidence,
                    detection_time=result.get('detection_time', 0.0)
                )

                # v11.0: EXECUTE ACTION if configured!
                if action_config and self.config.enable_action_execution:
                    logger.info(
                        f"[{watcher.watcher_id}] 🚀 Executing action: {action_config.action_type.value}"
                    )

                    # VOICE NARRATION: Announce autonomous action
                    if self._tts_callback:
                        try:
                            await self._tts_callback(
                                "I am now taking control. Executing autonomous action."
                            )
                        except Exception as e:
                            logger.warning(f"TTS failed: {e}")

                    # Extract window_id for surgical cross-space targeting
                    window_id = watcher.window_id if hasattr(watcher, 'window_id') else None

                    action_result = await self._execute_response(
                        trigger_text=trigger_text,
                        detected_text=result.get('trigger', trigger_text),  # Actual detected text
                        action_config=action_config,
                        app_name=app_name,
                        space_id=space_id,
                        window_id=window_id,  # THE KEY: Pass window_id for Ghost Hands targeting!
                        confidence=detection_confidence
                    )

                    if action_result and action_result.success:
                        logger.info(
                            f"[{watcher.watcher_id}] ✅ Action executed successfully! "
                            f"Goal: {action_result.goal_executed}"
                        )
                        self._total_actions_succeeded += 1

                        # VOICE NARRATION: Announce success
                        if self._tts_callback:
                            try:
                                await self._tts_callback(
                                    "Action completed successfully. Autonomous task finished."
                                )
                            except Exception as e:
                                logger.warning(f"TTS failed: {e}")
                    else:
                        logger.warning(
                            f"[{watcher.watcher_id}] ❌ Action failed: "
                            f"{action_result.error if action_result else 'Unknown error'}"
                        )
                        self._total_actions_failed += 1

                        # VOICE NARRATION: Announce failure
                        if self._tts_callback:
                            try:
                                error_msg = action_result.error if action_result else 'Unknown error'
                                await self._tts_callback(
                                    f"Action failed: {error_msg}"
                                )
                            except Exception as e:
                                logger.warning(f"TTS failed: {e}")

                # Store in knowledge graph (with action result if available)
                if self.knowledge_graph:
                    knowledge_data = {
                        "type": "visual_event_detected",
                        "app_name": app_name,
                        "trigger_text": trigger_text,
                        "space_id": space_id,
                        "confidence": detection_confidence,
                        "detection_time": result.get('detection_time', 0.0),
                        "timestamp": datetime.now().isoformat(),
                    }

                    # v11.0: Add action execution result
                    if action_result:
                        knowledge_data["action_executed"] = {
                            "success": action_result.success,
                            "action_type": action_result.action_type.value,
                            "goal": action_result.goal_executed,
                            "error": action_result.error,
                            "duration_ms": action_result.duration_ms
                        }

                    await self.add_knowledge(
                        knowledge_type=KnowledgeType.OBSERVATION,
                        data=knowledge_data,
                        confidence=result.confidence,
                    )
            else:
                logger.info(
                    f"[{watcher.watcher_id}] ⏱️ Timeout waiting for '{trigger_text}'"
                )

            # Cleanup
            await self._stop_watcher_by_id(watcher.watcher_id)

            # Return comprehensive result
            if detected and action_result:
                # Event detected and action executed
                return {
                    "success": action_result.success,
                    "detected": True,
                    "confidence": detection_confidence,
                    "action_type": action_result.action_type.value,
                    "goal_executed": action_result.goal_executed,
                    "error": action_result.error,
                    "duration_ms": action_result.duration_ms
                }
            elif detected:
                # Event detected but no action (passive mode)
                return {
                    "success": True,
                    "detected": True,
                    "confidence": detection_confidence
                }
            else:
                # Timeout - event not detected
                return {
                    "success": False,
                    "detected": False,
                    "error": "Event not detected within timeout"
                }

        except Exception as e:
            logger.exception(f"Error in monitor_and_alert: {e}")
            await self._stop_watcher_by_id(watcher.watcher_id)
            return {
                "success": False,
                "detected": False,
                "error": f"Monitoring error: {str(e)}"
            }

    async def _send_detection_notification(
        self,
        trigger_text: str,
        app_name: str,
        space_id: int,
        confidence: float,
        window_id: Optional[int] = None
    ):
        """
        ROOT CAUSE FIX: Robust Multi-Channel Detection Notification v2.0.0

        Sends real-time notifications through ALL available channels:
        1. TTS Voice (UnifiedVoiceOrchestrator) - primary
        2. WebSocket (real-time frontend updates) - for UI transparency
        3. Legacy TTS callback - backward compatibility

        Features:
        - Configurable notification templates (no hardcoding!)
        - Async parallel delivery
        - Graceful degradation
        - Rich detection metadata
        """
        logger.info(
            f"[Detection] 📢 Notifying user: '{trigger_text}' detected in {app_name} "
            f"(Space {space_id}, confidence: {confidence:.2%})"
        )

        # =====================================================================
        # 1. TTS VOICE NOTIFICATION (UnifiedVoiceOrchestrator)
        # =====================================================================
        if VOICE_ORCHESTRATOR_AVAILABLE:
            try:
                # Load configurable notification templates
                import os
                detection_template = os.getenv(
                    "JARVIS_DETECTION_TTS_TEMPLATE",
                    "I found {trigger} in {app_name}"
                )

                # Format message with detected values
                tts_message = detection_template.format(
                    trigger=trigger_text,
                    app_name=app_name,
                    space_id=space_id,
                    confidence=f"{confidence:.0%}"
                )

                # Get voice orchestrator and speak
                orchestrator = get_voice_orchestrator()

                # Start orchestrator if not running
                if not orchestrator._running:
                    await orchestrator.start()

                # Speak with HIGH priority (detection is important!)
                await orchestrator.speak(
                    text=tts_message,
                    priority=VoicePriority.HIGH,
                    source=VoiceSource.AGENT,
                    wait=False,  # Don't block - async
                    topic=SpeechTopic.MONITORING
                )

                logger.info(f"[Detection] ✅ TTS notification sent: '{tts_message}'")

            except Exception as e:
                logger.error(f"[Detection] TTS notification failed: {e}", exc_info=True)
        else:
            # Fallback to legacy TTS callback
            if self._tts_callback:
                try:
                    await self._tts_callback(
                        f"Event detected! I found {trigger_text} in {app_name}."
                    )
                    logger.info(f"[Detection] ✅ Legacy TTS callback sent")
                except Exception as e:
                    logger.warning(f"[Detection] Legacy TTS callback failed: {e}")

        # =====================================================================
        # 2. WEBSOCKET REAL-TIME NOTIFICATION (Frontend Transparency)
        # =====================================================================
        if WEBSOCKET_MANAGER_AVAILABLE:
            try:
                ws_manager = get_ws_manager()

                # Create rich detection payload
                detection_payload = {
                    "type": "visual_detection",
                    "event": "detection_found",
                    "data": {
                        "trigger_text": trigger_text,
                        "app_name": app_name,
                        "space_id": space_id,
                        "window_id": window_id,
                        "confidence": confidence,
                        "timestamp": datetime.now().isoformat(),
                        "agent": self.agent_name,
                        "notification_channels": {
                            "tts": VOICE_ORCHESTRATOR_AVAILABLE,
                            "websocket": True,
                            "legacy_callback": self._tts_callback is not None
                        }
                    }
                }

                # Broadcast to ALL connected WebSocket clients
                await ws_manager.broadcast_to_all(detection_payload)

                logger.info(
                    f"[Detection] ✅ WebSocket notification broadcast to "
                    f"{len(ws_manager.connections)} clients"
                )

            except Exception as e:
                logger.error(f"[Detection] WebSocket notification failed: {e}", exc_info=True)

        # =====================================================================
        # 3. METRICS & LOGGING
        # =====================================================================
        # Log rich detection event for analytics
        detection_event = {
            "event_type": "visual_detection",
            "trigger": trigger_text,
            "app": app_name,
            "space": space_id,
            "window": window_id,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat(),
            "notification_methods": {
                "voice_orchestrator": VOICE_ORCHESTRATOR_AVAILABLE,
                "websocket": WEBSOCKET_MANAGER_AVAILABLE,
                "legacy_callback": self._tts_callback is not None
            }
        }

        logger.info(f"[Detection] Event logged: {json.dumps(detection_event, indent=2)}")

    # =========================================================================
    # v14.0: "Working Out Loud" - Narration Methods
    # =========================================================================

    def _get_narration_state(self, watcher_id: str) -> NarrationState:
        """Get or create narration state for a watcher."""
        if watcher_id not in self._narration_states:
            self._narration_states[watcher_id] = NarrationState()
        return self._narration_states[watcher_id]

    def _cleanup_narration_state(self, watcher_id: str) -> None:
        """Cleanup narration state when watcher stops."""
        self._narration_states.pop(watcher_id, None)

    async def _narrate_working_out_loud(
        self,
        message: str,
        narration_type: str,
        watcher_id: str,
        priority: str = "low"
    ) -> bool:
        """
        Send a "Working Out Loud" narration via TTS.

        Uses UnifiedVoiceOrchestrator for consistent voice output.
        Falls back to legacy TTS callback if orchestrator unavailable.

        Args:
            message: The narration message
            narration_type: "heartbeat", "near_miss", or "activity"
            watcher_id: Watcher this narration is for
            priority: Voice priority (low, normal, high)

        Returns:
            True if narration was sent, False otherwise
        """
        if not self.config.working_out_loud_enabled:
            return False

        state = self._get_narration_state(watcher_id)

        # Check rate limits and cooldowns
        if not state.can_narrate(narration_type, self.config):
            logger.debug(
                f"[Working Out Loud] Skipping {narration_type} narration - rate limited"
            )
            return False

        # Check content similarity (avoid repetition)
        if state.is_similar_content(message, narration_type):
            logger.debug(
                f"[Working Out Loud] Skipping {narration_type} - similar content"
            )
            return False

        # Attempt narration via UnifiedVoiceOrchestrator (preferred)
        narrated = False
        try:
            if VOICE_ORCHESTRATOR_AVAILABLE:
                orchestrator = await get_voice_orchestrator()
                if orchestrator:
                    # Map priority to VoicePriority
                    voice_priority = VoicePriority.LOW
                    if priority == "normal":
                        voice_priority = VoicePriority.NORMAL
                    elif priority == "high":
                        voice_priority = VoicePriority.HIGH

                    await orchestrator.speak_async(
                        text=message,
                        priority=voice_priority,
                        source=VoiceSource.VISION,
                        topic=SpeechTopic.STATUS_UPDATE,
                    )
                    narrated = True
                    logger.debug(f"[Working Out Loud] {narration_type}: {message}")
        except Exception as e:
            logger.debug(f"[Working Out Loud] Voice orchestrator failed: {e}")

        # Fallback to legacy TTS callback
        if not narrated and self._tts_callback:
            try:
                await self._tts_callback(message)
                narrated = True
                logger.debug(f"[Working Out Loud] (legacy) {narration_type}: {message}")
            except Exception as e:
                logger.debug(f"[Working Out Loud] Legacy TTS failed: {e}")

        # Record the narration for rate limiting
        if narrated:
            state.record_narration(narration_type, message)

        return narrated

    async def _narrate_heartbeat(
        self,
        watcher_id: str,
        trigger_text: str,
        app_name: str,
        elapsed_seconds: float,
        frames_checked: int,
        ocr_checks: int
    ) -> bool:
        """
        Narrate heartbeat status update during monitoring.

        Examples:
        - "Still watching Chrome for 'Build Complete'. 45 seconds in."
        - "Monitoring Terminal for 'Error'. So far so good, 2 minutes elapsed."
        """
        # Format elapsed time naturally
        if elapsed_seconds < 60:
            time_str = f"{int(elapsed_seconds)} seconds"
        elif elapsed_seconds < 3600:
            minutes = int(elapsed_seconds // 60)
            time_str = f"{minutes} minute{'s' if minutes != 1 else ''}"
        else:
            hours = int(elapsed_seconds // 3600)
            minutes = int((elapsed_seconds % 3600) // 60)
            time_str = f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minutes"

        # Vary the message based on verbosity and consecutive heartbeats
        state = self._get_narration_state(watcher_id)

        if self.config.narration_verbosity == "minimal":
            message = f"Still watching {app_name}. {time_str} elapsed."
        elif self.config.narration_verbosity == "verbose":
            message = (
                f"Still monitoring {app_name} for '{trigger_text}'. "
                f"{time_str} elapsed. Checked {ocr_checks} frames so far."
            )
        elif self.config.narration_verbosity == "debug":
            message = (
                f"Heartbeat: {app_name}, trigger='{trigger_text}', "
                f"elapsed={elapsed_seconds:.1f}s, frames={frames_checked}, OCR={ocr_checks}"
            )
        else:  # normal
            # Vary messages to avoid monotony
            variants = [
                f"Still watching {app_name} for '{trigger_text}'. {time_str} in.",
                f"Monitoring {app_name}. {time_str} elapsed, still looking.",
                f"Keeping an eye on {app_name} for '{trigger_text}'.",
            ]
            import random
            message = random.choice(variants)

        return await self._narrate_working_out_loud(
            message=message,
            narration_type="heartbeat",
            watcher_id=watcher_id,
            priority="low"
        )

    async def _narrate_near_miss(
        self,
        watcher_id: str,
        trigger_text: str,
        detected_text: str,
        app_name: str,
        similarity: float = 0.0
    ) -> bool:
        """
        Narrate when we detect text that's close to but not the trigger.

        Examples:
        - "I see 'Build Started' but waiting for 'Build Complete'."
        - "Detected 'Error loading' - close but not quite 'Error: Failed'."
        """
        # Extract key differences for context
        if len(detected_text) > 50:
            detected_short = detected_text[:47] + "..."
        else:
            detected_short = detected_text

        if self.config.narration_verbosity == "minimal":
            message = f"Saw something on {app_name}, but not the trigger yet."
        elif self.config.narration_verbosity == "verbose":
            message = (
                f"Near miss on {app_name}: Detected '{detected_short}' "
                f"but waiting for '{trigger_text}'. Similarity: {similarity:.0%}"
            )
        else:  # normal
            message = f"I see '{detected_short}' on {app_name}, but still waiting for '{trigger_text}'."

        return await self._narrate_working_out_loud(
            message=message,
            narration_type="near_miss",
            watcher_id=watcher_id,
            priority="low"
        )

    async def _narrate_activity(
        self,
        watcher_id: str,
        activity_description: str,
        app_name: str
    ) -> bool:
        """
        Narrate significant activity detected on the monitored window.

        Examples:
        - "Activity detected on Chrome - the page is loading."
        - "Terminal output is scrolling rapidly."
        """
        message = f"{activity_description} on {app_name}."

        return await self._narrate_working_out_loud(
            message=message,
            narration_type="activity",
            watcher_id=watcher_id,
            priority="low"
        )

    # =========================================================================
    # v15.0: Watcher Status Query - "Are you still watching?"
    # =========================================================================

    async def get_active_watcher_status(self) -> Dict[str, Any]:
        """
        Get status of all active watchers for user queries like "are you still watching?".

        Returns:
            {
                'active_watchers': int,
                'watchers': [
                    {
                        'task_id': str,
                        'app_name': str,
                        'trigger_text': str,
                        'elapsed_seconds': float,
                        'status': 'monitoring' | 'pending' | 'completed'
                    }
                ],
                'summary': str  # Human-readable summary
            }
        """
        import time
        now = time.time()

        if not hasattr(self, '_god_mode_tasks') or not self._god_mode_tasks:
            return {
                'active_watchers': 0,
                'watchers': [],
                'summary': "I'm not currently watching anything."
            }

        watchers = []
        for task_id, task in list(self._god_mode_tasks.items()):
            # Parse task_id for app name and trigger (format: god_mode_AppName_timestamp)
            parts = task_id.split('_')
            app_name = parts[2] if len(parts) > 2 else "Unknown"

            # Get narration state for timing info
            state = self._narration_states.get(task_id)
            elapsed = now - state.monitoring_start_time if state else 0

            status = 'completed' if task.done() else 'monitoring'

            watchers.append({
                'task_id': task_id,
                'app_name': app_name,
                'elapsed_seconds': elapsed,
                'status': status
            })

        active_count = sum(1 for w in watchers if w['status'] == 'monitoring')

        # Build summary message
        if active_count == 0:
            summary = "I finished monitoring. No active watchers."
        elif active_count == 1:
            w = next(w for w in watchers if w['status'] == 'monitoring')
            minutes = int(w['elapsed_seconds'] // 60)
            seconds = int(w['elapsed_seconds'] % 60)
            if minutes > 0:
                summary = f"Yes! I'm still watching {w['app_name']}. {minutes} minutes and {seconds} seconds in."
            else:
                summary = f"Yes! I'm still watching {w['app_name']}. {seconds} seconds in."
        else:
            apps = [w['app_name'] for w in watchers if w['status'] == 'monitoring']
            summary = f"Yes! I'm watching {active_count} apps: {', '.join(apps)}."

        return {
            'active_watchers': active_count,
            'watchers': watchers,
            'summary': summary
        }

    async def narrate_watcher_status(self) -> bool:
        """
        Narrate the current watcher status via TTS.

        Called when user asks "are you still watching?" or similar.
        """
        status = await self.get_active_watcher_status()

        if self.config.working_out_loud_enabled:
            # Use a special watcher_id for status queries
            return await self._narrate_working_out_loud(
                message=status['summary'],
                narration_type="startup",  # Use startup type to bypass MIN_GAP
                watcher_id="status_query",
                priority="normal"
            )

        return False

    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """
        Calculate similarity between two text strings.

        Uses a simple character-level comparison for speed.
        Returns value between 0.0 (no match) and 1.0 (exact match).
        """
        if not text1 or not text2:
            return 0.0

        t1 = text1.lower().strip()
        t2 = text2.lower().strip()

        if t1 == t2:
            return 1.0

        # Check for substring match
        if t1 in t2 or t2 in t1:
            return 0.8

        # Simple character overlap
        set1 = set(t1.split())
        set2 = set(t2.split())

        if not set1 or not set2:
            return 0.0

        intersection = len(set1 & set2)
        union = len(set1 | set2)

        return intersection / union if union > 0 else 0.0

    async def _send_alert(
        self,
        app_name: str,
        trigger_text: str,
        space_id: int,
        confidence: float,
        detection_time: float
    ):
        """Send voice alert and notification when event detected."""
        self._total_alerts_sent += 1

        # Voice alert
        if self.config.enable_voice_alerts:
            await self._send_voice_alert(app_name, trigger_text, space_id)

        # macOS notification
        if self.config.enable_notifications:
            await self._send_notification(app_name, trigger_text, space_id)

        # Broadcast event to other agents (only if connected)
        if self.message_bus:
            try:
                await self.broadcast(
                    message_type=MessageType.ANNOUNCEMENT,
                    payload={
                        "event": "visual_event_detected",
                        "app_name": app_name,
                        "trigger_text": trigger_text,
                        "space_id": space_id,
                        "confidence": confidence,
                        "detection_time": detection_time,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
            except RuntimeError:
                logger.debug("Message bus not available for broadcast")

    async def _send_voice_alert(
        self,
        app_name: str,
        trigger_text: str,
        space_id: int
    ):
        """Send voice alert via JARVIS Voice API."""
        try:
            # TODO: Integrate with JARVIS Voice API
            # For now, log the alert
            narration = f"{trigger_text} detected on {app_name}, Space {space_id}"
            logger.info(f"🔊 Voice Alert: {narration}")

            # In production, would call:
            # await jarvis_voice_api.speak(narration)

        except Exception as e:
            logger.error(f"Error sending voice alert: {e}")

    async def _send_notification(
        self,
        app_name: str,
        trigger_text: str,
        space_id: int
    ):
        """Send macOS notification."""
        try:
            # Use osascript for macOS notification
            import subprocess

            title = f"JARVIS - {app_name}"
            message = f"{trigger_text} (Space {space_id})"

            # Non-blocking notification (run in thread pool)
            await asyncio.to_thread(
                subprocess.run,
                ['osascript', '-e', f'display notification "{message}" with title "{title}"'],
                check=False,
                capture_output=True
            )

            logger.info(f"📬 Notification sent: {message}")

        except Exception as e:
            logger.error(f"Error sending notification: {e}")

    # =========================================================================
    # v11.0: Action Execution - "Watch & Act" Core Logic
    # =========================================================================

    async def _execute_response(
        self,
        trigger_text: str,
        detected_text: str,
        action_config: ActionConfig,
        app_name: str,
        space_id: int,
        window_id: Optional[int] = None,  # THE KEY: Exact window for cross-space targeting
        confidence: float = 0.0
    ) -> Optional[ActionExecutionResult]:
        """
        Execute configured action in response to visual event detection.

        This is the CORE of "Watch & Act" - the autonomous loop closer!

        v13.0 GHOST HANDS INTEGRATION:
        - When ActionType.GHOST_HANDS is used, we SKIP focus switching entirely
        - Instead, we use the YabaiAwareActuator to execute actions on the
          exact window_id, even if it's on a different Space
        - User's focus is NEVER stolen - they stay on their current Space

        Flow:
        1. For GHOST_HANDS: Execute via YabaiAwareActuator (NO focus switch!)
        2. For other types: Switch to target window (via SpatialAwarenessAgent)
        3. Execute action based on type:
           - GHOST_HANDS: Cross-space click/type via Ghost Hands
           - SIMPLE_GOAL: Use Computer Use for natural language goal
           - CONDITIONAL: Evaluate conditions and execute matching action
           - WORKFLOW: Delegate to AgenticTaskRunner for complex workflows
           - NOTIFICATION/VOICE_ALERT: Passive modes (already handled)
        4. Return execution result

        Args:
            trigger_text: Original trigger pattern
            detected_text: Actual detected text (may differ slightly)
            action_config: Configuration for action to execute
            app_name: Application where event detected
            space_id: Space ID where event detected
            window_id: EXACT window ID for surgical cross-space targeting
            confidence: Detection confidence

        Returns:
            ActionExecutionResult with execution details
        """
        import time
        start_time = time.time()

        logger.info(
            f"[ACTION EXECUTION] Type: {action_config.action_type.value}, "
            f"App: {app_name}, Space: {space_id}, Window: {window_id}"
        )

        try:
            self._total_actions_executed += 1

            # =================================================================
            # v13.0 GHOST HANDS: Zero-Focus Cross-Space Execution
            # =================================================================
            # If GHOST_HANDS action type, we NEVER switch focus!
            # Instead, we use YabaiAwareActuator to surgically interact
            # with the exact window, even on a hidden Space.
            # =================================================================
            if action_config.action_type == ActionType.GHOST_HANDS:
                return await self._execute_ghost_hands(
                    trigger_text=trigger_text,
                    detected_text=detected_text,
                    action_config=action_config,
                    app_name=app_name,
                    space_id=space_id,
                    window_id=window_id,
                    start_time=start_time
                )

            # Step 1: Switch to target window if requested (LEGACY path)
            # GHOST_HANDS skips this entirely (handled above)
            if action_config.switch_to_window and action_config.action_type != ActionType.GHOST_HANDS:
                await self._switch_to_app(app_name, space_id)

            # Step 2: Execute action based on type
            if action_config.action_type == ActionType.SIMPLE_GOAL:
                return await self._execute_simple_goal(
                    goal=action_config.goal,
                    app_name=app_name,
                    narrate=action_config.narrate,
                    timeout=action_config.timeout_seconds,
                    start_time=start_time
                )

            elif action_config.action_type == ActionType.CONDITIONAL:
                return await self._execute_conditional(
                    detected_text=detected_text,
                    conditions=action_config.conditions,
                    default_action=action_config.default_action,
                    app_name=app_name,
                    narrate=action_config.narrate,
                    timeout=action_config.timeout_seconds,
                    start_time=start_time
                )

            elif action_config.action_type == ActionType.WORKFLOW:
                return await self._execute_workflow(
                    workflow_goal=action_config.workflow_goal,
                    workflow_context=action_config.workflow_context,
                    app_name=app_name,
                    narrate=action_config.narrate,
                    timeout=action_config.timeout_seconds,
                    start_time=start_time
                )

            elif action_config.action_type in [ActionType.NOTIFICATION, ActionType.VOICE_ALERT]:
                # Passive modes - already handled by _send_alert
                duration_ms = (time.time() - start_time) * 1000
                return ActionExecutionResult(
                    success=True,
                    action_type=action_config.action_type,
                    goal_executed="Passive notification",
                    duration_ms=duration_ms
                )

            else:
                raise ValueError(f"Unknown action type: {action_config.action_type}")

        except Exception as e:
            logger.exception(f"[ACTION EXECUTION] Failed: {e}")
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=action_config.action_type,
                error=str(e),
                duration_ms=duration_ms
            )

    async def _switch_to_app(self, app_name: str, space_id: int) -> bool:
        """
        Switch to target app/window using SpatialAwarenessAgent.

        This is the "teleport" step before executing actions.
        """
        try:
            logger.info(f"[TELEPORT] Switching to {app_name} on Space {space_id}")

            if hasattr(self, 'coordinator') and self.coordinator:
                # Use SpatialAwarenessAgent to switch
                result = await self.coordinator.request(
                    to_agent="spatial_awareness_agent",
                    payload={
                        "action": "switch_to_app",
                        "app_name": app_name,
                        "space_id": space_id
                    }
                )

                if result and result.get("success"):
                    logger.info(f"[TELEPORT] ✅ Switched to {app_name}")
                    await asyncio.sleep(0.5)  # Wait for app to focus
                    return True
                else:
                    logger.warning(f"[TELEPORT] Failed to switch: {result}")
                    return False
            else:
                logger.warning("[TELEPORT] No coordinator available - skipping switch")
                return False

        except Exception as e:
            logger.error(f"[TELEPORT] Error switching to app: {e}")
            return False

    async def _execute_simple_goal(
        self,
        goal: Optional[str],
        app_name: str,
        narrate: bool,
        timeout: float,
        start_time: float
    ) -> ActionExecutionResult:
        """
        Execute simple goal using ClaudeComputerUseConnector.

        Example: "Click the Deploy button"
        """
        if not goal:
            raise ValueError("Simple goal action requires a goal")

        if not self._computer_use_connector:
            raise RuntimeError("Computer Use connector not available")

        logger.info(f"[SIMPLE GOAL] Executing: {goal}")

        try:
            # Execute via Computer Use
            task_result = await self._computer_use_connector.execute_task(
                goal=goal,
                context={"app_name": app_name},
                narrate=narrate
            )

            duration_ms = (time.time() - start_time) * 1000
            success = task_result.status.value in ["success", "SUCCESS"]

            return ActionExecutionResult(
                success=success,
                action_type=ActionType.SIMPLE_GOAL,
                goal_executed=goal,
                error=None if success else task_result.final_message,
                duration_ms=duration_ms,
                computer_use_result=asdict(task_result),
                narration=task_result.narration_log if hasattr(task_result, 'narration_log') else []
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.SIMPLE_GOAL,
                goal_executed=goal,
                error=str(e),
                duration_ms=duration_ms
            )

    async def _execute_conditional(
        self,
        detected_text: str,
        conditions: List[ConditionalAction],
        default_action: Optional[str],
        app_name: str,
        narrate: bool,
        timeout: float,
        start_time: float
    ) -> ActionExecutionResult:
        """
        Execute conditional action based on detected text.

        Example conditions:
        - If "Error" -> "Click Retry button"
        - If "Success" -> "Click Deploy button"
        """
        logger.info(f"[CONDITIONAL] Evaluating {len(conditions)} conditions")

        # Find matching condition
        matched_condition = None
        for condition in conditions:
            if self._matches_condition(detected_text, condition):
                matched_condition = condition
                logger.info(
                    f"[CONDITIONAL] ✅ Matched: '{condition.trigger_pattern}' "
                    f"-> '{condition.action_goal}'"
                )
                break

        # Determine which action to execute
        if matched_condition:
            action_goal = matched_condition.action_goal
        elif default_action:
            logger.info(f"[CONDITIONAL] No match, using default action")
            action_goal = default_action
        else:
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.CONDITIONAL,
                error="No conditions matched and no default action provided",
                duration_ms=duration_ms
            )

        # Execute the matched action via simple goal
        logger.info(f"[CONDITIONAL] Executing action: {action_goal}")
        return await self._execute_simple_goal(
            goal=action_goal,
            app_name=app_name,
            narrate=narrate,
            timeout=timeout,
            start_time=start_time
        )

    def _matches_condition(self, detected_text: str, condition: ConditionalAction) -> bool:
        """Check if detected text matches a conditional pattern."""
        import re

        search_text = detected_text if condition.case_sensitive else detected_text.lower()
        pattern = condition.trigger_pattern if condition.case_sensitive else condition.trigger_pattern.lower()

        if condition.use_regex:
            return bool(re.search(pattern, search_text))
        else:
            return pattern in search_text

    async def _execute_workflow(
        self,
        workflow_goal: Optional[str],
        workflow_context: Dict[str, Any],
        app_name: str,
        narrate: bool,
        timeout: float,
        start_time: float
    ) -> ActionExecutionResult:
        """
        Execute complex workflow using AgenticTaskRunner.

        Example: "Check if tests pass, if so deploy to staging, then notify team"
        """
        if not workflow_goal:
            raise ValueError("Workflow action requires a workflow_goal")

        if not self._agentic_task_runner:
            # Fallback to simple goal via Computer Use
            logger.warning("[WORKFLOW] AgenticTaskRunner unavailable, falling back to Computer Use")
            return await self._execute_simple_goal(
                goal=workflow_goal,
                app_name=app_name,
                narrate=narrate,
                timeout=timeout,
                start_time=start_time
            )

        logger.info(f"[WORKFLOW] Executing complex workflow: {workflow_goal}")

        try:
            # Execute via AgenticTaskRunner
            # Note: Implementation depends on AgenticTaskRunner interface
            # This is a placeholder - adjust based on actual interface
            result = await self._agentic_task_runner.execute_task(
                goal=workflow_goal,
                context={
                    "app_name": app_name,
                    **workflow_context
                },
                narrate=narrate,
                timeout=timeout
            )

            duration_ms = (time.time() - start_time) * 1000
            success = getattr(result, 'success', False)

            return ActionExecutionResult(
                success=success,
                action_type=ActionType.WORKFLOW,
                goal_executed=workflow_goal,
                error=None if success else getattr(result, 'error', 'Unknown error'),
                duration_ms=duration_ms,
                computer_use_result=result if hasattr(result, '__dict__') else None
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.WORKFLOW,
                goal_executed=workflow_goal,
                error=str(e),
                duration_ms=duration_ms
            )

    # =========================================================================
    # v13.0 GHOST HANDS: Zero-Focus Cross-Space Execution
    # =========================================================================
    # This is THE KEY INNOVATION: Execute actions on windows in OTHER Spaces
    # without EVER switching the user's focus. JARVIS becomes invisible.
    # =========================================================================

    async def _execute_ghost_hands(
        self,
        trigger_text: str,
        detected_text: str,
        action_config: ActionConfig,
        app_name: str,
        space_id: int,
        window_id: Optional[int],
        start_time: float
    ) -> ActionExecutionResult:
        """
        Execute action via Ghost Hands - ZERO FOCUS STEALING.

        This is the surgical, cross-space action execution that makes JARVIS
        truly invisible. The user NEVER sees their focus change.

        Flow:
        1. Initialize YabaiAwareActuator (lazy load)
        2. Use window_id to target exact window (THE GOLDEN PATH)
        3. Execute click/type via cross-space mechanisms
        4. User stays on their current Space, completely undisturbed

        Args:
            trigger_text: What triggered this action
            detected_text: Actual detected text
            action_config: Configuration including coordinates/element
            app_name: Application name
            space_id: Space where window lives
            window_id: EXACT window ID for surgical targeting
            start_time: Start time for duration tracking

        Returns:
            ActionExecutionResult with Ghost Hands execution details
        """
        import time

        logger.info(
            f"[GHOST HANDS] 👻 Zero-focus execution starting! "
            f"Window: {window_id}, Space: {space_id}, App: {app_name}"
        )

        # Voice narration: Announce invisible action
        if action_config.narrate and self._tts_callback:
            try:
                await self._tts_callback(
                    f"Ghost hands activated. Acting on {app_name} in background."
                )
            except Exception as e:
                logger.warning(f"[GHOST HANDS] TTS failed: {e}")

        try:
            # Lazy load the YabaiAwareActuator
            from ghost_hands.yabai_aware_actuator import get_yabai_actuator

            actuator = await get_yabai_actuator()

            if not actuator.yabai._initialized:
                raise RuntimeError("Yabai not available for cross-space actions")

            # Determine action to execute
            coordinates = action_config.ghost_hands_coordinates
            element = action_config.ghost_hands_element
            goal = action_config.goal  # Can use goal as fallback description

            # If no specific coordinates or element, click center of window
            if not coordinates and not element and window_id:
                window_info = await actuator.get_window_info(window_id)
                if window_info:
                    # Click center of window
                    coordinates = (
                        window_info.frame.width / 2,
                        window_info.frame.height / 2
                    )
                    logger.info(f"[GHOST HANDS] Auto-targeting center: {coordinates}")

            # Execute via YabaiAwareActuator
            if window_id:
                # THE GOLDEN PATH: We have exact window targeting!
                logger.info(
                    f"[GHOST HANDS] 🎯 Surgical click on window {window_id} "
                    f"(Space {space_id})"
                )

                report = await actuator.click(
                    window_id=window_id,
                    space_id=space_id,
                    selector=element,
                    coordinates=coordinates,
                )

                success = report.result.name == "SUCCESS"

                duration_ms = (time.time() - start_time) * 1000

                # Log the result
                if success:
                    logger.info(
                        f"[GHOST HANDS] ✅ Click executed via {report.backend_used} "
                        f"in {report.duration_ms:.0f}ms, focus preserved: {report.focus_preserved}"
                    )
                else:
                    logger.warning(
                        f"[GHOST HANDS] ❌ Click failed: {report.error}"
                    )

                # Voice narration: Announce result
                if action_config.narrate and self._tts_callback:
                    try:
                        if success:
                            await self._tts_callback("Ghost hands action complete.")
                        else:
                            await self._tts_callback(f"Ghost hands failed: {report.error}")
                    except Exception as e:
                        logger.warning(f"[GHOST HANDS] TTS failed: {e}")

                return ActionExecutionResult(
                    success=success,
                    action_type=ActionType.GHOST_HANDS,
                    goal_executed=goal or f"Click on window {window_id}",
                    error=report.error if not success else None,
                    duration_ms=duration_ms,
                    computer_use_result={
                        "backend": report.backend_used,
                        "focus_preserved": report.focus_preserved,
                        "target_space": report.target_space,
                        "window_id": report.window_id,
                    }
                )

            else:
                # Fallback: No window_id, try to discover from app_name
                logger.warning(
                    f"[GHOST HANDS] No window_id provided, discovering from {app_name}"
                )

                report = await actuator.click(
                    app_name=app_name,
                    space_id=space_id,
                    selector=element,
                    coordinates=coordinates,
                )

                success = report.result.name == "SUCCESS"
                duration_ms = (time.time() - start_time) * 1000

                return ActionExecutionResult(
                    success=success,
                    action_type=ActionType.GHOST_HANDS,
                    goal_executed=goal or f"Click on {app_name}",
                    error=report.error if not success else None,
                    duration_ms=duration_ms,
                    computer_use_result={
                        "backend": report.backend_used,
                        "focus_preserved": report.focus_preserved,
                        "discovered_window": report.window_id,
                    }
                )

        except ImportError as e:
            logger.error(f"[GHOST HANDS] Ghost Hands module not available: {e}")
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.GHOST_HANDS,
                goal_executed=action_config.goal,
                error=f"Ghost Hands not available: {e}",
                duration_ms=duration_ms
            )

        except Exception as e:
            logger.exception(f"[GHOST HANDS] Execution failed: {e}")
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.GHOST_HANDS,
                goal_executed=action_config.goal,
                error=str(e),
                duration_ms=duration_ms
            )

    async def _stop_watcher_by_id(self, watcher_id: str):
        """Stop watcher by ID and cleanup."""
        if watcher_id in self._active_watchers:
            info = self._active_watchers[watcher_id]
            watcher = info['watcher']

            # Stop watcher
            if self._watcher_manager:
                await self._watcher_manager.stop_watcher(watcher_id)

            # Cancel task
            if watcher_id in self._watch_tasks:
                task = self._watch_tasks[watcher_id]
                if not task.done():
                    task.cancel()
                del self._watch_tasks[watcher_id]

            # Remove from active watchers
            del self._active_watchers[watcher_id]

            logger.info(f"Stopped watcher {watcher_id}")

    async def _sync_state_loop(self):
        """Sync state to cross-repo file periodically."""
        while True:
            try:
                await asyncio.sleep(self.config.sync_interval_seconds)
                await self._sync_state()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in state sync: {e}")

    async def _sync_state(self):
        """Write current state to cross-repo file."""
        try:
            state_file = Path(self.config.cross_repo_dir).expanduser() / self.config.vmsi_state_file

            state = {
                "active_watchers": [
                    {
                        "watcher_id": watcher_id,
                        "app_name": info['app_name'],
                        "trigger_text": info['trigger_text'],
                        "space_id": info['space_id'],
                        "started_at": info['started_at'],
                        "status": "watching",
                        "repo": "JARVIS-AI-Agent"
                    }
                    for watcher_id, info in self._active_watchers.items()
                ],
                "stats": {
                    "total_watches_started": self._total_watches_started,
                    "total_events_detected": self._total_events_detected,
                    "total_alerts_sent": self._total_alerts_sent,
                    "active_watchers": len(self._active_watchers),
                },
                "last_updated": datetime.now().isoformat(),
            }

            # Write to file
            state_file.write_text(json.dumps(state, indent=2))

        except Exception as e:
            logger.error(f"Error syncing state: {e}")

    async def _handle_visual_message(self, message: AgentMessage) -> None:
        """Handle incoming visual monitoring messages."""
        payload = message.payload

        if payload.get("type") != "visual_request":
            return

        action = payload.get("action")
        if not action:
            return

        logger.info(f"Received visual request from {message.from_agent}: {action}")

        try:
            result = await self.execute_task(payload)

            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "visual_response",
                        "action": action,
                        "result": result,
                    },
                    from_agent=self.agent_name,
                )

        except Exception as e:
            logger.exception(f"Error handling visual message: {e}")
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "visual_response",
                        "action": action,
                        "error": str(e),
                    },
                    from_agent=self.agent_name,
                )

    # =========================================================================
    # Convenience methods
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        return {
            # Core stats
            "total_watches_started": self._total_watches_started,
            "total_events_detected": self._total_events_detected,
            "total_alerts_sent": self._total_alerts_sent,

            # v11.0: Action execution stats
            "total_actions_executed": self._total_actions_executed,
            "total_actions_succeeded": self._total_actions_succeeded,
            "total_actions_failed": self._total_actions_failed,

            # Watcher stats
            "active_watchers": len(self._active_watchers),
            "active_ferrari_watchers": len(self._active_video_watchers),  # v12.0
            "max_parallel": self.config.max_parallel_watchers,

            # Capabilities
            "capabilities": list(self.capabilities),
            "version": "12.0",  # Ferrari Engine Edition

            # Component availability
            "ferrari_engine_available": self._fast_capture_engine is not None,  # v12.0
            "watcher_manager_available": self._watcher_manager is not None,  # Legacy
            "detector_available": self._detector is not None,
            "computer_use_available": self._computer_use_connector is not None,

            # v12.0: Ferrari Engine details
            "capture_method": "ferrari_engine" if self._fast_capture_engine else "legacy",
            "gpu_accelerated": self._fast_capture_engine is not None,
        }


# =============================================================================
# Factory function for agent registration
# =============================================================================

async def create_visual_monitor_agent(
    config: Optional[VisualMonitorConfig] = None,
) -> VisualMonitorAgent:
    """
    Create a Visual Monitor Agent.

    This is the factory function used by AgentInitializer.

    Args:
        config: Optional configuration

    Returns:
        Configured VisualMonitorAgent
    """
    return VisualMonitorAgent(config=config)
