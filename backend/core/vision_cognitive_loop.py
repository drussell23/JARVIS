"""
Vision Cognitive Loop - The "Eyes" of JARVIS
==============================================

This module implements the Vision-First Agentic Loop that enables JARVIS
to "see" the OS state before acting, validate actions visually, and
coordinate across multiple macOS Spaces.

Architecture:
┌─────────────────────────────────────────────────────────────────────────┐
│                    THE VISION-COGNITIVE LOOP                            │
│                                                                         │
│  ┌────────────┐     ┌────────────┐     ┌────────────┐     ┌──────────┐  │
│  │   LOOK     │ ──▶ │   THINK    │ ──▶ │    ACT     │ ──▶ │  VERIFY  │  │
│  │  (Vision)  │     │  (Prime)   │     │ (Execute)  │     │ (Vision) │  │
│  └────────────┘     └────────────┘     └────────────┘     └──────────┘  │
│        │                  │                  │                  │       │
│        └──────────────────┴──────────────────┴──────────────────┘       │
│                                    │                                    │
│                                    ▼                                    │
│                            ┌────────────┐                               │
│                            │   LEARN    │                               │
│                            │ (Memory)   │                               │
│                            └────────────┘                               │
└─────────────────────────────────────────────────────────────────────────┘

The 5-Step Cognitive Loop:
1. LOOK (Vision Pre-Analysis): Capture and analyze screen state before planning
2. THINK (Planning): Generate plan with visual context awareness
3. ACT (Execution): Execute actions with spatial awareness
4. VERIFY (Visual Validation): Confirm action success through vision
5. LEARN (Memory): Store visual context for training

Key Features:
- Context-Aware Planning: Plans based on actual desktop state
- Visual Validation: Self-correcting loop with action verification
- Spatial Orchestration: Multi-space awareness via Yabai
- Visual Memory: Enriches training data with visual context

Author: JARVIS AI System
Version: 1.0.0 (Vision-Cognitive Loop)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

class CognitivePhase(str, Enum):
    """Phases of the cognitive loop."""
    IDLE = "idle"
    LOOK = "look"  # Vision pre-analysis
    THINK = "think"  # Planning with visual context
    ACT = "act"  # Execution
    VERIFY = "verify"  # Visual validation
    LEARN = "learn"  # Memory storage


class VerificationResult(str, Enum):
    """Result of visual verification."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class VisualState:
    """Captured visual state of the system."""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    space_id: int = 0
    current_app: str = ""
    visible_windows: List[Dict[str, Any]] = field(default_factory=list)
    applications: List[str] = field(default_factory=list)
    screen_text: List[str] = field(default_factory=list)
    ui_elements: List[Dict[str, Any]] = field(default_factory=list)
    is_locked: bool = False
    is_fullscreen: bool = False
    display_count: int = 1
    screenshot_hash: Optional[str] = None
    analysis_confidence: float = 0.0
    raw_analysis: Dict[str, Any] = field(default_factory=dict)

    def to_context_string(self) -> str:
        """Convert to a context string for LLM prompts."""
        lines = [
            "## Current OS State",
            f"- Active Space: {self.space_id}",
            f"- Current App: {self.current_app}",
            f"- Screen Locked: {self.is_locked}",
            f"- Fullscreen: {self.is_fullscreen}",
            f"- Display Count: {self.display_count}",
            "",
            "### Visible Windows:",
        ]

        for window in self.visible_windows[:10]:  # Limit to 10 windows
            app = window.get("app", "Unknown")
            title = window.get("title", "")[:50]
            lines.append(f"  - [{app}] {title}")

        if self.screen_text:
            lines.append("")
            lines.append("### Visible Text (OCR):")
            for text in self.screen_text[:5]:  # Limit to 5 text blocks
                lines.append(f"  - {text[:100]}")

        return "\n".join(lines)


