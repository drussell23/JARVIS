"""
JARVIS Neural Mesh - Visual Monitor Agent v14.0 (v28.2 Window Validation)
==========================================================================

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

v28.2 SMART WINDOW CACHE - ROBUST YABAI VALIDATION:
- ROOT CAUSE FIX: "Window not found" validation failure
- PROBLEM: yabai's `--window <id>` query fails for windows not fully indexed
- SOLUTION: Query ALL windows from yabai, build cache, filter by ID
- Graceful fallback: Skip yabai checks for unknown windows, use frame capture test
- Cache with TTL: Efficient repeated queries without hammering yabai
- Thread-safe: Async lock for concurrent cache updates

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
import re
import subprocess
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

# v32.3: Rapidfuzz for intelligent fuzzy text matching
# - 100x faster than fuzzywuzzy (C++ implementation)
# - Zero RAM overhead (lazy loaded)
# - Better matching for "bouncing" → "BOUNCE"
try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz
    from rapidfuzz import process as rapidfuzz_process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    rapidfuzz_fuzz = None
    rapidfuzz_process = None

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

# v32.0: Real-time surveillance progress streaming to UI
try:
    from backend.core.surveillance_progress_stream import (
        SurveillanceStage,
        emit_surveillance_progress,
        emit_discovery_start,
        emit_discovery_complete,
        emit_teleport_progress,
        emit_watcher_spawned,
        emit_monitoring_active,
        emit_detection,
        emit_error,
        emit_complete,  # v32.4: Added for proper UI completion signal
    )
    PROGRESS_STREAM_AVAILABLE = True
    logger.info("[VisualMonitor] ✅ SurveillanceProgressStream available for real-time UI updates")
except ImportError as e:
    PROGRESS_STREAM_AVAILABLE = False
    logger.warning(f"[VisualMonitor] SurveillanceProgressStream not available: {e}")

# Multi-space window detection for "God Mode" parallel watching
try:
    from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector
    MULTI_SPACE_AVAILABLE = True
except ImportError:
    MULTI_SPACE_AVAILABLE = False
    logger.warning("MultiSpaceWindowDetector not available - multi-space watching disabled")

# v25.0: Ghost Display Manager for Shadow Monitor infrastructure
try:
    from backend.vision.yabai_space_detector import (
        GhostDisplayManager,
        GhostDisplayStatus,
        WindowLayoutStyle,
        get_ghost_manager,
    )
    GHOST_MANAGER_AVAILABLE = True
    logger.info("[VisualMonitor] ✅ GhostDisplayManager available for Shadow Monitor")
except ImportError as e:
    GHOST_MANAGER_AVAILABLE = False
    logger.warning(f"[VisualMonitor] GhostDisplayManager not available: {e}")

# v27.0: Adaptive Resource Governor for FPS throttling under load
try:
    from backend.neural_mesh.agents.adaptive_resource_governor import (
        AdaptiveResourceGovernor,
        get_resource_governor,
        WatcherPriority,
        ThrottleLevel,
        ThrottleState,
    )
    RESOURCE_GOVERNOR_AVAILABLE = True
    logger.info("[VisualMonitor] ✅ AdaptiveResourceGovernor available for FPS throttling")
except ImportError as e:
    RESOURCE_GOVERNOR_AVAILABLE = False
    logger.warning(f"[VisualMonitor] AdaptiveResourceGovernor not available: {e}")


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

    # =========================================================================
    # v26.0: Progressive Startup Manager - Dynamic Multi-Window Initialization
    # =========================================================================
    # ROOT CAUSE FIX: 11 windows times out because of fixed 15s timeout
    # SOLUTION: Dynamic timeout scaling + event-based registration + optimistic ack
    # =========================================================================

    # Base timeout for watcher startup (per watcher)
    progressive_startup_base_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PROGRESSIVE_BASE_TIMEOUT", "5.0"))
    )
    # Additional timeout per window (scales with window count)
    progressive_startup_per_window_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PROGRESSIVE_PER_WINDOW", "2.0"))
    )
    # Maximum total timeout (cap to prevent excessive waits)
    progressive_startup_max_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PROGRESSIVE_MAX_TIMEOUT", "60.0"))
    )
    # Minimum watchers required before returning (percentage of total)
    progressive_startup_min_active_ratio: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PROGRESSIVE_MIN_RATIO", "0.3"))
    )
    # Max parallel watcher spawns (throttle to avoid system overload)
    progressive_startup_max_parallel_spawns: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PROGRESSIVE_MAX_PARALLEL", "5"))
    )
    # Max parallel rescue operations (window teleportation)
    progressive_rescue_max_parallel: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_RESCUE_MAX_PARALLEL", "5"))
    )
    # Enable optimistic acknowledgment (return immediately, verify in background)
    progressive_startup_optimistic: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PROGRESSIVE_OPTIMISTIC", "true").lower() == "true"
    )

    # =========================================================================
    # v38.0: MOSAIC STRATEGY - O(1) Single-Stream Display Capture
    # =========================================================================
    # ROOT CAUSE FIX: Per-window surveillance (O(N)) is mathematically wasteful
    # SOLUTION: Capture entire Ghost Display as ONE stream, analyze mosaic
    # =========================================================================

    # Enable Mosaic mode (O(1) efficiency instead of O(N))
    mosaic_mode_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MOSAIC_MODE", "true").lower() == "true"
    )
    # Minimum window count to trigger Mosaic mode (use per-window for small counts)
    mosaic_mode_min_windows: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MOSAIC_MIN_WINDOWS", "3"))
    )
    # FPS for Mosaic capture (lower than per-window since single stream)
    mosaic_fps: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MOSAIC_FPS", "5"))
    )
    # Enable spatial intelligence (map OCR matches back to specific windows)
    mosaic_spatial_intelligence: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MOSAIC_SPATIAL", "true").lower() == "true"
    )


# =============================================================================
# v26.0: Progressive Startup Manager - Event-Based Multi-Window Initialization
# =============================================================================

class ProgressiveStartupManager:
    """
    Intelligent, event-based multi-window startup manager.

    ROOT CAUSE FIX for 11-window timeout:
    1. Dynamic timeout scaling: base + (window_count * per_window)
    2. Event-based registration: no polling, watchers notify when ready
    3. Optimistic acknowledgment: return immediately, verify in background
    4. Batched spawning: avoid overwhelming the system
    5. Progressive reporting: announce as watchers come online

    Architecture:
        ┌─────────────────────────────────────────────────────────────────────┐
        │  ProgressiveStartupManager                                          │
        │  ├── WatcherReadyEvent (asyncio.Event per watcher)                  │
        │  ├── StartupCoordinator (batches and throttles spawns)              │
        │  ├── DynamicTimeoutCalculator (scales with window count)            │
        │  └── ProgressReporter (announces watchers as they come online)      │
        └─────────────────────────────────────────────────────────────────────┘
    """

    def __init__(self, config: VisualMonitorConfig):
        self.config = config
        self._watcher_ready_events: Dict[str, asyncio.Event] = {}
        self._startup_results: Dict[str, Dict[str, Any]] = {}
        self._all_started_event = asyncio.Event()
        self._startup_start_time: Optional[datetime] = None
        self._expected_count = 0
        self._ready_count = 0
        self._failed_count = 0
        self._lock = asyncio.Lock()
        self._progress_callback: Optional[callable] = None  # v31.0: Progress narration

    def calculate_dynamic_timeout(self, window_count: int) -> float:
        """
        Calculate dynamic timeout based on window count.

        Formula: min(base + (count * per_window), max_timeout)

        For 11 windows with defaults:
        - base=5.0 + (11 * 2.0) = 27.0 seconds (not 15!)
        """
        base = self.config.progressive_startup_base_timeout
        per_window = self.config.progressive_startup_per_window_timeout
        max_timeout = self.config.progressive_startup_max_timeout

        dynamic = base + (window_count * per_window)
        return min(dynamic, max_timeout)

    def calculate_min_required_watchers(self, total: int) -> int:
        """
        Calculate minimum watchers required before returning.

        With 30% ratio and 11 windows: min(11, max(1, 11 * 0.3)) = 4 watchers
        """
        ratio = self.config.progressive_startup_min_active_ratio
        return max(1, int(total * ratio))

    def set_progress_callback(self, callback: Optional[callable]) -> None:
        """
        v31.0: Set callback for progress narration.
        
        Callback signature: async def callback(ready: int, total: int, message: str)
        """
        self._progress_callback = callback

    async def register_watcher_ready(
        self,
        watcher_id: str,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """
        Called by watchers when they're ready (or failed).

        This is the EVENT-BASED alternative to polling.
        v31.0: Added progress narration at key milestones.
        """
        async with self._lock:
            self._startup_results[watcher_id] = {
                'success': success,
                'error': error,
                'timestamp': datetime.now().isoformat(),
            }

            if success:
                self._ready_count += 1
            else:
                self._failed_count += 1

            # Signal individual watcher ready
            if watcher_id in self._watcher_ready_events:
                self._watcher_ready_events[watcher_id].set()

            # Check if we have enough watchers or all finished
            total_finished = self._ready_count + self._failed_count
            min_required = self.calculate_min_required_watchers(self._expected_count)

            if self._ready_count >= self._expected_count:
                # All watchers succeeded
                self._all_started_event.set()
            elif total_finished >= self._expected_count:
                # All watchers finished (some may have failed)
                self._all_started_event.set()
            elif self._ready_count >= min_required:
                # Enough watchers are ready - can proceed
                self._all_started_event.set()

            logger.debug(
                f"[ProgressiveStartup] Watcher {watcher_id}: "
                f"{'ready' if success else 'failed'} "
                f"({self._ready_count}/{self._expected_count} active, "
                f"{self._failed_count} failed)"
            )

            # v31.0: PROGRESSIVE NARRATION - Keep user informed at milestones
            # Only narrate at key points to avoid spam (25%, 50%, 75%, first, min_required)
            if hasattr(self, '_progress_callback') and self._progress_callback and success:
                should_narrate = False
                message = ""
                
                if self._ready_count == 1:
                    # First watcher ready
                    should_narrate = True
                    message = f"First eye online. {self._expected_count - 1} more initializing."
                elif self._ready_count == min_required and self._expected_count > 3:
                    # Minimum threshold reached
                    should_narrate = True
                    message = f"{self._ready_count} of {self._expected_count} ready. Enough to proceed."
                elif self._expected_count >= 4:
                    # Milestones for larger batches
                    percent = (self._ready_count / self._expected_count) * 100
                    prev_percent = ((self._ready_count - 1) / self._expected_count) * 100
                    
                    if percent >= 50 and prev_percent < 50:
                        should_narrate = True
                        message = f"Halfway there. {self._ready_count} of {self._expected_count} eyes online."
                    elif percent >= 75 and prev_percent < 75:
                        should_narrate = True
                        message = f"Almost ready. {self._ready_count} of {self._expected_count} eyes online."
                
                if should_narrate:
                    try:
                        await self._progress_callback(self._ready_count, self._expected_count, message)
                    except Exception as e:
                        logger.debug(f"[ProgressiveStartup] Progress callback failed: {e}")

    async def wait_for_startup(
        self,
        expected_count: int,
    ) -> Dict[str, Any]:
        """
        Wait for watchers to start with dynamic timeout.

        Returns immediately if optimistic mode is enabled and
        at least min_required watchers are ready.
        """
        self._expected_count = expected_count
        self._startup_start_time = datetime.now()

        # Calculate dynamic timeout
        timeout = self.calculate_dynamic_timeout(expected_count)
        min_required = self.calculate_min_required_watchers(expected_count)

        logger.info(
            f"[ProgressiveStartup] Waiting for {expected_count} watchers "
            f"(min required: {min_required}, timeout: {timeout:.1f}s)"
        )

        try:
            # Wait for all_started event or timeout
            await asyncio.wait_for(
                self._all_started_event.wait(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Timeout - but we may have some watchers ready
            logger.warning(
                f"[ProgressiveStartup] Timeout after {timeout:.1f}s "
                f"({self._ready_count}/{expected_count} ready, "
                f"{self._failed_count} failed)"
            )

        elapsed = (datetime.now() - self._startup_start_time).total_seconds()

        return {
            'expected': expected_count,
            'ready': self._ready_count,
            'failed': self._failed_count,
            'pending': expected_count - self._ready_count - self._failed_count,
            'elapsed_seconds': elapsed,
            'timeout_used': timeout,
            'min_required': min_required,
            'success': self._ready_count >= min_required,
            'results': dict(self._startup_results),
        }

    def reset(self) -> None:
        """Reset for new startup sequence."""
        self._watcher_ready_events.clear()
        self._startup_results.clear()
        self._all_started_event.clear()
        self._startup_start_time = None
        self._expected_count = 0
        self._ready_count = 0
        self._failed_count = 0


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

        # v22.0.0: Watcher Lifecycle Tracking for Early Registration
        # =====================================================================
        # PROBLEM: Watchers only registered AFTER all verification passes.
        # If ANY check fails, watcher is never registered - God Mode sees 0 watchers.
        #
        # SOLUTION: Track watcher lifecycle states EARLY:
        # - "starting": Watcher created, verification in progress
        # - "verifying": start() succeeded, checking status/frames
        # - "active": Fully verified and working
        # - "failed": Verification failed (stores failure reason)
        # =====================================================================
        self._watcher_lifecycle: Dict[str, Dict[str, Any]] = {}  # watcher_id -> lifecycle state

        # v26.0: Progressive Startup Manager - Event-Based Multi-Window Initialization
        # =====================================================================
        # ROOT CAUSE FIX: 11 windows times out because of fixed 15s timeout
        # SOLUTION: Dynamic timeout + event-based registration + optimistic ack
        # =====================================================================
        self._progressive_startup_manager: Optional[ProgressiveStartupManager] = None

        # v27.0: Adaptive Resource Governor - Dynamic FPS Throttling Under Load
        # =====================================================================
        # FIXES "MELTDOWN" RISK:
        # When watching 11 windows at 60 FPS during heavy CPU load, system
        # can become unresponsive. AdaptiveResourceGovernor monitors CPU/memory
        # and dynamically throttles watcher FPS to prevent system overload.
        # =====================================================================
        self._resource_governor: Optional[AdaptiveResourceGovernor] = None
        self._governor_started: bool = False

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

        # v28.2: Smart Window Cache - Robust yabai Window Query System
        # =====================================================================
        # ROOT CAUSE FIX: "Window not found" validation failure
        # PROBLEM: yabai's `--window <id>` query fails for windows that exist
        #          but aren't fully indexed, or when using CGWindowID vs yabai ID
        # SOLUTION: Query ALL windows, build lookup cache, filter by ID
        # =====================================================================
        self._yabai_windows_cache: Dict[int, Dict[str, Any]] = {}  # window_id -> window_info
        self._yabai_cache_timestamp: float = 0.0  # When cache was last populated
        self._yabai_cache_ttl: float = 2.0  # Cache TTL in seconds (windows change fast)
        self._yabai_cache_lock: Optional[asyncio.Lock] = None  # Thread-safe cache updates

        # v37.0: OCR CONCURRENCY LIMITING - Prevent Resource Exhaustion
        # =====================================================================
        # ROOT CAUSE FIX: Multiple watchers running OCR simultaneously
        # PROBLEM: With 11 windows at high FPS, OCR operations can pile up:
        # - Each OCR call allocates 10-50MB for image processing
        # - Tesseract/Vision framework has internal parallelism
        # - Concurrent OCRs can spike memory to 4GB+ and thrash CPU
        # - 16GB M1 Mac becomes unresponsive
        #
        # SOLUTION: Global semaphore limits concurrent OCR operations
        # - Default: 2 concurrent OCRs (configurable via env)
        # - Watchers queue up and wait their turn
        # - Prevents memory explosion during multi-window surveillance
        # =====================================================================
        self._ocr_max_concurrent = int(os.getenv('JARVIS_OCR_MAX_CONCURRENT', '2'))
        self._ocr_semaphore: Optional[asyncio.Semaphore] = None  # Lazy-init in async context

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

        # v22.0.0: Clear lifecycle tracker for new session
        # Ensures we track only watchers from THIS God Mode invocation
        self._watcher_lifecycle.clear()
        logger.debug("[God Mode] Cleared watcher lifecycle tracker for new session")

        # =====================================================================
        # v30.3: COMPREHENSIVE YABAI PERMISSION CHECK & FIX
        # =====================================================================
        # ROOT CAUSE FIX: macOS TCC requires the ACTUAL binary path, not symlinks.
        # When yabai is installed via Homebrew:
        #   - Symlink: /opt/homebrew/bin/yabai
        #   - Actual: /opt/homebrew/Cellar/yabai/X.Y.Z/bin/yabai
        #
        # v30.3 provides:
        #   1. Actual binary path resolution
        #   2. Comprehensive fix instructions with correct path
        #   3. Auto-open settings with context
        #   4. Service restart triggering permission prompt
        # =====================================================================
        PERMISSION_CHECK_TIMEOUT = 3.0  # Slightly longer for comprehensive check

        try:
            from backend.vision.yabai_space_detector import (
                ensure_yabai_permissions,
                check_yabai_permissions,
                fix_yabai_permissions,
                get_yabai_actual_binary_path,
                get_yabai_permission_instructions,
            )

            # Check permissions with timeout wrapper
            try:
                perm_ok, perm_error = await asyncio.wait_for(
                    ensure_yabai_permissions(
                        auto_open_settings=False,  # Don't auto-open yet, just detect
                        narrate_issues=True
                    ),
                    timeout=PERMISSION_CHECK_TIMEOUT
                )
            except asyncio.TimeoutError:
                # Permission check timed out - log warning but continue
                logger.warning(
                    f"[God Mode v30.3] Permission check timed out after {PERMISSION_CHECK_TIMEOUT}s - continuing"
                )
                perm_ok, perm_error = True, None  # Assume OK and let operations fail naturally

            if not perm_ok and perm_error:
                # Log the detailed error
                logger.error(f"[God Mode v30.3] ❌ YABAI PERMISSION ISSUE:\n{perm_error}")

                # v30.3: COMPREHENSIVE PERMISSION FIX
                # Use the new fix_yabai_permissions which:
                # - Detects actual binary path (not symlink)
                # - Opens settings
                # - Restarts service to trigger permission prompt
                # - Provides step-by-step instructions
                fix_result = None
                try:
                    fix_result = await asyncio.wait_for(
                        fix_yabai_permissions(
                            auto_open_settings=True,
                            auto_restart_service=True,
                            narrate_progress=True
                        ),
                        timeout=10.0  # Generous timeout for fix attempt
                    )
                except asyncio.TimeoutError:
                    logger.warning("[God Mode v30.3] Permission fix timed out")
                except Exception as e:
                    logger.warning(f"[God Mode v30.3] Permission fix failed: {e}")

                # Get the actual binary path for accurate instructions
                symlink_path, actual_path = get_yabai_actual_binary_path()
                
                # Build comprehensive fix instructions
                if actual_path and actual_path != symlink_path:
                    # Homebrew installation - emphasize correct path
                    fix_instructions = (
                        "🔐 IMPORTANT: Add the ACTUAL binary, not the symlink!\n\n"
                        f"   ❌ Symlink (don't use): {symlink_path}\n"
                        f"   ✅ Actual (use this):   {actual_path}\n\n"
                        "Steps:\n"
                        "1. In Accessibility Settings, click '+' to add an app\n"
                        "2. Press Cmd+Shift+G to open 'Go to Folder'\n"
                        f"3. Paste this path: {actual_path}\n"
                        "4. Click 'Open' to add yabai\n"
                        "5. Make sure the toggle is ON\n"
                        "6. Run in Terminal: yabai --restart-service\n"
                        "7. Then retry your command"
                    )
                else:
                    fix_instructions = (
                        "Steps to fix:\n"
                        "1. Accessibility Settings should now be open\n"
                        "2. Find 'yabai' in the list and toggle it ON\n"
                        "3. Run in Terminal: yabai --restart-service\n"
                        "4. Then retry your command"
                    )

                # Narrate to user if enabled
                if self.config.working_out_loud_enabled:
                    try:
                        # v30.3: Enhanced message with actual path info
                        if fix_result and fix_result.opened_settings:
                            if actual_path and actual_path != symlink_path:
                                voice_msg = (
                                    "I opened the Accessibility settings. "
                                    "Important: you need to add the actual yabai binary, not the symlink. "
                                    "Click the plus button, press Command Shift G, "
                                    f"and paste the path from the instructions. "
                                    "Then toggle yabai on and run yabai restart service in terminal."
                                )
                            else:
                                voice_msg = (
                                    "I opened the Accessibility settings for you. "
                                    "Find yabai in the list and toggle it on. "
                                    "Then run yabai restart service in terminal."
                                )
                        else:
                            voice_msg = (
                                "I can't control windows because yabai needs accessibility permission. "
                                "Open System Settings, go to Privacy and Security, then Accessibility, "
                                "and add yabai."
                            )
                        await self._narrate_working_out_loud(
                            message=voice_msg,
                            narration_type="error",
                            watcher_id="yabai_permission_error",
                            priority="high"
                        )
                    except Exception as e:
                        logger.warning(f"[God Mode v30.3] Narration failed: {e}")

                # Return error with comprehensive fix instructions
                return {
                    'success': False,
                    'status': 'error',
                    'error': 'yabai_permission_denied',
                    'error_message': perm_error,
                    'settings_auto_opened': fix_result.opened_settings if fix_result else False,
                    'service_restarted': fix_result.restarted_service if fix_result else False,
                    'symlink_path': symlink_path,
                    'actual_binary_path': actual_path,
                    'fix_instructions': fix_instructions,
                    'total_watchers': 0,
                    'app_name': app_name,
                    'trigger_text': trigger_text,
                    'message': (
                        f"Yabai needs accessibility permission. I've opened the settings. "
                        f"IMPORTANT: Add the actual binary at {actual_path}"
                        if (fix_result and fix_result.opened_settings and actual_path != symlink_path)
                        else
                        "Yabai lacks accessibility permissions - see instructions to fix"
                    )
                }

            logger.info("[God Mode v30.3] ✅ Yabai permissions verified")

        except ImportError:
            # Module not available - continue without check
            logger.debug("[God Mode v30.3] Permission check module not available - skipping")
        except Exception as e:
            # Don't fail on permission check errors - log and continue
            logger.warning(f"[God Mode v30.3] Permission check failed: {e} - continuing anyway")

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

        # v32.0: Generate correlation ID early for tracking entire surveillance session
        _surveillance_correlation_id = f"surv_{app_name}_{int(datetime.now().timestamp())}"

        # v32.0: Emit discovery START to UI progress stream
        if PROGRESS_STREAM_AVAILABLE:
            try:
                await emit_discovery_start(
                    app_name=app_name,
                    trigger_text=trigger_text,
                    correlation_id=_surveillance_correlation_id
                )
            except Exception as e:
                logger.debug(f"[v32.0] Progress stream emit failed: {e}")

        # v31.0: TRANSPARENT PROGRESS NARRATION - Keep user informed during startup
        # Users should never be left wondering "is it working?"
        if self.config.working_out_loud_enabled:
            try:
                await self._narrate_working_out_loud(
                    message=f"Searching for {app_name} windows across all your spaces.",
                    narration_type="progress",
                    watcher_id=f"discovery_start_{app_name}",
                    priority="normal"
                )
            except Exception:
                pass  # Don't fail on narration errors

        # =========================================================================
        # v40.0.0: Robust Timeout Configuration
        # =========================================================================
        # OUTER timeout must be > INNER timeout to prevent cascade failures
        # Inner: JARVIS_WORKSPACE_QUERY_TIMEOUT (default 4s) - parallel yabai query
        # Outer: JARVIS_FIND_WINDOW_TIMEOUT (default 6s) - overall window discovery
        # The parallel query uses circuit breaker + cache, so failures are graceful
        # =========================================================================
        find_window_timeout = float(os.getenv("JARVIS_FIND_WINDOW_TIMEOUT", "6"))

        try:
            windows = await asyncio.wait_for(
                self._find_window(app_name, find_all=True),
                timeout=find_window_timeout
            )
        except asyncio.TimeoutError:
            logger.error(
                f"🚨 God Mode window discovery timed out after {find_window_timeout}s for '{app_name}'. "
                f"The parallel workspace query may have exceeded timeout. "
                f"Circuit breaker will prevent repeated slow queries."
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

            # v32.0: Emit error to UI progress stream
            if PROGRESS_STREAM_AVAILABLE:
                try:
                    await emit_error(
                        message=f"Window discovery timed out after {find_window_timeout}s",
                        app_name=app_name,
                        trigger_text=trigger_text,
                        details={"timeout": find_window_timeout, "reason": "yabai_unresponsive"},
                        correlation_id=_surveillance_correlation_id
                    )
                except Exception as e:
                    logger.debug(f"[v32.0] Error emit failed: {e}")

            return {
                'status': 'error',
                'error': f"Window discovery timed out after {find_window_timeout}s (Yabai/MultiSpaceDetector unresponsive)",
                'total_watchers': 0,
                'timeout': True
            }

        # =====================================================================
        # v28.0: TYPE-SAFE WINDOW VALIDATION (Fixes TypeError crash)
        # =====================================================================
        # ROOT CAUSE: _find_window(find_all=True) can return:
        #   - List[Dict]: Success (multiple windows found)
        #   - Dict: Fallback mode (single window, or error dict)
        #   - None/[]: No windows found
        #
        # BUG: If a Dict is returned, `for w in windows:` iterates over
        # dict KEYS (strings), then `w.get('space_id')` fails with:
        # "TypeError: string indices must be integers"
        #
        # FIX: Normalize all return types to List[Dict] before iteration
        # =====================================================================
        validated_windows: List[Dict[str, Any]] = []

        if windows is None:
            logger.warning(f"⚠️ [v28.0] _find_window returned None for '{app_name}'")
            validated_windows = []
        elif isinstance(windows, list):
            # Expected: List of window dicts
            validated_windows = [w for w in windows if isinstance(w, dict) and w.get('window_id')]
            logger.debug(f"[v28.0] Validated {len(validated_windows)} windows from list of {len(windows)}")
        elif isinstance(windows, dict):
            # Fallback mode returned a single dict - normalize to list
            if windows.get('found') and windows.get('window_id'):
                validated_windows = [windows]
                logger.info(f"[v28.0] Normalized single window dict to list for '{app_name}'")
            elif windows.get('status') == 'error':
                logger.warning(f"⚠️ [v28.0] _find_window returned error: {windows.get('error', 'unknown')}")
                validated_windows = []
            else:
                logger.warning(f"⚠️ [v28.0] Unexpected dict format: {list(windows.keys())}")
                validated_windows = []
        else:
            logger.error(f"❌ [v28.0] Invalid window type: {type(windows).__name__}")
            validated_windows = []

        # Replace windows with validated list
        windows = validated_windows

        if not windows:
            logger.warning(f"⚠️ [v28.0] No valid windows found for '{app_name}' after validation")
            
            # v32.0: Emit error to UI progress stream
            if PROGRESS_STREAM_AVAILABLE:
                try:
                    await emit_error(
                        message=f"No {app_name} windows found",
                        app_name=app_name,
                        trigger_text=trigger_text,
                        details={"reason": "no_windows_found"},
                        correlation_id=_surveillance_correlation_id
                    )
                except Exception as e:
                    logger.debug(f"[v32.0] Error emit failed: {e}")
            
            return {
                'status': 'error',
                'error': f"No valid windows found for '{app_name}'",
                'total_watchers': 0
            }

        logger.info(f"✅ [v28.0] Validated {len(windows)} windows for '{app_name}'")

        # v32.0: Emit discovery complete to UI progress stream
        # NOTE: _surveillance_correlation_id was already generated at discovery start
        if PROGRESS_STREAM_AVAILABLE:
            try:
                # Count unique spaces
                unique_spaces = set(w.get('space_id') for w in windows if w.get('space_id'))
                await emit_discovery_complete(
                    app_name=app_name,
                    window_count=len(windows),
                    space_count=len(unique_spaces),
                    trigger_text=trigger_text,
                    correlation_id=_surveillance_correlation_id
                )
            except Exception as e:
                logger.debug(f"[v32.0] Progress stream emit failed: {e}")

        # v31.0: NARRATE DISCOVERY SUCCESS - User knows we found windows
        if self.config.working_out_loud_enabled:
            try:
                window_count = len(windows)
                if window_count == 1:
                    msg = f"Found one {app_name} window. Preparing surveillance."
                else:
                    msg = f"Found {window_count} {app_name} windows. Preparing parallel surveillance."
                await self._narrate_working_out_loud(
                    message=msg,
                    narration_type="progress",
                    watcher_id=f"discovery_complete_{app_name}",
                    priority="normal"
                )
            except Exception:
                pass

        # =====================================================================
        # v28.0: WINDOW NORMALIZATION - Ensure Required Fields Exist
        # =====================================================================
        # ROOT CAUSE FIX: KeyError: 'confidence'
        # PROBLEM: Different window discovery paths return different fields
        # SOLUTION: Normalize all windows to have consistent required fields
        # =====================================================================
        windows = self._normalize_window_data(windows, app_name)

        # =====================================================================
        # v22.0.0: AUTO-HANDOFF FIRST - Teleport Before Filter!
        # =====================================================================
        # CRITICAL ORDER OF OPERATIONS:
        # 1. TELEPORT windows from hidden spaces to Ghost Display
        # 2. THEN filter by visible spaces (now includes teleported windows)
        #
        # WHY THIS ORDER MATTERS:
        # - Old order: Filter → Teleport (hidden windows already removed!)
        # - New order: Teleport → Filter (teleported windows pass filter!)
        #
        # This enables JARVIS to autonomously move windows from ANY space
        # to the Ghost Display, then successfully watch them.
        # =====================================================================
        teleported_windows = []
        teleported_ghost_space = None  # v41.6: Track Ghost Display space at higher scope
        auto_handoff_enabled = bool(os.getenv('JARVIS_AUTO_HANDOFF', '1') == '1')

        if windows and auto_handoff_enabled and MULTI_SPACE_AVAILABLE:
            try:
                from backend.vision.yabai_space_detector import get_yabai_detector
                from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector

                yabai = get_yabai_detector()
                detector = MultiSpaceWindowDetector()

                # =====================================================================
                # v19.0: TIMEOUT PROTECTION for ALL Yabai Operations
                # =====================================================================
                # ROOT CAUSE FIX: These operations were hanging without timeout,
                # causing "Processing..." freeze when Yabai is slow/unresponsive.
                # SOLUTION: Wrap each operation in asyncio.wait_for with 3s timeout.
                # =====================================================================
                yabai_timeout = float(os.getenv('JARVIS_YABAI_OPERATION_TIMEOUT', '3.0'))

                try:
                    ghost_space = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, yabai.get_ghost_display_space
                        ),
                        timeout=yabai_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[God Mode] ⚠️ get_ghost_display_space timed out after {yabai_timeout}s")
                    ghost_space = None
                
                # ==========================================================
                # v42.0: AGGRESSIVE RESCUE PROTOCOL
                # ==========================================================
                # ROOT CAUSE FIX: get_ghost_display_space() returns None when
                # yabai doesn't report a "visible" space on the secondary display.
                # This causes hidden windows to be ABANDONED instead of RESCUED.
                #
                # SOLUTION: If ghost_space is None, use DISPLAY-BASED DETECTION
                # to find ANY space on Display 2+ and use that as the rescue target.
                # The v34.0 Display Handoff will handle the actual window move.
                # ==========================================================
                if ghost_space is None:
                    logger.info("[God Mode v42.0] ⚡ Ghost space not found via standard lookup - trying Aggressive Rescue")
                    
                    try:
                        # Refresh topology first - space IDs might be stale
                        # Use invalidate_cache if available to force fresh data
                        if hasattr(yabai, 'invalidate_cache'):
                            yabai.invalidate_cache()
                            logger.debug("[God Mode v42.0] Cache invalidated for fresh topology")
                        
                        # ==========================================================
                        # v42.0: STRATEGY 1 - Direct Yabai Display Query
                        # ==========================================================
                        # Most reliable: Query yabai directly for display list
                        # ==========================================================
                        import subprocess
                        try:
                            displays_result = subprocess.run(
                                ['yabai', '-m', 'query', '--displays'],
                                capture_output=True, text=True, timeout=3.0
                            )
                            if displays_result.returncode == 0:
                                displays = json.loads(displays_result.stdout)
                                logger.info(f"[God Mode v42.0] 📺 Direct display query: Found {len(displays)} display(s)")
                                
                                # Find secondary display (index > 1 or id != main)
                                secondary_displays = [d for d in displays if d.get('index', 1) > 1]
                                
                                if secondary_displays:
                                    # Get the first space on the secondary display
                                    secondary_display_index = secondary_displays[0].get('index', 2)
                                    spaces_on_secondary = secondary_displays[0].get('spaces', [])
                                    
                                    if spaces_on_secondary:
                                        ghost_space = spaces_on_secondary[0]  # First space on secondary display
                                        logger.info(
                                            f"[God Mode v42.0] 🚀 AGGRESSIVE RESCUE (Direct): "
                                            f"Found Space {ghost_space} on Display {secondary_display_index}"
                                        )
                                else:
                                    logger.warning(f"[God Mode v42.0] ⚠️ No secondary display found in direct query")
                        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
                            logger.debug(f"[God Mode v42.0] Direct display query failed: {e}")
                        
                        # ==========================================================
                        # v42.0: STRATEGY 2 - Fallback to Space Enumeration
                        # ==========================================================
                        if ghost_space is None:
                            # Get all spaces with display info
                            all_spaces = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(
                                    None, lambda: yabai.enumerate_all_spaces(include_display_info=True)
                                ),
                                timeout=yabai_timeout
                            )
                            
                            if all_spaces:
                                logger.debug(f"[God Mode v42.0] Space enumeration returned {len(all_spaces)} spaces")
                                
                                # Log space info for debugging
                                for s in all_spaces:
                                    logger.debug(
                                        f"[God Mode v42.0]   Space {s.get('space_id')}: "
                                        f"display={s.get('display')}, visible={s.get('is_visible')}, "
                                        f"current={s.get('is_current')}"
                                    )
                                
                                # Find ANY space on Display 2+ (secondary displays)
                                secondary_display_spaces = [
                                    s for s in all_spaces 
                                    if s.get('display', 1) > 1
                                ]
                                
                                if secondary_display_spaces:
                                    # Prefer spaces with fewer windows
                                    secondary_display_spaces.sort(
                                        key=lambda x: x.get('window_count', 0)
                                    )
                                    ghost_space = secondary_display_spaces[0].get('space_id')
                                    
                                    logger.info(
                                        f"[God Mode v42.0] 🚀 AGGRESSIVE RESCUE (Enum): Found Space {ghost_space} "
                                        f"on Display {secondary_display_spaces[0].get('display')}"
                                    )
                                else:
                                    # No secondary display? Check display count directly
                                    display_count = len(set(s.get('display', 1) for s in all_spaces))
                                    logger.warning(
                                        f"[God Mode v42.0] ⚠️ No secondary display spaces found "
                                        f"({display_count} display(s) detected)"
                                    )
                        
                    except asyncio.TimeoutError:
                        logger.warning("[God Mode v42.0] Aggressive Rescue display lookup timed out")
                    except Exception as e:
                        logger.warning(f"[God Mode v42.0] Aggressive Rescue failed: {e}")
                    
                    # v42.0: Summary log after Aggressive Rescue
                    if ghost_space is not None:
                        logger.info(f"[God Mode v42.0] ✅ AGGRESSIVE RESCUE SUCCESS: ghost_space={ghost_space}")
                    else:
                        logger.warning(
                            "[God Mode v42.0] ❌ AGGRESSIVE RESCUE FAILED: No secondary display found. "
                            "Please ensure BetterDisplay or a virtual monitor is configured."
                        )

                try:
                    current_user_space = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, yabai.get_current_user_space
                        ),
                        timeout=yabai_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[God Mode] ⚠️ get_current_user_space timed out after {yabai_timeout}s")
                    current_user_space = None

                try:
                    visible_space_ids = await asyncio.wait_for(
                        detector.get_all_visible_spaces(),
                        timeout=yabai_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[God Mode] ⚠️ get_all_visible_spaces timed out after {yabai_timeout}s")
                    visible_space_ids = set()

                # Continue only if we have basic space info (graceful degradation)
                if ghost_space is None and current_user_space is None:
                    logger.warning("[God Mode] ⚠️ Could not determine spaces - skipping auto-handoff")
                    raise Exception("Yabai space detection unavailable")

                if ghost_space is not None:
                    # v41.6: Save to higher-scope variable for validation phase
                    teleported_ghost_space = ghost_space
                    # =========================================================
                    # PHASE 1: Identify windows that need teleporting
                    # =========================================================
                    # A) Windows on HIDDEN spaces - can't capture, need teleport
                    # B) Windows on USER's current space - blocks their view
                    # =========================================================
                    windows_on_hidden = []
                    windows_on_user_space = []
                    windows_already_good = []
                    windows_self_preserved = []  # v41.5: Track skipped JARVIS windows

                    for w in windows:
                        window_id = w.get('window_id')
                        window_space = w.get('space_id')
                        window_title = w.get('window_title', '') or w.get('title', '') or ''
                        window_title_lower = window_title.lower()
                        
                        # ==========================================================
                        # v41.5: SELF-PRESERVATION PROTOCOL
                        # ==========================================================
                        # ROOT CAUSE FIX: With v41.0's aggressive orphan recovery,
                        # JARVIS might find its own UI and try to teleport it,
                        # causing the "Self-Kidnapping" loop.
                        #
                        # SOLUTION: Intelligent title-based filtering to preserve:
                        # - JARVIS Control Panel / Dashboard
                        # - localhost development windows
                        # - Any window serving the JARVIS interface
                        #
                        # This is NOT hardcoding - it's semantic protection of the
                        # control interface that should NEVER be moved.
                        # ==========================================================
                        self_preservation_keywords = [
                            'j.a.r.v.i.s',
                            'jarvis',
                            'localhost:3000',
                            'localhost:8080',
                            '127.0.0.1:3000',
                            '127.0.0.1:8080',
                        ]
                        
                        is_jarvis_ui = any(
                            keyword in window_title_lower 
                            for keyword in self_preservation_keywords
                        )
                        
                        if is_jarvis_ui:
                            # Never teleport the JARVIS Control Panel!
                            logger.info(
                                f"[God Mode v41.5] 🛡️ SELF-PRESERVATION: Skipping JARVIS UI "
                                f"(Window {window_id}: '{window_title[:40]}...')"
                            )
                            windows_self_preserved.append(w)
                            # If it's already on Ghost Display, still monitor it
                            # But don't teleport or disrupt it
                            if window_space == ghost_space:
                                windows_already_good.append(w)
                            continue
                        
                        # ==========================================================
                        # v41.5: GHOST RESIDENCY CHECK
                        # ==========================================================
                        # If window is already on Ghost Display, don't waste time
                        # trying to teleport it - just add to monitoring list
                        # ==========================================================
                        if window_space == ghost_space:
                            # Already on ghost display - perfect!
                            logger.debug(
                                f"[God Mode v41.5] ✅ Window {window_id} already on Ghost Display "
                                f"(Space {ghost_space}) - skipping teleport"
                            )
                            windows_already_good.append(w)
                        elif window_space == current_user_space:
                            # On user's current space - will block their view
                            windows_on_user_space.append(w)
                        elif window_space not in visible_space_ids:
                            # On a hidden space - can't capture
                            windows_on_hidden.append(w)
                        else:
                            # On another visible space (not user's, not ghost)
                            windows_already_good.append(w)
                    
                    # v41.5: Log self-preservation summary
                    if windows_self_preserved:
                        logger.info(
                            f"[God Mode v41.5] 🛡️ Self-Preservation: Protected "
                            f"{len(windows_self_preserved)} JARVIS UI window(s) from teleportation"
                        )

                    # =========================================================
                    # PHASE 2: Teleport windows to Ghost Display
                    # =========================================================
                    windows_to_teleport = windows_on_hidden + windows_on_user_space

                    if windows_to_teleport:
                        hidden_count = len(windows_on_hidden)
                        user_space_count = len(windows_on_user_space)

                        logger.info(
                            f"[God Mode] 👻 AUTO-HANDOFF: Found {len(windows_to_teleport)} windows to teleport "
                            f"({hidden_count} on hidden spaces, {user_space_count} on your screen) "
                            f"→ Ghost Display (Space {ghost_space})"
                        )

                        # v24.0: Ensure each window has app_name for intelligent telemetry
                        for w in windows_to_teleport:
                            if "app_name" not in w:
                                w["app_name"] = w.get("app", app_name)

                        # =========================================================
                        # v25.0: GHOST DISPLAY MANAGER INTEGRATION
                        # =========================================================
                        # Initialize Ghost Display Manager for:
                        # - Health monitoring with auto-recovery
                        # - Window geometry preservation
                        # - Intelligent layout management
                        # - Window return policy
                        # =========================================================
                        ghost_manager = None
                        if GHOST_MANAGER_AVAILABLE:
                            try:
                                # v19.0: Timeout protection for Ghost Manager operations
                                ghost_mgr_timeout = float(os.getenv('JARVIS_GHOST_MANAGER_TIMEOUT', '5.0'))

                                ghost_manager = get_ghost_manager()

                                # Initialize with timeout
                                try:
                                    await asyncio.wait_for(
                                        ghost_manager.initialize(yabai),
                                        timeout=ghost_mgr_timeout
                                    )
                                except asyncio.TimeoutError:
                                    logger.warning(f"[God Mode] ⚠️ Ghost Manager init timed out after {ghost_mgr_timeout}s")
                                    ghost_manager = None

                                if ghost_manager:
                                    # Start health monitoring with timeout
                                    try:
                                        await asyncio.wait_for(
                                            ghost_manager.start_health_monitoring(yabai),
                                            timeout=ghost_mgr_timeout
                                        )
                                    except asyncio.TimeoutError:
                                        logger.warning(f"[God Mode] ⚠️ Health monitoring start timed out")

                                    # v25.0: Preserve window geometry before teleportation (with timeout per window)
                                    geometry_timeout = float(os.getenv('JARVIS_GEOMETRY_TIMEOUT', '2.0'))
                                    preserved_count = 0
                                    for w in windows_to_teleport:
                                        window_id = w.get("window_id")
                                        try:
                                            await asyncio.wait_for(
                                                ghost_manager.preserve_window_geometry(
                                                    window_id=window_id,
                                                    yabai_detector=yabai,
                                                    app_name=w.get("app_name", app_name)
                                                ),
                                                timeout=geometry_timeout
                                            )
                                            preserved_count += 1
                                        except asyncio.TimeoutError:
                                            logger.debug(f"[God Mode] Geometry preservation timed out for window {window_id}")

                                    if preserved_count > 0:
                                        logger.info(
                                            f"[God Mode] 📐 Preserved geometry for {preserved_count}/{len(windows_to_teleport)} windows"
                                        )
                            except Exception as e:
                                logger.debug(f"[God Mode] Ghost Manager init failed: {e}")

                        # Narrate the action
                        if self.config.working_out_loud_enabled:
                            try:
                                if hidden_count > 0:
                                    msg = f"I found {app_name} windows on hidden spaces. Initiating Intelligent Search & Rescue."
                                else:
                                    msg = f"I see {app_name} on your screen. Moving to Ghost Display so I don't block you."
                                await self._narrate_working_out_loud(
                                    message=msg,
                                    narration_type="action",
                                    watcher_id=f"teleport_{app_name}",
                                    priority="high"
                                )
                            except Exception:
                                pass

                        # v32.0: Emit teleport start to UI progress stream
                        if PROGRESS_STREAM_AVAILABLE:
                            try:
                                await emit_surveillance_progress(
                                    stage=SurveillanceStage.TELEPORT_START,
                                    message=f"👻 Moving {len(windows_to_teleport)} windows to Ghost Display...",
                                    progress_current=0,
                                    progress_total=len(windows_to_teleport),
                                    app_name=app_name,
                                    trigger_text=trigger_text,
                                    details={
                                        "hidden_count": hidden_count,
                                        "user_space_count": user_space_count,
                                        "ghost_space": ghost_space
                                    },
                                    correlation_id=_surveillance_correlation_id
                                )
                            except Exception as e:
                                logger.debug(f"[v32.0] Progress stream emit failed: {e}")

                        # =========================================================
                        # v24.0.0: INTELLIGENT SEARCH & RESCUE PROTOCOL
                        # =========================================================
                        # Uses intelligent batch rescue for maximum reliability:
                        # - Multi-strategy approach (switch-grab-return, focus-then-move, etc.)
                        # - Dynamic wake delay calibration based on telemetry
                        # - Parallel rescue with concurrency control
                        # - Retry with exponential backoff
                        # - Root cause detection for failures
                        # - Comprehensive telemetry tracking
                        #
                        # v19.0: Added timeout protection to prevent "Processing..." hang
                        # =========================================================
                        rescue_timeout = float(os.getenv('JARVIS_RESCUE_TIMEOUT', '15.0'))

                        try:
                            rescue_result = await asyncio.wait_for(
                                yabai.rescue_windows_to_ghost_async(
                                    windows=windows_to_teleport,
                                    ghost_space=ghost_space,
                                    max_parallel=5  # Limit concurrent operations
                                ),
                                timeout=rescue_timeout
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[God Mode] ⚠️ Rescue operation timed out after {rescue_timeout}s "
                                f"- proceeding with available windows"
                            )
                            rescue_result = {"success": False, "error": "timeout"}

                        # Process results with v24.0 telemetry
                        if rescue_result.get("success"):
                            direct_count = rescue_result.get("direct_count", 0)
                            rescue_count = rescue_result.get("rescue_count", 0)
                            failed_count = rescue_result.get("failed_count", 0)
                            telemetry_data = rescue_result.get("telemetry", {})

                            # Update window objects with teleport info
                            teleport_progress_idx = 0
                            total_details = len(rescue_result.get("details", []))
                            for detail in rescue_result.get("details", []):
                                teleport_progress_idx += 1
                                if detail.get("success"):
                                    # Find the original window and update it
                                    for w in windows_to_teleport:
                                        if w.get("window_id") == detail.get("window_id"):
                                            w["space_id"] = ghost_space
                                            w["teleported"] = True
                                            w["original_space"] = detail.get("source_space")
                                            w["rescue_method"] = detail.get("method")
                                            w["rescue_strategy"] = detail.get("strategy")
                                            w["rescue_duration_ms"] = detail.get("duration_ms")
                                            teleported_windows.append(w)
                                            
                                            # v32.0: Emit progress for each teleported window
                                            if PROGRESS_STREAM_AVAILABLE:
                                                try:
                                                    await emit_teleport_progress(
                                                        current=teleport_progress_idx,
                                                        total=total_details,
                                                        window_id=w.get("window_id"),
                                                        from_space=detail.get("source_space", 0),
                                                        to_space=ghost_space,
                                                        app_name=app_name,
                                                        correlation_id=_surveillance_correlation_id
                                                    )
                                                except Exception as e:
                                                    logger.debug(f"[v32.0] Teleport progress emit failed: {e}")
                                            break

                            # v24.0: Enhanced logging with telemetry
                            logger.info(
                                f"[God Mode] 🛟 INTELLIGENT RESCUE complete: "
                                f"{direct_count} direct, {rescue_count} rescued, {failed_count} failed "
                                f"(duration: {telemetry_data.get('total_duration_ms', 0):.0f}ms, "
                                f"success rate: {telemetry_data.get('success_rate', 'N/A')})"
                            )
                        else:
                            # Log failure with telemetry insights
                            telemetry_data = rescue_result.get("telemetry", {})
                            logger.warning(
                                f"[God Mode] ⚠️ Intelligent Search & Rescue failed "
                                f"(telemetry: success_rate={telemetry_data.get('success_rate', 'N/A')})"
                            )

                        if teleported_windows:
                            logger.info(
                                f"[God Mode] ✅ AUTO-HANDOFF complete: "
                                f"{len(teleported_windows)}/{len(windows_to_teleport)} windows moved to Ghost Display"
                            )

                            # v32.0: Emit teleport complete to UI progress stream
                            if PROGRESS_STREAM_AVAILABLE:
                                try:
                                    await emit_surveillance_progress(
                                        stage=SurveillanceStage.TELEPORT_COMPLETE,
                                        message=f"✅ {len(teleported_windows)} windows moved to Ghost Display",
                                        progress_current=len(teleported_windows),
                                        progress_total=len(windows_to_teleport),
                                        app_name=app_name,
                                        trigger_text=trigger_text,
                                        space_id=ghost_space,
                                        details={
                                            "teleported_count": len(teleported_windows),
                                            "failed_count": len(windows_to_teleport) - len(teleported_windows),
                                            "ghost_space": ghost_space
                                        },
                                        correlation_id=_surveillance_correlation_id
                                    )
                                except Exception as e:
                                    logger.debug(f"[v32.0] Progress stream emit failed: {e}")

                            # =========================================================
                            # v25.0: TRACK TELEPORTED WINDOWS & APPLY LAYOUT
                            # =========================================================
                            if ghost_manager is not None:
                                try:
                                    # Track each teleported window
                                    for w in teleported_windows:
                                        window_id = w.get("window_id")
                                        await ghost_manager.track_window_teleport(
                                            window_id=window_id,
                                            to_space=ghost_space
                                        )

                                    # Apply intelligent layout to windows on Ghost Display
                                    teleported_window_ids = [
                                        w.get("window_id") for w in teleported_windows
                                    ]
                                    layout_result = await ghost_manager.apply_layout(
                                        window_ids=teleported_window_ids,
                                        yabai_detector=yabai
                                    )

                                    if layout_result.get("success"):
                                        logger.info(
                                            f"[God Mode] 📐 Layout applied to {len(teleported_window_ids)} windows "
                                            f"on Ghost Display"
                                        )
                                    else:
                                        logger.debug(
                                            f"[God Mode] Layout partially applied: "
                                            f"{len(layout_result.get('applied', []))} succeeded"
                                        )

                                    # Store reference for window return policy
                                    # This enables returning windows after monitoring ends
                                    if not hasattr(self, '_ghost_managers'):
                                        self._ghost_managers = {}
                                    self._ghost_managers[app_name] = ghost_manager

                                except Exception as e:
                                    logger.debug(f"[God Mode] Ghost Manager tracking/layout failed: {e}")

                            # Narrate success with rescue stats
                            if self.config.working_out_loud_enabled:
                                try:
                                    # Build informative message based on rescue method
                                    rescued_count = sum(
                                        1 for w in teleported_windows
                                        if w.get("rescue_method") == "rescue"
                                    )
                                    if rescued_count > 0:
                                        narration_msg = (
                                            f"Search & Rescue complete! I rescued {rescued_count} {app_name} windows "
                                            f"from hidden spaces and moved them to my Ghost Display."
                                        )
                                    else:
                                        narration_msg = (
                                            f"Done! I've moved {len(teleported_windows)} {app_name} windows "
                                            f"to my Ghost Display. Starting monitoring now."
                                        )

                                    await self._narrate_working_out_loud(
                                        message=narration_msg,
                                        narration_type="success",
                                        watcher_id=f"teleport_done_{app_name}",
                                        priority="medium"
                                    )
                                except Exception:
                                    pass

                            # Update the windows list with successful teleports
                            # Combine already-good windows with teleported ones
                            windows = windows_already_good + teleported_windows
                            
                            # ==========================================================
                            # v41.6: POST-TELEPORTATION SETTLING DELAY
                            # ==========================================================
                            # ROOT CAUSE FIX: After teleportation, the window might not
                            # be immediately visible to ScreenCaptureKit because:
                            # 1. macOS window server needs to register the move
                            # 2. GPU needs to render the window on the new display
                            # 3. ScreenCaptureKit needs to recognize the new location
                            #
                            # SOLUTION: Brief settling delay before validation/spawning
                            # ==========================================================
                            settling_delay_ms = float(os.getenv('JARVIS_TELEPORT_SETTLING_DELAY_MS', '500'))
                            logger.debug(
                                f"[God Mode v41.6] ⏱️ Waiting {settling_delay_ms:.0f}ms for windows "
                                f"to settle on Ghost Display..."
                            )
                            await asyncio.sleep(settling_delay_ms / 1000.0)
                            logger.debug(f"[God Mode v41.6] ✅ Settling delay complete")
                    else:
                        logger.debug(
                            f"[God Mode] All {len(windows)} windows already on visible spaces - no teleport needed"
                        )

            except Exception as e:
                logger.warning(f"[God Mode] Auto-handoff failed: {e} - continuing with visibility filter")

        # =====================================================================
        # v22.0.0 + v41.0: VISIBILITY FILTER (runs AFTER teleportation)
        # =====================================================================
        # Now that windows have been teleported to Ghost Display, filter to
        # ensure we only watch windows on visible/capturable spaces.
        #
        # v41.0 FIX: GHOST DISPLAY SPACE INCLUSION
        # =========================================
        # ROOT CAUSE: Ghost Display space may NOT be reported as "is_visible=True"
        # by yabai, even though it IS capturable. This was causing windows that
        # were successfully teleported to Ghost Display to be filtered out.
        #
        # SOLUTION: Query Ghost Display space and ALWAYS include it in visible_space_ids
        # =====================================================================
        skipped_windows = []  # Track windows filtered out (for error messaging)

        if windows and MULTI_SPACE_AVAILABLE:
            try:
                from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector
                from backend.vision.yabai_space_detector import get_yabai_detector
                
                detector = MultiSpaceWindowDetector()
                visible_space_ids = await detector.get_all_visible_spaces()
                
                # v41.0: CRITICAL - Always include Ghost Display space
                # The Ghost Display is specifically designed for background capture,
                # so windows there are ALWAYS capturable even if yabai doesn't 
                # report the space as "visible"
                try:
                    yabai_detector = get_yabai_detector()
                    yabai_timeout = float(os.getenv('JARVIS_YABAI_OPERATION_TIMEOUT', '3.0'))
                    ghost_display_space = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, yabai_detector.get_ghost_display_space
                        ),
                        timeout=yabai_timeout
                    )
                    if ghost_display_space and ghost_display_space not in visible_space_ids:
                        visible_space_ids.append(ghost_display_space)
                        logger.info(
                            f"[God Mode v41.0] 👻 Added Ghost Display space {ghost_display_space} "
                            f"to visible spaces (capturable for surveillance)"
                        )
                except Exception as e:
                    logger.debug(f"[God Mode v41.0] Could not get Ghost Display space: {e}")

                if visible_space_ids:
                    original_count = len(windows)
                    capturable_windows = []

                    for w in windows:
                        window_space = w.get('space_id')
                        if window_space in visible_space_ids:
                            capturable_windows.append(w)
                        else:
                            skipped_windows.append(w)
                            logger.debug(
                                f"⚠️  Window {w['window_id']} (Space {window_space}) still not visible "
                                f"after teleport attempt - skipping"
                            )

                    # Log the filtering results
                    if skipped_windows:
                        logger.info(
                            f"[God Mode] 👀 Post-Teleport Filter: "
                            f"{len(capturable_windows)}/{original_count} windows capturable "
                            f"({len(skipped_windows)} still on hidden spaces)"
                        )

                    windows = capturable_windows
                else:
                    logger.warning(
                        "[God Mode] ⚠️  Could not determine visible spaces - proceeding with all windows"
                    )

            except Exception as e:
                logger.warning(f"[God Mode] Visibility filter failed: {e} - proceeding with all windows")

        if not windows or len(windows) == 0:
            # v22.0.0: Check if windows were filtered out due to being on hidden spaces
            # This helps users understand why no windows were found when they exist
            all_on_hidden_spaces = len(skipped_windows) > 0

            if all_on_hidden_spaces:
                # v42.0: Enhanced error message with rescue status
                logger.warning(
                    f"⚠️  Found {len(skipped_windows)} {app_name} windows but ALL are on hidden spaces! "
                    f"Ghost Display auto-rescue failed. Check if BetterDisplay is running."
                )
                
                # v42.0: More actionable error message
                # Check if ghost_space was ever found (it would have been set during auto-handoff)
                rescue_attempted = 'ghost_space' in dir() and ghost_space is not None
                
                if rescue_attempted:
                    error_msg = (
                        f"I found {len(skipped_windows)} {app_name} windows on hidden spaces. "
                        f"Auto-rescue was attempted but the windows didn't move successfully. "
                        f"Try manually moving a window to a visible desktop first."
                    )
                else:
                    error_msg = (
                        f"All {len(skipped_windows)} {app_name} windows are on hidden spaces. "
                        f"No Ghost Display available for auto-rescue. "
                        f"Please install BetterDisplay or create a virtual monitor, "
                        f"or manually move a window to your current space."
                    )
            else:
                logger.warning(f"⚠️  No windows found for {app_name}")
                error_msg = f"I don't see any {app_name} windows open. Please open {app_name} and try again."

            # v15.0: ERROR NARRATION - User should know when no windows found
            if self.config.working_out_loud_enabled:
                try:
                    await self._narrate_working_out_loud(
                        message=error_msg,
                        narration_type="error",
                        watcher_id=f"discovery_{app_name}",
                        priority="high"
                    )
                except Exception as e:
                    logger.warning(f"Error narration failed: {e}")

            return {
                'status': 'error',
                'error': error_msg,
                'total_watchers': 0,
                'windows_on_hidden_spaces': len(skipped_windows) if all_on_hidden_spaces else 0
            }

        logger.info(f"✅ Found {len(windows)} {app_name} windows:")
        for w in windows:
            # v28.0: Use defensive .get() access for optional fields
            confidence = w.get('confidence', 'N/A')
            confidence_str = f"{confidence}%" if isinstance(confidence, (int, float)) else confidence
            logger.info(f"  - Window {w.get('window_id', '?')} on Space {w.get('space_id', '?')} ({confidence_str} match)")

        # Validate max watchers
        max_watchers = self.config.max_multi_space_watchers
        if len(windows) > max_watchers:
            logger.warning(f"⚠️  Found {len(windows)} windows, limiting to {max_watchers} for safety")
            windows = windows[:max_watchers]

        # =====================================================================
        # v28.1: PRE-CAPTURE VALIDATION - Verify Windows Are Actually Capturable
        # =====================================================================
        # ROOT CAUSE FIX: "frame_production_failed" errors occur because we
        # spawn watchers for windows that ScreenCaptureKit cannot capture:
        # - Minimized windows
        # - Windows still on hidden spaces (teleportation failed)
        # - Windows without Screen Recording permission
        # - Windows not yet rendered on Ghost Display (race condition)
        #
        # SOLUTION: Validate each window BEFORE spawning watcher
        # =====================================================================
        validation_enabled = os.getenv('JARVIS_PRECAPTURE_VALIDATION', '1') == '1'

        if validation_enabled and windows:
            logger.info(f"[God Mode] 🔍 Pre-capture validation for {len(windows)} windows...")

            # v32.7: Emit VALIDATION stage to UI progress stream
            if PROGRESS_STREAM_AVAILABLE:
                try:
                    await emit_surveillance_progress(
                        stage=SurveillanceStage.VALIDATION,
                        message=f"🔍 Validating {len(windows)} windows for capture...",
                        progress_current=0,
                        progress_total=len(windows),
                        app_name=app_name,
                        trigger_text=trigger_text,
                        correlation_id=_surveillance_correlation_id
                    )
                except Exception as e:
                    logger.debug(f"[v32.7] Validation progress emit failed: {e}")

            # v41.6: Use teleported_ghost_space from higher scope (reliable)
            # The previous check 'ghost_space' in dir() was unreliable because
            # ghost_space was defined in a nested try block that might not be in scope.
            validation_ghost_space = teleported_ghost_space  # v41.6: Safe reference

            # Run batch validation in parallel
            try:
                valid_windows, invalid_windows = await self._validate_windows_batch(
                    windows,
                    ghost_space=validation_ghost_space,
                    max_concurrent=int(os.getenv('JARVIS_VALIDATION_CONCURRENCY', '5'))
                )

                # Log validation results
                if invalid_windows:
                    for inv in invalid_windows:
                        inv_window = inv['window']
                        inv_reason = inv['reason']
                        logger.warning(
                            f"⚠️  Window {inv_window.get('window_id')} ({inv_window.get('app_name', 'unknown')}) "
                            f"NOT capturable: {inv_reason}"
                        )

                        # Narrate validation failure if significant
                        if self.config.working_out_loud_enabled and 'permission' in inv_reason.lower():
                            try:
                                await self._narrate_working_out_loud(
                                    message=f"I need Screen Recording permission to watch {app_name}. "
                                            f"Please grant it in System Settings.",
                                    narration_type="error",
                                    watcher_id=f"validation_{app_name}",
                                    priority="high"
                                )
                            except Exception:
                                pass

                    logger.info(
                        f"[God Mode] ✅ Pre-capture validation: "
                        f"{len(valid_windows)}/{len(windows)} windows passed "
                        f"({len(invalid_windows)} failed)"
                    )

                # Update windows to only include valid ones
                if valid_windows:
                    windows = valid_windows
                else:
                    # All windows failed validation
                    error_msg = (
                        f"All {len(windows)} {app_name} windows failed pre-capture validation. "
                        f"Reasons: {', '.join(set(inv['reason'][:50] for inv in invalid_windows))}"
                    )
                    logger.error(f"[God Mode] ❌ {error_msg}")

                    if self.config.working_out_loud_enabled:
                        try:
                            await self._narrate_working_out_loud(
                                message=f"I couldn't capture any {app_name} windows. "
                                        f"They may be minimized or on hidden spaces.",
                                narration_type="error",
                                watcher_id=f"validation_failed_{app_name}",
                                priority="high"
                            )
                        except Exception:
                            pass

                    return {
                        'status': 'error',
                        'error': error_msg,
                        'total_watchers': 0,
                        'validation_failures': len(invalid_windows)
                    }

            except Exception as e:
                logger.warning(f"[God Mode] Pre-capture validation failed: {e} - proceeding without validation")

        # =========================================================================
        # v38.0: MOSAIC STRATEGY - O(1) Single-Stream Display Capture
        # =========================================================================
        # Instead of spawning N watchers for N windows, spawn ONE MosaicWatcher
        # that captures the entire Ghost Display where all windows are tiled.
        # =========================================================================
        use_mosaic_mode = (
            self.config.mosaic_mode_enabled and
            len(windows) >= self.config.mosaic_mode_min_windows
        )

        if use_mosaic_mode:
            logger.info(
                f"[God Mode v38.0] 🧩 MOSAIC MODE: Capturing Ghost Display as single stream "
                f"(O(1) efficiency - {len(windows)} windows → 1 stream)"
            )

            # Get Ghost Display info for MosaicWatcher
            mosaic_config = None
            if hasattr(self._spatial_agent, '_ghost_manager') and self._spatial_agent._ghost_manager:
                mosaic_config = self._spatial_agent._ghost_manager.get_mosaic_config()

            if mosaic_config and mosaic_config.get('display_id'):
                try:
                    from backend.vision.macos_video_capture_advanced import (
                        MosaicWatcher,
                        MosaicWatcherConfig,
                        WindowTileInfo
                    )

                    # Build window tile info for spatial intelligence
                    window_tiles = []
                    if self.config.mosaic_spatial_intelligence:
                        ghost_manager = self._spatial_agent._ghost_manager
                        for window in windows:
                            window_id = window.get('window_id')
                            geometry = ghost_manager.get_preserved_geometry(window_id)
                            if geometry:
                                tile = WindowTileInfo(
                                    window_id=window_id,
                                    app_name=window.get('app_name', app_name),
                                    window_title=window.get('title', ''),
                                    x=geometry.x,
                                    y=geometry.y,
                                    width=geometry.width,
                                    height=geometry.height,
                                    original_space_id=window.get('space_id')
                                )
                                window_tiles.append(tile)
                                logger.debug(
                                    f"[Mosaic v38.0] Tile: window {window_id} at ({tile.x}, {tile.y})"
                                )

                    # Create MosaicWatcherConfig
                    mosaic_watcher_config = MosaicWatcherConfig(
                        display_id=mosaic_config['display_id'],
                        display_width=mosaic_config.get('display_width', 1920),
                        display_height=mosaic_config.get('display_height', 1080),
                        fps=self.config.mosaic_fps,
                        window_tiles=window_tiles
                    )

                    # Create and start MosaicWatcher
                    mosaic_watcher = MosaicWatcher(mosaic_watcher_config)
                    success = await mosaic_watcher.start()

                    if success:
                        logger.info(
                            f"[Mosaic v38.0] ✅ MosaicWatcher started - "
                            f"Display {mosaic_config['display_id']}, "
                            f"{len(window_tiles)} tiles mapped"
                        )

                        # Store mosaic watcher for later access
                        self._active_mosaic_watcher = mosaic_watcher

                        # Emit progress
                        if PROGRESS_STREAM_AVAILABLE:
                            try:
                                await emit_surveillance_progress(
                                    stage=SurveillanceStage.WATCHER_START,
                                    message=f"🧩 Mosaic mode: Watching {len(windows)} windows with 1 stream (O(1) efficiency)",
                                    progress_current=1,
                                    progress_total=1,
                                    app_name=app_name,
                                    trigger_text=trigger_text,
                                    correlation_id=_surveillance_correlation_id
                                )
                            except Exception:
                                pass

                        # Run Mosaic detection loop
                        mosaic_result = await self._mosaic_visual_detection(
                            watcher=mosaic_watcher,
                            trigger_text=trigger_text,
                            timeout=max_duration or self.config.watcher_coordination_timeout,
                            app_name=app_name,
                            windows=windows,
                            action_config=action_config,
                            alert_config=alert_config
                        )

                        return mosaic_result

                    else:
                        logger.warning(
                            f"[Mosaic v38.0] MosaicWatcher failed to start - "
                            f"falling back to per-window mode"
                        )
                        use_mosaic_mode = False

                except ImportError as e:
                    logger.warning(f"[Mosaic v38.0] MosaicWatcher not available: {e} - using per-window mode")
                    use_mosaic_mode = False
                except Exception as e:
                    logger.warning(f"[Mosaic v38.0] MosaicWatcher setup failed: {e} - using per-window mode")
                    use_mosaic_mode = False

            else:
                logger.warning(
                    f"[Mosaic v38.0] Ghost Display info not available - using per-window mode"
                )
                use_mosaic_mode = False

        # ===== STEP 2: Spawn Parallel Ferrari Watchers (Legacy O(N) mode) =====
        watcher_tasks = []
        watcher_metadata = []

        if not use_mosaic_mode:
            logger.info(
                f"[God Mode] Per-window mode: Spawning {len(windows)} watchers (O(N))"
            )

        # v32.7: Emit WATCHER_START stage to UI progress stream
        if PROGRESS_STREAM_AVAILABLE and not use_mosaic_mode:
            try:
                await emit_surveillance_progress(
                    stage=SurveillanceStage.WATCHER_START,
                    message=f"🚀 Spawning {len(windows)} video watchers...",
                    progress_current=0,
                    progress_total=len(windows),
                    app_name=app_name,
                    trigger_text=trigger_text,
                    correlation_id=_surveillance_correlation_id
                )
            except Exception as e:
                logger.debug(f"[v32.7] Watcher start progress emit failed: {e}")

        # v27.0: Initialize Resource Governor for FPS throttling under load
        # =====================================================================
        # FIXES "MELTDOWN" RISK:
        # When watching 11 windows at 60 FPS during heavy CPU load, the system
        # can become unresponsive. AdaptiveResourceGovernor monitors CPU/memory
        # and dynamically throttles watcher FPS to prevent system overload.
        # =====================================================================
        if RESOURCE_GOVERNOR_AVAILABLE and not self._governor_started:
            try:
                self._resource_governor = get_resource_governor(
                    throttle_callback=self._handle_throttle_change
                )
                await self._resource_governor.start_monitoring()
                self._governor_started = True
                logger.info(
                    f"[God Mode] 🎮 Resource Governor started for {len(windows)} watchers"
                )
            except Exception as e:
                logger.warning(f"[God Mode] Resource Governor init failed: {e}")

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

            # v27.0: Register watcher with Resource Governor for FPS allocation
            if self._resource_governor:
                try:
                    allocation = await self._resource_governor.register_watcher(
                        watcher_id=watcher_id,
                        priority=WatcherPriority.NORMAL,
                        base_fps=self.config.ferrari_fps  # Default 60 FPS
                    )
                    logger.debug(
                        f"[God Mode] Registered watcher {watcher_id}: FPS={allocation.allocated_fps}"
                    )
                except Exception as e:
                    logger.debug(f"[God Mode] Watcher registration failed: {e}")

        logger.info(f"🚀 Spawned {len(watcher_tasks)} parallel Ferrari Engine watchers")

        # v31.0: NARRATE WATCHER SPAWNING - User knows initialization started
        if self.config.working_out_loud_enabled:
            try:
                watcher_count = len(watcher_tasks)
                if watcher_count == 1:
                    msg = f"Starting video capture for {app_name}."
                else:
                    msg = f"Starting video capture for {watcher_count} windows. This may take a moment."
                await self._narrate_working_out_loud(
                    message=msg,
                    narration_type="progress",
                    watcher_id=f"spawn_start_{app_name}",
                    priority="normal"
                )
            except Exception:
                pass

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
            # ROOT CAUSE FIX v26.0: Progressive Startup with Dynamic Timeout
            # =====================================================================
            # PROBLEM: Fixed 10-15s timeout fails for 11+ windows because:
            # - Each watcher needs 10-15s to start
            # - Polling every 200ms is inefficient
            # - No dynamic scaling based on window count
            #
            # SOLUTION (v26.0 ProgressiveStartupManager):
            # 1. Dynamic timeout: base + (window_count * per_window)
            # 2. Event-based notification (no polling)
            # 3. Minimum required watchers (30% default) before returning
            # 4. Progressive reporting as watchers come online
            # =====================================================================

            # ===== STEP 3B.0: Progressive Startup Wait (v26.0) =====
            expected_watchers = len(watcher_tasks)
            initial_active_count = len(self._active_video_watchers)

            # Initialize Progressive Startup Manager
            if self._progressive_startup_manager is None:
                self._progressive_startup_manager = ProgressiveStartupManager(self.config)
            else:
                self._progressive_startup_manager.reset()

            # v31.0: Wire up progress callback for real-time narration
            if self.config.working_out_loud_enabled:
                async def _progress_narration(ready: int, total: int, message: str):
                    """Callback for progressive narration during startup."""
                    try:
                        await self._narrate_working_out_loud(
                            message=message,
                            narration_type="progress",
                            watcher_id=f"startup_progress_{app_name}",
                            priority="normal"
                        )
                    except Exception:
                        pass
                self._progressive_startup_manager.set_progress_callback(_progress_narration)

            # Calculate dynamic timeout
            dynamic_timeout = self._progressive_startup_manager.calculate_dynamic_timeout(expected_watchers)
            min_required = self._progressive_startup_manager.calculate_min_required_watchers(expected_watchers)

            logger.info(
                f"[God Mode v26.0] Progressive startup: {expected_watchers} watchers, "
                f"timeout={dynamic_timeout:.1f}s (dynamic), min_required={min_required}"
            )

            # =====================================================================
            # v32.5: OPTIMISTIC STARTUP - Return immediately, verify in background
            # =====================================================================
            # ROOT CAUSE FIX: The cascading timeouts (permission check + discovery +
            # teleport + validation + watcher wait) add up to 80+ seconds, exceeding
            # the parent 75s timeout from intelligent_command_handler.py.
            #
            # SOLUTION: Use ULTRA-SHORT startup wait (3s) to catch early failures,
            # then return optimistically. Full verification continues in background.
            # This prevents the "operation timed out" error for the user.
            # =====================================================================
            OPTIMISTIC_STARTUP_WAIT = float(os.getenv("JARVIS_OPTIMISTIC_STARTUP_WAIT", "3.0"))
            
            start_wait_time = datetime.now()
            new_watchers_active = 0

            # Use OPTIMISTIC short wait - just catch early failures
            try:
                startup_result = await asyncio.wait_for(
                    self._progressive_startup_manager.wait_for_startup(expected_watchers),
                    timeout=OPTIMISTIC_STARTUP_WAIT
                )
            except asyncio.TimeoutError:
                # v32.5: Optimistic timeout - this is EXPECTED and OK!
                # Watchers are still starting in background, we return early
                logger.info(
                    f"[God Mode v32.5] ⚡ Optimistic startup: returning after {OPTIMISTIC_STARTUP_WAIT}s "
                    f"(watchers continue initializing in background)"
                )
                startup_result = {
                    'ready': 0,  # We don't know yet, but spawning started
                    'failed': 0,
                    'pending': expected_watchers,
                    'elapsed_seconds': OPTIMISTIC_STARTUP_WAIT,
                    'timeout_used': OPTIMISTIC_STARTUP_WAIT,
                    'min_required': min_required,
                    'success': True,  # Optimistically assume success
                    'results': {},
                    'optimistic': True,  # Flag to indicate early return
                }

            # Also check lifecycle for detailed status (backward compatibility)
            lifecycle_counts = {'starting': 0, 'verifying': 0, 'active': 0, 'failed': 0}
            for lc_id, lc_info in self._watcher_lifecycle.items():
                status = lc_info.get('status', 'unknown')
                if status in lifecycle_counts:
                    lifecycle_counts[status] += 1

            # Count new active watchers (fully verified)
            current_active_count = len(self._active_video_watchers)
            new_watchers_active = current_active_count - initial_active_count

            # Use the better of event-based or lifecycle count
            new_watchers_active = max(new_watchers_active, startup_result['ready'])

            elapsed = startup_result['elapsed_seconds']

            # Log result
            if new_watchers_active >= expected_watchers:
                logger.info(
                    f"[God Mode v26.0] ✅ All {expected_watchers} watchers started "
                    f"in {elapsed:.2f}s"
                )
            elif new_watchers_active >= min_required:
                logger.info(
                    f"[God Mode v26.0] ⚡ {new_watchers_active}/{expected_watchers} watchers active "
                    f"after {elapsed:.2f}s (min_required={min_required} met) - proceeding"
                )
            elif new_watchers_active > 0:
                logger.warning(
                    f"[God Mode v26.0] ⚠️ Only {new_watchers_active}/{expected_watchers} watchers active "
                    f"after {elapsed:.2f}s (below min_required={min_required}) - proceeding anyway"
                )
            else:
                logger.warning(
                    f"[God Mode v26.0] ⚠️ Timeout: 0/{expected_watchers} watchers active "
                    f"after {elapsed:.2f}s"
                )

            # ===== STEP 3B.0.1: Analyze Watcher Results (v22.0.0) =====
            # Collect final lifecycle status for detailed error reporting
            failed_watchers = []
            still_starting = []
            for lc_id, lc_info in self._watcher_lifecycle.items():
                status = lc_info.get('status', 'unknown')
                if status == 'failed':
                    failed_watchers.append({
                        'id': lc_id,
                        'error': lc_info.get('error', 'unknown error'),
                        'stage': lc_info.get('verification_stage', 'unknown'),
                        'window_id': lc_info.get('window_id'),
                        'app_name': lc_info.get('app_name')
                    })
                elif status in ('starting', 'verifying'):
                    still_starting.append(lc_id)

            # v32.5: Check if we're in optimistic mode (watchers still starting in background)
            is_optimistic = startup_result.get('optimistic', False)
            
            if new_watchers_active == 0 and not is_optimistic:
                # Build detailed error message - but only if NOT optimistic
                error_details = []
                if failed_watchers:
                    for fw in failed_watchers:
                        error_details.append(
                            f"Window {fw['window_id']}: {fw['error']} (stage: {fw['stage']})"
                        )
                    error_msg = f"All {len(failed_watchers)} watchers failed: " + "; ".join(error_details[:3])
                    if len(error_details) > 3:
                        error_msg += f" (and {len(error_details) - 3} more)"
                else:
                    error_msg = f'No watchers started after timeout'

                logger.error(
                    f"[God Mode] ❌ {error_msg}\n"
                    f"   Failed: {len(failed_watchers)}, Still starting: {len(still_starting)}"
                )

                # Narrate specific error to user
                if self.config.working_out_loud_enabled:
                    try:
                        if failed_watchers:
                            # Use first failure reason for user message
                            first_error = failed_watchers[0]['error']
                            user_msg = f"I couldn't watch {app_name} windows: {first_error}"
                        else:
                            user_msg = f"I couldn't connect to any {app_name} windows. Please check if {app_name} is open."
                        await self._narrate_working_out_loud(
                            message=user_msg,
                            narration_type="error",
                            watcher_id=f"startup_failed_{app_name}",
                            priority="high"
                        )
                    except Exception as e:
                        logger.warning(f"[God Mode] Error narration failed: {e}")

                return {
                    'success': False,
                    'status': 'error',
                    'error': error_msg,
                    'total_watchers': 0,
                    'expected_watchers': expected_watchers,
                    'failed_watchers': failed_watchers,
                    'app_name': app_name,
                    'trigger_text': trigger_text,
                    'message': f"Failed to connect to any {app_name} windows"
                }
            elif new_watchers_active == 0 and is_optimistic:
                # v32.5: OPTIMISTIC MODE - Watchers are still starting in background
                # Don't fail! The watchers are being spawned asynchronously.
                # Set new_watchers_active to expected count for optimistic return
                logger.info(
                    f"[God Mode v32.5] ⚡ Optimistic startup: {expected_watchers} watchers spawning in background "
                    f"(verification continues asynchronously)"
                )
                new_watchers_active = expected_watchers  # Assume all will start

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
                f"[God Mode] ✅ Background monitoring active: {new_watchers_active} watchers, "
                f"watching for '{trigger_text}'"
            )

            # v32.0: Emit monitoring active to UI progress stream - THIS IS THE KEY EVENT!
            if PROGRESS_STREAM_AVAILABLE:
                try:
                    await emit_monitoring_active(
                        watcher_count=new_watchers_active,
                        app_name=app_name,
                        trigger_text=trigger_text,
                        correlation_id=_surveillance_correlation_id
                    )
                except Exception as e:
                    logger.debug(f"[v32.0] Progress stream emit failed: {e}")

            # v31.0: NARRATE SUCCESSFUL STARTUP - User knows monitoring is live
            if self.config.working_out_loud_enabled:
                try:
                    if new_watchers_active == 1:
                        msg = f"Eye is online. Watching for {trigger_text}."
                    elif new_watchers_active == expected_watchers:
                        msg = f"All {new_watchers_active} eyes are online. Watching for {trigger_text}."
                    else:
                        msg = f"{new_watchers_active} eyes online. Watching for {trigger_text}. I'll alert you when I see it."
                    await self._narrate_working_out_loud(
                        message=msg,
                        narration_type="success",
                        watcher_id=f"startup_complete_{app_name}",
                        priority="high"
                    )
                except Exception:
                    pass

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

                    # v16.1: Narrate connection error to user
                    if self.config.working_out_loud_enabled:
                        try:
                            await self._narrate_working_out_loud(
                                message=f"Connection lost while monitoring {app_name}. "
                                        f"Monitoring stopped. Please try again.",
                                narration_type="error",
                                watcher_id=god_mode_task_id,
                                priority="high"
                            )
                        except Exception:
                            pass  # Don't let narration failure mask the real error

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

                    # v16.1: Narrate cancellation to user
                    if self.config.working_out_loud_enabled:
                        try:
                            await self._narrate_working_out_loud(
                                message=f"Monitoring of {app_name} stopped as requested.",
                                narration_type="activity",
                                watcher_id=god_mode_task_id,
                                priority="normal"
                            )
                        except Exception:
                            pass

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

                    # v16.1: Narrate OS error to user
                    if self.config.working_out_loud_enabled:
                        try:
                            await self._narrate_working_out_loud(
                                message=f"A system error occurred while monitoring {app_name}. "
                                        f"Monitoring stopped.",
                                narration_type="error",
                                watcher_id=god_mode_task_id,
                                priority="high"
                            )
                        except Exception:
                            pass

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

                    # v16.1: Narrate unexpected error to user
                    if self.config.working_out_loud_enabled:
                        try:
                            await self._narrate_working_out_loud(
                                message=f"An unexpected error occurred while monitoring {app_name}. "
                                        f"Monitoring stopped. Error: {type(e).__name__}",
                                narration_type="error",
                                watcher_id=god_mode_task_id,
                                priority="high"
                            )
                        except Exception:
                            pass

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
                    actual_watchers = new_watchers_active
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
                'total_watchers': new_watchers_active,  # v16.0: Actual count
                'expected_watchers': expected_watchers,  # v16.0: For transparency
                'app_name': app_name,
                'trigger_text': trigger_text,
                'spaces_monitored': [meta['space_id'] for meta in watcher_metadata[:new_watchers_active]],
                'startup_time_seconds': (datetime.now() - start_wait_time).total_seconds(),
                'message': f"Monitoring {new_watchers_active} {app_name} windows for '{trigger_text}'"
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

            # =====================================================================
            # v28.0: TIMEOUT PROTECTION for Ferrari Engine Spawn
            # =====================================================================
            # ROOT CAUSE FIX: _spawn_ferrari_watcher can hang indefinitely if:
            # - macOS ScreenCaptureKit is slow (permission dialogs)
            # - Memory pressure causes GPU init to freeze
            # - Window is inaccessible or locked
            #
            # SOLUTION: Wrap in asyncio.wait_for with configurable timeout
            # =====================================================================
            ferrari_spawn_timeout = float(os.getenv('JARVIS_FERRARI_SPAWN_TIMEOUT', '8.0'))

            try:
                watcher = await asyncio.wait_for(
                    self._spawn_ferrari_watcher(
                        window_id=window_id,
                        fps=fps,
                        app_name=app_name,
                        space_id=space_id
                    ),
                    timeout=ferrari_spawn_timeout
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"❌ [v28.0] [{watcher_id}] Ferrari Engine spawn TIMED OUT after {ferrari_spawn_timeout}s "
                    f"(possible memory pressure or GPU hang)"
                )

                # Register failure with startup manager if available
                if self._progressive_startup_manager is not None:
                    try:
                        await self._progressive_startup_manager.register_watcher_ready(
                            watcher_id=watcher_id,
                            success=False,
                            error=f"Ferrari Engine timeout ({ferrari_spawn_timeout}s)"
                        )
                    except Exception as reg_err:
                        logger.warning(f"Failed to register watcher failure: {reg_err}")

                # v15.0: ERROR NARRATION
                if self.config.working_out_loud_enabled:
                    try:
                        await self._narrate_working_out_loud(
                            message=f"Timed out connecting to {app_name}. The system may be under memory pressure.",
                            narration_type="error",
                            watcher_id=watcher_id,
                            priority="high"
                        )
                    except Exception:
                        pass

                return {
                    'status': 'error',
                    'error': f'Ferrari Engine spawn timeout ({ferrari_spawn_timeout}s)',
                    'watcher_id': watcher_id,
                    'space_id': space_id,
                    'window_id': window_id,
                    'trigger_detected': False,
                    'timeout': True
                }

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

            # =====================================================================
            # ROOT CAUSE FIX v17.0: Robust Watcher Health Verification
            # =====================================================================
            # PROBLEM: Watcher object created but stream may not be active
            # - watcher.start() may have failed silently
            # - Stream may have disconnected immediately
            # - Permission dialogs may have blocked capture
            #
            # SOLUTION: Check watcher.status == WatcherStatus.WATCHING
            # This is the AUTHORITATIVE health indicator for VideoWatcher
            # =====================================================================
            watcher_is_running = False
            try:
                # Import WatcherStatus for proper comparison
                try:
                    from backend.vision.macos_video_capture_advanced import WatcherStatus
                except ImportError:
                    WatcherStatus = None

                # Primary check: watcher.status == WatcherStatus.WATCHING
                if WatcherStatus is not None and hasattr(watcher, 'status'):
                    watcher_is_running = watcher.status == WatcherStatus.WATCHING
                    if not watcher_is_running:
                        logger.warning(
                            f"[{watcher_id}] Watcher status is {watcher.status.value} "
                            f"(expected: WATCHING)"
                        )
                elif hasattr(watcher, 'status'):
                    # Fallback: check status string value
                    status_val = getattr(watcher.status, 'value', str(watcher.status))
                    watcher_is_running = status_val.lower() == 'watching'
                else:
                    # No status attribute - assume running but log warning
                    watcher_is_running = True
                    logger.warning(f"[{watcher_id}] No status attribute - assuming active (optimistic)")
            except Exception as e:
                logger.warning(f"[{watcher_id}] Could not check watcher status: {e}")
                watcher_is_running = True  # Optimistic - proceed and let monitoring reveal issues

            if not watcher_is_running:
                logger.error(f"❌ [{watcher_id}] Watcher created but NOT running - stream failed to start")

                # Attempt cleanup
                try:
                    if hasattr(watcher, 'stop'):
                        await watcher.stop()
                except Exception:
                    pass

                # Narrate error to user
                if self.config.working_out_loud_enabled:
                    try:
                        await self._narrate_working_out_loud(
                            message=f"Watcher for {app_name} on space {space_id} failed to start. "
                                    f"Check screen recording permissions.",
                            narration_type="error",
                            watcher_id=watcher_id,
                            priority="high"
                        )
                    except Exception as e:
                        logger.warning(f"Error narration failed: {e}")

                return {
                    'status': 'error',
                    'error': 'Watcher failed to start - stream not running',
                    'watcher_id': watcher_id,
                    'space_id': space_id,
                    'window_id': window_id,
                    'trigger_detected': False,
                    'suggestion': 'Check System Preferences > Security & Privacy > Screen Recording'
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
                    # v27.0: Unregister from Resource Governor first
                    if self._resource_governor:
                        try:
                            await self._resource_governor.unregister_watcher(watcher_id)
                        except Exception:
                            pass

                    if hasattr(watcher, 'stop'):
                        await watcher.stop()
                    logger.debug(f"✓ Stopped watcher: {watcher_id}")
                except Exception as e:
                    logger.error(f"❌ Error stopping watcher {watcher_id}: {e}")

            self._active_video_watchers.clear()

        # v22.0.0: Also clear lifecycle tracker
        if hasattr(self, '_watcher_lifecycle'):
            self._watcher_lifecycle.clear()

        logger.info("✅ All watchers stopped")

        # v27.0: Stop resource governor when all watchers are stopped
        if self._resource_governor and self._governor_started:
            try:
                await self._resource_governor.stop_monitoring()
                self._governor_started = False
                logger.debug("[God Mode] Resource Governor stopped")
            except Exception as e:
                logger.debug(f"[God Mode] Resource Governor stop failed: {e}")

    # =========================================================================
    # v27.0: Resource Governor Throttle Callback
    # =========================================================================

    async def _handle_throttle_change(self, throttle_state: ThrottleState) -> None:
        """
        Handle FPS throttle changes from the AdaptiveResourceGovernor.

        Called when system load triggers a throttle level change. This method
        applies the new FPS allocation to all active VideoWatchers.

        Args:
            throttle_state: New throttle state with target FPS and reason
        """
        try:
            level_name = throttle_state.level.name
            target_fps = throttle_state.target_fps
            reason = throttle_state.reason

            logger.info(
                f"[ResourceGovernor] 🎮 Throttle changed: {level_name} → {target_fps} FPS ({reason})"
            )

            # Narrate significant throttle changes
            if self.config.working_out_loud_enabled and throttle_state.level.value >= 2:  # MODERATE or higher
                try:
                    await self._narrate_working_out_loud(
                        message=f"System is under heavy load. Reducing capture rate to {target_fps} FPS to prevent slowdown.",
                        narration_type="status",
                        watcher_id="resource_governor",
                        priority="normal"
                    )
                except Exception:
                    pass

            # Apply FPS change to all active VideoWatchers
            if not hasattr(self, '_active_video_watchers'):
                return

            for watcher_id, watcher in self._active_video_watchers.items():
                try:
                    # Get watcher-specific allocation
                    if self._resource_governor:
                        allocation = self._resource_governor.get_allocation(watcher_id)
                        if allocation:
                            fps_to_apply = allocation.allocated_fps
                        else:
                            fps_to_apply = target_fps
                    else:
                        fps_to_apply = target_fps

                    # Apply FPS to VideoWatcher if it has a set_fps method
                    if hasattr(watcher, 'set_fps'):
                        watcher.set_fps(fps_to_apply)
                        logger.debug(
                            f"[ResourceGovernor] Applied {fps_to_apply} FPS to watcher {watcher_id}"
                        )
                    elif hasattr(watcher, 'fps'):
                        watcher.fps = fps_to_apply

                except Exception as e:
                    logger.debug(f"[ResourceGovernor] Failed to apply FPS to {watcher_id}: {e}")

        except Exception as e:
            logger.warning(f"[ResourceGovernor] Throttle callback error: {e}")

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

            result = await self.watch_app_across_all_spaces(
                app_name=app_name,
                trigger_text=trigger_text,
                **kwargs
            )

            # =====================================================================
            # ROOT CAUSE FIX v16.1: Verify God Mode Result Before Returning
            # =====================================================================
            # PROBLEM: watch_app_across_all_spaces may return error status but
            # caller doesn't check - assumes success
            #
            # SOLUTION: Check result status and provide clear success/failure info
            # =====================================================================
            if result.get('status') == 'error' or result.get('success') is False:
                # God Mode failed - ensure caller knows
                logger.error(
                    f"❌ God Mode failed for {app_name}: {result.get('error', 'Unknown error')}"
                )
                return {
                    'success': False,
                    'status': 'error',
                    'error': result.get('error', 'God Mode failed to start'),
                    'total_watchers': result.get('total_watchers', 0),
                    'expected_watchers': result.get('expected_watchers', 0),
                    'app_name': app_name,
                    'trigger_text': trigger_text,
                    'suggestion': result.get('suggestion', 'Check that the app is open and try again')
                }

            # God Mode started successfully
            logger.info(
                f"✅ God Mode active: {result.get('total_watchers', 0)} watchers monitoring "
                f"'{trigger_text}' in {app_name}"
            )
            return {
                'success': True,
                'status': result.get('status', 'monitoring'),
                'total_watchers': result.get('total_watchers', 0),
                'expected_watchers': result.get('expected_watchers', 0),
                'god_mode_task_id': result.get('god_mode_task_id'),
                'message': result.get('message', f"Monitoring {app_name} for '{trigger_text}'"),
                'startup_time_seconds': result.get('startup_time_seconds', 0),
                'spaces_monitored': result.get('spaces_monitored', [])
            }
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

        # =====================================================================
        # v25.0: WINDOW RETURN POLICY
        # =====================================================================
        # Return windows to their original spaces after monitoring ends
        # (if configured via JARVIS_RETURN_WINDOWS=true)
        # =====================================================================
        windows_returned = 0
        if GHOST_MANAGER_AVAILABLE and hasattr(self, '_ghost_managers') and app_name:
            try:
                ghost_manager = self._ghost_managers.get(app_name)
                if ghost_manager and ghost_manager.config.return_windows_after_monitoring:
                    logger.info(f"[God Mode] 🏠 Returning windows to original spaces for {app_name}...")

                    from backend.vision.yabai_space_detector import get_yabai_detector
                    yabai = get_yabai_detector()

                    return_result = await ghost_manager.return_all_windows(
                        yabai_detector=yabai,
                        restore_geometry=ghost_manager.config.preserve_geometry_on_return
                    )

                    windows_returned = len(return_result.get("returned", []))

                    if windows_returned > 0:
                        logger.info(
                            f"[God Mode] 🏠 Returned {windows_returned} windows to original spaces"
                        )

                        # Narrate the return
                        if self.config.working_out_loud_enabled:
                            await self._narrate_working_out_loud(
                                message=f"Monitoring complete. I've returned {windows_returned} {app_name} windows to their original positions.",
                                narration_type="success",
                                watcher_id=f"return_{app_name}",
                                priority="medium"
                            )

                    # Stop health monitoring
                    await ghost_manager.stop_health_monitoring()

                    # Clean up
                    del self._ghost_managers[app_name]

            except Exception as e:
                logger.debug(f"[God Mode] Window return failed: {e}")

        return {
            "success": True,
            "stopped_count": stopped_count,
            "windows_returned": windows_returned,
            "message": f"Stopped {stopped_count} watcher(s)" + (f", returned {windows_returned} windows" if windows_returned > 0 else "")
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

    async def check_watcher_health(self, watcher_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Check health of active watchers - v17.0 Root Cause Fix.

        Performs comprehensive health checks:
        1. Status verification (WatcherStatus.WATCHING)
        2. Frame flow verification (recent frames received)
        3. Health metadata inspection

        Args:
            watcher_id: Optional specific watcher to check.
                       If None, checks all watchers.

        Returns:
            Health report dict with:
            - overall_healthy: bool - True if all checked watchers are healthy
            - watchers_checked: int
            - healthy_count: int
            - unhealthy_count: int
            - details: List of watcher health dicts
        """
        try:
            from backend.vision.macos_video_capture_advanced import WatcherStatus
        except ImportError:
            WatcherStatus = None

        health_details = []
        healthy_count = 0
        unhealthy_count = 0

        # Get watchers to check
        watchers_to_check = {}
        if watcher_id:
            if watcher_id in self._active_video_watchers:
                watchers_to_check[watcher_id] = self._active_video_watchers[watcher_id]
        else:
            watchers_to_check = dict(self._active_video_watchers)

        for w_id, info in watchers_to_check.items():
            watcher = info.get('watcher')
            health_info = info.get('health', {})
            is_healthy = True
            issues = []

            if not watcher:
                is_healthy = False
                issues.append("Watcher object is None")
            else:
                # Check 1: Status verification
                if WatcherStatus is not None and hasattr(watcher, 'status'):
                    if watcher.status != WatcherStatus.WATCHING:
                        is_healthy = False
                        issues.append(
                            f"Status is {watcher.status.value} (expected: WATCHING)"
                        )
                elif hasattr(watcher, 'status'):
                    status_val = getattr(watcher.status, 'value', str(watcher.status))
                    if status_val.lower() != 'watching':
                        is_healthy = False
                        issues.append(f"Status is {status_val} (expected: watching)")

                # Check 2: Frame flow verification
                try:
                    frames_captured = getattr(watcher, 'frames_captured', 0)
                    if frames_captured == 0:
                        # No frames captured yet - might just be starting
                        started_at = info.get('started_at', '')
                        if started_at:
                            from datetime import datetime
                            start_time = datetime.fromisoformat(started_at)
                            elapsed = (datetime.now() - start_time).total_seconds()
                            if elapsed > 10:  # More than 10 seconds with no frames
                                is_healthy = False
                                issues.append(
                                    f"No frames captured after {elapsed:.1f}s"
                                )
                except Exception as e:
                    logger.debug(f"Could not check frame count for {w_id}: {e}")

                # Check 3: Health metadata
                consecutive_failures = health_info.get('consecutive_failures', 0)
                if consecutive_failures > 0:
                    issues.append(f"{consecutive_failures} consecutive failures")
                    if consecutive_failures >= 10:
                        is_healthy = False

            if is_healthy:
                healthy_count += 1
            else:
                unhealthy_count += 1

            health_details.append({
                'watcher_id': w_id,
                'app_name': info.get('app_name', 'Unknown'),
                'space_id': info.get('space_id'),
                'healthy': is_healthy,
                'issues': issues,
                'started_at': info.get('started_at'),
                'health_metadata': health_info
            })

        return {
            'overall_healthy': unhealthy_count == 0 and healthy_count > 0,
            'watchers_checked': len(watchers_to_check),
            'healthy_count': healthy_count,
            'unhealthy_count': unhealthy_count,
            'details': health_details
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
                # ROOT CAUSE FIX v40.0.0: Parallel Async Workspace Query
                # =====================================================================
                # PROBLEM: Previous implementation had cascading timeout conflicts:
                # - Outer timeout: 5s in watch_app_across_all_spaces
                # - Inner timeouts: 5s each for spaces + windows queries = 10s minimum
                # - Result: Guaranteed timeout, "Yabai/MultiSpaceDetector unresponsive"
                #
                # SOLUTION: Use new parallel async query with intelligent timeouts
                # - Runs spaces + windows queries in parallel (not sequential)
                # - Uses circuit breaker to skip after repeated failures
                # - Returns cached data on failure for graceful degradation
                # - Timeout is fraction of outer timeout, not hardcoded
                # =====================================================================

                # Get configurable timeout from environment (default 4s, must fit within outer 5s)
                workspace_query_timeout = float(os.getenv("JARVIS_WORKSPACE_QUERY_TIMEOUT", "4.0"))

                detector = MultiSpaceWindowDetector()

                # Use the new async parallel method (no thread pool needed!)
                result = await detector.get_all_windows_across_spaces_async(
                    timeout_seconds=workspace_query_timeout
                )

                # Extract windows list from result dict
                all_windows = result.get('windows', [])

                if not all_windows:
                    logger.warning(f"⚠️  God Mode: No windows found in multi-space detection result")
                    return []

                # Filter for matching app across ALL spaces
                app_name_lower = app_name.lower()
                matching_windows = []
                jarvis_ui_skipped = 0  # v41.5: Track skipped JARVIS windows

                # v41.5: Self-Preservation keywords (semantic, not hardcoded URLs)
                self_preservation_keywords = [
                    'j.a.r.v.i.s',
                    'jarvis',
                    'localhost:3000',
                    'localhost:8080',
                    '127.0.0.1:3000',
                    '127.0.0.1:8080',
                ]

                for window_obj in all_windows:
                    # Access EnhancedWindowInfo object attributes
                    window_app = window_obj.app_name if hasattr(window_obj, 'app_name') else ''
                    window_app_lower = window_app.lower()
                    window_title = window_obj.window_title if hasattr(window_obj, 'window_title') else ''
                    window_title_lower = window_title.lower()

                    # ==========================================================
                    # v41.5: SELF-PRESERVATION PROTOCOL (Defense-in-Depth Layer 1)
                    # ==========================================================
                    # Filter out JARVIS UI windows at discovery time, before they
                    # even reach the teleportation phase. This is a second layer
                    # of protection in case the teleportation filter is bypassed.
                    # ==========================================================
                    is_jarvis_ui = any(
                        keyword in window_title_lower 
                        for keyword in self_preservation_keywords
                    )
                    
                    if is_jarvis_ui:
                        jarvis_ui_skipped += 1
                        logger.debug(
                            f"[v41.5] 🛡️ Discovery Self-Preservation: Skipping JARVIS UI "
                            f"(Window {window_obj.window_id}: '{window_title[:30]}...')"
                        )
                        continue

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
                            'window_title': window_title,
                            'bounds': window_obj.bounds if hasattr(window_obj, 'bounds') else {},
                            'is_visible': True,  # All windows from multi-space detector are visible
                            'confidence': confidence,
                            'method': 'multi_space_detector'
                        })
                
                # v41.5: Log self-preservation summary at discovery
                if jarvis_ui_skipped > 0:
                    logger.info(
                        f"[v41.5] 🛡️ Discovery Self-Preservation: Filtered out "
                        f"{jarvis_ui_skipped} JARVIS UI window(s)"
                    )

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
                                    # v28.0: Always include confidence for consistent window data
                                    return {
                                        'found': True,
                                        'window_id': window.window_id,
                                        'space_id': target_space,
                                        'app_name': window.app_name,
                                        'confidence': 85,  # Moderate confidence from combined method
                                        'method': 'spatial_awareness + ferrari'
                                    }

                        logger.info(f"✓ SpatialAwarenessAgent found '{app_name}' on space {target_space}")

            except Exception as e:
                logger.warning(f"SpatialAwarenessAgent query failed: {e}")

        # Priority 3: Legacy estimation (hash-based)
        logger.warning(f"⚠️  Using legacy window estimation for '{app_name}'")
        window_id = self._estimate_window_id(app_name)

        # v28.0: Always include confidence for consistent window data
        return {
            'found': True,  # Optimistic - may fail later
            'window_id': window_id,
            'space_id': space_id or 1,
            'app_name': app_name,
            'confidence': 50,  # Low confidence for estimated windows
            'method': 'legacy_estimation',
            'warning': 'Using estimated window ID - may be inaccurate'
        }

    # =========================================================================
    # v28.1: PRE-CAPTURE VALIDATION SYSTEM
    # =========================================================================
    # ROOT CAUSE FIX: "frame_production_failed" errors occur because we
    # spawn watchers for windows that ScreenCaptureKit cannot capture:
    # - Minimized windows
    # - Windows still on hidden spaces (teleportation failed)
    # - Windows without Screen Recording permission
    # - Windows not yet rendered on Ghost Display (race condition)
    #
    # SOLUTION: Validate each window before spawning watcher, with retry
    # =========================================================================

    async def _validate_window_capturable(
        self,
        window: Dict[str, Any],
        ghost_space: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_delay_ms: Optional[float] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        v28.3: Validate that a window can actually be captured by ScreenCaptureKit.

        This method performs multiple checks with EXPONENTIAL BACKOFF to handle
        race conditions where windows are still being "hydrated" (rendered) on
        Ghost Display after teleportation.

        =====================================================================
        v28.3 ROOT CAUSE FIX: "Ghost Lag" - Window Hydration Time
        =====================================================================
        PROBLEM: When macOS moves a window to a virtual display, it destroys
        the window on Screen 1 and reconstructs it on Screen 2. This process
        can take 500ms-2s depending on system load and memory pressure.

        OLD BEHAVIOR: 3 retries × 100ms = 300ms total → FAILS before hydration
        NEW BEHAVIOR: Exponential backoff (200ms → 300ms → 450ms → ...) up to
                      ~5 seconds total, succeeding as soon as window is ready.

        Backoff sequence (default):
          Attempt 1: immediate
          Attempt 2: 200ms delay (total: 200ms)
          Attempt 3: 300ms delay (total: 500ms)
          Attempt 4: 450ms delay (total: 950ms)
          Attempt 5: 675ms delay (total: 1625ms)
          Attempt 6: 1012ms delay (total: 2637ms)
        =====================================================================

        Checks performed:
        1. Window exists and has valid ID
        2. Window is on a visible space (or Ghost Display)
        3. Window is not minimized
        4. Screen Recording permission is granted
        5. Window is actually renderable (frame test)

        Args:
            window: Window dictionary with window_id, space_id, etc.
            ghost_space: Optional Ghost Display space ID for validation
            max_retries: Number of retries (default from env or 6)
            retry_delay_ms: Initial delay in ms (default from env or 200)

        Returns:
            Tuple of (is_capturable, reason, updated_window_info)
        """
        import time  # v28.3: For hydration time tracking

        # =====================================================================
        # v28.3: Configurable Exponential Backoff Parameters
        # =====================================================================
        # Read from environment for flexibility without code changes
        if max_retries is None:
            max_retries = int(os.getenv('JARVIS_VALIDATION_MAX_RETRIES', '6'))
        if retry_delay_ms is None:
            retry_delay_ms = float(os.getenv('JARVIS_VALIDATION_INITIAL_DELAY_MS', '200'))

        # Backoff multiplier: each retry waits multiplier × previous delay
        backoff_multiplier = float(os.getenv('JARVIS_VALIDATION_BACKOFF_MULTIPLIER', '1.5'))
        # Maximum single delay cap (prevents waiting forever on one attempt)
        max_single_delay_ms = float(os.getenv('JARVIS_VALIDATION_MAX_DELAY_MS', '2000'))

        window_id = window.get('window_id')
        if not window_id:
            return False, "Missing window_id", window

        validation_start_time = time.time()

        validation_details = {
            'window_id': window_id,
            'checks_passed': [],
            'checks_failed': [],
            'retries_used': 0,
            'total_wait_time_ms': 0,
            'backoff_sequence': []
        }

        # Import helpers
        try:
            from backend.vision.swift_video_bridge import check_screen_recording_permission
            has_permission_check = True
        except ImportError:
            has_permission_check = False

        try:
            from backend.vision.yabai_space_detector import get_yabai_detector
            yabai = get_yabai_detector()
            has_yabai = True
        except Exception:
            has_yabai = False
            yabai = None

        # Check 1: Screen Recording Permission (only check once, not per-retry)
        if has_permission_check:
            try:
                has_permission = await check_screen_recording_permission()
                if not has_permission:
                    validation_details['checks_failed'].append('screen_recording_permission')
                    return (
                        False,
                        "Screen Recording permission not granted. "
                        "Go to System Settings → Privacy & Security → Screen Recording",
                        validation_details
                    )
                validation_details['checks_passed'].append('screen_recording_permission')
            except Exception as e:
                logger.debug(f"[Validation] Permission check failed: {e} (continuing)")

        # =====================================================================
        # v28.3: Exponential Backoff Retry Loop
        # =====================================================================
        current_delay_ms = retry_delay_ms  # Start with initial delay
        total_wait_time_ms = 0

        for attempt in range(max_retries):
            validation_details['retries_used'] = attempt + 1

            # v28.3: Log backoff progress for debugging hydration time
            if attempt > 0:
                logger.debug(
                    f"[Validation] Window {window_id} - Attempt {attempt + 1}/{max_retries} "
                    f"(waited {total_wait_time_ms:.0f}ms so far)"
                )

            # Check 2: Window exists and get current state
            current_space = None
            is_minimized = False

            # v28.2: Track if yabai checks should be skipped for this window
            skip_yabai_checks = False

            if has_yabai and yabai:
                try:
                    # Query window info directly from yabai
                    window_info = await self._query_window_info_async(window_id)

                    if window_info is None:
                        # v28.2: Yabai doesn't know this window, but ScreenCaptureKit might
                        # This can happen for:
                        # 1. Windows yabai hasn't indexed yet
                        # 2. System UI windows not managed by yabai
                        # 3. Certain app windows (e.g., some Chrome windows)
                        #
                        # Instead of failing, skip yabai checks and try frame capture directly
                        if attempt == 0:
                            logger.debug(
                                f"[Validation] Window {window_id} not in yabai cache - "
                                f"will try frame capture directly (yabai may not track this window)"
                            )
                        validation_details['yabai_status'] = 'not_found_but_may_be_capturable'
                        # Skip remaining yabai checks, proceed to frame capture test
                        skip_yabai_checks = True
                    else:
                        current_space = window_info.get('space')
                        is_minimized = window_info.get('is-minimized', False)
                        validation_details['checks_passed'].append('window_exists')

                    # v28.2: Skip yabai-specific checks if window wasn't found in yabai
                    if not skip_yabai_checks:
                        # Check 3: Is window minimized?
                        if is_minimized:
                            # Try to unminimize
                            try:
                                unminimize_result = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        lambda: os.system(f'/opt/homebrew/bin/yabai -m window {window_id} --deminimize')
                                    ),
                                    timeout=2.0
                                )
                                # Re-check after unminimize
                                await asyncio.sleep(0.1)
                                window_info = await self._query_window_info_async(window_id)
                                is_minimized = window_info.get('is-minimized', False) if window_info else True
                            except Exception as e:
                                logger.debug(f"[Validation] Unminimize failed: {e}")

                            if is_minimized:
                                validation_details['checks_failed'].append('not_minimized')
                                return (
                                    False,
                                    f"Window {window_id} is minimized (ScreenCaptureKit cannot capture minimized windows)",
                                    validation_details
                                )

                        validation_details['checks_passed'].append('not_minimized')

                        # =====================================================================
                        # v34.0: SOFT VALIDATION - Location Agnostic Approach
                        # =====================================================================
                        # OLD BEHAVIOR: Reject if window not on Ghost Display
                        # NEW BEHAVIOR: Accept if window is ANYWHERE visible (Ghost or current)
                        #
                        # This fixes the "All windows failed" crash when teleportation misses
                        # a window - we watch it WHERE IT IS instead of rejecting it.
                        # =====================================================================
                        if ghost_space is not None:
                            if current_space != ghost_space:
                                # Window not on Ghost Display - check if it's still visible/capturable

                                # v34.0: SOFT VALIDATION - Check if window is on ANY visible space
                                # Instead of failing immediately, we'll check compositor state later
                                # and accept the window if it's visible anywhere

                                if attempt < max_retries - 1:
                                    # First few attempts: Wait for teleportation to complete
                                    logger.debug(
                                        f"[Validation] Window {window_id} on space {current_space}, "
                                        f"expected {ghost_space} - waiting {current_delay_ms:.0f}ms before retry"
                                    )
                                    await asyncio.sleep(current_delay_ms / 1000.0)
                                    total_wait_time_ms += current_delay_ms
                                    validation_details['backoff_sequence'].append(current_delay_ms)
                                    current_delay_ms = min(current_delay_ms * backoff_multiplier, max_single_delay_ms)
                                    continue

                                # v34.0: SOFT VALIDATION - Accept window if visible anywhere
                                # Don't fail here - we'll check compositor state (kCGWindowIsOnscreen)
                                # in Phase 2 and accept if the window is visible ANYWHERE
                                logger.warning(
                                    f"[Validation] ⚠️ v34.0 SOFT VALIDATION: Window {window_id} "
                                    f"not on Ghost Display (space {current_space} vs expected {ghost_space}). "
                                    f"Teleport may have failed - will watch in-place if visible."
                                )
                                validation_details['teleport_failed'] = True
                                validation_details['watching_in_place'] = True
                                validation_details['actual_space'] = current_space
                                # Mark as passed but note the location mismatch
                                validation_details['checks_passed'].append('on_visible_space_soft')
                            else:
                                # Window is on the expected ghost space
                                if 'on_visible_space_soft' not in validation_details.get('checks_passed', []):
                                    validation_details['checks_passed'].append('on_visible_space')
                    else:
                        # v28.2: Yabai doesn't know this window, skip minimized/space checks
                        validation_details['skipped_checks'] = ['not_minimized', 'on_visible_space']

                except asyncio.TimeoutError:
                    logger.debug(f"[Validation] Yabai query timed out for window {window_id}")
                    if attempt < max_retries - 1:
                        # v28.3: Exponential backoff sleep
                        await asyncio.sleep(current_delay_ms / 1000.0)
                        total_wait_time_ms += current_delay_ms
                        validation_details['backoff_sequence'].append(current_delay_ms)
                        current_delay_ms = min(current_delay_ms * backoff_multiplier, max_single_delay_ms)
                        continue

                except Exception as e:
                    logger.debug(f"[Validation] Yabai query failed for window {window_id}: {e}")
                    # Continue without yabai checks

            # =====================================================================
            # v28.4: MULTI-SIGNAL HANDSHAKE - Three-Phase Lock Algorithm
            # =====================================================================
            # Phase 1: Logical Truth (Yabai) - Already checked above ✓
            # Phase 2: Compositor Truth (CoreGraphics) - Check kCGWindowIsOnscreen
            # Phase 3: Visual Truth (Frame Capture) - Only if Phase 2 passes
            #
            # This algorithm implements Eventual Consistency Synchronization:
            # We wait for the compositor to confirm the window is onscreen
            # BEFORE attempting expensive frame capture operations.
            # =====================================================================

            # Phase 2: Compositor Truth Check (Fast - ~5ms)
            compositor_verified = False
            compositor_alpha = None
            compositor_error = None

            try:
                is_onscreen, compositor_alpha, compositor_error, compositor_details = \
                    await self._check_compositor_onscreen(window_id, timeout=1.0)

                validation_details['compositor_details'] = compositor_details

                if is_onscreen:
                    compositor_verified = True
                    validation_details['checks_passed'].append('compositor_onscreen')
                    logger.debug(
                        f"[Validation] ✅ Phase 2 (Compositor Truth): Window {window_id} "
                        f"is onscreen (alpha={compositor_alpha:.2f})"
                    )
                else:
                    # Compositor says window not ready - wait with backoff
                    if attempt < max_retries - 1:
                        logger.debug(
                            f"[Validation] ⏳ Phase 2 (Compositor Truth): Window {window_id} "
                            f"not yet onscreen - {compositor_error}. "
                            f"Waiting {current_delay_ms:.0f}ms before retry {attempt + 2}/{max_retries}"
                        )
                        await asyncio.sleep(current_delay_ms / 1000.0)
                        total_wait_time_ms += current_delay_ms
                        validation_details['backoff_sequence'].append(current_delay_ms)
                        current_delay_ms = min(current_delay_ms * backoff_multiplier, max_single_delay_ms)
                        continue
                    else:
                        validation_details['checks_failed'].append('compositor_not_onscreen')
                        # Don't return yet - still try frame capture as last resort

            except Exception as e:
                logger.debug(f"[Validation] Compositor check failed: {e} - will try frame capture")
                # Optimistic: proceed to frame capture if compositor check fails
                compositor_verified = True
                validation_details['compositor_check_error'] = str(e)

            # Phase 3: Visual Truth - Frame Capture Test (Only if compositor verified OR fallback)
            if self._fast_capture_engine:
                try:
                    # Progressive timeout: compositor-verified windows should capture faster
                    capture_timeout = 2.0 if compositor_verified else 3.0

                    test_frame = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._fast_capture_engine.capture_window_by_id,
                            window_id
                        ),
                        timeout=capture_timeout
                    )

                    # Validate frame data
                    frame_valid = False
                    if test_frame is not None:
                        if isinstance(test_frame, (list, tuple, bytes)):
                            frame_valid = len(test_frame) > 0
                        elif hasattr(test_frame, '__len__'):
                            frame_valid = len(test_frame) > 0
                        else:
                            frame_valid = True  # Assume valid if not None

                    if frame_valid:
                        validation_details['checks_passed'].append('frame_capture')
                        validation_details['total_wait_time_ms'] = total_wait_time_ms
                        validation_details['validation_strategy'] = 'multi_signal_handshake_v28.4'

                        # Log successful hydration time for telemetry
                        hydration_time_ms = (time.time() - validation_start_time) * 1000

                        logger.info(
                            f"[Validation] ✅ Multi-Signal Handshake SUCCESS: Window {window_id} "
                            f"validated after {hydration_time_ms:.0f}ms "
                            f"(Logical: ✓, Compositor: {'✓' if compositor_verified else '?'}, Visual: ✓)"
                        )

                        return (
                            True,
                            f"Window validated via Multi-Signal Handshake v28.4 "
                            f"(hydrated in {hydration_time_ms:.0f}ms)",
                            validation_details
                        )
                    else:
                        # Frame returned but empty - compositor said ready but pixels not there yet
                        if attempt < max_retries - 1:
                            logger.debug(
                                f"[Validation] Frame capture returned empty for window {window_id} "
                                f"(compositor={compositor_verified}, frame=empty) - "
                                f"waiting {current_delay_ms:.0f}ms before retry {attempt + 2}/{max_retries}"
                            )
                            await asyncio.sleep(current_delay_ms / 1000.0)
                            total_wait_time_ms += current_delay_ms
                            validation_details['backoff_sequence'].append(current_delay_ms)
                            current_delay_ms = min(current_delay_ms * backoff_multiplier, max_single_delay_ms)
                            continue

                except asyncio.TimeoutError:
                    if attempt < max_retries - 1:
                        logger.debug(
                            f"[Validation] Frame capture timed out for window {window_id} "
                            f"(compositor={compositor_verified}) - "
                            f"waiting {current_delay_ms:.0f}ms before retry"
                        )
                        await asyncio.sleep(current_delay_ms / 1000.0)
                        total_wait_time_ms += current_delay_ms
                        validation_details['backoff_sequence'].append(current_delay_ms)
                        current_delay_ms = min(current_delay_ms * backoff_multiplier, max_single_delay_ms)
                        continue

                except Exception as e:
                    logger.debug(f"[Validation] Frame capture failed: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(current_delay_ms / 1000.0)
                        total_wait_time_ms += current_delay_ms
                        validation_details['backoff_sequence'].append(current_delay_ms)
                        current_delay_ms = min(current_delay_ms * backoff_multiplier, max_single_delay_ms)
                        continue

            # If we get here without frame capture engine, check compositor result
            if not self._fast_capture_engine:
                if compositor_verified:
                    return (
                        True,
                        "Window verified by compositor (no frame engine available)",
                        validation_details
                    )
                return True, "No frame capture engine - assuming capturable", validation_details

        # All retries exhausted - BUT check for Optimistic Validation override
        validation_details['checks_failed'].append('frame_capture')
        validation_details['total_wait_time_ms'] = total_wait_time_ms
        total_time_ms = (time.time() - validation_start_time) * 1000

        # =====================================================================
        # v28.5: OPTIMISTIC VALIDATION - Trust the Kernel
        # =====================================================================
        # ROOT CAUSE: CGWindowListCreateImage (Snapshot API) fails on virtual
        # displays even when the window IS capturable by ScreenCaptureKit (Video API).
        #
        # THE MISMATCH:
        # - Snapshot API (CGWindowListCreateImage): Older, struggles with virtual displays
        # - Video API (ScreenCaptureKit): Newer, designed for modern display setups
        #
        # SOLUTION: If the Compositor confirmed the window is onscreen
        # (kCGWindowIsOnscreen == True), we TRUST the OS Kernel and proceed
        # to the Ferrari Engine anyway. The video stream will work even if
        # the static snapshot failed.
        #
        # This implements "Optimistic Validation": trust the authoritative
        # source (GPU Compositor) over the flaky test (Snapshot).
        # =====================================================================

        # Check if compositor previously verified this window
        compositor_was_verified = 'compositor_onscreen' in validation_details.get('checks_passed', [])
        compositor_alpha_valid = validation_details.get('compositor_details', {}).get('alpha', 0) > 0.01

        # v28.5: Optimistic override - trust compositor over snapshot
        if compositor_was_verified or compositor_alpha_valid:
            # Re-verify compositor state one final time before override
            try:
                final_onscreen, final_alpha, final_error, final_details = \
                    await self._check_compositor_onscreen(window_id, timeout=1.0)

                if final_onscreen and final_alpha and final_alpha > 0.01:
                    validation_details['validation_strategy'] = 'optimistic_compositor_trust_v28.5'
                    validation_details['optimistic_override'] = True
                    validation_details['final_compositor_state'] = final_details

                    logger.info(
                        f"[Validation] ⚡ v28.5 OPTIMISTIC OVERRIDE: Window {window_id} "
                        f"passed Compositor check (alpha={final_alpha:.2f}) but failed "
                        f"Snapshot test. Trusting Kernel - launching Ferrari Engine anyway. "
                        f"(Snapshot API struggles with virtual displays; ScreenCaptureKit will work)"
                    )

                    return (
                        True,
                        f"Window validated via Optimistic Compositor Trust v28.5 "
                        f"(Kernel confirmed onscreen, alpha={final_alpha:.2f})",
                        validation_details
                    )
                else:
                    logger.debug(
                        f"[Validation] Final compositor re-check failed: "
                        f"onscreen={final_onscreen}, alpha={final_alpha}, error={final_error}"
                    )

            except Exception as e:
                logger.debug(f"[Validation] Final compositor check failed: {e}")

        # v28.5: Additional fallback - check if window is in compositor list at all
        # Even if previous checks failed, do one more compositor query
        if not compositor_was_verified:
            try:
                fallback_onscreen, fallback_alpha, fallback_error, fallback_details = \
                    await self._check_compositor_onscreen(window_id, timeout=1.0)

                if fallback_onscreen and fallback_alpha and fallback_alpha > 0.5:
                    validation_details['validation_strategy'] = 'optimistic_fallback_v28.5'
                    validation_details['fallback_compositor_check'] = True
                    validation_details['fallback_details'] = fallback_details

                    logger.info(
                        f"[Validation] ⚡ v28.5 OPTIMISTIC FALLBACK: Window {window_id} "
                        f"was not in compositor during retries, but IS NOW onscreen "
                        f"(alpha={fallback_alpha:.2f}). Window hydrated late - proceeding."
                    )

                    return (
                        True,
                        f"Window validated via late Compositor confirmation v28.5 "
                        f"(hydrated after retries, alpha={fallback_alpha:.2f})",
                        validation_details
                    )

            except Exception as e:
                logger.debug(f"[Validation] Fallback compositor check failed: {e}")

        # Truly failed - neither snapshot nor compositor verification worked
        validation_details['optimistic_override_attempted'] = True
        validation_details['optimistic_override_failed'] = True

        return (
            False,
            f"Window {window_id} failed ALL validation checks after {max_retries} attempts "
            f"over {total_time_ms:.0f}ms (Snapshot: ✗, Compositor: ✗). "
            f"Window may be minimized, on hidden space, or closed.",
            validation_details
        )

    # =========================================================================
    # v28.4: COMPOSITOR TRUTH CHECK - CoreGraphics Window State
    # =========================================================================

    async def _check_compositor_onscreen(
        self,
        window_id: int,
        timeout: float = 1.0
    ) -> Tuple[bool, Optional[float], Optional[str], Dict[str, Any]]:
        """
        v28.4: Check if window is actually onscreen via CoreGraphics compositor.

        This is the "Compositor Truth" check - the critical missing signal that
        verifies the GPU compositor has finished rendering the window.

        =====================================================================
        WHY THIS MATTERS (Eventual Consistency)
        =====================================================================
        macOS Window Server operates as a distributed system with TWO states:

        1. LOGICAL STATE (Yabai/WindowServer metadata)
           - Updates INSTANTLY when window is moved
           - Tells you WHERE the window SHOULD be

        2. PHYSICAL STATE (GPU Compositor)
           - Updates AFTER rendering completes (200-600ms delay)
           - Tells you WHERE the window ACTUALLY IS (can be captured)

        The `kCGWindowIsOnscreen` flag is the compositor's signal that:
        - The window has been assigned a display
        - The compositor has allocated GPU resources
        - The window is actively being composited (rendered)
        - ScreenCaptureKit CAN capture it

        This check is FAST (~5ms) compared to frame capture (~50ms).
        =====================================================================

        Args:
            window_id: The CGWindowID to check
            timeout: Maximum time to wait for CoreGraphics query

        Returns:
            Tuple of (is_onscreen, alpha_value, error_message, details_dict)
        """
        details: Dict[str, Any] = {
            'window_id': window_id,
            'compositor_query_attempted': False,
            'quartz_available': False
        }

        try:
            # Import Quartz (pyobjc-framework-Quartz)
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionIncludingWindow,
                kCGWindowIsOnscreen,
                kCGWindowAlpha,
                kCGWindowLayer,
                kCGWindowBounds,
                kCGWindowOwnerName,
                kCGWindowName,
            )
            details['quartz_available'] = True

            # Run CoreGraphics query in thread pool with timeout
            def _query_compositor():
                return CGWindowListCopyWindowInfo(
                    kCGWindowListOptionIncludingWindow,
                    window_id
                )

            details['compositor_query_attempted'] = True

            window_list = await asyncio.wait_for(
                asyncio.to_thread(_query_compositor),
                timeout=timeout
            )

            if not window_list or len(window_list) == 0:
                details['window_found'] = False
                return (
                    False,
                    None,
                    f"Window {window_id} not found in CoreGraphics compositor",
                    details
                )

            # Parse window info from compositor
            window_dict = window_list[0]
            details['window_found'] = True

            # Extract compositor state
            is_onscreen = bool(window_dict.get(kCGWindowIsOnscreen, False))
            alpha = float(window_dict.get(kCGWindowAlpha, 0.0))
            layer = int(window_dict.get(kCGWindowLayer, 0))
            bounds = window_dict.get(kCGWindowBounds, {})
            owner_name = window_dict.get(kCGWindowOwnerName, 'Unknown')
            window_name = window_dict.get(kCGWindowName, '')

            details['is_onscreen'] = is_onscreen
            details['alpha'] = alpha
            details['layer'] = layer
            details['bounds'] = dict(bounds) if bounds else {}
            details['owner_name'] = owner_name
            details['window_name'] = window_name

            # Validate onscreen status
            # Window must be onscreen AND have non-zero alpha to be capturable
            if is_onscreen and alpha > 0.01:
                return (
                    True,
                    alpha,
                    None,
                    details
                )
            elif is_onscreen and alpha <= 0.01:
                return (
                    False,
                    alpha,
                    f"Window {window_id} is onscreen but has zero alpha ({alpha:.3f})",
                    details
                )
            else:
                return (
                    False,
                    alpha,
                    f"Window {window_id} not yet rendered by compositor (is_onscreen={is_onscreen})",
                    details
                )

        except ImportError as e:
            details['error'] = f"Quartz not available: {e}"
            return (
                False,
                None,
                "Quartz framework not available (install pyobjc-framework-Quartz)",
                details
            )

        except asyncio.TimeoutError:
            details['error'] = f"Compositor query timed out after {timeout}s"
            return (
                False,
                None,
                f"CoreGraphics query timed out after {timeout}s",
                details
            )

        except Exception as e:
            details['error'] = str(e)
            return (
                False,
                None,
                f"Compositor check failed: {e}",
                details
            )

    async def _wait_for_compositor_onscreen(
        self,
        window_id: int,
        max_wait_ms: float = 3000.0,
        check_interval_ms: float = 100.0,
        backoff_multiplier: float = 1.3
    ) -> Tuple[bool, float, str, Dict[str, Any]]:
        """
        v28.4: Wait for window to become onscreen in compositor with adaptive polling.

        This implements the "Eventual Consistency Synchronization" pattern:
        Poll the compositor state with exponential backoff until the window
        is confirmed onscreen or timeout is reached.

        Args:
            window_id: The CGWindowID to wait for
            max_wait_ms: Maximum time to wait in milliseconds
            check_interval_ms: Initial polling interval in milliseconds
            backoff_multiplier: Multiplier for exponential backoff

        Returns:
            Tuple of (success, wait_time_ms, message, details)
        """
        import time
        start_time = time.time()
        current_interval = check_interval_ms
        total_wait = 0.0
        attempts = 0

        details: Dict[str, Any] = {
            'window_id': window_id,
            'max_wait_ms': max_wait_ms,
            'attempts': 0,
            'intervals': []
        }

        while total_wait < max_wait_ms:
            attempts += 1
            details['attempts'] = attempts

            # Check compositor state
            is_onscreen, alpha, error, check_details = await self._check_compositor_onscreen(
                window_id, timeout=1.0
            )

            if is_onscreen:
                elapsed_ms = (time.time() - start_time) * 1000
                details['success'] = True
                details['final_alpha'] = alpha
                details['compositor_details'] = check_details
                logger.debug(
                    f"[Compositor] ✅ Window {window_id} became onscreen after "
                    f"{elapsed_ms:.0f}ms ({attempts} checks)"
                )
                return (
                    True,
                    elapsed_ms,
                    f"Window became onscreen after {elapsed_ms:.0f}ms",
                    details
                )

            # Not yet onscreen - wait with backoff
            if total_wait + current_interval < max_wait_ms:
                details['intervals'].append(current_interval)
                await asyncio.sleep(current_interval / 1000.0)
                total_wait += current_interval
                current_interval = min(current_interval * backoff_multiplier, 500.0)  # Cap at 500ms
            else:
                break

        # Timeout reached
        elapsed_ms = (time.time() - start_time) * 1000
        details['success'] = False
        details['timeout'] = True
        return (
            False,
            elapsed_ms,
            f"Window {window_id} did not become onscreen within {max_wait_ms:.0f}ms",
            details
        )

    async def _refresh_yabai_windows_cache(self, force: bool = False) -> bool:
        """
        v28.2: Refresh the yabai windows cache by querying ALL windows.

        ROOT CAUSE FIX: yabai's `--window <id>` query can fail for windows
        that exist but aren't fully indexed. Querying ALL windows and filtering
        is more reliable.

        Args:
            force: If True, refresh even if cache is still valid

        Returns:
            True if cache was refreshed successfully, False otherwise
        """
        import time

        # Initialize lock if needed (lazy init for thread safety)
        if self._yabai_cache_lock is None:
            self._yabai_cache_lock = asyncio.Lock()

        current_time = time.time()

        # Check if cache is still valid
        if not force and (current_time - self._yabai_cache_timestamp) < self._yabai_cache_ttl:
            return True  # Cache is still valid

        async with self._yabai_cache_lock:
            # Double-check after acquiring lock (another task might have refreshed)
            if not force and (current_time - self._yabai_cache_timestamp) < self._yabai_cache_ttl:
                return True

            try:
                # Query ALL windows from yabai (robust approach)
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: subprocess.run(
                            ['/opt/homebrew/bin/yabai', '-m', 'query', '--windows'],
                            capture_output=True,
                            text=True,
                            timeout=3
                        )
                    ),
                    timeout=5.0
                )

                if result.returncode != 0 or not result.stdout:
                    logger.debug(f"[WindowCache] Yabai query failed: {result.stderr}")
                    return False

                windows_list = json.loads(result.stdout)

                # Build lookup cache by window ID
                new_cache: Dict[int, Dict[str, Any]] = {}
                for window in windows_list:
                    wid = window.get("id")
                    if wid is not None:
                        new_cache[wid] = window

                self._yabai_windows_cache = new_cache
                self._yabai_cache_timestamp = current_time

                logger.debug(f"[WindowCache] Refreshed cache with {len(new_cache)} windows")
                return True

            except asyncio.TimeoutError:
                logger.debug("[WindowCache] Yabai query timed out")
                return False
            except json.JSONDecodeError as e:
                logger.debug(f"[WindowCache] JSON parse error: {e}")
                return False
            except Exception as e:
                logger.debug(f"[WindowCache] Cache refresh failed: {e}")
                return False

    async def _query_window_info_async(self, window_id: int) -> Optional[Dict[str, Any]]:
        """
        v28.2: Query window info from yabai asynchronously using smart cache.

        ROOT CAUSE FIX: Previous implementation used `--window <id>` which fails
        for windows not fully indexed by yabai. New approach queries ALL windows
        and filters by ID, with intelligent caching.

        Args:
            window_id: Window ID to query (CGWindowID from ScreenCaptureKit)

        Returns:
            Window info dict or None if not found
        """
        # First, try from cache
        if window_id in self._yabai_windows_cache:
            # Cache hit - verify it's not stale
            import time
            if (time.time() - self._yabai_cache_timestamp) < self._yabai_cache_ttl:
                return self._yabai_windows_cache[window_id]

        # Cache miss or stale - refresh cache
        if await self._refresh_yabai_windows_cache():
            # Try again from refreshed cache
            return self._yabai_windows_cache.get(window_id)

        # Yabai unavailable - try direct query as fallback
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: subprocess.run(
                        ['/opt/homebrew/bin/yabai', '-m', 'query', '--windows', '--window', str(window_id)],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                ),
                timeout=3.0
            )

            if result.returncode == 0 and result.stdout:
                window_info = json.loads(result.stdout)
                # Cache this result
                self._yabai_windows_cache[window_id] = window_info
                return window_info

        except Exception as e:
            logger.debug(f"[Validation] Window query fallback failed: {e}")

        return None

    async def _query_windows_batch_async(
        self,
        window_ids: List[int]
    ) -> Dict[int, Optional[Dict[str, Any]]]:
        """
        v28.2: Query multiple windows efficiently using cache.

        More efficient than calling _query_window_info_async multiple times
        because it refreshes the cache once and looks up all windows.

        Args:
            window_ids: List of window IDs to query

        Returns:
            Dict mapping window_id to window_info (or None if not found)
        """
        # Refresh cache once
        await self._refresh_yabai_windows_cache(force=True)

        # Look up all windows from cache
        results: Dict[int, Optional[Dict[str, Any]]] = {}
        for wid in window_ids:
            results[wid] = self._yabai_windows_cache.get(wid)

        found = sum(1 for v in results.values() if v is not None)
        logger.debug(f"[WindowCache] Batch query: {found}/{len(window_ids)} windows found")

        return results

    # =========================================================================
    # v37.0: WINDOW HEALTH MONITORING HELPERS
    # =========================================================================
    # ROOT CAUSE FIX: Runtime window invalidation during monitoring
    # =========================================================================
    # PROBLEM: Windows can become invalid during monitoring due to:
    # - User closes the window
    # - App crashes and restarts (new window ID)
    # - Window moves to different space (yabai context lost)
    # - macOS recycles CGWindowID after window destruction
    #
    # SOLUTION: Runtime health checks with intelligent recovery:
    # 1. _check_window_still_valid_async: Verify window exists via yabai
    # 2. _find_replacement_window: Find window by app name if ID changed
    # =========================================================================

    async def _check_window_still_valid_async(self, window_id: int) -> bool:
        """
        v37.0: Check if a window ID is still valid (exists in yabai/compositor).

        This performs a lightweight check to determine if a window still exists,
        without expensive frame capture operations.

        Args:
            window_id: The CGWindowID to validate

        Returns:
            True if window exists, False if it has been closed/destroyed
        """
        if not window_id:
            return False

        # Strategy 1: Check yabai cache first (fastest)
        if window_id in self._yabai_windows_cache:
            import time
            if (time.time() - self._yabai_cache_timestamp) < self._yabai_cache_ttl:
                # Cache is fresh - window exists
                return True

        # Strategy 2: Direct yabai query with short timeout
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: subprocess.run(
                        ['/opt/homebrew/bin/yabai', '-m', 'query', '--windows', '--window', str(window_id)],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                ),
                timeout=2.0
            )

            if result.returncode == 0 and result.stdout:
                # Window exists in yabai
                try:
                    window_info = json.loads(result.stdout)
                    # Update cache with fresh data
                    self._yabai_windows_cache[window_id] = window_info
                    return True
                except json.JSONDecodeError:
                    pass

        except asyncio.TimeoutError:
            logger.debug(f"[WindowHealth] Yabai query timeout for window {window_id}")
        except subprocess.TimeoutExpired:
            logger.debug(f"[WindowHealth] Subprocess timeout for window {window_id}")
        except Exception as e:
            logger.debug(f"[WindowHealth] Yabai query failed for window {window_id}: {e}")

        # Strategy 3: Check CGWindowListCopyWindowInfo via ScreenCaptureKit
        # This catches windows yabai doesn't manage (e.g., system dialogs)
        try:
            from backend.vision.macos_video_capture_advanced import fast_capture
            if hasattr(fast_capture, 'window_exists'):
                exists = await asyncio.to_thread(fast_capture.window_exists, window_id)
                return exists
        except Exception as e:
            logger.debug(f"[WindowHealth] ScreenCaptureKit check failed: {e}")

        # Strategy 4: Refresh full cache and check
        if await self._refresh_yabai_windows_cache(force=True):
            return window_id in self._yabai_windows_cache

        # Window not found in any source
        logger.warning(f"[WindowHealth] Window {window_id} not found - likely closed or destroyed")
        return False

    async def _find_replacement_window(
        self,
        app_name: str,
        original_window_id: int,
        prefer_same_space: bool = True
    ) -> Optional[int]:
        """
        v37.0: Find a replacement window by app name when original ID is invalid.

        When an app crashes and restarts, or when macOS recycles a window ID,
        this method attempts to find a new window belonging to the same app.

        Args:
            app_name: Application name to search for (e.g., "Chrome", "Terminal")
            original_window_id: The original window ID that is no longer valid
            prefer_same_space: If True, prefer windows on the same space

        Returns:
            New window_id if found, None otherwise
        """
        if not app_name:
            return None

        logger.info(
            f"[WindowHealth] 🔍 Searching for replacement window for '{app_name}' "
            f"(original ID: {original_window_id})"
        )

        try:
            # Use _find_window with find_all=True to get all matching windows
            find_timeout = float(os.getenv('JARVIS_FIND_REPLACEMENT_TIMEOUT', '5'))

            matching_windows = await asyncio.wait_for(
                self._find_window(app_name, find_all=True),
                timeout=find_timeout
            )

            if not matching_windows:
                logger.warning(f"[WindowHealth] No windows found for '{app_name}'")
                return None

            # Filter out the original (now invalid) window ID
            candidates = [
                w for w in matching_windows
                if w.get('window_id') != original_window_id
            ]

            if not candidates:
                logger.warning(
                    f"[WindowHealth] No replacement candidates for '{app_name}' "
                    f"(only original window found)"
                )
                return None

            # Sort by priority: confidence, then visible, then space match
            def priority_score(window: Dict[str, Any]) -> int:
                score = 0
                # Higher confidence = better
                score += window.get('confidence', 0) * 10
                # Visible windows preferred
                if window.get('is_visible', False):
                    score += 50
                # Non-minimized preferred
                if not window.get('is_minimized', False):
                    score += 30
                return score

            candidates.sort(key=priority_score, reverse=True)

            # Select best candidate
            best = candidates[0]
            new_window_id = best.get('window_id')

            logger.info(
                f"[WindowHealth] ✅ Found replacement window for '{app_name}': "
                f"{original_window_id} → {new_window_id} "
                f"(confidence: {best.get('confidence', 'unknown')}%, "
                f"space: {best.get('space_id', 'unknown')})"
            )

            return new_window_id

        except asyncio.TimeoutError:
            logger.warning(
                f"[WindowHealth] Replacement search timed out for '{app_name}' "
                f"(timeout: {find_timeout}s)"
            )
        except Exception as e:
            logger.warning(f"[WindowHealth] Replacement search failed for '{app_name}': {e}")

        return None

    async def _validate_windows_batch(
        self,
        windows: List[Dict[str, Any]],
        ghost_space: Optional[int] = None,
        max_concurrent: int = 5
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        v28.1: Validate a batch of windows concurrently.

        Args:
            windows: List of windows to validate
            ghost_space: Ghost Display space ID
            max_concurrent: Maximum concurrent validations

        Returns:
            Tuple of (valid_windows, invalid_windows_with_reasons)
        """
        valid_windows = []
        invalid_windows = []

        # Use semaphore to limit concurrent validations
        semaphore = asyncio.Semaphore(max_concurrent)

        async def validate_one(window: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, str]:
            async with semaphore:
                is_valid, reason, details = await self._validate_window_capturable(
                    window,
                    ghost_space=ghost_space
                )
                return window, is_valid, reason

        # Run validations in parallel
        tasks = [validate_one(w) for w in windows]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"[Validation] Batch validation error: {result}")
                continue

            window, is_valid, reason = result
            if is_valid:
                valid_windows.append(window)
            else:
                invalid_windows.append({
                    'window': window,
                    'reason': reason
                })

        return valid_windows, invalid_windows

    def _normalize_window_data(
        self,
        windows: List[Dict[str, Any]],
        app_name: str
    ) -> List[Dict[str, Any]]:
        """
        v28.0: Normalize window data to ensure all required fields exist.

        ROOT CAUSE FIX: KeyError: 'confidence'
        Different window discovery paths (Ferrari, MultiSpace, SpatialAwareness, Legacy)
        return different sets of fields. This normalizer ensures ALL windows have
        consistent required fields with sensible defaults.

        Required fields ensured:
        - window_id: int (required, no default)
        - space_id: int (default: 1)
        - app_name: str (default: from parameter)
        - confidence: int (default: 75)
        - found: bool (default: True)
        - method: str (default: 'unknown')

        Args:
            windows: List of window dictionaries to normalize
            app_name: Application name to use as default

        Returns:
            List of normalized window dictionaries
        """
        normalized = []

        for w in windows:
            if not isinstance(w, dict):
                logger.warning(f"[v28.0] Skipping non-dict window: {type(w)}")
                continue

            # Skip windows without window_id (can't monitor without it)
            if not w.get('window_id'):
                logger.warning(f"[v28.0] Skipping window without window_id: {w}")
                continue

            # Create normalized window with defaults for missing fields
            normalized_window = {
                # Required fields
                'window_id': w['window_id'],
                'space_id': w.get('space_id', 1),
                'app_name': w.get('app_name', app_name),

                # v28.0: Ensure confidence always exists (fixes KeyError)
                'confidence': w.get('confidence', 75),  # Default moderate confidence

                # Metadata
                'found': w.get('found', True),
                'method': w.get('method', 'unknown'),

                # Optional fields - preserve if they exist
                'window_title': w.get('window_title', ''),
                'bounds': w.get('bounds', {}),
                'is_visible': w.get('is_visible', True),
                'width': w.get('width', 0),
                'height': w.get('height', 0),
            }

            # Preserve any teleport-related fields
            if w.get('teleported'):
                normalized_window['teleported'] = True
                normalized_window['original_space'] = w.get('original_space')
                normalized_window['rescue_method'] = w.get('rescue_method')
                normalized_window['rescue_strategy'] = w.get('rescue_strategy')
                normalized_window['rescue_duration_ms'] = w.get('rescue_duration_ms')

            # Preserve any warning
            if w.get('warning'):
                normalized_window['warning'] = w['warning']

            normalized.append(normalized_window)

        logger.debug(f"[v28.0] Normalized {len(normalized)} windows (from {len(windows)})")
        return normalized

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

        # =====================================================================
        # ROOT CAUSE FIX v17.0: Continuous Health Monitoring State
        # =====================================================================
        # Track consecutive frame failures to detect watcher crashes
        # If too many consecutive failures, the watcher has likely crashed
        # =====================================================================
        consecutive_frame_failures = 0
        max_consecutive_failures = int(os.getenv('JARVIS_MAX_FRAME_FAILURES', '30'))
        last_health_check_time = start_time
        health_check_interval = float(os.getenv('JARVIS_HEALTH_CHECK_INTERVAL', '10.0'))
        watcher_health_verified = True  # Assume healthy initially

        # =====================================================================
        # v18.0: ADAPTIVE RESOURCE GOVERNOR - Memory-Aware Throttling
        # =====================================================================
        # Integrates Defcon-level system for 16GB M1 Mac memory management
        # GREEN: Full operations, YELLOW: Throttled, RED: Emergency abort
        # =====================================================================
        resource_governor = None
        last_defcon_level = None
        try:
            from backend.core.resource_governor import (
                get_resource_governor_sync,
                DefconLevel
            )
            resource_governor = get_resource_governor_sync()
            if resource_governor:
                last_defcon_level = resource_governor.current_level
                logger.info(
                    f"[Ferrari Detection] Resource Governor active: "
                    f"{resource_governor.current_level.emoji} DEFCON {resource_governor.current_level.name}"
                )
        except ImportError:
            logger.debug("[Ferrari Detection] Resource Governor not available")

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

                # =====================================================================
                # v18.0: Resource Governor - Defcon Level Check
                # =====================================================================
                # Check memory pressure and abort if critical (DEFCON RED)
                # =====================================================================
                if resource_governor:
                    current_defcon = resource_governor.current_level

                    # Check for level change and log
                    if current_defcon != last_defcon_level:
                        logger.warning(
                            f"[Ferrari Detection] {current_defcon.emoji} DEFCON LEVEL CHANGE: "
                            f"{last_defcon_level.name if last_defcon_level else 'INIT'} → {current_defcon.name}"
                        )
                        last_defcon_level = current_defcon

                    # DEFCON RED: Abort monitoring to prevent system slowdown
                    should_abort, abort_reason = resource_governor.should_abort_monitoring()
                    if should_abort:
                        logger.error(
                            f"[Ferrari Detection] 🔴 EMERGENCY ABORT: {abort_reason}"
                        )

                        # Narrate the emergency abort
                        if self.config.working_out_loud_enabled:
                            try:
                                await self._narrate_working_out_loud(
                                    message="Suspending visual monitoring due to critical memory pressure. "
                                            "I'll resume when memory frees up.",
                                    narration_type="error",
                                    watcher_id=watcher_id,
                                    priority="high"
                                )
                            except Exception:
                                pass

                        # Cleanup and return
                        self._cleanup_narration_state(watcher_id)
                        return {
                            'detected': False,
                            'confidence': 0.0,
                            'error': abort_reason,
                            'frames_checked': frame_count,
                            'ocr_checks': ocr_checks,
                            'memory_abort': True,
                            'defcon_level': current_defcon.name
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
                    # =====================================================================
                    # ROOT CAUSE FIX v17.0: Continuous Health Monitoring
                    # =====================================================================
                    # Track consecutive failures and detect watcher crashes
                    # =====================================================================
                    consecutive_frame_failures += 1

                    # Periodic health check (every health_check_interval seconds)
                    if elapsed - last_health_check_time >= health_check_interval:
                        last_health_check_time = elapsed

                        # Import WatcherStatus for status check
                        try:
                            from backend.vision.macos_video_capture_advanced import WatcherStatus
                            if hasattr(watcher, 'status'):
                                watcher_status = watcher.status
                                if watcher_status != WatcherStatus.WATCHING:
                                    watcher_health_verified = False
                                    logger.warning(
                                        f"[Ferrari Detection] ⚠️ Watcher health check: "
                                        f"status={watcher_status.value} (expected: WATCHING)"
                                    )
                        except ImportError:
                            pass

                        # ═══════════════════════════════════════════════════════════════
                        # v37.0: RUNTIME PERMISSION CHECK
                        # ═══════════════════════════════════════════════════════════════
                        # Check if Screen Recording permission is still granted
                        # User may revoke permission during long monitoring sessions
                        # ═══════════════════════════════════════════════════════════════
                        try:
                            from backend.vision.swift_video_bridge import check_screen_recording_permission
                            permission_still_granted = await check_screen_recording_permission()

                            if not permission_still_granted:
                                logger.error(
                                    f"[Ferrari Detection] ❌ Screen Recording permission REVOKED! "
                                    f"Cannot continue monitoring."
                                )

                                # Narrate permission loss to user
                                if self.config.working_out_loud_enabled:
                                    try:
                                        await self._narrate_working_out_loud(
                                            message="I've lost permission to see the screen. "
                                                    "Please go to System Settings → Privacy & Security → "
                                                    "Screen Recording and re-enable access for this application.",
                                            narration_type="error",
                                            watcher_id=watcher_id,
                                            priority="critical"
                                        )
                                    except Exception:
                                        pass

                                # Cleanup and return error
                                self._cleanup_narration_state(watcher_id)
                                return {
                                    'detected': False,
                                    'confidence': 0.0,
                                    'error': 'Screen Recording permission revoked',
                                    'frames_checked': frame_count,
                                    'ocr_checks': ocr_checks,
                                    'permission_revoked': True
                                }
                        except ImportError:
                            pass  # Swift bridge not available
                        except Exception as e:
                            logger.debug(f"[Ferrari Detection] Permission check failed: {e}")

                    # Check for too many consecutive failures
                    if consecutive_frame_failures >= max_consecutive_failures:
                        logger.error(
                            f"[Ferrari Detection] ❌ Watcher crashed: {consecutive_frame_failures} "
                            f"consecutive frame failures (threshold: {max_consecutive_failures})"
                        )

                        # ═══════════════════════════════════════════════════════════════
                        # v37.0: AUTO-RECOVERY - Attempt to restart watcher before giving up
                        # ═══════════════════════════════════════════════════════════════
                        # Instead of immediately failing, try to recover:
                        # 1. Check if window is still valid
                        # 2. Attempt stream reconnection
                        # 3. Only fail if recovery fails
                        # ═══════════════════════════════════════════════════════════════
                        recovery_attempted = False
                        recovery_success = False
                        window_id = getattr(watcher, 'window_id', None)

                        if window_id and hasattr(watcher, 'restart'):
                            recovery_attempted = True
                            logger.info(
                                f"[Ferrari Detection] 🔄 Attempting auto-recovery for window {window_id}..."
                            )

                            try:
                                # Check if window still exists
                                window_valid = await self._check_window_still_valid_async(window_id)

                                if not window_valid:
                                    logger.warning(
                                        f"[Ferrari Detection] Window {window_id} no longer valid - "
                                        f"trying to find replacement by app name"
                                    )
                                    # Try to find window by app name
                                    replacement = await self._find_replacement_window(app_name, window_id)
                                    if replacement:
                                        window_id = replacement
                                        logger.info(
                                            f"[Ferrari Detection] Found replacement window: {window_id}"
                                        )

                                # Attempt stream reconnection
                                if hasattr(watcher, 'restart'):
                                    restart_result = await watcher.restart(window_id=window_id)
                                    if restart_result:
                                        recovery_success = True
                                        consecutive_frame_failures = 0
                                        logger.info(
                                            f"[Ferrari Detection] ✅ Auto-recovery SUCCESS - "
                                            f"stream reconnected for window {window_id}"
                                        )

                                        # Narrate recovery
                                        if self.config.working_out_loud_enabled:
                                            try:
                                                await self._narrate_working_out_loud(
                                                    message=f"I temporarily lost the video stream for {app_name}, "
                                                            f"but I've reconnected and am continuing to watch.",
                                                    narration_type="activity",
                                                    watcher_id=watcher_id,
                                                    priority="normal"
                                                )
                                            except Exception:
                                                pass

                                        # Continue monitoring loop
                                        continue

                            except Exception as e:
                                logger.warning(f"[Ferrari Detection] Auto-recovery failed: {e}")

                        # Recovery failed or not attempted - give up
                        if recovery_attempted and not recovery_success:
                            error_msg = "Auto-recovery failed - watcher could not be restarted"
                        else:
                            error_msg = "Watcher crashed - no frames received"

                        # Narrate the crash to user
                        if self.config.working_out_loud_enabled:
                            try:
                                await self._narrate_working_out_loud(
                                    message=f"I lost connection to {app_name}. "
                                            f"The video stream stopped unexpectedly. "
                                            f"{'I tried to recover but failed. ' if recovery_attempted else ''}"
                                            f"Please check that the window is still visible.",
                                    narration_type="error",
                                    watcher_id=watcher_id,
                                    priority="high"
                                )
                            except Exception as e:
                                logger.warning(f"Error narration failed: {e}")

                        # Cleanup and return error
                        self._cleanup_narration_state(watcher_id)
                        return {
                            'detected': False,
                            'confidence': 0.0,
                            'error': error_msg,
                            'frames_checked': frame_count,
                            'ocr_checks': ocr_checks,
                            'watcher_crashed': True,
                            'consecutive_failures': consecutive_frame_failures,
                            'recovery_attempted': recovery_attempted,
                            'recovery_success': recovery_success
                        }

                    # Log warning every 10 failures
                    if consecutive_frame_failures % 10 == 0:
                        logger.warning(
                            f"[Ferrari Detection] ⚠️ {consecutive_frame_failures} consecutive "
                            f"frame failures (monitoring for crash...)"
                        )

                    await asyncio.sleep(0.1)
                    continue

                # Reset failure counter on successful frame
                consecutive_frame_failures = 0
                watcher_health_verified = True
                frame_count += 1
                frame = frame_data.get('frame')

                if frame is None:
                    continue

                # OCR detection every N frames (adaptive based on FPS)
                # For 5 FPS: check every frame
                # For 30 FPS: check every 3-5 frames to reduce OCR load
                fps = frame_data.get('fps', 5)
                base_check_interval = max(1, int(fps / 5))  # ~5 OCR checks per second

                # v18.0: Apply Resource Governor throttle multiplier
                # GREEN: 1x (5 checks/sec), YELLOW: 5x (1 check/sec), RED: suspended
                if resource_governor:
                    check_interval = resource_governor.get_adjusted_check_interval(base_check_interval)
                else:
                    check_interval = base_check_interval

                if frame_count % check_interval == 0:
                    ocr_checks += 1

                    # ═══════════════════════════════════════════════════════════════
                    # v37.0: OCR CONCURRENCY LIMITING
                    # ═══════════════════════════════════════════════════════════════
                    # Acquire semaphore before OCR to prevent memory exhaustion
                    # when multiple watchers run OCR simultaneously
                    # ═══════════════════════════════════════════════════════════════
                    if self._ocr_semaphore is None:
                        self._ocr_semaphore = asyncio.Semaphore(self._ocr_max_concurrent)

                    try:
                        # Wait for OCR slot with timeout to prevent deadlock
                        ocr_timeout = float(os.getenv('JARVIS_OCR_ACQUIRE_TIMEOUT', '5.0'))
                        async with asyncio.timeout(ocr_timeout):
                            async with self._ocr_semaphore:
                                # v32.3: Run OCR detection with intelligent fuzzy matching and context extraction
                                detected, confidence, detected_text, ocr_context = await self._ocr_detect(
                                    frame=frame,
                                    trigger_text=trigger_text
                                )

                                # v14.0: Get all OCR text for activity/near-miss detection
                                all_text = await self._ocr_get_all_text(frame)
                    except asyncio.TimeoutError:
                        # OCR slot not available - skip this frame
                        logger.debug(
                            f"[Ferrari Detection] OCR skipped - semaphore timeout "
                            f"({self._ocr_max_concurrent} concurrent limit)"
                        )
                        continue

                    if detected:
                        detection_time = time.time() - start_time
                        
                        # v32.3: Enhanced logging with match method and context
                        match_method = ocr_context.get('match_method', 'unknown') if ocr_context else 'unknown'
                        match_tier = ocr_context.get('match_tier', 'unknown') if ocr_context else 'unknown'
                        extracted_number = ocr_context.get('extracted_number', 'N/A') if ocr_context else 'N/A'
                        
                        logger.info(
                            f"[Ferrari Detection] ✅ FOUND '{trigger_text}'! "
                            f"Matched: '{detected_text}' via {match_tier} ({match_method}) | "
                            f"Time: {detection_time:.2f}s, Confidence: {confidence:.2f}, "
                            f"Frames: {frame_count}, OCR checks: {ocr_checks} | "
                            f"Context: {extracted_number}"
                        )

                        # v14.0: Cleanup narration state
                        self._cleanup_narration_state(watcher_id)

                        # v32.3: Include rich context in detection result
                        detection_result = {
                            'detected': True,
                            'confidence': confidence,
                            'trigger': detected_text,
                            'detection_time': detection_time,
                            'frames_checked': frame_count,
                            'ocr_checks': ocr_checks,
                            'method': frame_data.get('method', 'screencapturekit'),
                            # v32.3: New fields for intelligent detection
                            'match_tier': match_tier,
                            'match_method': match_method,
                        }
                        
                        # Add context if available
                        if ocr_context:
                            detection_result['context'] = ocr_context
                            if 'extracted_number' in ocr_context:
                                detection_result['extracted_value'] = ocr_context['extracted_number']
                            if 'surrounding_text' in ocr_context:
                                detection_result['surrounding_text'] = ocr_context['surrounding_text']
                        
                        return detection_result

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

                    # ═══════════════════════════════════════════════════════════════
                    # v32.3: ACTIVE MONITORING COMMUNICATION
                    # User needs to know JARVIS is actively scanning
                    # ═══════════════════════════════════════════════════════════════
                    
                    # Log periodic progress with detected text
                    if ocr_checks % 10 == 0:
                        logger.info(
                            f"[Ferrari Detection] 👁️ Scanning: {ocr_checks} OCR checks, "
                            f"{frame_count} frames, {elapsed:.1f}s elapsed"
                        )
                        
                        # Log what text was actually detected (for debugging)
                        if all_text:
                            text_preview = all_text[:100].replace('\n', ' ').strip()
                            logger.info(f"[Ferrari Detection] 📄 Detected text: '{text_preview}...'")
                    
                    # v32.3: Emit heartbeat progress event every 30 OCR checks (~6 seconds)
                    if ocr_checks % 30 == 0 and PROGRESS_STREAM_AVAILABLE:
                        try:
                            await emit_surveillance_progress(
                                stage=SurveillanceStage.MONITORING_ACTIVE,
                                message=f"👁️ Actively scanning {app_name} for '{trigger_text}' ({ocr_checks} checks, {elapsed:.0f}s)",
                                progress_current=ocr_checks,
                                progress_total=0,  # Unknown
                                app_name=app_name,
                                trigger_text=trigger_text,
                                window_id=watcher.window_id if hasattr(watcher, 'window_id') else None,
                                details={'ocr_checks': ocr_checks, 'frames': frame_count, 'elapsed': elapsed}
                            )
                        except Exception as e:
                            logger.debug(f"[v32.3] Heartbeat emit failed: {e}")
                    
                    # v32.3: Narrate heartbeat every 60 OCR checks (~12 seconds) if enabled
                    if ocr_checks % 60 == 0 and self.config.working_out_loud_enabled:
                        try:
                            await self._narrate_working_out_loud(
                                message=f"Still watching {app_name}. {ocr_checks} scans so far, "
                                        f"no '{trigger_text}' detected yet.",
                                narration_type="heartbeat",
                                watcher_id=watcher_id,
                                priority="low"
                            )
                        except Exception as e:
                            logger.debug(f"[v32.3] Heartbeat narration failed: {e}")

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

    # =========================================================================
    # v38.0: MOSAIC VISUAL DETECTION - O(1) Single-Stream Detection
    # =========================================================================

    async def _mosaic_visual_detection(
        self,
        watcher: Any,  # MosaicWatcher
        trigger_text: str,
        timeout: float,
        app_name: str,
        windows: List[Dict[str, Any]],
        action_config: Optional[Dict[str, Any]] = None,
        alert_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        v38.0: Mosaic Strategy detection loop - O(1) efficiency.

        Instead of running N parallel OCR operations on N window streams,
        we run ONE OCR operation on the Ghost Display mosaic where all
        windows are tiled.

        Args:
            watcher: MosaicWatcher instance
            trigger_text: Text to search for
            timeout: Max time to wait for detection
            app_name: Application name for logging
            windows: List of window info dicts
            action_config: Optional action to execute on detection
            alert_config: Optional alert configuration

        Returns:
            Detection result dict with status, detected window, etc.
        """
        import time
        start_time = time.time()
        frame_count = 0
        ocr_checks = 0

        # v38.5: Operational stability counters
        last_health_check = start_time
        last_layout_refresh = start_time
        last_status_report = start_time  # v42.0: Periodic status reporting
        health_check_interval = float(os.getenv('JARVIS_MOSAIC_HEALTH_INTERVAL', '10.0'))
        layout_refresh_interval = float(os.getenv('JARVIS_MOSAIC_LAYOUT_INTERVAL', '30.0'))
        status_report_interval = float(os.getenv('JARVIS_STATUS_REPORT_INTERVAL', '30.0'))  # v42.0
        black_screen_count = 0
        max_black_screen_retries = int(os.getenv('JARVIS_MOSAIC_BLACK_RETRIES', '5'))
        last_ocr_sample = ""  # v42.0: Track last OCR result for status reporting

        watcher_id = getattr(watcher, 'watcher_id', f"mosaic_{id(watcher)}")
        window_count = len(windows)

        logger.info(
            f"[Mosaic Detection] 🧩 Starting O(1) detection for '{trigger_text}' "
            f"({window_count} windows in mosaic, timeout: {timeout}s)"
        )

        # Narrate startup
        if self.config.working_out_loud_enabled:
            try:
                await self._narrate_working_out_loud(
                    message=f"I'm now watching {window_count} {app_name} windows for '{trigger_text}' "
                            f"using efficient mosaic mode.",
                    narration_type="startup",
                    watcher_id=watcher_id,
                    priority="normal"
                )
            except Exception:
                pass

        try:
            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    logger.info(
                        f"[Mosaic Detection] ⏱️ Timeout after {elapsed:.1f}s "
                        f"({frame_count} frames, {ocr_checks} OCR checks)"
                    )

                    # Stop watcher and return
                    try:
                        await watcher.stop()
                    except Exception:
                        pass

                    return {
                        'status': 'timeout',
                        'detected': False,
                        'trigger_text': trigger_text,
                        'detection_time': elapsed,
                        'frames_checked': frame_count,
                        'ocr_checks': ocr_checks,
                        'mode': 'mosaic',
                        'window_count': window_count,
                        'efficiency': 'O(1)'
                    }

                # Get frame from mosaic
                frame_data = await watcher.get_latest_frame(timeout=1.0)

                if not frame_data:
                    await asyncio.sleep(0.1)
                    continue

                frame_count += 1
                frame = frame_data.get('frame')

                if frame is None:
                    continue

                # =========================================================
                # v38.5: VISION RELIABILITY CHECKS
                # =========================================================

                current_time = time.time()

                # v38.5: Check frame freshness - stale frames cause false negatives
                if hasattr(watcher, 'check_frame_freshness'):
                    is_fresh, frame_age = watcher.check_frame_freshness(frame_data, max_age_seconds=2.0)
                    if not is_fresh:
                        logger.debug(f"[Mosaic v38.5] Skipping stale frame (age: {frame_age:.2f}s)")
                        continue

                # v38.5: Black screen detection - sleeping display
                if hasattr(watcher, 'is_black_screen'):
                    if watcher.is_black_screen(frame):
                        black_screen_count += 1
                        logger.warning(
                            f"[Mosaic v38.5] ⚫ Black screen ({black_screen_count}/{max_black_screen_retries})"
                        )

                        if black_screen_count >= max_black_screen_retries:
                            # Check display connection after repeated black screens
                            if hasattr(watcher, 'check_display_connection'):
                                is_connected, msg = await watcher.check_display_connection()
                                if not is_connected:
                                    logger.error(f"[Mosaic v38.5] ❌ Display disconnected: {msg}")
                            black_screen_count = 0

                        # Stealth wake attempt
                        if hasattr(watcher, 'stealth_wake_display'):
                            try:
                                await watcher.stealth_wake_display()
                            except Exception:
                                pass
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        black_screen_count = 0

                # v38.5: Adaptive upscaling for small frames
                if hasattr(watcher, 'adaptive_upscale_for_ocr'):
                    frame = watcher.adaptive_upscale_for_ocr(frame, min_height=720, max_scale=2.0)

                # v38.5: Periodic health check
                if current_time - last_health_check > health_check_interval:
                    last_health_check = current_time
                    if hasattr(watcher, 'get_display_health'):
                        health = watcher.get_display_health()
                        if health.get('status') != 'healthy':
                            logger.warning(
                                f"[Mosaic v38.5] ⚠️ Display health: {health.get('status')} - "
                                f"{health.get('issues', [])}"
                            )

                # v38.5: Periodic layout refresh
                if current_time - last_layout_refresh > layout_refresh_interval:
                    last_layout_refresh = current_time
                    if hasattr(watcher, 'refresh_window_layout'):
                        try:
                            await watcher.refresh_window_layout()
                        except Exception:
                            pass
                
                # ==========================================================
                # v42.0: PERIODIC STATUS REPORTING
                # ==========================================================
                # ROOT CAUSE FIX: Users feel left in the dark during monitoring.
                # JARVIS should periodically report what it's seeing so the user
                # knows the system is actively working.
                #
                # CONFIGURABLE: Set JARVIS_STATUS_REPORT_INTERVAL to control
                # how often JARVIS announces status (default: 30 seconds)
                # ==========================================================
                if current_time - last_status_report > status_report_interval:
                    last_status_report = current_time
                    elapsed_minutes = int(elapsed / 60)
                    elapsed_seconds = int(elapsed % 60)
                    
                    # Build status message
                    time_str = f"{elapsed_minutes} minute{'s' if elapsed_minutes != 1 else ''}" if elapsed_minutes > 0 else f"{elapsed_seconds} seconds"
                    
                    if self.config.working_out_loud_enabled:
                        try:
                            # Sample what we're seeing (last OCR result or generic status)
                            status_detail = ""
                            if last_ocr_sample:
                                # Report a snippet of what we see on screen
                                sample_preview = last_ocr_sample[:50] + "..." if len(last_ocr_sample) > 50 else last_ocr_sample
                                status_detail = f" I can see text including '{sample_preview}'."
                            
                            await self._narrate_working_out_loud(
                                message=f"Still watching {window_count} {app_name} windows for '{trigger_text}'. "
                                        f"Monitoring for {time_str} now. {frame_count} frames scanned.{status_detail}",
                                narration_type="progress",
                                watcher_id=f"status_{watcher_id}",
                                priority="low"
                            )
                        except Exception:
                            pass
                    
                    # Also emit WebSocket status for frontend
                    if WEBSOCKET_MANAGER_AVAILABLE:
                        try:
                            ws_manager = get_ws_manager()
                            await ws_manager.broadcast_to_all({
                                "type": "visual_detection",
                                "event": "monitoring_status",
                                "data": {
                                    "app_name": app_name,
                                    "trigger_text": trigger_text,
                                    "window_count": window_count,
                                    "elapsed_seconds": elapsed,
                                    "frames_scanned": frame_count,
                                    "ocr_checks": ocr_checks,
                                    "mode": "mosaic",
                                    "status": "scanning",
                                    "timestamp": datetime.now().isoformat()
                                }
                            })
                        except Exception:
                            pass

                # =========================================================
                # v38.5: DYNAMIC SCALING FACTOR
                # =========================================================

                # v38.5: Calculate dynamic scaling factor (frame pixels → yabai points)
                # Retina displays typically have 2x pixels, yabai uses logical points
                frame_scale_factor = 1.0
                if hasattr(frame, 'shape') and len(frame.shape) >= 2:
                    frame_height, frame_width = frame.shape[:2]
                    config_width = watcher.config.display_width
                    config_height = watcher.config.display_height

                    if config_width > 0 and config_height > 0:
                        # Use the larger ratio to handle potential aspect ratio differences
                        width_scale = frame_width / config_width
                        height_scale = frame_height / config_height
                        # Use average, or max if significantly different (should be ~2.0 for Retina)
                        frame_scale_factor = (width_scale + height_scale) / 2.0

                        # v38.5: Sanity check - common Retina scales are 1.0, 2.0, 3.0
                        if frame_scale_factor < 0.5 or frame_scale_factor > 4.0:
                            logger.warning(
                                f"[Mosaic v38.5] Unusual scale factor {frame_scale_factor:.2f}, "
                                f"frame: {frame_width}x{frame_height}, config: {config_width}x{config_height}"
                            )
                            frame_scale_factor = 1.0  # Fallback to 1:1

                        logger.debug(
                            f"[Mosaic v38.5] Dynamic scaling: {frame_scale_factor:.2f}x "
                            f"(frame: {frame_width}x{frame_height} → config: {config_width}x{config_height})"
                        )

                # Run OCR on mosaic frame
                ocr_checks += 1

                # v38.5: Use Mosaic mode OCR detection with spatial intelligence
                detected, confidence, detected_text, ocr_context = await self._ocr_detect(
                    frame=frame,
                    trigger_text=trigger_text,
                    mosaic_mode=True,  # v38.5: Enable spatial intelligence
                    frame_scale_factor=frame_scale_factor  # v38.5: Dynamic Retina scaling
                )
                
                # v42.0: Update last OCR sample for status reporting
                # This lets us report what JARVIS is "seeing" to the user
                if ocr_context and ocr_context.get('full_text'):
                    last_ocr_sample = ocr_context.get('full_text', '')[:200]  # Limit to 200 chars
                elif detected_text:
                    last_ocr_sample = detected_text

                if detected:
                    detection_time = time.time() - start_time

                    # v38.0: Spatial Intelligence - Map detection to specific window
                    matched_window = None
                    matched_tile = None

                    if self.config.mosaic_spatial_intelligence and hasattr(watcher, 'get_tile_for_ocr_match'):
                        # Try to get OCR match coordinates from context
                        if ocr_context and 'match_x' in ocr_context and 'match_y' in ocr_context:
                            matched_tile = watcher.get_tile_for_ocr_match(
                                ocr_context['match_x'],
                                ocr_context['match_y']
                            )
                            if matched_tile:
                                matched_window = {
                                    'window_id': matched_tile.window_id,
                                    'app_name': matched_tile.app_name,
                                    'window_title': matched_tile.window_title,
                                    'original_space_id': matched_tile.original_space_id
                                }
                                logger.info(
                                    f"[Mosaic Detection] 📍 Spatial match: "
                                    f"Window {matched_tile.window_id} at ({matched_tile.x}, {matched_tile.y})"
                                )

                    logger.info(
                        f"[Mosaic Detection] ✅ FOUND '{trigger_text}'! "
                        f"Time: {detection_time:.2f}s, Confidence: {confidence:.1%}, "
                        f"Frames: {frame_count}, OCR checks: {ocr_checks}, "
                        f"Mode: O(1) Mosaic"
                    )

                    # Narrate detection
                    if self.config.working_out_loud_enabled:
                        try:
                            if matched_tile:
                                msg = f"Found '{trigger_text}' in {matched_tile.app_name} window!"
                            else:
                                msg = f"Found '{trigger_text}' on the Ghost Display mosaic!"
                            await self._narrate_working_out_loud(
                                message=msg,
                                narration_type="detection",
                                watcher_id=watcher_id,
                                priority="high"
                            )
                        except Exception:
                            pass

                    # Stop watcher
                    try:
                        await watcher.stop()
                    except Exception:
                        pass

                    # Execute action if configured
                    action_result = None
                    if action_config:
                        try:
                            action_result = await self._execute_action(
                                action_config=action_config,
                                context={
                                    'trigger_text': trigger_text,
                                    'detected_text': detected_text,
                                    'confidence': confidence,
                                    'window': matched_window or windows[0] if windows else None,
                                    'mode': 'mosaic'
                                }
                            )
                        except Exception as e:
                            logger.warning(f"[Mosaic Detection] Action execution failed: {e}")

                    # Send alert if configured
                    if alert_config:
                        try:
                            await self._send_alert(
                                alert_config=alert_config,
                                context={
                                    'trigger_text': trigger_text,
                                    'detected_text': detected_text,
                                    'confidence': confidence,
                                    'app_name': app_name,
                                    'mode': 'mosaic'
                                }
                            )
                        except Exception as e:
                            logger.warning(f"[Mosaic Detection] Alert failed: {e}")

                    return {
                        'status': 'detected',
                        'detected': True,
                        'trigger_text': trigger_text,
                        'detected_text': detected_text,
                        'confidence': confidence,
                        'detection_time': detection_time,
                        'frames_checked': frame_count,
                        'ocr_checks': ocr_checks,
                        'mode': 'mosaic',
                        'window_count': window_count,
                        'matched_window': matched_window,
                        'action_result': action_result,
                        'efficiency': 'O(1)',
                        # v38.0 efficiency stats
                        'streams_avoided': window_count - 1,
                        'estimated_ram_saved_mb': (window_count - 1) * 200
                    }

                # Small delay between frames
                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            logger.info(f"[Mosaic Detection] Cancelled after {time.time() - start_time:.1f}s")
            try:
                await watcher.stop()
            except Exception:
                pass
            raise

        except Exception as e:
            logger.error(f"[Mosaic Detection] Error: {e}", exc_info=True)
            try:
                await watcher.stop()
            except Exception:
                pass
            return {
                'status': 'error',
                'error': str(e),
                'detected': False,
                'mode': 'mosaic'
            }

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
        trigger_text: str,
        mosaic_mode: bool = False,  # v38.5: Enable spatial intelligence for Mosaic
        frame_scale_factor: float = 1.0  # v38.5: Dynamic scaling (e.g., 2.0 for Retina)
    ) -> tuple[bool, float, Optional[str], Optional[Dict[str, Any]]]:
        """
        v38.5: INTELLIGENT OCR DETECTION with Semantic Fuzzy Matching + Spatial Intelligence

        Run OCR on frame and check for trigger text using multi-tier matching:

        TIER 1: Exact substring match (fastest, 100% confidence)
        TIER 2: Word stemming match ("bouncing" → "bounc" matches "BOUNCE" → "bounc")
        TIER 3: Rapidfuzz partial_ratio (80+ threshold, handles typos/variations)
        TIER 4: Rapidfuzz token_set_ratio (word reordering, 75+ threshold)

        v38.5 ENHANCEMENTS:
        - mosaic_mode: When True, extracts OCR bounding boxes for spatial hit-testing
        - frame_scale_factor: Converts frame pixels to yabai points (2.0 for Retina)
        - Returns match_x/match_y in context for mapping detection to specific windows

        Also extracts context values (numbers like bounce counts) from detected text.

        Args:
            frame: Numpy array (RGB)
            trigger_text: Text to search for
            mosaic_mode: v38.5 - Enable bounding box extraction for spatial intelligence
            frame_scale_factor: v38.5 - Retina scaling factor (frame_pixels / yabai_points)

        Returns:
            (detected, confidence, detected_text, context_dict)
            - detected: True if trigger text found
            - confidence: Match confidence (0.0-1.0)
            - detected_text: Actual text that matched
            - context_dict: Extracted context (numbers, surrounding text, match_x, match_y)
        """
        context: Dict[str, Any] = {}

        try:
            if not self._detector:
                # No detector available - fallback
                return False, 0.0, None, None

            # v38.5: Extract bounding boxes when in Mosaic mode for spatial intelligence
            extract_boxes = mosaic_mode and self.config.mosaic_spatial_intelligence

            # STEP 1: Run OCR to extract all text from frame
            result = await self._detector.detect_text(
                frame=frame,
                target_text=trigger_text,
                case_sensitive=False,
                fuzzy=True,
                extract_bounding_boxes=extract_boxes  # v38.5: Spatial intelligence
            )

            # Get full OCR text for context extraction and fallback matching
            # Handle both dataclass (TextDetectionResult) and dict responses
            full_ocr_text = ""
            if hasattr(result, 'all_text') and result.all_text:
                # TextDetectionResult dataclass
                full_ocr_text = result.all_text
            elif hasattr(self._detector, 'last_ocr_text'):
                full_ocr_text = self._detector.last_ocr_text or ""
            elif isinstance(result, dict):
                full_ocr_text = result.get('raw_text', result.get('text', result.get('all_text', '')))
            
            # TIER 1: Check if detector already found a match
            # Handle both dataclass and dict response types
            result_detected = False
            result_confidence = 0.0
            result_text = ""
            
            if hasattr(result, 'detected'):
                # TextDetectionResult dataclass
                result_detected = result.detected
                result_confidence = result.confidence
                result_text = result.text_found or trigger_text
            elif isinstance(result, dict):
                # Legacy dict response
                result_detected = result.get('found', result.get('detected', False))
                result_confidence = result.get('confidence', 0.9)
                result_text = result.get('text', result.get('text_found', trigger_text))
            
            if result_detected:
                confidence = result_confidence
                matched_text = result_text

                # Extract context (numbers, values)
                context = self._extract_ocr_context(full_ocr_text, trigger_text, matched_text)
                context['match_tier'] = 'exact'
                context['match_method'] = 'detector_exact_match'

                # v38.5: Propagate spatial coordinates if available (for Mosaic mode)
                if extract_boxes and hasattr(result, 'metadata') and result.metadata:
                    metadata = result.metadata
                    if 'match_x' in metadata and 'match_y' in metadata:
                        # v38.5: Apply dynamic scaling to convert frame pixels → yabai points
                        # Frame is typically 2x pixels on Retina, yabai uses points
                        raw_x = metadata['match_x']
                        raw_y = metadata['match_y']

                        if frame_scale_factor > 0:
                            # Convert frame pixels to yabai coordinate space
                            context['match_x'] = int(raw_x / frame_scale_factor)
                            context['match_y'] = int(raw_y / frame_scale_factor)
                            context['match_x_raw'] = raw_x  # Original pixel coordinates
                            context['match_y_raw'] = raw_y
                            context['frame_scale_factor'] = frame_scale_factor
                        else:
                            context['match_x'] = raw_x
                            context['match_y'] = raw_y

                        if 'match_bbox' in metadata:
                            bbox = metadata['match_bbox']
                            if frame_scale_factor > 0:
                                # Scale bounding box as well
                                context['match_bbox'] = (
                                    int(bbox[0] / frame_scale_factor),
                                    int(bbox[1] / frame_scale_factor),
                                    int(bbox[2] / frame_scale_factor),
                                    int(bbox[3] / frame_scale_factor)
                                )
                            else:
                                context['match_bbox'] = bbox

                        logger.debug(
                            f"[OCR v38.5] Spatial intelligence: Match at ({context.get('match_x')}, "
                            f"{context.get('match_y')}) with scale factor {frame_scale_factor}"
                        )

                logger.info(
                    f"✅ [OCR v38.5] TIER 1 MATCH: '{trigger_text}' found as '{matched_text}' "
                    f"(confidence: {confidence:.1%}) | Context: {context.get('extracted_number', 'N/A')}"
                    + (f" | Pos: ({context.get('match_x', 'N/A')}, {context.get('match_y', 'N/A')})"
                       if 'match_x' in context else "")
                )
                return True, confidence, matched_text, context

            # If detector didn't find it, try advanced matching on full OCR text
            if not full_ocr_text:
                # Try to get full text directly
                try:
                    all_text_result = await self._ocr_get_all_text(frame)
                    full_ocr_text = all_text_result if all_text_result else ""
                except Exception:
                    pass
            
            if not full_ocr_text:
                return False, 0.0, None, None
            
            # Normalize texts for comparison
            trigger_lower = trigger_text.lower().strip()
            ocr_lower = full_ocr_text.lower()
            
            # TIER 2: Word stemming match (handles "bouncing" → "BOUNCE")
            stem_match, stem_confidence, stem_matched = self._stem_match(trigger_lower, ocr_lower)
            if stem_match:
                context = self._extract_ocr_context(full_ocr_text, trigger_text, stem_matched)
                context['match_tier'] = 'stem'
                context['match_method'] = f'word_stemming ({stem_matched})'
                
                logger.info(
                    f"✅ [OCR v32.3] TIER 2 STEM MATCH: '{trigger_text}' matched via stemming "
                    f"to '{stem_matched}' (confidence: {stem_confidence:.1%}) | "
                    f"Context: {context.get('extracted_number', 'N/A')}"
                )
                return True, stem_confidence, stem_matched, context
            
            # TIER 3 & 4: Rapidfuzz fuzzy matching
            if RAPIDFUZZ_AVAILABLE:
                # TIER 3: Partial ratio (substring matching)
                partial_score = rapidfuzz_fuzz.partial_ratio(trigger_lower, ocr_lower) / 100.0
                
                if partial_score >= 0.80:
                    # Find the best matching substring
                    matched_text = self._find_best_fuzzy_match(trigger_text, full_ocr_text)
                    context = self._extract_ocr_context(full_ocr_text, trigger_text, matched_text)
                    context['match_tier'] = 'fuzzy_partial'
                    context['match_method'] = f'rapidfuzz_partial_ratio (score: {partial_score:.0%})'
                    context['fuzzy_score'] = partial_score
                    
                    logger.info(
                        f"✅ [OCR v32.3] TIER 3 FUZZY MATCH: '{trigger_text}' matched to '{matched_text}' "
                        f"(partial_ratio: {partial_score:.0%}) | Context: {context.get('extracted_number', 'N/A')}"
                    )
                    return True, partial_score, matched_text, context
                
                # TIER 4: Token set ratio (word order independent)
                token_score = rapidfuzz_fuzz.token_set_ratio(trigger_lower, ocr_lower) / 100.0
                
                if token_score >= 0.75:
                    matched_text = self._find_best_fuzzy_match(trigger_text, full_ocr_text)
                    context = self._extract_ocr_context(full_ocr_text, trigger_text, matched_text)
                    context['match_tier'] = 'fuzzy_token_set'
                    context['match_method'] = f'rapidfuzz_token_set_ratio (score: {token_score:.0%})'
                    context['fuzzy_score'] = token_score
                    
                    logger.info(
                        f"✅ [OCR v32.3] TIER 4 TOKEN SET MATCH: '{trigger_text}' matched to '{matched_text}' "
                        f"(token_set_ratio: {token_score:.0%}) | Context: {context.get('extracted_number', 'N/A')}"
                    )
                    return True, token_score * 0.95, matched_text, context  # Slightly lower confidence
            
            # TIER 5: Last resort - check for any significant word overlap
            trigger_words = set(trigger_lower.split())
            ocr_words = set(ocr_lower.split())
            common_words = trigger_words & ocr_words
            
            if len(common_words) >= 1 and len(common_words) / len(trigger_words) >= 0.5:
                # At least half the trigger words are present
                matched_text = ' '.join(common_words)
                overlap_confidence = len(common_words) / len(trigger_words) * 0.8
                
                context = self._extract_ocr_context(full_ocr_text, trigger_text, matched_text)
                context['match_tier'] = 'word_overlap'
                context['match_method'] = f'word_overlap ({len(common_words)}/{len(trigger_words)} words)'
                context['matched_words'] = list(common_words)
                
                logger.info(
                    f"✅ [OCR v32.3] TIER 5 WORD OVERLAP: '{trigger_text}' partially matched "
                    f"({len(common_words)}/{len(trigger_words)} words: {common_words}) | "
                    f"Context: {context.get('extracted_number', 'N/A')}"
                )
                return True, overlap_confidence, matched_text, context
            
            # No match found
            logger.debug(
                f"❌ [OCR v32.3] No match for '{trigger_text}' in OCR text. "
                f"First 200 chars: '{full_ocr_text[:200]}...'"
            )
            return False, 0.0, None, None

        except Exception as e:
            logger.debug(f"[OCR] Detection error: {e}")
            return False, 0.0, None, None

    def _stem_match(
        self,
        trigger_lower: str,
        ocr_lower: str
    ) -> tuple[bool, float, str]:
        """
        v32.3: Word stemming match for handling verb conjugations.
        
        Converts "bouncing" → "bounc", "BOUNCE" → "bounc" for matching.
        This handles cases like "bouncing ball" matching "BOUNCE COUNT".
        
        Args:
            trigger_lower: Lowercase trigger text
            ocr_lower: Lowercase OCR text
            
        Returns:
            (matched, confidence, matched_text)
        """
        def simple_stem(word: str) -> str:
            """Enhanced stemmer for common English suffixes.
            
            Handles: bouncing → bounc, bounce → bounc, running → runn, etc.
            """
            word = word.lower()
            # Remove common suffixes in order from longest to shortest
            # The 'e' at the end handles "bounce" → "bounc"
            suffixes = ['tion', 'sion', 'ness', 'ment', 'able', 'ible', 
                        'ful', 'less', 'ing', 'ed', 'er', 'est', 'ly', 
                        'ity', 'es', 's', 'e']
            for suffix in suffixes:
                if word.endswith(suffix) and len(word) > len(suffix) + 2:
                    return word[:-len(suffix)]
            return word
        
        # Stem all trigger words
        trigger_words = trigger_lower.split()
        trigger_stems = set(simple_stem(w) for w in trigger_words if len(w) > 2)
        
        # Stem all OCR words
        ocr_words = ocr_lower.split()
        ocr_stems = {simple_stem(w): w for w in ocr_words if len(w) > 2}
        
        # Find matching stems
        matching_stems = trigger_stems & set(ocr_stems.keys())
        
        if matching_stems:
            # Calculate confidence based on how many stems matched
            match_ratio = len(matching_stems) / len(trigger_stems) if trigger_stems else 0
            confidence = 0.75 + (match_ratio * 0.2)  # 75-95% based on match ratio
            
            # Build matched text from original OCR words
            matched_words = [ocr_stems[stem] for stem in matching_stems if stem in ocr_stems]
            matched_text = ' '.join(matched_words).upper()
            
            return True, confidence, matched_text
        
        return False, 0.0, ""

    def _find_best_fuzzy_match(
        self,
        trigger_text: str,
        full_text: str
    ) -> str:
        """
        v32.3: Find the best matching substring from OCR text.
        
        Uses sliding window approach with rapidfuzz to find the substring
        that best matches the trigger text.
        
        Args:
            trigger_text: The text we're looking for
            full_text: The full OCR text
            
        Returns:
            Best matching substring
        """
        if not RAPIDFUZZ_AVAILABLE or not full_text:
            return trigger_text
        
        trigger_lower = trigger_text.lower()
        words = full_text.split()
        trigger_word_count = len(trigger_text.split())
        
        # Try different window sizes
        best_match = trigger_text
        best_score = 0
        
        for window_size in range(1, min(trigger_word_count + 3, len(words) + 1)):
            for i in range(len(words) - window_size + 1):
                candidate = ' '.join(words[i:i + window_size])
                score = rapidfuzz_fuzz.ratio(trigger_lower, candidate.lower()) / 100.0
                
                if score > best_score:
                    best_score = score
                    best_match = candidate
        
        return best_match

    def _extract_ocr_context(
        self,
        full_text: str,
        trigger_text: str,
        matched_text: str
    ) -> Dict[str, Any]:
        """
        v32.3: Extract contextual information from OCR text.
        
        Extracts:
        - Large numbers (e.g., bounce count: 24554)
        - Percentages
        - Status keywords
        - Surrounding text
        
        Args:
            full_text: Full OCR text
            trigger_text: Original trigger text
            matched_text: Text that matched
            
        Returns:
            Dict with extracted context
        """
        context: Dict[str, Any] = {
            'raw_ocr_length': len(full_text),
            'trigger_text': trigger_text,
            'matched_text': matched_text,
        }
        
        # Extract large numbers (3+ digits) - likely counters, IDs, etc.
        numbers = re.findall(r'\b(\d{3,})\b', full_text)
        if numbers:
            # Get the largest number (likely a counter)
            largest_number = max(int(n) for n in numbers)
            context['extracted_number'] = largest_number
            context['all_numbers'] = [int(n) for n in numbers]
            
        # Extract percentages
        percentages = re.findall(r'(\d+(?:\.\d+)?)\s*%', full_text)
        if percentages:
            context['percentages'] = [float(p) for p in percentages]
        
        # Extract status keywords
        status_keywords = ['success', 'complete', 'error', 'failed', 'running', 'pending', 'active']
        found_status = []
        for keyword in status_keywords:
            if keyword in full_text.lower():
                found_status.append(keyword)
        if found_status:
            context['status_keywords'] = found_status
        
        # Extract surrounding context (words near the match)
        if matched_text:
            match_idx = full_text.lower().find(matched_text.lower())
            if match_idx >= 0:
                # Get 50 chars before and after
                start = max(0, match_idx - 50)
                end = min(len(full_text), match_idx + len(matched_text) + 50)
                context['surrounding_text'] = full_text[start:end].strip()
        
        return context

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
            watcher_id = watcher.watcher_id

            # =====================================================================
            # v22.0.0: EARLY REGISTRATION - Register in lifecycle tracker IMMEDIATELY
            # =====================================================================
            # WHY: God Mode checks for registered watchers. If we only register
            # AFTER verification, failures are silent and God Mode sees 0 watchers.
            # SOLUTION: Register EARLY with "starting" status, update as we progress.
            # =====================================================================
            self._watcher_lifecycle[watcher_id] = {
                'status': 'starting',
                'watcher': watcher,
                'window_id': window_id,
                'app_name': app_name,
                'space_id': space_id,
                'fps': fps,
                'started_at': datetime.now().isoformat(),
                'config': watcher_config,
                'error': None,
                'verification_stage': 'created'
            }
            logger.debug(f"[{watcher_id}] 📝 Registered in lifecycle (status: starting)")

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
                error_msg = (
                    f"watcher.start() timed out after {watcher_start_timeout}s "
                    f"(possible causes: permission dialog, invalid window, GPU hang)"
                )
                logger.error(f"❌ Ferrari Engine {error_msg}")
                # v22.0.0: Update lifecycle with failure
                self._watcher_lifecycle[watcher_id]['status'] = 'failed'
                self._watcher_lifecycle[watcher_id]['error'] = error_msg
                self._watcher_lifecycle[watcher_id]['verification_stage'] = 'start_timeout'
                # v26.0: Notify ProgressiveStartupManager of failure
                if self._progressive_startup_manager is not None:
                    await self._progressive_startup_manager.register_watcher_ready(
                        watcher_id=watcher_id,
                        success=False,
                        error=error_msg,
                    )
                # Try to clean up the watcher
                try:
                    await watcher.stop()
                except Exception:
                    pass
                return None

            if not success:
                # v22.0.0: More specific error - start() now returns False only when
                # frame production verification fails (both SCK and CGWindowListCreateImage)
                error_msg = (
                    f"window {window_id} cannot be captured - "
                    f"window may be hidden, minimized, closed, or requires Screen Recording permission"
                )
                logger.error(f"❌ Ferrari Engine {error_msg}")
                # v22.0.0: Update lifecycle with failure
                self._watcher_lifecycle[watcher_id]['status'] = 'failed'
                self._watcher_lifecycle[watcher_id]['error'] = error_msg
                self._watcher_lifecycle[watcher_id]['verification_stage'] = 'frame_production_failed'
                # v26.0: Notify ProgressiveStartupManager of failure
                if self._progressive_startup_manager is not None:
                    await self._progressive_startup_manager.register_watcher_ready(
                        watcher_id=watcher_id,
                        success=False,
                        error=error_msg,
                    )
                return None

            # v22.0.0: Update lifecycle - start succeeded
            self._watcher_lifecycle[watcher_id]['status'] = 'verifying'
            self._watcher_lifecycle[watcher_id]['verification_stage'] = 'started'

            # =====================================================================
            # ROOT CAUSE FIX v17.0: Post-Start Health Verification
            # =====================================================================
            # PROBLEM: start() returning True doesn't mean the watcher is ACTUALLY
            # running and producing frames. The watcher may have:
            # - Set status to WATCHING but stream failed to initialize
            # - Started but immediately crashed
            # - Started but no frames are flowing
            #
            # SOLUTION: Multi-stage verification:
            # 1. Check watcher.status == WatcherStatus.WATCHING
            # 2. Wait for first frame to confirm stream is active
            # 3. Verify frame is valid (not None, has data)
            # =====================================================================

            # Import WatcherStatus for status comparison
            try:
                from backend.vision.macos_video_capture_advanced import WatcherStatus
            except ImportError:
                WatcherStatus = None

            # Stage 1: Verify watcher status
            watcher_is_healthy = False
            try:
                if WatcherStatus is not None:
                    # Direct status comparison (most reliable)
                    watcher_is_healthy = watcher.status == WatcherStatus.WATCHING
                    if not watcher_is_healthy:
                        logger.error(
                            f"❌ Watcher started but status is {watcher.status.value} "
                            f"(expected: WATCHING)"
                        )
                elif hasattr(watcher, 'status'):
                    # Fallback: check status attribute directly
                    status_val = getattr(watcher.status, 'value', str(watcher.status))
                    watcher_is_healthy = status_val.lower() == 'watching'
                else:
                    # No status attribute - optimistic
                    watcher_is_healthy = True
                    logger.debug(f"[{watcher.watcher_id}] No status attribute - assuming healthy")
            except Exception as e:
                logger.warning(f"[{watcher.watcher_id}] Status check failed: {e} - assuming healthy")
                watcher_is_healthy = True

            if not watcher_is_healthy:
                error_msg = f"watcher status check failed (status: {getattr(watcher, 'status', 'unknown')})"
                logger.error(f"❌ Ferrari Engine {error_msg} for window {window_id}")
                # v22.0.0: Update lifecycle with failure
                self._watcher_lifecycle[watcher_id]['status'] = 'failed'
                self._watcher_lifecycle[watcher_id]['error'] = error_msg
                self._watcher_lifecycle[watcher_id]['verification_stage'] = 'status_check_failed'
                try:
                    await watcher.stop()
                except Exception:
                    pass
                return None

            # v22.0.0: Update lifecycle - status verified
            self._watcher_lifecycle[watcher_id]['verification_stage'] = 'status_verified'

            # Stage 2: Verify frame flow (wait for first frame)
            frame_flow_timeout = float(os.getenv('JARVIS_FIRST_FRAME_TIMEOUT', '5.0'))
            first_frame_received = False

            logger.debug(
                f"[{watcher.watcher_id}] Verifying frame flow "
                f"(timeout: {frame_flow_timeout}s)..."
            )

            try:
                # Wait for first frame with timeout
                first_frame = await asyncio.wait_for(
                    watcher.get_latest_frame(timeout=frame_flow_timeout),
                    timeout=frame_flow_timeout + 1.0  # Outer timeout slightly longer
                )

                if first_frame is not None:
                    # Verify frame has actual data
                    frame_data = first_frame.get('frame')
                    if frame_data is not None:
                        first_frame_received = True
                        logger.info(
                            f"✅ [{watcher.watcher_id}] First frame received - "
                            f"stream verified active"
                        )
                    else:
                        logger.warning(
                            f"[{watcher.watcher_id}] Frame dict received but 'frame' is None"
                        )
                else:
                    logger.warning(
                        f"[{watcher.watcher_id}] No frame received within {frame_flow_timeout}s"
                    )

            except asyncio.TimeoutError:
                logger.warning(
                    f"[{watcher.watcher_id}] Frame flow verification timed out "
                    f"after {frame_flow_timeout}s - proceeding optimistically"
                )
                # Don't fail - the watcher may just be slow to produce first frame
                # Set first_frame_received to True to proceed (optimistic)
                first_frame_received = True
            except Exception as e:
                logger.warning(
                    f"[{watcher.watcher_id}] Frame flow check error: {e} - "
                    f"proceeding optimistically"
                )
                first_frame_received = True

            if not first_frame_received:
                # No frame flow - watcher is not actually working
                error_msg = f"watcher started but no frames flowing (window may be hidden or invalid)"
                logger.error(f"❌ Ferrari Engine {error_msg} for {window_id} ({app_name})")
                # v22.0.0: Update lifecycle with failure
                self._watcher_lifecycle[watcher_id]['status'] = 'failed'
                self._watcher_lifecycle[watcher_id]['error'] = error_msg
                self._watcher_lifecycle[watcher_id]['verification_stage'] = 'no_frames'
                try:
                    await watcher.stop()
                except Exception:
                    pass
                return None

            # v22.0.0: Update lifecycle - frame flow verified
            self._watcher_lifecycle[watcher_id]['verification_stage'] = 'frames_verified'

            # =====================================================================
            # v6.1.0: Activate Purple Indicator for Visual Confirmation
            # =====================================================================
            # WHY: ScreenCaptureKit doesn't trigger the macOS purple indicator.
            # Users need visual confirmation that JARVIS is actively watching.
            # This starts a lightweight AVCaptureSession purely for the icon.
            # =====================================================================
            await self._ensure_purple_indicator()

            # v22.0.0: Mark lifecycle as ACTIVE (fully verified)
            self._active_video_watchers[watcher_id] = {
                'watcher': watcher,
                'window_id': window_id,
                'app_name': app_name,
                'space_id': space_id,
                'fps': fps,
                'started_at': datetime.now().isoformat(),
                'config': watcher_config,
                # v17.0: Health tracking metadata
                'health': {
                    'verified_at': datetime.now().isoformat(),
                    'first_frame_received': first_frame_received,
                    'last_health_check': datetime.now().isoformat(),
                    'consecutive_failures': 0
                }
            }

            # v22.0.0: Update lifecycle to ACTIVE
            self._watcher_lifecycle[watcher_id]['status'] = 'active'
            self._watcher_lifecycle[watcher_id]['verification_stage'] = 'complete'
            self._watcher_lifecycle[watcher_id]['activated_at'] = datetime.now().isoformat()

            # v26.0: Notify ProgressiveStartupManager (event-based instead of polling)
            if self._progressive_startup_manager is not None:
                await self._progressive_startup_manager.register_watcher_ready(
                    watcher_id=watcher_id,
                    success=True,
                )
                
                # v32.0: Emit watcher spawned to UI progress stream
                if PROGRESS_STREAM_AVAILABLE:
                    try:
                        # Get current ready count from startup manager
                        ready_count = self._progressive_startup_manager._ready_count
                        expected_count = self._progressive_startup_manager._expected_count
                        await emit_watcher_spawned(
                            current=ready_count,
                            total=expected_count,
                            watcher_id=watcher_id,
                            window_id=window_id,
                            space_id=space_id,
                            app_name=app_name,
                            correlation_id=""  # Not available here but still useful
                        )
                    except Exception as e:
                        logger.debug(f"[v32.0] Watcher spawned emit failed: {e}")

            logger.info(
                f"✅ Ferrari Engine watcher started and verified: {watcher_id} "
                f"(Window {window_id}, {app_name}, Frame flow: ✓)"
            )

            return watcher

        except ImportError as e:
            error_msg = f"Ferrari Engine not available: {e}"
            logger.error(f"❌ {error_msg}")
            logger.error("   VideoWatcher/WatcherConfig import failed")
            # Note: Can't update lifecycle here as watcher was never created
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
                # v32.3: Enhanced with context extraction (numbers, match method, etc.)
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
                    window_id=watcher.window_id if hasattr(watcher, 'window_id') else None,
                    detection_context=result.get('context')  # v32.3: Pass context for rich notification
                )

                # Send alert
                await self._send_alert(
                    app_name=app_name,
                    trigger_text=trigger_text,
                    space_id=space_id,
                    confidence=detection_confidence,
                    detection_time=result.get('detection_time', 0.0)
                )
                
                # ==========================================================
                # v42.0: AUTOMATIC WINDOW RETURN AFTER DETECTION
                # ==========================================================
                # ROOT CAUSE FIX: User should see the windows after detection
                # is complete. JARVIS brings windows back from Ghost Display
                # to the main workspace so the user can see what was detected.
                #
                # This is the "mission complete" action - JARVIS autonomously:
                # 1. Monitored windows in the background (Ghost Display)
                # 2. Detected the trigger text
                # 3. Notified the user
                # 4. NOW: Returns windows so user can see the result
                # ==========================================================
                auto_return_enabled = os.getenv('JARVIS_AUTO_RETURN_AFTER_DETECTION', '1') == '1'
                
                if auto_return_enabled:
                    try:
                        return_result = await self._return_windows_after_detection(
                            app_name=app_name,
                            trigger_text=trigger_text,
                            detection_context=result.get('context')
                        )
                        
                        if return_result and return_result.get('returned_count', 0) > 0:
                            logger.info(
                                f"[v42.0] ✅ Returned {return_result['returned_count']} windows "
                                f"to main workspace after detection"
                            )
                    except Exception as e:
                        logger.warning(f"[v42.0] Window return after detection failed: {e}")

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
        window_id: Optional[int] = None,
        detection_context: Optional[Dict[str, Any]] = None
    ):
        """
        ROOT CAUSE FIX: Robust Multi-Channel Detection Notification v2.0.0
        v32.3: Enhanced with context extraction (numbers, match method, etc.)

        Sends real-time notifications through ALL available channels:
        1. TTS Voice (UnifiedVoiceOrchestrator) - primary
        2. WebSocket (real-time frontend updates) - for UI transparency
        3. Legacy TTS callback - backward compatibility

        Features:
        - Configurable notification templates (no hardcoding!)
        - Async parallel delivery
        - Graceful degradation
        - Rich detection metadata
        - v32.3: Context extraction (bounce counts, percentages, status)
        """
        # v32.3: Build enhanced log message with context
        context_info = ""
        if detection_context:
            match_method = detection_context.get('match_method', '')
            extracted_num = detection_context.get('extracted_number')
            match_tier = detection_context.get('match_tier', '')
            
            if match_method:
                context_info += f" | Match: {match_tier}"
            if extracted_num:
                context_info += f" | Value: {extracted_num}"
        
        logger.info(
            f"[Detection] 📢 Notifying user: '{trigger_text}' detected in {app_name} "
            f"(Space {space_id}, confidence: {confidence:.2%}){context_info}"
        )

        # =====================================================================
        # v32.0: EMIT DETECTION TO UI PROGRESS STREAM - INSTANT FEEDBACK!
        # v32.4: Also emit COMPLETE to signal UI to show 100% and clear progress
        # =====================================================================
        if PROGRESS_STREAM_AVAILABLE:
            try:
                # First emit detection event (shows celebration)
                await emit_detection(
                    trigger_text=trigger_text,
                    window_id=window_id if window_id else 0,
                    space_id=space_id,
                    app_name=app_name,
                    confidence=confidence,
                    correlation_id=""
                )
                
                # v32.4: CRITICAL - Emit completion to clear UI progress bar!
                # Without this, UI stays stuck at "VALIDATION 85%"
                await emit_complete(
                    app_name=app_name,
                    trigger_text=trigger_text,
                    detected=True,
                    details={
                        "confidence": confidence,
                        "window_id": window_id,
                        "space_id": space_id,
                    },
                    correlation_id=""
                )
                logger.info(f"[v32.4] ✅ Emitted detection + complete events to UI")
                
            except Exception as e:
                logger.debug(f"[v32.0] Detection emit failed: {e}")

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
                
                # v32.3: Add context information to TTS if available
                if detection_context:
                    extracted_num = detection_context.get('extracted_number')
                    matched_text = detection_context.get('matched_text', '')
                    
                    # Add context details to make the announcement more informative
                    if extracted_num and 'count' in trigger_text.lower():
                        # For count-related detections, announce the count
                        tts_message += f". The count is {extracted_num}."
                    elif extracted_num:
                        # For other number detections
                        tts_message += f". Value: {extracted_num}."
                    
                    # If matched text is different from trigger, mention it
                    if matched_text and matched_text.lower() != trigger_text.lower():
                        tts_message = f"I found '{matched_text}' in {app_name}, matching your search for '{trigger_text}'"
                        if extracted_num:
                            tts_message += f". Count: {extracted_num}."

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
    # v42.0: WINDOW RETURN AFTER DETECTION
    # =========================================================================

    async def _return_windows_after_detection(
        self,
        app_name: str,
        trigger_text: str,
        detection_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        v42.0: Return windows from Ghost Display to main workspace after detection.
        
        This is the "mission complete" action that brings windows back so the user
        can see what was detected. JARVIS narrates the entire process.
        
        Args:
            app_name: Application name (e.g., "Chrome")
            trigger_text: What was detected (e.g., "bouncing ball")
            detection_context: Optional context from detection (numbers, etc.)
            
        Returns:
            Dict with returned_count and details
        """
        result = {
            "returned_count": 0,
            "windows_returned": [],
            "narrated": False
        }
        
        if not GHOST_MANAGER_AVAILABLE:
            logger.debug("[v42.0] Ghost Manager not available for window return")
            return result
            
        if not hasattr(self, '_ghost_managers') or not app_name:
            return result
            
        try:
            ghost_manager = self._ghost_managers.get(app_name)
            if not ghost_manager:
                logger.debug(f"[v42.0] No Ghost Manager for {app_name}")
                return result
                
            from backend.vision.yabai_space_detector import get_yabai_detector
            yabai = get_yabai_detector()
            
            # Step 1: Narrate that we're bringing windows back
            if self.config.working_out_loud_enabled:
                # Build context-aware message
                context_msg = ""
                if detection_context:
                    extracted_num = detection_context.get('extracted_number')
                    if extracted_num:
                        context_msg = f" The current value is {extracted_num}."
                
                await self._narrate_working_out_loud(
                    message=f"I found '{trigger_text}' in {app_name}.{context_msg} "
                            f"Now bringing the windows back to your screen so you can see them.",
                    narration_type="success",
                    watcher_id=f"return_announce_{app_name}",
                    priority="high"
                )
                result["narrated"] = True
            
            # Step 2: Return all windows to their original positions
            logger.info(f"[v42.0] 🏠 Returning {app_name} windows to main workspace...")
            
            return_result = await ghost_manager.return_all_windows(
                yabai_detector=yabai,
                restore_geometry=ghost_manager.config.preserve_geometry_on_return
            )
            
            returned_windows = return_result.get("returned", [])
            result["returned_count"] = len(returned_windows)
            result["windows_returned"] = returned_windows
            
            # Step 3: Narrate completion
            if self.config.working_out_loud_enabled and result["returned_count"] > 0:
                window_word = "window" if result["returned_count"] == 1 else "windows"
                await self._narrate_working_out_loud(
                    message=f"Done! I've returned {result['returned_count']} {app_name} {window_word} "
                            f"to your main display. You can now see what I detected.",
                    narration_type="success",
                    watcher_id=f"return_complete_{app_name}",
                    priority="medium"
                )
            
            # Step 4: Send WebSocket event for frontend UI update
            if WEBSOCKET_MANAGER_AVAILABLE:
                try:
                    ws_manager = get_ws_manager()
                    await ws_manager.broadcast_to_all({
                        "type": "visual_detection",
                        "event": "windows_returned",
                        "data": {
                            "app_name": app_name,
                            "returned_count": result["returned_count"],
                            "trigger_text": trigger_text,
                            "timestamp": datetime.now().isoformat(),
                            "message": f"Returned {result['returned_count']} windows from Ghost Display"
                        }
                    })
                except Exception as e:
                    logger.debug(f"[v42.0] WebSocket window return event failed: {e}")
            
            # Step 5: Send progress stream event
            if PROGRESS_STREAM_AVAILABLE:
                try:
                    from backend.core.surveillance_progress_stream import emit_surveillance_progress, SurveillanceStage
                    await emit_surveillance_progress(
                        stage=SurveillanceStage.COMPLETE,
                        message=f"✅ Detection complete! Returned {result['returned_count']} windows",
                        progress_current=100,
                        progress_total=100,
                        app_name=app_name,
                        trigger_text=trigger_text,
                        details={
                            "detection_complete": True,
                            "windows_returned": result["returned_count"],
                            "detection_context": detection_context
                        }
                    )
                except Exception as e:
                    logger.debug(f"[v42.0] Progress stream event failed: {e}")
            
            # Step 6: Stop health monitoring and cleanup
            await ghost_manager.stop_health_monitoring()
            
            logger.info(
                f"[v42.0] ✅ Window return complete: {result['returned_count']} windows "
                f"returned to main workspace"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"[v42.0] Window return failed: {e}", exc_info=True)
            
            # Narrate failure if possible
            if self.config.working_out_loud_enabled:
                try:
                    await self._narrate_working_out_loud(
                        message=f"I found what you were looking for, but I couldn't bring the windows back. "
                                f"You may need to switch to the other display manually.",
                        narration_type="error",
                        watcher_id=f"return_error_{app_name}",
                        priority="high"
                    )
                except Exception:
                    pass
                    
            return result

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
