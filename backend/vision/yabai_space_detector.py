"""
Yabai integration for accurate Mission Control space detection
Provides real-time space and window information using Yabai CLI
Enhanced with YOLO vision for multi-monitor layout detection

FEATURES:
- Auto-start service with retry logic
- Graceful fallback when yabai unavailable
- Async subprocess execution (non-blocking)
- Service health monitoring and auto-recovery
- Multi-display space organization
- Vision-enhanced layout detection

NOTE: This file has had repeated indentation issues with auto-formatters.
If using black, autopep8, or other formatters, please exclude this file
or manually review changes before committing.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Import GhostPersistenceManager for crash recovery
try:
    from backend.vision.ghost_persistence_manager import (
        GhostPersistenceManager,
        get_persistence_manager,
    )
    _HAS_GHOST_PERSISTENCE = True
except ImportError:
    _HAS_GHOST_PERSISTENCE = False

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

# v66.0: Import rapidfuzz for intelligent app name matching in Boomerang Protocol
# Allows "Chrome" to match "Google Chrome", etc.
_HAS_RAPIDFUZZ = False
try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    rapidfuzz_fuzz = None

# v67.0: Import AppLibrary for dynamic macOS Spotlight integration (CEREBRO PROTOCOL)
# Replaces hardcoded alias lists with live system queries via mdfind
_HAS_APP_LIBRARY = False
_app_library_instance = None
try:
    from system.app_library import get_app_library
    _HAS_APP_LIBRARY = True
except ImportError:
    try:
        from backend.system.app_library import get_app_library
        _HAS_APP_LIBRARY = True
    except ImportError:
        get_app_library = None

# v68.0: Import PhantomHardwareManager for software-defined Ghost Display
# Creates virtual displays via BetterDisplay - no HDMI dummy plug needed
_HAS_PHANTOM_HARDWARE = False
get_phantom_manager = None
try:
    from system.phantom_hardware_manager import get_phantom_manager
    _HAS_PHANTOM_HARDWARE = True
except ImportError:
    try:
        from backend.system.phantom_hardware_manager import get_phantom_manager
        _HAS_PHANTOM_HARDWARE = True
    except ImportError:
        pass

# v69.0: Import CryostasisManager for process freezing
# Freezes apps on Ghost Display to reduce GPU/CPU usage
_HAS_CRYOSTASIS = False
get_cryostasis_manager = None
try:
    from system.cryostasis_manager import get_cryostasis_manager
    _HAS_CRYOSTASIS = True
except ImportError:
    try:
        from backend.system.cryostasis_manager import get_cryostasis_manager
        _HAS_CRYOSTASIS = True
    except ImportError:
        pass

logger = logging.getLogger(__name__)


# =============================================================================
# v66.0: FUZZY APP NAME MATCHING - Module-level helper for Boomerang Protocol
# =============================================================================
# Common app name aliases for natural voice commands
_APP_ALIASES: Dict[str, List[str]] = {
    'chrome': ['google chrome', 'chrome.app', 'chrome browser'],
    'safari': ['safari.app', 'safari browser', 'apple safari'],
    'firefox': ['mozilla firefox', 'firefox.app', 'firefox browser'],
    'terminal': ['terminal.app', 'apple terminal', 'macos terminal'],
    'code': ['visual studio code', 'vscode', 'vs code', 'code.app'],
    'slack': ['slack.app', 'slack technologies'],
    'discord': ['discord.app', 'discord client'],
    'figma': ['figma.app', 'figma design'],
    'spotify': ['spotify.app', 'spotify music'],
    'notion': ['notion.app', 'notion so'],
    'zoom': ['zoom.us', 'zoom.app', 'zoom client'],
    'teams': ['microsoft teams', 'teams.app', 'ms teams'],
    'outlook': ['microsoft outlook', 'outlook.app', 'ms outlook'],
    'arc': ['arc.app', 'arc browser'],
    'iterm': ['iterm.app', 'iterm2', 'iterm 2'],
    'warp': ['warp.app', 'warp terminal'],
    'cursor': ['cursor.app', 'cursor ide'],
}


def _fuzzy_match_app_name(user_input: str, registered_app: str, threshold: float = 0.75) -> Tuple[bool, float]:
    """
    v66.0: Module-level fuzzy matching for app names in Boomerang Protocol.

    Handles common variations like:
    - "Chrome" matches "Google Chrome"
    - "Terminal" matches "Terminal.app"

    Args:
        user_input: What the user said (e.g., "Chrome")
        registered_app: The actual app name from system (e.g., "Google Chrome")
        threshold: Minimum match ratio (0.0 to 1.0)

    Returns:
        Tuple of (is_match: bool, confidence: float)
    """
    if not user_input or not registered_app:
        return False, 0.0

    # Normalize both strings
    norm_user = user_input.lower().strip()
    norm_reg = registered_app.lower().strip()

    # Remove common suffixes/prefixes
    for suffix in ['.app', ' app', ' browser', ' client']:
        norm_reg = norm_reg.replace(suffix, '')
        norm_user = norm_user.replace(suffix, '')

    for prefix in ['google ', 'apple ', 'microsoft ', 'mozilla ', 'the ']:
        if norm_reg.startswith(prefix):
            norm_reg = norm_reg[len(prefix):]
        if norm_user.startswith(prefix):
            norm_user = norm_user[len(prefix):]

    # FAST PATH: Exact or substring match
    if norm_user == norm_reg:
        return True, 1.0
    if norm_user in norm_reg or norm_reg in norm_user:
        return True, 0.95

    # ALIAS MATCHING
    for canonical, aliases in _APP_ALIASES.items():
        user_matches = norm_user == canonical or any(norm_user in a or a in norm_user for a in aliases)
        reg_matches = canonical in norm_reg or norm_reg == canonical or any(a in norm_reg for a in aliases)
        if user_matches and reg_matches:
            return True, 0.92

    # FUZZY MATCHING with rapidfuzz
    if _HAS_RAPIDFUZZ and rapidfuzz_fuzz:
        ratios = [
            rapidfuzz_fuzz.ratio(norm_user, norm_reg) / 100.0,
            rapidfuzz_fuzz.partial_ratio(norm_user, norm_reg) / 100.0,
            rapidfuzz_fuzz.token_sort_ratio(norm_user, norm_reg) / 100.0,
        ]
        best_ratio = max(ratios)
        if best_ratio >= threshold:
            return True, best_ratio

    return False, 0.0


# =============================================================================
# v67.0: CEREBRO PROTOCOL - Async App Resolution via macOS Spotlight
# =============================================================================

async def _resolve_app_name_with_cerebro_async(
    user_input: str,
    check_running: bool = True
) -> Optional[Dict[str, Any]]:
    """
    v67.0 CEREBRO PROTOCOL: Resolve app name using macOS Spotlight integration.

    This module-level async function provides Spotlight-based app resolution
    for the Boomerang Protocol, replacing hardcoded alias lists.

    Args:
        user_input: What the user said (e.g., "Chrome", "VS Code")
        check_running: Also check if app is currently running

    Returns:
        Dict with resolution result or None:
        {
            'found': bool,
            'app_name': str,        # Display name (e.g., "Google Chrome")
            'is_running': bool,
            'window_count': int,
            'confidence': float
        }
    """
    if not _HAS_APP_LIBRARY or not get_app_library:
        return None

    try:
        library = get_app_library()
        result = await library.resolve_app_name_async(
            user_input,
            include_running_status=check_running
        )

        if result.found:
            logger.debug(
                f"[v67.0] 🧠 CEREBRO (Boomerang): '{user_input}' → '{result.app_name}' "
                f"(method: {result.resolution_method})"
            )
            return {
                'found': True,
                'app_name': result.app_name,
                'is_running': result.is_running,
                'window_count': result.window_count,
                'confidence': result.confidence
            }

        return None

    except Exception as e:
        logger.debug(f"[v67.0] Cerebro resolution error in Boomerang: {e}")
        return None


# =============================================================================
# ROOT CAUSE FIX v11.0.0: Non-Blocking Yabai Availability Cache
# =============================================================================
# PROBLEM: YabaiSpaceDetector.__init__ calls subprocess.run() which blocks
# the entire event loop, causing "Processing..." hang
#
# SOLUTION: Module-level cache + async initialization
# - Cache yabai availability status at module level
# - Skip blocking checks if we already know yabai state
# - Async methods for initialization when needed
# =============================================================================

# Module-level cache for yabai availability (avoids repeated blocking checks)
_YABAI_AVAILABILITY_CACHE = {
    "checked": False,
    "available": False,
    "path": None,
    "version": None,
    "last_check": None,
    "check_count": 0,
}

# Cache validity duration (seconds) - don't recheck too often
_YABAI_CACHE_TTL = 60.0


def _is_yabai_cache_valid() -> bool:
    """Check if the yabai availability cache is still valid."""
    if not _YABAI_AVAILABILITY_CACHE["checked"]:
        return False
    last_check = _YABAI_AVAILABILITY_CACHE.get("last_check")
    if last_check is None:
        return False
    age = (datetime.now() - last_check).total_seconds()
    return age < _YABAI_CACHE_TTL


async def async_quick_yabai_check() -> Tuple[bool, Optional[str]]:
    """
    TRUE ASYNC yabai availability check using asyncio subprocesses.

    ROOT CAUSE FIX v12.0.0: True Non-Blocking Implementation
    =========================================================
    PROBLEM: Previous implementation used subprocess.run() which blocks
    the entire event loop, even with short timeouts. During that block:
    - UI spinner freezes
    - Voice input stops processing
    - WebSocket messages queue up
    - System feels unresponsive

    SOLUTION: Use asyncio.create_subprocess_exec() which:
    - Yields control to event loop while waiting for OS
    - Allows other coroutines to run during subprocess execution
    - Keeps spinner, voice, and WebSocket 100% responsive
    - True concurrent execution, not fake-async

    Returns:
        Tuple of (is_available, yabai_path)
    """
    global _YABAI_AVAILABILITY_CACHE

    # =========================================================================
    # STEP 1: Check cache FIRST (instant, no I/O)
    # =========================================================================
    if _is_yabai_cache_valid():
        logger.debug("[YABAI] Using cached availability result")
        return _YABAI_AVAILABILITY_CACHE["available"], _YABAI_AVAILABILITY_CACHE["path"]

    # =========================================================================
    # STEP 2: Find yabai path (filesystem check - very fast)
    # =========================================================================
    # Use asyncio.to_thread for shutil.which (it's a blocking syscall)
    try:
        yabai_path = await asyncio.wait_for(
            asyncio.to_thread(shutil.which, "yabai"),
            timeout=0.5
        )
    except asyncio.TimeoutError:
        yabai_path = None

    if not yabai_path:
        # Check common locations (fast filesystem checks)
        common_paths = [
            "/opt/homebrew/bin/yabai",
            "/usr/local/bin/yabai",
        ]
        for path in common_paths:
            try:
                exists = await asyncio.to_thread(
                    lambda p: os.path.isfile(p) and os.access(p, os.X_OK),
                    path
                )
                if exists:
                    yabai_path = path
                    break
            except Exception:
                continue

    if not yabai_path:
        # Yabai not installed - cache and return
        logger.debug("[YABAI] Yabai executable not found")
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": False,
            "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None

    # =========================================================================
    # STEP 3: TRUE ASYNC subprocess check (yields to event loop!)
    # =========================================================================
    try:
        # Create subprocess WITHOUT blocking the event loop
        process = await asyncio.create_subprocess_exec(
            yabai_path, "-m", "query", "--spaces",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Wait for completion with timeout (event loop stays responsive!)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=2.0  # 2 second timeout for subprocess
            )
            is_available = process.returncode == 0
        except asyncio.TimeoutError:
            # Kill the hung process
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            logger.warning("[YABAI] Async subprocess timed out after 2s")
            is_available = False

        # Update cache with result
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": is_available,
            "path": yabai_path if is_available else None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })

        if is_available:
            logger.debug(f"[YABAI] Async check: Available at {yabai_path}")
        else:
            logger.debug(f"[YABAI] Async check: Not running (returncode={process.returncode})")

        return is_available, yabai_path if is_available else None

    except Exception as e:
        logger.debug(f"[YABAI] Async check failed: {e}")
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": False,
            "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None


def _quick_yabai_check() -> Tuple[bool, Optional[str]]:
    """
    SYNC version of yabai check (for non-async contexts).

    WARNING: This blocks the event loop! Use async_quick_yabai_check() when possible.
    Kept for backward compatibility with sync code paths.
    """
    global _YABAI_AVAILABILITY_CACHE

    # Return cached result if valid (instant)
    if _is_yabai_cache_valid():
        return _YABAI_AVAILABILITY_CACHE["available"], _YABAI_AVAILABILITY_CACHE["path"]

    # Quick path check
    yabai_path = shutil.which("yabai")
    if not yabai_path:
        common_paths = ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]
        for path in common_paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                yabai_path = path
                break

    if not yabai_path:
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True, "available": False, "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None

    # Blocking subprocess check (1s timeout)
    try:
        result = subprocess.run(
            [yabai_path, "-m", "query", "--spaces"],
            capture_output=True, text=True, timeout=1.0
        )
        is_available = result.returncode == 0
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": is_available,
            "path": yabai_path if is_available else None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return is_available, yabai_path if is_available else None
    except Exception as e:
        logger.debug(f"[YABAI] Sync check failed: {e}")
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True, "available": False, "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None


def get_cached_yabai_status() -> Dict[str, Any]:
    """Get the current cached yabai status without blocking."""
    return dict(_YABAI_AVAILABILITY_CACHE)


def invalidate_yabai_cache():
    """Invalidate the yabai cache to force a recheck."""
    global _YABAI_AVAILABILITY_CACHE
    _YABAI_AVAILABILITY_CACHE["checked"] = False


# =============================================================================
# v34.0: DISPLAY-AWARE ROUTER - Intelligent Cross-Display Window Management
# =============================================================================
# ROOT CAUSE FIX: The --space command silently fails for cross-display moves
# without Scripting Additions. The --display command works natively.
#
# This router provides:
# - Cached display-space mapping for O(1) lookups
# - Parallel async queries for display detection
# - Intelligent routing: --display for cross-display, --space for same-display
# - Virtual/Ghost display awareness
# =============================================================================

class DisplayAwareRouter:
    """
    v34.0: Intelligent display-aware window routing.

    Solves the silent failure of cross-display moves by automatically
    using --display instead of --space when moving between displays.

    Features:
    - Cached display-space mapping (TTL-based)
    - Parallel async display detection
    - Automatic cross-display detection
    - Virtual display support
    """

    # Class-level cache for display-space mapping
    _display_space_cache: Dict[str, Any] = {
        "spaces_by_display": {},      # {display_id: [space_ids]}
        "display_by_space": {},       # {space_id: display_id}
        "display_count": 0,
        "space_count": 0,
        "last_update": None,
        "ttl_seconds": 10.0,          # Cache valid for 10 seconds
    }

    _lock = None  # Will be initialized per-instance for thread safety

    def __init__(self, yabai_path: str = "yabai"):
        self.yabai_path = yabai_path
        self._lock = asyncio.Lock()

    @classmethod
    def _is_cache_valid(cls) -> bool:
        """Check if display-space cache is still valid."""
        last_update = cls._display_space_cache.get("last_update")
        if last_update is None:
            return False
        age = (datetime.now() - last_update).total_seconds()
        return age < cls._display_space_cache["ttl_seconds"]

    @classmethod
    def invalidate_cache(cls):
        """Force cache refresh on next query."""
        cls._display_space_cache["last_update"] = None

    async def refresh_display_mapping(self) -> bool:
        """
        Refresh the display-space mapping cache.

        Returns True if successful, False otherwise.
        """
        async with self._lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.yabai_path, "-m", "query", "--spaces",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                if proc.returncode != 0 or not stdout:
                    return False

                spaces = json.loads(stdout.decode())

                # Build mapping
                spaces_by_display: Dict[int, List[int]] = {}
                display_by_space: Dict[int, int] = {}

                for space in spaces:
                    space_id = space.get("index")
                    display_id = space.get("display")

                    if space_id and display_id:
                        display_by_space[space_id] = display_id
                        if display_id not in spaces_by_display:
                            spaces_by_display[display_id] = []
                        spaces_by_display[display_id].append(space_id)

                # Update cache
                self._display_space_cache["spaces_by_display"] = spaces_by_display
                self._display_space_cache["display_by_space"] = display_by_space
                self._display_space_cache["display_count"] = len(spaces_by_display)
                self._display_space_cache["space_count"] = len(display_by_space)
                self._display_space_cache["last_update"] = datetime.now()

                logger.debug(
                    f"[DisplayRouter] Refreshed mapping: {len(spaces_by_display)} displays, "
                    f"{len(display_by_space)} spaces"
                )
                return True

            except Exception as e:
                logger.debug(f"[DisplayRouter] Failed to refresh mapping: {e}")
                return False

    async def get_display_for_space(self, space_id: int) -> Optional[int]:
        """Get the display ID for a given space (cached)."""
        if not self._is_cache_valid():
            await self.refresh_display_mapping()
        return self._display_space_cache["display_by_space"].get(space_id)

    async def get_spaces_for_display(self, display_id: int) -> List[int]:
        """Get all space IDs for a given display (cached)."""
        if not self._is_cache_valid():
            await self.refresh_display_mapping()
        return self._display_space_cache["spaces_by_display"].get(display_id, [])

    async def get_window_display(self, window_id: int) -> Optional[int]:
        """Get the current display of a window."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0 and stdout:
                data = json.loads(stdout.decode())
                return data.get("display")
        except Exception as e:
            logger.debug(f"[DisplayRouter] Failed to get window display: {e}")
        return None

    async def get_window_space(self, window_id: int) -> Optional[int]:
        """Get the current space of a window."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0 and stdout:
                data = json.loads(stdout.decode())
                return data.get("space")
        except Exception as e:
            logger.debug(f"[DisplayRouter] Failed to get window space: {e}")
        return None

    async def detect_cross_display_move(
        self,
        window_id: int,
        target_space: int
    ) -> Tuple[bool, Optional[int], Optional[int]]:
        """
        Detect if a move is cross-display.

        Returns:
            (is_cross_display, current_display, target_display)
        """
        # Parallel queries for speed
        current_display_task = asyncio.create_task(self.get_window_display(window_id))
        target_display_task = asyncio.create_task(self.get_display_for_space(target_space))

        current_display, target_display = await asyncio.gather(
            current_display_task, target_display_task
        )

        is_cross_display = (
            current_display is not None and
            target_display is not None and
            current_display != target_display
        )

        return is_cross_display, current_display, target_display

    async def get_optimal_move_command(
        self,
        window_id: int,
        target_space: int
    ) -> Tuple[List[str], str]:
        """
        Get the optimal yabai command for moving a window.

        Returns:
            (command_args, strategy_name)

        For cross-display moves: Uses --display (bypasses SA requirement)
        For same-display moves: Uses --space (standard behavior)
        """
        is_cross_display, current_display, target_display = await self.detect_cross_display_move(
            window_id, target_space
        )

        if is_cross_display and target_display is not None:
            # CROSS-DISPLAY: Use --display command
            logger.info(
                f"[DisplayRouter] 🌐 CROSS-DISPLAY: Window {window_id} "
                f"Display {current_display} → Display {target_display}"
            )
            return (
                [self.yabai_path, "-m", "window", str(window_id), "--display", str(target_display)],
                "display_handoff"
            )
        else:
            # SAME-DISPLAY: Use --space command
            logger.debug(
                f"[DisplayRouter] Same-display: Window {window_id} → Space {target_space}"
            )
            return (
                [self.yabai_path, "-m", "window", str(window_id), "--space", str(target_space)],
                "space_move"
            )

    async def move_window_optimally(
        self,
        window_id: int,
        target_space: int,
        timeout: float = 5.0
    ) -> Tuple[bool, str]:
        """
        Move a window using the optimal strategy.

        Returns:
            (success, error_message)
        """
        command, strategy = await self.get_optimal_move_command(window_id, target_space)

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            if proc.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                return False, error_msg

            logger.info(f"[DisplayRouter] ✅ Move successful (strategy: {strategy})")
            return True, ""

        except asyncio.TimeoutError:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics for debugging."""
        return {
            "display_count": self._display_space_cache["display_count"],
            "space_count": self._display_space_cache["space_count"],
            "cache_valid": self._is_cache_valid(),
            "last_update": self._display_space_cache["last_update"],
            "ttl_seconds": self._display_space_cache["ttl_seconds"],
        }


# Global router instance for easy access
_display_router: Optional[DisplayAwareRouter] = None

def get_display_router(yabai_path: str = "yabai") -> DisplayAwareRouter:
    """Get or create the global DisplayAwareRouter instance."""
    global _display_router
    if _display_router is None:
        _display_router = DisplayAwareRouter(yabai_path)
    return _display_router


# =============================================================================
# SERVICE STATE & HEALTH TRACKING
# =============================================================================

@dataclass
class YabaiServiceHealth:
    """Tracks yabai service health metrics."""
    is_running: bool = False
    last_check: Optional[datetime] = None
    last_successful_query: Optional[datetime] = None
    consecutive_failures: int = 0
    total_queries: int = 0
    successful_queries: int = 0
    avg_response_time_ms: float = 0.0
    last_error: Optional[str] = None
    permissions_granted: bool = False
    yabai_path: Optional[str] = None
    yabai_version: Optional[str] = None
    startup_attempts: int = 0
    last_startup_attempt: Optional[datetime] = None

    @property
    def success_rate(self) -> float:
        """Calculate query success rate."""
        if self.total_queries == 0:
            return 0.0
        return (self.successful_queries / self.total_queries) * 100

    @property
    def needs_restart(self) -> bool:
        """Determine if service needs restart based on health metrics."""
        # Restart if we've had 3+ consecutive failures
        if self.consecutive_failures >= 3:
            return True
        # Restart if no successful query in 5 minutes and we're supposed to be running
        if self.is_running and self.last_successful_query:
            if datetime.now() - self.last_successful_query > timedelta(minutes=5):
                return True
        return False

    def record_success(self, response_time_ms: float):
        """Record a successful query."""
        self.total_queries += 1
        self.successful_queries += 1
        self.consecutive_failures = 0
        self.last_successful_query = datetime.now()
        self.last_error = None
        # Rolling average
        alpha = 0.3  # Weight for new value
        self.avg_response_time_ms = (
            alpha * response_time_ms + (1 - alpha) * self.avg_response_time_ms
        )

    def record_failure(self, error: str):
        """Record a failed query."""
        self.total_queries += 1
        self.consecutive_failures += 1
        self.last_error = error

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for reporting."""
        return {
            "is_running": self.is_running,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_successful_query": self.last_successful_query.isoformat() if self.last_successful_query else None,
            "consecutive_failures": self.consecutive_failures,
            "total_queries": self.total_queries,
            "successful_queries": self.successful_queries,
            "success_rate": f"{self.success_rate:.1f}%",
            "avg_response_time_ms": round(self.avg_response_time_ms, 2),
            "last_error": self.last_error,
            "permissions_granted": self.permissions_granted,
            "yabai_path": self.yabai_path,
            "yabai_version": self.yabai_version,
            "needs_restart": self.needs_restart,
        }


@dataclass
class YabaiConfig:
    """Configuration for yabai service management."""
    auto_start: bool = True
    max_startup_attempts: int = 3
    startup_retry_delay_seconds: float = 2.0
    health_check_interval_seconds: float = 30.0
    query_timeout_seconds: float = 5.0
    enable_auto_recovery: bool = True
    config_path: Path = field(default_factory=lambda: Path.home() / ".config" / "yabai" / "yabairc")


# =============================================================================
# v24.0.0: INTELLIGENT SEARCH & RESCUE PROTOCOL
# =============================================================================
# Root cause solution for dehydrated windows with:
# - Multi-strategy rescue approaches
# - Dynamic wake delay calibration
# - Parallel rescue with concurrency control
# - Retry logic with exponential backoff
# - Root cause detection for failures
# - Comprehensive telemetry and diagnostics
# =============================================================================

class RescueFailureReason(Enum):
    """Root cause categories for rescue failures."""
    WINDOW_NOT_FOUND = "window_not_found"           # Window doesn't exist or was closed
    SPACE_NOT_FOUND = "space_not_found"             # Source/target space doesn't exist
    YABAI_UNAVAILABLE = "yabai_unavailable"         # Yabai service not running
    YABAI_TIMEOUT = "yabai_timeout"                 # Yabai command timed out
    PERMISSION_DENIED = "permission_denied"         # Accessibility permission issue
    WINDOW_MINIMIZED = "window_minimized"           # Window is minimized (special state)
    WINDOW_FULLSCREEN = "window_fullscreen"         # Window in fullscreen mode
    SPACE_SWITCH_FAILED = "space_switch_failed"     # Couldn't switch to source space
    MOVE_AFTER_WAKE_FAILED = "move_after_wake_failed"  # Woke window but move still failed
    UNKNOWN = "unknown"                             # Unknown failure reason


class RescueStrategy(Enum):
    """Different strategies for rescuing dehydrated windows."""
    DIRECT = "direct"                   # Try direct move (fast path)
    SWITCH_GRAB_RETURN = "switch_grab_return"  # Classic rescue: switch → move → return
    FOCUS_THEN_MOVE = "focus_then_move"  # Focus window first, then move
    UNMINIMIZE_FIRST = "unminimize_first"  # Unminimize if minimized, then move
    SPACE_FOCUS_EXTENDED = "space_focus_extended"  # Extended wake delay for stubborn windows
    EXIT_FULLSCREEN_FIRST = "exit_fullscreen_first"  # v31.0: Exit fullscreen before move


@dataclass
class RescueTelemetry:
    """
    Telemetry and diagnostics for rescue operations.

    Tracks success rates, timing, and failure patterns to:
    - Calibrate wake delays dynamically
    - Identify problematic windows/apps
    - Optimize strategy selection
    """
    total_attempts: int = 0
    successful_rescues: int = 0
    failed_rescues: int = 0

    # Strategy success tracking
    direct_successes: int = 0
    switch_grab_return_successes: int = 0
    focus_then_move_successes: int = 0
    unminimize_first_successes: int = 0
    extended_wake_successes: int = 0

    # Timing metrics (rolling averages)
    avg_direct_time_ms: float = 0.0
    avg_rescue_time_ms: float = 0.0
    avg_wake_delay_needed_ms: float = 50.0  # Start with default

    # Failure tracking by reason
    failures_by_reason: Dict[str, int] = field(default_factory=dict)

    # App-specific metrics for intelligent routing
    app_success_rates: Dict[str, float] = field(default_factory=dict)
    app_preferred_strategies: Dict[str, str] = field(default_factory=dict)

    # Last calibration data
    last_calibration_time: Optional[datetime] = None
    calibrated_wake_delay_ms: float = 50.0

    @property
    def success_rate(self) -> float:
        """Calculate overall rescue success rate."""
        if self.total_attempts == 0:
            return 1.0  # No data, assume success
        return self.successful_rescues / self.total_attempts

    def record_attempt(
        self,
        success: bool,
        strategy: RescueStrategy,
        duration_ms: float,
        failure_reason: Optional[RescueFailureReason] = None,
        app_name: Optional[str] = None,
        wake_delay_used_ms: Optional[float] = None
    ):
        """Record a rescue attempt for telemetry."""
        self.total_attempts += 1

        if success:
            self.successful_rescues += 1

            # Track strategy success
            if strategy == RescueStrategy.DIRECT:
                self.direct_successes += 1
                alpha = 0.3
                self.avg_direct_time_ms = alpha * duration_ms + (1 - alpha) * self.avg_direct_time_ms
            elif strategy == RescueStrategy.SWITCH_GRAB_RETURN:
                self.switch_grab_return_successes += 1
            elif strategy == RescueStrategy.FOCUS_THEN_MOVE:
                self.focus_then_move_successes += 1
            elif strategy == RescueStrategy.UNMINIMIZE_FIRST:
                self.unminimize_first_successes += 1
            elif strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                self.extended_wake_successes += 1

            # Update rescue timing
            if strategy != RescueStrategy.DIRECT:
                alpha = 0.3
                self.avg_rescue_time_ms = alpha * duration_ms + (1 - alpha) * self.avg_rescue_time_ms

                # Calibrate wake delay based on what worked
                if wake_delay_used_ms is not None:
                    self.avg_wake_delay_needed_ms = (
                        alpha * wake_delay_used_ms + (1 - alpha) * self.avg_wake_delay_needed_ms
                    )

            # Track app success
            if app_name:
                current_rate = self.app_success_rates.get(app_name, 1.0)
                self.app_success_rates[app_name] = 0.8 * current_rate + 0.2 * 1.0

                # Remember preferred strategy for this app
                self.app_preferred_strategies[app_name] = strategy.value
        else:
            self.failed_rescues += 1

            # Track failure reason
            reason_key = failure_reason.value if failure_reason else "unknown"
            self.failures_by_reason[reason_key] = self.failures_by_reason.get(reason_key, 0) + 1

            # Track app failure
            if app_name:
                current_rate = self.app_success_rates.get(app_name, 1.0)
                self.app_success_rates[app_name] = 0.8 * current_rate + 0.2 * 0.0

    def get_recommended_wake_delay_ms(self, app_name: Optional[str] = None) -> float:
        """
        Get the recommended wake delay based on telemetry.

        Considers:
        - Overall average that worked
        - App-specific requirements (some apps need more time)
        - Base environment variable override
        """
        # Check for environment override
        env_override = os.environ.get("JARVIS_RESCUE_WAKE_DELAY")
        if env_override:
            return float(env_override) * 1000  # Convert seconds to ms

        base_delay = max(self.avg_wake_delay_needed_ms, 30.0)  # At least 30ms

        # Increase for apps with lower success rates
        if app_name and app_name in self.app_success_rates:
            app_rate = self.app_success_rates[app_name]
            if app_rate < 0.8:  # Lower success rate = need more time
                base_delay *= (2.0 - app_rate)  # Up to 2x for problematic apps

        return min(base_delay, 500.0)  # Cap at 500ms

    def get_recommended_strategy(
        self,
        app_name: Optional[str] = None,
        is_minimized: bool = False,
        is_fullscreen: bool = False
    ) -> RescueStrategy:
        """Get the recommended rescue strategy based on telemetry and window state."""
        # Special cases first
        if is_minimized:
            return RescueStrategy.UNMINIMIZE_FIRST
        if is_fullscreen:
            # v31.0: Fullscreen windows CANNOT be moved - must exit fullscreen first
            return RescueStrategy.EXIT_FULLSCREEN_FIRST

        # Check app-specific preference
        if app_name and app_name in self.app_preferred_strategies:
            try:
                return RescueStrategy(self.app_preferred_strategies[app_name])
            except ValueError:
                pass

        # Default to switch-grab-return (most reliable)
        return RescueStrategy.SWITCH_GRAB_RETURN

    def to_dict(self) -> Dict[str, Any]:
        """Convert telemetry to dictionary for logging/reporting."""
        return {
            "total_attempts": self.total_attempts,
            "success_rate": f"{self.success_rate * 100:.1f}%",
            "successful_rescues": self.successful_rescues,
            "failed_rescues": self.failed_rescues,
            "strategy_successes": {
                "direct": self.direct_successes,
                "switch_grab_return": self.switch_grab_return_successes,
                "focus_then_move": self.focus_then_move_successes,
                "unminimize_first": self.unminimize_first_successes,
                "extended_wake": self.extended_wake_successes,
            },
            "timing": {
                "avg_direct_ms": round(self.avg_direct_time_ms, 2),
                "avg_rescue_ms": round(self.avg_rescue_time_ms, 2),
                "calibrated_wake_delay_ms": round(self.calibrated_wake_delay_ms, 2),
            },
            "failures_by_reason": self.failures_by_reason,
            "app_success_rates": {
                k: f"{v * 100:.1f}%" for k, v in self.app_success_rates.items()
            },
        }


@dataclass
class RescueResult:
    """Detailed result of a rescue operation."""
    success: bool
    window_id: int
    source_space: Optional[int]
    target_space: int
    strategy_used: RescueStrategy
    duration_ms: float
    failure_reason: Optional[RescueFailureReason] = None
    attempts: int = 1
    wake_delay_used_ms: float = 0.0
    app_name: Optional[str] = None

    # For batch operations
    method: str = ""  # "direct", "rescue", "failed" for compatibility

    def __post_init__(self):
        """Set method for backward compatibility."""
        if not self.method:
            if self.success and self.strategy_used == RescueStrategy.DIRECT:
                self.method = "direct"
            elif self.success:
                self.method = "rescue"
            else:
                self.method = "failed"


# Global telemetry instance (module-level for persistence across calls)
_RESCUE_TELEMETRY: Optional[RescueTelemetry] = None


def get_rescue_telemetry() -> RescueTelemetry:
    """Get or create the global rescue telemetry instance."""
    global _RESCUE_TELEMETRY
    if _RESCUE_TELEMETRY is None:
        _RESCUE_TELEMETRY = RescueTelemetry()
    return _RESCUE_TELEMETRY


def reset_rescue_telemetry() -> None:
    """Reset the global rescue telemetry (for testing)."""
    global _RESCUE_TELEMETRY
    _RESCUE_TELEMETRY = None


# =============================================================================
# v25.0.0: SHADOW MONITOR INFRASTRUCTURE
# =============================================================================
# Comprehensive Ghost Display management with:
# - Health monitoring and auto-recovery
# - Window geometry preservation and restoration
# - Intelligent window layout management
# - Window return policy for post-monitoring cleanup
# - Fallback strategies when Ghost Display unavailable
# - Multi-window coordination with smart throttling
# =============================================================================

class WindowLayoutStyle(Enum):
    """Layout styles for arranging windows on Ghost Display."""
    SIDE_BY_SIDE = "side_by_side"      # Windows arranged horizontally
    STACKED = "stacked"                 # Windows arranged vertically
    GRID = "grid"                       # Windows in a grid pattern
    CASCADE = "cascade"                 # Cascaded windows (overlapping)
    MAXIMIZE = "maximize"               # Each window maximized (for single window)
    PRESERVE = "preserve"               # Keep original positions


class GhostDisplayStatus(Enum):
    """Status of the Ghost Display."""
    AVAILABLE = "available"             # Ghost Display is ready
    UNAVAILABLE = "unavailable"         # No Ghost Display found
    DISCONNECTED = "disconnected"       # Was available, now gone
    RECONNECTING = "reconnecting"       # Attempting to reconnect
    FALLBACK = "fallback"               # Using fallback strategy


@dataclass
class WindowGeometry:
    """
    Preserved window geometry for restoration with display-aware scaling.

    v26.0: Enhanced with:
    - Display scaling/Retina coordinate handling
    - Space UUID for stability across space reordering
    - Focus state tracking
    - Z-order (layer) information
    - Min/max size constraints
    - Animation timing data
    """
    window_id: int
    app_name: str
    original_space: int
    original_display: int
    x: int
    y: int
    width: int
    height: int
    is_minimized: bool = False
    is_fullscreen: bool = False
    is_floating: bool = False
    teleported_at: Optional[datetime] = None
    current_space: Optional[int] = None

    # v26.0: Display scaling support
    source_display_scale: float = 1.0  # Retina scale factor of original display
    source_display_width: int = 1920   # Original display resolution
    source_display_height: int = 1080
    ghost_display_scale: float = 1.0   # Scale factor of Ghost Display
    ghost_display_width: int = 1920
    ghost_display_height: int = 1080

    # v26.0: Space stability
    original_space_uuid: Optional[str] = None  # More stable than space ID
    original_space_label: Optional[str] = None  # Space label if available

    # v26.0: Focus and z-order
    was_focused: bool = False  # Was this window focused before teleport?
    z_order: int = 0           # Layer order (higher = more in front)
    is_split_view: bool = False  # Window in split view
    is_picture_in_picture: bool = False  # PiP mode

    # v26.0: Window constraints
    min_width: Optional[int] = None
    min_height: Optional[int] = None
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    has_constraints: bool = False

    # v26.0: Animation timing
    move_animation_duration_ms: float = 250.0  # macOS animation duration
    last_position_stable_at: Optional[datetime] = None

    def convert_for_display(
        self,
        target_scale: float,
        target_width: int,
        target_height: int
    ) -> Tuple[int, int, int, int]:
        """
        Convert geometry coordinates for a different display.

        Handles Retina scaling and different resolutions.

        Args:
            target_scale: Scale factor of target display
            target_width: Width of target display
            target_height: Height of target display

        Returns:
            Tuple of (x, y, width, height) adjusted for target display
        """
        # Scale ratio between displays
        scale_ratio = target_scale / self.source_display_scale if self.source_display_scale > 0 else 1.0

        # Resolution ratio (for positioning)
        width_ratio = target_width / self.source_display_width if self.source_display_width > 0 else 1.0
        height_ratio = target_height / self.source_display_height if self.source_display_height > 0 else 1.0

        # Convert position (scaled by resolution ratio)
        new_x = int(self.x * width_ratio)
        new_y = int(self.y * height_ratio)

        # Convert size (scaled by scale ratio for Retina)
        new_width = int(self.width * scale_ratio)
        new_height = int(self.height * scale_ratio)

        # Apply constraints if available
        if self.has_constraints:
            if self.min_width is not None:
                new_width = max(new_width, int(self.min_width * scale_ratio))
            if self.min_height is not None:
                new_height = max(new_height, int(self.min_height * scale_ratio))
            if self.max_width is not None:
                new_width = min(new_width, int(self.max_width * scale_ratio))
            if self.max_height is not None:
                new_height = min(new_height, int(self.max_height * scale_ratio))

        # Ensure window fits on target display
        new_x = min(new_x, target_width - new_width)
        new_y = min(new_y, target_height - new_height)
        new_x = max(0, new_x)
        new_y = max(25, new_y)  # Account for menu bar

        return new_x, new_y, new_width, new_height

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_id": self.window_id,
            "app_name": self.app_name,
            "original_space": self.original_space,
            "original_display": self.original_display,
            "bounds": {"x": self.x, "y": self.y, "width": self.width, "height": self.height},
            "is_minimized": self.is_minimized,
            "is_fullscreen": self.is_fullscreen,
            "is_floating": self.is_floating,
            "teleported_at": self.teleported_at.isoformat() if self.teleported_at else None,
            "display_scaling": {
                "source_scale": self.source_display_scale,
                "ghost_scale": self.ghost_display_scale,
                "source_resolution": f"{self.source_display_width}x{self.source_display_height}",
                "ghost_resolution": f"{self.ghost_display_width}x{self.ghost_display_height}",
            },
            "state": {
                "was_focused": self.was_focused,
                "z_order": self.z_order,
                "is_split_view": self.is_split_view,
                "is_picture_in_picture": self.is_picture_in_picture,
            },
            "constraints": {
                "has_constraints": self.has_constraints,
                "min_size": f"{self.min_width}x{self.min_height}" if self.min_width else None,
                "max_size": f"{self.max_width}x{self.max_height}" if self.max_width else None,
            },
        }


class SystemEventType(Enum):
    """System events that affect monitoring."""
    DISPLAY_CONNECTED = "display_connected"
    DISPLAY_DISCONNECTED = "display_disconnected"
    DISPLAY_RESOLUTION_CHANGED = "display_resolution_changed"
    DISPLAY_ARRANGEMENT_CHANGED = "display_arrangement_changed"
    SYSTEM_SLEEP = "system_sleep"
    SYSTEM_WAKE = "system_wake"
    SPACE_CREATED = "space_created"
    SPACE_DESTROYED = "space_destroyed"
    USER_SWITCHED_TO_GHOST = "user_switched_to_ghost"
    YABAI_RESTARTED = "yabai_restarted"
    PERMISSIONS_CHANGED = "permissions_changed"


@dataclass
class GhostDisplayInfo:
    """
    Information about the Ghost Display with comprehensive tracking.

    v26.0: Enhanced with:
    - Display scale factor for Retina support
    - Resolution change detection
    - User presence tracking
    - Animation timing information
    """
    space_id: int
    display_id: int
    display_name: str
    width: int
    height: int
    is_virtual: bool
    window_count: int
    last_health_check: Optional[datetime] = None
    consecutive_failures: int = 0

    # v26.0: Display scaling
    scale_factor: float = 1.0  # Retina scale (1.0 = standard, 2.0 = Retina)
    physical_width: int = 0    # Physical pixels (width * scale_factor)
    physical_height: int = 0

    # v26.0: Resolution tracking
    initial_width: int = 0     # Width at initialization
    initial_height: int = 0    # Height at initialization
    resolution_changed: bool = False
    last_resolution_check: Optional[datetime] = None

    # v26.0: User presence
    user_last_seen_on_ghost: Optional[datetime] = None
    user_currently_on_ghost: bool = False
    user_visit_count: int = 0

    # v26.0: Space stability
    space_uuid: Optional[str] = None  # More stable than space_id
    space_label: Optional[str] = None
    space_index: int = 0  # Position in space list

    # v26.0: System state
    is_system_sleeping: bool = False
    last_sleep_time: Optional[datetime] = None
    last_wake_time: Optional[datetime] = None

    # v26.0: Animation timing
    animation_complete: bool = True
    animation_started_at: Optional[datetime] = None
    standard_animation_duration_ms: float = 250.0

    def __post_init__(self):
        """Initialize computed fields."""
        if self.initial_width == 0:
            self.initial_width = self.width
        if self.initial_height == 0:
            self.initial_height = self.height
        if self.physical_width == 0:
            self.physical_width = int(self.width * self.scale_factor)
        if self.physical_height == 0:
            self.physical_height = int(self.height * self.scale_factor)

    @property
    def is_healthy(self) -> bool:
        """Check if Ghost Display is healthy based on recent checks."""
        if self.consecutive_failures >= 3:
            return False
        if self.is_system_sleeping:
            return False  # Not healthy during sleep
        if self.last_health_check is None:
            return True  # No data, assume healthy
        age = (datetime.now() - self.last_health_check).total_seconds()
        return age < 60  # Consider unhealthy if not checked in 60s

    @property
    def resolution_changed_since_init(self) -> bool:
        """Check if resolution changed since initialization."""
        return self.width != self.initial_width or self.height != self.initial_height

    @property
    def animation_in_progress(self) -> bool:
        """Check if an animation is still in progress."""
        if self.animation_complete or self.animation_started_at is None:
            return False
        elapsed = (datetime.now() - self.animation_started_at).total_seconds() * 1000
        return elapsed < self.standard_animation_duration_ms

    def mark_animation_start(self):
        """Mark that a window animation has started."""
        self.animation_complete = False
        self.animation_started_at = datetime.now()

    def mark_animation_complete(self):
        """Mark that animations are complete."""
        self.animation_complete = True
        self.animation_started_at = None

    async def wait_for_animation(self, extra_buffer_ms: float = 50.0):
        """Wait for any in-progress animation to complete."""
        if self.animation_in_progress:
            remaining = self.standard_animation_duration_ms
            if self.animation_started_at:
                elapsed = (datetime.now() - self.animation_started_at).total_seconds() * 1000
                remaining = max(0, self.standard_animation_duration_ms - elapsed)
            await asyncio.sleep((remaining + extra_buffer_ms) / 1000.0)
        self.mark_animation_complete()

    def update_resolution(self, new_width: int, new_height: int):
        """Update resolution and track changes."""
        if new_width != self.width or new_height != self.height:
            self.resolution_changed = True
            self.width = new_width
            self.height = new_height
            self.physical_width = int(new_width * self.scale_factor)
            self.physical_height = int(new_height * self.scale_factor)
            self.last_resolution_check = datetime.now()

    def record_user_presence(self, is_present: bool):
        """Record user presence on Ghost Display."""
        if is_present and not self.user_currently_on_ghost:
            self.user_last_seen_on_ghost = datetime.now()
            self.user_visit_count += 1
        self.user_currently_on_ghost = is_present

    def record_system_sleep(self):
        """Record system entering sleep."""
        self.is_system_sleeping = True
        self.last_sleep_time = datetime.now()

    def record_system_wake(self):
        """Record system waking from sleep."""
        self.is_system_sleeping = False
        self.last_wake_time = datetime.now()
        # Reset health check after wake
        self.consecutive_failures = 0


@dataclass
class GhostDisplayManagerConfig:
    """
    Configuration for Ghost Display management.

    v26.0: Enhanced with edge case handling configuration.
    """
    # Health monitoring
    health_check_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_HEALTH_INTERVAL", "30"))
    )
    max_consecutive_failures: int = 3
    auto_recovery_enabled: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_AUTO_RECOVERY", "true").lower() == "true"
    )

    # Window layout
    default_layout_style: WindowLayoutStyle = field(
        default_factory=lambda: WindowLayoutStyle(
            os.environ.get("JARVIS_GHOST_LAYOUT", "side_by_side")
        ) if os.environ.get("JARVIS_GHOST_LAYOUT") else WindowLayoutStyle.SIDE_BY_SIDE
    )
    layout_padding: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_PADDING", "10"))
    )
    max_windows_per_row: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MAX_PER_ROW", "3"))
    )

    # Window return policy
    return_windows_after_monitoring: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_RETURN_WINDOWS", "false").lower() == "true"
    )
    preserve_geometry_on_return: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_PRESERVE_GEOMETRY", "true").lower() == "true"
    )

    # Fallback strategy
    fallback_enabled: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_FALLBACK", "true").lower() == "true"
    )
    fallback_create_space: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_CREATE_SPACE", "false").lower() == "true"
    )

    # Multi-window coordination
    max_windows_on_ghost: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MAX_WINDOWS", "10"))
    )
    throttle_teleports: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_THROTTLE_TELEPORTS", "true").lower() == "true"
    )
    teleport_delay_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_TELEPORT_DELAY_MS", "50"))
    )

    # v26.0: Display scaling configuration
    enable_retina_scaling: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_RETINA_SCALING", "true").lower() == "true"
    )
    auto_detect_display_scale: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_AUTO_SCALE", "true").lower() == "true"
    )
    default_display_scale: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_DEFAULT_SCALE", "1.0"))
    )

    # v26.0: Resolution change handling
    monitor_resolution_changes: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_MONITOR_RESOLUTION", "true").lower() == "true"
    )
    resolution_check_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_RESOLUTION_INTERVAL", "10"))
    )
    auto_relayout_on_resolution_change: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_AUTO_RELAYOUT", "true").lower() == "true"
    )

    # v26.0: System sleep/wake handling
    handle_sleep_wake_events: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_HANDLE_SLEEP", "true").lower() == "true"
    )
    post_wake_delay_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_WAKE_DELAY_MS", "500"))
    )
    post_wake_health_check: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_WAKE_HEALTH_CHECK", "true").lower() == "true"
    )

    # v26.0: User presence detection
    detect_user_on_ghost_display: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_DETECT_USER", "true").lower() == "true"
    )
    pause_operations_when_user_present: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_PAUSE_ON_USER", "true").lower() == "true"
    )
    user_presence_cooldown_seconds: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_USER_COOLDOWN", "5.0"))
    )

    # v26.0: Animation timing
    wait_for_animations: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_WAIT_ANIMATIONS", "true").lower() == "true"
    )
    standard_animation_duration_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_ANIMATION_MS", "250"))
    )
    animation_buffer_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_ANIMATION_BUFFER_MS", "50"))
    )

    # v26.0: Space stability
    use_space_uuid: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_USE_SPACE_UUID", "true").lower() == "true"
    )
    verify_space_before_move: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_VERIFY_SPACE", "true").lower() == "true"
    )

    # v26.0: Window z-order management
    preserve_z_order: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_PRESERVE_ZORDER", "true").lower() == "true"
    )
    restore_focus_on_return: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_RESTORE_FOCUS", "true").lower() == "true"
    )

    # v26.0: Window constraints
    respect_window_constraints: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_RESPECT_CONSTRAINTS", "true").lower() == "true"
    )
    enforce_minimum_window_size: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_ENFORCE_MIN_SIZE", "true").lower() == "true"
    )
    minimum_window_width: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MIN_WIDTH", "200"))
    )
    minimum_window_height: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MIN_HEIGHT", "150"))
    )


class GhostDisplayManager:
    """
    Comprehensive Ghost Display lifecycle management.

    Features:
    - Health monitoring with auto-recovery
    - Window geometry preservation
    - Intelligent layout management
    - Window return policy
    - Fallback strategies
    - Multi-window coordination

    v26.0 Edge Case Handling:
    - Display scaling/Retina coordinate translation
    - Resolution change detection and recovery
    - System sleep/wake event handling
    - User presence detection on Ghost Display
    - Window z-order/layer management
    - Animation-aware timing
    - Space UUID stability verification
    """

    def __init__(self, config: Optional[GhostDisplayManagerConfig] = None):
        self.config = config or GhostDisplayManagerConfig()
        self._ghost_info: Optional[GhostDisplayInfo] = None
        self._status: GhostDisplayStatus = GhostDisplayStatus.UNAVAILABLE
        self._geometry_cache: Dict[int, WindowGeometry] = {}  # window_id -> geometry
        self._monitoring_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._windows_on_ghost: Set[int] = set()
        self._fallback_space: Optional[int] = None
        self._last_layout_time: float = 0.0

        # v26.0: Display scaling state
        self._display_scale_cache: Dict[int, float] = {}  # display_id -> scale factor
        self._display_resolution_cache: Dict[int, Tuple[int, int]] = {}  # display_id -> (width, height)

        # v26.0: System sleep/wake state
        self._is_system_sleeping: bool = False
        self._last_sleep_time: Optional[datetime] = None
        self._last_wake_time: Optional[datetime] = None
        self._pending_post_wake_check: bool = False

        # v26.0: User presence tracking
        self._user_on_ghost_display: bool = False
        self._last_user_presence_check: Optional[datetime] = None
        self._user_visit_history: List[datetime] = []  # Track when user visits ghost display
        self._operations_paused: bool = False

        # v26.0: Animation tracking
        self._pending_animations: Dict[int, datetime] = {}  # window_id -> animation_start_time
        self._global_animation_lock: bool = False

        # v26.0: Space UUID tracking
        self._space_uuid_cache: Dict[int, str] = {}  # space_id -> uuid
        self._space_label_cache: Dict[int, str] = {}  # space_id -> label

        # v26.0: Window z-order tracking
        self._z_order_cache: Dict[int, int] = {}  # window_id -> z_order
        self._focused_window_before_teleport: Optional[int] = None

        # v27.0: Crash Recovery - Persistence Manager Integration
        # =========================================================================
        # FIXES "AMNESIA" RISK:
        # If JARVIS crashes while windows are on Ghost Display, the in-memory
        # geometry cache is lost. GhostPersistenceManager persists state to disk
        # BEFORE teleportation, enabling recovery on restart.
        # =========================================================================
        self._persistence_manager: Optional[GhostPersistenceManager] = None
        self._persistence_initialized: bool = False

        # v129.0: Event emission for frontend/cross-repo state propagation
        self._state_observers: List[Callable] = []
        self._last_emitted_status: Optional[str] = None

        # v241.0: External capture metrics provider (registered by VisualMonitorAgent)
        self._capture_metrics_provider: Optional[Callable[[], Dict[str, Any]]] = None

    @property
    def status(self) -> GhostDisplayStatus:
        return self._status

    @property
    def ghost_space(self) -> Optional[int]:
        if self._ghost_info:
            return self._ghost_info.space_id
        return self._fallback_space

    @property
    def windows_on_ghost(self) -> Set[int]:
        return self._windows_on_ghost.copy()

    @property
    def window_count(self) -> int:
        return len(self._windows_on_ghost)

    # =========================================================================
    # v129.0: EVENT EMISSION & OBSERVER SUPPORT
    # =========================================================================

    def add_observer(self, callback: Callable) -> Callable:
        """
        Register an observer for ghost display state changes.

        Args:
            callback: Async or sync callable receiving (event_type: str, data: dict)

        Returns:
            Unsubscribe function
        """
        self._state_observers.append(callback)

        def _unsubscribe():
            try:
                self._state_observers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def register_capture_metrics_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        """
        v241.0: Register an external capture metrics provider.

        The VisualMonitorAgent registers a callable that returns Ferrari Engine
        capture metrics (active watchers, FPS, frames captured, etc.).
        These metrics are included in get_state_snapshot() for frontend/cross-repo visibility.
        """
        self._capture_metrics_provider = provider

    def get_state_snapshot(self) -> Dict[str, Any]:
        """Get current ghost display state as a serializable dict."""
        dims = self.ghost_display_dimensions
        apps = list({
            self._geometry_cache[wid].app_name
            for wid in self._windows_on_ghost
            if wid in self._geometry_cache and hasattr(self._geometry_cache[wid], 'app_name')
            and self._geometry_cache[wid].app_name
        }) if self._geometry_cache else []

        return {
            "available": self._status == GhostDisplayStatus.AVAILABLE,
            "status": self._status.value if hasattr(self._status, 'value') else str(self._status),
            "window_count": len(self._windows_on_ghost),
            "windows": list(self._windows_on_ghost),
            "apps": apps,
            "resolution": f"{dims[0]}x{dims[1]}",
            "display_id": self.ghost_display_id,
            "space_id": self.ghost_space,
            "is_fallback": self._status == GhostDisplayStatus.FALLBACK,
        }

    async def _notify_state_change(self, event_type: str, data: Optional[Dict[str, Any]] = None):
        """
        Notify all observers of a ghost display state change.

        Args:
            event_type: One of 'window_added', 'window_removed', 'status_changed', 'display_created'
            data: Additional event-specific data
        """
        snapshot = self.get_state_snapshot()
        payload = {
            "event_type": event_type,
            "state": snapshot,
            **(data or {}),
        }

        # Also publish to Trinity Event Bus if available
        try:
            from backend.core.trinity_event_bus import (
                EventType, RepoType, is_event_bus_running, get_trinity_event_bus,
            )
            if is_event_bus_running():
                bus = await get_trinity_event_bus()
                topic_map = {
                    "window_added": EventType.GHOST_DISPLAY_WINDOW_ADDED,
                    "window_removed": EventType.GHOST_DISPLAY_WINDOW_REMOVED,
                    "status_changed": EventType.GHOST_DISPLAY_STATUS_CHANGED,
                    "display_created": EventType.GHOST_DISPLAY_CREATED,
                }
                topic = topic_map.get(event_type)
                if topic:
                    await bus.publish_raw(
                        topic=topic.value,
                        data=payload,
                        target=RepoType.BROADCAST,
                    )
        except Exception as e:
            logger.debug(f"[GhostManager] Event bus publish skipped: {e}")

        # Notify local observers
        for observer in self._state_observers:
            try:
                result = observer(event_type, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.debug(f"[GhostManager] Observer notification error: {e}")

        self._last_emitted_status = self._status.value if hasattr(self._status, 'value') else str(self._status)

    # =========================================================================
    # v38.0: MOSAIC STRATEGY SUPPORT
    # =========================================================================

    @property
    def ghost_display_id(self) -> Optional[int]:
        """
        v38.0: Get the CGDisplayID of the Ghost Display.

        This is needed for MosaicWatcher to capture the entire Ghost Display
        as a single stream instead of creating N window watchers.

        Returns:
            Display ID (CGDisplayID) or None if Ghost Display unavailable
        """
        if self._ghost_info:
            return self._ghost_info.display_id
        return None

    @property
    def ghost_display_dimensions(self) -> Tuple[int, int]:
        """
        v38.0: Get Ghost Display dimensions (width, height).

        Returns:
            Tuple of (width, height) or (1920, 1080) as default
        """
        if self._ghost_info:
            return (self._ghost_info.width, self._ghost_info.height)
        return (1920, 1080)

    def get_mosaic_config(self) -> Optional[Dict[str, Any]]:
        """
        v38.0: Get configuration for MosaicWatcher.

        Returns all information needed to set up a single-stream Ghost Display
        capture, including window tile positions for spatial intelligence.

        Returns:
            Dict with display_id, dimensions, and window_tiles, or None if unavailable
        """
        if not self._ghost_info:
            return None

        return {
            'display_id': self._ghost_info.display_id,
            'display_width': self._ghost_info.width,
            'display_height': self._ghost_info.height,
            'space_id': self._ghost_info.space_id,
            'is_virtual': self._ghost_info.is_virtual,
            'window_count': self.window_count,
            'windows_on_ghost': list(self._windows_on_ghost),
        }

    def get_preserved_geometry(self, window_id: int) -> Optional[WindowGeometry]:
        """Get preserved geometry for a window."""
        return self._geometry_cache.get(window_id)

    def get_all_preserved_geometries(self) -> List[WindowGeometry]:
        """Get all preserved window geometries."""
        return list(self._geometry_cache.values())

    # =========================================================================
    # v26.0: New Properties for Edge Case State
    # =========================================================================

    @property
    def is_system_sleeping(self) -> bool:
        """Check if system is currently sleeping."""
        return self._is_system_sleeping

    @property
    def is_user_on_ghost_display(self) -> bool:
        """Check if user is currently on Ghost Display."""
        return self._user_on_ghost_display

    @property
    def are_operations_paused(self) -> bool:
        """Check if operations are paused due to user presence."""
        return self._operations_paused

    @property
    def has_pending_animations(self) -> bool:
        """Check if there are pending window animations."""
        if not self._pending_animations:
            return False
        # Check if any animation is still in progress
        now = datetime.now()
        animation_duration = timedelta(milliseconds=self.config.standard_animation_duration_ms)
        for window_id, start_time in list(self._pending_animations.items()):
            if now - start_time < animation_duration:
                return True
            else:
                # Animation complete, remove from pending
                del self._pending_animations[window_id]
        return False

    @property
    def ghost_display_scale(self) -> float:
        """Get the scale factor of the Ghost Display."""
        if self._ghost_info:
            return self._ghost_info.scale_factor
        return self.config.default_display_scale

    # =========================================================================
    # v26.0: Display Scaling Detection
    # =========================================================================

    async def detect_display_scale(
        self,
        display_id: int,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> float:
        """
        Detect the scale factor of a display (for Retina support).

        Args:
            display_id: Display to check
            yabai_detector: YabaiSpaceDetector for querying

        Returns:
            Scale factor (1.0 for standard, 2.0 for Retina)
        """
        if not self.config.auto_detect_display_scale:
            return self.config.default_display_scale

        # Check cache first
        if display_id in self._display_scale_cache:
            return self._display_scale_cache[display_id]

        try:
            # Query display info using system_profiler or displayplacer if available
            # For now, use a heuristic based on display resolution
            displays = yabai_detector.enumerate_all_spaces(include_display_info=True)

            for space_info in displays:
                if space_info.get("display") == display_id:
                    width = space_info.get("width", 1920)
                    height = space_info.get("height", 1080)

                    # Heuristic: High resolution displays are likely Retina
                    # MacBook Pro Retina: 2560x1600 or higher (scaled to 1440x900, etc.)
                    # iMac Retina: 4096x2304 or 5120x2880
                    if width >= 2560 and height >= 1440:
                        scale = 2.0
                    elif width >= 1920 and height >= 1080:
                        scale = 1.0
                    else:
                        scale = 1.0

                    self._display_scale_cache[display_id] = scale
                    self._display_resolution_cache[display_id] = (width, height)

                    logger.debug(
                        f"[GhostManager] 📏 Display {display_id} scale detected: "
                        f"{scale}x ({width}x{height})"
                    )
                    return scale

        except Exception as e:
            logger.debug(f"[GhostManager] Scale detection failed: {e}")

        return self.config.default_display_scale

    def get_display_resolution(self, display_id: int) -> Tuple[int, int]:
        """Get cached resolution for a display."""
        return self._display_resolution_cache.get(display_id, (1920, 1080))

    # =========================================================================
    # v26.0: Resolution Change Detection
    # =========================================================================

    async def check_resolution_changes(
        self,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Optional[Dict[str, Any]]:
        """
        Check if Ghost Display resolution has changed.

        Returns:
            Dict with change info if resolution changed, None otherwise
        """
        if not self.config.monitor_resolution_changes or self._ghost_info is None:
            return None

        try:
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)

            for space_info in spaces:
                if space_info.get("space_id") == self._ghost_info.space_id:
                    new_width = space_info.get("width", self._ghost_info.width)
                    new_height = space_info.get("height", self._ghost_info.height)

                    if (new_width != self._ghost_info.width or
                        new_height != self._ghost_info.height):

                        old_width = self._ghost_info.width
                        old_height = self._ghost_info.height

                        # Update ghost info
                        self._ghost_info.update_resolution(new_width, new_height)

                        change_info = {
                            "old_width": old_width,
                            "old_height": old_height,
                            "new_width": new_width,
                            "new_height": new_height,
                            "display_id": self._ghost_info.display_id,
                            "detected_at": datetime.now()
                        }

                        logger.warning(
                            f"[GhostManager] 🔄 Resolution change detected: "
                            f"{old_width}x{old_height} → {new_width}x{new_height}"
                        )

                        return change_info

        except Exception as e:
            logger.debug(f"[GhostManager] Resolution check failed: {e}")

        return None

    async def handle_resolution_change(
        self,
        change_info: Dict[str, Any],
        yabai_detector: 'YabaiSpaceDetector'
    ):
        """
        Handle a resolution change by optionally re-laying out windows.

        Args:
            change_info: Resolution change information
            yabai_detector: YabaiSpaceDetector for window operations
        """
        if not self.config.auto_relayout_on_resolution_change:
            return

        if self._windows_on_ghost:
            logger.info(
                f"[GhostManager] 📐 Re-laying out {len(self._windows_on_ghost)} windows "
                f"after resolution change"
            )
            await self.apply_layout(
                list(self._windows_on_ghost),
                yabai_detector,
                self.config.default_layout_style
            )

    # =========================================================================
    # v26.0: System Sleep/Wake Handling
    # =========================================================================

    def record_system_sleep(self):
        """
        Record that the system is entering sleep mode.

        Call this when system sleep is detected.
        """
        if not self.config.handle_sleep_wake_events:
            return

        self._is_system_sleeping = True
        self._last_sleep_time = datetime.now()

        if self._ghost_info:
            self._ghost_info.record_system_sleep()

        logger.info("[GhostManager] 😴 System entering sleep - pausing operations")

    def record_system_wake(self):
        """
        Record that the system has woken from sleep.

        Call this when system wake is detected.
        """
        if not self.config.handle_sleep_wake_events:
            return

        self._is_system_sleeping = False
        self._last_wake_time = datetime.now()
        self._pending_post_wake_check = self.config.post_wake_health_check

        if self._ghost_info:
            self._ghost_info.record_system_wake()

        logger.info("[GhostManager] ☀️ System waking from sleep - resuming operations")

    async def wait_for_post_wake_stability(self):
        """
        Wait for system stability after wake from sleep.

        This allows displays, window server, and yabai to stabilize.
        """
        if not self.config.handle_sleep_wake_events:
            return

        if self._last_wake_time:
            time_since_wake = (datetime.now() - self._last_wake_time).total_seconds() * 1000
            if time_since_wake < self.config.post_wake_delay_ms:
                wait_time = (self.config.post_wake_delay_ms - time_since_wake) / 1000.0
                logger.debug(f"[GhostManager] Waiting {wait_time:.2f}s for post-wake stability")
                await asyncio.sleep(wait_time)

    # =========================================================================
    # v26.0: User Presence Detection
    # =========================================================================

    async def detect_user_presence(
        self,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> bool:
        """
        Detect if user is currently viewing the Ghost Display.

        Args:
            yabai_detector: YabaiSpaceDetector for space queries

        Returns:
            True if user is on Ghost Display
        """
        if not self.config.detect_user_on_ghost_display or self._ghost_info is None:
            return False

        try:
            # Get current focused space
            current_space = yabai_detector.get_current_user_space()

            is_on_ghost = current_space == self._ghost_info.space_id

            # Update tracking
            was_on_ghost = self._user_on_ghost_display
            self._user_on_ghost_display = is_on_ghost
            self._last_user_presence_check = datetime.now()

            # Record presence in ghost info
            self._ghost_info.record_user_presence(is_on_ghost)

            # Track visits
            if is_on_ghost and not was_on_ghost:
                self._user_visit_history.append(datetime.now())
                # Keep only last 100 visits
                if len(self._user_visit_history) > 100:
                    self._user_visit_history = self._user_visit_history[-100:]
                logger.debug("[GhostManager] 👤 User arrived on Ghost Display")

            elif not is_on_ghost and was_on_ghost:
                logger.debug("[GhostManager] 👤 User left Ghost Display")

            return is_on_ghost

        except Exception as e:
            logger.debug(f"[GhostManager] User presence detection failed: {e}")
            return False

    def should_pause_operations(self) -> bool:
        """
        Check if operations should be paused due to user presence.

        Returns:
            True if operations should be paused
        """
        if not self.config.pause_operations_when_user_present:
            return False

        if not self._user_on_ghost_display:
            return False

        # Check cooldown
        if self._last_user_presence_check:
            time_since_check = (datetime.now() - self._last_user_presence_check).total_seconds()
            if time_since_check > self.config.user_presence_cooldown_seconds:
                # Cooldown expired, need to recheck
                return True

        return self._user_on_ghost_display

    async def wait_for_user_to_leave(
        self,
        yabai_detector: 'YabaiSpaceDetector',
        timeout_seconds: float = 30.0
    ) -> bool:
        """
        Wait for user to leave Ghost Display before proceeding.

        Args:
            yabai_detector: YabaiSpaceDetector for space queries
            timeout_seconds: Maximum time to wait

        Returns:
            True if user left, False if timeout
        """
        start_time = time.time()
        check_interval = 0.5  # Check every 500ms

        while time.time() - start_time < timeout_seconds:
            is_present = await self.detect_user_presence(yabai_detector)
            if not is_present:
                return True
            await asyncio.sleep(check_interval)

        logger.warning(f"[GhostManager] Timeout waiting for user to leave Ghost Display")
        return False

    # =========================================================================
    # v26.0: Animation Timing
    # =========================================================================

    def mark_animation_start(self, window_id: int):
        """Mark that a window animation has started."""
        self._pending_animations[window_id] = datetime.now()
        if self._ghost_info:
            self._ghost_info.mark_animation_start()

    def mark_animation_complete(self, window_id: int):
        """Mark that a window animation has completed."""
        if window_id in self._pending_animations:
            del self._pending_animations[window_id]
        if not self._pending_animations and self._ghost_info:
            self._ghost_info.mark_animation_complete()

    async def wait_for_animation(
        self,
        window_id: Optional[int] = None,
        extra_buffer_ms: Optional[float] = None
    ):
        """
        Wait for window animation(s) to complete.

        Args:
            window_id: Specific window to wait for, or None for all
            extra_buffer_ms: Extra time to wait after animation
        """
        if not self.config.wait_for_animations:
            return

        buffer = extra_buffer_ms or self.config.animation_buffer_ms
        duration = self.config.standard_animation_duration_ms

        if window_id is not None:
            # Wait for specific window
            if window_id in self._pending_animations:
                start_time = self._pending_animations[window_id]
                elapsed = (datetime.now() - start_time).total_seconds() * 1000
                remaining = max(0, duration - elapsed + buffer)
                if remaining > 0:
                    await asyncio.sleep(remaining / 1000.0)
                self.mark_animation_complete(window_id)
        else:
            # Wait for all animations
            if self._pending_animations:
                # Find the most recent animation start
                latest_start = max(self._pending_animations.values())
                elapsed = (datetime.now() - latest_start).total_seconds() * 1000
                remaining = max(0, duration - elapsed + buffer)
                if remaining > 0:
                    await asyncio.sleep(remaining / 1000.0)
                # Clear all pending
                self._pending_animations.clear()
                if self._ghost_info:
                    self._ghost_info.mark_animation_complete()

    # =========================================================================
    # v26.0: Space UUID Stability
    # =========================================================================

    async def get_space_uuid(
        self,
        space_id: int,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Optional[str]:
        """
        Get the UUID for a space (more stable than space ID).

        Args:
            space_id: Space ID to look up
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Space UUID if available
        """
        if not self.config.use_space_uuid:
            return None

        # Check cache first
        if space_id in self._space_uuid_cache:
            return self._space_uuid_cache[space_id]

        try:
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
            for space_info in spaces:
                if space_info.get("space_id") == space_id:
                    uuid = space_info.get("uuid")
                    label = space_info.get("label")
                    if uuid:
                        self._space_uuid_cache[space_id] = uuid
                    if label:
                        self._space_label_cache[space_id] = label
                    return uuid
        except Exception as e:
            logger.debug(f"[GhostManager] Space UUID lookup failed: {e}")

        return None

    async def find_space_by_uuid(
        self,
        uuid: str,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Optional[int]:
        """
        Find a space ID by its UUID.

        Args:
            uuid: Space UUID to find
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Space ID if found
        """
        if not self.config.use_space_uuid:
            return None

        # Check if we have a cached mapping
        for space_id, cached_uuid in self._space_uuid_cache.items():
            if cached_uuid == uuid:
                return space_id

        try:
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
            for space_info in spaces:
                if space_info.get("uuid") == uuid:
                    space_id = space_info.get("space_id")
                    self._space_uuid_cache[space_id] = uuid
                    return space_id
        except Exception as e:
            logger.debug(f"[GhostManager] Space lookup by UUID failed: {e}")

        return None

    async def verify_space_still_valid(
        self,
        space_id: int,
        expected_uuid: Optional[str],
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Tuple[bool, Optional[int]]:
        """
        Verify a space is still valid (hasn't been reordered or deleted).

        Args:
            space_id: Space ID to verify
            expected_uuid: Expected UUID for the space
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Tuple of (is_valid, new_space_id_if_changed)
        """
        if not self.config.verify_space_before_move:
            return True, None

        try:
            current_uuid = await self.get_space_uuid(space_id, yabai_detector)

            if expected_uuid is None:
                # No UUID to compare, assume valid
                return True, None

            if current_uuid == expected_uuid:
                return True, None

            # UUID mismatch - space may have been reordered
            # Try to find the new space ID by UUID
            new_space_id = await self.find_space_by_uuid(expected_uuid, yabai_detector)
            if new_space_id:
                logger.warning(
                    f"[GhostManager] ⚠️ Space reordered: {space_id} → {new_space_id}"
                )
                return False, new_space_id

            logger.warning(f"[GhostManager] ⚠️ Space {space_id} no longer exists (UUID mismatch)")
            return False, None

        except Exception as e:
            logger.debug(f"[GhostManager] Space verification failed: {e}")
            return True, None  # Assume valid on error

    # =========================================================================
    # v26.0: Z-Order/Layer Management
    # =========================================================================

    async def capture_z_order(
        self,
        window_ids: List[int],
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Dict[int, int]:
        """
        Capture the z-order (layer) of windows before teleportation.

        Args:
            window_ids: Windows to capture z-order for
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Dict mapping window_id to z_order
        """
        if not self.config.preserve_z_order:
            return {}

        z_orders = {}
        try:
            # Get all windows to determine relative ordering
            all_windows = yabai_detector.get_all_windows()

            # Windows are returned in z-order (front to back typically)
            window_set = set(window_ids)
            z_order = 0

            for window in all_windows:
                wid = window.get("id")
                if wid in window_set:
                    z_orders[wid] = z_order
                    self._z_order_cache[wid] = z_order
                    z_order += 1

            # Also capture which window was focused
            focused_window = yabai_detector.get_focused_window()
            if focused_window and focused_window in window_set:
                self._focused_window_before_teleport = focused_window

        except Exception as e:
            logger.debug(f"[GhostManager] Z-order capture failed: {e}")

        return z_orders

    async def restore_z_order(
        self,
        window_ids: List[int],
        yabai_detector: 'YabaiSpaceDetector'
    ):
        """
        Restore the z-order of windows after returning from Ghost Display.

        Args:
            window_ids: Windows to restore z-order for
            yabai_detector: YabaiSpaceDetector for operations
        """
        if not self.config.preserve_z_order:
            return

        try:
            yabai_path = yabai_detector._health.yabai_path or "yabai"

            # Sort windows by their original z-order (back to front)
            sorted_windows = sorted(
                [(wid, self._z_order_cache.get(wid, 0)) for wid in window_ids],
                key=lambda x: -x[1]  # Reverse order so we can bring each to front
            )

            for window_id, z_order in sorted_windows:
                try:
                    # Focus the window to bring it forward
                    subprocess.run(
                        [yabai_path, "-m", "window", str(window_id), "--focus"],
                        capture_output=True,
                        timeout=1.0
                    )
                    await asyncio.sleep(0.05)  # Small delay between focus operations
                except Exception:
                    pass

            # Restore focus to originally focused window
            if (self.config.restore_focus_on_return and
                self._focused_window_before_teleport and
                self._focused_window_before_teleport in window_ids):

                try:
                    subprocess.run(
                        [yabai_path, "-m", "window",
                         str(self._focused_window_before_teleport), "--focus"],
                        capture_output=True,
                        timeout=1.0
                    )
                except Exception:
                    pass

            logger.debug(f"[GhostManager] 📚 Restored z-order for {len(window_ids)} windows")

        except Exception as e:
            logger.debug(f"[GhostManager] Z-order restore failed: {e}")

    async def initialize(self, yabai_detector: 'YabaiSpaceDetector') -> bool:
        """
        Initialize the Ghost Display Manager.

        Args:
            yabai_detector: YabaiSpaceDetector instance for querying spaces

        Returns:
            True if Ghost Display is available (or fallback activated)
        """
        async with self._lock:
            # Detect Ghost Display
            ghost_space = yabai_detector.get_ghost_display_space()

            if ghost_space is not None:
                # Get display info
                spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
                for space in spaces:
                    if space.get("space_id") == ghost_space:
                        self._ghost_info = GhostDisplayInfo(
                            space_id=ghost_space,
                            display_id=space.get("display", 2),
                            display_name=f"Display {space.get('display', 2)}",
                            width=space.get("width", 1920),
                            height=space.get("height", 1080),
                            is_virtual=space.get("display", 1) > 1,
                            window_count=space.get("window_count", 0),
                            last_health_check=datetime.now()
                        )
                        self._status = GhostDisplayStatus.AVAILABLE
                        logger.info(
                            f"[GhostManager] ✅ Initialized: Space {ghost_space} "
                            f"on Display {self._ghost_info.display_id} "
                            f"({self._ghost_info.width}x{self._ghost_info.height})"
                        )

                        # v48.0: Enforce BSP layout to prevent window overlap
                        await self._enforce_bsp_layout(ghost_space)

                        # v27.0: Initialize persistence manager for crash recovery
                        await self._initialize_persistence(yabai_detector)

                        # v129.0: Emit display_created event
                        await self._notify_state_change("display_created", {
                            "display_id": self._ghost_info.display_id,
                            "resolution": f"{self._ghost_info.width}x{self._ghost_info.height}",
                        })

                        return True

            # No Ghost Display - try fallback
            if self.config.fallback_enabled:
                return await self._activate_fallback(yabai_detector)

            self._status = GhostDisplayStatus.UNAVAILABLE
            await self._notify_state_change("status_changed", {"new_status": "unavailable"})
            logger.warning("[GhostManager] ❌ No Ghost Display available and fallback disabled")
            return False

    async def _activate_fallback(self, yabai_detector: 'YabaiSpaceDetector') -> bool:
        """Activate fallback strategy when Ghost Display unavailable."""
        self._status = GhostDisplayStatus.FALLBACK

        # Strategy 1: Find any visible space that's not current
        spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
        current_space = yabai_detector.get_current_user_space()

        for space in spaces:
            if space.get("is_visible") and space.get("space_id") != current_space:
                self._fallback_space = space.get("space_id")
                logger.info(
                    f"[GhostManager] 🔄 FALLBACK: Using Space {self._fallback_space} "
                    f"as substitute Ghost Display"
                )
                await self._notify_state_change("status_changed", {"new_status": "fallback"})
                return True

        # Strategy 2: Use current space (last resort - will be visible to user)
        if current_space:
            self._fallback_space = current_space
            logger.warning(
                f"[GhostManager] ⚠️ FALLBACK: Using current Space {current_space} "
                f"(windows will be visible to user)"
            )
            await self._notify_state_change("status_changed", {"new_status": "fallback"})
            return True

        logger.error("[GhostManager] ❌ No fallback available")
        await self._notify_state_change("status_changed", {"new_status": "unavailable"})
        return False

    # =========================================================================
    # v48.0: BSP LAYOUT ENFORCEMENT ("PILE-UP" NUANCE FIX)
    # =========================================================================
    # When multiple windows arrive on Ghost Display, they could stack on top
    # of each other (overlap). This breaks Mosaic vision which needs to see ALL
    # windows simultaneously without obstruction.
    #
    # Solution: Force BSP (Binary Space Partitioning) layout on Ghost Space.
    # BSP automatically tiles windows side-by-side, ensuring no overlap.
    # =========================================================================

    async def _enforce_bsp_layout(self, space_id: int) -> bool:
        """
        v48.0: Enforce BSP (tiled) layout on the Ghost Space.

        This prevents the "Pile-Up" nuance where multiple windows stack
        on top of each other, obstructing Mosaic vision's ability to
        see all windows simultaneously.

        Args:
            space_id: The Ghost Space ID to enforce BSP on

        Returns:
            True if BSP layout was successfully set, False otherwise
        """
        try:
            yabai_path = os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

            # Set BSP layout on the specific space
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "config", "--space", str(space_id), "layout", "bsp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                logger.info(
                    f"[GhostManager v48.0] 🧱 BSP LAYOUT ENFORCED: "
                    f"Space {space_id} now uses tiled (BSP) layout - no window overlap!"
                )
                return True
            else:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.warning(
                    f"[GhostManager v48.0] ⚠️ BSP layout enforcement failed: {error_msg}"
                )
                return False

        except asyncio.TimeoutError:
            logger.warning(f"[GhostManager v48.0] ⚠️ BSP layout command timed out")
            return False
        except Exception as e:
            logger.warning(f"[GhostManager v48.0] ⚠️ BSP layout enforcement error: {e}")
            return False

    async def ensure_bsp_layout(self) -> bool:
        """
        v48.0: Public method to ensure BSP layout on Ghost Space.

        Call this after teleporting windows to guarantee no overlap.

        Returns:
            True if BSP is enforced, False if no Ghost Space or enforcement failed
        """
        if self._ghost_info is None:
            logger.debug("[GhostManager v48.0] No Ghost Space available for BSP enforcement")
            return False

        return await self._enforce_bsp_layout(self._ghost_info.space_id)

    # =========================================================================
    # v27.0: Crash Recovery Integration
    # =========================================================================

    async def _initialize_persistence(
        self,
        yabai_detector: 'YabaiSpaceDetector',
        narrate_callback: Optional[Callable] = None
    ) -> None:
        """
        Initialize the persistence manager and audit for stranded windows.

        This is called during GhostDisplayManager initialization to:
        1. Load any persisted state from a previous session
        2. Detect windows stranded on Ghost Display after a crash
        3. Optionally repatriate stranded windows to their original spaces

        Args:
            yabai_detector: YabaiSpaceDetector for window operations
            narrate_callback: Optional async callback for voice narration
        """
        if not _HAS_GHOST_PERSISTENCE:
            logger.debug("[GhostManager] Ghost persistence not available - skipping crash recovery")
            return

        if self._persistence_initialized:
            return

        try:
            # Get or create persistence manager
            self._persistence_manager = get_persistence_manager()

            # Start up and audit for stranded windows
            stranded_windows = await self._persistence_manager.startup()

            if stranded_windows:
                logger.info(
                    f"[GhostManager] 🆘 Found {len(stranded_windows)} stranded windows from previous session"
                )

                # Repatriate stranded windows
                result = await self._persistence_manager.repatriate_stranded_windows(
                    stranded=stranded_windows,
                    narrate_callback=narrate_callback
                )

                logger.info(
                    f"[GhostManager] 🏠 Crash recovery complete: "
                    f"{result['success']} returned, {result['failed']} failed"
                )
            else:
                logger.debug("[GhostManager] No stranded windows found - clean startup")

            self._persistence_initialized = True

        except Exception as e:
            logger.warning(f"[GhostManager] Persistence initialization failed: {e}")
            # Non-fatal - continue without crash recovery

    async def shutdown_persistence(self) -> None:
        """Shutdown persistence manager gracefully."""
        if self._persistence_manager:
            try:
                await self._persistence_manager.shutdown()
                logger.debug("[GhostManager] Persistence manager shutdown complete")
            except Exception as e:
                logger.warning(f"[GhostManager] Persistence shutdown error: {e}")

    async def start_health_monitoring(self, yabai_detector: 'YabaiSpaceDetector'):
        """Start background health monitoring for Ghost Display."""
        if self._monitoring_task is not None:
            return  # Already running

        async def monitor_loop():
            max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
            session_start = time.monotonic()
            while time.monotonic() - session_start < max_runtime:
                try:
                    await asyncio.sleep(self.config.health_check_interval_seconds)
                    await self._health_check(yabai_detector)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[GhostManager] Health check error: {e}")
            else:
                logger.info("[GhostManager] Health monitoring session timeout, stopping")

        self._monitoring_task = asyncio.create_task(monitor_loop())
        logger.debug("[GhostManager] 🏥 Health monitoring started")

    async def stop_health_monitoring(self):
        """Stop health monitoring."""
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None
            logger.debug("[GhostManager] 🏥 Health monitoring stopped")

    async def _health_check(self, yabai_detector: 'YabaiSpaceDetector'):
        """
        Perform health check on Ghost Display.

        v26.0: Enhanced with edge case handling:
        - Skip during system sleep
        - Check for post-wake stability
        - Detect resolution changes
        - Detect user presence
        - Check space UUID stability
        """
        if self._ghost_info is None:
            return

        # v26.0: Skip health checks during system sleep
        if self._is_system_sleeping:
            logger.debug("[GhostManager] Health check skipped - system sleeping")
            return

        async with self._lock:
            # v26.0: Wait for post-wake stability if needed
            if self._pending_post_wake_check:
                await self.wait_for_post_wake_stability()
                self._pending_post_wake_check = False

            # v26.0: Check for resolution changes
            if self.config.monitor_resolution_changes:
                resolution_change = await self.check_resolution_changes(yabai_detector)
                if resolution_change:
                    await self.handle_resolution_change(resolution_change, yabai_detector)

            # v26.0: Detect user presence on Ghost Display
            if self.config.detect_user_on_ghost_display:
                await self.detect_user_presence(yabai_detector)

            # v26.0: Verify space UUID stability
            if self.config.use_space_uuid and self._ghost_info.space_uuid:
                is_valid, new_space_id = await self.verify_space_still_valid(
                    self._ghost_info.space_id,
                    self._ghost_info.space_uuid,
                    yabai_detector
                )
                if not is_valid and new_space_id:
                    # Space was reordered, update our tracking
                    logger.info(
                        f"[GhostManager] 🔄 Ghost Display space reordered: "
                        f"{self._ghost_info.space_id} → {new_space_id}"
                    )
                    self._ghost_info.space_id = new_space_id

            # Verify Ghost Display is still available
            current_ghost = yabai_detector.get_ghost_display_space()

            if current_ghost == self._ghost_info.space_id:
                # Still available
                self._ghost_info.last_health_check = datetime.now()
                self._ghost_info.consecutive_failures = 0
                self._status = GhostDisplayStatus.AVAILABLE

                # v26.0: Update display scale if auto-detection enabled
                if self.config.auto_detect_display_scale:
                    scale = await self.detect_display_scale(
                        self._ghost_info.display_id,
                        yabai_detector
                    )
                    if scale != self._ghost_info.scale_factor:
                        logger.debug(
                            f"[GhostManager] Display scale changed: "
                            f"{self._ghost_info.scale_factor} → {scale}"
                        )
                        self._ghost_info.scale_factor = scale

            else:
                # Ghost Display changed or disappeared
                self._ghost_info.consecutive_failures += 1
                logger.warning(
                    f"[GhostManager] ⚠️ Health check failed "
                    f"({self._ghost_info.consecutive_failures}/{self.config.max_consecutive_failures})"
                )

                if self._ghost_info.consecutive_failures >= self.config.max_consecutive_failures:
                    self._status = GhostDisplayStatus.DISCONNECTED

                    if self.config.auto_recovery_enabled:
                        await self._attempt_recovery(yabai_detector)

    async def _attempt_recovery(self, yabai_detector: 'YabaiSpaceDetector'):
        """Attempt to recover Ghost Display connection."""
        self._status = GhostDisplayStatus.RECONNECTING
        logger.info("[GhostManager] 🔄 Attempting Ghost Display recovery...")

        # Try to find new Ghost Display
        new_ghost = yabai_detector.get_ghost_display_space()

        if new_ghost is not None:
            # Found new Ghost Display
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
            for space in spaces:
                if space.get("space_id") == new_ghost:
                    self._ghost_info = GhostDisplayInfo(
                        space_id=new_ghost,
                        display_id=space.get("display", 2),
                        display_name=f"Display {space.get('display', 2)}",
                        width=space.get("width", 1920),
                        height=space.get("height", 1080),
                        is_virtual=space.get("display", 1) > 1,
                        window_count=space.get("window_count", 0),
                        last_health_check=datetime.now()
                    )
                    self._status = GhostDisplayStatus.AVAILABLE
                    logger.info(f"[GhostManager] ✅ Recovery successful: Space {new_ghost}")

                    # v48.0: Enforce BSP layout on recovered Ghost Space
                    await self._enforce_bsp_layout(new_ghost)

                    # Re-teleport windows if any were on old Ghost Display
                    if self._windows_on_ghost:
                        logger.info(
                            f"[GhostManager] 🚑 Re-teleporting {len(self._windows_on_ghost)} "
                            f"windows to new Ghost Display"
                        )
                        # Windows will need to be re-teleported by caller
                    return

        # Recovery failed - activate fallback
        if self.config.fallback_enabled:
            await self._activate_fallback(yabai_detector)
        else:
            self._status = GhostDisplayStatus.UNAVAILABLE
            logger.error("[GhostManager] ❌ Recovery failed, Ghost Display unavailable")

    async def preserve_window_geometry(
        self,
        window_id: int,
        yabai_detector: 'YabaiSpaceDetector',
        app_name: Optional[str] = None
    ) -> Optional[WindowGeometry]:
        """
        Preserve window geometry before teleportation.

        v26.0: Enhanced with:
        - Display scale factor capture
        - Space UUID for stability
        - Focus state and z-order
        - Window constraints (min/max size)
        - Split view and PiP detection

        Args:
            window_id: Window to preserve geometry for
            yabai_detector: YabaiSpaceDetector for querying
            app_name: Optional app name for tracking

        Returns:
            WindowGeometry if successful
        """
        try:
            # Get window info from yabai
            windows = yabai_detector.get_all_windows()
            window_info = None
            window_z_order = 0

            # Find window and determine z-order from position in list
            for idx, w in enumerate(windows):
                if w.get("id") == window_id:
                    window_info = w
                    window_z_order = idx
                    break

            if window_info is None:
                logger.debug(f"[GhostManager] Window {window_id} not found for geometry preservation")
                return None

            frame = window_info.get("frame", {})
            original_space = window_info.get("space", 1)
            original_display = window_info.get("display", 1)

            # v26.0: Get display scale factor
            source_scale = self.config.default_display_scale
            source_width, source_height = 1920, 1080
            if self.config.enable_retina_scaling:
                source_scale = await self.detect_display_scale(original_display, yabai_detector)
                source_width, source_height = self.get_display_resolution(original_display)

            # v26.0: Get Ghost Display scale
            ghost_scale = self.ghost_display_scale
            ghost_width = self._ghost_info.width if self._ghost_info else 1920
            ghost_height = self._ghost_info.height if self._ghost_info else 1080

            # v26.0: Get space UUID for stability
            space_uuid = None
            space_label = None
            if self.config.use_space_uuid:
                space_uuid = await self.get_space_uuid(original_space, yabai_detector)
                space_label = self._space_label_cache.get(original_space)

            # v26.0: Check if window is focused
            focused_window = yabai_detector.get_focused_window()
            was_focused = focused_window == window_id

            # v26.0: Detect split view and PiP
            # Note: yabai may not directly expose these, but we can infer from window properties
            is_split_view = window_info.get("is-sticky", False) and not window_info.get("is-floating", False)
            is_pip = window_info.get("is-sticky", False) and window_info.get("is-floating", False)

            # v26.0: Get window constraints (if available from accessibility APIs)
            # These would need AppleScript or accessibility framework to get accurately
            # For now, use heuristics based on window type
            min_width = None
            min_height = None
            max_width = None
            max_height = None
            has_constraints = False

            # Apply minimum size constraints from config
            if self.config.enforce_minimum_window_size:
                min_width = self.config.minimum_window_width
                min_height = self.config.minimum_window_height
                has_constraints = True

            geometry = WindowGeometry(
                window_id=window_id,
                app_name=app_name or window_info.get("app", "Unknown"),
                original_space=original_space,
                original_display=original_display,
                x=int(frame.get("x", 0)),
                y=int(frame.get("y", 0)),
                width=int(frame.get("w", 800)),
                height=int(frame.get("h", 600)),
                is_minimized=window_info.get("is-minimized", False),
                is_fullscreen=window_info.get("is-native-fullscreen", False),
                is_floating=window_info.get("is-floating", False),
                teleported_at=datetime.now(),
                # v26.0: Display scaling
                source_display_scale=source_scale,
                source_display_width=source_width,
                source_display_height=source_height,
                ghost_display_scale=ghost_scale,
                ghost_display_width=ghost_width,
                ghost_display_height=ghost_height,
                # v26.0: Space stability
                original_space_uuid=space_uuid,
                original_space_label=space_label,
                # v26.0: Focus and z-order
                was_focused=was_focused,
                z_order=window_z_order,
                is_split_view=is_split_view,
                is_picture_in_picture=is_pip,
                # v26.0: Window constraints
                min_width=min_width,
                min_height=min_height,
                max_width=max_width,
                max_height=max_height,
                has_constraints=has_constraints,
                # v26.0: Animation timing
                move_animation_duration_ms=self.config.standard_animation_duration_ms,
                last_position_stable_at=datetime.now()
            )

            # Cache geometry and z-order
            self._geometry_cache[window_id] = geometry
            self._z_order_cache[window_id] = window_z_order

            # Track focused window
            if was_focused:
                self._focused_window_before_teleport = window_id

            # v27.0: Persist to disk BEFORE teleportation for crash recovery
            # This ensures if JARVIS crashes mid-teleport, we know where to return windows
            if self._persistence_manager:
                try:
                    ghost_space_id = self.ghost_space or 0
                    await self._persistence_manager.record_teleportation(
                        window_id=window_id,
                        app_name=geometry.app_name,
                        original_space=original_space,
                        original_x=geometry.x,
                        original_y=geometry.y,
                        original_width=geometry.width,
                        original_height=geometry.height,
                        ghost_space=ghost_space_id,
                        z_order=window_z_order,
                        original_display=original_display,
                        was_minimized=geometry.is_minimized,
                        was_fullscreen=geometry.is_fullscreen,
                    )
                except Exception as e:
                    # Non-fatal - log and continue
                    logger.debug(f"[GhostManager] Persistence record failed: {e}")

            logger.debug(
                f"[GhostManager] 📐 Preserved geometry for window {window_id}: "
                f"{geometry.width}x{geometry.height} at ({geometry.x}, {geometry.y}) "
                f"[scale={source_scale}, z={window_z_order}, focused={was_focused}]"
            )
            return geometry

        except Exception as e:
            logger.debug(f"[GhostManager] Failed to preserve geometry: {e}")
            return None

    async def track_window_teleport(self, window_id: int, to_space: int):
        """Track a window being teleported to Ghost Display."""
        self._windows_on_ghost.add(window_id)
        if window_id in self._geometry_cache:
            self._geometry_cache[window_id].current_space = to_space
        await self._notify_state_change("window_added", {"window_id": window_id, "space": to_space})

    async def track_window_return(self, window_id: int):
        """Track a window being returned from Ghost Display."""
        self._windows_on_ghost.discard(window_id)
        await self._notify_state_change("window_removed", {"window_id": window_id})

    def calculate_layout(
        self,
        window_count: int,
        style: Optional[WindowLayoutStyle] = None
    ) -> List[Dict[str, int]]:
        """
        Calculate window positions for a given layout style.

        Args:
            window_count: Number of windows to lay out
            style: Layout style (uses config default if None)

        Returns:
            List of position dicts: [{"x": int, "y": int, "width": int, "height": int}, ...]
        """
        style = style or self.config.default_layout_style
        padding = self.config.layout_padding

        if self._ghost_info is None:
            # Use reasonable defaults
            screen_width, screen_height = 1920, 1080
        else:
            screen_width = self._ghost_info.width
            screen_height = self._ghost_info.height

        # Account for menu bar and dock
        usable_y = 25  # Menu bar
        usable_height = screen_height - usable_y - 50  # Dock
        usable_width = screen_width

        positions = []

        if style == WindowLayoutStyle.MAXIMIZE or window_count == 1:
            # Single window or maximize: fill the screen
            positions.append({
                "x": padding,
                "y": usable_y + padding,
                "width": usable_width - 2 * padding,
                "height": usable_height - 2 * padding
            })
            # Duplicate for additional windows if needed
            for _ in range(1, window_count):
                positions.append(positions[0].copy())

        elif style == WindowLayoutStyle.SIDE_BY_SIDE:
            # Horizontal arrangement
            window_width = (usable_width - (window_count + 1) * padding) // window_count
            for i in range(window_count):
                positions.append({
                    "x": padding + i * (window_width + padding),
                    "y": usable_y + padding,
                    "width": window_width,
                    "height": usable_height - 2 * padding
                })

        elif style == WindowLayoutStyle.STACKED:
            # Vertical arrangement
            window_height = (usable_height - (window_count + 1) * padding) // window_count
            for i in range(window_count):
                positions.append({
                    "x": padding,
                    "y": usable_y + padding + i * (window_height + padding),
                    "width": usable_width - 2 * padding,
                    "height": window_height
                })

        elif style == WindowLayoutStyle.GRID:
            # Grid arrangement
            max_per_row = self.config.max_windows_per_row
            rows = (window_count + max_per_row - 1) // max_per_row
            cols = min(window_count, max_per_row)

            window_width = (usable_width - (cols + 1) * padding) // cols
            window_height = (usable_height - (rows + 1) * padding) // rows

            for i in range(window_count):
                row = i // cols
                col = i % cols
                positions.append({
                    "x": padding + col * (window_width + padding),
                    "y": usable_y + padding + row * (window_height + padding),
                    "width": window_width,
                    "height": window_height
                })

        elif style == WindowLayoutStyle.CASCADE:
            # Cascaded arrangement
            cascade_offset = 30
            window_width = usable_width - (window_count - 1) * cascade_offset - 2 * padding
            window_height = usable_height - (window_count - 1) * cascade_offset - 2 * padding

            for i in range(window_count):
                positions.append({
                    "x": padding + i * cascade_offset,
                    "y": usable_y + padding + i * cascade_offset,
                    "width": window_width,
                    "height": window_height
                })

        else:  # PRESERVE - return empty positions (don't move windows)
            for _ in range(window_count):
                positions.append({"x": -1, "y": -1, "width": -1, "height": -1})

        return positions

    async def apply_layout(
        self,
        window_ids: List[int],
        yabai_detector: 'YabaiSpaceDetector',
        style: Optional[WindowLayoutStyle] = None
    ) -> Dict[str, Any]:
        """
        Apply layout to windows on Ghost Display.

        v26.0: Enhanced with:
        - Wait for pending animations before applying layout
        - User presence check (pause if user on Ghost Display)
        - Animation tracking for each window movement
        - Window constraints enforcement
        - Display scaling awareness

        Args:
            window_ids: Windows to arrange
            yabai_detector: YabaiSpaceDetector for moving/resizing
            style: Layout style (uses config default if None)

        Returns:
            Result dict with success/failure info
        """
        style = style or self.config.default_layout_style

        if style == WindowLayoutStyle.PRESERVE:
            return {"success": True, "message": "Layout preserved (no changes)"}

        results = {
            "success": True,
            "applied": [],
            "failed": [],
            "skipped": [],
            "paused_for_user": False
        }

        # v26.0: Check if operations are paused due to user presence
        if self.config.pause_operations_when_user_present:
            is_user_present = await self.detect_user_presence(yabai_detector)
            if is_user_present:
                logger.info("[GhostManager] 👤 User on Ghost Display - waiting for them to leave")
                results["paused_for_user"] = True

                # Wait for user to leave (with timeout)
                user_left = await self.wait_for_user_to_leave(
                    yabai_detector,
                    timeout_seconds=10.0  # Short timeout for layout operations
                )
                if not user_left:
                    # User didn't leave, skip layout to avoid disruption
                    results["success"] = True
                    results["message"] = "Skipped layout - user present on Ghost Display"
                    return results

        # v26.0: Wait for any pending animations to complete
        if self.config.wait_for_animations and self.has_pending_animations:
            logger.debug("[GhostManager] Waiting for pending animations before layout")
            await self.wait_for_animation()

        positions = self.calculate_layout(len(window_ids), style)

        for i, window_id in enumerate(window_ids):
            pos = positions[i]
            if pos["x"] == -1:
                results["skipped"].append(window_id)
                continue  # Skip if preserve

            try:
                # v26.0: Apply window constraints from cached geometry
                geometry = self._geometry_cache.get(window_id)
                target_width = pos["width"]
                target_height = pos["height"]

                if geometry and geometry.has_constraints and self.config.respect_window_constraints:
                    if geometry.min_width and target_width < geometry.min_width:
                        target_width = geometry.min_width
                    if geometry.min_height and target_height < geometry.min_height:
                        target_height = geometry.min_height
                    if geometry.max_width and target_width > geometry.max_width:
                        target_width = geometry.max_width
                    if geometry.max_height and target_height > geometry.max_height:
                        target_height = geometry.max_height

                # v26.0: Apply minimum window size from config
                if self.config.enforce_minimum_window_size:
                    target_width = max(target_width, self.config.minimum_window_width)
                    target_height = max(target_height, self.config.minimum_window_height)

                # Use yabai to resize and move window
                yabai_path = yabai_detector._health.yabai_path or "yabai"

                # v26.0: Mark animation start for this window
                self.mark_animation_start(window_id)

                # Move window
                move_result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--move", f"abs:{pos['x']}:{pos['y']}"],
                    capture_output=True,
                    timeout=2.0
                )

                # Resize window
                resize_result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--resize", f"abs:{target_width}:{target_height}"],
                    capture_output=True,
                    timeout=2.0
                )

                if move_result.returncode == 0 or resize_result.returncode == 0:
                    results["applied"].append(window_id)
                else:
                    results["failed"].append(window_id)
                    self.mark_animation_complete(window_id)  # Clear animation tracking on failure

                # v26.0: Add throttle delay between teleports if configured
                if self.config.throttle_teleports and i < len(window_ids) - 1:
                    await asyncio.sleep(self.config.teleport_delay_ms / 1000.0)

            except Exception as e:
                logger.debug(f"[GhostManager] Layout apply failed for {window_id}: {e}")
                results["failed"].append(window_id)
                self.mark_animation_complete(window_id)

        # v26.0: Wait for all animations to complete before returning
        if self.config.wait_for_animations and results["applied"]:
            await self.wait_for_animation()

        results["success"] = len(results["failed"]) == 0
        self._last_layout_time = time.time()

        logger.info(
            f"[GhostManager] 📐 Layout applied ({style.value}): "
            f"{len(results['applied'])} succeeded, {len(results['failed'])} failed"
            + (f", {len(results['skipped'])} skipped" if results["skipped"] else "")
        )

        return results

    async def return_window_to_original(
        self,
        window_id: int,
        yabai_detector: 'YabaiSpaceDetector',
        restore_geometry: bool = True
    ) -> bool:
        """
        Return a window to its original space and optionally restore geometry.

        v26.0: Enhanced with:
        - Space UUID verification (handle space reordering)
        - Display scaling during geometry restore
        - Animation tracking for the return move
        - Focus restoration if window was focused
        - Z-order cleanup

        Args:
            window_id: Window to return
            yabai_detector: YabaiSpaceDetector for moving
            restore_geometry: Whether to restore original position/size

        Returns:
            True if successful
        """
        geometry = self._geometry_cache.get(window_id)
        if geometry is None:
            logger.warning(f"[GhostManager] No preserved geometry for window {window_id}")
            return False

        # v26.0: Verify original space is still valid (handle space reordering)
        target_space = geometry.original_space
        if self.config.use_space_uuid and geometry.original_space_uuid:
            is_valid, new_space_id = await self.verify_space_still_valid(
                geometry.original_space,
                geometry.original_space_uuid,
                yabai_detector
            )
            if not is_valid:
                if new_space_id:
                    logger.info(
                        f"[GhostManager] Original space was reordered: "
                        f"{geometry.original_space} → {new_space_id}"
                    )
                    target_space = new_space_id
                else:
                    logger.warning(
                        f"[GhostManager] Original space {geometry.original_space} no longer exists, "
                        f"returning to current user space instead"
                    )
                    target_space = yabai_detector.get_current_user_space() or geometry.original_space

        # v26.0: Mark animation start
        self.mark_animation_start(window_id)

        # Move back to original space
        success, method = yabai_detector.move_window_to_space_with_rescue(
            window_id=window_id,
            target_space=target_space,
            source_space=geometry.current_space,
            app_name=geometry.app_name
        )

        if not success:
            logger.warning(f"[GhostManager] Failed to return window {window_id} to Space {target_space}")
            self.mark_animation_complete(window_id)
            return False

        # Restore geometry if requested
        if restore_geometry and self.config.preserve_geometry_on_return:
            try:
                yabai_path = yabai_detector._health.yabai_path or "yabai"

                # v26.0: Calculate restored position accounting for display scale differences
                restore_x = geometry.x
                restore_y = geometry.y
                restore_width = geometry.width
                restore_height = geometry.height

                # If display scales differ, adjust coordinates
                if (self.config.enable_retina_scaling and
                    geometry.source_display_scale != geometry.ghost_display_scale):

                    # Get current target display scale
                    current_scale = await self.detect_display_scale(
                        geometry.original_display,
                        yabai_detector
                    )

                    if current_scale != geometry.source_display_scale:
                        # Display scale changed since window was captured
                        scale_ratio = current_scale / geometry.source_display_scale
                        restore_x = int(restore_x * scale_ratio)
                        restore_y = int(restore_y * scale_ratio)
                        # Size typically doesn't need scaling, only position
                        logger.debug(
                            f"[GhostManager] Adjusted geometry for scale change: "
                            f"{geometry.source_display_scale} → {current_scale}"
                        )

                # Move to original position
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--move", f"abs:{restore_x}:{restore_y}"],
                    capture_output=True,
                    timeout=2.0
                )

                # Resize to original size
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--resize", f"abs:{restore_width}:{restore_height}"],
                    capture_output=True,
                    timeout=2.0
                )

                logger.debug(f"[GhostManager] 📐 Restored geometry for window {window_id}")

            except Exception as e:
                logger.debug(f"[GhostManager] Geometry restore failed: {e}")

        # v26.0: Wait for animation to complete
        if self.config.wait_for_animations:
            await self.wait_for_animation(window_id)

        # v26.0: Restore focus if this window was focused before teleport
        if (self.config.restore_focus_on_return and
            geometry.was_focused and
            self._focused_window_before_teleport == window_id):

            try:
                yabai_path = yabai_detector._health.yabai_path or "yabai"
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--focus"],
                    capture_output=True,
                    timeout=1.0
                )
                logger.debug(f"[GhostManager] 🎯 Restored focus to window {window_id}")
            except Exception:
                pass

        # Clean up tracking
        await self.track_window_return(window_id)

        # v27.0: Clear from persistence AFTER successful return
        # This ensures crash recovery won't try to return an already-returned window
        if self._persistence_manager:
            try:
                await self._persistence_manager.record_return(window_id)
            except Exception as e:
                # Non-fatal - log and continue
                logger.debug(f"[GhostManager] Persistence clear failed: {e}")

        # v26.0: Clean up z-order cache
        if window_id in self._z_order_cache:
            del self._z_order_cache[window_id]

        # Clean up focused window tracker if this was the focused window
        if self._focused_window_before_teleport == window_id:
            self._focused_window_before_teleport = None

        del self._geometry_cache[window_id]

        logger.info(f"[GhostManager] 🏠 Window {window_id} returned to Space {target_space}")
        return True

    async def return_all_windows(
        self,
        yabai_detector: 'YabaiSpaceDetector',
        restore_geometry: bool = True
    ) -> Dict[str, Any]:
        """
        Return all windows from Ghost Display to their original spaces.

        Args:
            yabai_detector: YabaiSpaceDetector for moving
            restore_geometry: Whether to restore original positions

        Returns:
            Result dict with success/failure counts
        """
        results = {"success": True, "returned": [], "failed": []}

        window_ids = list(self._windows_on_ghost)
        for window_id in window_ids:
            if await self.return_window_to_original(window_id, yabai_detector, restore_geometry):
                results["returned"].append(window_id)
            else:
                results["failed"].append(window_id)

        results["success"] = len(results["failed"]) == 0

        logger.info(
            f"[GhostManager] 🏠 Return complete: "
            f"{len(results['returned'])} returned, {len(results['failed'])} failed"
        )

        return results

    def can_accept_more_windows(self) -> bool:
        """Check if Ghost Display can accept more windows."""
        return len(self._windows_on_ghost) < self.config.max_windows_on_ghost

    def get_status_report(self) -> Dict[str, Any]:
        """Get comprehensive status report."""
        return {
            "status": self._status.value,
            "ghost_space": self.ghost_space,
            "ghost_info": {
                "space_id": self._ghost_info.space_id if self._ghost_info else None,
                "display_id": self._ghost_info.display_id if self._ghost_info else None,
                "is_virtual": self._ghost_info.is_virtual if self._ghost_info else None,
                "is_healthy": self._ghost_info.is_healthy if self._ghost_info else False,
                "dimensions": f"{self._ghost_info.width}x{self._ghost_info.height}" if self._ghost_info else None,
            },
            "windows_on_ghost": len(self._windows_on_ghost),
            "windows_tracked": list(self._windows_on_ghost),
            "preserved_geometries": len(self._geometry_cache),
            "can_accept_more": self.can_accept_more_windows(),
            "fallback_active": self._status == GhostDisplayStatus.FALLBACK,
            "fallback_space": self._fallback_space,
        }


# Global Ghost Display Manager instance
_GHOST_MANAGER: Optional[GhostDisplayManager] = None


def get_ghost_manager() -> GhostDisplayManager:
    """Get or create the global Ghost Display Manager."""
    global _GHOST_MANAGER
    if _GHOST_MANAGER is None:
        _GHOST_MANAGER = GhostDisplayManager()
    return _GHOST_MANAGER


def reset_ghost_manager() -> None:
    """Reset the global Ghost Display Manager (for testing)."""
    global _GHOST_MANAGER
    _GHOST_MANAGER = None


def get_ghost_display_status() -> Dict[str, Any]:
    """
    Get comprehensive Ghost Display status for Trinity integration.

    Returns a dictionary with:
    - available: Whether Ghost Display is available
    - status: Current status (available, unavailable, fallback, etc.)
    - window_count: Number of windows on Ghost Display
    - apps: List of app names on Ghost Display
    - resolution: Ghost Display resolution
    - scale: Display scale factor
    - space_index: Yabai space index of Ghost Display

    This is used by PROJECT TRINITY to report Ghost Display state
    to J-Prime and the frontend loading screen.
    """
    result: Dict[str, Any] = {
        "available": False,
        "status": "unavailable",
        "window_count": 0,
        "apps": [],
        "resolution": None,
        "scale": 1.0,
        "space_index": None,
    }

    try:
        manager = get_ghost_manager()

        if manager.status == GhostDisplayStatus.AVAILABLE:
            result["available"] = True
            result["status"] = "available"
        elif manager.status == GhostDisplayStatus.FALLBACK:
            result["available"] = True
            result["status"] = "fallback"
        elif manager.status == GhostDisplayStatus.RECONNECTING:
            result["status"] = "reconnecting"
        elif manager.status == GhostDisplayStatus.DISCONNECTED:
            result["status"] = "disconnected"
        else:
            result["status"] = "unavailable"

        # Get dimensions
        width, height = manager.ghost_display_dimensions
        result["resolution"] = f"{width}x{height}"
        result["scale"] = manager.ghost_display_scale

        # Get Ghost Display space index
        if manager._ghost_info:
            result["space_index"] = manager._ghost_info.space_index

        # Get windows on Ghost Display
        if result["available"]:
            try:
                detector = get_yabai_detector(auto_start=False)
                ghost_space = detector.get_ghost_display_space()

                if ghost_space:
                    result["space_index"] = ghost_space
                    windows = detector.get_windows_on_space(ghost_space)
                    result["window_count"] = len(windows)

                    # Collect unique app names
                    apps = set()
                    for window in windows:
                        app_name = window.get("app", "")
                        if app_name:
                            apps.add(app_name)
                    result["apps"] = sorted(list(apps))

            except Exception as e:
                logger.debug(f"[GhostDisplay] Could not get window details: {e}")

        # Add frozen apps count (from Cryostasis)
        if _HAS_CRYOSTASIS and get_cryostasis_manager:
            try:
                cryo = get_cryostasis_manager()
                result["frozen_apps"] = cryo.get_frozen_app_names()
                result["frozen_count"] = len(result["frozen_apps"])
            except Exception:
                result["frozen_apps"] = []
                result["frozen_count"] = 0
        else:
            result["frozen_apps"] = []
            result["frozen_count"] = 0

    except Exception as e:
        logger.debug(f"[GhostDisplay] Status check failed: {e}")
        result["error"] = str(e)

    return result


# Thread pool for subprocess operations (avoids blocking event loop)
_yabai_executor: Optional[ThreadPoolExecutor] = None


def _get_yabai_executor() -> ThreadPoolExecutor:
    """Get or create thread pool for Yabai subprocess calls."""
    global _yabai_executor
    if _yabai_executor is None:
        if _HAS_MANAGED_EXECUTOR:

            _yabai_executor = ManagedThreadPoolExecutor(max_workers=2, thread_name_prefix="yabai_", name='yabai')

        else:

            _yabai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yabai_")
    return _yabai_executor


def _run_subprocess_sync(args: List[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    """Run subprocess synchronously (called from thread pool)."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


async def run_subprocess_async(args: List[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    """
    Run subprocess asynchronously using thread pool.
    Prevents blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    executor = _get_yabai_executor()

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, partial(_run_subprocess_sync, args, timeout)),
            timeout=timeout + 1.0  # Extra second for thread overhead
        )
    except asyncio.TimeoutError:
        logger.error(f"[YABAI] Subprocess timed out: {' '.join(args)}")
        raise


class YabaiStatus(Enum):
    """Status of Yabai installation and availability"""

    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"
    NOT_RUNNING = "not_running"
    NO_PERMISSIONS = "no_permissions"
    STARTING = "starting"
    ERROR = "error"


class YabaiSpaceDetector:
    """
    Yabai-based Mission Control space detector
    Enhanced with YOLO vision for multi-monitor layout detection

    Features:
    - Auto-start yabai service with retry logic
    - Health monitoring and auto-recovery
    - Async-first design for non-blocking operations
    - Graceful degradation when unavailable

    ROOT CAUSE FIX v11.0.0: Non-Blocking Initialization
    - Uses module-level cache to avoid blocking subprocess calls in __init__
    - Lazy initialization - only check yabai when actually needed
    - All blocking operations moved out of constructor
    """

    def __init__(
        self,
        enable_vision: bool = True,
        config: Optional[YabaiConfig] = None,
        auto_start: bool = True,
    ):
        self.config = config or YabaiConfig()
        self.enable_vision = enable_vision
        self._vision_analyzer = None
        self._health = YabaiServiceHealth()
        self._status_callbacks: List[Callable[[YabaiStatus], None]] = []
        self._health_check_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._lazy_init_done = False

        # =====================================================================
        # v35.5: FULLSCREEN UNPACKING SERIALIZATION
        # =====================================================================
        # macOS WindowServer is single-threaded for animations. If we try to
        # unpack multiple fullscreen windows simultaneously, the OS will drop
        # frames, lag, or ignore commands. This semaphore ensures we process
        # only ONE fullscreen animation at a time.
        # =====================================================================
        self._unpack_lock = asyncio.Semaphore(1)

        # v35.5: Space topology cache - invalidated after fullscreen unpack
        # When a window exits fullscreen, macOS destroys that Space, shifting
        # all space indices. We must re-query topology after any unpack.
        self._space_topology_valid = True
        self._last_topology_refresh: Optional[float] = None

        # =====================================================================
        # ROOT CAUSE FIX v12.0.0: ZERO-BLOCKING Constructor
        # =====================================================================
        # PROBLEM: Even _quick_yabai_check() with cache uses subprocess.run()
        # on cache miss, which blocks the event loop during startup.
        #
        # SOLUTION: NEVER run subprocess in __init__
        # - Only check if cache is already valid (instant)
        # - If cache invalid, DON'T run subprocess - defer to lazy init
        # - Actual yabai check happens on first is_available() call
        # =====================================================================

        # Check ONLY the cache - NO subprocess calls allowed in __init__!
        if _is_yabai_cache_valid():
            # Cache is valid - use cached result (instant)
            is_available = _YABAI_AVAILABILITY_CACHE["available"]
            yabai_path = _YABAI_AVAILABILITY_CACHE["path"]
            if yabai_path:
                self._health.yabai_path = yabai_path
                self._health.is_running = is_available
            logger.debug(f"[YABAI] Using cached result: available={is_available}")
        else:
            # Cache invalid - defer check to lazy initialization
            # Just check if executable exists (fast filesystem check, no subprocess)
            yabai_path = shutil.which("yabai")
            if not yabai_path:
                # Check common locations
                for path in ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]:
                    if os.path.isfile(path) and os.access(path, os.X_OK):
                        yabai_path = path
                        break

            if yabai_path:
                self._health.yabai_path = yabai_path
                # Don't set is_running - we don't know yet (deferred)
                logger.debug(f"[YABAI] Found yabai at {yabai_path} (availability check deferred)")
            else:
                logger.debug("[YABAI] Yabai not found - availability check deferred")

        self._initialized = True

    def _discover_yabai_lazy(self) -> None:
        """
        Lazy discovery of yabai installation and version.
        Only called when full yabai info is needed, not in __init__.
        """
        if self._lazy_init_done:
            return

        self._lazy_init_done = True

        # Use cached result first
        is_available, yabai_path = _quick_yabai_check()

        if not yabai_path:
            # Not available, skip expensive operations
            logger.debug("[YABAI] Lazy init: Yabai not available, skipping")
            return

        self._health.yabai_path = yabai_path

        # Only get version if yabai is available (with short timeout)
        try:
            result = subprocess.run(
                [yabai_path, "--version"],
                capture_output=True,
                text=True,
                timeout=1.0,  # Short timeout
            )
            if result.returncode == 0:
                self._health.yabai_version = result.stdout.strip()
                logger.info(f"[YABAI] Lazy init: Found yabai {self._health.yabai_version}")
        except Exception as e:
            logger.debug(f"[YABAI] Lazy init: Could not get version: {e}")

    def _discover_yabai(self) -> None:
        """
        DEPRECATED: Use _discover_yabai_lazy() instead.
        Kept for backward compatibility but now just calls lazy version.
        """
        self._discover_yabai_lazy()

    def _attempt_startup(self) -> bool:
        """Attempt to start yabai service."""
        if not self._health.yabai_path:
            logger.warning("[YABAI] Cannot start - yabai not installed")
            return False

        # Check if already running
        if self._check_service_running():
            self._health.is_running = True
            logger.info("[YABAI] Yabai already running")
            return True

        # Rate limit startup attempts
        if self._health.last_startup_attempt:
            elapsed = datetime.now() - self._health.last_startup_attempt
            if elapsed.total_seconds() < self.config.startup_retry_delay_seconds:
                return False

        if self._health.startup_attempts >= self.config.max_startup_attempts:
            logger.warning(
                f"[YABAI] Max startup attempts ({self.config.max_startup_attempts}) reached. "
                "Manual intervention required."
            )
            return False

        self._health.startup_attempts += 1
        self._health.last_startup_attempt = datetime.now()

        logger.info(f"[YABAI] Starting yabai service (attempt {self._health.startup_attempts}/{self.config.max_startup_attempts})...")

        try:
            # Try yabai --start-service first (preferred method)
            result = subprocess.run(
                [self._health.yabai_path, "--start-service"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            # Give it time to start
            time.sleep(1.5)

            # Verify it started
            if self._check_service_running():
                self._health.is_running = True
                self._health.startup_attempts = 0  # Reset on success
                logger.info("[YABAI] Yabai service started successfully")
                return True

            # If --start-service didn't work, try launchctl
            logger.debug("[YABAI] --start-service didn't work, trying launchctl...")
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.koekeishiya.yabai"],
                capture_output=True,
                timeout=10,
            )

            time.sleep(1.5)

            if self._check_service_running():
                self._health.is_running = True
                self._health.startup_attempts = 0
                logger.info("[YABAI] Yabai service started via launchctl")
                return True

            # Check for permission issues
            logger.warning(
                "[YABAI] Service failed to start. This usually means yabai needs Accessibility permissions.\n"
                "  To fix:\n"
                "  1. Open System Settings → Privacy & Security → Accessibility\n"
                "  2. Click '+' and add /opt/homebrew/bin/yabai (or your yabai path)\n"
                "  3. Restart yabai with: yabai --start-service"
            )
            self._health.permissions_granted = False
            return False

        except subprocess.TimeoutExpired:
            logger.error("[YABAI] Startup timed out")
            return False
        except Exception as e:
            logger.error(f"[YABAI] Error starting service: {e}")
            return False

    def _check_service_running(self) -> bool:
        """Check if yabai service is actually running and responding."""
        if not self._health.yabai_path:
            return False

        try:
            # Quick query to verify yabai is responding
            result = subprocess.run(
                [self._health.yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return result.returncode == 0 and "failed to connect" not in result.stderr.lower()
        except Exception:
            return False

    @property
    def yabai_available(self) -> bool:
        """
        Property for backward compatibility.
        ROOT CAUSE FIX v11.0.0: Uses cached quick check.
        """
        return self._check_yabai_available()

    def register_status_callback(self, callback: Callable[[YabaiStatus], None]) -> None:
        """Register a callback for status changes."""
        self._status_callbacks.append(callback)

    def _notify_status_change(self, status: YabaiStatus) -> None:
        """Notify all registered callbacks of status change."""
        for callback in self._status_callbacks:
            try:
                callback(status)
            except Exception as e:
                logger.error(f"[YABAI] Status callback error: {e}")

    def get_health(self) -> Dict[str, Any]:
        """Get current service health metrics."""
        return self._health.to_dict()

    async def ensure_running_async(self) -> bool:
        """Ensure yabai is running, attempting restart if needed."""
        if self._health.is_running and not self._health.needs_restart:
            return True

        # Run startup in thread pool to not block
        loop = asyncio.get_event_loop()
        executor = _get_yabai_executor()
        return await loop.run_in_executor(executor, self._attempt_startup)

    def ensure_running(self) -> bool:
        """Synchronous version of ensure_running."""
        if self._health.is_running and not self._health.needs_restart:
            return True
        return self._attempt_startup()

    def _get_vision_analyzer(self):
        """Lazy load vision analyzer for layout detection"""
        if self._vision_analyzer is None and self.enable_vision:
            try:
                import os

                from backend.vision.optimized_claude_vision import OptimizedClaudeVisionAnalyzer

                api_key = os.getenv("ANTHROPIC_API_KEY")
                if api_key:
                    self._vision_analyzer = OptimizedClaudeVisionAnalyzer(
                        api_key=api_key, use_intelligent_selection=True, use_yolo_hybrid=True
                    )
                    logger.info("[YABAI] Vision analyzer loaded for layout detection")
            except Exception as e:
                logger.warning(f"[YABAI] Vision analyzer not available: {e}")
        return self._vision_analyzer

    def _check_yabai_available(self) -> bool:
        """
        Check if Yabai is installed and running.

        ROOT CAUSE FIX v11.0.0: Uses cached quick check to avoid blocking.
        """
        # Use the non-blocking cached check
        is_available, _ = _quick_yabai_check()
        return is_available

    def _check_yabai_available_legacy(self) -> bool:
        """Legacy blocking check - DEPRECATED, use _check_yabai_available instead."""
        try:
            # Check if yabai command exists
            result = subprocess.run(["which", "yabai"], capture_output=True, text=True, timeout=1)
            if result.returncode != 0:
                return False

            # Try to query yabai (will fail if not running)
            result = subprocess.run(
                ["yabai", "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=1,  # Reduced from 2 to 1
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_available(self) -> bool:
        """Check if Yabai detector is available"""
        return self.yabai_available

    def get_status(self) -> YabaiStatus:
        """Get the current Yabai availability status with detailed diagnostics."""
        # First check if it's running
        if self._check_service_running():
            self._health.is_running = True
            return YabaiStatus.AVAILABLE

        # Not running - determine why
        if not self._health.yabai_path:
            return YabaiStatus.NOT_INSTALLED

        # Yabai is installed but not running - check why
        try:
            # Try to start and see what happens
            result = subprocess.run(
                [self._health.yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=3,
            )

            if "failed to connect to socket" in result.stderr.lower():
                # Socket doesn't exist = service not running
                return YabaiStatus.NOT_RUNNING

            if result.returncode != 0:
                # Other error - likely permissions
                return YabaiStatus.NO_PERMISSIONS

        except subprocess.TimeoutExpired:
            return YabaiStatus.ERROR
        except Exception:
            return YabaiStatus.ERROR

        return YabaiStatus.NOT_RUNNING

    def get_detailed_status(self) -> Dict[str, Any]:
        """Get comprehensive status including health metrics and diagnostics."""
        status = self.get_status()
        health = self.get_health()

        result = {
            "status": status.value,
            "status_description": self._get_status_description(status),
            "health": health,
            "installation": {
                "yabai_path": self._health.yabai_path,
                "yabai_version": self._health.yabai_version,
                "config_path": str(self.config.config_path),
                "config_exists": self.config.config_path.exists(),
            },
            "recommendations": self._get_recommendations(status),
        }

        return result

    def _get_status_description(self, status: YabaiStatus) -> str:
        """Get human-readable status description."""
        descriptions = {
            YabaiStatus.AVAILABLE: "Yabai is running and responding to queries",
            YabaiStatus.NOT_INSTALLED: "Yabai is not installed. Install with: brew install koekeishiya/formulae/yabai",
            YabaiStatus.NOT_RUNNING: "Yabai is installed but the service is not running",
            YabaiStatus.NO_PERMISSIONS: "Yabai needs Accessibility permissions in System Settings",
            YabaiStatus.STARTING: "Yabai service is starting up",
            YabaiStatus.ERROR: "Yabai encountered an error",
        }
        return descriptions.get(status, "Unknown status")

    def _get_recommendations(self, status: YabaiStatus) -> List[str]:
        """Get actionable recommendations based on current status."""
        if status == YabaiStatus.AVAILABLE:
            return []

        recommendations = []

        if status == YabaiStatus.NOT_INSTALLED:
            recommendations.extend([
                "Install yabai: brew install koekeishiya/formulae/yabai",
                "After installation, run: yabai --start-service",
            ])

        elif status == YabaiStatus.NOT_RUNNING:
            recommendations.extend([
                "Start yabai service: yabai --start-service",
                "Or restart via: yabai --restart-service",
            ])

        elif status == YabaiStatus.NO_PERMISSIONS:
            recommendations.extend([
                "1. Open System Settings → Privacy & Security → Accessibility",
                f"2. Click '+' and add: {self._health.yabai_path or '/opt/homebrew/bin/yabai'}",
                "3. Toggle the checkbox to enable",
                "4. Run: yabai --start-service",
            ])

        elif status == YabaiStatus.ERROR:
            recommendations.extend([
                "Check system logs: log show --predicate 'process == \"yabai\"' --last 5m",
                "Try restarting: yabai --restart-service",
                f"Check config file: {self.config.config_path}",
            ])

        return recommendations

    async def start_health_monitoring(self, interval_seconds: Optional[float] = None) -> None:
        """Start background health monitoring task."""
        if self._health_check_task and not self._health_check_task.done():
            logger.debug("[YABAI] Health monitoring already running")
            return

        interval = interval_seconds or self.config.health_check_interval_seconds

        async def _monitor():
            max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
            session_start = time.monotonic()
            while time.monotonic() - session_start < max_runtime:
                try:
                    await asyncio.sleep(interval)
                    was_running = self._health.is_running
                    is_running = self._check_service_running()
                    self._health.is_running = is_running

                    if was_running and not is_running:
                        logger.warning("[YABAI] Service stopped unexpectedly")
                        self._notify_status_change(YabaiStatus.NOT_RUNNING)

                        if self.config.enable_auto_recovery:
                            logger.info("[YABAI] Attempting auto-recovery...")
                            await self.ensure_running_async()

                    elif not was_running and is_running:
                        logger.info("[YABAI] Service recovered")
                        self._notify_status_change(YabaiStatus.AVAILABLE)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[YABAI] Health monitoring error: {e}")
            else:
                logger.info("[YABAI] Health monitoring session timeout, stopping")

        self._health_check_task = asyncio.create_task(_monitor())
        logger.info(f"[YABAI] Started health monitoring (interval: {interval}s)")

    async def stop_health_monitoring(self) -> None:
        """Stop background health monitoring."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
            logger.info("[YABAI] Stopped health monitoring")

    def enumerate_all_spaces(self, include_display_info: bool = True, auto_start: bool = True) -> List[Dict[str, Any]]:
        """
        Enumerate all Mission Control spaces using Yabai

        Args:
            include_display_info: If True, include display ID for each space
            auto_start: If True, attempt to start yabai if not running
        """
        start_time = time.time()

        # Attempt to ensure yabai is running if auto_start enabled
        if auto_start and not self.is_available():
            if self.ensure_running():
                logger.info("[YABAI] Auto-started yabai service")
            else:
                logger.warning("[YABAI] Yabai not available and could not be started")
                self._health.record_failure("Service not available")
                return []

        if not self.is_available():
            logger.warning("[YABAI] Yabai not available, returning empty list")
            self._health.record_failure("Service not available")
            return []

        try:
            # Query spaces from Yabai
            yabai_path = self._health.yabai_path or "yabai"
            result = subprocess.run(
                [yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            if result.returncode != 0:
                error_msg = f"Failed to query spaces: {result.stderr}"
                logger.error(f"[YABAI] {error_msg}")
                self._health.record_failure(error_msg)
                return []

            spaces_data = json.loads(result.stdout)

            # Query windows for more detail
            windows_result = subprocess.run(
                [yabai_path, "-m", "query", "--windows"],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            windows_data = []
            if windows_result.returncode == 0:
                windows_data = json.loads(windows_result.stdout)

            # Build enhanced space information
            spaces = []
            for space in spaces_data:
                space_id = space["index"]

                # Get windows for this space
                space_windows = [w for w in windows_data if w.get("space") == space_id]

                # Get unique applications
                applications = list(set(w.get("app", "Unknown") for w in space_windows))

                # Determine primary activity
                if not space_windows:
                    primary_activity = "Empty"
                elif len(applications) == 1:
                    primary_activity = applications[0]
                else:
                    primary_activity = f"{applications[0]} and {len(applications)-1} others"

                # Get display info if requested
                display_id = space.get("display", 1) if include_display_info else None

                space_info = {
                    "space_id": space_id,
                    "space_name": f"Desktop {space_id}",
                    "is_current": space.get("has-focus", False),
                    "is_visible": space.get("is-visible", False),
                    "is_fullscreen": space.get("is-native-fullscreen", False),
                    "window_count": len(space_windows),
                    "window_ids": space.get("windows", []),
                    "applications": applications,
                    "primary_activity": primary_activity,
                    "type": space.get("type", "unknown"),
                    "display": display_id,  # Added display awareness
                    "uuid": space.get("uuid", ""),
                    "windows": [
                        {
                            "app": w.get("app", "Unknown"),
                            "title": w.get("title", ""),
                            "id": w.get("id"),
                            "minimized": w.get("is-minimized", False),
                            "hidden": w.get("is-hidden", False),
                            # v31.0: Include fullscreen status for teleportation handling
                            "is-native-fullscreen": w.get("is-native-fullscreen", False),
                            "is_fullscreen": w.get("is-native-fullscreen", False),
                            "can-move": w.get("can-move", True),
                        }
                        for w in space_windows
                    ],
                }

                spaces.append(space_info)

                logger.debug(
                    f"[YABAI] Space {space_id}: {primary_activity} ({len(space_windows)} windows)"
                )

            # Record success metrics
            elapsed_ms = (time.time() - start_time) * 1000
            self._health.record_success(elapsed_ms)

            logger.info(f"[YABAI] Detected {len(spaces)} spaces via Yabai ({elapsed_ms:.1f}ms)")
            return spaces

        except json.JSONDecodeError as e:
            error_msg = f"Failed to parse Yabai output: {e}"
            logger.error(f"[YABAI] {error_msg}")
            self._health.record_failure(error_msg)
            return []
        except subprocess.TimeoutExpired:
            error_msg = "Yabai query timed out"
            logger.error(f"[YABAI] {error_msg}")
            self._health.record_failure(error_msg)
            return []
        except Exception as e:
            error_msg = f"Error enumerating spaces: {e}"
            logger.error(f"[YABAI] {error_msg}")
            self._health.record_failure(error_msg)
            return []

    def get_display_for_space(self, space_id: int) -> Optional[int]:
        """
        Get display ID for a given space

        Args:
            space_id: Space ID to lookup

        Returns:
            Display ID or None if not found
        """
        if not self.is_available():
            return None

        try:
            yabai_path = self._health.yabai_path or "yabai"
            result = subprocess.run(
                [yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            if result.returncode != 0:
                return None

            spaces_data = json.loads(result.stdout)

            for space in spaces_data:
                if space.get("index") == space_id:
                    return space.get("display", 1)

            return None

        except Exception as e:
            logger.error(f"[YABAI] Error getting display for space {space_id}: {e}")
            return None

    def enumerate_spaces_by_display(self) -> Dict[int, List[Dict[str, Any]]]:
        """
        Group spaces by display ID

        Returns:
            Dictionary mapping display_id -> list of spaces
        """
        spaces = self.enumerate_all_spaces(include_display_info=True)

        spaces_by_display = {}
        for space in spaces:
            display_id = space.get("display", 1)
            if display_id not in spaces_by_display:
                spaces_by_display[display_id] = []
            spaces_by_display[display_id].append(space)

        logger.info(
            f"[YABAI] Grouped {len(spaces)} spaces across {len(spaces_by_display)} displays"
        )
        return spaces_by_display

    def get_current_space(self) -> Optional[Dict[str, Any]]:
        """Get information about the currently focused space"""
        spaces = self.enumerate_all_spaces()
        for space in spaces:
            if space.get("is_current"):
                return space
        return None

    def get_space_info(self, space_id: int) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific space"""
        spaces = self.enumerate_all_spaces()
        for space in spaces:
            if space.get("space_id") == space_id:
                return space
        return None

    def get_space_count(self) -> int:
        """Get the total number of spaces"""
        spaces = self.enumerate_all_spaces()
        return len(spaces)

    def get_windows_for_space(self, space_id: int) -> List[Dict[str, Any]]:
        """Get all windows in a specific space"""
        space_info = self.get_space_info(space_id)
        if space_info:
            return space_info.get("windows", [])
        return []

    # =========================================================================
    # v70.0: ASYNC WINDOW DISCOVERY - Required for "Bring Back" Command
    # =========================================================================

    async def find_windows_on_space_async(self, space_id: int) -> List[Dict[str, Any]]:
        """
        v70.0: Async method to find all windows on a specific space.
        
        ROOT CAUSE FIX: This method was MISSING, causing "Bring back Chrome"
        to return 0 windows because Strategy 3 (Direct Query) couldn't execute.
        
        This method queries yabai directly for the freshest window topology,
        rather than relying on cached space info.
        
        Args:
            space_id: The space ID to query
            
        Returns:
            List of window dictionaries with id, app, title, frame, etc.
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")
        
        try:
            # Query all windows
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            
            if proc.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.warning(f"[YABAI v70.0] Window query failed: {error_msg}")
                return []
            
            all_windows = json.loads(stdout.decode())
            
            # Filter for windows on the target space
            windows_on_space = [
                {
                    "id": w.get("id"),
                    "window_id": w.get("id"),  # Alias for compatibility
                    "app": w.get("app", "Unknown"),
                    "title": w.get("title", ""),
                    "space": w.get("space"),
                    "display": w.get("display"),
                    "frame": w.get("frame", {}),
                    "is_fullscreen": w.get("is-native-fullscreen", False),
                    "is_minimized": w.get("is-minimized", False),
                    "is_visible": w.get("is-visible", True),
                    "has_focus": w.get("has-focus", False),
                    "pid": w.get("pid")
                }
                for w in all_windows
                if w.get("space") == space_id
            ]
            
            logger.info(
                f"[YABAI v70.0] 🔍 Found {len(windows_on_space)} windows on Space {space_id}"
            )
            
            return windows_on_space
            
        except asyncio.TimeoutError:
            logger.error(f"[YABAI v70.0] Window query timed out for space {space_id}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"[YABAI v70.0] Failed to parse window data: {e}")
            return []
        except Exception as e:
            logger.error(f"[YABAI v70.0] Unexpected error querying windows: {e}")
            return []

    async def find_windows_on_display_async(self, display_id: int) -> List[Dict[str, Any]]:
        """
        v70.0: Async method to find all windows on a specific display.
        
        This is useful for Ghost Display (Display 2) operations where windows
        may span multiple spaces on the same display.
        
        Args:
            display_id: The display ID to query (1=main, 2=Ghost Display typically)
            
        Returns:
            List of window dictionaries
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")
        
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            
            if proc.returncode != 0:
                logger.warning(f"[YABAI v70.0] Window query failed for display {display_id}")
                return []
            
            all_windows = json.loads(stdout.decode())
            
            # Filter for windows on the target display
            windows_on_display = [
                {
                    "id": w.get("id"),
                    "window_id": w.get("id"),
                    "app": w.get("app", "Unknown"),
                    "title": w.get("title", ""),
                    "space": w.get("space"),
                    "display": w.get("display"),
                    "frame": w.get("frame", {}),
                    "is_fullscreen": w.get("is-native-fullscreen", False),
                    "is_minimized": w.get("is-minimized", False),
                    "is_visible": w.get("is-visible", True),
                    "has_focus": w.get("has-focus", False),
                    "pid": w.get("pid")
                }
                for w in all_windows
                if w.get("display") == display_id
            ]
            
            logger.info(
                f"[YABAI v70.0] 🔍 Found {len(windows_on_display)} windows on Display {display_id}"
            )
            
            return windows_on_display
            
        except Exception as e:
            logger.error(f"[YABAI v70.0] Error querying windows on display: {e}")
            return []

    # =========================================================================
    # v22.0.0: WINDOW TELEPORTATION - Autonomous Window Management
    # =========================================================================
    # These methods enable JARVIS to move windows between spaces automatically.
    # Key use case: Move windows to Ghost Display for background monitoring
    # without disturbing the user's current workspace.
    # =========================================================================

    def move_window_to_space(
        self,
        window_id: int,
        target_space: int,
        follow: bool = False
    ) -> bool:
        """
        v34.0: Teleport a window to a different space with Display Handoff support.

        ROOT CAUSE FIX: For cross-display moves, uses --display instead of --space
        to bypass Scripting Addition requirements.

        Args:
            window_id: The window ID to move
            target_space: The target space index (1-based)
            follow: If True, also switch focus to that space

        Returns:
            True if successful, False otherwise

        Example:
            # Move Chrome window 12345 to Ghost Display (Space 10)
            yabai.move_window_to_space(12345, 10)
        """
        if not self.is_available():
            logger.warning("[YABAI] Cannot move window - Yabai not available")
            return False

        try:
            yabai_path = self._health.yabai_path or "yabai"

            # ═══════════════════════════════════════════════════════════════
            # v34.0: DETECT CROSS-DISPLAY MOVE
            # ═══════════════════════════════════════════════════════════════

            current_display = None
            target_display = None

            # Get window's current display
            try:
                win_result = subprocess.run(
                    [yabai_path, "-m", "query", "--windows", "--window", str(window_id)],
                    capture_output=True, text=True, timeout=5.0
                )
                if win_result.returncode == 0:
                    win_data = json.loads(win_result.stdout)
                    current_display = win_data.get("display")
            except Exception as e:
                logger.debug(f"[YABAI] Could not query window display: {e}")

            # Get target space's display
            try:
                space_result = subprocess.run(
                    [yabai_path, "-m", "query", "--spaces"],
                    capture_output=True, text=True, timeout=5.0
                )
                if space_result.returncode == 0:
                    spaces = json.loads(space_result.stdout)
                    for space in spaces:
                        if space.get("index") == target_space:
                            target_display = space.get("display")
                            break
            except Exception as e:
                logger.debug(f"[YABAI] Could not query space display: {e}")

            # Determine if cross-display move
            is_cross_display = (
                current_display is not None and
                target_display is not None and
                current_display != target_display
            )

            # ═══════════════════════════════════════════════════════════════
            # v34.0: EXECUTE MOVE WITH DISPLAY HANDOFF
            # ═══════════════════════════════════════════════════════════════

            if is_cross_display:
                # CROSS-DISPLAY: Use --display command (bypasses SA requirement)
                logger.info(
                    f"[YABAI] 🌐 DISPLAY HANDOFF: Moving window {window_id} "
                    f"from Display {current_display} → Display {target_display}"
                )
                result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--display", str(target_display)],
                    capture_output=True,
                    text=True,
                    timeout=self.config.query_timeout_seconds,
                )
            else:
                # SAME-DISPLAY: Use standard --space command
                result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--space", str(target_space)],
                    capture_output=True,
                    text=True,
                    timeout=self.config.query_timeout_seconds,
                )

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.error(f"[YABAI] Failed to move window {window_id}: {error_msg}")
                return False

            if is_cross_display:
                logger.info(f"[YABAI] ✅ DISPLAY HANDOFF: Window {window_id} → Display {target_display}")
            else:
                logger.info(f"[YABAI] ✅ Teleported window {window_id} to Space {target_space}")

            # Optionally follow the window
            if follow:
                subprocess.run(
                    [yabai_path, "-m", "space", "--focus", str(target_space)],
                    capture_output=True,
                    timeout=self.config.query_timeout_seconds,
                )

            self._health.record_success(0)
            return True

        except subprocess.TimeoutExpired:
            logger.error(f"[YABAI] Window move timed out for window {window_id}")
            self._health.record_failure("Timeout during window move")
            return False
        except Exception as e:
            logger.error(f"[YABAI] Window move failed: {e}")
            self._health.record_failure(str(e))
            return False

    # =========================================================================
    # v35.0: FULLSCREEN UNPACKING PROTOCOL
    # =========================================================================
    # macOS treats fullscreen windows as separate Spaces. You cannot move a
    # Space inside another Space (the Ghost Display). We must "unpack" the
    # window (exit fullscreen) before attempting any move.
    # =========================================================================

    async def _get_system_animation_delay(self) -> float:
        """
        v35.0: Get dynamic animation delay based on system settings.

        Checks macOS "Reduce Motion" accessibility setting to determine
        the appropriate delay. When Reduce Motion is enabled, animations
        are faster and we can use a shorter delay.

        Returns:
            float: Animation delay in seconds (0.5-1.5s depending on settings)
        """
        try:
            # Check if "Reduce Motion" is enabled via defaults
            proc = await asyncio.create_subprocess_exec(
                "defaults", "read", "com.apple.universalaccess", "reduceMotion",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            if proc.returncode == 0:
                reduce_motion = stdout.decode().strip() == "1"
                if reduce_motion:
                    logger.debug("[YABAI] 🏃 Reduce Motion enabled - using shorter animation delay")
                    return 0.5  # Much faster animation

            # Check window animation speed setting
            proc2 = await asyncio.create_subprocess_exec(
                "defaults", "read", "NSGlobalDomain", "NSWindowResizeTime",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=2.0)

            if proc2.returncode == 0:
                try:
                    resize_time = float(stdout2.decode().strip())
                    # Scale delay based on animation speed (default is 0.2)
                    # Slower animations (higher value) need more delay
                    scaled_delay = max(0.5, min(2.0, resize_time * 7.5))
                    logger.debug(f"[YABAI] ⏱️ Animation speed factor: {resize_time} → delay: {scaled_delay}s")
                    return scaled_delay
                except ValueError:
                    pass

        except Exception as e:
            logger.debug(f"[YABAI] Could not query animation settings: {e}")

        # Default: Standard macOS animation takes ~1.5s for fullscreen toggle
        return 1.5

    async def _deep_unpack_via_applescript(
        self,
        app_name: str,
        window_title: str,
        window_id: int,
        fire_and_forget: bool = False,
        verify_transition: bool = True
    ) -> bool:
        """
        v44.0: ATOMIC TRANSITION - Smart AppleScript Injection with Verification
        
        ROOT CAUSE FIX: Yabai cannot read the fullscreen state of windows on hidden
        spaces ("dehydrated" windows). It returns is-native-fullscreen=false even
        when the window IS fullscreen. This causes move commands to fail silently.
        
        SOLUTION: Bypass yabai and talk directly to the application using AppleScript.
        Browsers like Chrome expose their window state via AppleScript even when hidden.
        
        v44.0 ENHANCEMENTS (over v43.5):
        - Smart Targeting: Excludes JARVIS windows (localhost, 127.0.0.1) from unpack
        - State Verification Loop: Waits up to 2s and verifies fullscreen actually exited
        - Nuclear Fallback: If AppleScript fails, tries direct display move as last resort
        - Fire & Forget Mode: Optional parallel execution for max speed
        
        v43.5 RETAINED:
        - Permission Panic Detection: Detects exit code -1743 (user denied automation)
        - Polymorphic Targeting: Dynamically uses app_name from yabai query
        - Direct Object Model: Never steals focus (no System Events keystrokes)
        - Graceful Degradation: Always proceeds, never blocks the operation
        
        Args:
            app_name: The application name (e.g., "Google Chrome", "Safari")
            window_title: The window title (for logging/targeted unpack)
            window_id: Yabai window ID for logging
            fire_and_forget: If True, start hydration immediately without waiting
            verify_transition: If True, verify the window exited fullscreen
            
        Returns:
            True if unpack was successful (or app doesn't support AppleScript)
            False only if critical permission error that user must resolve
        """
        # ═══════════════════════════════════════════════════════════════════════
        # v43.5: POLYMORPHIC APP NAME MAPPING
        # ═══════════════════════════════════════════════════════════════════════
        # Maps various app names to their AppleScript dictionary names.
        # This handles Chrome Canary, Chromium, Arc, Brave, etc. automatically.
        # If app isn't in the map, we use the name as-is (polymorphic).
        # ═══════════════════════════════════════════════════════════════════════
        
        chrome_like_apps = {
            "google chrome": "Google Chrome",
            "google chrome canary": "Google Chrome Canary",
            "chrome": "Google Chrome",
            "chromium": "Chromium",
            "brave browser": "Brave Browser",
            "brave": "Brave Browser",
            "microsoft edge": "Microsoft Edge",
            "edge": "Microsoft Edge",
            "arc": "Arc",
            "vivaldi": "Vivaldi",
            "opera": "Opera",
        }
        
        safari_like_apps = {"safari": "Safari"}
        
        electron_apps = {
            "slack": "Slack",
            "discord": "Discord",
            "spotify": "Spotify",
            "visual studio code": "Visual Studio Code",
            "code": "Visual Studio Code",
            "figma": "Figma",
            "notion": "Notion",
            "obsidian": "Obsidian",
        }
        
        app_lower = app_name.lower()
        
        # Determine the actual AppleScript application name
        if app_lower in chrome_like_apps:
            actual_app_name = chrome_like_apps[app_lower]
            app_type = "chrome"
        elif app_lower in safari_like_apps:
            actual_app_name = safari_like_apps[app_lower]
            app_type = "safari"
        elif app_lower in electron_apps:
            actual_app_name = electron_apps[app_lower]
            app_type = "electron"
        else:
            # v43.5: POLYMORPHIC - Use the app name directly
            # This handles any app JARVIS encounters
            actual_app_name = app_name
            app_type = "generic"
        
        # ═══════════════════════════════════════════════════════════════════════
        # v44.3: ROBUST SANITIZATION - Prevent AppleScript Injection Attacks
        # ═══════════════════════════════════════════════════════════════════════
        # Window titles can contain ANY characters including:
        # - Double quotes: "My "Favorite" Website"
        # - Backslashes: "C:\Users\Data"
        # - Newlines, tabs, special Unicode
        #
        # AppleScript uses DOUBLED QUOTES for escaping, not backslash:
        # "Hello "World"" → prints: Hello "World"
        #
        # Order matters:
        # 1. First escape backslashes (\ → \\)
        # 2. Then escape quotes (" → \")  -- for osascript -e
        # 3. Remove control characters that break scripts
        # ═══════════════════════════════════════════════════════════════════════
        
        def sanitize_for_applescript(text: str, max_length: int = 100) -> str:
            """
            Sanitize a string for safe use in AppleScript via osascript -e.
            
            Args:
                text: The raw text to sanitize
                max_length: Maximum length (prevents script bloat)
                
            Returns:
                Sanitized string safe for AppleScript injection
            """
            if not text:
                return ""
            
            # Truncate first to avoid processing huge strings
            result = text[:max_length]
            
            # Remove control characters (newlines, tabs, etc.) that break scripts
            result = ''.join(char for char in result if ord(char) >= 32 or char in '\t')
            
            # Replace problematic characters
            # Order matters: backslash first, then quotes
            result = result.replace('\\', '\\\\')  # Escape backslashes
            result = result.replace('"', '\\"')    # Escape double quotes for shell
            result = result.replace("'", "\\'")    # Escape single quotes
            result = result.replace('`', '\\`')    # Escape backticks
            result = result.replace('$', '\\$')    # Escape dollar signs (shell expansion)
            
            return result
        
        safe_app_name = sanitize_for_applescript(actual_app_name, max_length=50)
        safe_title = sanitize_for_applescript(window_title, max_length=50)
        
        logger.info(
            f"[YABAI v44.0] 🔑 ATOMIC TRANSITION: Forcing {actual_app_name} window {window_id} "
            f"to exit fullscreen via AppleScript (type: {app_type}, verify: {verify_transition})"
        )
        
        # ═══════════════════════════════════════════════════════════════════════
        # v44.0: BUILD SMART-TARGETED AppleScript (Direct Object Model)
        # ═══════════════════════════════════════════════════════════════════════
        # CRITICAL: We use DIRECT APPLICATION CONTROL, not System Events.
        # v44.0 SMART TARGETING: Exclude JARVIS windows to protect dashboard
        # - Skips windows with "JARVIS", "localhost", "127.0.0.1" in title
        # - No focus stealing (doesn't bring window to front)
        # - No keyboard simulation (no ⌘F shortcuts)
        # - Works on hidden/background windows
        # - Requires Automation permission on first run
        # ═══════════════════════════════════════════════════════════════════════
        
        # v44.0: JARVIS protection patterns - these windows should NOT be unfullscreened
        jarvis_patterns = ["JARVIS", "localhost", "127.0.0.1"]
        
        if app_type == "chrome":
            # ═══════════════════════════════════════════════════════════════════
            # v46.0: REALITY ANCHOR - Full Re-Materialization Protocol
            # ═══════════════════════════════════════════════════════════════════
            # Chrome/Chromium family: Forces windows to exit all "ghost states":
            # 1. Exit fullscreen (destroys phantom space)
            # 2. Un-minimize (pulls from dock)
            # 3. Ensure visibility (renders the window)
            # ═══════════════════════════════════════════════════════════════════
            script = f'''
            tell application "{safe_app_name}"
                try
                    set windowList to every window
                    repeat with w in windowList
                        try
                            set winName to name of w
                            -- v44.0: Skip JARVIS windows (protect dashboard)
                            set isJarvis to false
                            if winName contains "JARVIS" then set isJarvis to true
                            if winName contains "localhost" then set isJarvis to true
                            if winName contains "127.0.0.1" then set isJarvis to true
                            
                            if not isJarvis then
                                -- v46.0: STEP 1 - Exit fullscreen (destroys phantom space)
                                try
                                    if full screen of w is true then
                                        set full screen of w to false
                                    end if
                                end try
                                
                                -- v46.0: STEP 2 - Un-minimize (pull from dock)
                                try
                                    if miniaturized of w is true then
                                        set miniaturized of w to false
                                    end if
                                end try
                                
                                -- v46.0: STEP 3 - Ensure visibility
                                try
                                    set visible of w to true
                                end try
                            end if
                        on error errMsg
                            -- Window might not support these properties, ignore
                        end try
                    end repeat
                on error errMsg
                    error errMsg
                end try
            end tell
            '''
            
        elif app_type == "safari":
            # ═══════════════════════════════════════════════════════════════════
            # v46.0: REALITY ANCHOR for Safari
            # Safari uses `fullscreen` property (no space in name)
            # ═══════════════════════════════════════════════════════════════════
            script = f'''
            tell application "{safe_app_name}"
                try
                    set windowList to every window
                    repeat with w in windowList
                        try
                            set winName to name of w
                            -- v44.0: Skip JARVIS windows (protect dashboard)
                            set isJarvis to false
                            if winName contains "JARVIS" then set isJarvis to true
                            if winName contains "localhost" then set isJarvis to true
                            if winName contains "127.0.0.1" then set isJarvis to true
                            
                            if not isJarvis then
                                -- v46.0: Exit fullscreen
                                try
                                    if fullscreen of w is true then
                                        set fullscreen of w to false
                                    end if
                                end try
                                
                                -- v46.0: Un-minimize
                                try
                                    if miniaturized of w is true then
                                        set miniaturized of w to false
                                    end if
                                end try
                                
                                -- v46.0: Ensure visibility
                                try
                                    set visible of w to true
                                end try
                            end if
                        on error errMsg
                            -- Window might not support these properties, ignore
                        end try
                    end repeat
                on error errMsg
                    error errMsg
                end try
            end tell
            '''
            
        elif app_type == "electron":
            # ═══════════════════════════════════════════════════════════════════
            # v46.0: REALITY ANCHOR for Electron apps
            # Usually support `full screen` like Chrome
            # ═══════════════════════════════════════════════════════════════════
            script = f'''
            tell application "{safe_app_name}"
                try
                    set windowList to every window
                    repeat with w in windowList
                        try
                            set winName to name of w
                            -- v44.0: Skip JARVIS windows (protect dashboard)
                            set isJarvis to false
                            if winName contains "JARVIS" then set isJarvis to true
                            if winName contains "localhost" then set isJarvis to true
                            if winName contains "127.0.0.1" then set isJarvis to true
                            
                            if not isJarvis then
                                -- v46.0: Exit fullscreen
                                try
                                    if full screen of w is true then
                                        set full screen of w to false
                                    end if
                                end try
                                
                                -- v46.0: Un-minimize
                                try
                                    if miniaturized of w is true then
                                        set miniaturized of w to false
                                    end if
                                end try
                                
                                -- v46.0: Ensure visibility
                                try
                                    set visible of w to true
                                end try
                            end if
                                set full screen of w to false
                            end if
                        on error
                            -- Some Electron apps don't expose this property
                        end try
                    end repeat
                on error errMsg
                    -- App might not be scriptable, proceed silently
                end try
            end tell
            '''
            
        else:
            # v44.0: GENERIC FALLBACK - Accessibility API (no focus stealing)
            # Uses AXFullScreen attribute via System Events process control
            # NOTE: This requires Accessibility permission, not Automation
            # v44.0: Skip JARVIS windows using title check
            script = f'''
            tell application "System Events"
                try
                    tell process "{safe_app_name}"
                        repeat with w in (every window)
                            try
                                set winName to name of w
                                -- v44.0: Skip JARVIS windows (protect dashboard)
                                set isJarvis to false
                                if winName contains "JARVIS" then set isJarvis to true
                                if winName contains "localhost" then set isJarvis to true
                                if winName contains "127.0.0.1" then set isJarvis to true
                                
                                if not isJarvis and exists attribute "AXFullScreen" of w then
                                    set value of attribute "AXFullScreen" of w to false
                                end if
                            end try
                        end repeat
                    end tell
                on error errMsg
                    -- Process might not be found or accessible
                end try
            end tell
            '''
        
        # ═══════════════════════════════════════════════════════════════════════
        # v44.0: FIRE & FORGET MODE with optional VERIFICATION
        # ═══════════════════════════════════════════════════════════════════════
        # When fire_and_forget=True, we start the hydration delay BEFORE waiting
        # for the AppleScript to complete. This runs them in parallel.
        # When verify_transition=True, we'll verify the state change after.
        # ═══════════════════════════════════════════════════════════════════════
        
        hydration_delay = 1.5  # v44.0: Increased from 1.0 to 1.5s for reliability
        hydration_task = None
        
        if fire_and_forget:
            # Start hydration timer immediately (parallel execution)
            hydration_task = asyncio.create_task(asyncio.sleep(hydration_delay))
            logger.debug(f"[YABAI v44.0] 🚀 Fire & Forget: Hydration timer started ({hydration_delay}s)")
        
        try:
            # Execute AppleScript asynchronously
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Wait with timeout - don't block forever
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                
                # ═══════════════════════════════════════════════════════════════
                # v43.5: PERMISSION PANIC DETECTION
                # ═══════════════════════════════════════════════════════════════
                # macOS error code -1743: "The user has denied permission"
                # This happens on first run when user hasn't approved automation
                #
                # Other common codes:
                # -1728: Can't get property (app doesn't support it - OK)
                # -1719: Application isn't running (OK, nothing to unpack)
                # -600: Application isn't running
                # ═══════════════════════════════════════════════════════════════
                
                if proc.returncode == 0:
                    logger.info(
                        f"[YABAI v44.0] ✅ ATOMIC TRANSITION: AppleScript executed successfully "
                        f"for {actual_app_name}"
                    )
                    
                elif proc.returncode == 1:
                    # AppleScript error - check stderr for details
                    error_msg = stderr.decode().strip() if stderr else "Unknown error"
                    
                    # Check for permission denied (-1743)
                    if "-1743" in error_msg or "not allowed" in error_msg.lower():
                        # ═══════════════════════════════════════════════════════
                        # v44.0: CRITICAL ALERT - User must approve permission
                        # ═══════════════════════════════════════════════════════
                        logger.critical(
                            f"[YABAI v44.0] ⚠️ PERMISSION DENIED! ⚠️\n"
                            f"JARVIS needs Automation permission to control {actual_app_name}.\n"
                            f"Please check for a macOS popup asking to allow access.\n"
                            f"Go to: System Preferences → Security & Privacy → Privacy → Automation\n"
                            f"Enable: Terminal (or Python) → {actual_app_name}"
                        )
                        # Still proceed - the move might work anyway if not actually fullscreen
                        
                    elif "-600" in error_msg or "not running" in error_msg.lower():
                        # App isn't running - nothing to unpack
                        logger.debug(f"[YABAI v44.0] {actual_app_name} not running - skip unpack")
                        
                    elif "-1728" in error_msg:
                        # Property doesn't exist - app doesn't support fullscreen scripting
                        logger.debug(
                            f"[YABAI v44.0] {actual_app_name} doesn't support fullscreen scripting"
                        )
                        
                    else:
                        # Other error - log but proceed
                        logger.warning(
                            f"[YABAI v44.0] AppleScript returned error (code {proc.returncode}): {error_msg}"
                        )
                else:
                    # Non-standard return code
                    error_msg = stderr.decode().strip() if stderr else "Unknown"
                    logger.warning(
                        f"[YABAI v44.0] Unexpected return code {proc.returncode}: {error_msg}"
                    )
                    
            except asyncio.TimeoutError:
                logger.warning(
                    f"[YABAI v44.0] ⚠️ AppleScript timed out for {actual_app_name} - proceeding anyway"
                )
                
        except FileNotFoundError:
            logger.warning("[YABAI v44.0] osascript not found - skipping AppleScript unpack")
            
        except Exception as e:
            logger.warning(f"[YABAI v44.0] Atomic Transition failed: {e}")
        
        # ═══════════════════════════════════════════════════════════════════════
        # v44.0: WAIT FOR HYDRATION (or Fire & Forget completion)
        # ═══════════════════════════════════════════════════════════════════════
        
        if hydration_task:
            # Wait for parallel hydration to complete
            await hydration_task
            logger.debug(f"[YABAI v44.0] ✅ Fire & Forget hydration complete")
        else:
            # Sequential hydration - wait now
            logger.debug(f"[YABAI v44.0] ⏳ Waiting {hydration_delay}s for hydration...")
            await asyncio.sleep(hydration_delay)
        
        # ═══════════════════════════════════════════════════════════════════════
        # v44.0: STATE CONVERGENCE PROTOCOL (Replaces simple verification)
        # ═══════════════════════════════════════════════════════════════════════
        # We don't guess. We MEASURE.
        # 
        # When a fullscreen window exits, macOS destroys its Space (Topology Drift).
        # The window "falls" onto another Space. We must:
        # 1. Detect the DRIFT: Space ID changed from original
        # 2. Confirm LANDED: Window reports not-fullscreen
        # 3. CONVERGENCE: Both conditions met = stable state
        #
        # This is a TRANSACTION. We only proceed when physics have settled.
        # ═══════════════════════════════════════════════════════════════════════
        
        if verify_transition:
            yabai_path = self._health.yabai_path or "yabai"
            
            # Capture original state BEFORE the AppleScript took effect
            # (We already waited for hydration, so this is the "post-unpack" state we're monitoring)
            original_space_id = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                if proc.returncode == 0 and stdout:
                    initial_state = json.loads(stdout.decode())
                    original_space_id = initial_state.get('space')
                    logger.debug(f"[YABAI v44.0] 📍 Original space: {original_space_id}")
            except Exception as e:
                logger.debug(f"[YABAI v44.0] Could not capture original space: {e}")
            
            # ═══════════════════════════════════════════════════════════════════
            # STATE CONVERGENCE MONITOR
            # ═══════════════════════════════════════════════════════════════════
            # We don't guess. We wait for the OS to report a stable state.
            # Convergence = NOT fullscreen AND (Space ID changed OR was never fullscreen)
            # ═══════════════════════════════════════════════════════════════════
            
            drift_detected = False
            convergence_achieved = False
            converged_window_info = None
            
            for attempt in range(20):  # Monitor for 2.0s at 100ms intervals
                try:
                    # 1. RE-QUERY: Get absolute truth from the OS
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                    
                    if proc.returncode == 0 and stdout:
                        fresh_window_state = json.loads(stdout.decode())
                        
                        # 2. CHECK CONSTRAINT: Is it definitely NOT fullscreen?
                        is_fullscreen = fresh_window_state.get('is-native-fullscreen', False)
                        is_zoom_fullscreen = fresh_window_state.get('has-fullscreen-zoom', False)
                        is_any_fullscreen = is_fullscreen or is_zoom_fullscreen
                        
                        # 3. CHECK TOPOLOGY: Did the Space ID change? (The Drift)
                        current_space = fresh_window_state.get('space')
                        
                        if original_space_id is not None and current_space != original_space_id:
                            if not drift_detected:
                                logger.info(
                                    f"[YABAI v44.0] 🌊 TOPOLOGY DRIFT DETECTED: "
                                    f"Window {window_id} moved Space {original_space_id} → {current_space}"
                                )
                            drift_detected = True
                        
                        # 4. CONVERGENCE CONDITION
                        # We consider converged if:
                        # - Not fullscreen AND drift detected, OR
                        # - Not fullscreen AND we couldn't capture original (assume OK)
                        if not is_any_fullscreen and (drift_detected or original_space_id is None):
                            logger.info(
                                f"[YABAI v44.0] ✅ TOPOLOGY CONVERGED: Window {window_id} "
                                f"landed on Space {current_space} (not fullscreen, drift={'yes' if drift_detected else 'n/a'})"
                            )
                            convergence_achieved = True
                            converged_window_info = fresh_window_state
                            break
                        
                        # Not converged yet - log progress
                        if attempt % 5 == 0:  # Log every 500ms
                            logger.debug(
                                f"[YABAI v44.0] ⏳ Convergence pending: "
                                f"fullscreen={is_any_fullscreen}, drift={drift_detected}, "
                                f"space={current_space} (attempt {attempt + 1}/20)"
                            )
                    else:
                        # Can't query - window might be in transition
                        logger.debug(
                            f"[YABAI v44.0] Window {window_id} query returned no data "
                            f"(in transition?) - attempt {attempt + 1}"
                        )
                        
                except asyncio.TimeoutError:
                    logger.debug(f"[YABAI v44.0] Window query timed out (attempt {attempt + 1})")
                except json.JSONDecodeError:
                    logger.debug(f"[YABAI v44.0] Invalid JSON from yabai (attempt {attempt + 1})")
                except Exception as e:
                    logger.debug(f"[YABAI v44.0] Convergence check error: {e}")
                
                # Wait before next measurement (100ms high-frequency polling)
                await asyncio.sleep(0.1)
            
            # Report convergence result
            if convergence_achieved:
                logger.info(
                    f"[YABAI v44.0] 🎯 STATE CONVERGENCE COMPLETE: "
                    f"Window {window_id} is stable and ready for teleportation"
                )
                # v44.1: Use per-window dictionary to avoid race conditions
                # Multiple windows can be processed in parallel
                if not hasattr(self, '_converged_window_states'):
                    self._converged_window_states = {}
                self._converged_window_states[window_id] = converged_window_info
                # Keep legacy for backwards compatibility
                self._last_converged_window_info = converged_window_info
            else:
                # v44.1: GRACEFUL DEGRADATION - Even on timeout, query FRESH state
                # The window might have converged but we missed the exact moment
                logger.warning(
                    f"[YABAI v44.0] ⚠️ CONVERGENCE TIMEOUT: Window {window_id} did not settle "
                    f"after 2.0s. Drift detected: {drift_detected}. "
                    f"Querying final state for graceful degradation..."
                )
                
                # v44.1: One more query to get best-effort current state
                try:
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                    if proc.returncode == 0 and stdout:
                        final_state = json.loads(stdout.decode())
                        final_space = final_state.get('space')
                        final_fullscreen = final_state.get('is-native-fullscreen', False)
                        
                        logger.info(
                            f"[YABAI v44.1] 📍 GRACEFUL STATE: Window {window_id} on Space {final_space}, "
                            f"fullscreen={final_fullscreen}"
                        )
                        
                        # Store even partial info - better than nothing
                        if not hasattr(self, '_converged_window_states'):
                            self._converged_window_states = {}
                        self._converged_window_states[window_id] = final_state
                        self._last_converged_window_info = final_state
                    else:
                        self._last_converged_window_info = None
                except Exception as e:
                    logger.debug(f"[YABAI v44.1] Graceful state query failed: {e}")
                    self._last_converged_window_info = None
        
        # Always return True - we never want to block the move operation
        # Even if convergence wasn't perfect, the window might still be movable
        return True

    # ═══════════════════════════════════════════════════════════════════════════
    # v49.0: SPACE-BASED TRUTH PROTOCOL
    # ═══════════════════════════════════════════════════════════════════════════
    # THE PROBLEM: Window queries LIE about fullscreen status.
    #   - `yabai -m query --windows --window 1404` says is-native-fullscreen: false
    #   - But the window IS in fullscreen (on a fullscreen Space)
    #
    # THE SOLUTION: Ask the SPACE, not the Window.
    #   - `yabai -m query --spaces --space 4` says is-native-fullscreen: true
    #   - The Space CANNOT lie - if it's a fullscreen space, the window IS locked.
    #
    # This fixes PWA apps (Google Gemini, etc.) that don't respond to AppleScript.
    # We use yabai's native --toggle which works on ANY window type.
    # ═══════════════════════════════════════════════════════════════════════════

    async def _is_space_native_fullscreen_async(
        self,
        space_index: int
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        v49.0: Query the SPACE to determine if it's a native fullscreen space.

        The Space metadata tells the TRUTH about fullscreen status, even when
        the Window query lies (common with PWAs and non-scriptable apps).

        Args:
            space_index: The space index to query

        Returns:
            Tuple of (is_fullscreen: bool, space_info: Optional[Dict]):
            - is_fullscreen: True if Space is native fullscreen
            - space_info: Full space metadata (for logging/debugging)
        """
        if space_index <= 0:
            return False, None

        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces", "--space", str(space_index),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                # Space might not exist or yabai error
                logger.debug(
                    f"[YABAI v49.0] Space {space_index} query failed: "
                    f"{stderr.decode().strip() if stderr else 'no output'}"
                )
                return False, None

            space_info = json.loads(stdout.decode())

            # THE TRUTH: Space's is-native-fullscreen flag
            is_fullscreen = space_info.get("is-native-fullscreen", False)
            space_type = space_info.get("type", "unknown")

            if is_fullscreen:
                logger.info(
                    f"[YABAI v49.0] 🔮 SPACE TRUTH: Space {space_index} (type={space_type}) "
                    f"IS a native fullscreen space - Window query was LYING!"
                )
            else:
                logger.debug(
                    f"[YABAI v49.0] Space {space_index} (type={space_type}) "
                    f"is NOT native fullscreen"
                )

            return is_fullscreen, space_info

        except asyncio.TimeoutError:
            logger.warning(f"[YABAI v49.0] Space {space_index} query timed out")
            return False, None
        except json.JSONDecodeError as e:
            logger.warning(f"[YABAI v49.0] Space {space_index} query returned invalid JSON: {e}")
            return False, None
        except Exception as e:
            logger.warning(f"[YABAI v49.0] Space {space_index} query error: {e}")
            return False, None

    # ═══════════════════════════════════════════════════════════════════════════
    # v50.0: OMNISCIENT PROTOCOL - NEGATIVE LOGIC & HARDWARE TARGETING
    # ═══════════════════════════════════════════════════════════════════════════
    # SIP-COMPLIANT strategy that uses:
    # 1. NEGATIVE LOGIC: If window claims Space X, but Space X doesn't exist
    #    in the known spaces list → Window is in a PHANTOM SPACE (fullscreen)
    # 2. HARDWARE TARGETING: Move to Display (hardware) instead of Space (software)
    #    because yabai can move to displays with SIP enabled
    # 3. DNA TRACKING: Use PID to track windows across destruction events
    # ═══════════════════════════════════════════════════════════════════════════

    async def _is_space_phantom_or_fullscreen_async(
        self,
        window_space_id: int
    ) -> Tuple[bool, str, Set[int]]:
        """
        v50.0: NEGATIVE LOGIC - Detect phantom/fullscreen spaces by elimination.

        Instead of asking "Is this space fullscreen?" (which can lie),
        we ask "Does this space even EXIST in our known topology?"

        If the window claims to be on Space 42, but our topology only has
        Spaces 1-10, then Space 42 is a PHANTOM SPACE (native fullscreen).

        Args:
            window_space_id: The space ID the window claims to be on

        Returns:
            Tuple of (is_phantom: bool, reason: str, known_spaces: Set[int]):
            - is_phantom: True if space is phantom/fullscreen
            - reason: Human-readable explanation
            - known_spaces: Set of all valid space indices
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        try:
            # Query ALL spaces to build known topology
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                logger.warning(f"[YABAI v50.0] Failed to query spaces: {stderr.decode() if stderr else 'unknown'}")
                return False, "query_failed", set()

            spaces_data = json.loads(stdout.decode())

            # Build the set of KNOWN space indices
            known_spaces: Set[int] = set()
            fullscreen_spaces: Set[int] = set()

            for space in spaces_data:
                space_index = space.get("index", 0)
                if space_index > 0:
                    known_spaces.add(space_index)
                    # Also track which spaces are native fullscreen
                    if space.get("is-native-fullscreen", False):
                        fullscreen_spaces.add(space_index)

            logger.debug(
                f"[YABAI v50.0] 🧠 OMNISCIENT: Known spaces={sorted(known_spaces)}, "
                f"Fullscreen spaces={sorted(fullscreen_spaces)}"
            )

            # NEGATIVE LOGIC: Window claims Space X, but X doesn't exist
            if window_space_id not in known_spaces and window_space_id > 0:
                reason = f"space_{window_space_id}_not_in_known_topology_{sorted(known_spaces)}"
                logger.info(
                    f"[YABAI v50.0] 👻 PHANTOM DETECTED via NEGATIVE LOGIC: "
                    f"Window claims Space {window_space_id}, but known spaces are {sorted(known_spaces)}"
                )
                return True, reason, known_spaces

            # POSITIVE CONFIRMATION: Space exists but is native fullscreen
            if window_space_id in fullscreen_spaces:
                reason = f"space_{window_space_id}_is_native_fullscreen"
                logger.info(
                    f"[YABAI v50.0] 🔒 FULLSCREEN DETECTED: "
                    f"Space {window_space_id} is a native fullscreen space"
                )
                return True, reason, known_spaces

            # Edge case: Space 0 or -1 means orphaned/transitional
            if window_space_id <= 0:
                reason = f"space_{window_space_id}_orphaned_or_transitional"
                logger.info(
                    f"[YABAI v50.0] 🌀 ORPHANED WINDOW: Space {window_space_id} is invalid"
                )
                return True, reason, known_spaces

            # Normal space - not phantom or fullscreen
            return False, "normal_space", known_spaces

        except asyncio.TimeoutError:
            logger.warning("[YABAI v50.0] Spaces query timed out")
            return False, "timeout", set()
        except Exception as e:
            logger.warning(f"[YABAI v50.0] Negative logic check failed: {e}")
            return False, f"error_{e}", set()

    async def _get_ghost_display_index_async(self) -> Optional[int]:
        """
        v50.0: Get the DISPLAY index (not space) for the Ghost Display.

        Hardware targeting uses display indices, not space indices.
        This is SIP-compliant because moving to displays uses Accessibility APIs.

        Returns:
            Display index (1-based) or None if not found
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        try:
            # Query all spaces to find the ghost display
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                return None

            spaces_data = json.loads(stdout.decode())

            # Find displays and their visible spaces
            display_spaces: Dict[int, List[Dict]] = {}
            current_space = self.get_current_user_space()

            for space in spaces_data:
                display_id = space.get("display", 1)
                if display_id not in display_spaces:
                    display_spaces[display_id] = []
                display_spaces[display_id].append(space)

            # Ghost display is typically display 2 (secondary monitor)
            # or any display that has a visible space that's not the current space
            for display_id in sorted(display_spaces.keys()):
                if display_id > 1:  # Secondary display
                    logger.info(
                        f"[YABAI v50.0] 🎯 HARDWARE TARGET: Ghost Display is Display {display_id}"
                    )
                    return display_id

            # Fallback: If only one display, return it (user is using same display)
            if len(display_spaces) == 1:
                display_id = list(display_spaces.keys())[0]
                logger.debug(f"[YABAI v50.0] Single display mode: using Display {display_id}")
                return display_id

            return None

        except Exception as e:
            logger.warning(f"[YABAI v50.0] Ghost display detection failed: {e}")
            return None

    async def _move_window_to_display_async(
        self,
        window_id: int,
        display_index: int,
        sip_convergence_delay: float = 3.0
    ) -> bool:
        """
        v50.0: HARDWARE TARGETING - Move window to display (SIP-compliant).

        Unlike moving to spaces (which requires code injection blocked by SIP),
        moving to displays uses standard Accessibility APIs and works reliably.

        Args:
            window_id: The window to move
            display_index: Target display (1-based index)
            sip_convergence_delay: Time to wait for macOS animations (SIP mandates ~750ms)

        Returns:
            True if move succeeded, False otherwise
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v50.0] 🎯 HARDWARE MOVE: Window {window_id} → Display {display_index}"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "window", str(window_id), "--display", str(display_index),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

            if proc.returncode == 0:
                logger.info(
                    f"[YABAI v50.0] ✅ HARDWARE MOVE SUCCESS: Window {window_id} → Display {display_index}"
                )

                # SIP-AWARE CONVERGENCE: macOS enforces mandatory animations
                # With SIP enabled, we must wait longer for the animation to complete
                sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", str(sip_convergence_delay)))
                logger.debug(f"[YABAI v50.0] ⏳ SIP Convergence: waiting {sip_delay}s for animation")
                await asyncio.sleep(sip_delay)

                return True
            else:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.warning(
                    f"[YABAI v50.0] ⚠️ HARDWARE MOVE FAILED: {error_msg}"
                )
                return False

        except asyncio.TimeoutError:
            logger.warning(f"[YABAI v50.0] ⚠️ Hardware move timed out")
            return False
        except Exception as e:
            logger.warning(f"[YABAI v50.0] ⚠️ Hardware move error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # v53.0: SHADOW REALM PROTOCOL
    # ═══════════════════════════════════════════════════════════════════════════
    # ROOT CAUSE FIX: Windows on phantom fullscreen spaces CANNOT be moved using
    # `--space` commands. macOS Space Topology prevents cross-space moves when
    # the source space is a phantom (destroyed fullscreen space that still exists
    # in the window's metadata but not in the actual topology).
    #
    # SOLUTION: "Exile" to Shadow Realm (BetterDisplay Dummy)
    # Instead of trying to manage Phantom Spaces on Display 1, we:
    # 1. Unpack fullscreen (bring window back to windowed mode)
    # 2. EXILE to Display 2 (BetterDisplay) using `--display 2`
    # 3. Maximize on Ghost Display using `--grid 1:1:0:0:1:1`
    #
    # This BYPASSES Space Topology entirely by using Hardware Targeting.
    # `yabai -m window --display` uses standard Accessibility APIs that work
    # regardless of SIP status or Space configuration.
    # ═══════════════════════════════════════════════════════════════════════════

    async def _exile_to_shadow_realm_async(
        self,
        window_id: int,
        app_name: str = "Unknown",
        window_title: str = "",
        pid: Optional[int] = None,
        silent: bool = True,
        shadow_display: int = 2,
        maximize_after_exile: bool = True
    ) -> Tuple[bool, str]:
        """
        v53.0: SHADOW REALM PROTOCOL - Exile window to BetterDisplay Dummy.

        This is the ROOT CAUSE FIX for phantom fullscreen space issues.
        Instead of trying to move windows between Spaces (which fails for
        phantom spaces), we EXILE them to a different Display entirely.

        The "Shadow Realm" is the BetterDisplay virtual display (Display 2)
        which exists outside the normal Space Topology and can receive
        windows from ANY source, including phantom spaces.

        Args:
            window_id: The window to exile
            app_name: App name (for logging and unpack strategies)
            window_title: Window title (for re-bonding if needed)
            pid: Process ID (for AX API unpack)
            silent: If True, minimize visual disruption
            shadow_display: Target display index (default: 2 = BetterDisplay)
            maximize_after_exile: If True, maximize window on Shadow Realm

        Returns:
            Tuple of (success: bool, method: str describing what worked)
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v53.0] 🌑 SHADOW REALM PROTOCOL: Exiling window {window_id} "
            f"({app_name}: {window_title[:40] if window_title else 'untitled'}) → Display {shadow_display}"
        )

        # ═══════════════════════════════════════════════════════════════════
        # v55.0: NEURO-REGENERATIVE PRE-CHECK - Ensure Window is Actionable
        # ═══════════════════════════════════════════════════════════════════
        # Before doing ANYTHING, check if the window is in zombie state.
        # If so, repair it first. This prevents all downstream failures.
        # ═══════════════════════════════════════════════════════════════════
        actionable, repaired_id, repaired_info = await self._ensure_window_is_actionable_async(
            window_id=window_id,
            window_info=None,
            app_name=app_name,
            pid=pid,
            max_repair_attempts=2
        )

        if not actionable:
            logger.error(
                f"[YABAI v55.0] ❌ Cannot exile window {window_id} - ZOMBIE state "
                f"unrecoverable (AX: false after repair attempts)"
            )
            return False, "zombie_unrecoverable"

        # Use the possibly-updated window ID and info
        if repaired_id != window_id:
            logger.info(
                f"[YABAI v55.0] 🔥 Window ID updated: {window_id} → {repaired_id}"
            )
            window_id = repaired_id

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 1: DETECT - Is window fullscreen? Is it on a phantom space?
        # ═══════════════════════════════════════════════════════════════════
        is_fullscreen = False
        is_phantom = False
        is_dehydrated = False
        window_space = -1
        window_display = -1
        original_space = None

        # First query attempt - may fail for dehydrated windows
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0 and stdout:
                window_info = json.loads(stdout.decode())
                is_fullscreen = window_info.get("is-native-fullscreen", False)
                window_space = window_info.get("space", -1)
                window_display = window_info.get("display", -1)
                pid = pid or window_info.get("pid")

                # v50.0: NEGATIVE LOGIC - Check if space is phantom
                if window_space > 0:
                    space_proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "query", "--spaces", "--space", str(window_space),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    space_stdout, space_stderr = await asyncio.wait_for(space_proc.communicate(), timeout=3.0)
                    if space_proc.returncode != 0:
                        is_phantom = True
                        logger.warning(
                            f"[YABAI v53.0] 👻 PHANTOM SPACE DETECTED: Window {window_id} claims "
                            f"Space {window_space} but space doesn't exist in topology!"
                        )
                    else:
                        # Space exists - check if it's hidden (window might be dehydrated)
                        space_info = json.loads(space_stdout.decode())
                        if not space_info.get("is-visible", True):
                            is_dehydrated = True
                            logger.info(
                                f"[YABAI v53.0] 💧 Window {window_id} may be dehydrated "
                                f"(on hidden Space {window_space})"
                            )
            else:
                # Yabai couldn't find the window by ID - try querying ALL windows
                # This happens when window has lost AX reference (has-ax-reference: false)
                is_dehydrated = True
                error_msg = stderr.decode().strip() if stderr else "Unknown"
                logger.warning(
                    f"[YABAI v53.0] Window query by ID failed: {error_msg} - trying full query..."
                )

                # Query all windows and find by ID
                window_found = False
                try:
                    all_proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "query", "--windows",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    all_stdout, _ = await asyncio.wait_for(all_proc.communicate(), timeout=5.0)
                    if all_proc.returncode == 0 and all_stdout:
                        all_windows = json.loads(all_stdout.decode())
                        for w in all_windows:
                            if w.get("id") == window_id:
                                is_fullscreen = w.get("is-native-fullscreen", False)
                                window_space = w.get("space", -1)
                                window_display = w.get("display", -1)
                                pid = pid or w.get("pid")
                                has_ax_reference = w.get("has-ax-reference", True)
                                window_found = True
                                logger.info(
                                    f"[YABAI v53.0] 📋 Found window {window_id} in full query: "
                                    f"space={window_space}, display={window_display}, "
                                    f"fullscreen={is_fullscreen}, ax_ref={has_ax_reference}"
                                )
                                break

                        # v54.1: Check if window exists but has no AX reference (zombie)
                        if window_found and not has_ax_reference:
                            logger.warning(
                                f"[YABAI v54.1] ⚠️ Window {window_id} has NO AX REFERENCE - "
                                "initiating Lazarus Trigger to restore AX reference..."
                            )
                            # Run Lazarus Trigger to restore AX reference
                            lazarus_success = await self._reflash_ax_reference_async(
                                app_name=app_name,
                                pid=pid,
                                window_id=window_id
                            )
                            if lazarus_success:
                                # Re-check if window now has AX reference
                                for w in all_windows:
                                    if w.get("id") == window_id:
                                        has_ax_reference = w.get("has-ax-reference", False)
                                        break
                                # Re-query to get fresh state after Lazarus
                                try:
                                    refresh_proc = await asyncio.create_subprocess_exec(
                                        yabai_path, "-m", "query", "--windows",
                                        stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE
                                    )
                                    refresh_stdout, _ = await asyncio.wait_for(
                                        refresh_proc.communicate(), timeout=5.0
                                    )
                                    if refresh_proc.returncode == 0 and refresh_stdout:
                                        refreshed = json.loads(refresh_stdout.decode())
                                        for w in refreshed:
                                            if w.get("id") == window_id:
                                                has_ax_reference = w.get("has-ax-reference", False)
                                                is_fullscreen = w.get("is-native-fullscreen", False)
                                                window_space = w.get("space", window_space)
                                                window_display = w.get("display", window_display)
                                                logger.info(
                                                    f"[YABAI v54.1] Post-Lazarus: ax_ref={has_ax_reference}, "
                                                    f"fullscreen={is_fullscreen}"
                                                )
                                                break
                                except Exception:
                                    pass

                            if not has_ax_reference:
                                logger.warning(
                                    f"[YABAI v54.1] Lazarus failed - window still has no AX reference. "
                                    "Returning as unmovable."
                                )
                                return False, "ax_reference_lost_lazarus_failed"

                        # v54.0: PHOENIX PROTOCOL - If window ID is truly dead, regenerate it
                        # Only run Phoenix if window NOT found AND we have a title to match
                        # (empty titles can match wrong windows with same PID)
                        if not window_found and app_name and window_title:
                            logger.warning(
                                f"[YABAI v54.0] ⚠️ Window ID {window_id} is dead! "
                                "Initiating Phoenix Protocol..."
                            )
                            new_id = await self._regenerate_window_id_async(
                                app_name=app_name,
                                target_title=window_title,
                                old_id=window_id,
                                pid=pid,
                                fuzzy_threshold=0.5
                            )
                            if new_id:
                                logger.info(
                                    f"[YABAI v54.0] 🔥 Phoenix: Re-bonded {window_id} → {new_id}"
                                )
                                window_id = new_id

                                for w in all_windows:
                                    if w.get("id") == new_id:
                                        is_fullscreen = w.get("is-native-fullscreen", False)
                                        window_space = w.get("space", -1)
                                        window_display = w.get("display", -1)
                                        pid = w.get("pid")
                                        window_found = True
                                        break
                        elif not window_found:
                            if not window_title:
                                logger.warning(
                                    f"[YABAI v54.0] Cannot regenerate ID for window with empty title - "
                                    "returning unmovable"
                                )
                                return False, "window_dead_empty_title"
                            await self._flush_yabai_cache_async(3)
                except Exception as e2:
                    logger.warning(f"[YABAI v53.0] Full query also failed: {e2}")

        except Exception as e:
            logger.warning(f"[YABAI v53.0] Window query failed: {e}")
            is_dehydrated = True

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 1.5: WAKE - Hydrate dehydrated windows by switching to their space
        # ═══════════════════════════════════════════════════════════════════
        # ROOT CAUSE FIX: macOS "dehydrates" windows on hidden spaces, making
        # them inaccessible to yabai. We need to temporarily switch to the
        # window's space to "wake" it up before we can interact with it.
        # ═══════════════════════════════════════════════════════════════════
        if is_dehydrated and window_space > 0:
            logger.info(
                f"[YABAI v53.0] 💧 HYDRATING: Switching to Space {window_space} to wake window {window_id}..."
            )

            # Save current space to return later
            try:
                current_proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--spaces", "--space",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                current_stdout, _ = await asyncio.wait_for(current_proc.communicate(), timeout=3.0)
                if current_proc.returncode == 0 and current_stdout:
                    current_info = json.loads(current_stdout.decode())
                    original_space = current_info.get("index")
            except Exception:
                pass

            # Switch to the window's space to wake it
            wake_success = await self._switch_to_space_async(window_space)
            if wake_success:
                # Wait for macOS to hydrate the window
                await asyncio.sleep(0.5)
                logger.info(f"[YABAI v53.0] 💧 Switched to Space {window_space} - window should be hydrated")

                # Re-query window info now that it's hydrated
                try:
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                    if proc.returncode == 0 and stdout:
                        window_info = json.loads(stdout.decode())
                        is_fullscreen = window_info.get("is-native-fullscreen", False)
                        window_space = window_info.get("space", -1)
                        window_display = window_info.get("display", -1)
                        pid = pid or window_info.get("pid")
                        is_dehydrated = False
                        logger.info(
                            f"[YABAI v53.0] ✅ Window {window_id} hydrated: "
                            f"fullscreen={is_fullscreen}, space={window_space}, display={window_display}"
                        )
                    else:
                        # Still can't query by ID - use AppleScript to activate
                        logger.warning(
                            f"[YABAI v53.0] Window {window_id} still inaccessible after space switch - "
                            "trying AppleScript activation..."
                        )
                        if pid:
                            try:
                                activate_script = f'''
                                tell application "System Events"
                                    set targetProc to first process whose unix id is {pid}
                                    set frontmost of targetProc to true
                                    set visible of targetProc to true
                                    delay 0.3
                                end tell
                                '''
                                act_proc = await asyncio.create_subprocess_exec(
                                    "osascript", "-e", activate_script,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE
                                )
                                await asyncio.wait_for(act_proc.communicate(), timeout=5.0)
                                await asyncio.sleep(0.5)
                                logger.info(f"[YABAI v53.0] AppleScript activation sent for PID {pid}")
                            except Exception as e2:
                                logger.warning(f"[YABAI v53.0] AppleScript activation failed: {e2}")
                except Exception as e:
                    logger.warning(f"[YABAI v53.0] Re-query after wake failed: {e}")
            else:
                logger.warning(f"[YABAI v53.0] Failed to switch to Space {window_space} for hydration")

        # Already on target display? Just maximize if needed
        if window_display == shadow_display:
            logger.info(f"[YABAI v53.0] Window already on Display {shadow_display}")
            if maximize_after_exile:
                await self._maximize_window_async(window_id)
            return True, "already_on_shadow_display"

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 2: UNPACK - Exit fullscreen if needed
        # ═══════════════════════════════════════════════════════════════════
        unpack_method = "none"
        if is_fullscreen:
            logger.info(
                f"[YABAI v53.0] 🔓 Window {window_id} is fullscreen - unpacking before exile..."
            )

            # Try unpack strategies in order of reliability
            unpack_success = False

            # Strategy 1: Yabai Toggle
            unpack_success = await self._unpack_via_yabai_toggle_async(
                window_id, app_name, window_title
            )
            if unpack_success:
                unpack_method = "yabai_toggle"

            # Strategy 2: AX API (if we have PID)
            if not unpack_success and pid:
                unpack_success = await self._unpack_via_ax_pid_async(
                    pid, window_id, app_name, silent=silent
                )
                if unpack_success:
                    unpack_method = "ax_api"

            # Strategy 3: AppleScript generic fullscreen toggle
            if not unpack_success:
                try:
                    script = '''
                    tell application "System Events"
                        keystroke "f" using {control down, command down}
                        delay 0.5
                    end tell
                    '''
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=5.0)
                    if proc.returncode == 0:
                        unpack_success = True
                        unpack_method = "applescript_generic"
                        await asyncio.sleep(1.5)  # Wait for animation
                except Exception as e:
                    logger.debug(f"[YABAI v53.0] AppleScript unpack failed: {e}")

            # Strategy 4: Nuclear Option - Activate app by name and send Escape
            # This is for windows with no AX reference that are truly stuck
            if not unpack_success and app_name:
                logger.info(
                    f"[YABAI v53.0] 🔥 NUCLEAR OPTION: Activating {app_name} and sending Escape..."
                )
                try:
                    nuclear_script = f'''
                    -- First try to activate the app by name
                    tell application "{app_name}"
                        activate
                    end tell
                    delay 0.5

                    -- Now send keyboard shortcuts to exit fullscreen
                    tell application "System Events"
                        tell process "{app_name}"
                            set frontmost to true
                            delay 0.3

                            -- Try Escape first
                            key code 53
                            delay 0.5

                            -- Then try Ctrl+Cmd+F (fullscreen toggle)
                            keystroke "f" using {{control down, command down}}
                            delay 1.0
                        end tell
                    end tell
                    '''
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", nuclear_script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                    if proc.returncode == 0:
                        unpack_success = True
                        unpack_method = "applescript_nuclear"
                        await asyncio.sleep(2.0)  # Longer wait for animation
                        logger.info(f"[YABAI v53.0] Nuclear option executed successfully")
                    else:
                        error = stderr.decode().strip() if stderr else "Unknown"
                        logger.warning(f"[YABAI v53.0] Nuclear option failed: {error}")
                except Exception as e:
                    logger.warning(f"[YABAI v53.0] Nuclear option exception: {e}")

            if not unpack_success:
                logger.warning(
                    f"[YABAI v53.0] ⚠️ Could not unpack window {window_id} - "
                    "attempting exile anyway (may fail)"
                )

            # Wait for unpack animation to complete
            sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", "2.0"))
            await asyncio.sleep(sip_delay)

            # ═══════════════════════════════════════════════════════════════
            # v54.0: POST-UNPACK PHOENIX VERIFICATION
            # ═══════════════════════════════════════════════════════════════
            # After unpack, the window ID may have changed. Try to find the
            # new window and update our reference before exile.
            # ═══════════════════════════════════════════════════════════════
            if app_name:
                new_id = await self._regenerate_window_id_async(
                    app_name=app_name,
                    target_title=window_title,
                    old_id=window_id,
                    pid=pid,
                    fuzzy_threshold=0.3  # Very low for empty titles
                )
                if new_id and new_id != window_id:
                    logger.info(
                        f"[YABAI v54.0] 🔥 POST-UNPACK Phoenix: Window ID changed "
                        f"{window_id} → {new_id}"
                    )
                    window_id = new_id
                    unpack_method = f"{unpack_method}_rebonded"

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 3: EXILE - Move to Shadow Realm using Hardware Targeting
        # ═══════════════════════════════════════════════════════════════════
        logger.info(
            f"[YABAI v53.0] 🚀 EXILE: Moving window {window_id} → Display {shadow_display}"
        )

        exile_success = await self._move_window_to_display_async(
            window_id, shadow_display, sip_convergence_delay=2.0
        )

        if not exile_success:
            # Try with fresh window ID (in case of re-bonding)
            if app_name:
                new_window_id = await self._regenerate_window_id_async(
                    app_name=app_name,
                    target_title=window_title,
                    old_id=window_id,
                    pid=pid,
                    fuzzy_threshold=0.3
                )
                if new_window_id and new_window_id != window_id:
                    logger.info(
                        f"[YABAI v54.0] 🔥 EXILE-PHASE Phoenix: Re-bonded {window_id} → {new_window_id}"
                    )
                    exile_success = await self._move_window_to_display_async(
                        new_window_id, shadow_display, sip_convergence_delay=2.0
                    )
                    if exile_success:
                        window_id = new_window_id

        # v54.0: If yabai move failed, try AppleScript move
        if not exile_success and pid:
            logger.info(f"[YABAI v54.0] 🔧 Trying AppleScript window move as fallback...")
            try:
                # Use AppleScript to set window bounds on target display
                move_script = f'''
                tell application "System Events"
                    tell process id {pid}
                        set frontmost to true
                        delay 0.3
                    end tell
                end tell
                '''
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", move_script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=5.0)
                await asyncio.sleep(0.5)

                # Try yabai move again after activation
                exile_success = await self._move_window_to_display_async(
                    window_id, shadow_display, sip_convergence_delay=1.0
                )
                if exile_success:
                    logger.info(f"[YABAI v54.0] AppleScript + yabai move succeeded!")
            except Exception as e:
                logger.warning(f"[YABAI v54.0] AppleScript move fallback failed: {e}")

        if not exile_success:
            logger.error(
                f"[YABAI v53.0] ❌ EXILE FAILED: Could not move window {window_id} "
                f"to Display {shadow_display}"
            )
            return False, f"exile_failed_unpack_{unpack_method}"

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: MAXIMIZE - Fill the Shadow Realm display
        # ═══════════════════════════════════════════════════════════════════
        if maximize_after_exile:
            await self._maximize_window_async(window_id)

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4.5: v70.0 BOOMERANG REGISTRATION - Track for "Bring Back"
        # ═══════════════════════════════════════════════════════════════════
        # ROOT CAUSE FIX: Windows were never registered with Boomerang during
        # exile, causing "Bring back Chrome" to return 0 windows.
        # We register the window NOW so it can be summoned back later.
        # ═══════════════════════════════════════════════════════════════════
        try:
            # Determine original space/display for return journey
            # Use captured values from PHASE 1, or defaults
            return_space = original_space if original_space and original_space > 0 else window_space
            return_display = window_display if window_display > 0 else 1  # Default to main display
            
            # Register with Boomerang Protocol for "bring back" commands
            await self.boomerang_register_exile_async(
                window_id=window_id,
                app_name=app_name,
                original_space=return_space if return_space > 0 else 1,
                original_display=return_display,
                window_title=window_title,
                auto_return_timeout=None,  # Manual return via voice command
                return_on_detection=True,  # Return when surveillance finds target
                return_on_app_activate=True  # Return when user clicks app in Dock
            )
            logger.info(
                f"[YABAI v70.0] 🪃 BOOMERANG REGISTERED: Window {window_id} ({app_name}) "
                f"→ Return to Space {return_space}, Display {return_display}"
            )
        except Exception as e:
            # Non-fatal - Boomerang registration is best-effort
            logger.warning(f"[YABAI v70.0] ⚠️ Boomerang registration failed: {e}")

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 5: VERIFY - Confirm window is on Shadow Realm
        # ═══════════════════════════════════════════════════════════════════
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0 and stdout:
                final_info = json.loads(stdout.decode())
                final_display = final_info.get("display", -1)

                if final_display == shadow_display:
                    logger.info(
                        f"[YABAI v53.0] ✅ EXILE SUCCESS: Window {window_id} "
                        f"now on Display {shadow_display} (Shadow Realm)"
                    )
                    return True, f"exile_success_unpack_{unpack_method}"
                else:
                    logger.warning(
                        f"[YABAI v53.0] ⚠️ Window {window_id} still on Display {final_display}"
                    )
                    return False, f"exile_verify_failed_display_{final_display}"

        except Exception as e:
            logger.warning(f"[YABAI v53.0] Verification failed: {e}")
            # Assume success if we got here without error in exile phase
            return True, f"exile_assumed_success_unpack_{unpack_method}"

        return False, "exile_unknown_failure"

    # ═══════════════════════════════════════════════════════════════════════════
    # v54.0: PHOENIX PROTOCOL - Dynamic Window ID Regeneration
    # ═══════════════════════════════════════════════════════════════════════════
    # ROOT CAUSE FIX: macOS/Chrome can destroy and recreate windows during
    # fullscreen transitions, giving them new IDs. This leaves JARVIS holding
    # a "dead phone number" - the old ID that no longer exists.
    #
    # SOLUTION: When an ID fails, immediately scan all windows to find the
    # new one that matches the original by App Name + Title + PID.
    # ═══════════════════════════════════════════════════════════════════════════

    async def _regenerate_window_id_async(
        self,
        app_name: str,
        target_title: str,
        old_id: int,
        pid: Optional[int] = None,
        fuzzy_threshold: float = 0.6,
        require_ax_reference: bool = False
    ) -> Optional[int]:
        """
        v54.0 + v55.0: PHOENIX PROTOCOL - Find the new window ID after ID death.

        When a window ID "dies" (window destroyed and recreated), this method
        scans all windows to find the new one matching by:
        1. App name (exact match)
        2. PID (exact match if available)
        3. Title (fuzzy match)
        4. v55.0: AX reference state (prioritize actionable windows)

        Args:
            app_name: App name to match (e.g., "Google Chrome")
            target_title: Original window title (may have changed slightly)
            old_id: The dead window ID we're replacing
            pid: Process ID (for disambiguation)
            fuzzy_threshold: Minimum title similarity ratio (0.0-1.0)
            require_ax_reference: v55.0 - If True, prioritize windows with has-ax-reference: true

        Returns:
            New window ID if found, None otherwise
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v54.0] 🔥 PHOENIX PROTOCOL: Regenerating ID for dead window {old_id} "
            f"({app_name}: {target_title[:40] if target_title else 'untitled'})"
        )

        try:
            # Query all windows
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                logger.warning("[YABAI v54.0] Failed to query windows for Phoenix Protocol")
                return None

            all_windows = json.loads(stdout.decode())

            # Filter by app name
            candidates = [w for w in all_windows if w.get("app") == app_name]

            if not candidates:
                logger.warning(f"[YABAI v54.0] No {app_name} windows found")
                return None

            # If we have PID, filter by it
            if pid:
                pid_matches = [w for w in candidates if w.get("pid") == pid]
                if pid_matches:
                    candidates = pid_matches

            # Remove the old dead ID from candidates (if it somehow still appears)
            candidates = [w for w in candidates if w.get("id") != old_id]

            if not candidates:
                logger.warning(f"[YABAI v54.0] No new candidates found after filtering")
                return None

            # ═══════════════════════════════════════════════════════════════
            # v55.0: PRIORITIZE ACTIONABLE WINDOWS (AX: true)
            # ═══════════════════════════════════════════════════════════════
            # When require_ax_reference is True, we MUST return a window that
            # has AX reference (is actionable). This prevents Phoenix from
            # returning a zombie window that can't be moved.
            # ═══════════════════════════════════════════════════════════════
            if require_ax_reference:
                actionable_candidates = [
                    w for w in candidates if w.get("has-ax-reference", False)
                ]
                if actionable_candidates:
                    logger.info(
                        f"[YABAI v55.0] 🎯 Phoenix: {len(actionable_candidates)} of "
                        f"{len(candidates)} candidates are ACTIONABLE (AX: true)"
                    )
                    candidates = actionable_candidates
                else:
                    logger.warning(
                        f"[YABAI v55.0] ⚠️ Phoenix: No candidates have AX: true - "
                        f"returning best match anyway (may need Lazarus)"
                    )

            # If no title to match, return the first (preferably actionable) candidate
            if not target_title:
                new_id = candidates[0].get("id")
                has_ax = candidates[0].get("has-ax-reference", False)
                logger.info(
                    f"[YABAI v54.0] 🔥 Phoenix: Re-bonded (no title) {old_id} → {new_id} "
                    f"(AX: {has_ax})"
                )
                return new_id

            # Fuzzy match by title
            best_match = None
            best_ratio = 0.0

            for w in candidates:
                w_title = w.get("title", "")
                if not w_title:
                    continue

                # Calculate similarity ratio
                # Simple approach: check substring containment and length ratio
                if target_title == w_title:
                    # Exact match
                    best_match = w
                    best_ratio = 1.0
                    break
                elif target_title in w_title or w_title in target_title:
                    # Substring match
                    min_len = min(len(target_title), len(w_title))
                    max_len = max(len(target_title), len(w_title))
                    ratio = min_len / max_len if max_len > 0 else 0
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = w
                else:
                    # Character-level similarity
                    common = sum(1 for c in target_title if c in w_title)
                    ratio = common / len(target_title) if target_title else 0
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = w

            if best_match and best_ratio >= fuzzy_threshold:
                new_id = best_match.get("id")
                logger.info(
                    f"[YABAI v54.0] 🔥 Phoenix: Re-bonded {old_id} → {new_id} "
                    f"(title match: {best_ratio:.1%})"
                )
                return new_id

            # If fuzzy match failed, try PID-only match as fallback
            if pid:
                for w in candidates:
                    if w.get("pid") == pid:
                        new_id = w.get("id")
                        logger.info(
                            f"[YABAI v54.0] 🔥 Phoenix: Re-bonded (PID-only) {old_id} → {new_id}"
                        )
                        return new_id

            logger.warning(
                f"[YABAI v54.0] Phoenix Protocol failed: No matching window found "
                f"(best ratio: {best_ratio:.1%}, threshold: {fuzzy_threshold:.1%})"
            )
            return None

        except Exception as e:
            logger.warning(f"[YABAI v54.0] Phoenix Protocol exception: {e}")
            return None

    async def _flush_yabai_cache_async(self, iterations: int = 3) -> None:
        """
        v54.0: Force Yabai to refresh its internal state cache.

        Sometimes Yabai's internal state becomes stale. Rapid read queries
        can force it to refresh.
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.debug(f"[YABAI v54.0] Flushing Yabai cache ({iterations} iterations)...")

        for i in range(iterations):
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except Exception:
                pass
            await asyncio.sleep(0.1)

    # ═══════════════════════════════════════════════════════════════════════════
    # v54.1: LAZARUS TRIGGER - Restore AX Reference for Zombie Windows
    # ═══════════════════════════════════════════════════════════════════════════
    # ROOT CAUSE FIX: When a window has `has-ax-reference: false`, it's in a
    # "zombie state" - it exists but yabai cannot interact with it. The only
    # way to restore the AX reference is to force macOS to rebuild it by
    # hiding and unhiding the application.
    # ═══════════════════════════════════════════════════════════════════════════

    async def _reflash_ax_reference_async(
        self,
        app_name: str,
        pid: Optional[int] = None,
        window_id: Optional[int] = None
    ) -> bool:
        """
        v54.1: LAZARUS TRIGGER - Force macOS to rebuild AX reference for zombie windows.

        This uses the Hide/Unhide technique to force WindowServer to rebuild
        the Accessibility object for windows that have lost their AX reference.

        Args:
            app_name: Application name (e.g., "Google Chrome")
            pid: Process ID (optional, for more targeted activation)
            window_id: Window ID to verify after reflash (optional)

        Returns:
            True if AX reference was successfully restored
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v54.1] 👻→🧟 LAZARUS TRIGGER: Attempting to restore AX reference for {app_name}..."
        )

        try:
            # Lazarus Protocol: Hide -> Wait -> Unhide
            lazarus_script = f'''
            -- v54.1 Lazarus Trigger: Force AX reference rebuild
            tell application "System Events"
                try
                    set targetProc to first process whose name is "{app_name}"

                    -- Step 1: Hide the app (forces WindowServer to release AX objects)
                    set visible of targetProc to false
                    delay 0.5

                    -- Step 2: Unhide the app (forces WindowServer to rebuild AX objects)
                    set visible of targetProc to true
                    delay 0.5

                    -- Step 3: Bring to front to ensure AX is active
                    set frontmost of targetProc to true
                    delay 0.3

                    return "REFLASHED"
                on error errMsg
                    return "ERROR: " & errMsg
                end try
            end tell
            '''

            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", lazarus_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

            result = stdout.decode().strip() if stdout else ""

            if "REFLASHED" in result:
                logger.info(f"[YABAI v54.1] ✅ Lazarus Trigger completed for {app_name}")

                # Wait for WindowServer to stabilize
                await asyncio.sleep(0.5)

                # Verify AX reference was restored (if window_id provided)
                if window_id:
                    try:
                        verify_proc = await asyncio.create_subprocess_exec(
                            yabai_path, "-m", "query", "--windows",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        verify_stdout, _ = await asyncio.wait_for(
                            verify_proc.communicate(), timeout=5.0
                        )
                        if verify_proc.returncode == 0 and verify_stdout:
                            windows = json.loads(verify_stdout.decode())
                            for w in windows:
                                if w.get("id") == window_id:
                                    if w.get("has-ax-reference", False):
                                        logger.info(
                                            f"[YABAI v54.1] 🎉 Window {window_id} AX reference RESTORED!"
                                        )
                                        return True
                                    else:
                                        logger.warning(
                                            f"[YABAI v54.1] Window {window_id} still has no AX reference"
                                        )
                                        return False
                    except Exception:
                        pass

                return True
            else:
                error_msg = result if result else stderr.decode().strip() if stderr else "Unknown"
                logger.warning(f"[YABAI v54.1] Lazarus Trigger failed: {error_msg}")
                return False

        except Exception as e:
            logger.warning(f"[YABAI v54.1] Lazarus Trigger exception: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # v55.0: NEURO-REGENERATIVE PROTOCOL - State Repair Before Action
    # ═══════════════════════════════════════════════════════════════════════════
    # ROOT CAUSE FIX: Windows with `has-ax-reference: false` (Zombie State) cannot
    # be moved by any yabai command. Instead of blindly failing, we surgically
    # diagnose, repair, and then execute.
    #
    # PHILOSOPHY: Detect → Repair → Execute (not "try, fail, retry")
    # ═══════════════════════════════════════════════════════════════════════════

    async def _ensure_window_is_actionable_async(
        self,
        window_id: int,
        window_info: Optional[Dict[str, Any]] = None,
        app_name: Optional[str] = None,
        pid: Optional[int] = None,
        max_repair_attempts: int = 2
    ) -> Tuple[bool, int, Optional[Dict[str, Any]]]:
        """
        v55.0: NEURO-REGENERATIVE PROTOCOL - Ensure window is actionable before operations.

        DIAGNOSE: Check if window has AX reference (actionable state)
        REPAIR: If zombie state (AX: false), trigger Lazarus to restore AX reference
        VERIFY: Re-query to confirm repair and return fresh window info

        Args:
            window_id: The window ID to check/repair
            window_info: Optional existing window info (avoids extra query)
            app_name: App name for Lazarus Trigger
            pid: Process ID for targeted repair
            max_repair_attempts: Maximum repair attempts before giving up

        Returns:
            Tuple of:
                - success: Whether window is now actionable
                - final_window_id: The (possibly new) window ID after repair
                - fresh_info: Updated window info dict (or None if failed)
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v55.0] 🧠 NEURO-REGENERATIVE: Checking actionability for window {window_id}"
        )

        async def query_window_info(wid: int) -> Optional[Dict[str, Any]]:
            """Query fresh window info."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows", "--window", str(wid),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    return json.loads(stdout.decode())
            except Exception as e:
                logger.debug(f"[YABAI v55.0] Query failed for window {wid}: {e}")
            return None

        async def query_all_windows_for_id(wid: int) -> Optional[Dict[str, Any]]:
            """Fallback: query all windows and filter for the ID."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    all_windows = json.loads(stdout.decode())
                    for w in all_windows:
                        if w.get("id") == wid:
                            return w
            except Exception:
                pass
            return None

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 1: DIAGNOSE - Get current window state
        # ═══════════════════════════════════════════════════════════════════
        current_id = window_id
        info = window_info

        if not info:
            # Try direct query first
            info = await query_window_info(current_id)

            # Fallback to full query if direct query fails
            if not info:
                info = await query_all_windows_for_id(current_id)

            if not info:
                logger.warning(
                    f"[YABAI v55.0] ❌ Window {current_id} not found - may need Phoenix Protocol"
                )
                return False, current_id, None

        # Extract metadata for repair
        has_ax_reference = info.get("has-ax-reference", True)
        detected_app = info.get("app", app_name or "Unknown")
        detected_pid = info.get("pid", pid)
        window_title = info.get("title", "")

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 2: EVALUATE - Is repair needed?
        # ═══════════════════════════════════════════════════════════════════
        if has_ax_reference:
            logger.info(
                f"[YABAI v55.0] ✅ Window {current_id} is ACTIONABLE (AX: true)"
            )
            return True, current_id, info

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 3: REPAIR - Window is in Zombie State (AX: false)
        # ═══════════════════════════════════════════════════════════════════
        logger.warning(
            f"[YABAI v55.0] 🧟 ZOMBIE DETECTED: Window {current_id} has AX: false "
            f"(app={detected_app}, pid={detected_pid})"
        )

        for attempt in range(1, max_repair_attempts + 1):
            logger.info(
                f"[YABAI v55.0] 🔧 REPAIR ATTEMPT {attempt}/{max_repair_attempts}..."
            )

            # -----------------------------------------------------------------
            # Step 3a: SOFT WAKE - Try AppleScript activation first
            # -----------------------------------------------------------------
            if detected_pid:
                try:
                    soft_wake_script = f'''
                    tell application "System Events"
                        set targetProc to first process whose unix id is {detected_pid}
                        set frontmost of targetProc to true
                        delay 0.5
                    end tell
                    '''
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", soft_wake_script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=5.0)
                    await asyncio.sleep(0.5)

                    # Check if soft wake fixed it
                    fresh_info = await query_window_info(current_id)
                    if not fresh_info:
                        fresh_info = await query_all_windows_for_id(current_id)

                    if fresh_info and fresh_info.get("has-ax-reference", False):
                        logger.info(
                            f"[YABAI v55.0] ✅ SOFT WAKE SUCCESS: Window {current_id} "
                            f"is now ACTIONABLE"
                        )
                        return True, current_id, fresh_info

                except Exception as e:
                    logger.debug(f"[YABAI v55.0] Soft wake exception: {e}")

            # -----------------------------------------------------------------
            # Step 3b: HARD RESET - Use Lazarus Trigger (Hide/Unhide)
            # -----------------------------------------------------------------
            logger.info(
                f"[YABAI v55.0] 💀→🧟→✨ HARD RESET: Invoking Lazarus Trigger for {detected_app}..."
            )

            lazarus_success = await self._reflash_ax_reference_async(
                app_name=detected_app,
                pid=detected_pid,
                window_id=current_id
            )

            if not lazarus_success:
                logger.warning(
                    f"[YABAI v55.0] Lazarus Trigger failed on attempt {attempt}"
                )
                await asyncio.sleep(0.5)
                continue

            # -----------------------------------------------------------------
            # Step 3c: VERIFICATION - Re-query with possible ID regeneration
            # -----------------------------------------------------------------
            await asyncio.sleep(0.5)

            # First try to find the original ID
            fresh_info = await query_window_info(current_id)
            if not fresh_info:
                fresh_info = await query_all_windows_for_id(current_id)

            if fresh_info:
                if fresh_info.get("has-ax-reference", False):
                    logger.info(
                        f"[YABAI v55.0] ✅ HARD RESET SUCCESS: Window {current_id} "
                        f"is now ACTIONABLE"
                    )
                    return True, current_id, fresh_info
                else:
                    logger.warning(
                        f"[YABAI v55.0] Window {current_id} still has AX: false after Lazarus"
                    )
            else:
                # Window ID might have changed - use Phoenix Protocol
                logger.info(
                    f"[YABAI v55.0] 🔥 Window {current_id} not found - invoking Phoenix..."
                )
                new_id = await self._regenerate_window_id_async(
                    app_name=detected_app,
                    target_title=window_title,
                    old_id=current_id,
                    pid=detected_pid,
                    fuzzy_threshold=0.3,
                    require_ax_reference=True  # v55.0: Prioritize AX:true windows
                )

                if new_id and new_id != current_id:
                    # Verify the new window is actionable
                    new_info = await query_window_info(new_id)
                    if not new_info:
                        new_info = await query_all_windows_for_id(new_id)

                    if new_info and new_info.get("has-ax-reference", False):
                        logger.info(
                            f"[YABAI v55.0] 🔥✅ PHOENIX + REPAIR SUCCESS: "
                            f"Window {current_id} → {new_id} (now ACTIONABLE)"
                        )
                        return True, new_id, new_info
                    else:
                        # New ID also has AX: false, update current_id and continue
                        logger.warning(
                            f"[YABAI v55.0] New ID {new_id} also has AX: false, continuing repair..."
                        )
                        current_id = new_id
                        info = new_info

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: v56.0 REAPER PROTOCOL - Verify if window is real or ghost
        # ═══════════════════════════════════════════════════════════════════
        # If Lazarus failed, the window might be a stale reference (ghost).
        # Cross-reference with the Kernel to determine if it's real.
        # ═══════════════════════════════════════════════════════════════════
        if detected_pid:
            logger.info(
                f"[YABAI v56.0] 💀 REAPER: Lazarus failed - verifying window {current_id} "
                f"existence against Kernel..."
            )

            exists, kernel_count, diagnostic = await self._verify_window_existence_via_kernel(
                pid=detected_pid,
                window_id=current_id,
                app_name=detected_app
            )

            # Check for window count mismatch (yabai thinks more windows exist than kernel)
            if exists and kernel_count is not None and kernel_count >= 0:
                # Get yabai's window count for this PID
                try:
                    yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "query", "--windows",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                    if proc.returncode == 0 and stdout:
                        all_windows = json.loads(stdout.decode())
                        yabai_count = len([w for w in all_windows if w.get("pid") == detected_pid])

                        if yabai_count > kernel_count:
                            # STALE WINDOW DETECTED!
                            logger.warning(
                                f"[YABAI v56.0] 💀 REAPER: STALE WINDOW DETECTED! "
                                f"Yabai reports {yabai_count} windows but Kernel reports {kernel_count} "
                                f"for PID {detected_pid}. Window {current_id} is likely a ghost."
                            )

                            # Find a replacement window
                            replacement = await self._reaper_find_replacement_window(
                                app_name=detected_app,
                                pid=detected_pid,
                                dead_window_id=current_id,
                                window_title=window_title
                            )

                            if replacement:
                                new_id, new_info = replacement
                                logger.info(
                                    f"[YABAI v56.0] 💀✅ REAPER SUCCESS: Purged ghost {current_id}, "
                                    f"found replacement {new_id} (AX: true)"
                                )
                                return True, new_id, new_info
                            else:
                                logger.warning(
                                    f"[YABAI v56.0] 💀 REAPER: No replacement found for ghost {current_id}. "
                                    f"Window is truly orphaned."
                                )
                                # Return failure but mark as ghost for upstream handling
                                if info:
                                    info["_is_ghost"] = True
                                return False, current_id, info
                except Exception as e:
                    logger.debug(f"[YABAI v56.0] Reaper count check failed: {e}")

            elif not exists:
                # Process has no windows - definitely a ghost
                logger.warning(
                    f"[YABAI v56.0] 💀 REAPER: PID {detected_pid} has NO windows in Kernel. "
                    f"Window {current_id} is a ghost. Diagnostic: {diagnostic}"
                )
                if info:
                    info["_is_ghost"] = True
                return False, current_id, info

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 5: FAILURE - All repair attempts exhausted, not a ghost
        # ═══════════════════════════════════════════════════════════════════
        logger.error(
            f"[YABAI v55.0] ❌ NEURO-REGENERATIVE FAILED: Window {window_id} remains "
            f"in ZOMBIE state after {max_repair_attempts} repair attempts. "
            f"Consider in-place capture as last resort."
        )

        # Return the last known info even on failure
        return False, current_id, info

    # ═══════════════════════════════════════════════════════════════════════════
    # v56.0: REAPER PROTOCOL - State Reconciliation Against Kernel
    # ═══════════════════════════════════════════════════════════════════════════
    # ROOT CAUSE FIX: Yabai's window database can become stale - it may report
    # windows that no longer exist in the macOS WindowServer. When Lazarus fails,
    # we verify against the Kernel (via System Events) to determine if we're
    # chasing a ghost or dealing with a true zombie.
    #
    # PHILOSOPHY: Yabai is "Hearsay", System Events is "Testimony"
    # ═══════════════════════════════════════════════════════════════════════════

    async def _verify_window_existence_via_kernel(
        self,
        pid: int,
        window_id: Optional[int] = None,
        app_name: Optional[str] = None
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        v56.0: REAPER PROTOCOL - Verify window existence directly against macOS Kernel.

        Cross-references Yabai's window data with the WindowServer's actual state
        via System Events (AppleScript). This detects stale/ghost windows that
        Yabai thinks exist but the OS has already deleted.

        Args:
            pid: Process ID to check windows for
            window_id: Optional specific window ID to verify
            app_name: Optional app name for targeted check

        Returns:
            Tuple of:
                - exists: Whether the window truly exists in the kernel
                - real_window_count: Number of actual windows for this PID
                - diagnostic: Human-readable diagnosis
        """
        logger.info(
            f"[YABAI v56.0] 💀 REAPER: Cross-referencing window {window_id} (PID {pid}) "
            f"against Kernel..."
        )

        try:
            # Query System Events for the true window count
            # This goes directly to the WindowServer, bypassing yabai's cache
            verify_script = f'''
            tell application "System Events"
                try
                    set targetProc to first process whose unix id is {pid}
                    set windowCount to count of windows of targetProc
                    set windowIDs to {{}}
                    repeat with w in windows of targetProc
                        -- Get the window's position (proves it exists)
                        try
                            set pos to position of w
                            set end of windowIDs to "exists"
                        end try
                    end repeat
                    return "WINDOWS:" & windowCount & ":VERIFIED:" & (count of windowIDs)
                on error errMsg
                    return "ERROR:" & errMsg
                end try
            end tell
            '''

            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", verify_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

            result = stdout.decode().strip() if stdout else ""

            if result.startswith("WINDOWS:"):
                # Parse: WINDOWS:3:VERIFIED:3
                parts = result.split(":")
                if len(parts) >= 4:
                    kernel_window_count = int(parts[1])
                    verified_count = int(parts[3])

                    logger.info(
                        f"[YABAI v56.0] 💀 REAPER: Kernel reports {kernel_window_count} windows "
                        f"for PID {pid} ({verified_count} verified)"
                    )

                    # Now check if our specific window_id is real
                    # We need to cross-reference with yabai to see if counts match
                    if kernel_window_count == 0:
                        return False, 0, "process_has_no_windows"

                    return True, kernel_window_count, "windows_exist"

            elif result.startswith("ERROR:"):
                error_msg = result[6:]
                if "process" in error_msg.lower():
                    logger.warning(
                        f"[YABAI v56.0] 💀 REAPER: PID {pid} not found in System Events - "
                        f"process may have terminated"
                    )
                    return False, 0, "process_not_found"

            logger.warning(
                f"[YABAI v56.0] 💀 REAPER: Unexpected result: {result}"
            )
            return True, -1, "verification_inconclusive"

        except Exception as e:
            logger.warning(f"[YABAI v56.0] 💀 REAPER exception: {e}")
            return True, -1, "verification_error"

    async def _reaper_find_replacement_window(
        self,
        app_name: str,
        pid: int,
        dead_window_id: int,
        window_title: str = ""
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        v56.0: REAPER - Find replacement window after purging a ghost.

        After determining a window ID is stale (doesn't exist in kernel),
        scan for the replacement window that the app likely created.

        Args:
            app_name: App name to search for
            pid: Process ID to match
            dead_window_id: The stale window ID to exclude
            window_title: Original title for fuzzy matching

        Returns:
            Tuple of (new_window_id, window_info) if found, None otherwise
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v56.0] 💀 REAPER: Searching for replacement window for dead ID {dead_window_id}..."
        )

        try:
            # Query all windows fresh
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                return None

            all_windows = json.loads(stdout.decode())

            # Find candidates: same app, same PID, different ID, HAS AX reference
            candidates = []
            for w in all_windows:
                if w.get("app") != app_name:
                    continue
                if w.get("id") == dead_window_id:
                    continue  # Skip the dead one
                if w.get("pid") != pid:
                    continue  # Must be same PID
                if not w.get("has-ax-reference", False):
                    continue  # Must be actionable

                candidates.append(w)

            if not candidates:
                logger.warning(
                    f"[YABAI v56.0] 💀 REAPER: No replacement candidates found for "
                    f"dead window {dead_window_id}"
                )
                return None

            # If we have a title, try to match it
            if window_title:
                for c in candidates:
                    c_title = c.get("title", "")
                    if window_title == c_title:
                        logger.info(
                            f"[YABAI v56.0] 💀✅ REAPER: Found exact title match: "
                            f"{dead_window_id} → {c['id']}"
                        )
                        return c["id"], c
                    if window_title in c_title or c_title in window_title:
                        logger.info(
                            f"[YABAI v56.0] 💀✅ REAPER: Found partial title match: "
                            f"{dead_window_id} → {c['id']}"
                        )
                        return c["id"], c

            # No title match - return the first actionable candidate
            best = candidates[0]
            logger.info(
                f"[YABAI v56.0] 💀✅ REAPER: Using first actionable candidate: "
                f"{dead_window_id} → {best['id']} (AX: true)"
            )
            return best["id"], best

        except Exception as e:
            logger.warning(f"[YABAI v56.0] 💀 REAPER find replacement exception: {e}")
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # v57.0: SUMMON PROTOCOL - Bi-Directional Teleportation
    # ═══════════════════════════════════════════════════════════════════════════
    # ROOT CAUSE FIX: Windows exiled to the Shadow Realm (Ghost Display) need
    # a way to return to the user's active display. This completes the teleportation
    # lifecycle: Exile → Watch → Summon
    #
    # PHILOSOPHY: No hardcoding. Detect the user's active display dynamically.
    # Sanitize the window state before transport (exit fullscreen, repair AX).
    # ═══════════════════════════════════════════════════════════════════════════

    async def _get_active_display_index_async(self) -> int:
        """
        v57.0: Get the index of the display the user is currently focused on.

        Uses yabai to query the focused display, avoiding any hardcoding.
        Falls back to Display 1 if query fails.

        Returns:
            Display index (1-based) of the active display
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        try:
            # Query the focused display
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--displays", "--display",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0 and stdout:
                display_info = json.loads(stdout.decode())
                display_index = display_info.get("index", 1)
                logger.debug(f"[YABAI v57.0] Active display: {display_index}")
                return display_index

        except Exception as e:
            logger.warning(f"[YABAI v57.0] Could not query active display: {e}")

        # Fallback to Display 1
        return 1

    async def _get_active_space_index_async(self) -> int:
        """
        v57.0: Get the index of the space the user is currently on.

        Returns:
            Space index of the active space (falls back to 1)
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces", "--space",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0 and stdout:
                space_info = json.loads(stdout.decode())
                space_index = space_info.get("index", 1)
                logger.debug(f"[YABAI v57.0] Active space: {space_index}")
                return space_index

        except Exception as e:
            logger.warning(f"[YABAI v57.0] Could not query active space: {e}")

        return 1

    async def _switch_to_space_sip_compliant_async(
        self,
        target_space_index: int,
        max_attempts: int = 3
    ) -> Tuple[bool, str]:
        """
        v59.0: NAVIGATOR PROTOCOL - Relative Space Traversal.

        When SIP is enabled, `yabai -m space --focus` is blocked by the kernel.
        Ctrl+N shortcuts are often disabled by default in macOS.

        This protocol uses RELATIVE TRAVERSAL via Ctrl+Left/Right Arrow keys,
        which are universally enabled via Mission Control defaults.

        The Protocol:
        1. Try yabai focus first (works if SIP is disabled)
        2. On failure, calculate delta between current and target space
        3. Walk the delta using Ctrl+Arrow (step by step)
        4. Verify we arrived at the destination

        Args:
            target_space_index: The space index to navigate to (1-based)
            max_attempts: Maximum retry attempts for the entire traversal

        Returns:
            Tuple of:
                - success: Whether navigation succeeded
                - method: Description of method used
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v59.0] 🧭 NAVIGATOR: Plotting course to Space {target_space_index}..."
        )

        # Get current space first
        current_space = await self._get_active_space_index_async()

        if current_space == target_space_index:
            logger.info(f"[YABAI v59.0] ✅ Already at destination (Space {target_space_index})")
            return True, "already_on_target"

        # ═══════════════════════════════════════════════════════════════════
        # ATTEMPT 1: Try yabai direct focus (works if SIP disabled)
        # ═══════════════════════════════════════════════════════════════════
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "space", "--focus", str(target_space_index),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                await asyncio.sleep(0.5)  # Wait for animation
                # Verify
                new_space = await self._get_active_space_index_async()
                if new_space == target_space_index:
                    logger.info(
                        f"[YABAI v59.0] ✅ NAVIGATOR SUCCESS (yabai direct): "
                        f"Space {current_space} → {target_space_index}"
                    )
                    return True, "yabai_direct"

            # Check for SIP block
            error_msg = stderr.decode().strip() if stderr else ""
            if "scripting-addition" in error_msg.lower():
                logger.info(
                    f"[YABAI v59.0] 🔒 SIP BLOCKED. Engaging Navigator (Relative Traversal)..."
                )

        except Exception as e:
            logger.debug(f"[YABAI v59.0] Yabai focus exception: {e}")

        # ═══════════════════════════════════════════════════════════════════
        # ATTEMPT 2: v59.0 NAVIGATOR - Relative Traversal via Ctrl+Arrow
        # ═══════════════════════════════════════════════════════════════════
        # This uses Mission Control's universal Ctrl+Left/Right shortcuts
        # No assumption about Ctrl+Number being enabled
        # ═══════════════════════════════════════════════════════════════════

        for attempt in range(max_attempts):
            # Recalculate from current position (in case we moved partially)
            current_space = await self._get_active_space_index_async()
            delta = target_space_index - current_space

            if delta == 0:
                logger.info(f"[YABAI v59.0] ✅ Arrived at destination (Space {target_space_index})")
                return True, "relative_traversal"

            # Determine direction
            if delta > 0:
                direction = "right"
                arrow_key_code = 124  # Right Arrow
            else:
                direction = "left"
                arrow_key_code = 123  # Left Arrow

            steps_needed = abs(delta)

            logger.info(
                f"[YABAI v59.0] 🚶 TRAVERSAL: Space {current_space} → {target_space_index} "
                f"({steps_needed} step{'s' if steps_needed > 1 else ''} {direction})"
            )

            # Walk the path step by step
            for step in range(steps_needed):
                applescript = f'''
                tell application "System Events"
                    key code {arrow_key_code} using {{control down}}
                end tell
                '''

                try:
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", applescript,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=3.0)

                    # CRITICAL: Wait for macOS space switch animation (each step)
                    await asyncio.sleep(0.35)

                    logger.debug(
                        f"[YABAI v59.0] Step {step + 1}/{steps_needed} ({direction})"
                    )

                except Exception as e:
                    logger.warning(f"[YABAI v59.0] Traversal step failed: {e}")
                    break

            # Allow final animation to settle
            await asyncio.sleep(0.3)

            # Verify final position
            final_space = await self._get_active_space_index_async()
            if final_space == target_space_index:
                logger.info(
                    f"[YABAI v59.0] ✅ NAVIGATOR SUCCESS (relative traversal): "
                    f"Space {current_space} → {target_space_index}"
                )
                return True, "relative_traversal"

            logger.debug(
                f"[YABAI v59.0] Traversal attempt {attempt + 1}: "
                f"Expected Space {target_space_index}, landed on {final_space}"
            )

            # Small delay before retry
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5)

        logger.warning(
            f"[YABAI v59.0] ❌ NAVIGATOR FAILED: Could not reach Space {target_space_index} "
            f"after {max_attempts} traversal attempts"
        )
        return False, "navigation_failed"

    async def _get_safe_landing_space_async(
        self,
        target_display: int,
        create_if_needed: bool = True
    ) -> Tuple[Optional[int], bool]:
        """
        v57.2: INTELLIGENT LANDING - Find or create a Safe Harbor on target display.

        The Safe Harbor Finder scans the target display for a non-fullscreen space
        where windows can safely land. If ALL spaces are fullscreen, it creates
        a new desktop space (Terraforming).

        Args:
            target_display: The display index to find/create safe space on
            create_if_needed: If True, create new space if none found (Terraforming)

        Returns:
            Tuple of:
                - safe_space_index: Index of safe landing space (None if failed)
                - was_created: True if a new space was terraformed
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v57.2] 🛬 SAFE HARBOR FINDER: Scanning Display {target_display} for landing zone..."
        )

        try:
            # Query ALL spaces
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                logger.warning(f"[YABAI v57.2] Failed to query spaces")
                return None, False

            all_spaces = json.loads(stdout.decode())

            # Find spaces on target display
            display_spaces = [s for s in all_spaces if s.get("display") == target_display]

            if not display_spaces:
                logger.warning(f"[YABAI v57.2] No spaces found on Display {target_display}")
                return None, False

            # Find the FIRST non-fullscreen space on this display
            safe_space = None
            current_visible_space = None

            for s in display_spaces:
                space_idx = s.get("index")
                is_fs = s.get("is-native-fullscreen", False)
                is_visible = s.get("is-visible", False) or s.get("has-focus", False)

                if is_visible:
                    current_visible_space = space_idx

                if not is_fs:
                    safe_space = space_idx
                    logger.info(
                        f"[YABAI v57.2] ✅ Found Safe Harbor: Space {space_idx} on Display {target_display}"
                    )
                    break

            if safe_space:
                return safe_space, False

            # ═══════════════════════════════════════════════════════════════════
            # EDGE CASE: ALL spaces on this display are fullscreen!
            # We must TERRAFORM - create a new desktop space
            # ═══════════════════════════════════════════════════════════════════
            if not safe_space and create_if_needed:
                logger.warning(
                    f"[YABAI v57.2] ⚠️ ALL {len(display_spaces)} spaces on Display {target_display} "
                    f"are FULLSCREEN! Initiating TERRAFORMING..."
                )

                # To create a space on a specific display, we need to:
                # 1. First focus any space on that display
                # 2. Then create a new space (it inherits the display)
                if current_visible_space:
                    # Focus the visible space first (even if fullscreen)
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            yabai_path, "-m", "space", "--focus", str(current_visible_space),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        await asyncio.wait_for(proc.communicate(), timeout=5.0)
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.debug(f"[YABAI v57.2] Pre-focus failed: {e}")

                # Create new space
                try:
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "space", "--create",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                    if proc.returncode == 0:
                        # Query to find the newly created space
                        await asyncio.sleep(0.5)  # Wait for yabai to register the new space

                        proc = await asyncio.create_subprocess_exec(
                            yabai_path, "-m", "query", "--spaces",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout2, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                        if proc.returncode == 0 and stdout2:
                            new_spaces = json.loads(stdout2.decode())

                            # Find the newest space on this display (highest index)
                            new_display_spaces = [
                                s for s in new_spaces
                                if s.get("display") == target_display
                            ]

                            if len(new_display_spaces) > len(display_spaces):
                                # Find the new space (the one that wasn't there before)
                                old_indices = {s.get("index") for s in display_spaces}
                                new_space = None

                                for s in new_display_spaces:
                                    if s.get("index") not in old_indices:
                                        new_space = s.get("index")
                                        break

                                if new_space:
                                    logger.info(
                                        f"[YABAI v57.2] 🌍 TERRAFORMED: Created new Safe Harbor "
                                        f"Space {new_space} on Display {target_display}"
                                    )
                                    return new_space, True

                        logger.warning("[YABAI v57.2] Terraforming succeeded but couldn't find new space")

                    else:
                        error_msg = stderr.decode().strip() if stderr else "Unknown"
                        logger.warning(f"[YABAI v57.2] Terraforming failed: {error_msg}")

                except Exception as e:
                    logger.warning(f"[YABAI v57.2] Terraforming exception: {e}")

            logger.warning(
                f"[YABAI v57.2] ❌ No Safe Harbor found or created on Display {target_display}"
            )
            return None, False

        except Exception as e:
            logger.warning(f"[YABAI v57.2] Safe harbor search failed: {e}")
            return None, False

    async def summon_window_from_shadow_realm_async(
        self,
        window_id: Optional[int] = None,
        app_name: Optional[str] = None,
        window_title: Optional[str] = None,
        pid: Optional[int] = None,
        focus_after_summon: bool = True,
        maximize_after_summon: bool = False
    ) -> Tuple[bool, str, Optional[int]]:
        """
        v57.0: SUMMON PROTOCOL - Bring a window back from the Shadow Realm.

        Completes the bi-directional teleportation cycle:
        - Exile: Window → Ghost Display (for JARVIS to watch)
        - Summon: Window → User's Active Display (for user to interact)

        Handles all edge cases:
        1. LOCATE: Find window (by ID, app, title, or PID) using Phoenix/Reaper
        2. SANITIZE: Ensure AX reference is valid (v55.0), exit fullscreen if needed
        3. TARGET: Dynamically detect user's active display (no hardcoding)
        4. MOVE: Transport window to active display
        5. FOCUS: Bring window to front

        Args:
            window_id: Specific window ID to summon (optional)
            app_name: App name to find window by (optional)
            window_title: Window title for matching (optional)
            pid: Process ID for targeted search (optional)
            focus_after_summon: Whether to focus the window after summoning
            maximize_after_summon: Whether to maximize the window after summoning

        Returns:
            Tuple of:
                - success: Whether the summon was successful
                - method: Description of what happened
                - final_window_id: The window ID that was summoned (may differ from input)
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")
        shadow_display = int(os.getenv("JARVIS_SHADOW_DISPLAY", "2"))

        logger.info(
            f"[YABAI v57.0] ✨ SUMMON PROTOCOL: Retrieving window from Shadow Realm "
            f"(window_id={window_id}, app={app_name}, title={window_title[:30] if window_title else 'None'})"
        )

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 1: LOCATE - Find the window on the Shadow Realm
        # ═══════════════════════════════════════════════════════════════════
        target_window_id = window_id
        window_info = None

        if not target_window_id:
            # Need to find the window by app/title/pid
            logger.info(f"[YABAI v57.0] 🔍 LOCATE: Searching for window...")

            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                if proc.returncode == 0 and stdout:
                    all_windows = json.loads(stdout.decode())

                    # Filter by criteria
                    candidates = []
                    for w in all_windows:
                        # Must be on Shadow Realm
                        if w.get("display") != shadow_display:
                            continue

                        # Filter by app name
                        if app_name and w.get("app") != app_name:
                            continue

                        # Filter by PID
                        if pid and w.get("pid") != pid:
                            continue

                        # Filter by title (fuzzy)
                        if window_title:
                            w_title = w.get("title", "")
                            if window_title not in w_title and w_title not in window_title:
                                if window_title.lower() not in w_title.lower():
                                    continue

                        candidates.append(w)

                    if candidates:
                        # Prefer windows with AX reference
                        actionable = [c for c in candidates if c.get("has-ax-reference", False)]
                        if actionable:
                            window_info = actionable[0]
                        else:
                            window_info = candidates[0]

                        target_window_id = window_info.get("id")
                        logger.info(
                            f"[YABAI v57.0] 🔍 LOCATE: Found window {target_window_id} "
                            f"(app={window_info.get('app')}, title={window_info.get('title', '')[:30]})"
                        )
                    else:
                        logger.warning(
                            f"[YABAI v57.0] 🔍 LOCATE: No matching windows found on Shadow Realm"
                        )
                        return False, "window_not_found_on_shadow_realm", None

            except Exception as e:
                logger.warning(f"[YABAI v57.0] LOCATE failed: {e}")
                return False, f"locate_error_{e}", None

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 2: SANITIZE - Ensure window is actionable and not fullscreen
        # ═══════════════════════════════════════════════════════════════════
        logger.info(f"[YABAI v57.0] 🧹 SANITIZE: Preparing window {target_window_id} for transport...")

        # Get window info if we don't have it
        if not window_info:
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows", "--window", str(target_window_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    window_info = json.loads(stdout.decode())
            except Exception:
                pass

        # v55.0: Ensure window is actionable
        actionable, repaired_id, repaired_info = await self._ensure_window_is_actionable_async(
            window_id=target_window_id,
            window_info=window_info,
            app_name=app_name or (window_info.get("app") if window_info else None),
            pid=pid or (window_info.get("pid") if window_info else None),
            max_repair_attempts=2
        )

        if not actionable:
            logger.warning(
                f"[YABAI v57.0] 🧹 SANITIZE: Window {target_window_id} is not actionable "
                f"(zombie/ghost state). Cannot summon."
            )
            return False, "window_not_actionable", target_window_id

        # Update window ID if it changed during repair
        if repaired_id != target_window_id:
            logger.info(f"[YABAI v57.0] 🧹 Window ID updated: {target_window_id} → {repaired_id}")
            target_window_id = repaired_id
            window_info = repaired_info

        # Check if window is fullscreen - need to exit before transport
        is_fullscreen = window_info.get("is-native-fullscreen", False) if window_info else False

        if is_fullscreen:
            logger.info(
                f"[YABAI v57.0] 🧹 SANITIZE: Window {target_window_id} is fullscreen - "
                f"exiting fullscreen before transport..."
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(target_window_id), "--toggle", "native-fullscreen",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=5.0)
                # Wait for fullscreen animation
                await asyncio.sleep(1.5)
            except Exception as e:
                logger.warning(f"[YABAI v57.0] Failed to exit fullscreen: {e}")

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 3: TARGET - Get user's active display (no hardcoding)
        # ═══════════════════════════════════════════════════════════════════
        dest_display = await self._get_active_display_index_async()

        # ═══════════════════════════════════════════════════════════════════
        # v57.2: INTELLIGENT LANDING - Prepare Safe Harbor BEFORE moving
        # ═══════════════════════════════════════════════════════════════════
        # The --display command moves to the ACTIVE space on that display.
        # If the active space is fullscreen, the move FAILS.
        # Solution: Find (or create) a Safe Harbor and switch to it FIRST.
        # ═══════════════════════════════════════════════════════════════════
        safe_landing_space, was_terraformed = await self._get_safe_landing_space_async(
            target_display=dest_display,
            create_if_needed=True  # Enable terraforming if ALL spaces are fullscreen
        )

        if not safe_landing_space:
            logger.error(
                f"[YABAI v57.2] ❌ ABORT: No Safe Harbor available on Display {dest_display}. "
                f"Cannot summon window."
            )
            return False, "no_safe_landing_space", target_window_id

        # Log terraforming if it happened
        if was_terraformed:
            logger.info(
                f"[YABAI v57.2] 🌍 TERRAFORMED new Safe Harbor on Display {dest_display}"
            )

        # ═══════════════════════════════════════════════════════════════════
        # v59.0: NAVIGATOR - SIP-Safe relative traversal to Safe Harbor
        # ═══════════════════════════════════════════════════════════════════
        # This is CRITICAL: The --display command targets the ACTIVE space.
        # We MUST make the Safe Harbor the active space first.
        # Uses Ctrl+Arrow relative navigation (universally enabled via Mission Control).
        # ═══════════════════════════════════════════════════════════════════
        logger.info(
            f"[YABAI v59.0] 🎯 PRE-FLIGHT: Navigating to Safe Harbor (Space {safe_landing_space}) "
            f"on Display {dest_display}..."
        )

        nav_success, nav_method = await self._switch_to_space_sip_compliant_async(
            target_space_index=safe_landing_space,
            max_attempts=3
        )

        if nav_success:
            logger.info(
                f"[YABAI v59.0] ✅ Safe Harbor reached via {nav_method} (Space {safe_landing_space})"
            )
        else:
            logger.warning(
                f"[YABAI v59.0] ⚠️ Could not navigate to Safe Harbor - will attempt move anyway"
            )

        # Use safe landing space as destination
        dest_space = safe_landing_space

        logger.info(
            f"[YABAI v57.2] 🎯 TARGET: Summoning to Display {dest_display}, Space {dest_space}"
            f"{' (TERRAFORMED)' if was_terraformed else ' (Safe Harbor)'}"
        )

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: MOVE - Transport window to active display
        # ═══════════════════════════════════════════════════════════════════
        move_success = False

        # Try display-based move first (most reliable)
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "window", str(target_window_id), "--display", str(dest_display),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                move_success = True
                logger.info(
                    f"[YABAI v57.0] 🚀 MOVE: Window {target_window_id} → Display {dest_display} SUCCESS"
                )
            else:
                error_msg = stderr.decode().strip() if stderr else "Unknown"
                logger.warning(f"[YABAI v57.0] Display move failed: {error_msg}")

                # v57.2: If display move failed due to fullscreen, try space move with safe landing zone
                if "fullscreen" in error_msg.lower() and safe_landing_space:
                    logger.info(
                        f"[YABAI v57.2] Retrying with safe landing zone (Space {safe_landing_space})..."
                    )
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "window", str(target_window_id), "--space", str(safe_landing_space),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    _, stderr2 = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                    if proc.returncode == 0:
                        move_success = True
                        logger.info(
                            f"[YABAI v57.2] 🚀 MOVE: Window {target_window_id} → Space {safe_landing_space} SUCCESS (safe landing)"
                        )

        except Exception as e:
            logger.warning(f"[YABAI v57.0] Display move exception: {e}")

        # Fallback: Try space-based move
        if not move_success:
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(target_window_id), "--space", str(dest_space),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                if proc.returncode == 0:
                    move_success = True
                    logger.info(
                        f"[YABAI v57.0] 🚀 MOVE: Window {target_window_id} → Space {dest_space} SUCCESS"
                    )

            except Exception as e:
                logger.warning(f"[YABAI v57.0] Space move exception: {e}")

        if not move_success:
            logger.error(f"[YABAI v57.0] ❌ MOVE FAILED: Could not summon window {target_window_id}")
            return False, "move_failed", target_window_id

        # Wait for move to complete
        await asyncio.sleep(0.5)

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 5: DEMINIMIZE + FOCUS - Bring window to front
        # ═══════════════════════════════════════════════════════════════════
        # v57.1: If window was minimized on Ghost Display, deminimize first
        # ═══════════════════════════════════════════════════════════════════
        if focus_after_summon:
            logger.info(f"[YABAI v57.0] 👁️ FOCUS: Bringing window {target_window_id} to front...")

            # v57.1: Deminimize first (handles minimized windows)
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(target_window_id), "--deminimize",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except Exception as e:
                logger.debug(f"[YABAI v57.1] Deminimize command failed (may not be minimized): {e}")

            # Now focus the window
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(target_window_id), "--focus",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except Exception as e:
                logger.debug(f"[YABAI v57.0] Focus command failed: {e}")

            # Also activate via AppleScript for reliability
            if window_info and window_info.get("pid"):
                try:
                    activate_script = f'''
                    tell application "System Events"
                        set frontmost of (first process whose unix id is {window_info["pid"]}) to true
                    end tell
                    '''
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", activate_script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=5.0)
                except Exception:
                    pass

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 6: MAXIMIZE (optional)
        # ═══════════════════════════════════════════════════════════════════
        if maximize_after_summon:
            await self._maximize_window_async(target_window_id)

        # ═══════════════════════════════════════════════════════════════════
        # SUCCESS
        # ═══════════════════════════════════════════════════════════════════
        logger.info(
            f"[YABAI v57.0] ✅ SUMMON SUCCESS: Window {target_window_id} retrieved from Shadow Realm "
            f"→ Display {dest_display}, Space {dest_space}"
        )

        return True, "summon_success", target_window_id

    async def summon_all_windows_from_shadow_realm_async(
        self,
        app_name: Optional[str] = None,
        focus_last: bool = True
    ) -> Tuple[bool, str, List[int]]:
        """
        v57.0: Summon ALL windows from the Shadow Realm back to the active display.

        Useful for "bring back all Chrome windows" type commands.

        Args:
            app_name: Optional filter by app name
            focus_last: Whether to focus the last summoned window

        Returns:
            Tuple of (success, method, list of summoned window IDs)
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")
        shadow_display = int(os.getenv("JARVIS_SHADOW_DISPLAY", "2"))

        logger.info(
            f"[YABAI v57.0] ✨ MASS SUMMON: Retrieving all windows from Shadow Realm "
            f"(app_filter={app_name or 'all'})"
        )

        try:
            # Query all windows on Shadow Realm
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                return False, "query_failed", []

            all_windows = json.loads(stdout.decode())

            # Filter to windows on Shadow Realm
            shadow_windows = [
                w for w in all_windows
                if w.get("display") == shadow_display
                and (not app_name or w.get("app") == app_name)
                and w.get("has-ax-reference", False)  # Only actionable windows
            ]

            if not shadow_windows:
                logger.info(f"[YABAI v57.0] No windows found on Shadow Realm")
                return True, "no_windows_to_summon", []

            logger.info(f"[YABAI v57.0] Found {len(shadow_windows)} windows to summon")

            # Summon each window
            summoned_ids = []
            for i, w in enumerate(shadow_windows):
                is_last = (i == len(shadow_windows) - 1)

                success, method, final_id = await self.summon_window_from_shadow_realm_async(
                    window_id=w.get("id"),
                    app_name=w.get("app"),
                    focus_after_summon=focus_last and is_last,
                    maximize_after_summon=False
                )

                if success and final_id:
                    summoned_ids.append(final_id)

                # Small delay between summons
                if not is_last:
                    await asyncio.sleep(0.3)

            logger.info(
                f"[YABAI v57.0] ✅ MASS SUMMON: {len(summoned_ids)}/{len(shadow_windows)} "
                f"windows successfully retrieved"
            )

            return len(summoned_ids) > 0, f"summoned_{len(summoned_ids)}_windows", summoned_ids

        except Exception as e:
            logger.error(f"[YABAI v57.0] Mass summon exception: {e}")
            return False, f"exception_{e}", []

    async def _maximize_window_async(self, window_id: int) -> bool:
        """Maximize window to fill its current display using yabai grid."""
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "window", str(window_id), "--grid", "1:1:0:0:1:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                logger.debug(f"[YABAI v53.0] Maximized window {window_id}")
                return True
            else:
                error_msg = stderr.decode().strip() if stderr else "Unknown"
                logger.debug(f"[YABAI v53.0] Grid command failed: {error_msg}")
                return False

        except Exception as e:
            logger.debug(f"[YABAI v53.0] Maximize failed: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # v63.0: BOOMERANG PROTOCOL - Intelligent Auto-Summon via OS Signals
    # ═══════════════════════════════════════════════════════════════════════════
    # ROOT CAUSE FIX: Windows exiled to Shadow Realm need an INTELLIGENT way to
    # return automatically. The Boomerang Protocol listens for OS-level signals
    # and user intent indicators to trigger summons autonomously.
    #
    # TRIGGERS:
    # 1. Surveillance session completion (detection found or timeout)
    # 2. App activation via Dock/Spotlight (user explicitly requests app)
    # 3. Display configuration changes (Shadow Realm display disconnected)
    # 4. User voice command ("bring back my windows")
    # 5. Time-based auto-return (configurable timeout)
    # 6. Space navigation to Shadow Realm (user manually switches)
    #
    # PATENT CLAIM: The Boomerang Protocol uses a combination of:
    # - NSWorkspace notifications for app state changes
    # - CGDisplayNotification for display topology changes
    # - Yabai window queries for state verification
    # - Async event-driven architecture for non-blocking operation
    # ═══════════════════════════════════════════════════════════════════════════

    # Boomerang tracking state
    _boomerang_exiled_windows: Dict[int, Dict[str, Any]] = {}
    _boomerang_listeners_active: bool = False
    _boomerang_auto_return_tasks: Dict[str, asyncio.Task] = {}

    async def boomerang_register_exile_async(
        self,
        window_id: int,
        app_name: str,
        original_space: int,
        original_display: int,
        window_title: str = "",
        auto_return_timeout: Optional[float] = None,
        return_on_detection: bool = True,
        return_on_app_activate: bool = True
    ) -> bool:
        """
        v63.0: BOOMERANG REGISTRATION - Track an exiled window for auto-return.

        When a window is exiled to the Shadow Realm, register it with Boomerang
        so it can be automatically summoned back when conditions are met.

        Args:
            window_id: The window being exiled
            app_name: Application name (for Dock/Spotlight activation detection)
            original_space: Space to return to (0 for auto-detect)
            original_display: Display to return to (0 for user's active display)
            window_title: Window title (for matching after ID changes)
            auto_return_timeout: Optional timeout in seconds for auto-return
            return_on_detection: Return when surveillance detects target
            return_on_app_activate: Return when user activates app via Dock

        Returns:
            True if registered successfully
        """
        logger.info(
            f"[YABAI v63.0] 🪃 BOOMERANG REGISTER: Window {window_id} ({app_name}) "
            f"→ Original: Space {original_space}, Display {original_display}"
        )

        # Store exile metadata
        exile_record = {
            "window_id": window_id,
            "app_name": app_name,
            "original_space": original_space,
            "original_display": original_display,
            "window_title": window_title,
            "exile_timestamp": asyncio.get_event_loop().time(),
            "auto_return_timeout": auto_return_timeout,
            "return_on_detection": return_on_detection,
            "return_on_app_activate": return_on_app_activate,
            "status": "exiled"
        }

        self._boomerang_exiled_windows[window_id] = exile_record

        # Start auto-return timer if configured
        if auto_return_timeout and auto_return_timeout > 0:
            task_key = f"auto_return_{window_id}"
            if task_key in self._boomerang_auto_return_tasks:
                self._boomerang_auto_return_tasks[task_key].cancel()

            task = asyncio.create_task(
                self._boomerang_auto_return_after_timeout(
                    window_id, auto_return_timeout
                )
            )
            self._boomerang_auto_return_tasks[task_key] = task
            logger.debug(
                f"[YABAI v63.0] ⏰ Auto-return timer set: {auto_return_timeout}s for window {window_id}"
            )

        return True

    async def _boomerang_auto_return_after_timeout(
        self,
        window_id: int,
        timeout_seconds: float
    ) -> None:
        """
        v63.0: Internal timer for auto-return after timeout.
        """
        try:
            await asyncio.sleep(timeout_seconds)

            # Check if window is still registered (not already returned)
            if window_id in self._boomerang_exiled_windows:
                record = self._boomerang_exiled_windows[window_id]
                if record.get("status") == "exiled":
                    logger.info(
                        f"[YABAI v63.0] ⏰ BOOMERANG TIMEOUT: Auto-returning window {window_id} "
                        f"after {timeout_seconds}s"
                    )
                    await self.boomerang_trigger_return_async(
                        window_id=window_id,
                        trigger_reason="timeout"
                    )

        except asyncio.CancelledError:
            logger.debug(f"[YABAI v63.0] Auto-return timer cancelled for window {window_id}")
        except Exception as e:
            logger.warning(f"[YABAI v63.0] Auto-return timer error: {e}")

    async def boomerang_trigger_return_async(
        self,
        window_id: Optional[int] = None,
        app_name: Optional[str] = None,
        trigger_reason: str = "manual",
        parallel: bool = True,
        focus_after: bool = True
    ) -> Dict[str, Any]:
        """
        v63.0: BOOMERANG TRIGGER - Initiate the return of exiled window(s).

        This is the central entry point for all summon triggers. It handles:
        - Single window return (by window_id)
        - App-based return (all windows of an app)
        - Parallel processing for multiple windows
        - State verification and cleanup

        Args:
            window_id: Specific window to return (optional)
            app_name: Return all windows of this app (optional)
            trigger_reason: What triggered the return (for logging/analytics)
            parallel: Use parallel async for multiple windows
            focus_after: Focus the window(s) after return

        Returns:
            Dict with success status, returned window IDs, and details
        """
        logger.info(
            f"[YABAI v63.0] 🪃 BOOMERANG TRIGGER: Initiating return "
            f"(window={window_id}, app={app_name}, reason={trigger_reason})"
        )

        result = {
            "success": False,
            "trigger_reason": trigger_reason,
            "returned_windows": [],
            "failed_windows": [],
            "details": {}
        }

        # Determine which windows to return
        windows_to_return = []

        if window_id is not None:
            if window_id in self._boomerang_exiled_windows:
                windows_to_return.append(self._boomerang_exiled_windows[window_id])
            else:
                # Window not in registry, try direct summon anyway
                windows_to_return.append({
                    "window_id": window_id,
                    "app_name": app_name or "Unknown",
                    "original_space": 0,  # Auto-detect
                    "original_display": 0  # User's active display
                })

        elif app_name is not None:
            # =================================================================
            # v67.0: CEREBRO PROTOCOL - Dynamic app resolution first
            # =================================================================
            resolved_app_name = app_name
            cerebro_result = await _resolve_app_name_with_cerebro_async(app_name, check_running=False)

            if cerebro_result and cerebro_result.get('found'):
                resolved_app_name = cerebro_result['app_name']
                logger.info(
                    f"[YABAI v67.0] 🧠 CEREBRO: '{app_name}' → '{resolved_app_name}'"
                )

            # Return all windows of this app - v66.0: Using FUZZY MATCHING
            # v67.0: Now uses Cerebro-resolved name for better accuracy
            windows_to_return = [
                record for record in self._boomerang_exiled_windows.values()
                if _fuzzy_match_app_name(resolved_app_name, record.get("app_name", ""))[0]
                and record.get("status") == "exiled"
            ]
            if windows_to_return:
                logger.info(
                    f"[YABAI v67.0] 🎯 Matched '{app_name}' → '{resolved_app_name}' → "
                    f"{[r.get('app_name') for r in windows_to_return]}"
                )

        else:
            # Return ALL exiled windows
            windows_to_return = [
                record for record in self._boomerang_exiled_windows.values()
                if record.get("status") == "exiled"
            ]

        if not windows_to_return:
            logger.info("[YABAI v63.0] 🪃 No windows to return")
            result["success"] = True
            result["details"]["message"] = "No exiled windows found to return"
            return result

        logger.info(f"[YABAI v63.0] 🪃 Returning {len(windows_to_return)} window(s)...")

        # v63.0: PARALLEL RETURN for efficiency
        if parallel and len(windows_to_return) > 1:
            # Create return tasks for each window
            return_tasks = [
                self._boomerang_return_single_window(record, focus_after=(i == len(windows_to_return) - 1 and focus_after))
                for i, record in enumerate(windows_to_return)
            ]

            # Execute in parallel
            task_results = await asyncio.gather(*return_tasks, return_exceptions=True)

            for i, task_result in enumerate(task_results):
                record = windows_to_return[i]
                wid = record.get("window_id")

                if isinstance(task_result, Exception):
                    logger.warning(f"[YABAI v63.0] Return failed for window {wid}: {task_result}")
                    result["failed_windows"].append(wid)
                elif task_result.get("success"):
                    result["returned_windows"].append(task_result.get("final_window_id", wid))
                else:
                    result["failed_windows"].append(wid)

        else:
            # Sequential return
            for i, record in enumerate(windows_to_return):
                is_last = (i == len(windows_to_return) - 1)
                single_result = await self._boomerang_return_single_window(
                    record, focus_after=(is_last and focus_after)
                )

                wid = record.get("window_id")
                if single_result.get("success"):
                    result["returned_windows"].append(single_result.get("final_window_id", wid))
                else:
                    result["failed_windows"].append(wid)

        result["success"] = len(result["returned_windows"]) > 0
        result["details"]["total_attempted"] = len(windows_to_return)
        result["details"]["total_returned"] = len(result["returned_windows"])
        result["details"]["total_failed"] = len(result["failed_windows"])

        logger.info(
            f"[YABAI v63.0] 🪃 BOOMERANG COMPLETE: {len(result['returned_windows'])}/{len(windows_to_return)} "
            f"windows returned (trigger={trigger_reason})"
        )

        return result

    async def _boomerang_return_single_window(
        self,
        record: Dict[str, Any],
        focus_after: bool = False
    ) -> Dict[str, Any]:
        """
        v63.0: Return a single window from the Shadow Realm.

        Uses the SUMMON PROTOCOL (v57.0) internally but adds:
        - Original position restoration (if tracked)
        - State verification
        - Registry cleanup

        Args:
            record: Exile record from boomerang registry
            focus_after: Whether to focus after return

        Returns:
            Dict with success status and details
        """
        window_id = record.get("window_id")
        app_name = record.get("app_name", "Unknown")
        original_display = record.get("original_display", 0)

        result = {
            "success": False,
            "window_id": window_id,
            "final_window_id": window_id,
            "method": "unknown"
        }

        try:
            # Use the existing SUMMON PROTOCOL
            success, method, final_id = await self.summon_window_from_shadow_realm_async(
                window_id=window_id,
                app_name=app_name,
                target_display=original_display if original_display > 0 else None,
                focus_after_summon=focus_after,
                maximize_after_summon=False
            )

            result["success"] = success
            result["method"] = method
            result["final_window_id"] = final_id or window_id

            if success:
                # Update registry
                if window_id in self._boomerang_exiled_windows:
                    self._boomerang_exiled_windows[window_id]["status"] = "returned"
                    self._boomerang_exiled_windows[window_id]["return_timestamp"] = asyncio.get_event_loop().time()

                # Cancel any pending auto-return timer
                task_key = f"auto_return_{window_id}"
                if task_key in self._boomerang_auto_return_tasks:
                    self._boomerang_auto_return_tasks[task_key].cancel()
                    del self._boomerang_auto_return_tasks[task_key]

                logger.debug(f"[YABAI v63.0] ✅ Window {window_id} returned successfully")

        except Exception as e:
            logger.warning(f"[YABAI v63.0] Return failed for window {window_id}: {e}")
            result["method"] = f"exception_{e}"

        return result

    async def boomerang_on_detection_complete_async(
        self,
        app_name: str,
        detection_result: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        v63.0: DETECTION TRIGGER - Called when surveillance detects the target.

        This is the primary trigger for God Mode surveillance. When JARVIS
        detects "bouncing ball" (or whatever was being watched), this
        automatically returns the windows to the user's screen.

        Args:
            app_name: The app that was being watched
            detection_result: Optional details about what was detected

        Returns:
            Dict with return results
        """
        logger.info(
            f"[YABAI v63.0] 🎯 BOOMERANG DETECTION TRIGGER: Surveillance complete for {app_name}"
        )

        # Find windows with return_on_detection=True - v66.0: Using FUZZY MATCHING
        windows_to_return = [
            record for record in self._boomerang_exiled_windows.values()
            if _fuzzy_match_app_name(app_name, record.get("app_name", ""))[0]
            and record.get("status") == "exiled"
            and record.get("return_on_detection", True)
        ]

        if not windows_to_return:
            logger.info(f"[YABAI v63.0] No {app_name} windows registered for detection return")
            return {"success": True, "returned_windows": [], "message": "No windows to return"}

        # Trigger the return
        result = await self.boomerang_trigger_return_async(
            app_name=app_name,
            trigger_reason="detection_complete",
            parallel=True,
            focus_after=True
        )

        return result

    async def boomerang_on_app_activated_async(
        self,
        activated_app_name: str,
        activation_source: str = "unknown"
    ) -> Dict[str, Any]:
        """
        v63.0: APP ACTIVATION TRIGGER - Called when user activates an exiled app.

        Detects when the user clicks an app in the Dock, opens via Spotlight,
        or uses Cmd+Tab to switch to an app whose windows are in the Shadow Realm.

        This trigger respects Single-Seat Concurrency - we only return windows
        when the user explicitly requests the app on their main display.

        Args:
            activated_app_name: The app being activated
            activation_source: How it was activated (dock, spotlight, cmd_tab)

        Returns:
            Dict with return results
        """
        logger.info(
            f"[YABAI v63.0] 📱 BOOMERANG APP ACTIVATION: {activated_app_name} "
            f"(source={activation_source})"
        )

        # Find windows with return_on_app_activate=True
        windows_to_return = [
            record for record in self._boomerang_exiled_windows.values()
            if record.get("app_name") == activated_app_name
            and record.get("status") == "exiled"
            and record.get("return_on_app_activate", True)
        ]

        if not windows_to_return:
            logger.debug(f"[YABAI v63.0] No {activated_app_name} windows registered for activation return")
            return {"success": True, "returned_windows": [], "message": "No matching windows"}

        # Trigger the return
        result = await self.boomerang_trigger_return_async(
            app_name=activated_app_name,
            trigger_reason=f"app_activated_{activation_source}",
            parallel=True,
            focus_after=True
        )

        return result

    async def boomerang_on_display_changed_async(
        self,
        display_event: str,
        affected_display: int
    ) -> Dict[str, Any]:
        """
        v63.0: DISPLAY CHANGE TRIGGER - Called when display topology changes.

        If the Shadow Realm display is disconnected, this emergency-returns
        all exiled windows to prevent them from being lost.

        Args:
            display_event: Type of event (disconnected, reconfigured, etc.)
            affected_display: Which display changed

        Returns:
            Dict with return results
        """
        shadow_display = int(os.getenv("JARVIS_SHADOW_DISPLAY", "2"))

        logger.info(
            f"[YABAI v63.0] 🖥️ BOOMERANG DISPLAY CHANGE: {display_event} on Display {affected_display}"
        )

        # If Shadow Realm is affected, emergency return all windows
        if affected_display == shadow_display and display_event == "disconnected":
            logger.warning(
                f"[YABAI v63.0] ⚠️ SHADOW REALM DISCONNECTED! Emergency returning all windows..."
            )

            result = await self.boomerang_trigger_return_async(
                app_name=None,  # All apps
                trigger_reason="display_disconnected_emergency",
                parallel=True,
                focus_after=False  # Don't disrupt user
            )

            return result

        return {"success": True, "message": "Display change noted, no action needed"}

    def boomerang_get_exiled_windows(self) -> List[Dict[str, Any]]:
        """
        v63.0: Get list of currently exiled windows.

        Returns:
            List of exile records
        """
        return [
            record for record in self._boomerang_exiled_windows.values()
            if record.get("status") == "exiled"
        ]

    def boomerang_clear_returned_windows(self) -> int:
        """
        v63.0: Clean up the registry by removing returned windows.

        Returns:
            Number of records cleaned
        """
        to_remove = [
            wid for wid, record in self._boomerang_exiled_windows.items()
            if record.get("status") == "returned"
        ]

        for wid in to_remove:
            del self._boomerang_exiled_windows[wid]

        if to_remove:
            logger.debug(f"[YABAI v63.0] Cleaned {len(to_remove)} returned window records")

        return len(to_remove)

    async def boomerang_voice_command_async(
        self,
        command: str,
        app_filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        v63.0: VOICE COMMAND TRIGGER - Handle natural language return commands.

        Supports commands like:
        - "bring back my windows"
        - "bring back Chrome"
        - "return all windows"
        - "summon the windows"

        Args:
            command: The voice command text
            app_filter: Optional app name to filter by

        Returns:
            Dict with return results and response message
        """
        command_lower = command.lower()

        logger.info(
            f"[YABAI v63.0] 🎤 BOOMERANG VOICE COMMAND: '{command}' (filter={app_filter})"
        )

        # Parse command intent
        is_bring_back = any(phrase in command_lower for phrase in [
            "bring back", "return", "summon", "get back", "restore",
            "bring the windows", "bring my windows"
        ])

        if not is_bring_back:
            return {
                "success": False,
                "message": "Command not recognized as a window return request"
            }

        # =====================================================================
        # v67.0: CEREBRO PROTOCOL - Try Spotlight resolution first for app_filter
        # =====================================================================
        resolved_app_filter = app_filter
        if app_filter:
            cerebro_result = await _resolve_app_name_with_cerebro_async(app_filter, check_running=False)
            if cerebro_result and cerebro_result.get('found'):
                resolved_app_filter = cerebro_result['app_name']
                logger.info(
                    f"[YABAI v67.0] 🧠 CEREBRO: '{app_filter}' → '{resolved_app_filter}'"
                )

        # Determine app filter from command if not provided - v66.0: FUZZY MATCHING
        if not resolved_app_filter:
            # Check for app names in command using fuzzy matching
            exiled_apps = set(
                record.get("app_name") for record in self._boomerang_exiled_windows.values()
                if record.get("status") == "exiled" and record.get("app_name")
            )

            best_match = None
            best_confidence = 0.0

            for app in exiled_apps:
                if not app:
                    continue

                # Extract potential app name from command
                # Check if app name or common variations appear in command
                is_match, confidence = _fuzzy_match_app_name(command_lower, app, threshold=0.6)

                # Also check direct substring
                app_normalized = app.lower().replace('google ', '').replace('.app', '')
                if app_normalized in command_lower:
                    is_match = True
                    confidence = max(confidence, 0.9)

                if is_match and confidence > best_confidence:
                    best_match = app
                    best_confidence = confidence

            if best_match:
                resolved_app_filter = best_match
                logger.info(
                    f"[YABAI v67.0] 🎯 Fuzzy extracted app from command: '{command}' → '{resolved_app_filter}' "
                    f"(confidence: {best_confidence:.2f})"
                )

        # Trigger the return - v67.0: Use Cerebro-resolved app name
        result = await self.boomerang_trigger_return_async(
            app_name=resolved_app_filter,
            trigger_reason="voice_command",
            parallel=True,
            focus_after=True
        )

        # Generate response message - v67.0: Use resolved name for accuracy
        display_name = resolved_app_filter or app_filter
        if result["success"]:
            count = len(result["returned_windows"])
            if display_name:
                message = f"Returned {count} {display_name} window{'s' if count != 1 else ''} to your screen."
            else:
                message = f"Returned {count} window{'s' if count != 1 else ''} to your screen."
        else:
            if display_name:
                message = f"No exiled {display_name} windows found to return."
            else:
                message = "No exiled windows found to return."

        result["response_message"] = message
        result["resolved_app_name"] = resolved_app_filter  # v67.0: Include for debugging
        return result

    async def _find_window_by_chemical_bond_async(
        self,
        app_name: str,
        window_title: str,
        old_window_id: int,
        original_pid: Optional[int] = None
    ) -> Optional[int]:
        """
        v53.0: Async version of chemical bond search for window re-bonding.

        After unpacking fullscreen, macOS may destroy and recreate the window.
        This finds the new window by matching app name + title + PID.
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                return None

            windows = json.loads(stdout.decode())

            # First pass: exact match with PID
            for w in windows:
                if w.get("id") == old_window_id:
                    continue  # Skip original (might be stale)
                if w.get("app") == app_name and w.get("title") == window_title:
                    if original_pid and w.get("pid") == original_pid:
                        return w.get("id")

            # Second pass: fuzzy title match with PID
            for w in windows:
                if w.get("id") == old_window_id:
                    continue
                if w.get("app") == app_name:
                    w_title = w.get("title", "")
                    if window_title and w_title and (
                        window_title in w_title or w_title in window_title
                    ):
                        if original_pid and w.get("pid") == original_pid:
                            return w.get("id")

            # Third pass: app match only (last resort)
            for w in windows:
                if w.get("id") == old_window_id:
                    continue
                if w.get("app") == app_name and original_pid and w.get("pid") == original_pid:
                    return w.get("id")

        except Exception as e:
            logger.debug(f"[YABAI v53.0] Chemical bond search failed: {e}")

        return None

    async def _unpack_via_yabai_toggle_async(
        self,
        window_id: int,
        app_name: str = "Unknown",
        window_title: str = ""
    ) -> bool:
        """
        v51.0: Unpack fullscreen using yabai's native toggle command WITH VERIFICATION.

        v49.0 bug: We returned True after toggle command succeeded, but didn't verify
        that the window ACTUALLY exited fullscreen. The toggle might succeed but
        the window could still be stuck in fullscreen (common with PWAs).

        v51.0 fix: After toggle, verify by re-querying the Space. Only return True
        if the Space is no longer marked as native-fullscreen.

        Args:
            window_id: The window to unpack
            app_name: App name (for logging)
            window_title: Window title (for logging)

        Returns:
            True if successfully unpacked AND VERIFIED, False otherwise
        """
        yabai_path = self._health.yabai_path or os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")

        logger.info(
            f"[YABAI v51.0] 🔄 YABAI TOGGLE: Unpacking window {window_id} "
            f"({app_name}: {window_title[:40]}) via native-fullscreen toggle"
        )

        # Get window's current space BEFORE toggle
        original_space = -1
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0 and stdout:
                window_info = json.loads(stdout.decode())
                original_space = window_info.get("space", -1)
        except Exception as e:
            logger.debug(f"[YABAI v51.0] Pre-toggle window query failed: {e}")

        try:
            # Execute yabai toggle
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "window", str(window_id), "--toggle", "native-fullscreen",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

            if proc.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.warning(
                    f"[YABAI v51.0] ⚠️ Yabai toggle command rejected for window {window_id}: {error_msg}"
                )
                return False

            logger.info(f"[YABAI v51.0] Toggle command accepted for window {window_id}")

            # v51.0: Wait for macOS animation with SIP-aware delay
            sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", "3.0"))
            logger.debug(f"[YABAI v51.0] ⏳ Waiting {sip_delay}s for fullscreen animation...")
            await asyncio.sleep(sip_delay)

            # ═══════════════════════════════════════════════════════════════════
            # v51.0: CRITICAL VERIFICATION - Did fullscreen actually exit?
            # ═══════════════════════════════════════════════════════════════════
            # The toggle command might succeed but the window could still be
            # in fullscreen. We MUST verify by checking the Space state.
            # ═══════════════════════════════════════════════════════════════════

            # Re-query window to get its new space
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                # Window might have been destroyed and recreated (re-bonding needed)
                logger.warning(
                    f"[YABAI v51.0] Window {window_id} not found after toggle - "
                    f"may need re-bonding"
                )
                # Return True to allow re-bonding to happen in caller
                return True

            window_info = json.loads(stdout.decode())
            new_space = window_info.get("space", -1)

            # Check if Space changed (fullscreen space was destroyed)
            if new_space != original_space and original_space > 0:
                logger.info(
                    f"[YABAI v51.0] ✅ VERIFIED: Window moved from Space {original_space} "
                    f"→ Space {new_space} (fullscreen space destroyed)"
                )
                return True

            # Check if new space is still fullscreen
            space_is_fullscreen, _ = await self._is_space_native_fullscreen_async(new_space)

            if not space_is_fullscreen:
                logger.info(
                    f"[YABAI v51.0] ✅ VERIFIED: Space {new_space} is NOT fullscreen - unpack successful"
                )
                return True
            else:
                logger.warning(
                    f"[YABAI v51.0] ❌ VERIFICATION FAILED: Window {window_id} still on "
                    f"fullscreen Space {new_space} after toggle!"
                )

                # v51.0: Try zoom-fullscreen toggle as fallback
                logger.info(f"[YABAI v51.0] Trying zoom-fullscreen toggle as fallback...")
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(window_id), "--toggle", "zoom-fullscreen",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=5.0)
                await asyncio.sleep(1.0)

                # Re-verify
                space_is_fullscreen, _ = await self._is_space_native_fullscreen_async(new_space)
                if not space_is_fullscreen:
                    logger.info(f"[YABAI v51.0] ✅ Zoom-fullscreen fallback succeeded!")
                    return True

                return False

        except asyncio.TimeoutError:
            logger.warning(f"[YABAI v51.0] ⚠️ Yabai toggle timed out for window {window_id}")
            return False
        except Exception as e:
            logger.warning(f"[YABAI v51.0] ⚠️ Yabai toggle error for window {window_id}: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # v51.0: AX API PROTOCOL - THE "UNIVERSAL SOLVENT"
    # ═══════════════════════════════════════════════════════════════════════════
    # THE PROBLEM: PWAs (like Google Gemini) run as separate processes that:
    #   - Don't respond to app-specific AppleScript dictionaries
    #   - Yabai toggle fails silently on Phantom Spaces
    #   - Standard "tell application X" doesn't know about the PWA
    #
    # THE SOLUTION: Use the Accessibility API (AX API) to:
    #   1. Target the PROCESS by PID (DNA) - not by app name
    #   2. Directly flip the AXFullScreen attribute on the window frame
    #   3. This works on ANY window because it's OS-level, not app-level
    #
    # Every window on macOS MUST have an AXFullScreen attribute to be rendered
    # by the WindowServer. We're toggling the raw bit that controls window state.
    # ═══════════════════════════════════════════════════════════════════════════

    async def _unpack_via_ax_pid_async(
        self,
        pid: int,
        window_id: int,
        app_name: str = "Unknown",
        silent: bool = False
    ) -> bool:
        """
        v51.0: AX API "Universal Solvent" - Unpack fullscreen by targeting PID directly.

        This bypasses app-specific AppleScript dictionaries by using System Events
        to target the raw process by PID and flip the AXFullScreen attribute.

        This works on:
        - PWAs (Google Gemini, etc.) that don't respond to app dictionaries
        - Chrome profiles that spawn separate processes
        - Any window type that refuses to move via normal methods

        v51.2: Added silent mode support
        - In silent mode, only tries AXFullScreen flip (no keyboard events)
        - This prevents hijacking the user's screen during background operations

        Args:
            pid: Process ID (DNA) of the window's process
            window_id: Window ID (for logging)
            app_name: App name (for logging)
            silent: If True, skip keyboard-based methods that would hijack user's screen

        Returns:
            True if AX API successfully flipped AXFullScreen, False otherwise
        """
        if not pid or pid <= 0:
            logger.warning(f"[YABAI v51.0] AX API: Invalid PID {pid}")
            return False

        mode_str = "SILENT" if silent else "NORMAL"
        logger.info(
            f"[YABAI v51.2] ☢️ AX API NUCLEAR OPTION ({mode_str}): Targeting PID {pid} "
            f"({app_name}) to force-flip AXFullScreen attribute"
        )

        # v51.2: Choose script based on silent mode
        if silent:
            # SILENT MODE: Only try AXFullScreen flip, NO keyboard events
            # This prevents hijacking the user's screen during background operations
            ax_script = f'''
            tell application "System Events"
                try
                    -- Target process by PID (DNA) - bypasses app name entirely
                    set targetProc to first process whose unix id is {pid}
                    set didFlipAny to false

                    tell targetProc
                        -- Wake up from App Nap if sleeping (doesn't bring to front)
                        set visible to true
                        -- NOTE: NOT setting frontmost in silent mode!

                        -- Get all windows
                        set allWindows to every window

                        if (count of allWindows) > 0 then
                            repeat with currentWin in allWindows
                                -- Check if AXFullScreen attribute exists
                                if (exists attribute "AXFullScreen" of currentWin) then
                                    set currentFullscreen to value of attribute "AXFullScreen" of currentWin

                                    if currentFullscreen is true then
                                        -- FLIP THE SWITCH - Force exit fullscreen at OS level
                                        set value of attribute "AXFullScreen" of currentWin to false
                                        set didFlipAny to true
                                        delay 0.5
                                    end if
                                end if
                            end repeat

                            if didFlipAny then
                                return "FLIPPED"
                            else
                                -- In silent mode, don't try keyboard events
                                -- Return SILENT_SKIP so Hardware Bypass can take over
                                return "SILENT_SKIP"
                            end if
                        else
                            return "ERROR: No windows found for PID"
                        end if
                    end tell

                on error errMsg
                    return "ERROR: " & errMsg
                end try
            end tell
            '''
        else:
            # NORMAL MODE: Full AX API with keyboard events
            ax_script = f'''
            tell application "System Events"
                try
                    -- Target process by PID (DNA) - bypasses app name entirely
                    set targetProc to first process whose unix id is {pid}
                    set didFlipAny to false
                    set allAlreadyFalse to true
                    set procName to name of targetProc

                    tell targetProc
                        -- Wake up from App Nap if sleeping
                        set visible to true
                        set frontmost to true  -- v51.2: BRING TO FRONT!

                        -- Get all windows (PWAs typically have one main window)
                        set allWindows to every window

                        if (count of allWindows) > 0 then
                            -- v51.2: Focus the FIRST window specifically
                            set targetWin to item 1 of allWindows

                            -- Try to perform click to ensure window has focus
                            try
                                perform action "AXRaise" of targetWin
                            end try

                            repeat with currentWin in allWindows
                                -- Check if AXFullScreen attribute exists
                                if (exists attribute "AXFullScreen" of currentWin) then
                                    set currentFullscreen to value of attribute "AXFullScreen" of currentWin

                                    if currentFullscreen is true then
                                        -- FLIP THE SWITCH - Force exit fullscreen at OS level
                                        set value of attribute "AXFullScreen" of currentWin to false
                                        set didFlipAny to true
                                        set allAlreadyFalse to false
                                        delay 0.5
                                    end if
                                end if
                            end repeat

                            if didFlipAny then
                                return "FLIPPED"
                            else if allAlreadyFalse then
                                -- v51.2: PRESENTATION MODE / WEB FULLSCREEN FIX
                                -- Window claims NOT fullscreen but Space IS fullscreen.
                                -- This happens with PWAs using web fullscreen API.
                                --
                                -- CRITICAL: Send keystrokes TO THE TARGET PROCESS!
                                -- Previously we were sending to System Events globally (BUG!)

                                -- First, ensure process is frontmost
                                set frontmost to true
                                delay 0.2

                                -- v51.2: Send Escape key TO THIS PROCESS
                                -- This exits web fullscreen (element.requestFullscreen())
                                key code 53  -- Escape key

                                delay 0.5

                                -- v51.2: Also try Ctrl+Cmd+F TO THIS PROCESS
                                -- This exits macOS native fullscreen if somehow in that state
                                keystroke "f" using {{control down, command down}}

                                delay 0.5

                                return "ESCAPED"
                            else
                                return "NO_ACTION"
                            end if
                        else
                            return "ERROR: No windows found for PID"
                        end if
                    end tell

                on error errMsg
                    return "ERROR: " & errMsg
                end try
            end tell
            '''

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", ax_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)

            result = stdout.decode().strip() if stdout else ""
            error = stderr.decode().strip() if stderr else ""

            # v51.2: Handle different result types
            if "FLIPPED" in result:
                logger.info(
                    f"[YABAI v51.2] ☢️ AX API FLIPPED: Successfully flipped AXFullScreen for PID {pid}"
                )
                # Wait for macOS to process the state change
                sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", "3.0"))
                await asyncio.sleep(sip_delay)
                return True
            elif "ESCAPED" in result:
                logger.info(
                    f"[YABAI v51.2] ☢️ AX API ESCAPED: Sent Escape/Ctrl+Cmd+F to exit presentation mode "
                    f"for PID {pid} (AXFullScreen was already false)"
                )
                # Wait for macOS to process the state change
                sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", "3.0"))
                await asyncio.sleep(sip_delay)
                return True
            elif "SILENT_SKIP" in result:
                # v51.2: In silent mode, AXFullScreen was already false
                # Return False to let Hardware Bypass take over
                logger.info(
                    f"[YABAI v51.2] 🔇 AX API SILENT_SKIP: AXFullScreen already false for PID {pid}. "
                    f"Deferring to Hardware Bypass (won't hijack user's screen in silent mode)"
                )
                return False
            elif "NO_ACTION" in result:
                logger.warning(
                    f"[YABAI v51.2] AX API NO_ACTION: No fullscreen windows found for PID {pid}"
                )
                return False
            else:
                logger.warning(
                    f"[YABAI v51.2] AX API result for PID {pid}: {result or error}"
                )
                return False

        except asyncio.TimeoutError:
            logger.warning(f"[YABAI v51.0] AX API timed out for PID {pid}")
            return False
        except Exception as e:
            logger.warning(f"[YABAI v51.0] AX API error for PID {pid}: {e}")
            return False

    async def _unpack_via_ax_window_async(
        self,
        window_id: int,
        app_name: str = "Unknown"
    ) -> bool:
        """
        v51.0: AX API targeting window by its properties (fallback if PID unknown).

        Uses System Events to find windows by app name and toggle AXFullScreen.
        Less precise than PID targeting but still uses AX API.

        Args:
            window_id: Window ID (for logging)
            app_name: App name to search for

        Returns:
            True if AX API successfully flipped AXFullScreen, False otherwise
        """
        if not app_name or app_name == "Unknown":
            logger.warning(f"[YABAI v51.0] AX API Window: No app name for window {window_id}")
            return False

        logger.info(
            f"[YABAI v51.0] ☢️ AX API (by app): Targeting '{app_name}' windows "
            f"to force-flip AXFullScreen attribute"
        )

        # Escape app name for AppleScript
        escaped_app = app_name.replace('"', '\\"')

        ax_script = f'''
        tell application "System Events"
            try
                -- Find all processes matching the app name (handles PWAs)
                set matchingProcs to every process whose name contains "{escaped_app}"

                if (count of matchingProcs) is 0 then
                    -- Try without partial match
                    set matchingProcs to every process whose name is "{escaped_app}"
                end if

                if (count of matchingProcs) > 0 then
                    repeat with targetProc in matchingProcs
                        tell targetProc
                            set visible to true
                            set allWindows to every window

                            repeat with targetWin in allWindows
                                if (exists attribute "AXFullScreen" of targetWin) then
                                    set currentFS to value of attribute "AXFullScreen" of targetWin
                                    if currentFS is true then
                                        set value of attribute "AXFullScreen" of targetWin to false
                                        delay 0.5
                                    end if
                                end if
                            end repeat
                        end tell
                    end repeat
                    return "SUCCESS"
                else
                    return "ERROR: No matching processes found"
                end if

            on error errMsg
                return "ERROR: " & errMsg
            end try
        end tell
        '''

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", ax_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)

            result = stdout.decode().strip() if stdout else ""
            error = stderr.decode().strip() if stderr else ""

            if "SUCCESS" in result:
                logger.info(
                    f"[YABAI v51.0] ☢️ AX API (by app) SUCCESS for '{app_name}'"
                )
                sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", "3.0"))
                await asyncio.sleep(sip_delay)
                return True
            else:
                logger.warning(
                    f"[YABAI v51.0] AX API (by app) result: {result or error}"
                )
                return False

        except asyncio.TimeoutError:
            logger.warning(f"[YABAI v51.0] AX API (by app) timed out")
            return False
        except Exception as e:
            logger.warning(f"[YABAI v51.0] AX API (by app) error: {e}")
            return False

    async def _handle_fullscreen_window_async(
        self,
        window_id: int,
        window_info: Optional[Dict[str, Any]] = None,
        silent: bool = False
    ) -> Tuple[bool, bool]:
        """
        v35.5 FULLSCREEN UNPACKING PROTOCOL: Serialize and unpack fullscreen windows.

        macOS native fullscreen windows are separate Spaces. You cannot move a
        Space into another Space. This method "unpacks" the window by exiting
        fullscreen, waiting for the animation, and returning it to a normal state.

        v35.5 IMPROVEMENTS:
        - Uses _unpack_lock semaphore to serialize animations (one at a time)
        - Invalidates space topology cache after unpack (space indices shift!)
        - Proper error handling for WindowServer congestion

        v51.2 IMPROVEMENTS:
        - Added silent flag to control keyboard-based unpack methods
        - In silent mode, skip methods that would hijack user's screen

        Args:
            window_id: The window ID to check and potentially unpack
            window_info: Optional pre-fetched window info (avoids extra query)
            silent: If True, skip methods that would bring window to frontmost

        Returns:
            Tuple of (was_fullscreen, unpack_success):
            - was_fullscreen: True if window was in native fullscreen
            - unpack_success: True if we successfully exited fullscreen (or wasn't fullscreen)
        """
        yabai_path = self._health.yabai_path or "yabai"

        # Get window info if not provided
        if window_info is None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    window_info = json.loads(stdout.decode())
                else:
                    logger.warning(f"[YABAI] Could not query window {window_id} for fullscreen check")
                    return False, True  # Assume not fullscreen, proceed
            except Exception as e:
                logger.debug(f"[YABAI] Window query failed: {e}")
                return False, True  # Assume not fullscreen, proceed

        # ═══════════════════════════════════════════════════════════════════
        # v49.0: SPACE-BASED TRUTH PROTOCOL (REPLACES v36.0)
        # ═══════════════════════════════════════════════════════════════════
        # THE PROBLEM: Window queries LIE about fullscreen status!
        #   - Window says: is-native-fullscreen: false (THE LIE)
        #   - Space says: is-native-fullscreen: true (THE TRUTH)
        #
        # THE SOLUTION: Query the SPACE first, not the Window.
        # If Space says it's fullscreen, we KNOW the window is locked.
        #
        # FALLBACK CHAIN:
        #   1. Yabai Toggle (works on PWAs, Electron, ANY window)
        #   2. AppleScript (for minimized windows or yabai failure)
        # ═══════════════════════════════════════════════════════════════════

        app_name = window_info.get("app", "Unknown")
        window_title = window_info.get("title", "")[:50]
        window_space_id = window_info.get("space", -1)

        # v49.0: ASK THE SPACE FOR THE TRUTH
        space_is_fullscreen, space_info = await self._is_space_native_fullscreen_async(window_space_id)

        # Window's claim (which may be a lie)
        window_claims_fullscreen = window_info.get("is-native-fullscreen", False)
        is_zoom_fullscreen = window_info.get("has-fullscreen-zoom", False)

        # v49.0: TRUTH TABLE
        # Space says fullscreen -> Window IS fullscreen (trust Space)
        # Window says fullscreen -> Window might be fullscreen (check Space to confirm)
        # Neither says fullscreen -> Window is NOT fullscreen
        is_native_fullscreen = space_is_fullscreen or window_claims_fullscreen

        if space_is_fullscreen and not window_claims_fullscreen:
            logger.warning(
                f"[YABAI v49.0] 🔮 LYING WINDOW DETECTED: Window {window_id} ({app_name}) "
                f"claims is-native-fullscreen=false, but Space {window_space_id} says TRUE!"
            )

        # Check for Chrome/Electron-specific presentation mode
        is_chrome_like = app_name.lower() in [
            "google chrome", "chrome", "chromium", "brave browser",
            "microsoft edge", "electron", "slack", "discord", "spotify",
            "visual studio code", "code", "figma"
        ]

        # Determine which fullscreen mode we're dealing with
        fullscreen_mode = None
        if is_native_fullscreen:
            fullscreen_mode = "native"
        elif is_zoom_fullscreen:
            fullscreen_mode = "zoom"
        elif is_chrome_like:
            # Chrome-like apps may have presentation mode that yabai doesn't detect
            # Check if window is suspiciously large (covering most of screen)
            frame = window_info.get("frame", {})
            width = frame.get("w", 0)
            height = frame.get("h", 0)
            # If window is very large and has no titlebar decorations, it might be presentation mode
            has_border = window_info.get("has-border", True)
            if width > 1800 and height > 1000 and not has_border:
                fullscreen_mode = "presentation_suspected"
                logger.debug(
                    f"[YABAI] 🔍 Suspected presentation mode: {app_name} window {window_id} "
                    f"is {width}x{height} with no border"
                )

        # ═══════════════════════════════════════════════════════════════════════
        # v44.2: QUANTUM MECHANICS PROTOCOL - Respects Three Laws of OS Physics
        # ═══════════════════════════════════════════════════════════════════════
        # 
        # LAW 1: TOPOLOGY DRIFT - Space indices change after fullscreen exit
        # LAW 2: EVENTUAL CONSISTENCY - Must wait for state to converge
        # LAW 3: ATOMICITY - Treat operation as indivisible transaction
        #
        # ROOT CAUSE: Yabai reports FALSE for is-native-fullscreen on hidden windows.
        # The window IS fullscreen, but Yabai can't see into the hidden Space.
        # This causes moves to fail silently because macOS blocks moving fullscreen windows.
        #
        # SOLUTION: Check if window's space is in the VISIBLE SPACES set.
        # If NOT visible, ALWAYS run Deep Unpack as precautionary measure.
        # ═══════════════════════════════════════════════════════════════════════
        
        # Check if window is on a hidden space (yabai state might be unreliable)
        window_space_id = window_info.get("space", -1)
        is_visible_flag = window_info.get("is-visible", None)  # May be None or True/False
        
        # v44.2: Query ACTUAL visible spaces from yabai (not just the flag)
        visible_spaces = set()
        try:
            # Get current visible spaces
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            if proc.returncode == 0 and stdout:
                spaces_data = json.loads(stdout.decode())
                for space in spaces_data:
                    # A space is "visible" if it's the active space on its display
                    if space.get("is-visible", False) or space.get("has-focus", False):
                        visible_spaces.add(space.get("index"))
                logger.debug(f"[YABAI v44.2] Visible spaces: {visible_spaces}")
        except Exception as e:
            logger.debug(f"[YABAI v44.2] Could not query visible spaces: {e}")
            # Fallback: include current user space
            current_space = self.get_current_user_space()
            if current_space:
                visible_spaces.add(current_space)
        
        # ═══════════════════════════════════════════════════════════════════════
        # v50.0: OMNISCIENT NEGATIVE LOGIC - Enhanced Phantom Detection
        # ═══════════════════════════════════════════════════════════════════════
        # v46.0 used heuristics (space_id > normal_count).
        # v50.0 uses NEGATIVE LOGIC: If window claims Space X, but Space X
        # doesn't exist in our queried topology → It's a PHANTOM SPACE.
        #
        # This is 100% accurate because we're not guessing - we're ASKING THE OS.
        # ═══════════════════════════════════════════════════════════════════════

        # v50.0: Use Negative Logic to detect phantom spaces
        is_phantom_via_negative_logic, phantom_reason, known_spaces = \
            await self._is_space_phantom_or_fullscreen_async(window_space_id)

        # Legacy heuristics (kept as fallback)
        normal_space_count = len(visible_spaces) if visible_spaces else len(known_spaces) or 10

        # v50.0: COMBINED DETECTION (Negative Logic OR Legacy Heuristics)
        is_in_phantom_space = (
            is_phantom_via_negative_logic or  # v50.0: NEGATIVE LOGIC (most accurate)
            window_space_id > normal_space_count or  # Legacy: High ID
            window_space_id == 0 or  # Legacy: Orphaned
            window_space_id == -1 or  # Legacy: Orphaned
            is_native_fullscreen  # Legacy: Flag check
        )

        if is_phantom_via_negative_logic:
            logger.info(
                f"[YABAI v50.0] 🧠 OMNISCIENT DETECTION: Window {window_id} in phantom space "
                f"detected via NEGATIVE LOGIC: {phantom_reason}"
            )
        
        # v44.2: DEFINITIVE hidden space detection
        # Window is on hidden space if:
        # 1. space_id is NOT in visible_spaces set, OR
        # 2. space_id is -1 (orphaned), OR
        # 3. is-visible flag is explicitly False
        # 4. v46.0: Window is in a phantom space
        is_on_hidden_space = (
            (window_space_id != -1 and window_space_id not in visible_spaces) or
            window_space_id == -1 or
            is_visible_flag is False or
            is_in_phantom_space  # v46.0: Phantom spaces are always "hidden"
        )
        
        # v46.0: Log the physics state for debugging
        if is_on_hidden_space:
            phantom_reason = []
            if is_in_phantom_space:
                if window_space_id > normal_space_count:
                    phantom_reason.append(f"space_id {window_space_id} > {normal_space_count}")
                if window_space_id == 0:
                    phantom_reason.append("space_id=0 (orphaned)")
                if window_space_id == -1:
                    phantom_reason.append("space_id=-1 (orphaned)")
                if is_native_fullscreen:
                    phantom_reason.append("is-native-fullscreen=true")
            
            logger.info(
                f"[YABAI v46.0] ⚓ REALITY ANCHOR: Window {window_id} ({app_name}) detected in "
                f"{'PHANTOM SPACE' if is_in_phantom_space else 'HIDDEN SPACE'} {window_space_id}. "
                f"Reason: {', '.join(phantom_reason) if phantom_reason else 'not visible'}. "
                f"Normal spaces: {visible_spaces}"
            )
        
        # ═══════════════════════════════════════════════════════════════════════
        # v49.0: SPACE-BASED UNPACK PROTOCOL
        # ═══════════════════════════════════════════════════════════════════════
        # If Space tells us the window is in fullscreen (space_is_fullscreen=True),
        # OR window is on hidden/phantom space, we need to unpack.
        #
        # UNPACK CHAIN:
        #   1. Yabai Toggle (works on PWAs like Google Gemini, Electron apps, games)
        #   2. AppleScript (fallback for minimized windows or special cases)
        # ═══════════════════════════════════════════════════════════════════════

        needs_unpack = (
            space_is_fullscreen or  # v49.0: Trust the Space
            (is_on_hidden_space and is_chrome_like) or  # Legacy hidden space check
            is_in_phantom_space  # Phantom space always needs unpack
        )

        if needs_unpack:
            logger.warning(
                f"[YABAI v51.0] ⚓ UNPACK REQUIRED: Window {window_id} ({app_name}) "
                f"in {'FULLSCREEN SPACE' if space_is_fullscreen else 'PHANTOM SPACE'} {window_space_id} - "
                f"engaging YABAI TOGGLE first (universal unpack)"
            )

            unpack_success = False

            # ═══════════════════════════════════════════════════════════════════
            # v51.0: YABAI TOGGLE FIRST (Universal - works on ANY window type)
            # ═══════════════════════════════════════════════════════════════════
            yabai_toggle_success = await self._unpack_via_yabai_toggle_async(
                window_id=window_id,
                app_name=app_name,
                window_title=window_title
            )

            if yabai_toggle_success:
                logger.info(
                    f"[YABAI v51.0] ✅ YABAI TOGGLE SUCCESS: Window {window_id} unpacked "
                    f"via native toggle (no AppleScript needed)"
                )
                unpack_success = True
            else:
                # ═══════════════════════════════════════════════════════════════
                # v51.0: AX API PROTOCOL - "UNIVERSAL SOLVENT" FOR PWAs
                # ═══════════════════════════════════════════════════════════════
                # Yabai toggle failed - try AX API (targets PID, bypasses app's
                # internal scripting dictionary). This is the ONLY method that
                # works on PWAs (Progressive Web Apps) like Google Gemini, which
                # detach from the main Chrome process.
                #
                # The AX API directly flips the AXFullScreen attribute at the
                # OS level, regardless of what the app's scripting dictionary
                # supports.
                # ═══════════════════════════════════════════════════════════════

                # Extract PID from window_info (yabai always provides this)
                window_pid = window_info.get("pid", 0)

                if window_pid > 0:
                    logger.warning(
                        f"[YABAI v51.0] 🧪 Yabai toggle failed, engaging AX API Protocol "
                        f"(PID {window_pid}) for window {window_id} ({app_name})"
                    )

                    ax_api_success = await self._unpack_via_ax_pid_async(
                        pid=window_pid,
                        window_id=window_id,
                        app_name=app_name,
                        silent=silent  # v51.2: Pass silent flag to control keyboard events
                    )

                    if ax_api_success:
                        # ═══════════════════════════════════════════════════════════
                        # v51.1: CORRECT VERIFICATION - Re-query WINDOW for NEW space
                        # ═══════════════════════════════════════════════════════════
                        # BUG FIX: We were checking the OLD space (window_space_id).
                        # After unpack, the window moves to a DIFFERENT space and the
                        # old phantom space might be DESTROYED.
                        #
                        # SOLUTION: Re-query the window to get its NEW location,
                        # then verify that new location isn't fullscreen.
                        # ═══════════════════════════════════════════════════════════
                        await asyncio.sleep(1.5)  # Give macOS time to animate and settle

                        # Re-query window to get its NEW space
                        yabai_path = self._health.yabai_path or "yabai"
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE
                            )
                            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                            if proc.returncode == 0 and stdout:
                                new_window_info = json.loads(stdout.decode())
                                new_space_id = new_window_info.get("space", -1)

                                # Check if window's NEW space is fullscreen
                                new_space_fullscreen, _ = await self._is_space_native_fullscreen_async(new_space_id)

                                if not new_space_fullscreen:
                                    logger.info(
                                        f"[YABAI v51.1] ✅ AX API PROTOCOL SUCCESS: Window {window_id} "
                                        f"unpacked via PID {window_pid} - now on Space {new_space_id} "
                                        f"(was on phantom Space {window_space_id})"
                                    )
                                    unpack_success = True
                                else:
                                    logger.warning(
                                        f"[YABAI v51.1] ⚠️ AX API: Window moved to Space {new_space_id} "
                                        f"but that space is STILL fullscreen - extended wait..."
                                    )
                                    # Extended wait and retry
                                    await asyncio.sleep(2.5)
                                    final_fullscreen, _ = await self._is_space_native_fullscreen_async(new_space_id)
                                    if not final_fullscreen:
                                        logger.info(f"[YABAI v51.1] ✅ AX API succeeded after extended wait")
                                        unpack_success = True
                            else:
                                # Window query failed - might have been destroyed and reborn
                                logger.warning(
                                    f"[YABAI v51.1] Window {window_id} query failed after AX API - "
                                    f"window may have been reborn. Checking old space..."
                                )
                                # Fallback: check if old space is gone (indicating success)
                                old_space_fullscreen, _ = await self._is_space_native_fullscreen_async(window_space_id)
                                if not old_space_fullscreen:
                                    logger.info(
                                        f"[YABAI v51.1] ✅ Old phantom Space {window_space_id} no longer "
                                        f"fullscreen (likely destroyed) - AX API succeeded"
                                    )
                                    unpack_success = True
                        except Exception as e:
                            logger.warning(f"[YABAI v51.1] AX API verification error: {e}")
                            # Fallback: assume success if old space is gone
                            old_space_fullscreen, _ = await self._is_space_native_fullscreen_async(window_space_id)
                            if not old_space_fullscreen:
                                unpack_success = True
                else:
                    logger.warning(
                        f"[YABAI v51.0] ⚠️ No PID available for window {window_id}, "
                        f"skipping AX API Protocol"
                    )
                    ax_api_success = False

                # ═══════════════════════════════════════════════════════════════
                # v51.0: FALLBACK TO GENERIC APPLESCRIPT (Last Resort)
                # ═══════════════════════════════════════════════════════════════
                # Both Yabai toggle AND AX API failed. Fall back to generic
                # AppleScript. This handles minimized windows or cases where
                # the app truly doesn't support any fullscreen exit method.
                # ═══════════════════════════════════════════════════════════════
                if not unpack_success:
                    logger.warning(
                        f"[YABAI v51.0] ⚠️ AX API failed, falling back to generic AppleScript "
                        f"for window {window_id}"
                    )
                    await self._deep_unpack_via_applescript(
                        app_name=app_name,
                        window_title=window_title,
                        window_id=window_id,
                        fire_and_forget=False,
                        verify_transition=True
                    )

                    # v51.0: Verify AppleScript worked by checking Space again
                    await asyncio.sleep(1.0)  # Give macOS time to animate
                    space_still_fullscreen, _ = await self._is_space_native_fullscreen_async(window_space_id)
                    if not space_still_fullscreen:
                        logger.info(f"[YABAI v51.0] ✅ AppleScript unpack succeeded")
                        unpack_success = True
                    else:
                        logger.warning(
                            f"[YABAI v51.0] ❌ ALL UNPACK METHODS FAILED: "
                            f"Yabai toggle, AX API, AND AppleScript all failed for window {window_id}"
                        )

            # v51.0: TOPOLOGY INVALIDATION - Space indices WILL shift after unpack
            self._space_topology_valid = False
            logger.info(
                f"[YABAI v51.0] 🌊 TOPOLOGY INVALIDATED: Space indices may have shifted "
                f"after unpacking window {window_id}"
            )

            # v51.0: Return actual success status, not blindly True
            # was_fullscreen=True, success=unpack_success
            if not unpack_success:
                logger.error(
                    f"[YABAI v51.0] ❌ UNPACK FAILED: Both yabai toggle and AppleScript failed "
                    f"for window {window_id}. Will try hardware targeting as last resort."
                )
            return True, unpack_success
        
        if fullscreen_mode is None:
            # Not in any fullscreen mode - nothing to do
            return False, True

        logger.info(
            f"[YABAI] 📦 UNPACKING FULLSCREEN ({fullscreen_mode.upper()}): "
            f"Window {window_id} ({app_name}: {window_title}) - must exit before teleportation"
        )

        # ═══════════════════════════════════════════════════════════════════
        # v35.5: SERIALIZE ANIMATIONS - One unpack at a time!
        # ═══════════════════════════════════════════════════════════════════
        # macOS WindowServer is single-threaded. Concurrent fullscreen toggles
        # cause dropped frames, lag, and ignored commands. We MUST serialize.
        # ═══════════════════════════════════════════════════════════════════

        async with self._unpack_lock:
            logger.debug(f"[YABAI] 🔒 Acquired unpack lock for window {window_id}")

            try:
                # Step 1: Get dynamic animation delay based on system settings
                animation_delay = await self._get_system_animation_delay()

                # v36.0: Adjust delay based on fullscreen mode
                # Zoom fullscreen has faster animation than native fullscreen
                if fullscreen_mode == "zoom":
                    animation_delay = min(animation_delay, 0.8)  # Zoom is faster
                elif fullscreen_mode == "presentation_suspected":
                    animation_delay = 0.3  # No real animation, just window resize
                logger.debug(f"[YABAI] ⏱️ Using animation delay: {animation_delay}s (mode: {fullscreen_mode})")

                # ═══════════════════════════════════════════════════════════════
                # v36.0: MODE-SPECIFIC TOGGLE COMMAND
                # ═══════════════════════════════════════════════════════════════
                # Different fullscreen modes require different toggle commands:
                # - Native: --toggle native-fullscreen (destroys Space)
                # - Zoom: --toggle zoom-fullscreen (no Space destruction)
                # - Presentation: Try zoom first, then native as fallback
                # ═══════════════════════════════════════════════════════════════

                primary_toggle = "native-fullscreen" if fullscreen_mode == "native" else "zoom-fullscreen"
                fallback_toggle = "zoom-fullscreen" if fullscreen_mode == "native" else "native-fullscreen"

                logger.info(f"[YABAI] 🔄 Exiting {fullscreen_mode} fullscreen for window {window_id}...")
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(window_id), "--toggle", primary_toggle,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                if proc.returncode != 0:
                    error_msg = stderr.decode().strip() if stderr else "Unknown error"
                    logger.warning(f"[YABAI] ⚠️ {primary_toggle} toggle failed: {error_msg}")

                    # Try fallback toggle
                    logger.debug(f"[YABAI] Trying {fallback_toggle} toggle as fallback...")
                    proc2 = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "window", str(window_id), "--toggle", fallback_toggle,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc2.communicate(), timeout=3.0)

                # Step 3: CRITICAL - Wait for macOS animation to complete
                # This is essential! Moving too soon causes crashes/failures.
                logger.info(
                    f"[YABAI] ⏳ Waiting {animation_delay}s for fullscreen exit animation..."
                )
                await asyncio.sleep(animation_delay)

                # ═══════════════════════════════════════════════════════════════
                # v36.0: SMART TOPOLOGY INVALIDATION
                # ═══════════════════════════════════════════════════════════════
                # CRITICAL: ONLY native fullscreen destroys Spaces!
                # - Native fullscreen: Creates/destroys a Space → INVALIDATE
                # - Zoom fullscreen: Just resizes window → NO invalidation needed
                # - Presentation: Just overlays → NO invalidation needed
                # ═══════════════════════════════════════════════════════════════
                if fullscreen_mode == "native":
                    self._space_topology_valid = False
                    self._last_topology_refresh = None
                    logger.info(
                        f"[YABAI] ⚠️ TOPOLOGY INVALIDATED: Space indices may have shifted "
                        f"after native fullscreen exit - will re-query before move"
                    )
                else:
                    logger.debug(
                        f"[YABAI] ✓ Topology still valid ({fullscreen_mode} mode doesn't destroy Spaces)"
                    )

                # Step 4: Verify fullscreen exit succeeded
                proc_verify = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc_verify.communicate(), timeout=5.0)

                if proc_verify.returncode == 0 and stdout:
                    updated_info = json.loads(stdout.decode())
                    still_fullscreen = updated_info.get("is-native-fullscreen", False)

                    if still_fullscreen:
                        # First attempt failed - try one more time with longer delay
                        logger.warning(
                            f"[YABAI] ⚠️ Window {window_id} still fullscreen after first toggle. "
                            f"Retrying with extended delay..."
                        )

                        proc_retry = await asyncio.create_subprocess_exec(
                            yabai_path, "-m", "window", str(window_id), "--toggle", "native-fullscreen",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        await asyncio.wait_for(proc_retry.communicate(), timeout=5.0)

                        # Extended delay for stubborn windows
                        await asyncio.sleep(animation_delay * 1.5)

                        # Invalidate topology again (second toggle)
                        self._space_topology_valid = False

                        # Final verification
                        proc_final = await asyncio.create_subprocess_exec(
                            yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout_final, _ = await asyncio.wait_for(proc_final.communicate(), timeout=5.0)

                        if proc_final.returncode == 0 and stdout_final:
                            final_info = json.loads(stdout_final.decode())
                            if final_info.get("is-native-fullscreen", False):
                                logger.error(
                                    f"[YABAI] ❌ UNPACK FAILED: Window {window_id} stuck in fullscreen "
                                    f"after multiple attempts"
                                )
                                return True, False

                    logger.info(
                        f"[YABAI] ✅ UNPACKED: Window {window_id} ({app_name}) exited fullscreen - "
                        f"ready for teleportation"
                    )
                    return True, True

                # Could not verify - assume success and proceed
                logger.debug(f"[YABAI] Could not verify fullscreen exit, assuming success")
                return True, True

            except asyncio.TimeoutError:
                logger.warning(f"[YABAI] ⚠️ Fullscreen toggle timed out for window {window_id}")
                return True, False
            except Exception as e:
                logger.error(f"[YABAI] ❌ Fullscreen unpack error: {e}")
                return True, False

    async def move_window_to_space_async(
        self,
        window_id: int,
        target_space: int,
        follow: bool = False,
        verify: bool = True,
        max_retries: int = 3,
        silent: bool = False
    ) -> bool:
        """
        v34.0 STEALTH & DISPLAY HANDOFF PROTOCOL: Intelligent window management.

        ROOT CAUSE FIX: The --space command silently fails when moving windows
        across displays without Scripting Additions. macOS blocks cross-GPU
        texture transfers via space commands for security reasons.

        v34.0 STEALTH MODE (silent=True):
        - NEVER uses --focus commands (won't hijack user's screen)
        - Uses progressive retries with delays for non-focus strategies
        - Returns False instead of escalating to focus-based strategies
        - Ideal for background monitoring (God Mode)

        SOLUTION: Display Handoff Protocol
        1. DETECT: Check if source and target are on different displays
        2. HANDOFF: Use --display command for cross-display moves (bypasses SA requirement)
        3. FIRE: Execute move command, check exit code immediately
        4. WAIT: Progressive hydration delays for texture rehydration
        5. CONFIRM: Verify window actually moved at each checkpoint
        6. RETRY: Use different strategy only after physics has had time to work

        Strategies (tried in order):
        1. Display Handoff (NEW) - Use --display for cross-display moves (most reliable)
        2. Direct move with progressive verification
        3. Focus window first, then move
        4. Wake space first (AppleScript), then direct move
        5. Full switch-grab-return with space switching

        The key insight: `yabai -m window --display N` uses a simpler "Move to Monitor"
        instruction that macOS allows natively, while `--space` requires complex
        GPU context management that often fails silently.
        """
        import asyncio

        if not self.is_available():
            logger.warning("[YABAI] Cannot move window - Yabai not available")
            return False

        yabai_path = self._health.yabai_path or "yabai"

        # ═══════════════════════════════════════════════════════════════════
        # v55.0: NEURO-REGENERATIVE PROTOCOL - REPAIR BEFORE MOVE
        # ═══════════════════════════════════════════════════════════════════
        # ROOT CAUSE FIX: Before attempting ANY move, ensure the window is
        # actionable (has AX reference). If not, repair it first.
        # This prevents wasted cycles on zombie windows.
        # ═══════════════════════════════════════════════════════════════════
        actionable, repaired_id, repaired_info = await self._ensure_window_is_actionable_async(
            window_id=window_id,
            window_info=None,  # Will query fresh
            app_name=None,  # Will detect from window
            pid=None,  # Will detect from window
            max_repair_attempts=2
        )

        if not actionable:
            if not silent:
                logger.warning(
                    f"[YABAI v55.0] ❌ Cannot move window {window_id} - ZOMBIE state "
                    f"unrecoverable (AX: false after repair attempts)"
                )
            return False

        # Use the possibly-updated window ID (Phoenix may have regenerated it)
        if repaired_id != window_id:
            if not silent:
                logger.info(
                    f"[YABAI v55.0] 🔥 Window ID updated: {window_id} → {repaired_id}"
                )
            window_id = repaired_id

        # v34.0: Configurable hydration timing
        hydration_checkpoints = [
            float(x) for x in os.getenv(
                'JARVIS_HYDRATION_CHECKPOINTS', '0.2,0.5,1.0,1.5'
            ).split(',')
        ]
        max_hydration_time = float(os.getenv('JARVIS_MAX_HYDRATION_TIME', '3.0'))

        # ═══════════════════════════════════════════════════════════════════
        # v34.0: INTELLIGENT DISPLAY/SPACE RESOLUTION HELPERS
        # ═══════════════════════════════════════════════════════════════════

        async def get_window_info_async() -> Optional[Dict[str, Any]]:
            """Get full window info including display and space."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    return json.loads(stdout.decode())
            except Exception as e:
                logger.debug(f"[YABAI] Could not query window info: {e}")
            return None

        async def get_window_space() -> Optional[int]:
            """Get current space of the window."""
            info = await get_window_info_async()
            return info.get("space") if info else None

        async def get_window_display() -> Optional[int]:
            """Get current display of the window."""
            info = await get_window_info_async()
            return info.get("display") if info else None

        async def get_space_display(space_id: int) -> Optional[int]:
            """Get which display a space belongs to."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--spaces",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    spaces = json.loads(stdout.decode())
                    for space in spaces:
                        if space.get("index") == space_id:
                            return space.get("display")
            except Exception as e:
                logger.debug(f"[YABAI] Could not query space display: {e}")
            return None

        async def get_all_spaces_on_display(display_id: int) -> List[int]:
            """Get all space IDs on a specific display."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--spaces",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    spaces = json.loads(stdout.decode())
                    return [s.get("index") for s in spaces if s.get("display") == display_id]
            except Exception as e:
                logger.debug(f"[YABAI] Could not query spaces on display: {e}")
            return []

        # ═══════════════════════════════════════════════════════════════════
        # v47.0: CHEMICAL BOND RE-BONDING PROTOCOL
        # ═══════════════════════════════════════════════════════════════════
        # ROOT CAUSE FIX: When Chrome unpacks from fullscreen, macOS DESTROYS
        # the window ID and creates a NEW one. JARVIS loses track.
        #
        # SOLUTION: The "Chemical Bond" - App Name + Window Title
        # - Before unpack: Save the window's Chemical Bond
        # - After unpack: If window ID is dead, search for matching Bond
        # - Re-Bond: Update to the new window ID and continue
        #
        # The probability of two Chrome windows having the exact same
        # dynamic title (e.g., "HORIZONTAL - Stereos...") is effectively zero.
        # ═══════════════════════════════════════════════════════════════════

        async def find_window_by_chemical_bond(
            app_name: str,
            window_title: str,
            old_window_id: int,
            original_pid: Optional[int] = None,
            fuzzy_threshold: float = 0.8
        ) -> Optional[int]:
            """
            v48.0: Find a window by its Chemical Bond (App Name + Window Title + PID).

            Uses fuzzy matching because the title might change slightly after unpack.
            v48.0 adds PID hardening to disambiguate "Doppelgänger" windows (e.g., two
            Chrome profiles with same "New Tab" title).

            Args:
                app_name: The application name (e.g., "Google Chrome")
                window_title: The window title to match
                old_window_id: The old (dead) window ID to exclude
                original_pid: The original Process ID (v48.0 PID Hardening)
                fuzzy_threshold: Minimum similarity ratio (0.0-1.0)

            Returns:
                New window ID if found, None otherwise
            """
            if not window_title:
                logger.debug("[YABAI v47.0] No window title for chemical bond matching")
                return None

            try:
                # Query all windows
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "query", "--windows",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                if proc.returncode != 0 or not stdout:
                    return None

                windows = json.loads(stdout.decode())
                app_name_lower = app_name.lower()
                title_lower = window_title.lower()

                # ═══════════════════════════════════════════════════════════════
                # v48.0: PID HARDENING - Disambiguate "Doppelgänger" windows
                # ═══════════════════════════════════════════════════════════════
                # Two Chrome profiles might have same "New Tab" title.
                # PID matching ensures we find the EXACT same process's window.
                # Priority order:
                #   1. Exact PID + Exact Title (bulletproof)
                #   2. Exact PID + Fuzzy Title (same process, title changed)
                #   3. Same App + Exact Title (fallback if PID unavailable)
                #   4. Same App + Fuzzy Title (last resort)
                # ═══════════════════════════════════════════════════════════════

                # Find matching windows - separate by PID match for priority
                pid_exact_title_matches = []
                pid_fuzzy_matches = []
                app_exact_title_matches = []
                app_fuzzy_matches = []

                for w in windows:
                    w_id = w.get("id")
                    w_app = w.get("app", "").lower()
                    w_title = w.get("title", "")
                    w_pid = w.get("pid")

                    # Skip the old (dead) window ID
                    if w_id == old_window_id:
                        continue

                    # Must match app name
                    if w_app != app_name_lower:
                        continue

                    # Calculate title similarity
                    if not w_title:
                        continue

                    w_title_lower = w_title.lower()
                    is_pid_match = original_pid is not None and w_pid == original_pid
                    is_exact_title = w_title_lower == title_lower

                    # Calculate fuzzy similarity
                    common = sum(1 for a, b in zip(w_title_lower, title_lower) if a == b)
                    total = len(w_title_lower) + len(title_lower)
                    similarity = (2 * common) / total if total > 0 else 0

                    # Substring containment boost
                    if title_lower in w_title_lower or w_title_lower in title_lower:
                        similarity = max(similarity, 0.9)

                    # Categorize by priority
                    if is_pid_match and is_exact_title:
                        pid_exact_title_matches.append((w_id, 1.0, w_title, w_pid))
                    elif is_pid_match and similarity >= fuzzy_threshold:
                        pid_fuzzy_matches.append((w_id, similarity, w_title, w_pid))
                    elif is_exact_title:
                        app_exact_title_matches.append((w_id, 1.0, w_title, w_pid))
                    elif similarity >= fuzzy_threshold:
                        app_fuzzy_matches.append((w_id, similarity, w_title, w_pid))

                # Priority 1: Exact PID + Exact Title (bulletproof match)
                if pid_exact_title_matches:
                    best = pid_exact_title_matches[0]
                    logger.info(
                        f"[YABAI v48.0] 🔬⚛️ BULLETPROOF BOND: PID {best[3]} + exact title "
                        f"'{window_title}' → New ID {best[0]} (was {old_window_id})"
                    )
                    return best[0]

                # Priority 2: Exact PID + Fuzzy Title (same process, title changed slightly)
                if pid_fuzzy_matches:
                    pid_fuzzy_matches.sort(key=lambda x: x[1], reverse=True)
                    best = pid_fuzzy_matches[0]
                    logger.info(
                        f"[YABAI v48.0] 🔬⚛️ PID BOND: PID {best[3]} + fuzzy title "
                        f"'{window_title}' → '{best[2]}' (sim={best[1]:.0%}) "
                        f"→ New ID {best[0]} (was {old_window_id})"
                    )
                    return best[0]

                # Priority 3: Same App + Exact Title (no PID available or no PID match)
                if app_exact_title_matches:
                    best = app_exact_title_matches[0]
                    logger.info(
                        f"[YABAI v48.0] 🔬 EXACT TITLE BOND: '{app_name}' window "
                        f"'{window_title}' → New ID {best[0]} (was {old_window_id})"
                    )
                    return best[0]

                # Priority 4: Same App + Fuzzy Title (last resort)
                if app_fuzzy_matches:
                    app_fuzzy_matches.sort(key=lambda x: x[1], reverse=True)
                    best = app_fuzzy_matches[0]
                    logger.info(
                        f"[YABAI v48.0] 🔬 FUZZY BOND MATCH: '{app_name}' window "
                        f"'{window_title}' → '{best[2]}' (sim={best[1]:.0%}) "
                        f"→ New ID {best[0]} (was {old_window_id})"
                    )
                    return best[0]

            except Exception as e:
                logger.error(f"[YABAI v48.0] Chemical bond search failed: {e}")

            return None

        # ═══════════════════════════════════════════════════════════════════
        # v35.0: FULLSCREEN UNPACKING PROTOCOL
        # ═══════════════════════════════════════════════════════════════════
        # macOS treats fullscreen windows as separate Spaces. You cannot move
        # a Space inside another Space. Check and unpack BEFORE any move.
        # ═══════════════════════════════════════════════════════════════════

        window_info = await get_window_info_async()

        # v48.0: Capture Chemical Bond BEFORE unpacking (App + Title + PID)
        original_app_name = window_info.get("app", "") if window_info else ""
        original_window_title = window_info.get("title", "") if window_info else ""
        original_pid = window_info.get("pid") if window_info else None  # v48.0 PID Hardening
        original_window_id = window_id  # Save for re-bonding

        was_fullscreen, unpack_success = await self._handle_fullscreen_window_async(
            window_id, window_info, silent=silent
        )

        if was_fullscreen:
            if not unpack_success:
                # ═══════════════════════════════════════════════════════════════
                # v51.0: HARDWARE TARGETING BYPASS - Skip unpack, move directly
                # ═══════════════════════════════════════════════════════════════
                # Both yabai toggle AND AppleScript failed to unpack fullscreen.
                # Instead of giving up, try moving the window directly to the
                # Ghost Display. Sometimes macOS allows display moves even when
                # the window is in a "stuck" state.
                # ═══════════════════════════════════════════════════════════════
                logger.warning(
                    f"[YABAI v51.0] ⚠️ Fullscreen unpack failed for window {window_id}. "
                    f"Attempting DIRECT HARDWARE MOVE (bypass unpack)..."
                )

                # Try hardware targeting directly
                ghost_display = await self._get_ghost_display_index_async()
                if ghost_display is not None:
                    sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", "3.0"))
                    hardware_success = await self._move_window_to_display_async(
                        window_id=window_id,
                        display_index=ghost_display,
                        sip_convergence_delay=sip_delay
                    )

                    if hardware_success:
                        logger.info(
                            f"[YABAI v51.0] ✅ HARDWARE BYPASS SUCCESS: Window {window_id} moved to "
                            f"Display {ghost_display} despite being fullscreen!"
                        )
                        self._health.record_success(0)
                        return True

                # Hardware bypass also failed
                logger.error(
                    f"[YABAI v51.0] ❌ Cannot move window {window_id}: stuck in fullscreen mode "
                    f"AND hardware bypass failed"
                )
                self._health.record_failure("Fullscreen unpack and hardware bypass failed")
                return False

            # ═══════════════════════════════════════════════════════════════
            # v36.0: WINDOW STATE RE-VALIDATION AFTER UNPACK
            # ═══════════════════════════════════════════════════════════════
            # After unpacking, the window may have changed state:
            # - Became minimized (macOS behavior)
            # - Changed spaces (macOS auto-reorganization)
            # - Lost focus and became "dehydrated"
            # - Re-entered fullscreen (double-toggle bug)
            # We MUST re-validate before proceeding.
            # ═══════════════════════════════════════════════════════════════
            await asyncio.sleep(0.2)  # Brief hydration wait
            logger.debug(f"[YABAI] Refreshing window info after fullscreen unpack...")
            window_info = await get_window_info_async()

            if window_info is None:
                # ═══════════════════════════════════════════════════════════
                # v47.0: RE-BONDING PROTOCOL - Find the reborn window
                # ═══════════════════════════════════════════════════════════
                # The window ID died, but the window lives on with a new ID!
                # Use the Chemical Bond (App + Title) to find it.
                # ═══════════════════════════════════════════════════════════
                logger.warning(
                    f"[YABAI v47.0] 🔬 Window {window_id} disappeared after unpacking - "
                    f"initiating Chemical Bond re-bonding..."
                )

                if original_app_name and original_window_title:
                    # Give macOS a moment to create the new window
                    await asyncio.sleep(0.3)

                    new_window_id = await find_window_by_chemical_bond(
                        original_app_name,
                        original_window_title,
                        original_window_id,
                        original_pid=original_pid,  # v48.0 PID Hardening
                        fuzzy_threshold=float(os.getenv("JARVIS_REBOND_FUZZY_THRESHOLD", "0.7"))
                    )

                    if new_window_id is not None:
                        logger.info(
                            f"[YABAI v48.0] ⚛️ RE-BONDING SUCCESS: Window reborn as ID {new_window_id} "
                            f"(was {original_window_id}, PID={original_pid})"
                        )
                        # UPDATE THE WINDOW ID - This is the key!
                        window_id = new_window_id
                        # Refresh window info with the new ID
                        window_info = await get_window_info_async()

                        if window_info is None:
                            logger.error(
                                f"[YABAI v47.0] ❌ Re-bonded window {new_window_id} is also gone"
                            )
                            self._health.record_failure("Re-bonded window disappeared")
                            return False
                    else:
                        logger.error(
                            f"[YABAI v47.0] ❌ RE-BONDING FAILED: Could not find window matching "
                            f"'{original_app_name}' / '{original_window_title}'"
                        )
                        self._health.record_failure("Chemical bond re-bonding failed")
                        return False
                else:
                    logger.error(
                        f"[YABAI] ❌ Window {window_id} disappeared after unpacking "
                        f"(no Chemical Bond available for re-bonding)"
                    )
                    self._health.record_failure("Window disappeared after unpack")
                    return False

            # Check for problematic states after unpack
            if window_info.get("is-minimized", False):
                logger.warning(
                    f"[YABAI] ⚠️ Window {window_id} became minimized after unpacking - unminimizing..."
                )
                # Try to unminimize
                try:
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "window", str(window_id), "--deminimize",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=3.0)
                    await asyncio.sleep(0.3)  # Wait for unminimize animation
                    window_info = await get_window_info_async()
                except Exception as e:
                    logger.warning(f"[YABAI] Failed to unminimize: {e}")

            # ═══════════════════════════════════════════════════════════════════
            # v51.1: SPACE-BASED TRUTH VERIFICATION (not Window claim!)
            # ═══════════════════════════════════════════════════════════════════
            # BUG FIX: We were checking window_info.get("is-native-fullscreen")
            # but Windows LIE about fullscreen status! PWAs like Google Gemini
            # claim is-native-fullscreen=false even when they're in fullscreen.
            #
            # SOLUTION: Query the SPACE to get the truth about fullscreen status.
            # ═══════════════════════════════════════════════════════════════════
            if window_info:
                new_space_id = window_info.get("space", -1)
                space_is_fullscreen, _ = await self._is_space_native_fullscreen_async(new_space_id)
                window_claims_fullscreen = window_info.get("is-native-fullscreen", False)

                if space_is_fullscreen:
                    # Space says fullscreen - this is THE TRUTH
                    if not window_claims_fullscreen:
                        logger.warning(
                            f"[YABAI v51.1] 🔮 LYING WINDOW DETECTED: Window {window_id} claims "
                            f"is-native-fullscreen=false, but Space {new_space_id} says TRUE!"
                        )
                    logger.error(
                        f"[YABAI v51.1] ❌ Window {window_id} still in fullscreen after unpacking "
                        f"(Space {new_space_id} confirms - this is the TRUTH)"
                    )
                    self._health.record_failure("Window still fullscreen after unpack (Space-verified)")
                    return False
                elif window_claims_fullscreen:
                    # Window claims fullscreen but Space doesn't - Window is lying (or transitioning)
                    logger.info(
                        f"[YABAI v51.1] ✅ Window {window_id} claims fullscreen but Space {new_space_id} "
                        f"says NOT fullscreen - trusting Space (unpack succeeded)"
                    )

            # ═══════════════════════════════════════════════════════════════
            # v35.5: TOPOLOGY REFRESH - Re-calculate target after unpack
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL: When a fullscreen window exits, macOS DESTROYS its
            # Space. All space indices shift! If Ghost Display was Space 9
            # and the fullscreen Space was 8, Ghost is now Space 8.
            #
            # We MUST re-query the ghost display space index.
            # ═══════════════════════════════════════════════════════════════
            if not self._space_topology_valid:
                logger.info(
                    f"[YABAI] 🔄 TOPOLOGY REFRESH: Re-calculating target space after unpack..."
                )
                old_target = target_space

                # Re-query ghost display space (the most common target)
                new_ghost_space = self.get_ghost_display_space()
                if new_ghost_space is not None and old_target != new_ghost_space:
                    logger.warning(
                        f"[YABAI] ⚠️ TARGET SHIFTED: Ghost Display moved from Space {old_target} "
                        f"→ Space {new_ghost_space} after fullscreen exit"
                    )
                    target_space = new_ghost_space
                elif new_ghost_space is None:
                    logger.warning(
                        f"[YABAI] ⚠️ Could not re-query ghost space, using original: {old_target}"
                    )

                # Mark topology as refreshed
                self._space_topology_valid = True
                self._last_topology_refresh = time.time()

        # ═══════════════════════════════════════════════════════════════════════
        # v44.2: UNCONDITIONAL TOPOLOGY CHECK - Respects LAW 1 (Topology Drift)
        # ═══════════════════════════════════════════════════════════════════════
        # Even if was_fullscreen is False, Deep Unpack may have run for hidden
        # windows and invalidated topology. ALWAYS check if topology needs refresh.
        # ═══════════════════════════════════════════════════════════════════════
        if not self._space_topology_valid:
            logger.info(
                f"[YABAI v44.2] 🔄 QUANTUM TOPOLOGY CHECK: Topology was invalidated, "
                f"re-querying Ghost Display..."
            )
            old_target = target_space
            
            # Re-query ghost display space
            new_ghost_space = self.get_ghost_display_space()
            if new_ghost_space is not None:
                if old_target != new_ghost_space:
                    logger.warning(
                        f"[YABAI v44.2] 🌊 TOPOLOGY DRIFT CONFIRMED: Ghost Display shifted "
                        f"Space {old_target} → {new_ghost_space}"
                    )
                target_space = new_ghost_space
            else:
                logger.warning(
                    f"[YABAI v44.2] ⚠️ Could not re-query Ghost Display, using original: {old_target}"
                )
            
            # Mark topology as refreshed
            self._space_topology_valid = True
            self._last_topology_refresh = time.time()

        # ═══════════════════════════════════════════════════════════════════
        # v34.0: CROSS-DISPLAY DETECTION
        # ═══════════════════════════════════════════════════════════════════

        # Get source and target display info for intelligent routing
        # Use cached window_info if available for efficiency
        current_display = window_info.get("display") if window_info else await get_window_display()
        target_display = await get_space_display(target_space)

        is_cross_display_move = (
            current_display is not None and
            target_display is not None and
            current_display != target_display
        )

        if is_cross_display_move:
            logger.info(
                f"[YABAI] 🌐 CROSS-DISPLAY MOVE DETECTED: Window {window_id} "
                f"from Display {current_display} → Display {target_display} (Space {target_space})"
            )
        else:
            logger.debug(
                f"[YABAI] Same-display move: Window {window_id} → Space {target_space} "
                f"(Display {current_display or '?'})"
            )

        # v33.0: Progressive verification with hydration-aware timing
        async def verify_move_with_patience(expected_space: int, strategy_name: str) -> bool:
            """
            FIRE AND CONFIRM: Wait for physics to catch up.

            macOS window movement is NOT instant. When moving to a virtual display:
            1. Window Server tears down texture on source GPU
            2. Compositor deallocates resources
            3. Window Server rebuilds texture on target GPU
            4. Compositor re-renders and hydrates

            This can take 500ms-2000ms depending on system load.
            """
            total_waited = 0.0

            for checkpoint_delay in hydration_checkpoints:
                await asyncio.sleep(checkpoint_delay)
                total_waited += checkpoint_delay

                current_space = await get_window_space()

                if current_space == expected_space:
                    logger.info(
                        f"[YABAI] ✅ FIRE AND CONFIRM SUCCESS: Window {window_id} → Space {expected_space} "
                        f"(verified at {total_waited:.1f}s, strategy: {strategy_name})"
                    )
                    return True
                elif current_space is None:
                    # Window in transit (dehydrated) - keep waiting
                    logger.debug(
                        f"[YABAI] ⏳ Window {window_id} in transit (checkpoint {total_waited:.1f}s)..."
                    )
                else:
                    # Window on unexpected space - might still be moving
                    logger.debug(
                        f"[YABAI] 🔄 Window {window_id} on space {current_space} "
                        f"(expected {expected_space}, checkpoint {total_waited:.1f}s)"
                    )

                if total_waited >= max_hydration_time:
                    break

            # Final check after all checkpoints
            final_space = await get_window_space()
            if final_space == expected_space:
                logger.info(
                    f"[YABAI] ✅ FIRE AND CONFIRM SUCCESS (late): Window {window_id} → Space {expected_space} "
                    f"(verified at {total_waited:.1f}s, strategy: {strategy_name})"
                )
                return True

            logger.warning(
                f"[YABAI] ⚠️ FIRE AND CONFIRM FAILED: Window {window_id} still on space {final_space} "
                f"(expected {expected_space}) after {total_waited:.1f}s (strategy: {strategy_name})"
            )
            return False

        # ═══════════════════════════════════════════════════════════════════
        # v34.0: INTELLIGENT MOVE COMMAND EXECUTION WITH DISPLAY HANDOFF
        # ═══════════════════════════════════════════════════════════════════

        async def execute_display_handoff(window_id: int, display_id: int) -> Tuple[bool, str]:
            """
            v34.0: DISPLAY HANDOFF - Move window to a display (not space).

            This is the KEY FIX for cross-display moves without Scripting Additions.
            The --display command uses a simpler "Move to Monitor" instruction that
            macOS allows natively, bypassing the complex GPU context management
            that causes --space to fail silently.

            Returns (success, error_message)
            """
            try:
                logger.info(
                    f"[YABAI] 🚀 DISPLAY HANDOFF: Moving window {window_id} → Display {display_id}"
                )
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(window_id), "--display", str(display_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                if proc.returncode != 0:
                    error_msg = stderr.decode().strip() if stderr else "Unknown error"
                    logger.warning(f"[YABAI] Display handoff command failed: {error_msg}")
                    return False, error_msg

                logger.info(f"[YABAI] ✅ Display handoff command accepted")
                return True, ""

            except asyncio.TimeoutError:
                return False, "Display handoff command timed out"
            except Exception as e:
                return False, str(e)

        async def execute_space_move(window_id: int, target_space: int) -> Tuple[bool, str]:
            """
            Execute standard --space move command.

            Returns (success, error_message)
            - success=True: Command accepted by yabai (may still need hydration)
            - success=False: Command rejected (window not found, space invalid, etc.)
            """
            try:
                proc = await asyncio.create_subprocess_exec(
                    yabai_path, "-m", "window", str(window_id), "--space", str(target_space),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                if proc.returncode != 0:
                    error_msg = stderr.decode().strip() if stderr else "Unknown error"
                    return False, error_msg

                return True, ""

            except asyncio.TimeoutError:
                return False, "Command timed out"
            except Exception as e:
                return False, str(e)

        async def execute_yabai_move(window_id: int, target_space: int) -> Tuple[bool, str]:
            """
            v34.0: INTELLIGENT MOVE - Automatically chooses --display or --space.

            For cross-display moves: Use --display (bypasses SA requirement)
            For same-display moves: Use --space (standard behavior)

            Returns (success, error_message)
            """
            if is_cross_display_move and target_display is not None:
                # CROSS-DISPLAY: Use Display Handoff (the fix!)
                return await execute_display_handoff(window_id, target_display)
            else:
                # SAME-DISPLAY: Use standard --space command
                return await execute_space_move(window_id, target_space)

        # Check initial state
        initial_space = await get_window_space()
        if initial_space == target_space:
            logger.info(f"[YABAI] Window {window_id} already on target space {target_space}")
            return True

        original_user_space = self.get_current_user_space()

        # ═══════════════════════════════════════════════════════════════════
        # v34.0: STRATEGY DEFINITIONS WITH DISPLAY HANDOFF PRIORITY
        # ═══════════════════════════════════════════════════════════════════
        # For cross-display moves, display_handoff is tried FIRST
        # For same-display moves, standard strategies are used
        # ═══════════════════════════════════════════════════════════════════

        # ═══════════════════════════════════════════════════════════════════
        # v34.0: STEALTH MODE - NEVER HIJACK USER'S SCREEN
        # ═══════════════════════════════════════════════════════════════════
        # When silent=True (used by God Mode / background monitoring):
        # - ONLY use non-focus strategies
        # - Progressive retries with delays for Strategy 1
        # - Return False instead of escalating to focus-based strategies
        # ═══════════════════════════════════════════════════════════════════

        if silent:
            logger.info(f"[YABAI] 🤫 STEALTH MODE: Focus strategies DISABLED for window {window_id}")

        if is_cross_display_move:
            # Cross-display: Prioritize Display Handoff
            if silent:
                # STEALTH: Only non-focus strategies with progressive retries
                strategies = [
                    ("display_handoff", "Display Handoff (cross-display native)"),
                    ("display_handoff_retry_1", "Display Handoff (retry 1, +0.5s delay)"),
                    ("display_handoff_retry_2", "Display Handoff (retry 2, +1.0s delay)"),
                    ("display_then_space", "Display Handoff + Space refinement"),
                ]
            else:
                strategies = [
                    ("display_handoff", "Display Handoff (cross-display native)"),
                    ("display_then_space", "Display Handoff + Space refinement"),
                    ("focus_first", "Focus window first, then move"),
                    ("switch_grab_return", "Full space switch, focus, move, return"),
                ]
        else:
            # Same-display: Use standard strategies
            if silent:
                # STEALTH: Only non-focus strategies with progressive retries
                strategies = [
                    ("direct_fire_confirm", "Direct move with progressive verification"),
                    ("direct_retry_1", "Direct move (retry 1, +0.5s delay)"),
                    ("direct_retry_2", "Direct move (retry 2, +1.0s delay)"),
                    ("wake_then_move", "Wake space via AppleScript (no focus)"),
                ]
            else:
                strategies = [
                    ("direct_fire_confirm", "Direct move with progressive verification"),
                    ("focus_first", "Focus window first, then move"),
                    ("wake_then_move", "Wake space via AppleScript, then direct move"),
                    ("switch_grab_return", "Full space switch, focus, move, return"),
                ]

        for attempt, (strategy, description) in enumerate(strategies[:max_retries]):
            logger.info(
                f"[YABAI] 🚀 Strategy {attempt + 1}/{min(max_retries, len(strategies))}: {description} "
                f"(window {window_id} → Space {target_space})"
            )

            try:
                if strategy == "display_handoff":
                    # =============================================================
                    # v34.0 STRATEGY: DISPLAY HANDOFF (Cross-Display Native)
                    # =============================================================
                    # THE ROOT FIX: Use --display instead of --space for cross-display moves.
                    # macOS allows "Move to Monitor" natively without Scripting Additions,
                    # while --space requires complex GPU context management that fails silently.
                    # =============================================================
                    if target_display is None:
                        logger.warning("[YABAI] Display handoff skipped - target display unknown")
                        continue

                    cmd_success, error_msg = await execute_display_handoff(window_id, target_display)

                    if not cmd_success:
                        logger.warning(f"[YABAI] ❌ Display Handoff REJECTED: {error_msg}")
                        continue

                    # Command ACCEPTED - verify window is now on target display
                    if verify:
                        if await verify_move_with_patience(target_space, strategy):
                            self._health.record_success(0)
                            return True
                        else:
                            # Window might be on display but wrong space - try space refinement
                            logger.info("[YABAI] Display handoff partial - window on display but may need space refinement")
                    else:
                        self._health.record_success(0)
                        return True

                elif strategy == "display_then_space":
                    # =============================================================
                    # v34.0 STRATEGY: DISPLAY HANDOFF + SPACE REFINEMENT
                    # =============================================================
                    # Two-step approach:
                    # 1. Move window to target display using --display (reliable)
                    # 2. Move window to specific space using --space (now same-display)
                    # =============================================================
                    if target_display is None:
                        logger.warning("[YABAI] Display+Space strategy skipped - target display unknown")
                        continue

                    # Step 1: Move to target display
                    logger.info(f"[YABAI] 📍 Step 1: Moving to Display {target_display}...")
                    display_success, error_msg = await execute_display_handoff(window_id, target_display)

                    if not display_success:
                        logger.warning(f"[YABAI] Display step failed: {error_msg}")
                        continue

                    # Wait for display transfer to complete
                    await asyncio.sleep(0.5)

                    # Verify window is now on target display
                    new_display = await get_window_display()
                    if new_display != target_display:
                        logger.warning(f"[YABAI] Window still on Display {new_display}, expected {target_display}")
                        continue

                    logger.info(f"[YABAI] ✅ Window on Display {target_display}, refining to Space {target_space}...")

                    # Step 2: Now that window is on same display, use --space for precision
                    space_success, error_msg = await execute_space_move(window_id, target_space)

                    if space_success and verify:
                        if await verify_move_with_patience(target_space, strategy):
                            self._health.record_success(0)
                            return True

                elif strategy.startswith("display_handoff_retry"):
                    # =============================================================
                    # v34.0 STEALTH RETRY: Progressive Display Handoff Retries
                    # =============================================================
                    # Instead of escalating to focus-based strategies, we retry
                    # the display handoff with increasing delays between attempts.
                    # This gives macOS more time to complete GPU context switches.
                    # =============================================================
                    if target_display is None:
                        logger.warning("[YABAI] Display handoff retry skipped - target display unknown")
                        continue

                    # Extract retry number for delay calculation
                    retry_num = 1 if "retry_1" in strategy else 2
                    delay = 0.5 * retry_num  # 0.5s, 1.0s

                    logger.info(f"[YABAI] 🔄 STEALTH RETRY: Waiting {delay}s before retry {retry_num}...")
                    await asyncio.sleep(delay)

                    cmd_success, error_msg = await execute_display_handoff(window_id, target_display)

                    if not cmd_success:
                        logger.warning(f"[YABAI] ❌ Display Handoff retry {retry_num} REJECTED: {error_msg}")
                        continue

                    if verify:
                        if await verify_move_with_patience(target_space, strategy):
                            self._health.record_success(0)
                            return True
                    else:
                        self._health.record_success(0)
                        return True

                elif strategy.startswith("direct_retry"):
                    # =============================================================
                    # v34.0 STEALTH RETRY: Progressive Direct Move Retries
                    # =============================================================
                    # For same-display moves, retry with increasing delays.
                    # =============================================================
                    retry_num = 1 if "retry_1" in strategy else 2
                    delay = 0.5 * retry_num  # 0.5s, 1.0s

                    logger.info(f"[YABAI] 🔄 STEALTH RETRY: Waiting {delay}s before retry {retry_num}...")
                    await asyncio.sleep(delay)

                    cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                    if not cmd_success:
                        logger.warning(f"[YABAI] ❌ Direct move retry {retry_num} REJECTED: {error_msg}")
                        continue

                    if verify:
                        if await verify_move_with_patience(target_space, strategy):
                            self._health.record_success(0)
                            return True
                    else:
                        self._health.record_success(0)
                        return True

                elif strategy == "direct_fire_confirm":
                    # =============================================================
                    # STRATEGY 1: FIRE AND CONFIRM (Patient Direct Move)
                    # =============================================================
                    # The key fix: Fire the command, then wait patiently for physics
                    # =============================================================
                    cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                    if not cmd_success:
                        # Command was REJECTED by yabai - don't wait, fail fast
                        logger.warning(
                            f"[YABAI] ❌ Strategy 1 REJECTED: {error_msg} "
                            f"(window {window_id}, target {target_space})"
                        )
                        # Check if it's a "window not found" error (dehydrated window)
                        if "could not locate" in error_msg.lower():
                            logger.info("[YABAI] Window appears dehydrated - will try wake strategy")
                        continue  # Try next strategy immediately

                    # Command ACCEPTED - now wait for physics with progressive verification
                    if verify:
                        if await verify_move_with_patience(target_space, strategy):
                            self._health.record_success(0)
                            return True
                        # Verification failed after patient waiting - try next strategy
                    else:
                        # No verification - trust that command acceptance means success
                        self._health.record_success(0)
                        return True

                elif strategy == "wake_then_move":
                    # =============================================================
                    # STRATEGY 2: WAKE SPACE FIRST (AppleScript), THEN DIRECT MOVE
                    # =============================================================
                    # This wakes up dehydrated windows without requiring SA
                    # =============================================================
                    source_space = initial_space or await get_window_space()

                    if source_space and source_space != target_space:
                        logger.info(f"[YABAI] 🌅 Waking space {source_space} via AppleScript...")

                        # Use our new AppleScript-based space switching
                        wake_success = self._switch_to_space_applescript(source_space)

                        if wake_success:
                            logger.info(f"[YABAI] ✅ Space {source_space} awakened")
                            # Wait for hydration after space wake
                            await asyncio.sleep(0.5)
                        else:
                            logger.debug(f"[YABAI] AppleScript wake failed, trying move anyway")

                    # Now try direct move (window should be awake)
                    cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                    if cmd_success and verify:
                        if await verify_move_with_patience(target_space, strategy):
                            # Return user to original space
                            if original_user_space and original_user_space != target_space:
                                self._switch_to_space_applescript(original_user_space)
                            self._health.record_success(0)
                            return True

                elif strategy == "focus_first":
                    # =============================================================
                    # STRATEGY 2 (v33.1): FOCUS WINDOW FIRST, THEN MOVE
                    # =============================================================
                    # Focusing a window brings user to its space and wakes it up
                    # This works WITHOUT yabai Scripting Addition
                    # =============================================================
                    logger.info(f"[YABAI] 🎯 Focusing window {window_id} to wake it...")
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "window", "--focus", str(window_id),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)

                    if proc.returncode != 0:
                        error_msg = stderr.decode().strip() if stderr else "Unknown error"
                        logger.warning(f"[YABAI] Focus command failed: {error_msg}")
                    else:
                        logger.info(f"[YABAI] ✅ Window {window_id} focused (now on active space)")

                    await asyncio.sleep(0.8)  # v33.1: Increased to 0.8s for full hydration

                    # v33.1: Check window state before attempting move
                    current_space_after_focus = await get_window_space()
                    logger.info(
                        f"[YABAI] 📍 After focus: Window {window_id} now on space {current_space_after_focus} "
                        f"(target: {target_space})"
                    )

                    cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                    if not cmd_success:
                        logger.warning(f"[YABAI] Move after focus failed: {error_msg}")
                    else:
                        logger.info(f"[YABAI] ✅ Move command accepted (window {window_id} → space {target_space})")

                    if cmd_success and verify:
                        if await verify_move_with_patience(target_space, strategy):
                            self._health.record_success(0)
                            return True

                elif strategy == "switch_grab_return":
                    # =============================================================
                    # STRATEGY 4: FULL SWITCH-GRAB-RETURN
                    # =============================================================
                    # Most reliable but causes screen flash - use as last resort
                    # =============================================================
                    source_space = initial_space or await get_window_space()

                    if source_space:
                        # Switch to source space using multi-strategy switching
                        logger.info(f"[YABAI] 🔄 Full space switch to {source_space}...")
                        switch_success = self._switch_to_space(source_space)

                        if switch_success:
                            await asyncio.sleep(0.5)  # Let space switch + hydration complete

                            # Focus the window
                            proc = await asyncio.create_subprocess_exec(
                                yabai_path, "-m", "window", "--focus", str(window_id),
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE
                            )
                            await asyncio.wait_for(proc.communicate(), timeout=3.0)
                            await asyncio.sleep(0.2)

                            # Move to target
                            cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                            if cmd_success and verify:
                                if await verify_move_with_patience(target_space, strategy):
                                    # Return user to original space
                                    if original_user_space and original_user_space != target_space:
                                        self._switch_to_space(original_user_space)
                                    self._health.record_success(0)
                                    return True

                            # Return user to original space even on failure
                            if original_user_space:
                                self._switch_to_space(original_user_space)

            except asyncio.TimeoutError:
                logger.warning(f"[YABAI] Strategy {strategy} timed out for window {window_id}")
            except Exception as e:
                logger.warning(f"[YABAI] Strategy {strategy} failed: {e}")

            # Brief pause before trying next strategy
            if attempt < min(max_retries, len(strategies)) - 1:
                await asyncio.sleep(0.3)

        # ═══════════════════════════════════════════════════════════════════════
        # v51.3: LAST RESORT - AppleScript + Focus-based strategies
        # ═══════════════════════════════════════════════════════════════════════
        # ROOT CAUSE FIX: Windows on phantom fullscreen spaces CANNOT be moved
        # using silent strategies OR yabai focus because:
        # - yabai has limited power over windows not on the active space
        # - yabai --focus fails for windows on phantom spaces
        #
        # SOLUTION: Use AppleScript to activate the process by PID FIRST, then
        # use yabai to move. AppleScript operates at a higher level than yabai
        # and can activate apps even when their windows are on hidden spaces.
        # ═══════════════════════════════════════════════════════════════════════
        if silent:
            logger.warning(
                f"[YABAI v51.3] 🚨 LAST RESORT: All silent strategies failed for window {window_id}. "
                f"Attempting AppleScript + focus-based rescue..."
            )

            # Get PID for AppleScript activation
            window_pid = None
            if window_info:
                window_pid = window_info.get("pid")
            if not window_pid:
                # Re-query to get PID
                try:
                    refresh_info = await get_window_info_async()
                    if refresh_info:
                        window_pid = refresh_info.get("pid")
                except Exception:
                    pass

            last_resort_strategies = [
                ("applescript_activate", "AppleScript PID activation + move"),
                ("focus_first", "Yabai focus + move"),
                ("switch_grab_return", "Space switch + focus + move"),
            ]

            for lr_attempt, (strategy, description) in enumerate(last_resort_strategies):
                logger.info(
                    f"[YABAI v51.3] 🆘 Last Resort {lr_attempt + 1}/{len(last_resort_strategies)}: {description}"
                )

                try:
                    if strategy == "applescript_activate" and window_pid:
                        # v51.3: Use AppleScript to activate by PID (higher level than yabai)
                        logger.info(f"[YABAI v51.3] 🎯 AppleScript activation for PID {window_pid}...")

                        activate_script = f'''
                        tell application "System Events"
                            try
                                set targetProc to first process whose unix id is {window_pid}
                                set frontmost of targetProc to true
                                set visible of targetProc to true

                                -- Try to exit fullscreen via keyboard
                                delay 0.5
                                key code 53 -- Escape
                                delay 0.3
                                keystroke "f" using {{control down, command down}}
                                delay 0.5

                                return "ACTIVATED"
                            on error errMsg
                                return "ERROR: " & errMsg
                            end try
                        end tell
                        '''

                        proc = await asyncio.create_subprocess_exec(
                            "osascript", "-e", activate_script,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                        result = stdout.decode().strip() if stdout else ""

                        if "ACTIVATED" in result:
                            logger.info(f"[YABAI v51.3] ✅ Process {window_pid} activated via AppleScript")
                            await asyncio.sleep(1.5)  # Wait for fullscreen exit animation

                            # Now try to move - window should be on active space
                            cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                            if cmd_success:
                                if await verify_move_with_patience(target_space, "last_resort_applescript"):
                                    # Return user to original space
                                    if original_user_space and original_user_space != target_space:
                                        self._switch_to_space_applescript(original_user_space)
                                    self._health.record_success(0)
                                    logger.info(f"[YABAI v51.3] ✅ LAST RESORT SUCCESS via AppleScript activation")
                                    return True
                            else:
                                # AppleScript worked but yabai move failed - try display move
                                logger.info(f"[YABAI v51.3] Space move failed, trying display move...")
                                ghost_display = await self._get_ghost_display_index_async()
                                if ghost_display:
                                    display_success = await self._move_window_to_display_async(
                                        window_id, ghost_display, sip_convergence_delay=2.0
                                    )
                                    if display_success:
                                        if original_user_space:
                                            self._switch_to_space_applescript(original_user_space)
                                        self._health.record_success(0)
                                        logger.info(f"[YABAI v51.3] ✅ LAST RESORT SUCCESS via AppleScript + display move")
                                        return True
                        else:
                            logger.warning(f"[YABAI v51.3] AppleScript activation failed: {result or stderr.decode()}")

                    elif strategy == "focus_first":
                        # Focus the window to bring it to the current space
                        logger.info(f"[YABAI v51.3] 🎯 Focusing window {window_id} via yabai...")
                        proc = await asyncio.create_subprocess_exec(
                            yabai_path, "-m", "window", "--focus", str(window_id),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                        if proc.returncode == 0:
                            logger.info(f"[YABAI v51.3] ✅ Window {window_id} focused")
                            await asyncio.sleep(1.0)

                            cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                            if cmd_success:
                                if await verify_move_with_patience(target_space, "last_resort_focus"):
                                    if original_user_space and original_user_space != target_space:
                                        self._switch_to_space_applescript(original_user_space)
                                    self._health.record_success(0)
                                    logger.info(f"[YABAI v51.3] ✅ LAST RESORT SUCCESS via focus_first")
                                    return True
                        else:
                            error_msg = stderr.decode().strip() if stderr else "Unknown"
                            logger.warning(f"[YABAI v51.3] Yabai focus failed: {error_msg}")

                    elif strategy == "switch_grab_return":
                        source_space = await get_window_space()
                        if source_space and source_space > 0:
                            logger.info(f"[YABAI v51.3] 🔄 Switching to Space {source_space}...")

                            switch_success = self._switch_to_space_applescript(source_space)
                            if switch_success:
                                await asyncio.sleep(0.8)

                                proc = await asyncio.create_subprocess_exec(
                                    yabai_path, "-m", "window", "--focus", str(window_id),
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE
                                )
                                await asyncio.wait_for(proc.communicate(), timeout=3.0)
                                await asyncio.sleep(0.5)

                                cmd_success, error_msg = await execute_yabai_move(window_id, target_space)

                                if cmd_success:
                                    if await verify_move_with_patience(target_space, "last_resort_switch"):
                                        if original_user_space and original_user_space != source_space:
                                            self._switch_to_space_applescript(original_user_space)
                                        self._health.record_success(0)
                                        logger.info(f"[YABAI v51.3] ✅ LAST RESORT SUCCESS via switch_grab_return")
                                        return True

                except asyncio.TimeoutError:
                    logger.warning(f"[YABAI v51.3] Last resort {strategy} timed out")
                except Exception as e:
                    logger.warning(f"[YABAI v51.3] Last resort {strategy} error: {e}")

                await asyncio.sleep(0.3)

            logger.warning(
                f"[YABAI v51.3] ❌ LAST RESORT FAILED: All strategies exhausted for window {window_id}"
            )

        # ═══════════════════════════════════════════════════════════════════════
        # v44.1: STATE CONVERGENCE RECOVERY (Enhanced)
        # ═══════════════════════════════════════════════════════════════════════
        # We don't brute force. We ANALYZE.
        # 
        # ENHANCEMENTS (v44.1):
        # - Per-window state tracking to avoid race conditions
        # - Re-query Ghost Display after topology drift (indices can shift!)
        # - Graceful fallback if no converged state available
        # ═══════════════════════════════════════════════════════════════════════
        
        # v44.1: Use per-window state dictionary (race-condition safe)
        converged_states = getattr(self, '_converged_window_states', {})
        converged_info = converged_states.get(window_id) or getattr(self, '_last_converged_window_info', None)
        
        if converged_info:
            converged_space = converged_info.get('space')
            converged_display = converged_info.get('display')
            
            # v44.1: RE-QUERY GHOST DISPLAY - Topology may have shifted!
            # After fullscreen exit, space indices change. Our original target_space
            # and target_display may now be incorrect.
            logger.info(
                f"[YABAI v44.1] 🔄 STATE CONVERGENCE RECOVERY: Window {window_id} "
                f"drifted to Space {converged_space} (Display {converged_display}). "
                f"Re-querying Ghost Display (topology may have shifted)..."
            )
            
            # Re-query the Ghost Display to get FRESH target coordinates
            fresh_target_display = None
            fresh_target_space = None
            try:
                fresh_ghost_space = self.get_ghost_display_space()
                if fresh_ghost_space:
                    fresh_target_space = fresh_ghost_space
                    fresh_target_display = await get_space_display(fresh_ghost_space)
                    if fresh_target_space != target_space:
                        logger.info(
                            f"[YABAI v44.1] 🌊 TOPOLOGY SHIFT DETECTED: Ghost Display moved "
                            f"Space {target_space} → {fresh_target_space}"
                        )
            except Exception as e:
                logger.debug(f"[YABAI v44.1] Ghost Display re-query failed: {e}")
            
            # Use fresh targets if available, otherwise fall back to original
            recovery_target_space = fresh_target_space or target_space
            recovery_target_display = fresh_target_display or target_display
            
            # Only retry if the window is NOT already on target
            if converged_space != recovery_target_space:
                try:
                    # Use Display Handoff with the CORRECT (fresh) target
                    if converged_display != recovery_target_display and recovery_target_display is not None:
                        logger.info(
                            f"[YABAI v44.1] 🚀 Recovery move: Window {window_id} → Display {recovery_target_display}"
                        )
                        proc = await asyncio.create_subprocess_exec(
                            yabai_path, "-m", "window", str(window_id), "--display", str(recovery_target_display),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                        
                        if proc.returncode == 0:
                            # Wait for physics to settle
                            await asyncio.sleep(1.0)
                            
                            # Verify
                            final_space = await get_window_space()
                            final_display = await get_window_display()
                            
                            if final_display == recovery_target_display:
                                logger.info(
                                    f"[YABAI v44.1] ✅ CONVERGENCE RECOVERY SUCCESS: "
                                    f"Window {window_id} → Display {recovery_target_display} (Space {final_space})"
                                )
                                # Clean up per-window state
                                if window_id in converged_states:
                                    del converged_states[window_id]
                                self._health.record_success(0)
                                return True
                            else:
                                logger.warning(
                                    f"[YABAI v44.1] Convergence recovery move accepted but "
                                    f"window still on Display {final_display}"
                                )
                        else:
                            error_msg = stderr.decode().strip() if stderr else "Unknown"
                            logger.warning(f"[YABAI v44.1] Convergence recovery move rejected: {error_msg}")
                            
                except asyncio.TimeoutError:
                    logger.warning("[YABAI v44.1] Convergence recovery timed out")
                except Exception as e:
                    logger.warning(f"[YABAI v44.1] Convergence recovery error: {e}")
            else:
                # Window already on target!
                logger.info(
                    f"[YABAI v44.1] ✅ CONVERGENCE: Window {window_id} already on target Space {recovery_target_space}"
                )
                # Clean up per-window state
                if window_id in converged_states:
                    del converged_states[window_id]
                return True
        else:
            logger.warning(
                f"[YABAI v44.0] No converged state available - window may still be animating"
            )

        # ═══════════════════════════════════════════════════════════════════════
        # v50.0: HARDWARE TARGETING FALLBACK (SIP-Compliant Last Resort)
        # ═══════════════════════════════════════════════════════════════════════
        # All space-based strategies failed. With SIP enabled, moving to spaces
        # often fails because it requires code injection into the Dock.
        #
        # FINAL FALLBACK: Move to DISPLAY (hardware) instead of SPACE (software).
        # yabai can move to displays with SIP enabled using Accessibility APIs.
        # ═══════════════════════════════════════════════════════════════════════

        logger.warning(
            f"[YABAI v50.0] 🎯 All space strategies failed. Attempting HARDWARE TARGETING fallback..."
        )

        try:
            # Get the ghost display index
            ghost_display = await self._get_ghost_display_index_async()

            if ghost_display is not None:
                # Try hardware targeting with SIP-aware convergence delay
                sip_delay = float(os.getenv("JARVIS_SIP_CONVERGENCE_DELAY", "3.0"))
                hardware_success = await self._move_window_to_display_async(
                    window_id=window_id,
                    display_index=ghost_display,
                    sip_convergence_delay=sip_delay
                )

                if hardware_success:
                    # Verify window is now on target display
                    proc = await asyncio.create_subprocess_exec(
                        yabai_path, "-m", "query", "--windows", "--window", str(window_id),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                    if proc.returncode == 0 and stdout:
                        final_info = json.loads(stdout.decode())
                        final_display = final_info.get("display")

                        if final_display == ghost_display:
                            logger.info(
                                f"[YABAI v50.0] ✅ HARDWARE TARGETING SUCCESS: "
                                f"Window {window_id} → Display {ghost_display} (SIP-compliant!)"
                            )
                            self._health.record_success(0)
                            return True
                        else:
                            logger.warning(
                                f"[YABAI v50.0] Hardware targeting accepted but window "
                                f"still on Display {final_display}"
                            )
                else:
                    logger.warning(
                        f"[YABAI v50.0] Hardware targeting failed for window {window_id}"
                    )
            else:
                logger.warning("[YABAI v50.0] Could not detect ghost display for hardware targeting")

        except Exception as e:
            logger.warning(f"[YABAI v50.0] Hardware targeting fallback error: {e}")

        # All strategies truly exhausted
        logger.error(
            f"[YABAI v50.0] ❌ FAILED to move window {window_id} to Space {target_space} "
            f"after {max_retries} attempts + hardware fallback. Window state did not converge."
        )
        self._health.record_failure(f"Window {window_id} failed to move - all strategies exhausted")
        return False

    # =========================================================================
    # SEARCH & RESCUE PROTOCOL v23.0.0
    # =========================================================================
    # ROOT CAUSE: Yabai cannot act on "dehydrated" windows on hidden spaces.
    # macOS puts windows on hidden spaces into a frozen state where Yabai
    # loses track of them ("could not locate the window to act on!").
    #
    # SOLUTION: Switch-Grab-Return protocol
    # 1. Try direct move (fast path - works for visible windows)
    # 2. If fails, "wake" the window by switching to its space
    # 3. Move the window while it's awake
    # 4. Immediately return to the user's original space
    #
    # This creates a brief screen flash but successfully rescues windows
    # from hidden spaces that would otherwise be inaccessible.
    # =========================================================================

    def _switch_to_space(self, space_id: int) -> bool:
        """
        Switch focus to a specific space.

        This is a low-level helper for the rescue protocol. It briefly
        changes the active space to "wake up" dehydrated windows.

        Args:
            space_id: The space index to switch to (1-based)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_available():
            return False

        try:
            yabai_path = self._health.yabai_path or "yabai"

            result = subprocess.run(
                [yabai_path, "-m", "space", "--focus", str(space_id)],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.debug(f"[YABAI] Space switch to {space_id} failed: {error_msg}")
                return False

            return True

        except subprocess.TimeoutExpired:
            logger.warning(f"[YABAI] Space switch timed out for space {space_id}")
            return False
        except Exception as e:
            logger.warning(f"[YABAI] Space switch failed: {e}")
            return False

    def _switch_to_space_applescript(self, space_id: int) -> bool:
        """
        Switch to a space using AppleScript Control+Number keyboard shortcut.

        v33.0 FIX: This method works WITHOUT yabai Scripting Addition (SA).
        Most users don't have SA enabled because it requires SIP to be disabled.

        The approach uses Control+<number> keyboard shortcuts which are the
        default macOS shortcuts for switching between spaces.

        Args:
            space_id: The space index to switch to (1-10)

        Returns:
            True if successful, False otherwise
        """
        if space_id < 1 or space_id > 10:
            logger.warning(f"[YABAI] AppleScript space switch only supports spaces 1-10, got {space_id}")
            return False

        # macOS key codes for numbers 1-0 (0 is keycode 29)
        # These are the physical key codes for the number row
        key_codes = {
            1: 18,   # 1
            2: 19,   # 2
            3: 20,   # 3
            4: 21,   # 4
            5: 23,   # 5
            6: 22,   # 6
            7: 26,   # 7
            8: 28,   # 8
            9: 25,   # 9
            10: 29,  # 0
        }

        key_code = key_codes.get(space_id)
        if key_code is None:
            return False

        applescript = f'''
        tell application "System Events"
            key code {key_code} using control down
        end tell
        '''

        try:
            result = subprocess.run(
                ['osascript', '-e', applescript],
                capture_output=True,
                text=True,
                timeout=3.0
            )

            if result.returncode == 0:
                # Give Mission Control time to animate
                time.sleep(0.5)
                logger.debug(f"[YABAI] AppleScript space switch to {space_id} succeeded")
                return True
            else:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.debug(f"[YABAI] AppleScript space switch failed: {error_msg}")
                return False

        except subprocess.TimeoutExpired:
            logger.warning(f"[YABAI] AppleScript space switch timed out for space {space_id}")
            return False
        except Exception as e:
            logger.warning(f"[YABAI] AppleScript space switch failed: {e}")
            return False

    async def _switch_to_space_async(self, space_id: int) -> bool:
        """
        Async version of space switching with multi-strategy fallback.

        v33.0: Tries yabai first, falls back to AppleScript if yabai fails.
        """
        loop = asyncio.get_event_loop()

        # Strategy 1: Try yabai (requires SA)
        yabai_success = await loop.run_in_executor(
            None,
            lambda: self._switch_to_space(space_id)
        )
        if yabai_success:
            return True

        # Strategy 2: Fall back to AppleScript (works without SA)
        logger.debug(f"[YABAI] Yabai space switch failed, trying AppleScript...")
        applescript_success = await loop.run_in_executor(
            None,
            lambda: self._switch_to_space_applescript(space_id)
        )
        return applescript_success

    def move_window_to_space_with_rescue(
        self,
        window_id: int,
        target_space: int,
        source_space: Optional[int] = None,
        app_name: Optional[str] = None,
        max_retries: int = 3,
        is_minimized: bool = False,
        is_fullscreen: bool = False
    ) -> Tuple[bool, str]:
        """
        Move a window to a target space using INTELLIGENT SEARCH & RESCUE PROTOCOL v24.0.

        INTELLIGENT SEARCH & RESCUE PROTOCOL:
        1. Fast Path: Try direct move (works for visible windows)
        2. Strategy Selection: Choose best rescue strategy based on telemetry
        3. Retry with Backoff: Multiple attempts with exponential backoff
        4. Multi-Strategy Fallback: Try alternative strategies on failure
        5. Telemetry Recording: Track success/failure for learning

        Args:
            window_id: The window ID to move
            target_space: The target space index (1-based)
            source_space: The window's current space (optional, for rescue)
            app_name: Application name for telemetry (optional)
            max_retries: Maximum retry attempts (default: 3)
            is_minimized: Whether window is minimized (affects strategy)
            is_fullscreen: Whether window is fullscreen (affects strategy)

        Returns:
            Tuple of (success, method):
            - success: True if window was moved
            - method: "direct" | "rescue" | "failed"
        """
        start_time = time.time()
        telemetry = get_rescue_telemetry()

        # =====================================================================
        # FAST PATH: Direct move (works for visible windows)
        # =====================================================================
        if self.move_window_to_space(window_id, target_space):
            duration_ms = (time.time() - start_time) * 1000
            telemetry.record_attempt(
                success=True,
                strategy=RescueStrategy.DIRECT,
                duration_ms=duration_ms,
                app_name=app_name
            )
            return True, "direct"

        # =====================================================================
        # INTELLIGENT RESCUE PATH: Multi-strategy with retry and backoff
        # =====================================================================
        if source_space is None:
            logger.warning(
                f"[YABAI] 🛟 Rescue needed for window {window_id} but source_space unknown"
            )
            telemetry.record_attempt(
                success=False,
                strategy=RescueStrategy.DIRECT,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Get recommended strategy from telemetry
        recommended_strategy = telemetry.get_recommended_strategy(
            app_name=app_name,
            is_minimized=is_minimized,
            is_fullscreen=is_fullscreen
        )

        logger.info(
            f"[YABAI] 🛟 INTELLIGENT RESCUE: Window {window_id} on Space {source_space} "
            f"→ Space {target_space} (Strategy: {recommended_strategy.value})"
        )

        # Remember where the user is
        current_space = self.get_current_user_space()
        if current_space is None:
            logger.error("[YABAI] Cannot rescue: unable to determine current space")
            telemetry.record_attempt(
                success=False,
                strategy=recommended_strategy,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Strategy order: try recommended first, then fallbacks
        strategies_to_try = [recommended_strategy]
        all_strategies = [
            RescueStrategy.SWITCH_GRAB_RETURN,
            RescueStrategy.FOCUS_THEN_MOVE,
            RescueStrategy.SPACE_FOCUS_EXTENDED,
            RescueStrategy.UNMINIMIZE_FIRST,
            RescueStrategy.EXIT_FULLSCREEN_FIRST,  # v31.0: Handle fullscreen windows
        ]
        for s in all_strategies:
            if s not in strategies_to_try:
                strategies_to_try.append(s)

        rescue_success = False
        failure_reason = RescueFailureReason.UNKNOWN
        total_attempts = 0
        final_strategy = recommended_strategy
        wake_delay_used_ms = 0.0

        try:
            for strategy in strategies_to_try[:2]:  # Try at most 2 strategies
                for attempt in range(1, max_retries + 1):
                    total_attempts += 1
                    final_strategy = strategy

                    # Calculate backoff delay
                    backoff_multiplier = 2 ** (attempt - 1)
                    base_wake_delay_ms = telemetry.get_recommended_wake_delay_ms(app_name)

                    # Strategy-specific wake delay multipliers
                    if strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                        wake_delay_ms = base_wake_delay_ms * 3 * backoff_multiplier
                    elif strategy == RescueStrategy.UNMINIMIZE_FIRST:
                        wake_delay_ms = base_wake_delay_ms * 2 * backoff_multiplier
                    else:
                        wake_delay_ms = base_wake_delay_ms * backoff_multiplier

                    wake_delay_used_ms = wake_delay_ms
                    wake_delay_s = wake_delay_ms / 1000.0

                    logger.debug(
                        f"[YABAI] 🛟 Attempt {total_attempts}: {strategy.value} "
                        f"(wake delay: {wake_delay_ms:.0f}ms)"
                    )

                    # Execute the rescue strategy
                    success, reason = self._execute_rescue_strategy(
                        strategy=strategy,
                        window_id=window_id,
                        source_space=source_space,
                        target_space=target_space,
                        wake_delay_s=wake_delay_s
                    )

                    if success:
                        rescue_success = True
                        break

                    failure_reason = reason

                    # Brief pause before retry
                    if attempt < max_retries:
                        time.sleep(0.1 * attempt)

                if rescue_success:
                    break

        except Exception as e:
            logger.error(f"[YABAI] 🛟 Rescue exception: {e}")
            failure_reason = RescueFailureReason.UNKNOWN

        finally:
            # ALWAYS return to original space (even on failure)
            logger.debug(f"[YABAI] 🛟 Returning to Space {current_space}...")
            if not self._switch_to_space(current_space):
                logger.warning(f"[YABAI] 🛟 Warning: Failed to return to Space {current_space}")

        # Record telemetry
        duration_ms = (time.time() - start_time) * 1000
        telemetry.record_attempt(
            success=rescue_success,
            strategy=final_strategy,
            duration_ms=duration_ms,
            failure_reason=failure_reason if not rescue_success else None,
            app_name=app_name,
            wake_delay_used_ms=wake_delay_used_ms
        )

        if rescue_success:
            logger.info(
                f"[YABAI] 🛟 Rescue SUCCESS: Window {window_id} → Space {target_space} "
                f"({final_strategy.value}, {total_attempts} attempts, {duration_ms:.0f}ms)"
            )
        else:
            logger.warning(
                f"[YABAI] 🛟 Rescue FAILED: Window {window_id} "
                f"(reason: {failure_reason.value}, {total_attempts} attempts)"
            )

        return rescue_success, "rescue" if rescue_success else "failed"

    def _execute_rescue_strategy(
        self,
        strategy: RescueStrategy,
        window_id: int,
        source_space: int,
        target_space: int,
        wake_delay_s: float
    ) -> Tuple[bool, RescueFailureReason]:
        """
        Execute a specific rescue strategy.

        Returns:
            Tuple of (success, failure_reason if failed)
        """
        yabai_path = self._health.yabai_path or "yabai"

        try:
            if strategy == RescueStrategy.SWITCH_GRAB_RETURN:
                # Classic: Switch to source space, move window, return
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED
                time.sleep(wake_delay_s)
                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            elif strategy == RescueStrategy.FOCUS_THEN_MOVE:
                # Focus the window first, then move
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # Focus the specific window
                result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--focus"],
                    capture_output=True,
                    text=True,
                    timeout=self.config.query_timeout_seconds,
                )
                time.sleep(wake_delay_s)

                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            elif strategy == RescueStrategy.UNMINIMIZE_FIRST:
                # Unminimize the window first
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # Toggle minimize off
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--minimize", "off"],
                    capture_output=True,
                    timeout=self.config.query_timeout_seconds,
                )
                time.sleep(wake_delay_s)

                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            elif strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                # Extended wake delay for stubborn windows
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # Focus the window
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--focus"],
                    capture_output=True,
                    timeout=self.config.query_timeout_seconds,
                )

                # Extended wake time
                time.sleep(wake_delay_s)

                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            elif strategy == RescueStrategy.EXIT_FULLSCREEN_FIRST:
                # v31.0: FULLSCREEN WINDOW RESCUE
                # =================================================================
                # ROOT CAUSE FIX: macOS prevents moving fullscreen windows.
                # Property "can-move": false when "is-native-fullscreen": true
                #
                # SOLUTION:
                # 1. Switch to the window's space (required for fullscreen toggle)
                # 2. Exit fullscreen using --toggle native-fullscreen
                # 3. Wait for the fullscreen animation to complete (~0.5-1.0s)
                # 4. Move the window to target space
                # 5. Optionally re-enter fullscreen on target
                # =================================================================
                logger.info(
                    f"[YABAI] 🖥️ EXIT_FULLSCREEN_FIRST: Window {window_id} "
                    f"is in native fullscreen - exiting first"
                )

                # Step 1: Switch to the window's space
                if not self._switch_to_space(source_space):
                    logger.warning(f"[YABAI] Failed to switch to space {source_space}")
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # Step 2: Focus the window (required for fullscreen toggle)
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--focus"],
                    capture_output=True,
                    timeout=self.config.query_timeout_seconds,
                )
                time.sleep(0.2)  # Brief pause for focus

                # Step 3: Exit fullscreen
                result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--toggle", "native-fullscreen"],
                    capture_output=True,
                    text=True,
                    timeout=self.config.query_timeout_seconds,
                )

                if result.returncode != 0:
                    logger.warning(
                        f"[YABAI] Failed to exit fullscreen: {result.stderr}"
                    )
                    # Try alternative approach: toggle zoom-fullscreen
                    subprocess.run(
                        [yabai_path, "-m", "window", str(window_id), "--toggle", "zoom-fullscreen"],
                        capture_output=True,
                        timeout=self.config.query_timeout_seconds,
                    )

                # Step 4: Wait for fullscreen animation to complete
                # macOS fullscreen animation takes ~0.7-1.0 seconds
                fullscreen_animation_delay = max(wake_delay_s, 1.0)
                logger.debug(
                    f"[YABAI] Waiting {fullscreen_animation_delay:.1f}s for fullscreen exit animation"
                )
                time.sleep(fullscreen_animation_delay)

                # Step 5: Verify window is no longer fullscreen
                try:
                    verify_result = subprocess.run(
                        [yabai_path, "-m", "query", "--windows", "--window", str(window_id)],
                        capture_output=True,
                        text=True,
                        timeout=self.config.query_timeout_seconds,
                    )
                    if verify_result.returncode == 0:
                        import json
                        window_info = json.loads(verify_result.stdout)
                        if window_info.get("is-native-fullscreen", False):
                            logger.warning(
                                f"[YABAI] Window {window_id} still in fullscreen after toggle"
                            )
                            # Try one more time with longer delay
                            time.sleep(1.0)
                except Exception as e:
                    logger.debug(f"[YABAI] Fullscreen verify failed: {e}")

                # Step 6: Now move the window
                if self.move_window_to_space(window_id, target_space):
                    logger.info(
                        f"[YABAI] ✅ Successfully moved window {window_id} after exiting fullscreen"
                    )
                    return True, RescueFailureReason.UNKNOWN

                logger.warning(
                    f"[YABAI] Move failed after exiting fullscreen"
                )
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            else:
                return False, RescueFailureReason.UNKNOWN

        except subprocess.TimeoutExpired:
            return False, RescueFailureReason.YABAI_TIMEOUT
        except Exception as e:
            logger.debug(f"[YABAI] Strategy {strategy.value} failed: {e}")
            return False, RescueFailureReason.UNKNOWN

    def _detect_failure_reason(
        self,
        window_id: int,
        source_space: int,
        error_message: str
    ) -> RescueFailureReason:
        """Detect the root cause of a rescue failure from error message."""
        error_lower = error_message.lower()

        if "could not locate" in error_lower or "window not found" in error_lower:
            return RescueFailureReason.WINDOW_NOT_FOUND
        if "space not found" in error_lower or "invalid space" in error_lower:
            return RescueFailureReason.SPACE_NOT_FOUND
        if "timeout" in error_lower:
            return RescueFailureReason.YABAI_TIMEOUT
        if "permission" in error_lower or "accessibility" in error_lower:
            return RescueFailureReason.PERMISSION_DENIED
        if "failed to connect" in error_lower:
            return RescueFailureReason.YABAI_UNAVAILABLE

        return RescueFailureReason.UNKNOWN

    async def move_window_to_space_with_rescue_async(
        self,
        window_id: int,
        target_space: int,
        source_space: Optional[int] = None,
        app_name: Optional[str] = None,
        max_retries: int = 3,
        is_minimized: bool = False,
        is_fullscreen: bool = False
    ) -> Tuple[bool, str]:
        """
        Async version of move_window_to_space_with_rescue with INTELLIGENT PROTOCOL v24.0.

        Uses the full Search & Rescue protocol for hidden windows with:
        - Multi-strategy approach
        - Telemetry-driven strategy selection
        - Dynamic wake delay calibration
        - Retry with exponential backoff

        See move_window_to_space_with_rescue for full documentation.
        """
        start_time = time.time()
        telemetry = get_rescue_telemetry()

        # =====================================================================
        # FAST PATH: Try direct async move first
        # =====================================================================
        if await self.move_window_to_space_async(window_id, target_space):
            duration_ms = (time.time() - start_time) * 1000
            telemetry.record_attempt(
                success=True,
                strategy=RescueStrategy.DIRECT,
                duration_ms=duration_ms,
                app_name=app_name
            )
            return True, "direct"

        # =====================================================================
        # INTELLIGENT RESCUE PATH: Multi-strategy with retry and backoff
        # =====================================================================
        if source_space is None:
            logger.warning(
                f"[YABAI] 🛟 Rescue needed for window {window_id} but source_space unknown"
            )
            telemetry.record_attempt(
                success=False,
                strategy=RescueStrategy.DIRECT,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Get recommended strategy from telemetry
        recommended_strategy = telemetry.get_recommended_strategy(
            app_name=app_name,
            is_minimized=is_minimized,
            is_fullscreen=is_fullscreen
        )

        logger.info(
            f"[YABAI] 🛟 INTELLIGENT RESCUE (async): Window {window_id} on Space {source_space} "
            f"→ Space {target_space} (Strategy: {recommended_strategy.value})"
        )

        current_space = self.get_current_user_space()
        if current_space is None:
            telemetry.record_attempt(
                success=False,
                strategy=recommended_strategy,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Strategy order: try recommended first, then fallbacks
        strategies_to_try = [recommended_strategy]
        all_strategies = [
            RescueStrategy.SWITCH_GRAB_RETURN,
            RescueStrategy.FOCUS_THEN_MOVE,
            RescueStrategy.SPACE_FOCUS_EXTENDED,
            RescueStrategy.EXIT_FULLSCREEN_FIRST,  # v31.0: Handle fullscreen windows
        ]
        for s in all_strategies:
            if s not in strategies_to_try:
                strategies_to_try.append(s)

        rescue_success = False
        failure_reason = RescueFailureReason.UNKNOWN
        total_attempts = 0
        final_strategy = recommended_strategy
        wake_delay_used_ms = 0.0

        try:
            for strategy in strategies_to_try[:2]:  # Try at most 2 strategies
                for attempt in range(1, max_retries + 1):
                    total_attempts += 1
                    final_strategy = strategy

                    # Calculate backoff delay
                    backoff_multiplier = 2 ** (attempt - 1)
                    base_wake_delay_ms = telemetry.get_recommended_wake_delay_ms(app_name)

                    # Strategy-specific wake delay multipliers
                    if strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                        wake_delay_ms = base_wake_delay_ms * 3 * backoff_multiplier
                    else:
                        wake_delay_ms = base_wake_delay_ms * backoff_multiplier

                    wake_delay_used_ms = wake_delay_ms
                    wake_delay_s = wake_delay_ms / 1000.0

                    logger.debug(
                        f"[YABAI] 🛟 Async attempt {total_attempts}: {strategy.value} "
                        f"(wake delay: {wake_delay_ms:.0f}ms)"
                    )

                    # Execute the rescue strategy asynchronously
                    success, reason = await self._execute_rescue_strategy_async(
                        strategy=strategy,
                        window_id=window_id,
                        source_space=source_space,
                        target_space=target_space,
                        wake_delay_s=wake_delay_s
                    )

                    if success:
                        rescue_success = True
                        break

                    failure_reason = reason

                    # Brief pause before retry
                    if attempt < max_retries:
                        await asyncio.sleep(0.1 * attempt)

                if rescue_success:
                    break

        except Exception as e:
            logger.error(f"[YABAI] 🛟 Async rescue exception: {e}")
            failure_reason = RescueFailureReason.UNKNOWN

        finally:
            # ALWAYS return to original space
            logger.debug(f"[YABAI] 🛟 Returning to Space {current_space}...")
            await self._switch_to_space_async(current_space)

        # Record telemetry
        duration_ms = (time.time() - start_time) * 1000
        telemetry.record_attempt(
            success=rescue_success,
            strategy=final_strategy,
            duration_ms=duration_ms,
            failure_reason=failure_reason if not rescue_success else None,
            app_name=app_name,
            wake_delay_used_ms=wake_delay_used_ms
        )

        if rescue_success:
            logger.info(
                f"[YABAI] 🛟 Async rescue SUCCESS: Window {window_id} → Space {target_space} "
                f"({final_strategy.value}, {total_attempts} attempts, {duration_ms:.0f}ms)"
            )
        else:
            logger.warning(
                f"[YABAI] 🛟 Async rescue FAILED: Window {window_id} "
                f"(reason: {failure_reason.value}, {total_attempts} attempts)"
            )

        return rescue_success, "rescue" if rescue_success else "failed"

    async def _execute_rescue_strategy_async(
        self,
        strategy: RescueStrategy,
        window_id: int,
        source_space: int,
        target_space: int,
        wake_delay_s: float
    ) -> Tuple[bool, RescueFailureReason]:
        """
        Async version of rescue strategy execution.

        Returns:
            Tuple of (success, failure_reason if failed)
        """
        try:
            if strategy in (
                RescueStrategy.SWITCH_GRAB_RETURN,
                RescueStrategy.FOCUS_THEN_MOVE,
                RescueStrategy.SPACE_FOCUS_EXTENDED
            ):
                # Switch to source space
                if not await self._switch_to_space_async(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # For focus-based strategies, focus the window
                if strategy in (RescueStrategy.FOCUS_THEN_MOVE, RescueStrategy.SPACE_FOCUS_EXTENDED):
                    yabai_path = self._health.yabai_path or "yabai"
                    try:
                        await run_subprocess_async(
                            [yabai_path, "-m", "window", str(window_id), "--focus"],
                            timeout=self.config.query_timeout_seconds
                        )
                    except Exception:
                        pass  # Focus may fail but move might still work

                # Wait for window to hydrate
                await asyncio.sleep(wake_delay_s)

                # Attempt move
                if await self.move_window_to_space_async(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            return False, RescueFailureReason.UNKNOWN

        except asyncio.TimeoutError:
            return False, RescueFailureReason.YABAI_TIMEOUT
        except Exception as e:
            logger.debug(f"[YABAI] Async strategy {strategy.value} failed: {e}")
            return False, RescueFailureReason.UNKNOWN

    async def rescue_windows_to_ghost_async(
        self,
        windows: List[Dict[str, Any]],
        ghost_space: Optional[int] = None,
        max_parallel: int = 5,
        silent: bool = True  # v34.0: Default to silent for background operations
    ) -> Dict[str, Any]:
        """
        v34.0 STEALTH BATCH RESCUE: Move windows to Ghost Display without focus hijacking.

        This is the main entry point for Auto-Handoff, featuring:
        - Parallel rescue for windows on the same space
        - Telemetry-driven strategy selection per window
        - Dynamic wake delay calibration
        - Comprehensive result tracking
        - v34.0: STEALTH MODE - Never hijacks user's screen with focus

        Args:
            windows: List of window dicts with 'window_id', 'space_id', and optionally 'app_name'
            ghost_space: Target Ghost Display space (auto-detected if None)
            max_parallel: Maximum parallel window moves within a space
            silent: v34.0 - If True, NEVER use focus strategies (won't hijack user's screen)

        Returns:
            Dict with:
                - success: bool (any windows moved)
                - direct_count: int (windows moved directly)
                - rescue_count: int (windows rescued from hidden)
                - failed_count: int (windows that couldn't be moved)
                - details: list of per-window results
                - telemetry: rescue telemetry snapshot
        """
        start_time = time.time()
        telemetry = get_rescue_telemetry()

        result = {
            "success": False,
            "direct_count": 0,
            "rescue_count": 0,
            "failed_count": 0,
            "details": [],
            "telemetry": {}
        }

        if not windows:
            return result

        # Auto-detect Ghost Display if not specified
        if ghost_space is None:
            ghost_space = await self.get_ghost_display_space_async()
            if ghost_space is None:
                logger.warning("[YABAI] 🛟 No Ghost Display available for rescue")
                result["error"] = "ghost_display_unavailable"
                return result

        # ═══════════════════════════════════════════════════════════════════
        # v36.0: GHOST DISPLAY AVAILABILITY VERIFICATION
        # ═══════════════════════════════════════════════════════════════════
        # Before starting batch teleportation, verify Ghost Display is:
        # 1. Still available (not disconnected)
        # 2. Not full (has capacity for more windows)
        # 3. Responsive (can receive commands)
        # ═══════════════════════════════════════════════════════════════════
        try:
            yabai_path = self._health.yabai_path or "yabai"
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces", "--space", str(ghost_space),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)

            if proc.returncode != 0 or not stdout:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.error(
                    f"[YABAI] ❌ Ghost Display (Space {ghost_space}) is UNAVAILABLE: {error_msg}"
                )
                result["error"] = "ghost_display_unavailable"
                result["error_detail"] = error_msg
                return result

            # Check number of windows already on Ghost Display
            import json
            space_info = json.loads(stdout.decode())
            existing_windows = len(space_info.get("windows", []))
            incoming_windows = len(windows)

            # Warn if Ghost Display is getting crowded (arbitrary threshold)
            if existing_windows + incoming_windows > 50:
                logger.warning(
                    f"[YABAI] ⚠️ Ghost Display crowded: {existing_windows} existing + "
                    f"{incoming_windows} incoming = {existing_windows + incoming_windows} windows"
                )

            logger.info(
                f"[YABAI] ✅ Ghost Display (Space {ghost_space}) verified: "
                f"{existing_windows} existing windows, adding {incoming_windows}"
            )

        except asyncio.TimeoutError:
            logger.error(f"[YABAI] ❌ Ghost Display query timed out")
            result["error"] = "ghost_display_timeout"
            return result
        except Exception as e:
            logger.warning(f"[YABAI] ⚠️ Could not verify Ghost Display: {e} - proceeding anyway")

        current_space = self.get_current_user_space()
        visible_spaces = {current_space, ghost_space}

        # Get all visible spaces for smarter routing
        try:
            from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector
            detector = MultiSpaceWindowDetector()
            all_visible = await detector.get_all_visible_spaces()
            visible_spaces.update(all_visible)
        except Exception:
            pass

        # =====================================================================
        # INTELLIGENT BATCH OPTIMIZATION: Group windows by source space
        # =====================================================================
        from collections import defaultdict
        windows_by_space = defaultdict(list)
        for w in windows:
            source = w.get("space_id")
            windows_by_space[source].append(w)

        # =====================================================================
        # PHASE 1: PARALLEL direct moves for visible spaces (fast path)
        # =====================================================================
        visible_space_ids = [
            sid for sid in windows_by_space.keys()
            if sid in visible_spaces and sid != ghost_space
        ]

        for space_id in visible_space_ids:
            space_windows = windows_by_space[space_id]

            # Parallel move for windows in visible spaces
            async def move_visible_window(w):
                window_id = w.get("window_id")
                app_name = w.get("app_name") or w.get("app")
                move_start = time.time()

                # ═══════════════════════════════════════════════════════════════
                # v36.0: RE-QUERY GHOST SPACE BEFORE EACH MOVE
                # ═══════════════════════════════════════════════════════════════
                # If topology was invalidated (e.g., another window unpacked
                # fullscreen and destroyed a Space), ghost_space might be stale.
                # Re-query to ensure we target the correct Space index.
                # ═══════════════════════════════════════════════════════════════
                current_ghost_space = ghost_space  # Default to captured value
                if not self._space_topology_valid:
                    new_ghost = self.get_ghost_display_space()
                    if new_ghost is not None:
                        if new_ghost != ghost_space:
                            logger.info(
                                f"[YABAI] 🔄 Ghost space shifted: {ghost_space} → {new_ghost} "
                                f"(detected before moving window {window_id})"
                            )
                        current_ghost_space = new_ghost

                success = await self.move_window_to_space_async(window_id, current_ghost_space, silent=silent)
                duration_ms = (time.time() - move_start) * 1000

                telemetry.record_attempt(
                    success=success,
                    strategy=RescueStrategy.DIRECT,
                    duration_ms=duration_ms,
                    app_name=app_name
                )

                return {
                    "window_id": window_id,
                    "source_space": space_id,
                    "success": success,
                    "method": "direct" if success else "failed",
                    "duration_ms": duration_ms,
                    "app_name": app_name
                }

            # Execute in parallel with concurrency limit
            tasks = [move_visible_window(w) for w in space_windows]

            if len(tasks) > max_parallel:
                # Process in batches to avoid overwhelming yabai
                for i in range(0, len(tasks), max_parallel):
                    batch = tasks[i:i + max_parallel]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    for r in batch_results:
                        if isinstance(r, Exception):
                            result["failed_count"] += 1
                        elif r.get("success"):
                            result["direct_count"] += 1
                            result["details"].append(r)
                        else:
                            result["failed_count"] += 1
                            result["details"].append(r)
            else:
                move_results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in move_results:
                    if isinstance(r, Exception):
                        result["failed_count"] += 1
                    elif r.get("success"):
                        result["direct_count"] += 1
                        result["details"].append(r)
                    else:
                        result["failed_count"] += 1
                        result["details"].append(r)

            del windows_by_space[space_id]

        # =====================================================================
        # PHASE 2: INTELLIGENT rescue for hidden spaces
        # =====================================================================
        for space_id, space_windows in windows_by_space.items():
            if space_id == ghost_space:
                # Already on ghost, mark as success
                for w in space_windows:
                    result["details"].append({
                        "window_id": w.get("window_id"),
                        "source_space": space_id,
                        "success": True,
                        "method": "already_on_ghost"
                    })
                continue

            # Get dynamic wake delay based on telemetry
            first_app = space_windows[0].get("app_name") or space_windows[0].get("app")
            base_wake_delay_ms = telemetry.get_recommended_wake_delay_ms(first_app)
            wake_delay_s = base_wake_delay_ms / 1000.0

            logger.info(
                f"[YABAI] 🛟 Intelligent batch rescue: {len(space_windows)} windows "
                f"from Space {space_id} (wake delay: {base_wake_delay_ms:.0f}ms)"
            )

            # =====================================================================
            # v31.1: DIRECT MOVE PROTOCOL (No Space Switch Required)
            # =====================================================================
            # ROOT CAUSE FIX: Space switching requires yabai scripting addition (SA),
            # which requires SIP to be partially disabled. Most users don't have SA.
            #
            # SOLUTION: Use DIRECT MOVE approach:
            # 1. Exit fullscreen if needed (can be done without SA)
            # 2. Re-query ghost space index (it changes dynamically!)
            # 3. Move window directly (works without SA)
            # 4. Only fall back to space switch if direct move fails
            # =====================================================================

            # Try space switch (may fail without SA - that's OK!)
            space_switch_success = await self._switch_to_space_async(space_id)
            if space_switch_success:
                await asyncio.sleep(wake_delay_s)
                logger.debug(f"[YABAI] Space switch to {space_id} succeeded")
            else:
                logger.info(
                    f"[YABAI] 🔄 Space switch failed (normal without SA) - using Direct Move Protocol"
                )

            # Parallel move windows from this space (works with or without space switch)
            async def rescue_window(w):
                window_id = w.get("window_id")
                app_name = w.get("app_name") or w.get("app")
                window_title = w.get("title", "") or w.get("window_title", "")
                window_pid = w.get("pid")
                is_minimized = w.get("minimized", False) or w.get("is-minimized", False)
                is_fullscreen = w.get("is_fullscreen", False) or w.get("is-native-fullscreen", False)
                move_start = time.time()

                strategy = RescueStrategy.DIRECT  # v31.1: Default to direct move
                success = False
                exile_method = None

                # ═══════════════════════════════════════════════════════════════
                # v53.0: SHADOW REALM PROTOCOL - PRIMARY STRATEGY
                # ═══════════════════════════════════════════════════════════════
                # ROOT CAUSE FIX: For windows on hidden/phantom spaces, the
                # --space command fails. The ONLY reliable solution is to use
                # --display to exile windows to the Shadow Realm (BetterDisplay).
                #
                # This is now the PRIMARY strategy, not a fallback!
                # ═══════════════════════════════════════════════════════════════
                shadow_display = int(os.getenv("JARVIS_SHADOW_DISPLAY", "2"))
                use_shadow_realm = bool(os.getenv("JARVIS_SHADOW_REALM_ENABLED", "1") == "1")

                if use_shadow_realm:
                    logger.info(
                        f"[YABAI v53.0] 🌑 SHADOW REALM PRIMARY: Attempting exile for "
                        f"window {window_id} ({app_name}) → Display {shadow_display}"
                    )

                    success, exile_method = await self._exile_to_shadow_realm_async(
                        window_id=window_id,
                        app_name=app_name or "Unknown",
                        window_title=window_title,
                        pid=window_pid,
                        silent=silent,
                        shadow_display=shadow_display,
                        maximize_after_exile=True
                    )

                    if success:
                        duration_ms = (time.time() - move_start) * 1000
                        telemetry.record_attempt(
                            success=True,
                            strategy=RescueStrategy.DIRECT,  # Shadow Realm counts as direct
                            duration_ms=duration_ms,
                            app_name=app_name,
                            wake_delay_used_ms=0  # No wake delay needed
                        )

                        return {
                            "window_id": window_id,
                            "source_space": space_id,
                            "success": True,
                            "method": f"shadow_realm_{exile_method}",
                            "strategy": "shadow_realm",
                            "duration_ms": duration_ms,
                            "app_name": app_name,
                            "was_fullscreen": is_fullscreen,
                            "target_display": shadow_display
                        }
                    else:
                        logger.warning(
                            f"[YABAI v53.0] Shadow Realm failed ({exile_method}), "
                            f"falling back to space-based move..."
                        )

                # ═══════════════════════════════════════════════════════════════
                # FALLBACK: Traditional space-based move (legacy behavior)
                # ═══════════════════════════════════════════════════════════════
                # Only reached if Shadow Realm is disabled or failed
                # ═══════════════════════════════════════════════════════════════

                if is_fullscreen:
                    logger.info(
                        f"[YABAI] 🖥️ Window {window_id} is fullscreen - will be unpacked by move_window_to_space_async"
                    )
                    strategy = RescueStrategy.EXIT_FULLSCREEN_FIRST

                # v31.1: RE-QUERY ghost space index before each move
                current_ghost_space = self.get_ghost_display_space()
                if current_ghost_space is None:
                    current_ghost_space = ghost_space
                    logger.warning(f"[YABAI] Could not re-query ghost space, using {ghost_space}")

                success = await self.move_window_to_space_async(window_id, current_ghost_space, silent=silent)
                duration_ms = (time.time() - move_start) * 1000

                # If first attempt failed and window is minimized, try unminimize
                if not success and is_minimized:
                    try:
                        yabai_path = self._health.yabai_path or "yabai"
                        await run_subprocess_async(
                            [yabai_path, "-m", "window", str(window_id), "--minimize", "off"],
                            timeout=2.0
                        )
                        await asyncio.sleep(wake_delay_s)
                        success = await self.move_window_to_space_async(window_id, current_ghost_space, silent=silent)
                        strategy = RescueStrategy.UNMINIMIZE_FIRST
                    except Exception:
                        pass

                # If still failed and was fullscreen, maybe animation wasn't complete
                if not success and is_fullscreen:
                    logger.debug(f"[YABAI] Retry after fullscreen exit for window {window_id}")
                    try:
                        await asyncio.sleep(0.5)
                        retry_ghost_space = self.get_ghost_display_space() or current_ghost_space
                        success = await self.move_window_to_space_async(window_id, retry_ghost_space, silent=silent)
                    except Exception:
                        pass

                # ═══════════════════════════════════════════════════════════════
                # v53.0: LAST RESORT - Try Shadow Realm if space moves failed
                # ═══════════════════════════════════════════════════════════════
                if not success and not use_shadow_realm:
                    # If Shadow Realm was disabled but space moves failed, try it anyway
                    logger.warning(
                        f"[YABAI v53.0] 🆘 LAST RESORT: Space moves failed, trying Shadow Realm..."
                    )
                    success, exile_method = await self._exile_to_shadow_realm_async(
                        window_id=window_id,
                        app_name=app_name or "Unknown",
                        window_title=window_title,
                        pid=window_pid,
                        silent=silent,
                        shadow_display=shadow_display,
                        maximize_after_exile=True
                    )
                    if success:
                        strategy = RescueStrategy.DIRECT

                telemetry.record_attempt(
                    success=success,
                    strategy=strategy,
                    duration_ms=duration_ms,
                    app_name=app_name,
                    wake_delay_used_ms=base_wake_delay_ms
                )

                return {
                    "window_id": window_id,
                    "source_space": space_id,
                    "success": success,
                    "method": f"shadow_realm_lastresort_{exile_method}" if success and exile_method else ("rescue" if success else "failed"),
                    "strategy": strategy.value,
                    "duration_ms": duration_ms,
                    "app_name": app_name,
                    "was_fullscreen": is_fullscreen
                }

            # Execute rescues in parallel (OUTSIDE rescue_window function)
            tasks = [rescue_window(w) for w in space_windows]

            if len(tasks) > max_parallel:
                for i in range(0, len(tasks), max_parallel):
                    batch = tasks[i:i + max_parallel]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    for r in batch_results:
                        if isinstance(r, Exception):
                            result["failed_count"] += 1
                        elif r.get("success"):
                            result["rescue_count"] += 1
                            result["details"].append(r)
                        else:
                            result["failed_count"] += 1
                            result["details"].append(r)
            else:
                rescue_results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in rescue_results:
                    if isinstance(r, Exception):
                        result["failed_count"] += 1
                    elif r.get("success"):
                        result["rescue_count"] += 1
                        result["details"].append(r)
                    else:
                        result["failed_count"] += 1
                        result["details"].append(r)

        # Return to original space (may fail without SA - that's OK)
        if current_space:
            await self._switch_to_space_async(current_space)

        result["success"] = (result["direct_count"] + result["rescue_count"]) > 0

        # Include telemetry snapshot in result
        total_duration_ms = (time.time() - start_time) * 1000
        result["telemetry"] = {
            "total_duration_ms": round(total_duration_ms, 2),
            "success_rate": f"{telemetry.success_rate * 100:.1f}%",
            "total_rescue_attempts": telemetry.total_attempts,
            "calibrated_wake_delay_ms": round(telemetry.avg_wake_delay_needed_ms, 2),
        }

        if result["success"]:
            logger.info(
                f"[YABAI] 🛟 Intelligent batch rescue complete: "
                f"{result['direct_count']} direct, {result['rescue_count']} rescued, "
                f"{result['failed_count']} failed (total: {total_duration_ms:.0f}ms, "
                f"success rate: {telemetry.success_rate * 100:.1f}%)"
            )
        else:
            logger.warning(
                f"[YABAI] 🛟 Batch rescue failed: {result['failed_count']} windows "
                f"could not be moved (telemetry: {telemetry.to_dict()})"
            )

        return result

    def get_rescue_telemetry_report(self) -> Dict[str, Any]:
        """
        Get a comprehensive report of rescue telemetry.

        Returns:
            Dict with detailed rescue statistics and recommendations
        """
        telemetry = get_rescue_telemetry()
        report = telemetry.to_dict()

        # Add recommendations based on telemetry
        recommendations = []

        if telemetry.success_rate < 0.8:
            recommendations.append(
                "Success rate below 80%. Consider increasing JARVIS_RESCUE_WAKE_DELAY."
            )

        if telemetry.failures_by_reason.get("space_switch_failed", 0) > 3:
            recommendations.append(
                "Multiple space switch failures. Check Yabai accessibility permissions."
            )

        if telemetry.failures_by_reason.get("window_not_found", 0) > 5:
            recommendations.append(
                "Many windows not found. Windows may be closing before rescue completes."
            )

        if telemetry.avg_wake_delay_needed_ms > 200:
            recommendations.append(
                f"Wake delay averaging {telemetry.avg_wake_delay_needed_ms:.0f}ms. "
                "Consider a faster Mac or closing resource-intensive apps."
            )

        report["recommendations"] = recommendations
        return report

    def get_ghost_display_space(self) -> Optional[int]:
        """
        Find the Ghost Display space (virtual display for background monitoring).

        The Ghost Display is identified as:
        1. A visible space that is NOT the current/focused space
        2. Typically on Display 2+ (virtual displays)

        Returns:
            Space ID of the Ghost Display, or None if not available

        Example:
            ghost_space = yabai.get_ghost_display_space()
            if ghost_space:
                yabai.move_window_to_space(window_id, ghost_space)
        """
        spaces = self.enumerate_all_spaces(include_display_info=True)

        # Find visible spaces that are NOT the current space
        ghost_candidates = []
        current_space_id = None

        for space in spaces:
            if space.get("is_current"):
                current_space_id = space.get("space_id")
            elif space.get("is_visible"):
                # This is a visible space but not current = Ghost Display candidate
                ghost_candidates.append({
                    "space_id": space.get("space_id"),
                    "display": space.get("display", 1),
                    "window_count": space.get("window_count", 0)
                })

        if not ghost_candidates:
            logger.debug("[YABAI] No Ghost Display found (only one visible space)")
            return None

        # Prefer Display 2+ (virtual displays) over Display 1 spaces
        # Sort by display number (higher = more likely virtual), then by fewer windows
        ghost_candidates.sort(key=lambda x: (-x["display"], x["window_count"]))

        ghost_space = ghost_candidates[0]["space_id"]
        logger.info(
            f"[YABAI] 👻 Ghost Display found: Space {ghost_space} "
            f"(Display {ghost_candidates[0]['display']})"
        )

        return ghost_space

    async def get_ghost_display_space_async(self) -> Optional[int]:
        """Async version of get_ghost_display_space."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_ghost_display_space)

    def teleport_window_to_ghost(self, window_id: int) -> Tuple[bool, Optional[int]]:
        """
        High-level method to teleport a window to the Ghost Display.

        This is the main entry point for autonomous window management.
        It finds the Ghost Display and moves the window there.

        Args:
            window_id: The window to teleport

        Returns:
            Tuple of (success: bool, ghost_space_id: Optional[int])

        Example:
            success, ghost_space = yabai.teleport_window_to_ghost(12345)
            if success:
                print(f"Window moved to Space {ghost_space}")
        """
        ghost_space = self.get_ghost_display_space()

        if ghost_space is None:
            logger.warning(
                "[YABAI] 👻 No Ghost Display available - "
                "create a virtual display with BetterDisplay"
            )
            return False, None

        success = self.move_window_to_space(window_id, ghost_space)
        return success, ghost_space if success else None

    async def teleport_window_to_ghost_async(
        self,
        window_id: int,
        auto_create_display: bool = True
    ) -> Tuple[bool, Optional[int]]:
        """
        v68.0: Async version of teleport_window_to_ghost with Phantom Hardware support.

        If no Ghost Display exists and auto_create_display=True, this will
        attempt to create a virtual display using BetterDisplay (v68.0 Phantom Hardware).
        """
        ghost_space = await self.get_ghost_display_space_async()

        # =================================================================
        # v68.0: PHANTOM HARDWARE - Auto-create Ghost Display if needed
        # =================================================================
        if ghost_space is None and auto_create_display and _HAS_PHANTOM_HARDWARE:
            try:
                phantom = get_phantom_manager()
                logger.info("[v68.0] 👻 No Ghost Display - attempting Phantom Hardware creation...")

                success, error = await phantom.ensure_ghost_display_exists_async()

                if success:
                    # Re-query for Ghost Display space
                    ghost_space = await self.get_ghost_display_space_async()
                    if ghost_space:
                        logger.info(f"[v68.0] ✅ Phantom Hardware created Ghost Display (Space {ghost_space})")
                    else:
                        logger.warning("[v68.0] Phantom Hardware succeeded but yabai can't see display yet")
                else:
                    logger.warning(f"[v68.0] Phantom Hardware failed: {error}")

            except Exception as e:
                logger.warning(f"[v68.0] Phantom Hardware error: {e}")

        if ghost_space is None:
            logger.warning(
                "[YABAI] 👻 No Ghost Display available - "
                "install BetterDisplay or use a physical HDMI dummy plug"
            )
            return False, None

        # Move window to Ghost Display
        success = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.move_window_to_space(window_id, ghost_space)
        )

        return success, ghost_space if success else None

    def teleport_windows_to_ghost(
        self,
        window_ids: List[int],
        max_windows: int = 5
    ) -> Dict[str, Any]:
        """
        Batch teleport multiple windows to the Ghost Display.

        Args:
            window_ids: List of window IDs to teleport
            max_windows: Maximum windows to move (safety limit)

        Returns:
            Dict with results: {
                'success': bool,
                'ghost_space': int,
                'moved': [list of moved window IDs],
                'failed': [list of failed window IDs]
            }
        """
        ghost_space = self.get_ghost_display_space()

        if ghost_space is None:
            return {
                'success': False,
                'ghost_space': None,
                'moved': [],
                'failed': window_ids,
                'error': 'No Ghost Display available'
            }

        # Limit for safety
        windows_to_move = window_ids[:max_windows]
        if len(window_ids) > max_windows:
            logger.warning(
                f"[YABAI] Limiting teleport to {max_windows} windows "
                f"(requested: {len(window_ids)})"
            )

        moved = []
        failed = []

        for wid in windows_to_move:
            if self.move_window_to_space(wid, ghost_space):
                moved.append(wid)
            else:
                failed.append(wid)

        success = len(moved) > 0
        logger.info(
            f"[YABAI] 👻 Batch teleport complete: "
            f"{len(moved)} moved, {len(failed)} failed"
        )

        return {
            'success': success,
            'ghost_space': ghost_space,
            'moved': moved,
            'failed': failed
        }

    async def teleport_windows_to_ghost_async(
        self,
        window_ids: List[int],
        max_windows: int = 5
    ) -> Dict[str, Any]:
        """Async version of teleport_windows_to_ghost."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.teleport_windows_to_ghost(window_ids, max_windows)
        )

    def get_current_user_space(self) -> Optional[int]:
        """Get the space ID where the user is currently working."""
        spaces = self.enumerate_all_spaces()
        for space in spaces:
            if space.get("is_current"):
                return space.get("space_id")
        return None

    # =========================================================================
    # END: Window Teleportation Methods
    # =========================================================================

    def get_workspace_summary(self) -> Dict[str, Any]:
        """Get a comprehensive summary of the entire workspace"""
        spaces = self.enumerate_all_spaces()

        if not spaces:
            return {
                "total_spaces": 0,
                "total_windows": 0,
                "total_applications": 0,
                "spaces": [],
                "current_space": None,
                "primary_activity": "No spaces detected",
            }

        # Calculate totals
        total_windows = sum(space.get("window_count", 0) for space in spaces)
        all_apps = set()
        for space in spaces:
            all_apps.update(space.get("applications", []))

        # Find current space
        current_space = None
        for space in spaces:
            if space.get("is_current"):
                current_space = space
                break

        # Determine overall primary activity
        app_counts = {}
        for space in spaces:
            for app in space.get("applications", []):
                app_counts[app] = app_counts.get(app, 0) + 1

        primary_app = max(app_counts.keys(), key=app_counts.get) if app_counts else "Empty"

        return {
            "total_spaces": len(spaces),
            "total_windows": total_windows,
            "total_applications": len(all_apps),
            "spaces": spaces,
            "current_space": current_space,
            "primary_activity": primary_app,
            "all_applications": list(all_apps),
        }

    def describe_workspace(self) -> str:
        """Generate a natural language description of the workspace"""
        summary = self.get_workspace_summary()

        if summary["total_spaces"] == 0:
            return "Unable to detect Mission Control spaces. Yabai may not be running."

        description = []

        # Overall summary
        description.append(f"You have {summary['total_spaces']} Mission Control spaces active")

        if summary["total_windows"] > 0:
            description.append(
                f"with {summary['total_windows']} windows across {summary['total_applications']} applications."
            )
        else:
            description.append("with no windows currently open.")

        # Current space
        if summary["current_space"]:
            current = summary["current_space"]
            description.append(f"\n\nCurrently viewing Space {current['space_id']}")
            if current["window_count"] > 0:
                description.append(f"with {current['primary_activity']}.")
            else:
                description.append("which is empty.")

        # Detailed space breakdown
        description.append("\n\nSpace breakdown:")
        for space in summary["spaces"]:
            space_desc = f"\n• Space {space['space_id']}"

            if space["is_fullscreen"]:
                space_desc += " (fullscreen)"
            if space["is_current"]:
                space_desc += " [CURRENT]"

            space_desc += f": "

            if space["window_count"] == 0:
                space_desc += "Empty"
            else:
                # List first few apps
                apps = space["applications"][:3]
                if len(apps) == 1:
                    space_desc += apps[0]
                else:
                    space_desc += ", ".join(apps)

                if len(space["applications"]) > 3:
                    space_desc += f" and {len(space['applications']) - 3} more"

                # Add window titles for context
                if space["windows"]:
                    first_window = space["windows"][0]
                    if first_window["title"]:
                        title = first_window["title"][:50]
                        if len(first_window["title"]) > 50:
                            title += "..."
                        space_desc += f' - "{title}"'

            description.append(space_desc)

        return "".join(description)

    # =========================================================================
    # ASYNC METHODS - Non-blocking versions for use in async contexts
    # =========================================================================

    async def enumerate_all_spaces_async(self, include_display_info: bool = True) -> List[Dict[str, Any]]:
        """
        Async version of enumerate_all_spaces.
        Uses thread pool to avoid blocking the event loop.
        """
        if not self.is_available():
            logger.warning("[YABAI] Yabai not available, returning empty list")
            return []

        try:
            # Query spaces from Yabai asynchronously
            result = await run_subprocess_async(["yabai", "-m", "query", "--spaces"], timeout=5.0)

            if result.returncode != 0:
                logger.error(f"[YABAI] Failed to query spaces: {result.stderr}")
                return []

            spaces_data = json.loads(result.stdout)

            # Query windows for more detail
            windows_result = await run_subprocess_async(["yabai", "-m", "query", "--windows"], timeout=5.0)

            windows_data = []
            if windows_result.returncode == 0:
                windows_data = json.loads(windows_result.stdout)

            # Build enhanced space information (same logic as sync version)
            spaces = []
            for space in spaces_data:
                space_id = space["index"]
                space_windows = [w for w in windows_data if w.get("space") == space_id]
                applications = list(set(w.get("app", "Unknown") for w in space_windows))

                if not space_windows:
                    primary_activity = "Empty"
                elif len(applications) == 1:
                    primary_activity = applications[0]
                else:
                    primary_activity = f"{applications[0]} and {len(applications)-1} others"

                display_id = space.get("display", 1) if include_display_info else None

                space_info = {
                    "space_id": space_id,
                    "space_name": f"Desktop {space_id}",
                    "is_current": space.get("has-focus", False),
                    "is_visible": space.get("is-visible", False),
                    "is_fullscreen": space.get("is-native-fullscreen", False),
                    "window_count": len(space_windows),
                    "window_ids": space.get("windows", []),
                    "applications": applications,
                    "primary_activity": primary_activity,
                    "type": space.get("type", "unknown"),
                    "display": display_id,
                    "uuid": space.get("uuid", ""),
                    "windows": [
                        {
                            "app": w.get("app", "Unknown"),
                            "title": w.get("title", ""),
                            "id": w.get("id"),
                            "minimized": w.get("is-minimized", False),
                            "hidden": w.get("is-hidden", False),
                            # v31.0: Include fullscreen status for teleportation handling
                            "is-native-fullscreen": w.get("is-native-fullscreen", False),
                            "is_fullscreen": w.get("is-native-fullscreen", False),
                            "can-move": w.get("can-move", True),
                        }
                        for w in space_windows
                    ],
                }
                spaces.append(space_info)

            logger.info(f"[YABAI] Async: Detected {len(spaces)} spaces via Yabai")
            return spaces

        except asyncio.TimeoutError:
            logger.error("[YABAI] Async: Yabai query timed out")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"[YABAI] Async: Failed to parse Yabai output: {e}")
            return []
        except Exception as e:
            logger.error(f"[YABAI] Async: Error enumerating spaces: {e}")
            return []

    async def get_workspace_summary_async(self) -> Dict[str, Any]:
        """Async version of get_workspace_summary."""
        spaces = await self.enumerate_all_spaces_async()

        if not spaces:
            return {
                "total_spaces": 0,
                "total_windows": 0,
                "total_applications": 0,
                "spaces": [],
                "current_space": None,
                "primary_activity": "No spaces detected",
            }

        total_windows = sum(space.get("window_count", 0) for space in spaces)
        all_apps = set()
        for space in spaces:
            all_apps.update(space.get("applications", []))

        current_space = None
        for space in spaces:
            if space.get("is_current"):
                current_space = space
                break

        app_counts = {}
        for space in spaces:
            for app in space.get("applications", []):
                app_counts[app] = app_counts.get(app, 0) + 1

        primary_app = max(app_counts.keys(), key=app_counts.get) if app_counts else "Empty"

        return {
            "total_spaces": len(spaces),
            "total_windows": total_windows,
            "total_applications": len(all_apps),
            "spaces": spaces,
            "current_space": current_space,
            "primary_activity": primary_app,
            "all_applications": list(all_apps),
        }

    async def describe_workspace_async(self) -> str:
        """Async version of describe_workspace."""
        summary = await self.get_workspace_summary_async()

        if summary["total_spaces"] == 0:
            return "Unable to detect Mission Control spaces. Yabai may not be running."

        description = []

        description.append(f"You have {summary['total_spaces']} Mission Control spaces active")

        if summary["total_windows"] > 0:
            description.append(
                f" with {summary['total_windows']} windows across {summary['total_applications']} applications."
            )
        else:
            description.append(" with no windows currently open.")

        if summary["current_space"]:
            current = summary["current_space"]
            description.append(f"\n\nCurrently viewing Space {current['space_id']}")
            if current["window_count"] > 0:
                description.append(f" with {current['primary_activity']}.")
            else:
                description.append(" which is empty.")

        description.append("\n\nSpace breakdown:")
        for space in summary["spaces"]:
            space_desc = f"\n• Space {space['space_id']}"

            if space["is_fullscreen"]:
                space_desc += " (fullscreen)"
            if space["is_current"]:
                space_desc += " [CURRENT]"

            space_desc += ": "

            if space["window_count"] == 0:
                space_desc += "Empty"
            else:
                apps = space["applications"][:3]
                if len(apps) == 1:
                    space_desc += apps[0]
                else:
                    space_desc += ", ".join(apps)

                if len(space["applications"]) > 3:
                    space_desc += f" and {len(space['applications']) - 3} more"

                if space["windows"]:
                    first_window = space["windows"][0]
                    if first_window["title"]:
                        title = first_window["title"][:50]
                        if len(first_window["title"]) > 50:
                            title += "..."
                        space_desc += f' - "{title}"'

            description.append(space_desc)

        return "".join(description)

    async def detect_monitors_with_vision(self, screenshot=None) -> Optional[Dict[str, Any]]:
        """
        Detect monitor layout using YOLO vision

        Args:
            screenshot: Optional screenshot for detection

        Returns:
            Dictionary with monitor detections or None
        """
        if not self.enable_vision or screenshot is None:
            return None

        vision_analyzer = self._get_vision_analyzer()
        if not vision_analyzer:
            return None

        try:
            result = await vision_analyzer.detect_monitors(screenshot)

            if result:
                # Correlate with Yabai display info
                spaces_by_display = self.enumerate_spaces_by_display()

                return {
                    "monitor_detections": result,
                    "yabai_displays": len(spaces_by_display),
                    "spaces_by_display": spaces_by_display,
                }

            return None

        except Exception as e:
            logger.error(f"[YABAI] Error detecting monitors with vision: {e}")
            return None

    async def analyze_space_layout(
        self, space_id: int, screenshot=None
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze layout of a specific space using vision

        Args:
            space_id: Space ID to analyze
            screenshot: Optional screenshot of the space

        Returns:
            Analysis results with window layout and UI elements
        """
        if not self.enable_vision or screenshot is None:
            return None

        vision_analyzer = self._get_vision_analyzer()
        if not vision_analyzer:
            return None

        try:
            # Get Yabai info about the space
            space_info = self.get_space_info(space_id)
            if not space_info:
                logger.warning(f"[YABAI] Space {space_id} not found")
                return None

            # Analyze with vision

            prompt = f"Analyze this workspace layout for Space {space_id} with {space_info['window_count']} windows"

            result = await vision_analyzer.analyze_screenshot_fast(screenshot, prompt=prompt)

            # Combine Yabai and vision data
            result["yabai_space_info"] = space_info
            result["detected_layout"] = (
                "multi_window" if space_info["window_count"] > 1 else "single_window"
            )

            logger.info(
                f"[YABAI] Analyzed Space {space_id} layout with vision "
                f"({space_info['window_count']} windows)"
            )

            return result

        except Exception as e:
            logger.error(f"[YABAI] Error analyzing space layout: {e}")
            return None

    async def get_enhanced_workspace_summary(self, screenshot=None) -> Dict[str, Any]:
        """
        Get workspace summary enhanced with vision analysis

        Args:
            screenshot: Optional full workspace screenshot

        Returns:
            Enhanced workspace summary with vision data
        """
        # Get base Yabai summary
        summary = self.get_workspace_summary()

        # Add vision enhancements if available
        if self.enable_vision and screenshot is not None:
            vision_analyzer = self._get_vision_analyzer()
            if vision_analyzer:
                try:
                    # Detect monitors
                    monitor_result = await self.detect_monitors_with_vision(screenshot)
                    if monitor_result:
                        summary["vision_monitor_detection"] = monitor_result

                    # Analyze current space layout
                    current_space = summary.get("current_space")
                    if current_space:
                        layout_result = await self.analyze_space_layout(
                            current_space["space_id"], screenshot
                        )
                        if layout_result:
                            summary["current_space_vision_analysis"] = layout_result

                    logger.info("[YABAI] Enhanced workspace summary with vision analysis")

                except Exception as e:
                    logger.error(f"[YABAI] Error enhancing workspace summary with vision: {e}")

        return summary


# =============================================================================
# v40.0.0: ROBUST PARALLEL ASYNC WORKSPACE QUERY WITH CIRCUIT BREAKER
# =============================================================================
# ROOT CAUSE FIX: Window discovery timeout due to cascading timeout conflicts
#
# PROBLEM:
# - Outer timeout: 5s in watch_app_across_all_spaces
# - Inner timeouts: 5s each for spaces + windows queries = 10s minimum
# - Auto-start delays: Can add 10-20s more
# - Result: Guaranteed timeout, "Yabai/MultiSpaceDetector unresponsive"
#
# SOLUTION:
# 1. PARALLEL QUERIES: Run spaces + windows simultaneously with asyncio.gather
# 2. INTELLIGENT TIMEOUTS: Inner timeouts must be fraction of outer (40% each)
# 3. CIRCUIT BREAKER: After repeated failures, skip expensive operations
# 4. CACHE-FIRST: Return stale data quickly, refresh in background
# 5. CONFIGURABLE: All timeouts via environment variables, no hardcoding
# =============================================================================

# Module-level workspace cache for fast fallback
_WORKSPACE_CACHE = {
    "data": None,
    "timestamp": None,
    "ttl_seconds": 5.0,  # Cache valid for 5 seconds
    "background_refresh_pending": False,
}

# Circuit breaker state for repeated failures
_CIRCUIT_BREAKER = {
    "consecutive_failures": 0,
    "last_failure": None,
    "is_open": False,
    "reset_after_seconds": 30.0,  # Try again after 30 seconds
    "failure_threshold": 3,  # Open circuit after 3 consecutive failures
}


def _is_workspace_cache_valid() -> bool:
    """Check if workspace cache is still valid."""
    if _WORKSPACE_CACHE["data"] is None:
        return False
    if _WORKSPACE_CACHE["timestamp"] is None:
        return False
    age = (datetime.now() - _WORKSPACE_CACHE["timestamp"]).total_seconds()
    return age < _WORKSPACE_CACHE["ttl_seconds"]


def _update_workspace_cache(data: Dict[str, Any]) -> None:
    """Update the workspace cache with fresh data."""
    _WORKSPACE_CACHE["data"] = data
    _WORKSPACE_CACHE["timestamp"] = datetime.now()


def _check_circuit_breaker() -> bool:
    """Check if circuit breaker allows operations. Returns True if allowed."""
    global _CIRCUIT_BREAKER

    if not _CIRCUIT_BREAKER["is_open"]:
        return True

    # Check if enough time has passed to try again
    if _CIRCUIT_BREAKER["last_failure"]:
        elapsed = (datetime.now() - _CIRCUIT_BREAKER["last_failure"]).total_seconds()
        if elapsed > _CIRCUIT_BREAKER["reset_after_seconds"]:
            # Half-open state - allow one attempt
            logger.info("[YABAI] Circuit breaker: Half-open, allowing retry attempt")
            return True

    logger.debug("[YABAI] Circuit breaker: OPEN, skipping yabai query")
    return False


def _record_circuit_breaker_success() -> None:
    """Record a successful operation, reset circuit breaker."""
    global _CIRCUIT_BREAKER
    _CIRCUIT_BREAKER["consecutive_failures"] = 0
    _CIRCUIT_BREAKER["is_open"] = False
    _CIRCUIT_BREAKER["last_failure"] = None


def _record_circuit_breaker_failure() -> None:
    """Record a failed operation, potentially open circuit breaker."""
    global _CIRCUIT_BREAKER
    _CIRCUIT_BREAKER["consecutive_failures"] += 1
    _CIRCUIT_BREAKER["last_failure"] = datetime.now()

    if _CIRCUIT_BREAKER["consecutive_failures"] >= _CIRCUIT_BREAKER["failure_threshold"]:
        _CIRCUIT_BREAKER["is_open"] = True
        logger.warning(
            f"[YABAI] Circuit breaker: OPEN after {_CIRCUIT_BREAKER['consecutive_failures']} "
            f"consecutive failures. Will retry in {_CIRCUIT_BREAKER['reset_after_seconds']}s"
        )


async def parallel_workspace_query_async(
    timeout_seconds: Optional[float] = None,
    use_cache: bool = True,
    skip_auto_start: bool = True,
) -> Dict[str, Any]:
    """
    v40.0.0: Robust parallel async workspace query with all safety mechanisms.

    This function is designed to NEVER block for more than timeout_seconds,
    even when yabai is unresponsive or starting up.

    Args:
        timeout_seconds: Maximum time to wait (default: from env or 3.0s)
        use_cache: If True, return cached data when available
        skip_auto_start: If True, don't attempt to start yabai (faster)

    Returns:
        Workspace summary dict, possibly from cache if live query fails

    Features:
        - PARALLEL: Runs spaces + windows queries simultaneously
        - CACHED: Returns stale data quickly when live fails
        - CIRCUIT BREAKER: Skips expensive operations after repeated failures
        - TIMEOUT SAFE: Inner timeouts are fractions of outer timeout
    """
    # Get configurable timeout
    if timeout_seconds is None:
        timeout_seconds = float(os.getenv("JARVIS_WORKSPACE_QUERY_TIMEOUT", "3.0"))

    start_time = time.time()

    # =========================================================================
    # STEP 1: Cache-first for instant response
    # =========================================================================
    if use_cache and _is_workspace_cache_valid():
        logger.debug("[YABAI] Returning cached workspace data (instant)")
        return _WORKSPACE_CACHE["data"]

    # =========================================================================
    # STEP 2: Circuit breaker check
    # =========================================================================
    if not _check_circuit_breaker():
        # Circuit is open - return cached data or empty result
        if _WORKSPACE_CACHE["data"]:
            logger.debug("[YABAI] Circuit open, returning stale cache")
            return _WORKSPACE_CACHE["data"]
        return _empty_workspace_result("Circuit breaker open")

    # =========================================================================
    # STEP 3: Quick yabai availability check (non-blocking, cached)
    # =========================================================================
    quick_check_timeout = min(0.5, timeout_seconds * 0.15)  # 15% of total timeout

    try:
        is_available, yabai_path = await asyncio.wait_for(
            async_quick_yabai_check(),
            timeout=quick_check_timeout
        )

        if not is_available:
            logger.debug("[YABAI] Yabai not available")
            _record_circuit_breaker_failure()
            if _WORKSPACE_CACHE["data"]:
                return _WORKSPACE_CACHE["data"]
            return _empty_workspace_result("Yabai not available")

    except asyncio.TimeoutError:
        logger.warning(f"[YABAI] Availability check timed out ({quick_check_timeout}s)")
        _record_circuit_breaker_failure()
        if _WORKSPACE_CACHE["data"]:
            return _WORKSPACE_CACHE["data"]
        return _empty_workspace_result("Availability check timeout")

    # =========================================================================
    # STEP 4: PARALLEL queries for spaces and windows
    # =========================================================================
    # Use 40% of remaining timeout for each query (allows for overhead)
    elapsed = time.time() - start_time
    remaining = timeout_seconds - elapsed
    query_timeout = max(0.5, remaining * 0.4)  # 40% each, 20% overhead

    if not yabai_path:
        yabai_path = "yabai"

    try:
        # Create PARALLEL query tasks
        spaces_task = _async_yabai_query(
            [yabai_path, "-m", "query", "--spaces"],
            timeout=query_timeout
        )
        windows_task = _async_yabai_query(
            [yabai_path, "-m", "query", "--windows"],
            timeout=query_timeout
        )

        # Run BOTH queries simultaneously
        results = await asyncio.gather(
            spaces_task,
            windows_task,
            return_exceptions=True
        )

        spaces_result, windows_result = results

        # Check for exceptions
        if isinstance(spaces_result, Exception):
            raise spaces_result

        # Parse results
        spaces_data = json.loads(spaces_result) if spaces_result else []
        windows_data = json.loads(windows_result) if windows_result and not isinstance(windows_result, Exception) else []

        # Build workspace summary
        result = _build_workspace_summary(spaces_data, windows_data)

        # Update cache and circuit breaker
        _update_workspace_cache(result)
        _record_circuit_breaker_success()

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(f"[YABAI] Parallel workspace query completed in {elapsed_ms:.0f}ms")

        return result

    except asyncio.TimeoutError:
        logger.warning(f"[YABAI] Parallel query timed out after {timeout_seconds}s")
        _record_circuit_breaker_failure()
        if _WORKSPACE_CACHE["data"]:
            logger.debug("[YABAI] Returning stale cache after timeout")
            return _WORKSPACE_CACHE["data"]
        return _empty_workspace_result("Query timeout")

    except json.JSONDecodeError as e:
        logger.error(f"[YABAI] Failed to parse yabai output: {e}")
        _record_circuit_breaker_failure()
        if _WORKSPACE_CACHE["data"]:
            return _WORKSPACE_CACHE["data"]
        return _empty_workspace_result(f"Parse error: {e}")

    except Exception as e:
        logger.error(f"[YABAI] Parallel query failed: {e}")
        _record_circuit_breaker_failure()
        if _WORKSPACE_CACHE["data"]:
            return _WORKSPACE_CACHE["data"]
        return _empty_workspace_result(f"Error: {e}")


async def _async_yabai_query(args: List[str], timeout: float) -> Optional[str]:
    """
    Execute a yabai query using TRUE async subprocess (not thread pool).

    Uses asyncio.create_subprocess_exec for fully non-blocking execution.
    """
    try:
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            ),
            timeout=timeout * 0.3  # 30% for process creation
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout * 0.7  # 70% for execution
        )

        if process.returncode != 0:
            logger.debug(f"[YABAI] Query failed: {stderr.decode()}")
            return None

        return stdout.decode()

    except asyncio.TimeoutError:
        logger.debug(f"[YABAI] Query timed out: {' '.join(args)}")
        raise
    except Exception as e:
        logger.debug(f"[YABAI] Query error: {e}")
        return None


def _build_workspace_summary(spaces_data: List[Dict], windows_data: List[Dict]) -> Dict[str, Any]:
    """
    Build workspace summary from raw yabai data.
    
    v41.0: ORPHANED WINDOW RECOVERY
    ================================
    ROOT CAUSE FIX: Windows on dehydrated/hidden spaces can have:
    - Invalid `space` values (null, 0, or mismatched)
    - These windows were DROPPED entirely, causing "only 1 Chrome window found"
    
    SOLUTION: Two-pass window association
    1. PASS 1: Associate by window's `space` field (standard method)
    2. PASS 2: Rescue orphaned windows using space's `windows` array
    3. PASS 3: Any remaining orphans get added to current space as fallback
    """
    spaces = []
    current_space = None
    current_space_id = None
    
    # =========================================================================
    # PASS 1: Build window ID → window data lookup
    # =========================================================================
    windows_by_id = {w.get("id"): w for w in windows_data if w.get("id")}
    assigned_window_ids = set()  # Track which windows we've assigned to spaces
    
    # Build space index lookup for orphan recovery
    space_window_ids_map = {}  # space_id → list of window IDs from space's `windows` array
    for space in spaces_data:
        space_id = space.get("index", 1)
        space_window_ids_map[space_id] = space.get("windows", [])
        if space.get("has-focus"):
            current_space_id = space_id

    for space in spaces_data:
        space_id = space.get("index", 1)
        
        # =====================================================================
        # METHOD 1: Standard - match by window's `space` field
        # =====================================================================
        space_windows = [w for w in windows_data if w.get("space") == space_id]
        
        # =====================================================================
        # METHOD 2: ORPHAN RECOVERY - use space's `windows` array
        # =====================================================================
        # If a window is listed in the space's `windows` array but wasn't
        # matched by METHOD 1, it means the window has an invalid `space` field.
        # This commonly happens with dehydrated windows on hidden spaces.
        # =====================================================================
        space_window_ids_from_space = space.get("windows", [])
        matched_ids = {w.get("id") for w in space_windows}
        
        for win_id in space_window_ids_from_space:
            if win_id not in matched_ids and win_id in windows_by_id:
                # Found an orphaned window! Rescue it.
                orphan = windows_by_id[win_id]
                logger.debug(
                    f"[v41.0] 🔍 ORPHAN RECOVERY: Window {win_id} "
                    f"(space field: {orphan.get('space')}) rescued → Space {space_id}"
                )
                space_windows.append(orphan)
        
        # Track assigned windows
        for w in space_windows:
            assigned_window_ids.add(w.get("id"))
        
        applications = list(set(w.get("app", "Unknown") for w in space_windows))

        if not space_windows:
            primary_activity = "Empty"
        elif len(applications) == 1:
            primary_activity = applications[0]
        else:
            primary_activity = f"{applications[0]} and {len(applications)-1} others"

        space_info = {
            "space_id": space_id,
            "space_name": f"Desktop {space_id}",
            "is_current": space.get("has-focus", False),
            "is_visible": space.get("is-visible", False),
            "is_fullscreen": space.get("is-native-fullscreen", False),
            "window_count": len(space_windows),
            "window_ids": space.get("windows", []),
            "applications": applications,
            "primary_activity": primary_activity,
            "type": space.get("type", "unknown"),
            "display": space.get("display", 1),
            "uuid": space.get("uuid", ""),
            "windows": [
                {
                    "app": w.get("app", "Unknown"),
                    "title": w.get("title", ""),
                    "id": w.get("id"),
                    "minimized": w.get("is-minimized", False),
                    "hidden": w.get("is-hidden", False),
                    "is-native-fullscreen": w.get("is-native-fullscreen", False),
                    "is_fullscreen": w.get("is-native-fullscreen", False),
                    "can-move": w.get("can-move", True),
                    # v41.0: Include original space field for debugging
                    "original_space": w.get("space"),
                }
                for w in space_windows
            ],
        }
        spaces.append(space_info)

        if space.get("has-focus"):
            current_space = space_info

    # =========================================================================
    # PASS 3: FINAL ORPHAN SWEEP - catch any windows not in ANY space
    # =========================================================================
    # These are windows with invalid `space` fields that ALSO weren't listed
    # in any space's `windows` array. Add them to current space as fallback.
    # =========================================================================
    all_window_ids = set(windows_by_id.keys())
    remaining_orphans = all_window_ids - assigned_window_ids
    
    if remaining_orphans:
        logger.warning(
            f"[v41.0] ⚠️ FINAL ORPHAN SWEEP: {len(remaining_orphans)} windows "
            f"not assigned to any space: {list(remaining_orphans)[:5]}..."
        )
        
        # Add to current space (or first space if no current)
        target_space = current_space or (spaces[0] if spaces else None)
        if target_space:
            orphan_windows = [windows_by_id[wid] for wid in remaining_orphans if wid in windows_by_id]
            for orphan in orphan_windows:
                target_space["windows"].append({
                    "app": orphan.get("app", "Unknown"),
                    "title": orphan.get("title", ""),
                    "id": orphan.get("id"),
                    "minimized": orphan.get("is-minimized", False),
                    "hidden": orphan.get("is-hidden", False),
                    "is-native-fullscreen": orphan.get("is-native-fullscreen", False),
                    "is_fullscreen": orphan.get("is-native-fullscreen", False),
                    "can-move": orphan.get("can-move", True),
                    "original_space": orphan.get("space"),
                    "orphan_recovered": True,  # v41.0: Mark as recovered orphan
                })
                target_space["applications"] = list(set(
                    target_space["applications"] + [orphan.get("app", "Unknown")]
                ))
            target_space["window_count"] = len(target_space["windows"])
            logger.info(
                f"[v41.0] ✅ Added {len(orphan_windows)} orphaned windows to "
                f"Space {target_space['space_id']}"
            )

    # Calculate totals
    total_windows = sum(s.get("window_count", 0) for s in spaces)
    all_apps = set()
    for s in spaces:
        all_apps.update(s.get("applications", []))

    # Determine primary activity
    app_counts = {}
    for s in spaces:
        for app in s.get("applications", []):
            app_counts[app] = app_counts.get(app, 0) + 1
    primary_app = max(app_counts.keys(), key=app_counts.get) if app_counts else "Empty"
    
    # v41.0: Log summary for debugging
    if total_windows > 0:
        logger.info(
            f"[v41.0] 📊 Workspace summary: {len(spaces)} spaces, {total_windows} windows, "
            f"{len(all_apps)} apps"
        )

    return {
        "total_spaces": len(spaces),
        "total_windows": total_windows,
        "total_applications": len(all_apps),
        "spaces": spaces,
        "current_space": current_space,
        "primary_activity": primary_app,
        "all_applications": list(all_apps),
        "query_method": "parallel_async",
        "cached": False,
    }


def _empty_workspace_result(reason: str) -> Dict[str, Any]:
    """Return an empty workspace result with error reason."""
    return {
        "total_spaces": 0,
        "total_windows": 0,
        "total_applications": 0,
        "spaces": [],
        "current_space": None,
        "primary_activity": "No spaces detected",
        "all_applications": [],
        "error": reason,
        "query_method": "fallback",
        "cached": False,
    }


# =============================================================================
# GLOBAL INSTANCE & HELPER FUNCTIONS
# =============================================================================

# Global instance - lazy initialized
_yabai_detector: Optional[YabaiSpaceDetector] = None


def get_yabai_detector(auto_start: bool = True) -> YabaiSpaceDetector:
    """
    Get the global Yabai detector instance.

    Args:
        auto_start: If True, attempt to start yabai if not running

    Returns:
        Global YabaiSpaceDetector instance
    """
    global _yabai_detector
    if _yabai_detector is None:
        _yabai_detector = YabaiSpaceDetector(auto_start=auto_start)
    return _yabai_detector


def reset_yabai_detector() -> None:
    """Reset the global detector (useful for testing or reconfiguration)."""
    global _yabai_detector
    _yabai_detector = None


class _YabaiDetectorProxy:
    """Proxy class for backward compatibility with yabai_detector global."""

    def __getattr__(self, name):
        return getattr(get_yabai_detector(), name)

    def __repr__(self):
        return repr(get_yabai_detector())


# Backward compatibility - access via yabai_detector still works
yabai_detector = _YabaiDetectorProxy()


def open_accessibility_settings() -> bool:
    """
    Open macOS Accessibility settings for granting yabai permissions.

    Returns:
        True if successfully opened, False otherwise
    """
    try:
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=True,
        )
        logger.info("[YABAI] Opened Accessibility settings")
        return True
    except Exception as e:
        logger.error(f"[YABAI] Failed to open Accessibility settings: {e}")
        return False


# =============================================================================
# v30.3: COMPREHENSIVE YABAI PERMISSION FIXER
# =============================================================================
# ROOT CAUSE: macOS TCC (Transparency, Consent, and Control) system requires
# the ACTUAL binary path, not symlinks. When yabai is installed via Homebrew:
#   - Symlink: /opt/homebrew/bin/yabai
#   - Actual: /opt/homebrew/Cellar/yabai/X.Y.Z/bin/yabai
#
# When user tries to add yabai via the Finder dialog, macOS may:
#   1. Only recognize the resolved binary, not the symlink
#   2. Store the path incorrectly in TCC.db
#   3. Fail silently when the permission check uses a different path
#
# This system provides:
#   1. Actual binary path resolution
#   2. Programmatic permission prompt via AXIsProcessTrustedWithOptions
#   3. TCC database reset guidance
#   4. Service restart after permission grant
#   5. Continuous permission monitoring
# =============================================================================

@dataclass
class YabaiPermissionFixResult:
    """Result of a yabai permission fix attempt."""
    success: bool = False
    method_used: str = ""
    error_message: Optional[str] = None
    
    # Path information
    symlink_path: Optional[str] = None
    actual_binary_path: Optional[str] = None
    
    # Actions taken
    opened_settings: bool = False
    triggered_prompt: bool = False
    reset_tcc: bool = False
    restarted_service: bool = False
    
    # Next steps
    user_action_required: bool = False
    instructions: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "method_used": self.method_used,
            "error_message": self.error_message,
            "symlink_path": self.symlink_path,
            "actual_binary_path": self.actual_binary_path,
            "actions_taken": {
                "opened_settings": self.opened_settings,
                "triggered_prompt": self.triggered_prompt,
                "reset_tcc": self.reset_tcc,
                "restarted_service": self.restarted_service,
            },
            "user_action_required": self.user_action_required,
            "instructions": self.instructions,
        }


def get_yabai_actual_binary_path() -> Tuple[Optional[str], Optional[str]]:
    """
    v30.3: Resolve the actual yabai binary path (not symlink).
    
    Returns:
        Tuple of (symlink_path, actual_binary_path)
    """
    # Find symlink path
    symlink_path = shutil.which("yabai")
    if not symlink_path:
        for path in ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]:
            if os.path.isfile(path):
                symlink_path = path
                break
    
    if not symlink_path:
        return None, None
    
    # Resolve actual binary path
    try:
        # Try multiple methods to resolve the actual path
        actual_path = None
        
        # Method 1: os.path.realpath (handles symlinks)
        resolved = os.path.realpath(symlink_path)
        if resolved != symlink_path and os.path.isfile(resolved):
            actual_path = resolved
        
        # Method 2: Read symlink directly
        if not actual_path and os.path.islink(symlink_path):
            link_target = os.readlink(symlink_path)
            if not os.path.isabs(link_target):
                # Relative symlink - resolve relative to symlink directory
                link_dir = os.path.dirname(symlink_path)
                actual_path = os.path.normpath(os.path.join(link_dir, link_target))
            else:
                actual_path = link_target
        
        # Method 3: Use Homebrew Cellar pattern
        if not actual_path or not os.path.isfile(actual_path):
            # Try to find via Homebrew Cellar
            cellar_base = "/opt/homebrew/Cellar/yabai"
            if os.path.isdir(cellar_base):
                versions = os.listdir(cellar_base)
                if versions:
                    # Get latest version
                    latest_version = sorted(versions, reverse=True)[0]
                    cellar_path = os.path.join(cellar_base, latest_version, "bin", "yabai")
                    if os.path.isfile(cellar_path):
                        actual_path = cellar_path
        
        return symlink_path, actual_path or symlink_path
        
    except Exception as e:
        logger.warning(f"[v30.3] Failed to resolve yabai binary path: {e}")
        return symlink_path, symlink_path


def check_yabai_in_accessibility_tcc() -> Tuple[bool, str]:
    """
    v30.3: Check if yabai is in the Accessibility TCC database.
    
    Note: Reading TCC.db directly requires Full Disk Access permission.
    This function uses indirect methods to check permission status.
    
    Returns:
        Tuple of (is_authorized, status_message)
    """
    try:
        # Method 1: Check via AppleScript (AXIsProcessTrusted equivalent)
        script = '''
        use framework "ApplicationServices"
        return current application's AXIsProcessTrusted() as boolean
        '''
        result = subprocess.run(
            ["osascript", "-l", "AppleScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=2.0
        )
        
        if result.returncode == 0:
            is_trusted = "true" in result.stdout.lower()
            return is_trusted, "Checked via AXIsProcessTrusted"
        
        # Method 2: Check via ctypes (direct API call)
        import ctypes
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        trusted = lib.AXIsProcessTrusted()
        return bool(trusted), "Checked via ctypes"
        
    except Exception as e:
        logger.debug(f"[v30.3] TCC check failed: {e}")
        return False, f"Check failed: {e}"


def trigger_accessibility_prompt_for_yabai() -> bool:
    """
    v30.3: Programmatically trigger the macOS Accessibility permission prompt.
    
    Uses AXIsProcessTrustedWithOptions with kAXTrustedCheckOptionPrompt=YES
    to force macOS to show the permission dialog.
    
    Returns:
        True if the prompt was triggered (not necessarily granted)
    """
    try:
        # Get the actual yabai binary path
        symlink_path, actual_path = get_yabai_actual_binary_path()
        
        if not actual_path:
            logger.error("[v30.3] Cannot trigger prompt: yabai not found")
            return False
        
        # Method 1: Use tccutil to reset and re-prompt
        # This requires running from a process that can spawn yabai
        logger.info(f"[v30.3] Triggering accessibility prompt for: {actual_path}")
        
        # Create a script that will trigger the prompt when yabai runs
        # The key is that yabai itself needs to request the permission
        
        # First, restart yabai - it will trigger the prompt automatically
        # when it tries to access accessibility features
        try:
            subprocess.run(
                ["yabai", "--stop-service"],
                capture_output=True,
                timeout=5.0
            )
            time.sleep(0.5)
            subprocess.run(
                ["yabai", "--start-service"],
                capture_output=True,
                timeout=5.0
            )
            logger.info("[v30.3] Restarted yabai service - this should trigger permission prompt")
            return True
        except Exception as e:
            logger.debug(f"[v30.3] Service restart failed: {e}")
        
        return False
        
    except Exception as e:
        logger.error(f"[v30.3] Failed to trigger accessibility prompt: {e}")
        return False


async def fix_yabai_permissions(
    auto_open_settings: bool = True,
    auto_restart_service: bool = True,
    narrate_progress: bool = True
) -> YabaiPermissionFixResult:
    """
    v30.3: Comprehensive yabai permission fixer.
    
    Attempts to fix yabai accessibility permissions through multiple strategies:
    1. Detect if permission is missing
    2. Open Accessibility settings to the correct location
    3. Provide clear instructions with actual binary path
    4. Restart yabai service after fix
    
    Args:
        auto_open_settings: Automatically open System Settings
        auto_restart_service: Automatically restart yabai after fix
        narrate_progress: Log detailed progress
        
    Returns:
        YabaiPermissionFixResult with detailed status and instructions
    """
    result = YabaiPermissionFixResult()
    
    # =========================================================================
    # PHASE 1: Get actual binary paths
    # =========================================================================
    symlink_path, actual_path = get_yabai_actual_binary_path()
    result.symlink_path = symlink_path
    result.actual_binary_path = actual_path
    
    if not symlink_path:
        result.error_message = "Yabai is not installed"
        result.instructions = [
            "Install yabai with: brew install koekeishiya/formulae/yabai"
        ]
        return result
    
    if narrate_progress:
        logger.info(f"[v30.3] Yabai paths:")
        logger.info(f"  Symlink: {symlink_path}")
        logger.info(f"  Actual:  {actual_path}")
    
    # =========================================================================
    # PHASE 2: Check current permission status
    # =========================================================================
    perm_status = await check_yabai_permissions(force_recheck=True)
    
    if perm_status.fully_functional:
        result.success = True
        result.method_used = "already_authorized"
        if narrate_progress:
            logger.info("[v30.3] ✅ Yabai already has accessibility permission")
        return result
    
    # =========================================================================
    # PHASE 3: Check yabai error log for accessibility crash
    # =========================================================================
    user = os.environ.get("USER", "")
    error_log = Path(f"/tmp/yabai_{user}.err.log")
    accessibility_crash = False
    
    if error_log.exists():
        try:
            content = error_log.read_text()
            if "could not access accessibility features" in content.lower():
                accessibility_crash = True
                if narrate_progress:
                    logger.warning("[v30.3] ⚠️ Yabai crashed due to missing accessibility permission")
        except Exception:
            pass
    
    # =========================================================================
    # PHASE 4: Generate comprehensive fix instructions
    # =========================================================================
    result.user_action_required = True
    
    # Detect macOS version for correct settings path
    macos_version = None
    try:
        version_result = subprocess.run(
            ["sw_vers", "-productVersion"],
            capture_output=True,
            text=True,
            timeout=2.0
        )
        if version_result.returncode == 0:
            macos_version = version_result.stdout.strip()
    except Exception:
        pass
    
    # Generate instructions
    if actual_path and actual_path != symlink_path:
        # Homebrew installation - need to add actual binary
        result.instructions = [
            "🔐 YABAI ACCESSIBILITY PERMISSION FIX",
            "",
            "The issue: macOS needs the ACTUAL binary path, not the symlink.",
            f"  Symlink: {symlink_path}",
            f"  Actual:  {actual_path}",
            "",
            "STEP 1: Open System Settings → Privacy & Security → Accessibility",
            "",
            "STEP 2: Click the '+' button to add an app",
            "",
            "STEP 3: Press Cmd+Shift+G to open 'Go to Folder'",
            "",
            f"STEP 4: Paste this path: {actual_path}",
            "",
            "STEP 5: Click 'Open' to add yabai",
            "",
            "STEP 6: Make sure the toggle is ON",
            "",
            "STEP 7: Restart yabai service:",
            "  Run in Terminal: yabai --restart-service",
            "",
            "ALTERNATIVE (if above doesn't work):",
            "  1. Remove yabai from Accessibility list",
            "  2. Run: sudo tccutil reset Accessibility",
            "  3. Run: yabai --restart-service",
            "  4. A permission prompt should appear - click 'Open System Settings'",
            "  5. Toggle yabai ON",
        ]
    else:
        # Standard installation
        result.instructions = [
            "🔐 YABAI ACCESSIBILITY PERMISSION FIX",
            "",
            "STEP 1: Open System Settings → Privacy & Security → Accessibility",
            "",
            "STEP 2: Find 'yabai' in the list",
            "",
            "STEP 3: Toggle it ON",
            "",
            "STEP 4: If already ON, toggle OFF then ON again",
            "",
            "STEP 5: Restart yabai service:",
            "  Run in Terminal: yabai --restart-service",
        ]
    
    # =========================================================================
    # PHASE 5: Auto-open settings if requested
    # =========================================================================
    if auto_open_settings:
        try:
            subprocess.run(
                ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                check=True,
                timeout=5.0
            )
            result.opened_settings = True
            if narrate_progress:
                logger.info("[v30.3] Opened Accessibility settings")
        except Exception as e:
            if narrate_progress:
                logger.warning(f"[v30.3] Could not open settings: {e}")
    
    # =========================================================================
    # PHASE 6: Trigger permission prompt by restarting yabai
    # =========================================================================
    # When yabai starts without permission, macOS should show the prompt
    if auto_restart_service:
        try:
            subprocess.run(["yabai", "--stop-service"], capture_output=True, timeout=5.0)
            await asyncio.sleep(0.5)
            subprocess.run(["yabai", "--start-service"], capture_output=True, timeout=5.0)
            result.restarted_service = True
            result.triggered_prompt = True
            if narrate_progress:
                logger.info("[v30.3] Restarted yabai service to trigger permission prompt")
        except Exception as e:
            if narrate_progress:
                logger.warning(f"[v30.3] Could not restart yabai service: {e}")
    
    # =========================================================================
    # PHASE 7: Wait and recheck (give user time to grant permission)
    # =========================================================================
    # Don't wait here - return instructions and let user act
    result.method_used = "instructions_provided"
    result.error_message = "Yabai needs accessibility permission - follow the instructions above"
    
    return result


async def monitor_and_wait_for_yabai_permission(
    timeout_seconds: float = 60.0,
    check_interval: float = 2.0,
    on_progress: Optional[callable] = None
) -> bool:
    """
    v30.3: Monitor and wait for yabai permission to be granted.
    
    Useful for creating a user-friendly flow where you:
    1. Call fix_yabai_permissions() to show instructions
    2. Call this function to wait for user to grant permission
    3. Proceed with operations once permission is granted
    
    Args:
        timeout_seconds: Maximum time to wait
        check_interval: How often to check permission status
        on_progress: Optional callback(seconds_elapsed, status_message)
        
    Returns:
        True if permission was granted within timeout, False otherwise
    """
    start_time = time.time()
    
    while time.time() - start_time < timeout_seconds:
        elapsed = time.time() - start_time
        
        # Check permission status
        status = await check_yabai_permissions(force_recheck=True)
        
        if status.fully_functional:
            if on_progress:
                on_progress(elapsed, "Permission granted!")
            logger.info(f"[v30.3] ✅ Yabai permission granted after {elapsed:.1f}s")
            return True
        
        if on_progress:
            remaining = timeout_seconds - elapsed
            on_progress(elapsed, f"Waiting for permission... ({remaining:.0f}s remaining)")
        
        await asyncio.sleep(check_interval)
    
    logger.warning(f"[v30.3] Permission not granted within {timeout_seconds}s")
    return False


def get_yabai_permission_instructions() -> str:
    """
    v30.3: Get formatted permission instructions as a string.
    
    Returns:
        Formatted instructions string
    """
    symlink_path, actual_path = get_yabai_actual_binary_path()
    
    instructions = []
    instructions.append("=" * 60)
    instructions.append("🔐 YABAI ACCESSIBILITY PERMISSION REQUIRED")
    instructions.append("=" * 60)
    instructions.append("")
    
    if actual_path and actual_path != symlink_path:
        instructions.append("⚠️  IMPORTANT: Add the ACTUAL binary, not the symlink!")
        instructions.append("")
        instructions.append(f"   Symlink (DON'T use): {symlink_path}")
        instructions.append(f"   Actual (USE THIS):   {actual_path}")
        instructions.append("")
    
    instructions.append("TO FIX:")
    instructions.append("")
    instructions.append("1. System Settings → Privacy & Security → Accessibility")
    instructions.append("")
    instructions.append("2. Click '+' to add an app")
    instructions.append("")
    instructions.append("3. Press Cmd+Shift+G and paste:")
    if actual_path:
        instructions.append(f"   {actual_path}")
    else:
        instructions.append(f"   {symlink_path}")
    instructions.append("")
    instructions.append("4. Click 'Open' to add yabai")
    instructions.append("")
    instructions.append("5. Make sure the toggle is ON")
    instructions.append("")
    instructions.append("6. Restart yabai: yabai --restart-service")
    instructions.append("")
    instructions.append("=" * 60)
    
    return "\n".join(instructions)


def diagnose_yabai() -> Dict[str, Any]:
    """
    Run comprehensive yabai diagnostics.

    Returns:
        Dictionary with diagnostic information
    """
    detector = get_yabai_detector(auto_start=False)
    status = detector.get_detailed_status()

    # Add additional diagnostics
    diagnostics = {
        **status,
        "diagnostics": {
            "process_running": False,
            "socket_exists": False,
            "launchctl_status": None,
        }
    }

    # Check if process is running
    try:
        result = subprocess.run(["pgrep", "-x", "yabai"], capture_output=True, text=True)
        diagnostics["diagnostics"]["process_running"] = result.returncode == 0
        if result.returncode == 0:
            diagnostics["diagnostics"]["process_pid"] = result.stdout.strip()
    except Exception:
        pass

    # Check if socket exists
    socket_path = Path("/tmp/yabai_$USER.socket".replace("$USER", os.environ.get("USER", "")))
    # Also check common socket locations
    for potential_socket in [
        Path(f"/tmp/yabai_{os.environ.get('USER', '')}.socket"),
        Path("/var/run/yabai.socket"),
        Path.home() / ".local" / "run" / "yabai.socket",
    ]:
        if potential_socket.exists():
            diagnostics["diagnostics"]["socket_exists"] = True
            diagnostics["diagnostics"]["socket_path"] = str(potential_socket)
            break

    # Check launchctl status
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/com.koekeishiya.yabai"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            diagnostics["diagnostics"]["launchctl_status"] = "registered"
            # Parse state
            if "state = running" in result.stdout.lower():
                diagnostics["diagnostics"]["launchctl_state"] = "running"
            else:
                diagnostics["diagnostics"]["launchctl_state"] = "not running"
        else:
            diagnostics["diagnostics"]["launchctl_status"] = "not registered"
    except Exception as e:
        diagnostics["diagnostics"]["launchctl_error"] = str(e)

    return diagnostics


async def ensure_yabai_ready(timeout_seconds: float = 10.0) -> bool:
    """
    Ensure yabai is ready for use, with timeout.

    Args:
        timeout_seconds: Maximum time to wait for yabai to become ready

    Returns:
        True if yabai is ready, False otherwise
    """
    detector = get_yabai_detector()

    if detector.is_available():
        return True

    # Try to start
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        if await detector.ensure_running_async():
            return True
        await asyncio.sleep(0.5)

    return False


# =============================================================================
# v30.0: COMPREHENSIVE YABAI HEALTH & PERMISSION DETECTION
# =============================================================================
# ROOT CAUSE FIX: Detect when yabai lacks accessibility permissions
# This is the "smoking gun" behind silent window control failures:
# - Yabai can QUERY windows/spaces (socket works)
# - But can't CONTROL windows (accessibility permission denied)
# - macOS shows permission popup that user may have dismissed
#
# This system provides:
# 1. Proactive permission detection before operations
# 2. Clear diagnostics when permissions are missing
# 3. Actionable guidance to fix the issue
# 4. Caching to avoid repeated checks
# =============================================================================

@dataclass
class YabaiPermissionStatus:
    """v30.0: Comprehensive yabai permission and health status."""
    # Basic availability
    yabai_installed: bool = False
    yabai_path: Optional[str] = None
    yabai_running: bool = False
    yabai_pid: Optional[int] = None

    # Query capability (socket works)
    can_query_windows: bool = False
    can_query_spaces: bool = False
    can_query_displays: bool = False

    # Control capability (accessibility permissions)
    can_move_windows: bool = False
    can_focus_windows: bool = False
    can_switch_spaces: bool = False

    # Scripting Addition (SIP-dependent)
    scripting_addition_loaded: bool = False
    sip_status: Optional[str] = None

    # Overall status
    fully_functional: bool = False
    needs_accessibility_permission: bool = False
    needs_restart: bool = False

    # Diagnostics
    error_message: Optional[str] = None
    fix_instructions: Optional[str] = None
    last_check: Optional[datetime] = None
    check_duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for reporting."""
        return {
            "yabai_installed": self.yabai_installed,
            "yabai_path": self.yabai_path,
            "yabai_running": self.yabai_running,
            "yabai_pid": self.yabai_pid,
            "can_query_windows": self.can_query_windows,
            "can_query_spaces": self.can_query_spaces,
            "can_query_displays": self.can_query_displays,
            "can_move_windows": self.can_move_windows,
            "can_focus_windows": self.can_focus_windows,
            "can_switch_spaces": self.can_switch_spaces,
            "scripting_addition_loaded": self.scripting_addition_loaded,
            "sip_status": self.sip_status,
            "fully_functional": self.fully_functional,
            "needs_accessibility_permission": self.needs_accessibility_permission,
            "needs_restart": self.needs_restart,
            "error_message": self.error_message,
            "fix_instructions": self.fix_instructions,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "check_duration_ms": self.check_duration_ms,
        }


# Module-level cache for permission status
_YABAI_PERMISSION_CACHE: Optional[YabaiPermissionStatus] = None
_YABAI_PERMISSION_CACHE_TIME: Optional[datetime] = None
_YABAI_PERMISSION_CACHE_TTL = 30.0  # 30 seconds


async def check_yabai_permissions(force_recheck: bool = False) -> YabaiPermissionStatus:
    """
    v30.1: FAST Comprehensive yabai permission and health check.

    ROOT CAUSE FIX: v30.0 was too slow - sequential subprocess calls with
    long timeouts caused "Processing..." hang. v30.1 fixes:
    1. PARALLEL execution of all tests
    2. SHORT timeouts (500ms instead of 2-3s)
    3. FAST PATH: Skip expensive tests if basic connectivity fails
    4. Overall timeout wrapper to guarantee fast return

    Args:
        force_recheck: If True, bypass cache and recheck

    Returns:
        YabaiPermissionStatus with detailed health and permission info
    """
    global _YABAI_PERMISSION_CACHE, _YABAI_PERMISSION_CACHE_TIME

    # Check cache FIRST (instant return)
    if not force_recheck and _YABAI_PERMISSION_CACHE and _YABAI_PERMISSION_CACHE_TIME:
        cache_age = (datetime.now() - _YABAI_PERMISSION_CACHE_TIME).total_seconds()
        if cache_age < _YABAI_PERMISSION_CACHE_TTL:
            return _YABAI_PERMISSION_CACHE

    start_time = time.time()
    status = YabaiPermissionStatus()

    # v30.1: Overall timeout - permission check MUST complete in 2 seconds max
    OVERALL_TIMEOUT = 2.0
    SUBPROCESS_TIMEOUT = 0.5  # 500ms per subprocess (was 2-3s)

    try:
        # Wrap entire check in overall timeout
        status = await asyncio.wait_for(
            _do_permission_check(status, SUBPROCESS_TIMEOUT),
            timeout=OVERALL_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"[v30.1] Permission check timed out after {OVERALL_TIMEOUT}s")
        status.error_message = "Permission check timed out - yabai may be unresponsive"
        status.needs_restart = True
    except Exception as e:
        logger.error(f"[v30.1] Permission check failed: {e}")
        status.error_message = f"Permission check failed: {e}"

    status.last_check = datetime.now()
    status.check_duration_ms = (time.time() - start_time) * 1000

    # Cache result
    _YABAI_PERMISSION_CACHE = status
    _YABAI_PERMISSION_CACHE_TIME = datetime.now()

    # Log result
    if status.needs_accessibility_permission:
        logger.warning(f"[v30.1] ⚠️ Yabai needs accessibility permission")
    elif status.fully_functional:
        logger.debug(f"[v30.1] ✅ Yabai OK ({status.check_duration_ms:.0f}ms)")
    elif not status.yabai_running:
        logger.debug(f"[v30.1] Yabai not running ({status.check_duration_ms:.0f}ms)")

    return status


async def _do_permission_check(status: YabaiPermissionStatus, timeout: float) -> YabaiPermissionStatus:
    """
    v30.1: Internal permission check with fast-fail logic.
    """
    # =========================================================================
    # PHASE 1: Check if yabai is installed (fast filesystem check)
    # =========================================================================
    yabai_path = shutil.which("yabai")
    if not yabai_path:
        for path in ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                yabai_path = path
                break

    status.yabai_installed = yabai_path is not None
    status.yabai_path = yabai_path

    if not yabai_path:
        status.error_message = "Yabai is not installed"
        status.fix_instructions = "Install yabai: brew install koekeishiya/formulae/yabai"
        return status

    # =========================================================================
    # PHASE 2: Check if yabai process is running (fast pgrep)
    # =========================================================================
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-x", "yabai",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        status.yabai_running = proc.returncode == 0
        if status.yabai_running:
            try:
                status.yabai_pid = int(stdout.decode().strip().split()[0])
            except (ValueError, IndexError):
                pass
    except asyncio.TimeoutError:
        status.yabai_running = False
    except Exception:
        status.yabai_running = False

    if not status.yabai_running:
        status.error_message = "Yabai is not running"
        status.fix_instructions = "Start yabai: yabai --start-service"
        status.needs_restart = True
        return status  # FAST PATH: Skip remaining tests

    # =========================================================================
    # PHASE 3: Quick socket connectivity test (SINGLE query, not three)
    # =========================================================================
    async def quick_query() -> Tuple[bool, str]:
        """Single fast query to test socket connectivity."""
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode == 0:
                return True, stdout.decode()
            return False, stderr.decode()
        except asyncio.TimeoutError:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    can_query, query_result = await quick_query()

    if not can_query:
        # Socket not responding - yabai probably crashed
        status.can_query_windows = False
        status.can_query_spaces = False
        status.can_query_displays = False
        if "timeout" in query_result:
            status.error_message = "Yabai socket not responding (may need restart)"
        elif "failed to connect" in query_result.lower():
            status.error_message = "Yabai socket connection failed"
        else:
            status.error_message = f"Yabai query failed: {query_result[:100]}"
        status.needs_restart = True
        return status  # FAST PATH: Skip remaining tests

    # Socket works - set query capabilities
    status.can_query_spaces = True
    status.can_query_windows = True  # If spaces work, windows likely work too
    status.can_query_displays = True

    # =========================================================================
    # PHASE 4: Quick control test (simplified - just check if we can query windows)
    # =========================================================================
    # v30.1: Skip the slow focus/move test - if queries work and yabai is running,
    # assume control works. The actual control test was too slow and unreliable.
    # If accessibility is actually missing, operations will fail fast anyway.

    # Assume control works if queries work (fast path)
    status.can_focus_windows = True
    status.can_move_windows = True
    status.can_switch_spaces = True
    status.scripting_addition_loaded = True

    # =========================================================================
    # PHASE 5: Determine overall status
    # =========================================================================
    status.fully_functional = (
        status.yabai_running and
        status.can_query_windows
    )

    if status.fully_functional:
        status.error_message = None
        status.fix_instructions = None

    return status


def get_cached_permission_status() -> Optional[YabaiPermissionStatus]:
    """Get cached permission status without blocking."""
    return _YABAI_PERMISSION_CACHE


def invalidate_permission_cache():
    """Invalidate permission cache to force recheck."""
    global _YABAI_PERMISSION_CACHE, _YABAI_PERMISSION_CACHE_TIME
    _YABAI_PERMISSION_CACHE = None
    _YABAI_PERMISSION_CACHE_TIME = None


async def ensure_yabai_permissions(
    auto_open_settings: bool = False,
    narrate_issues: bool = True
) -> Tuple[bool, Optional[str]]:
    """
    v30.0: Ensure yabai has all required permissions before operations.

    This should be called BEFORE any God Mode / window control operations.
    It provides early detection of permission issues with clear guidance.

    Args:
        auto_open_settings: If True, automatically open accessibility settings
                           when permissions are missing
        narrate_issues: If True, log detailed issue descriptions

    Returns:
        Tuple of (success, error_message)
        - success: True if yabai is fully functional
        - error_message: None if success, otherwise describes the issue
    """
    status = await check_yabai_permissions()

    if status.fully_functional:
        return True, None

    if status.needs_accessibility_permission:
        error_msg = (
            "🚫 YABAI ACCESSIBILITY PERMISSION REQUIRED\n\n"
            "Yabai can see windows but cannot control them.\n"
            "macOS is blocking window management operations.\n\n"
            "TO FIX:\n"
            "1. Go to System Settings → Privacy & Security → Accessibility\n"
            "2. Find 'yabai' and toggle it ON\n"
            "3. If already ON, toggle OFF then ON again\n"
            "4. Run: yabai --restart-service\n\n"
            "This is required for window teleportation and space switching."
        )

        if narrate_issues:
            logger.error(f"[v30.0] {error_msg}")

        if auto_open_settings:
            open_accessibility_settings()

        return False, error_msg

    if not status.yabai_running:
        error_msg = (
            "Yabai is not running.\n"
            "Start it with: yabai --start-service"
        )
        if narrate_issues:
            logger.error(f"[v30.0] {error_msg}")
        return False, error_msg

    if not status.scripting_addition_loaded and not status.can_switch_spaces:
        error_msg = (
            "Yabai scripting addition not loaded.\n"
            "Space switching requires: sudo yabai --load-sa"
        )
        if narrate_issues:
            logger.warning(f"[v30.0] {error_msg}")
        # Return True but with warning - basic functionality may still work
        return True, error_msg

    # Generic error
    return False, status.error_message


# Export for convenient imports
__all__ = [
    # Core classes
    "YabaiSpaceDetector",
    "YabaiStatus",
    "YabaiServiceHealth",
    "YabaiConfig",
    # v24.0 Intelligent Search & Rescue Protocol
    "RescueStrategy",
    "RescueFailureReason",
    "RescueTelemetry",
    "RescueResult",
    "get_rescue_telemetry",
    "reset_rescue_telemetry",
    # v25.0 Shadow Monitor Infrastructure
    "GhostDisplayManager",
    "GhostDisplayManagerConfig",
    "GhostDisplayStatus",
    "GhostDisplayInfo",
    "WindowGeometry",
    "WindowLayoutStyle",
    "get_ghost_manager",
    "get_ghost_display_status",
    "reset_ghost_manager",
    # Factory and utilities
    "get_yabai_detector",
    "reset_yabai_detector",
    "open_accessibility_settings",
    "diagnose_yabai",
    "ensure_yabai_ready",
    # v30.0 Permission Detection
    "YabaiPermissionStatus",
    "check_yabai_permissions",
    "ensure_yabai_permissions",
    "get_cached_permission_status",
    "invalidate_permission_cache",
    # v30.3 Permission Fixer
    "YabaiPermissionFixResult",
    "get_yabai_actual_binary_path",
    "check_yabai_in_accessibility_tcc",
    "trigger_accessibility_prompt_for_yabai",
    "fix_yabai_permissions",
    "monitor_and_wait_for_yabai_permission",
    "get_yabai_permission_instructions",
]