@dataclass
class SpaceContext:
    """Multi-space context from Yabai."""
    spaces: List[Dict[str, Any]] = field(default_factory=list)
    current_space_id: int = 0
    total_spaces: int = 0
    displays: List[Dict[str, Any]] = field(default_factory=list)
    app_locations: Dict[str, int] = field(default_factory=dict)  # app -> space_id

    def get_space_for_app(self, app_name: str) -> Optional[int]:
        """Get the space ID where an app is located."""
        app_lower = app_name.lower()
        for app, space_id in self.app_locations.items():
            if app_lower in app.lower() or app.lower() in app_lower:
                return space_id
        return None

    def to_context_string(self) -> str:
        """Convert to a context string for LLM prompts."""
        lines = [
            "## Multi-Space Context",
            f"- Current Space: {self.current_space_id} of {self.total_spaces}",
            "",
            "### Spaces:",
        ]

        for space in self.spaces:
            space_id = space.get("space_id", "?")
            is_current = "→" if space.get("is_current", False) else " "
            apps = space.get("applications", [])[:3]
            apps_str = ", ".join(apps) if apps else "Empty"
            lines.append(f"  {is_current} Space {space_id}: {apps_str}")

        return "\n".join(lines)


@dataclass
class VisualVerification:
    """Result of visual verification after action."""
    result: VerificationResult = VerificationResult.UNKNOWN
    confidence: float = 0.0
    changes_detected: List[str] = field(default_factory=list)
    expected_state: Optional[str] = None
    actual_state: Optional[str] = None
    screenshot_diff_percent: float = 0.0
    verification_time_ms: float = 0.0
    retry_suggested: bool = False
    alternative_strategy: Optional[str] = None


@dataclass
class CognitiveLoopResult:
    """Result of a complete cognitive loop execution."""
    success: bool = False
    phase: CognitivePhase = CognitivePhase.IDLE
    visual_state_before: Optional[VisualState] = None
    visual_state_after: Optional[VisualState] = None
    space_context: Optional[SpaceContext] = None
    verification: Optional[VisualVerification] = None
    action_taken: Optional[str] = None
    error: Optional[str] = None
    total_time_ms: float = 0.0
    retries: int = 0


# =============================================================================
# Vision Cognitive Loop
# =============================================================================

