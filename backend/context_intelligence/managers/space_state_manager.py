"""
Space State Manager - Advanced Edge Case Handling
==================================================

Handles all space-related edge cases dynamically and robustly:
- Space existence validation
- Empty space detection
- Minimized-only window detection
- Space transition handling with retry logic
- Fullscreen app detection
- Split view detection

Architecture:
    Query → SpaceValidator → State Detection → Edge Case Handler
      ↓           ↓                ↓                  ↓
    Parse    Validate Space    Check State      Handle Edge Case
      ↓           ↓                ↓                  ↓
    Space ID  Exists? Count?  Empty/Min/Full   Retry/Error/Success
      ↓           ↓                ↓                  ↓
      └───────────┴────────────────┴────────────→ Response

Features:
- ✅ Async/await throughout
- ✅ Dynamic space detection (no hardcoding)
- ✅ Robust retry logic with exponential backoff
- ✅ Comprehensive state detection
- ✅ Natural language error messages
- ✅ Integration with YabaiSpaceDetector
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import subprocess
import json

logger = logging.getLogger(__name__)


async def _yabai_query(kind: str) -> List[Dict[str, Any]]:
    """``yabai -m query --<kind>`` JSON from the host's space backend.

    On Windows / WSL the desktop agent answers in yabai's own schema
    (``index``, ``has-focus``, ``space``, ``frame`` ...), so every reader
    below stays unchanged. Elsewhere the yabai CLI runs exactly as before.
    Raises on failure; each caller keeps its own fallback.
    """
    from vision.windows_desktop import space_backend

    if space_backend() == "windows_agent":
        from vision.yabai_space_detector import get_yabai_detector

        return await get_yabai_detector().query_raw_async(kind)

    proc = await asyncio.create_subprocess_shell(
        f"yabai -m query --{kind}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yabai query --{kind} failed: {stderr.decode().strip()}")
    return json.loads(stdout.decode())


# ============================================================================
# SPACE STATE DEFINITIONS
# ============================================================================

class SpaceState(Enum):
    """Possible states for a Mission Control space"""
    # Normal states
    ACTIVE = "active"                    # Has visible windows
    EMPTY = "empty"                      # No windows at all
    MINIMIZED_ONLY = "minimized_only"    # Only minimized windows
    FULLSCREEN = "fullscreen"            # Single fullscreen app
    SPLIT_VIEW = "split_view"            # Multiple windows side-by-side

    # Transition states
    TRANSITIONING = "transitioning"      # User switching spaces
    LOADING = "loading"                  # Space loading content

    # Error states
    NOT_EXIST = "not_exist"             # Space doesn't exist
    INACCESSIBLE = "inaccessible"       # Can't access space (permissions)
    UNKNOWN = "unknown"                  # Can't determine state


class WindowState(Enum):
    """States for windows within a space"""
    VISIBLE = "visible"
    MINIMIZED = "minimized"
    HIDDEN = "hidden"
    FULLSCREEN = "fullscreen"


@dataclass
class WindowInfo:
    """Information about a window"""
    id: int
    app: str
    title: str
    state: WindowState
    frame: Dict[str, float]
    is_focused: bool = False


@dataclass
class SpaceStateInfo:
    """Comprehensive space state information"""
    space_id: int
    state: SpaceState
    exists: bool
    window_count: int
    visible_window_count: int
    minimized_window_count: int
    windows: List[WindowInfo]
    applications: List[str]
    is_current: bool
    is_fullscreen: bool
    display_id: int
    error_message: Optional[str] = None
    detection_time: float = 0.0


@dataclass
class EdgeCaseResult:
    """Result of edge case handling"""
    success: bool
    space_id: int
    edge_case: str
    message: str
    state_info: Optional[SpaceStateInfo] = None
    retry_count: int = 0
    action_taken: Optional[str] = None  # What action was taken to handle the edge case


# ============================================================================
# SPACE VALIDATOR
# ============================================================================

class SpaceValidator:
    """
    Validates space existence and properties.

    Uses YabaiSpaceDetector for querying but adds comprehensive validation.
    """

    def __init__(self, max_retry: int = 3, retry_delay: float = 0.5):
        """
        Initialize space validator.

        Args:
            max_retry: Maximum number of retries for transient errors
            retry_delay: Initial delay between retries (exponential backoff)
        """
        self.max_retry = max_retry
        self.retry_delay = retry_delay

    async def validate_space_exists(self, space_id: int) -> Tuple[bool, Optional[int]]:
        """
        Validate that a space exists.

        Args:
            space_id: Space ID to validate

        Returns:
            Tuple of (exists, max_space_id)
        """
        try:
            spaces_data = await _yabai_query("spaces")
            space_ids = [s.get("index", 0) for s in spaces_data]

            exists = space_id in space_ids
            max_space_id = max(space_ids) if space_ids else 0

            return exists, max_space_id

        except Exception as e:
            logger.error(f"[SPACE-VALIDATOR] Error validating space {space_id}: {e}")
            return False, None

    async def get_space_window_states(self, space_id: int) -> List[WindowInfo]:
        """
        Get detailed window state information for a space.

        Args:
            space_id: Space ID to query

        Returns:
            List of WindowInfo objects
        """
        try:
            all_windows = await _yabai_query("windows")

            # Filter for this space and build WindowInfo objects
            windows = []
            for w in all_windows:
                if w.get("space") != space_id:
                    continue

                # Determine window state
                if w.get("is-native-fullscreen") or w.get("zoom-fullscreen"):
                    state = WindowState.FULLSCREEN
                elif w.get("is-minimized"):
                    state = WindowState.MINIMIZED
                elif w.get("is-hidden"):
                    state = WindowState.HIDDEN
                else:
                    state = WindowState.VISIBLE

                window_info = WindowInfo(
                    id=w.get("id", 0),
                    app=w.get("app", "Unknown"),
                    title=w.get("title", ""),
                    state=state,
                    frame=w.get("frame", {}),
                    is_focused=w.get("has-focus", False)
                )
                windows.append(window_info)

            return windows

        except Exception as e:
            logger.error(f"[SPACE-VALIDATOR] Error getting window states for space {space_id}: {e}")
            return []

    async def detect_space_state(self, space_id: int, windows: List[WindowInfo]) -> SpaceState:
        """
        Detect the current state of a space based on its windows.

        Args:
            space_id: Space ID
            windows: List of windows in the space

        Returns:
            SpaceState enum value
        """
        if not windows:
            return SpaceState.EMPTY

        # Count window states
        visible = [w for w in windows if w.state == WindowState.VISIBLE]
        minimized = [w for w in windows if w.state == WindowState.MINIMIZED]
        fullscreen = [w for w in windows if w.state == WindowState.FULLSCREEN]

        # Fullscreen detection
        if fullscreen and len(visible) == 1:
            return SpaceState.FULLSCREEN

        # Minimized-only detection
        if minimized and not visible and not fullscreen:
            return SpaceState.MINIMIZED_ONLY

        # Split view detection (2+ visible windows with similar sizes)
        if len(visible) >= 2:
            # Check if windows are side-by-side with similar heights
            # This is a simple heuristic - real split view detection is complex
            if await self._is_split_view(visible):
                return SpaceState.SPLIT_VIEW

        # Active space with visible windows
        if visible:
            return SpaceState.ACTIVE

        return SpaceState.UNKNOWN

    async def _is_split_view(self, windows: List[WindowInfo]) -> bool:
        """
        Heuristic to detect if windows are in split view.

        Args:
            windows: List of visible windows

        Returns:
            True if likely split view
        """
        if len(windows) < 2:
            return False

        # Get frames
        frames = [w.frame for w in windows if w.frame]
        if len(frames) < 2:
            return False

        # Check if windows have similar heights and are side-by-side
        heights = [f.get("h", 0) for f in frames]
        widths = [f.get("w", 0) for f in frames]
        x_positions = [f.get("x", 0) for f in frames]

        # Similar heights (within 10% tolerance)
        if heights:
            avg_height = sum(heights) / len(heights)
            height_variance = all(abs(h - avg_height) / avg_height < 0.1 for h in heights if avg_height > 0)
        else:
            height_variance = False

        # Side-by-side (x positions are significantly different)
        if x_positions:
            x_diff = max(x_positions) - min(x_positions)
            side_by_side = x_diff > 100  # At least 100px apart
        else:
            side_by_side = False

        return height_variance and side_by_side


# ============================================================================
# SPACE TRANSITION HANDLER
# ============================================================================

class SpaceTransitionHandler:
    """
    Handles space transitions with retry logic.

    When switching spaces, there can be transient states where captures fail.
    This handler implements exponential backoff retry logic.
    """

    def __init__(self, max_retry: int = 3, initial_delay: float = 0.5):
        """
        Initialize transition handler.

        Args:
            max_retry: Maximum number of retries
            initial_delay: Initial delay in seconds (doubles each retry)
        """
        self.max_retry = max_retry
        self.initial_delay = initial_delay

    async def wait_for_stable_state(
        self,
        space_id: int,
        validator: SpaceValidator,
        timeout: float = 5.0
    ) -> bool:
        """
        Wait for a space to reach a stable state.

        Args:
            space_id: Space to wait for
            validator: SpaceValidator instance
            timeout: Maximum time to wait

        Returns:
            True if space became stable, False if timeout
        """
        start_time = datetime.now()
        delay = self.initial_delay

        for attempt in range(self.max_retry):
            # Check if space is stable
            windows = await validator.get_space_window_states(space_id)
            state = await validator.detect_space_state(space_id, windows)

            if state not in [SpaceState.TRANSITIONING, SpaceState.LOADING, SpaceState.UNKNOWN]:
                logger.info(f"[TRANSITION-HANDLER] Space {space_id} stable after {attempt} retries")
                return True

            # Check timeout
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed >= timeout:
                logger.warning(f"[TRANSITION-HANDLER] Timeout waiting for space {space_id} to stabilize")
                return False

            # Wait with exponential backoff
            logger.debug(f"[TRANSITION-HANDLER] Space {space_id} transitioning, retry {attempt + 1}/{self.max_retry}")
            await asyncio.sleep(delay)
            delay *= 2  # Exponential backoff

        return False

    async def retry_operation(
        self,
        operation: callable,
        space_id: int,
        *args,
        **kwargs
    ) -> Tuple[bool, Any, int]:
        """
        Retry an operation with exponential backoff.

        Args:
            operation: Async function to retry
            space_id: Space ID (for logging)
            *args, **kwargs: Arguments to pass to operation

        Returns:
            Tuple of (success, result, retry_count)
        """
        delay = self.initial_delay

        for attempt in range(self.max_retry):
            try:
                result = await operation(*args, **kwargs)
                logger.info(f"[TRANSITION-HANDLER] Operation succeeded on attempt {attempt + 1}")
                return True, result, attempt
            except Exception as e:
                logger.warning(f"[TRANSITION-HANDLER] Attempt {attempt + 1} failed: {e}")

                if attempt < self.max_retry - 1:
                    await asyncio.sleep(delay)
                    delay *= 2

        logger.error(f"[TRANSITION-HANDLER] Operation failed after {self.max_retry} attempts")
        return False, None, self.max_retry


# ============================================================================
# SPACE STATE MANAGER
# ============================================================================

class SpaceStateManager:
    """
    Main coordinator for space state management and edge case handling.

    Integrates SpaceValidator and SpaceTransitionHandler to provide
    comprehensive space state management with robust error handling.
    """

    def __init__(
        self,
        max_retry: int = 3,
        retry_delay: float = 0.5,
        transition_timeout: float = 5.0
    ):
        """
        Initialize space state manager.

        Args:
            max_retry: Maximum retries for operations
            retry_delay: Initial retry delay
            transition_timeout: Maximum time to wait for transitions
        """
        self.validator = SpaceValidator(max_retry, retry_delay)
        self.transition_handler = SpaceTransitionHandler(max_retry, retry_delay)
        self.transition_timeout = transition_timeout

        logger.info("[SPACE-STATE-MANAGER] Initialized")

    async def get_space_state(self, space_id: int) -> SpaceStateInfo:
        """
        Get comprehensive state information for a space.

        Args:
            space_id: Space ID to query

        Returns:
            SpaceStateInfo with complete state information
        """
        start_time = datetime.now()

        try:
            # Validate space exists
            exists, max_space_id = await self.validator.validate_space_exists(space_id)

            if not exists:
                return SpaceStateInfo(
                    space_id=space_id,
                    state=SpaceState.NOT_EXIST,
                    exists=False,
                    window_count=0,
                    visible_window_count=0,
                    minimized_window_count=0,
                    windows=[],
                    applications=[],
                    is_current=False,
                    is_fullscreen=False,
                    display_id=1,
                    error_message=f"Space {space_id} doesn't exist. You have {max_space_id} spaces.",
                    detection_time=(datetime.now() - start_time).total_seconds()
                )

            # Get window states
            windows = await self.validator.get_space_window_states(space_id)

            # Detect space state
            state = await self.validator.detect_space_state(space_id, windows)

            # Count window states
            visible_count = sum(1 for w in windows if w.state == WindowState.VISIBLE)
            minimized_count = sum(1 for w in windows if w.state == WindowState.MINIMIZED)

            # Get applications
            applications = list(set(w.app for w in windows))

            # Query space properties
            space_props = await self._get_space_properties(space_id)

            return SpaceStateInfo(
                space_id=space_id,
                state=state,
                exists=True,
                window_count=len(windows),
                visible_window_count=visible_count,
                minimized_window_count=minimized_count,
                windows=windows,
                applications=applications,
                is_current=space_props.get("is_current", False),
                is_fullscreen=space_props.get("is_fullscreen", False),
                display_id=space_props.get("display_id", 1),
                error_message=None,
                detection_time=(datetime.now() - start_time).total_seconds()
            )

        except Exception as e:
            logger.error(f"[SPACE-STATE-MANAGER] Error getting state for space {space_id}: {e}")
            return SpaceStateInfo(
                space_id=space_id,
                state=SpaceState.UNKNOWN,
                exists=False,
                window_count=0,
                visible_window_count=0,
                minimized_window_count=0,
                windows=[],
                applications=[],
                is_current=False,
                is_fullscreen=False,
                display_id=1,
                error_message=f"Error querying space {space_id}: {str(e)}",
                detection_time=(datetime.now() - start_time).total_seconds()
            )

    async def _get_space_properties(self, space_id: int) -> Dict[str, Any]:
        """
        Get space properties from yabai.

        Args:
            space_id: Space ID

        Returns:
            Dictionary of properties
        """
        try:
            for space in await _yabai_query("spaces"):
                if space.get("index") == space_id:
                    return {
                        "is_current": space.get("has-focus", False),
                        "is_fullscreen": space.get("is-native-fullscreen", False),
                        "display_id": space.get("display", 1),
                        "is_visible": space.get("is-visible", False)
                    }

            return {}

        except Exception as e:
            logger.error(f"[SPACE-STATE-MANAGER] Error getting space properties: {e}")
            return {}

    async def handle_edge_case(self, space_id: int) -> EdgeCaseResult:
        """
        Handle all edge cases for a space.

        This is the main entry point for edge case handling.

        Args:
            space_id: Space ID to check

        Returns:
            EdgeCaseResult with handling outcome
        """
        # Get space state
        state_info = await self.get_space_state(space_id)

        # Handle different edge cases
        if state_info.state == SpaceState.NOT_EXIST:
            return await self._handle_not_exist(state_info)

        elif state_info.state == SpaceState.EMPTY:
            return await self._handle_empty_space(state_info)

        elif state_info.state == SpaceState.MINIMIZED_ONLY:
            return await self._handle_minimized_only(state_info)

        elif state_info.state == SpaceState.TRANSITIONING:
            return await self._handle_transitioning(state_info)

        elif state_info.state == SpaceState.FULLSCREEN:
            return await self._handle_fullscreen(state_info)

        elif state_info.state == SpaceState.SPLIT_VIEW:
            return await self._handle_split_view(state_info)

        else:
            # Normal state - no edge case
            return EdgeCaseResult(
                success=True,
                space_id=space_id,
                edge_case="none",
                message=f"Space {space_id} is active with {state_info.window_count} window(s).",
                state_info=state_info
            )

    async def _handle_not_exist(self, state_info: SpaceStateInfo) -> EdgeCaseResult:
        """Handle space doesn't exist edge case"""
        return EdgeCaseResult(
            success=False,
            space_id=state_info.space_id,
            edge_case="not_exist",
            message=state_info.error_message or f"Space {state_info.space_id} doesn't exist.",
            state_info=state_info,
            action_taken="returned_error"
        )

    async def _handle_empty_space(self, state_info: SpaceStateInfo) -> EdgeCaseResult:
        """Handle empty space edge case"""
        return EdgeCaseResult(
            success=True,
            space_id=state_info.space_id,
            edge_case="empty",
            message=f"Space {state_info.space_id} is empty (no windows).",
            state_info=state_info,
            action_taken="skipped_capture"
        )

    async def _handle_minimized_only(self, state_info: SpaceStateInfo) -> EdgeCaseResult:
        """Handle minimized-only windows edge case"""
        apps = ", ".join(state_info.applications[:3])
        if len(state_info.applications) > 3:
            apps += f" and {len(state_info.applications) - 3} more"

        return EdgeCaseResult(
            success=False,
            space_id=state_info.space_id,
            edge_case="minimized_only",
            message=f"Space {state_info.space_id} has {state_info.minimized_window_count} minimized window(s) only ({apps}). Cannot capture.",
            state_info=state_info,
            action_taken="skipped_capture"
        )

    async def _handle_transitioning(self, state_info: SpaceStateInfo) -> EdgeCaseResult:
        """Handle space mid-transition edge case"""
        # Wait for stable state
        stable = await self.transition_handler.wait_for_stable_state(
            state_info.space_id,
            self.validator,
            self.transition_timeout
        )

        if stable:
            # Retry getting state
            new_state = await self.get_space_state(state_info.space_id)
            return EdgeCaseResult(
                success=True,
                space_id=state_info.space_id,
                edge_case="transitioning",
                message=f"Space {state_info.space_id} was transitioning, now stable.",
                state_info=new_state,
                retry_count=1,
                action_taken="waited_and_retried"
            )
        else:
            return EdgeCaseResult(
                success=False,
                space_id=state_info.space_id,
                edge_case="transitioning",
                message=f"Space {state_info.space_id} is mid-transition. Please try again.",
                state_info=state_info,
                action_taken="timeout"
            )

    async def _handle_fullscreen(self, state_info: SpaceStateInfo) -> EdgeCaseResult:
        """Handle fullscreen app edge case"""
        app = state_info.applications[0] if state_info.applications else "Unknown"

        return EdgeCaseResult(
            success=True,
            space_id=state_info.space_id,
            edge_case="fullscreen",
            message=f"Space {state_info.space_id} has {app} in fullscreen. Capture will work normally.",
            state_info=state_info,
            action_taken="normal_capture"
        )

    async def _handle_split_view(self, state_info: SpaceStateInfo) -> EdgeCaseResult:
        """Handle split view edge case"""
        apps = " and ".join(state_info.applications[:2])

        return EdgeCaseResult(
            success=True,
            space_id=state_info.space_id,
            edge_case="split_view",
            message=f"Space {state_info.space_id} has split view ({apps}). Entire space will be captured.",
            state_info=state_info,
            action_taken="full_space_capture"
        )

    async def validate_and_prepare_capture(self, space_id: int) -> Tuple[bool, str, Optional[SpaceStateInfo]]:
        """
        Validate a space and prepare for capture.

        This is a convenience method that handles all edge cases and
        returns whether capture should proceed.

        Args:
            space_id: Space to validate

        Returns:
            Tuple of (should_capture, message, state_info)
        """
        result = await self.handle_edge_case(space_id)

        # Determine if capture should proceed
        should_capture = result.success and result.edge_case not in ["empty", "minimized_only", "not_exist"]

        return should_capture, result.message, result.state_info


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_space_state_manager: Optional[SpaceStateManager] = None


def get_space_state_manager() -> SpaceStateManager:
    """Get or create the global SpaceStateManager instance"""
    global _space_state_manager
    if _space_state_manager is None:
        _space_state_manager = SpaceStateManager()
    return _space_state_manager


def initialize_space_state_manager(
    max_retry: int = 3,
    retry_delay: float = 0.5,
    transition_timeout: float = 5.0
) -> SpaceStateManager:
    """
    Initialize the global SpaceStateManager with custom settings.

    Args:
        max_retry: Maximum retries for operations
        retry_delay: Initial retry delay
        transition_timeout: Maximum time to wait for transitions

    Returns:
        Initialized SpaceStateManager
    """
    global _space_state_manager
    _space_state_manager = SpaceStateManager(max_retry, retry_delay, transition_timeout)
    return _space_state_manager