class VisionCognitiveLoop:
    """
    The Vision-Cognitive Loop - Enables JARVIS to see, think, act, and verify.

    This class integrates:
    - VisionIntelligence: Screen capture and analysis
    - YabaiSpaceDetector: Multi-space awareness
    - Visual Validation: Action verification through vision
    - Visual Memory: Training data enrichment

    Usage:
        loop = VisionCognitiveLoop()
        await loop.initialize()

        # Pre-analyze before planning
        visual_context = await loop.look()

        # Execute with verification
        result = await loop.act_and_verify(
            action="click",
            target="search button",
            expected_outcome="search field focused"
        )

        # Store for learning
        await loop.learn(result)
    """

    def __init__(
        self,
        enable_vision: bool = True,
        enable_multi_space: bool = True,
        verification_timeout_ms: float = 5000.0,
        max_retries: int = 3,
        **kwargs  # Accept any additional parameters gracefully
    ):
        """
        Initialize Vision Cognitive Loop.

        Args:
            enable_vision: Enable vision analysis
            enable_multi_space: Enable multi-space awareness
            verification_timeout_ms: Timeout for verification operations
            max_retries: Maximum retries for operations
            **kwargs: Additional parameters (e.g., name_prefix) - safely ignored for flexibility
        """
        self.enable_vision = enable_vision
        self.enable_multi_space = enable_multi_space
        self.verification_timeout_ms = verification_timeout_ms
        self.max_retries = max_retries

        # Ignore any additional kwargs (for backward compatibility)
        if kwargs:
            logger.debug(f"[VisionLoop] Ignoring additional init parameters: {list(kwargs.keys())}")

        # Components (lazy loaded)
        self._vision_bridge = None
        self._yabai_detector = None
        self._screenshot_capture = None
        self._space_state_manager = None

        # State
        self._initialized = False
        self._current_phase = CognitivePhase.IDLE
        self._last_visual_state: Optional[VisualState] = None
        self._last_space_context: Optional[SpaceContext] = None

        # Metrics
        self._look_count = 0
        self._verify_count = 0
        self._verify_success_count = 0
        self._learn_count = 0

        # Visual memory cache for learning
        self._visual_memory_cache: List[Dict[str, Any]] = []
        self._max_cache_size = 100

        logger.info("[VisionLoop] Vision Cognitive Loop created")

    async def initialize(self, **kwargs) -> bool:
        """
        Initialize all vision components.

        **Flexible Initialization** - accepts optional parameters for Neural Mesh integration:
        - message_bus: Optional message bus (ignored in standalone mode)
        - registry: Optional registry (ignored in standalone mode)
        - Any other kwargs (ignored gracefully)

        This dual-mode design allows:
        1. Standalone usage: `await loop.initialize()`
        2. Neural Mesh integration: `await loop.initialize(message_bus=bus, registry=reg)`

        Args:
            **kwargs: Optional parameters (e.g., message_bus, registry) - safely ignored

        Returns:
            True if initialization successful, False otherwise
        """
        if self._initialized:
            logger.debug("[VisionLoop] Already initialized")
            return True

        try:
            # Detect mode (Neural Mesh or standalone)
            message_bus = kwargs.get('message_bus')
            registry = kwargs.get('registry')

            if message_bus and registry:
                logger.info("[VisionLoop] Initializing with Neural Mesh integration")
                # Future: Could register with Neural Mesh here
            else:
                logger.info("[VisionLoop] Initializing in standalone mode")

            # Initialize vision components
            if self.enable_vision:
                await self._init_vision_components()

            # Initialize multi-space components
            if self.enable_multi_space:
                await self._init_space_components()

            self._initialized = True
            logger.info("[VisionLoop] Vision Cognitive Loop initialized successfully")
            return True

        except Exception as e:
            logger.error(f"[VisionLoop] Initialization failed: {e}", exc_info=True)
            return False

    async def _init_vision_components(self) -> None:
        """Initialize vision-related components."""
        # Try to import vision bridge
        try:
            from vision.intelligence.vision_intelligence_bridge import VisionIntelligenceBridge
            self._vision_bridge = VisionIntelligenceBridge()
            logger.debug("[VisionLoop] VisionIntelligenceBridge loaded")
        except ImportError:
            logger.debug("[VisionLoop] VisionIntelligenceBridge not available")

        # Try to import screenshot capture
        try:
            from vision.reliable_screenshot_capture import ReliableScreenshotCapture
            self._screenshot_capture = ReliableScreenshotCapture()
            logger.debug("[VisionLoop] ReliableScreenshotCapture loaded")
        except ImportError:
            logger.debug("[VisionLoop] ReliableScreenshotCapture not available")

    async def _init_space_components(self) -> None:
        """Initialize multi-space components."""
        # Try to import Yabai detector
        try:
            from vision.windows_desktop import create_space_detector
            self._yabai_detector = create_space_detector(enable_vision=False)
            logger.debug("[VisionLoop] YabaiSpaceDetector loaded")
        except ImportError:
            logger.debug("[VisionLoop] YabaiSpaceDetector not available")

        # Try to import space state manager
        try:
            from context_intelligence.managers.space_state_manager import SpaceStateManager
            self._space_state_manager = SpaceStateManager()
            logger.debug("[VisionLoop] SpaceStateManager loaded")
        except ImportError:
            logger.debug("[VisionLoop] SpaceStateManager not available")

    # =========================================================================
    # Phase 1: LOOK - Vision Pre-Analysis
    # =========================================================================

    async def look(
        self,
        include_ocr: bool = True,
        include_ui_elements: bool = True,
        space_id: Optional[int] = None,
    ) -> Tuple[VisualState, SpaceContext]:
        """
        Phase 1: LOOK - Capture and analyze the current visual state.

        This is the "see before you think" phase that provides context
        for planning and execution.

        Args:
            include_ocr: Whether to include OCR text extraction
            include_ui_elements: Whether to analyze UI elements
            space_id: Specific space to analyze (None = current)

        Returns:
            Tuple of (VisualState, SpaceContext)
        """
        self._current_phase = CognitivePhase.LOOK
        self._look_count += 1
        start_time = time.time()

        visual_state = VisualState()
        space_context = SpaceContext()

        try:
            # Get multi-space context first (fast operation)
            if self._yabai_detector:
                space_context = await self._get_space_context()

            # Capture and analyze screen
            if self._screenshot_capture:
                visual_state = await self._capture_and_analyze(
                    include_ocr=include_ocr,
                    include_ui_elements=include_ui_elements,
                    space_id=space_id or space_context.current_space_id,
                )

            # Cache for learning
            self._last_visual_state = visual_state
            self._last_space_context = space_context

            logger.info(
                f"[VisionLoop] LOOK complete: "
                f"space={space_context.current_space_id}, "
                f"app={visual_state.current_app}, "
                f"windows={len(visual_state.visible_windows)}"
            )

        except Exception as e:
            logger.error(f"[VisionLoop] LOOK failed: {e}")
            visual_state.raw_analysis["error"] = str(e)

        return visual_state, space_context

    async def _get_space_context(self) -> SpaceContext:
        """Get multi-space context from Yabai."""
        context = SpaceContext()

        try:
            if not self._yabai_detector or not self._yabai_detector.yabai_available:
                return context

            # Get all spaces
            spaces = await self._yabai_detector.enumerate_all_spaces_async(
                include_display_info=True
            )

            context.spaces = spaces
            context.total_spaces = len(spaces)

            # Find current space and build app location map
            for space in spaces:
                space_id = space.get("space_id", 0)
                if space.get("is_current", False):
                    context.current_space_id = space_id

                # Map apps to spaces
                for app in space.get("applications", []):
                    context.app_locations[app] = space_id

            # Get display info
            context.displays = await self._get_display_info()

        except Exception as e:
            logger.debug(f"[VisionLoop] Space context failed: {e}")

        return context

    async def _get_display_info(self) -> List[Dict[str, Any]]:
        """Get display information."""
        displays = []
        try:
            if hasattr(self._yabai_detector, 'get_display_info'):
                displays = await self._yabai_detector.get_display_info()
        except Exception:
            pass
        return displays

    async def _capture_and_analyze(
        self,
        include_ocr: bool = True,
        include_ui_elements: bool = True,
        space_id: int = 0,
    ) -> VisualState:
        """Capture screenshot and analyze it."""
        state = VisualState(space_id=space_id)

        try:
            # Capture screenshot
            screenshot_data = await self._capture_screenshot(space_id)

            if screenshot_data:
                # Generate hash for change detection
                state.screenshot_hash = hashlib.md5(screenshot_data).hexdigest()

                # Analyze with vision bridge
                if self._vision_bridge:
                    analysis = await self._vision_bridge.analyze_visual_state(
                        screenshot_data=screenshot_data,
                        include_ocr=include_ocr,
                    )
                    state.raw_analysis = analysis

                    # Extract structured data
                    state.current_app = analysis.get("current_app", "")
                    state.visible_windows = analysis.get("windows", [])
                    state.applications = analysis.get("applications", [])
                    state.screen_text = analysis.get("text", [])
                    state.ui_elements = analysis.get("ui_elements", [])
                    state.is_locked = analysis.get("is_locked", False)
                    state.is_fullscreen = analysis.get("is_fullscreen", False)
                    state.analysis_confidence = analysis.get("confidence", 0.0)

                else:
                    # Fallback: basic window info from Yabai
                    if self._yabai_detector:
                        windows = await self._get_window_info()
                        state.visible_windows = windows
                        if windows:
                            state.current_app = windows[0].get("app", "")
                            state.applications = list(set(w.get("app", "") for w in windows))

        except Exception as e:
            logger.debug(f"[VisionLoop] Capture/analyze failed: {e}")
            state.raw_analysis["error"] = str(e)

        return state

    async def _capture_screenshot(self, space_id: int = 0) -> Optional[bytes]:
        """Capture screenshot of current or specified space."""
        try:
            if self._screenshot_capture:
                result = await self._screenshot_capture.capture_space_with_matrix(space_id)
                if result.success and result.image_data:
                    return result.image_data

            # Fallback: use screencapture CLI
            import subprocess
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                temp_path = f.name

            subprocess.run(
                ["screencapture", "-x", "-C", temp_path],
                timeout=5.0,
                check=True,
            )

            with open(temp_path, "rb") as f:
                data = f.read()

            os.unlink(temp_path)
            return data

        except Exception as e:
            logger.debug(f"[VisionLoop] Screenshot capture failed: {e}")
            return None

    async def _get_window_info(self) -> List[Dict[str, Any]]:
        """Get window information from Yabai."""
        try:
            if hasattr(self._yabai_detector, 'get_visible_windows'):
                return await self._yabai_detector.get_visible_windows()
            return []
        except Exception:
            return []

    # =========================================================================
    # Phase 3.5: VERIFY - Visual Validation Loop
    # =========================================================================

    async def verify(
        self,
        expected_outcome: str,
        before_state: Optional[VisualState] = None,
        timeout_ms: Optional[float] = None,
    ) -> VisualVerification:
        """
        Phase 3.5: VERIFY - Validate that an action succeeded through vision.

        This is the self-correcting loop that ensures actions achieved
        their intended effect.

        Args:
            expected_outcome: Description of expected visual change
            before_state: Visual state before action (for comparison)
            timeout_ms: Timeout for verification

        Returns:
            VisualVerification result
        """
        self._current_phase = CognitivePhase.VERIFY
        self._verify_count += 1
        start_time = time.time()
        timeout = timeout_ms or self.verification_timeout_ms

        verification = VisualVerification(expected_state=expected_outcome)

        try:
            # Wait a moment for UI to update
            await asyncio.sleep(0.3)

            # Capture current state
            after_state, _ = await self.look(include_ocr=True, include_ui_elements=True)

            # Compare states
            verification = await self._compare_states(
                before_state=before_state or self._last_visual_state,
                after_state=after_state,
                expected_outcome=expected_outcome,
            )

            verification.verification_time_ms = (time.time() - start_time) * 1000

            if verification.result == VerificationResult.SUCCESS:
                self._verify_success_count += 1
                logger.info(f"[VisionLoop] VERIFY success: {expected_outcome}")
            else:
                logger.warning(
                    f"[VisionLoop] VERIFY {verification.result.value}: "
                    f"expected='{expected_outcome}', "
                    f"changes={verification.changes_detected}"
                )

        except asyncio.TimeoutError:
            verification.result = VerificationResult.TIMEOUT
            verification.retry_suggested = True
            logger.warning(f"[VisionLoop] VERIFY timeout after {timeout}ms")

        except Exception as e:
            verification.result = VerificationResult.FAILED
            verification.alternative_strategy = f"Error during verification: {e}"
            logger.error(f"[VisionLoop] VERIFY error: {e}")

        return verification

    async def _compare_states(
        self,
        before_state: Optional[VisualState],
        after_state: VisualState,
        expected_outcome: str,
    ) -> VisualVerification:
        """Compare before/after states to verify action success."""
        verification = VisualVerification(expected_state=expected_outcome)

        # If no before state, we can only check if after state matches expectations
        if not before_state:
            verification.result = VerificationResult.UNKNOWN
            verification.actual_state = after_state.current_app
            return verification

        changes = []

        # Check for window changes
        before_windows = set(w.get("title", "") for w in before_state.visible_windows)
        after_windows = set(w.get("title", "") for w in after_state.visible_windows)

        new_windows = after_windows - before_windows
        closed_windows = before_windows - after_windows

        if new_windows:
            changes.append(f"New windows: {list(new_windows)[:3]}")
        if closed_windows:
            changes.append(f"Closed windows: {list(closed_windows)[:3]}")

        # Check for app changes
        if before_state.current_app != after_state.current_app:
            changes.append(f"App changed: {before_state.current_app} → {after_state.current_app}")

        # Check for screenshot hash change (any visual change)
        if before_state.screenshot_hash != after_state.screenshot_hash:
            changes.append("Visual content changed")

        # Check for text changes (OCR)
        before_text = set(before_state.screen_text)
        after_text = set(after_state.screen_text)
        new_text = after_text - before_text

        if new_text:
            changes.append(f"New text appeared: {list(new_text)[:2]}")

        verification.changes_detected = changes
        verification.actual_state = after_state.current_app

        # Determine success based on expected outcome
        expected_lower = expected_outcome.lower()

        # Heuristic matching
        if changes:
            # Check if any change matches expected outcome
            for change in changes:
                change_lower = change.lower()
                if any(word in change_lower for word in expected_lower.split()):
                    verification.result = VerificationResult.SUCCESS
                    verification.confidence = 0.8
                    return verification

            # Some change happened, might be partial success
            verification.result = VerificationResult.PARTIAL
            verification.confidence = 0.5
            verification.retry_suggested = True
        else:
            # No changes detected
            verification.result = VerificationResult.FAILED
            verification.retry_suggested = True
            verification.alternative_strategy = "No visual change detected. Try alternative action."

        return verification

    # =========================================================================
    # Phase 5: LEARN - Visual Memory
    # =========================================================================

    async def learn(
        self,
        goal: str,
        action: str,
        result: CognitiveLoopResult,
        success: bool,
    ) -> bool:
        """
        Phase 5: LEARN - Store visual context for training.

        This enriches the Data Flywheel with visual state information
        that can be used to train models on visual cues.

        Args:
            goal: The original goal
            action: The action that was taken
            result: The cognitive loop result
            success: Whether the overall task succeeded

        Returns:
            True if successfully stored
        """
        self._current_phase = CognitivePhase.LEARN
        self._learn_count += 1

        try:
            # Build visual memory record
            memory_record = {
                "event_id": str(uuid.uuid4())[:8],
                "timestamp": datetime.now().isoformat(),
                "goal": goal,
                "action": action,
                "success": success,
                "visual_context": {
                    "before": self._state_to_dict(result.visual_state_before),
                    "after": self._state_to_dict(result.visual_state_after),
                },
                "space_context": self._space_to_dict(result.space_context),
                "verification": self._verification_to_dict(result.verification),
                "metrics": {
                    "total_time_ms": result.total_time_ms,
                    "retries": result.retries,
                },
            }

            # Add to cache
            self._visual_memory_cache.append(memory_record)
            if len(self._visual_memory_cache) > self._max_cache_size:
                self._visual_memory_cache = self._visual_memory_cache[-self._max_cache_size:]

            # Write to cross-repo bridge for Reactor-Core
            await self._write_to_flywheel(memory_record)

            logger.debug(f"[VisionLoop] LEARN: stored visual memory for '{goal[:50]}'")
            return True

        except Exception as e:
            logger.debug(f"[VisionLoop] LEARN failed: {e}")
            return False

    def _state_to_dict(self, state: Optional[VisualState]) -> Optional[Dict[str, Any]]:
        """Convert VisualState to dictionary for storage."""
        if not state:
            return None
        return {
            "timestamp": state.timestamp,
            "space_id": state.space_id,
            "current_app": state.current_app,
            "applications": state.applications,
            "window_count": len(state.visible_windows),
            "is_locked": state.is_locked,
            "is_fullscreen": state.is_fullscreen,
            "screenshot_hash": state.screenshot_hash,
            "confidence": state.analysis_confidence,
        }

    def _space_to_dict(self, context: Optional[SpaceContext]) -> Optional[Dict[str, Any]]:
        """Convert SpaceContext to dictionary for storage."""
        if not context:
            return None
        return {
            "current_space_id": context.current_space_id,
            "total_spaces": context.total_spaces,
            "app_locations": context.app_locations,
        }

    def _verification_to_dict(self, verification: Optional[VisualVerification]) -> Optional[Dict[str, Any]]:
        """Convert VisualVerification to dictionary for storage."""
        if not verification:
            return None
        return {
            "result": verification.result.value,
            "confidence": verification.confidence,
            "changes_detected": verification.changes_detected,
            "verification_time_ms": verification.verification_time_ms,
        }

    async def _write_to_flywheel(self, record: Dict[str, Any]) -> None:
        """Write visual memory to Data Flywheel bridge."""
        try:
            bridge_dir = Path.home() / ".jarvis" / "cross_repo" / "visual_learning"
            bridge_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{record['event_id']}.json"
            filepath = bridge_dir / filename

            with open(filepath, "w") as f:
                json.dump(record, f, indent=2)

        except Exception as e:
            logger.debug(f"[VisionLoop] Flywheel write failed: {e}")

    # =========================================================================
    # High-Level API: Act and Verify
    # =========================================================================

    async def act_and_verify(
        self,
        action_callback: Callable,
        expected_outcome: str,
        max_retries: Optional[int] = None,
    ) -> CognitiveLoopResult:
        """
        Execute an action and verify it succeeded through vision.

        This is the core Act-Verify loop that implements self-correction.

        Args:
            action_callback: Async function that performs the action
            expected_outcome: Description of expected visual change
            max_retries: Maximum retry attempts

        Returns:
            CognitiveLoopResult with verification details
        """
        retries = max_retries or self.max_retries
        start_time = time.time()

        result = CognitiveLoopResult()

        # Capture before state
        result.visual_state_before, result.space_context = await self.look()

        for attempt in range(retries + 1):
            result.retries = attempt

            try:
                # Execute action
                self._current_phase = CognitivePhase.ACT
                result.action_taken = expected_outcome
                await action_callback()

                # Verify outcome
                verification = await self.verify(
                    expected_outcome=expected_outcome,
                    before_state=result.visual_state_before,
                )
                result.verification = verification

                if verification.result == VerificationResult.SUCCESS:
                    result.success = True
                    result.phase = CognitivePhase.VERIFY
                    break

                elif verification.result == VerificationResult.PARTIAL:
                    # Partial success might be acceptable
                    result.success = True
                    result.phase = CognitivePhase.VERIFY
                    logger.info(f"[VisionLoop] Partial success on attempt {attempt + 1}")
                    break

                elif not verification.retry_suggested or attempt >= retries:
                    # No retry suggested or max retries reached
                    result.success = False
                    result.error = f"Verification failed: {verification.changes_detected}"
                    break

                else:
                    # Retry
                    logger.info(f"[VisionLoop] Retrying action (attempt {attempt + 2}/{retries + 1})")
                    await asyncio.sleep(0.5)  # Brief pause before retry

            except Exception as e:
                result.error = str(e)
                if attempt >= retries:
                    result.success = False
                    break

        # Capture after state
        result.visual_state_after, _ = await self.look()
        result.total_time_ms = (time.time() - start_time) * 1000

        return result

    # =========================================================================
    # Context Generation for LLM
    # =========================================================================

    def get_visual_context_for_prompt(self) -> str:
        """
        Get formatted visual context for inclusion in LLM prompts.

        This is called before planning to provide JARVIS Prime with
        awareness of the current OS state.

        Returns:
            Formatted context string
        """
        sections = []

        if self._last_visual_state:
            sections.append(self._last_visual_state.to_context_string())

        if self._last_space_context:
            sections.append(self._last_space_context.to_context_string())

        if not sections:
            return "## Current OS State\nVisual context not available."

        return "\n\n".join(sections)

    # =========================================================================
    # Metrics and Status
    # =========================================================================

    def get_metrics(self) -> Dict[str, Any]:
        """Get cognitive loop metrics."""
        return {
            "initialized": self._initialized,
            "current_phase": self._current_phase.value,
            "look_count": self._look_count,
            "verify_count": self._verify_count,
            "verify_success_rate": (
                self._verify_success_count / self._verify_count
                if self._verify_count > 0
                else 0.0
            ),
            "learn_count": self._learn_count,
            "visual_memory_cache_size": len(self._visual_memory_cache),
            "components": {
                "vision_bridge": self._vision_bridge is not None,
                "yabai_detector": self._yabai_detector is not None,
                "screenshot_capture": self._screenshot_capture is not None,
                "space_state_manager": self._space_state_manager is not None,
            },
        }


# =============================================================================
# Global Instance
# =============================================================================

_cognitive_loop: Optional[VisionCognitiveLoop] = None


def get_vision_cognitive_loop() -> VisionCognitiveLoop:
    """Get or create the global Vision Cognitive Loop instance."""
    global _cognitive_loop
    if _cognitive_loop is None:
        _cognitive_loop = VisionCognitiveLoop()
    return _cognitive_loop


async def initialize_vision_cognitive_loop() -> bool:
    """Initialize the global Vision Cognitive Loop."""
    loop = get_vision_cognitive_loop()
    return await loop.initialize()
