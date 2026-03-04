"""
v68.0 PHANTOM HARDWARE PROTOCOL - Software-Defined Ghost Display

This module provides JARVIS with kernel-level virtual display management using
BetterDisplay, eliminating the need for physical HDMI dummy plugs.

FEATURES:
- Multi-path BetterDisplay CLI discovery (no hardcoded paths)
- Automatic virtual display creation and management
- Kernel registration wait loop with exponential backoff
- Permission verification before operations
- Display persistence tracking
- BetterDisplay.app auto-launch support
- Graceful degradation when BetterDisplay unavailable

ROOT CAUSE FIX:
Instead of relying on physical hardware (HDMI dummy plugs), we create
software-defined virtual displays that:
- Cannot be "unplugged"
- Survive system restarts (with re-creation)
- Work on any Mac without additional hardware

USAGE:
    from backend.system.phantom_hardware_manager import get_phantom_manager

    manager = get_phantom_manager()

    # Ensure Ghost Display exists
    success, error = await manager.ensure_ghost_display_exists_async()

    # Check current status
    status = await manager.get_display_status_async()
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy imports for memory_types used by DisplayPressureController
# (deferred to avoid circular imports during early startup)
_memory_types_loaded = False
DisplayState = None
PressureTier = None
MemoryBudgetEventType = None


def _ensure_memory_types():
    """Load memory_types on first use to avoid circular import at module load."""
    global _memory_types_loaded, DisplayState, PressureTier, MemoryBudgetEventType
    if _memory_types_loaded:
        return
    from backend.core.memory_types import (
        DisplayState as _DS,
        PressureTier as _PT,
        MemoryBudgetEventType as _MBET,
    )
    DisplayState = _DS
    PressureTier = _PT
    MemoryBudgetEventType = _MBET
    _memory_types_loaded = True


# =============================================================================
# v68.0: DISPLAY STATUS DATACLASS
# =============================================================================

@dataclass
class VirtualDisplayInfo:
    """Information about a virtual display."""
    display_id: Optional[int] = None
    name: str = ""
    resolution: str = ""
    is_active: bool = False
    is_jarvis_ghost: bool = False
    space_id: Optional[int] = None
    created_at: Optional[datetime] = None


@dataclass
class PhantomHardwareStatus:
    """Overall status of the Phantom Hardware system."""
    cli_available: bool = False
    cli_path: Optional[str] = None
    cli_version: Optional[str] = None
    app_running: bool = False
    driverkit_approved: bool = False
    ghost_display_active: bool = False
    ghost_display_info: Optional[VirtualDisplayInfo] = None
    permissions_ok: bool = False
    last_check: Optional[datetime] = None
    error: Optional[str] = None


# =============================================================================
# v68.0: PHANTOM HARDWARE MANAGER SINGLETON
# =============================================================================

class PhantomHardwareManager:
    """
    v68.0 PHANTOM HARDWARE PROTOCOL: Software-Defined Ghost Display Manager.

    This singleton manages virtual displays created via BetterDisplay,
    eliminating the need for physical HDMI dummy plugs.

    Architecture:
    1. CLI Discovery - Find BetterDisplay CLI dynamically
    2. App Verification - Ensure BetterDisplay.app is running
    3. Display Creation - Create virtual display with correct settings
    4. Registration Wait - Wait for kernel to recognize display
    5. Yabai Integration - Verify yabai can see the display
    """

    _instance: Optional['PhantomHardwareManager'] = None

    def __new__(cls) -> 'PhantomHardwareManager':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True

        # Configuration from environment
        self.ghost_display_name = os.getenv("JARVIS_GHOST_DISPLAY_NAME", "JARVIS_GHOST")
        self.preferred_resolution = os.getenv("JARVIS_GHOST_RESOLUTION", "1920x1080")
        self.preferred_aspect = os.getenv("JARVIS_GHOST_ASPECT", "16:9")

        # CLI discovery paths (in priority order)
        self._cli_search_paths = [
            "/usr/local/bin/betterdisplaycli",
            "/opt/homebrew/bin/betterdisplaycli",
            os.path.expanduser("~/.local/bin/betterdisplaycli"),
            os.path.expanduser("~/bin/betterdisplaycli"),
            "/Applications/BetterDisplay.app/Contents/MacOS/betterdisplaycli",
        ]

        # Cached CLI path
        self._cached_cli_path: Optional[str] = None
        self._cli_version: Optional[str] = None

        # Display state tracking
        self._ghost_display_info: Optional[VirtualDisplayInfo] = None
        self._last_status_check: Optional[datetime] = None
        self._status_cache_ttl = timedelta(seconds=30)
        self._ensure_inflight: Optional[asyncio.Task] = None
        self._last_registration_state: Dict[str, Any] = {}
        self._registration_latency_ema: Optional[float] = None
        self._registration_latency_alpha = float(
            os.getenv("JARVIS_GHOST_REGISTRATION_EMA_ALPHA", "0.35")
        )
        self._registration_wait_cap_seconds = float(
            os.getenv("JARVIS_GHOST_REGISTRATION_WAIT_CAP_SECONDS", "45.0")
        )
        self._registration_stabilization_seconds = float(
            os.getenv("JARVIS_GHOST_REGISTRATION_STABILIZATION_SECONDS", "4.0")
        )

        # Stats
        self._stats = {
            "displays_created": 0,
            "cli_discoveries": 0,
            "registration_waits": 0,
            "total_queries": 0,
            "resolution_changes": 0,
            "disconnects": 0,
            "reconnects": 0,
        }

        logger.info("[v68.0] 👻 PHANTOM HARDWARE: Manager initialized")

    def _effective_registration_wait_seconds(self, requested_wait_seconds: float) -> float:
        """Compute dynamic wait budget from current request + observed registration latency."""
        requested = max(2.0, float(requested_wait_seconds))
        if self._registration_latency_ema is None:
            return min(requested, self._registration_wait_cap_seconds)

        adaptive_target = self._registration_latency_ema * 2.0
        return min(
            max(requested, adaptive_target),
            self._registration_wait_cap_seconds,
        )

    def _update_registration_latency_ema(self, latency_seconds: float) -> None:
        """Update EWMA of registration latency for dynamic timeout adaptation."""
        latency = max(0.0, float(latency_seconds))
        if self._registration_latency_ema is None:
            self._registration_latency_ema = latency
            return

        alpha = min(max(self._registration_latency_alpha, 0.05), 0.95)
        self._registration_latency_ema = (
            alpha * latency + (1.0 - alpha) * self._registration_latency_ema
        )

    def _analyze_yabai_spaces_for_registration(
        self,
        spaces: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Analyze yabai spaces for ghost-display registration progress."""
        display_ids: List[int] = []
        ghost_candidates: List[Dict[str, int]] = []

        for space in spaces:
            try:
                display_id = int(space.get("display", 1) or 1)
            except (TypeError, ValueError):
                display_id = 1
            display_ids.append(display_id)

            if (not space.get("is_current")) and bool(space.get("is_visible")):
                ghost_candidates.append(
                    {
                        "space_id": int(space.get("space_id", 0) or 0),
                        "display": display_id,
                        "window_count": int(space.get("window_count", 0) or 0),
                    }
                )

        ghost_space: Optional[int] = None
        if ghost_candidates:
            ghost_candidates.sort(key=lambda item: (-item["display"], item["window_count"]))
            candidate_space = ghost_candidates[0]["space_id"]
            ghost_space = candidate_space if candidate_space > 0 else None

        unique_displays = sorted(set(display_ids))
        display_count = len(unique_displays)
        recognized_without_space = ghost_space is None and display_count >= 2

        return {
            "ghost_space": ghost_space,
            "display_ids": unique_displays,
            "display_count": display_count,
            "recognized_without_space": recognized_without_space,
        }

    # =========================================================================
    # PRIMARY API: Ensure Ghost Display Exists
    # =========================================================================

    async def ensure_ghost_display_exists_async(
        self,
        wait_for_registration: bool = True,
        max_wait_seconds: float = 15.0
    ) -> Tuple[bool, Optional[str]]:
        """
        v68.0: Ensure a virtual Ghost Display exists for JARVIS operations.

        This is the primary entry point. It will:
        1. Verify BetterDisplay CLI is available
        2. Check if BetterDisplay.app is running
        3. Check if JARVIS_GHOST display already exists
        4. Create display if needed
        5. Wait for kernel registration

        Args:
            wait_for_registration: Wait for yabai to recognize the display
            max_wait_seconds: Maximum time to wait for registration

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        # Single-flight guard: avoid concurrent create/probe races from
        # startup, health recovery, and command-triggered call sites.
        inflight = self._ensure_inflight
        if inflight and not inflight.done():
            return await asyncio.shield(inflight)

        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._ensure_ghost_display_exists_impl(
                wait_for_registration=wait_for_registration,
                max_wait_seconds=max_wait_seconds,
            ),
            name="phantom-ensure-ghost-display",
        )
        self._ensure_inflight = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._ensure_inflight is task and task.done():
                self._ensure_inflight = None

    async def _ensure_ghost_display_exists_impl(
        self,
        wait_for_registration: bool = True,
        max_wait_seconds: float = 15.0
    ) -> Tuple[bool, Optional[str]]:
        """Internal implementation for ensure_ghost_display_exists_async."""
        logger.info("[v68.0] 🔧 Ensuring Ghost Display exists...")

        # =================================================================
        # STEP 0: Quick check — does the display already exist?
        # v251.2: system_profiler works without CLI integration.
        # If the display is already present, skip all CLI operations.
        # =================================================================
        existing_display = await self._find_display_via_system_profiler()
        if existing_display:
            logger.info(
                f"[v68.0] Ghost Display '{self.ghost_display_name}' already "
                f"exists (detected via system_profiler)"
            )
            self._ghost_display_info = existing_display

            if wait_for_registration:
                space_id = await self._verify_yabai_recognition_async(
                    max_wait_seconds
                )
                if space_id and self._ghost_display_info:
                    self._ghost_display_info.space_id = space_id

            return True, None

        # =================================================================
        # STEP 1: Ensure BetterDisplay.app is Running
        # v283.2: Moved BEFORE CLI discovery. The CLI communicates with the
        # running app via Apple notifications — ``betterdisplaycli help``
        # returns "Failed. Request timed out." when the host app isn't
        # running, causing _verify_cli_works_async() to reject the path
        # and _discover_cli_path_async() to return None. Previously STEP 2,
        # but STEP 1 (CLI discovery) could never succeed without the app.
        # =================================================================
        app_running = await self._check_app_running_async()

        if not app_running:
            # Try to launch BetterDisplay.app
            launched = await self._launch_betterdisplay_app_async()
            if not launched:
                error_msg = (
                    "BetterDisplay.app is not running and could not be launched. "
                    "Please start BetterDisplay manually."
                )
                logger.warning(f"[v283.2] {error_msg}")
                return False, error_msg

            # Wait for app to initialize before CLI verification
            await asyncio.sleep(3.0)

        # =================================================================
        # STEP 2: Discover BetterDisplay CLI
        # v283.2: Now runs after app is confirmed running, so CLI
        # verification (``betterdisplaycli help``) will succeed.
        # =================================================================
        cli_path = await self._discover_cli_path_async()

        if not cli_path:
            error_msg = (
                "BetterDisplay CLI not found. Please install BetterDisplay from "
                "https://betterdisplay.pro/ or use a physical HDMI dummy plug."
            )
            logger.info(f"[v68.0] {error_msg}")
            return False, error_msg

        # =================================================================
        # STEP 3: Check if Ghost Display Already Exists (via CLI)
        # =================================================================
        existing_display = await self._find_existing_ghost_display_async(cli_path)

        if existing_display:
            logger.info(
                f"[v68.0] Ghost Display '{self.ghost_display_name}' already "
                f"exists (ID: {existing_display.display_id})"
            )
            self._ghost_display_info = existing_display

            if wait_for_registration:
                space_id = await self._verify_yabai_recognition_async(
                    max_wait_seconds
                )
                if space_id and self._ghost_display_info:
                    self._ghost_display_info.space_id = space_id

            return True, None

        # =================================================================
        # STEP 4: Create New Virtual Display
        # =================================================================
        create_result = await self._create_virtual_display_async(cli_path)

        if not create_result[0]:
            return False, create_result[1]

        self._stats["displays_created"] += 1

        # =================================================================
        # STEP 5: Wait for Kernel Registration
        # =================================================================
        if wait_for_registration:
            self._stats["registration_waits"] += 1
            effective_wait_seconds = self._effective_registration_wait_seconds(
                max_wait_seconds
            )
            space_id = await self._wait_for_display_registration_async(
                effective_wait_seconds
            )

            if space_id is None:
                registration_state = self._last_registration_state or {}
                if registration_state.get("recognized_without_space"):
                    logger.info(
                        "[v68.0] Display recognized by yabai (display_count=%s), "
                        "ghost space still stabilizing; continuing.",
                        registration_state.get("display_count", "unknown"),
                    )
                else:
                    logger.warning(
                        "[v68.0] Display created but yabai hasn't recognized it "
                        "yet. It may appear shortly."
                    )

            if self._ghost_display_info:
                self._ghost_display_info.space_id = space_id

        logger.info(f"[v68.0] Ghost Display '{self.ghost_display_name}' is ready")
        return True, None

    # =========================================================================
    # CLI DISCOVERY
    # =========================================================================

    async def _discover_cli_path_async(self) -> Optional[str]:
        """
        v68.0: Discover BetterDisplay CLI using multiple strategies.

        Priority:
        1. Cached path (if still valid)
        2. 'which' command discovery
        3. Known path scanning
        4. Spotlight search (mdfind)
        """
        # Check cache first
        if self._cached_cli_path:
            if await self._verify_cli_works_async(self._cached_cli_path):
                return self._cached_cli_path
            else:
                self._cached_cli_path = None

        self._stats["cli_discoveries"] += 1

        # =================================================================
        # Strategy 1: Use 'which' command
        # =================================================================
        try:
            proc = await asyncio.create_subprocess_exec(
                "which", "betterdisplaycli",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            if proc.returncode == 0:
                discovered_path = stdout.decode().strip()
                if discovered_path and await self._verify_cli_works_async(discovered_path):
                    self._cached_cli_path = discovered_path
                    logger.info(f"[v68.0] Found CLI via 'which': {discovered_path}")
                    return discovered_path

        except Exception as e:
            logger.debug(f"[v68.0] 'which' discovery failed: {e}")

        # =================================================================
        # Strategy 2: Scan known paths
        # =================================================================
        for path in self._cli_search_paths:
            if os.path.exists(path) and await self._verify_cli_works_async(path):
                self._cached_cli_path = path
                logger.info(f"[v68.0] Found CLI at known path: {path}")
                return path

        # =================================================================
        # Strategy 3: Spotlight search via mdfind
        # =================================================================
        try:
            proc = await asyncio.create_subprocess_exec(
                "mdfind", "kMDItemFSName == 'betterdisplaycli'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                paths = stdout.decode().strip().split('\n')
                for path in paths:
                    if path and await self._verify_cli_works_async(path):
                        self._cached_cli_path = path
                        logger.info(f"[v68.0] Found CLI via Spotlight: {path}")
                        return path

        except Exception as e:
            logger.debug(f"[v68.0] Spotlight discovery failed: {e}")

        # v251.1: Downgraded from WARNING → INFO. BetterDisplay is optional.
        logger.info("[v68.0] BetterDisplay CLI not found (optional)")
        return None

    async def _verify_cli_works_async(self, cli_path: str) -> bool:
        """Verify the CLI is executable and responds.

        v251.2: Uses ``help`` instead of ``--version``.
        BetterDisplay does NOT support ``--version`` — unrecognized flags
        cause it to launch a **new app instance**, spawning zombie
        processes and extra menu-bar icons on every verification attempt.
        The ``help`` command exits cleanly and includes version info on
        the first line (``BetterDisplay Version X.X.X Build NNNNN``).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                cli_path, "help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode().strip()
                # First line: "BetterDisplay Version X.X.X Build NNNNN ..."
                if output:
                    first_line = output.split('\n')[0]
                    self._cli_version = first_line.split(' - ')[0].strip()
                    return True

        except Exception:
            pass

        return False

    # =========================================================================
    # APP STATE MANAGEMENT
    # =========================================================================

    async def _check_app_running_async(self) -> bool:
        """Check if BetterDisplay.app is running."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-x", "BetterDisplay",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            return proc.returncode == 0 and bool(stdout.decode().strip())

        except Exception:
            return False

    async def _launch_betterdisplay_app_async(self) -> bool:
        """Launch BetterDisplay.app if installed."""
        app_paths = [
            "/Applications/BetterDisplay.app",
            os.path.expanduser("~/Applications/BetterDisplay.app"),
        ]

        for app_path in app_paths:
            if os.path.exists(app_path):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "open", "-a", app_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=5.0)

                    if proc.returncode == 0:
                        logger.info(f"[v68.0] Launched BetterDisplay from {app_path}")
                        return True

                except Exception as e:
                    logger.debug(f"[v68.0] Failed to launch BetterDisplay: {e}")

        return False

    # =========================================================================
    # DISPLAY MANAGEMENT
    # =========================================================================

    async def _find_existing_ghost_display_async(
        self,
        cli_path: Optional[str] = None
    ) -> Optional[VirtualDisplayInfo]:
        """Check if JARVIS Ghost Display already exists.

        v251.2: Uses two detection strategies:
        1. BetterDisplay CLI ``get -nameLike=... -list`` (requires CLI
           integration enabled in BetterDisplay settings)
        2. ``system_profiler SPDisplaysDataType`` fallback — always
           works, detects any display whose name contains the ghost
           display name (case-insensitive, underscores treated as spaces)
        """
        # ==============================================================
        # Strategy 1: BetterDisplay CLI (fast, but needs integration on)
        # ==============================================================
        if cli_path:
            try:
                # v251.2: Correct syntax is ``get -nameLike=... -list``
                # (NOT ``list`` which is not a valid BetterDisplay operation
                # and causes the app to launch a new instance).
                search_name = self.ghost_display_name.replace("_", " ")
                proc = await asyncio.create_subprocess_exec(
                    cli_path, "get",
                    f"-nameLike={search_name}", "-list",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=5.0
                )

                if proc.returncode == 0:
                    output = stdout.decode()
                    if output.strip() and "failed" not in output.lower():
                        return VirtualDisplayInfo(
                            name=self.ghost_display_name,
                            is_active=True,
                            is_jarvis_ghost=True,
                            created_at=datetime.now()
                        )
            except Exception as e:
                logger.debug(f"[v68.0] CLI display query failed: {e}")

        # ==============================================================
        # Strategy 2: system_profiler fallback (always available)
        # ==============================================================
        return await self._find_display_via_system_profiler()

    async def _find_display_via_system_profiler(self) -> Optional[VirtualDisplayInfo]:
        """Detect ghost display via macOS system_profiler.

        v251.2: Works regardless of whether BetterDisplay CLI integration
        is enabled.  Parses ``system_profiler SPDisplaysDataType`` output
        for a display whose name contains our ghost display name
        (case-insensitive, underscores→spaces).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "system_profiler", "SPDisplaysDataType",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=10.0
            )

            if proc.returncode != 0:
                return None

            output = stdout.decode()
            # Match "JARVIS_GHOST" as "jarvis ghost" or "jarvis_ghost"
            # in display names (system_profiler uses spaces)
            search_variants = [
                self.ghost_display_name.lower(),
                self.ghost_display_name.lower().replace("_", " "),
                self.ghost_display_name.lower().replace("_", ""),
            ]

            output_lower = output.lower()
            for variant in search_variants:
                if variant in output_lower:
                    # Parse resolution from nearby lines
                    resolution = ""
                    for line in output.splitlines():
                        if any(v in line.lower() for v in search_variants):
                            continue
                        if "resolution" in line.lower() and resolution == "":
                            # e.g. "Resolution: 5120 x 2880 ..."
                            resolution = line.strip().split(":", 1)[-1].strip()

                    logger.info(
                        f"[v68.0] Found ghost display via system_profiler"
                        f"{f': {resolution}' if resolution else ''}"
                    )
                    return VirtualDisplayInfo(
                        name=self.ghost_display_name,
                        resolution=resolution,
                        is_active=True,
                        is_jarvis_ghost=True,
                        created_at=datetime.now()
                    )

        except Exception as e:
            logger.debug(f"[v68.0] system_profiler query failed: {e}")

        return None

    async def _create_virtual_display_async(
        self,
        cli_path: str
    ) -> Tuple[bool, Optional[str]]:
        """
        v68.0/v251.2: Create a new virtual display using BetterDisplay CLI.

        Uses the correct BetterDisplay CLI syntax:
        ``create -type=VirtualScreen -virtualScreenName=NAME -aspectWidth=W -aspectHeight=H``

        Requires CLI integration to be enabled in BetterDisplay settings.
        """
        logger.info(
            f"[v68.0] Creating virtual display: {self.ghost_display_name} "
            f"({self.preferred_resolution})"
        )

        try:
            # Parse aspect ratio
            aspect_parts = self.preferred_aspect.split(':')
            aspect_w = aspect_parts[0] if len(aspect_parts) == 2 else "16"
            aspect_h = aspect_parts[1] if len(aspect_parts) == 2 else "9"

            display_name = self.ghost_display_name.replace("_", " ")

            # v251.2: Correct BetterDisplay CLI syntax (per help docs).
            # ``create`` requires ``-type=VirtualScreen``.
            cmd = [
                cli_path, "create",
                "-type=VirtualScreen",
                f"-virtualScreenName={display_name}",
                f"-aspectWidth={aspect_w}",
                f"-aspectHeight={aspect_h}",
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=10.0
                )

                combined = (stdout.decode() + stderr.decode()).strip()

                if proc.returncode == 0 and "failed" not in combined.lower():
                    self._ghost_display_info = VirtualDisplayInfo(
                        name=self.ghost_display_name,
                        resolution=self.preferred_resolution,
                        is_active=True,
                        is_jarvis_ghost=True,
                        created_at=datetime.now()
                    )
                    logger.info("[v68.0] Virtual display created successfully")

                    # v283.1: Activate the display. BetterDisplay `create`
                    # only defines the virtual screen in config — it does NOT
                    # connect it to the GPU framebuffer.  Without this step
                    # the display never appears in system_profiler or yabai.
                    await self._connect_virtual_display_async(cli_path)

                    return True, None

                # "already exists" is success
                if "already exists" in combined.lower():
                    self._ghost_display_info = VirtualDisplayInfo(
                        name=self.ghost_display_name,
                        resolution=self.preferred_resolution,
                        is_active=True,
                        is_jarvis_ghost=True,
                    )

                    # v283.1: Ensure existing display is connected too.
                    await self._connect_virtual_display_async(cli_path)

                    return True, None

                # "Failed." typically means CLI integration is disabled
                if combined.lower().strip() == "failed.":
                    return False, (
                        "BetterDisplay CLI integration is disabled. "
                        "Enable it in BetterDisplay > Settings > Integration."
                    )

                return False, f"CLI create failed: {combined}"

            except asyncio.TimeoutError:
                return False, "Display creation timed out"

        except Exception as e:
            error_msg = f"Display creation error: {e}"
            logger.error(f"[v68.0] {error_msg}")
            return False, error_msg

    async def _connect_virtual_display_async(
        self,
        cli_path: str,
    ) -> bool:
        """
        v283.1: Activate a virtual screen so it appears as a real display.

        BetterDisplay ``create`` only defines the virtual screen in config.
        ``set -connected=on`` connects it to the GPU framebuffer, making it
        visible to system_profiler, yabai, and all downstream consumers.
        """
        display_name = self.ghost_display_name.replace("_", " ")
        try:
            proc = await asyncio.create_subprocess_exec(
                cli_path, "set",
                f"-virtualScreenName={display_name}",
                "-connected=on",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10.0
            )
            combined = (stdout.decode() + stderr.decode()).strip()

            if proc.returncode == 0 and "failed" not in combined.lower():
                logger.info("[v283.1] Ghost display connected to GPU framebuffer")
                return True

            logger.warning(
                "[v283.1] Ghost display connect returned: %s (rc=%s)",
                combined or "(empty)",
                proc.returncode,
            )
            return False

        except asyncio.TimeoutError:
            logger.warning("[v283.1] Ghost display connect timed out")
            return False
        except Exception as e:
            logger.warning("[v283.1] Ghost display connect error: %s", e)
            return False

    # -----------------------------------------------------------------
    # Runtime display control methods (used by DisplayPressureController)
    # -----------------------------------------------------------------

    async def set_resolution_async(self, resolution: str) -> bool:
        """
        Change the ghost display resolution at runtime.

        Idempotent: returns True immediately if the current resolution
        already matches *resolution*. Returns False when no CLI path
        is available or the subprocess fails.
        """
        if not self._cached_cli_path:
            logger.debug("[phantom] set_resolution_async: no CLI path available")
            return False

        if (
            self._ghost_display_info is not None
            and self._ghost_display_info.resolution == resolution
        ):
            logger.debug(
                "[phantom] set_resolution_async: already at %s, skipping",
                resolution,
            )
            return True

        display_name = self.ghost_display_name.replace("_", " ")
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cached_cli_path, "set",
                f"-virtualScreenName={display_name}",
                f"-resolution={resolution}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10.0,
            )
            combined = (stdout.decode() + stderr.decode()).strip()

            if proc.returncode == 0 and "failed" not in combined.lower():
                if self._ghost_display_info is not None:
                    self._ghost_display_info.resolution = resolution
                self._stats["resolution_changes"] += 1
                logger.info(
                    "[phantom] Ghost display resolution set to %s", resolution,
                )
                return True

            logger.error(
                "[phantom] set_resolution_async failed: %s (rc=%s)",
                combined or "(empty)",
                proc.returncode,
            )
            return False

        except asyncio.TimeoutError:
            logger.error("[phantom] set_resolution_async timed out (10s)")
            return False
        except Exception as e:
            logger.error("[phantom] set_resolution_async error: %s", e)
            return False

    async def disconnect_async(self) -> bool:
        """
        Disconnect (deactivate) the ghost display from the GPU framebuffer.

        Idempotent: returns True immediately when the display is already
        inactive. Returns False when no CLI path is available or the
        subprocess fails.
        """
        if not self._cached_cli_path:
            logger.debug("[phantom] disconnect_async: no CLI path available")
            return False

        if (
            self._ghost_display_info is not None
            and not self._ghost_display_info.is_active
        ):
            logger.debug("[phantom] disconnect_async: already disconnected, skipping")
            return True

        display_name = self.ghost_display_name.replace("_", " ")
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cached_cli_path, "set",
                f"-virtualScreenName={display_name}",
                "-connected=off",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10.0,
            )
            combined = (stdout.decode() + stderr.decode()).strip()

            if proc.returncode == 0 and "failed" not in combined.lower():
                if self._ghost_display_info is not None:
                    self._ghost_display_info.is_active = False
                self._stats["disconnects"] += 1
                logger.info("[phantom] Ghost display disconnected")
                return True

            logger.error(
                "[phantom] disconnect_async failed: %s (rc=%s)",
                combined or "(empty)",
                proc.returncode,
            )
            return False

        except asyncio.TimeoutError:
            logger.error("[phantom] disconnect_async timed out (10s)")
            return False
        except Exception as e:
            logger.error("[phantom] disconnect_async error: %s", e)
            return False

    async def reconnect_async(self, resolution: str = "") -> bool:
        """
        Reconnect (reactivate) the ghost display on the GPU framebuffer.

        If *resolution* is provided, ``set_resolution_async`` is called
        after a successful reconnect. Returns False when no CLI path is
        available or the subprocess fails.
        """
        if not self._cached_cli_path:
            logger.debug("[phantom] reconnect_async: no CLI path available")
            return False

        display_name = self.ghost_display_name.replace("_", " ")
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cached_cli_path, "set",
                f"-virtualScreenName={display_name}",
                "-connected=on",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10.0,
            )
            combined = (stdout.decode() + stderr.decode()).strip()

            if proc.returncode == 0 and "failed" not in combined.lower():
                if self._ghost_display_info is not None:
                    self._ghost_display_info.is_active = True
                self._stats["reconnects"] += 1
                logger.info("[phantom] Ghost display reconnected")

                if resolution:
                    return await self.set_resolution_async(resolution)
                return True

            logger.error(
                "[phantom] reconnect_async failed: %s (rc=%s)",
                combined or "(empty)",
                proc.returncode,
            )
            return False

        except asyncio.TimeoutError:
            logger.error("[phantom] reconnect_async timed out (10s)")
            return False
        except Exception as e:
            logger.error("[phantom] reconnect_async error: %s", e)
            return False

    async def get_current_mode_async(self) -> Dict[str, Any]:
        """
        Query the live resolution and connected state of the ghost display.

        Returns a dict with ``resolution`` (str), ``connected`` (bool),
        and ``raw_output`` (str). On any error the defaults are
        ``"unknown"`` / ``False``.
        """
        defaults: Dict[str, Any] = {
            "resolution": "unknown",
            "connected": False,
            "raw_output": "",
        }

        if not self._cached_cli_path:
            logger.debug("[phantom] get_current_mode_async: no CLI path available")
            return defaults

        display_name = self.ghost_display_name.replace("_", " ")
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cached_cli_path, "get",
                f"-virtualScreenName={display_name}",
                "-connected",
                "-resolution",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10.0,
            )
            raw = stdout.decode().strip()
            result: Dict[str, Any] = {
                "resolution": "unknown",
                "connected": False,
                "raw_output": raw,
            }

            for line in raw.splitlines():
                lower = line.strip().lower()
                if lower.startswith("resolution:"):
                    result["resolution"] = line.split(":", 1)[1].strip()
                elif lower.startswith("connected:"):
                    result["connected"] = "true" in lower

            return result

        except asyncio.TimeoutError:
            logger.error("[phantom] get_current_mode_async timed out (10s)")
            return defaults
        except Exception as e:
            logger.error("[phantom] get_current_mode_async error: %s", e)
            return defaults

    async def _wait_for_display_registration_async(
        self,
        max_wait_seconds: float = 15.0
    ) -> Optional[int]:
        """
        v68.0: Wait for newly-created display to be recognized by yabai.

        Uses polling loop with exponential backoff.
        """
        try:
            from backend.vision.yabai_space_detector import get_yabai_detector
        except ImportError:
            logger.debug("[v68.0] Yabai detector not available")
            return None

        yabai = get_yabai_detector()
        start_time = time.time()
        poll_interval = 0.5  # Start with 500ms
        self._last_registration_state = {
            "recognized_without_space": False,
            "display_count": 0,
            "ghost_space": None,
            "elapsed_seconds": 0.0,
        }
        recognized_without_space_at: Optional[float] = None
        last_analysis: Dict[str, Any] = {
            "recognized_without_space": False,
            "display_count": 0,
            "ghost_space": None,
        }

        while (time.time() - start_time) < max_wait_seconds:
            spaces = yabai.enumerate_all_spaces(include_display_info=True)
            analysis = self._analyze_yabai_spaces_for_registration(spaces)
            last_analysis = analysis
            ghost_space = analysis.get("ghost_space")

            if ghost_space is not None:
                elapsed = time.time() - start_time
                self._update_registration_latency_ema(elapsed)
                self._last_registration_state = {
                    **analysis,
                    "elapsed_seconds": elapsed,
                }
                logger.info(
                    f"[v68.0] ✅ Display registered with yabai (Space {ghost_space}) "
                    f"after {elapsed:.1f}s"
                )
                return ghost_space

            if analysis.get("recognized_without_space"):
                now = time.time()
                if recognized_without_space_at is None:
                    recognized_without_space_at = now
                    logger.info(
                        "[v68.0] Yabai now sees %s displays; waiting for ghost "
                        "space stabilization.",
                        analysis.get("display_count", "unknown"),
                    )
                elif (now - recognized_without_space_at) >= self._registration_stabilization_seconds:
                    elapsed = now - start_time
                    self._update_registration_latency_ema(elapsed)
                    self._last_registration_state = {
                        **analysis,
                        "elapsed_seconds": elapsed,
                    }
                    logger.info(
                        "[v68.0] Yabai display registration confirmed after %.1fs "
                        "(ghost space pending).",
                        elapsed,
                    )
                    return None

            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 2.0)  # Exponential backoff

        self._last_registration_state = {
            **last_analysis,
            "elapsed_seconds": max_wait_seconds,
        }
        if last_analysis.get("recognized_without_space"):
            logger.info(
                "[v68.0] Yabai recognized display topology but ghost space "
                "was not stable within %.1fs.",
                max_wait_seconds,
            )
            return None

        logger.warning(
            f"[v68.0] ⚠️ Display not recognized by yabai after {max_wait_seconds}s"
        )
        return None

    async def _verify_yabai_recognition_async(
        self,
        max_wait_seconds: float = 5.0
    ) -> Optional[int]:
        """Verify yabai can see the Ghost Display."""
        return await self._wait_for_display_registration_async(max_wait_seconds)

    # =========================================================================
    # PERMISSION CHECKING
    # =========================================================================

    async def check_permissions_async(self) -> Dict[str, Any]:
        """
        v68.0: Check all required permissions for Phantom Hardware operations.

        Returns dict with permission status for:
        - betterdisplay_cli: CLI is available and working
        - betterdisplay_app: App is running
        - driverkit_approved: DriverKit extension approved by user
        - display_control: Can create/modify displays
        """
        permissions = {
            "betterdisplay_cli": False,
            "betterdisplay_app": False,
            "driverkit_approved": None,  # Unknown until checked
            "display_control": False,
            "all_ok": False
        }

        # Check CLI
        cli_path = await self._discover_cli_path_async()
        permissions["betterdisplay_cli"] = cli_path is not None

        # Check app
        permissions["betterdisplay_app"] = await self._check_app_running_async()

        # Check DriverKit (requires system extensions check)
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemextensionsctl", "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            output = stdout.decode().lower()

            if "betterdisplay" in output:
                if "activated" in output or "enabled" in output:
                    permissions["driverkit_approved"] = True
                else:
                    permissions["driverkit_approved"] = False
            else:
                permissions["driverkit_approved"] = None  # Not installed

        except Exception:
            permissions["driverkit_approved"] = None

        # Overall status
        permissions["display_control"] = (
            permissions["betterdisplay_cli"] and
            permissions["betterdisplay_app"]
        )
        permissions["all_ok"] = permissions["display_control"]

        return permissions

    # =========================================================================
    # STATUS & UTILITIES
    # =========================================================================

    async def get_status_async(self) -> PhantomHardwareStatus:
        """Get comprehensive status of the Phantom Hardware system."""
        self._stats["total_queries"] += 1

        status = PhantomHardwareStatus(last_check=datetime.now())

        # CLI status
        cli_path = await self._discover_cli_path_async()
        status.cli_available = cli_path is not None
        status.cli_path = cli_path
        status.cli_version = self._cli_version

        # App status
        status.app_running = await self._check_app_running_async()

        # Display status (uses system_profiler fallback if CLI fails)
        existing = await self._find_existing_ghost_display_async(cli_path)
        if existing:
            status.ghost_display_active = True
            status.ghost_display_info = existing

        # Permissions
        permissions = await self.check_permissions_async()
        status.permissions_ok = permissions["all_ok"]
        status.driverkit_approved = permissions.get("driverkit_approved", False)

        return status

    async def get_display_status_async(self) -> PhantomHardwareStatus:
        """Backward-compatible alias used by supervisor health/recovery paths."""
        return await self.get_status_async()

    async def destroy_ghost_display_async(self) -> Tuple[bool, Optional[str]]:
        """
        v68.0: Remove the JARVIS Ghost Display.

        Use this when cleaning up or when user wants to disable virtual display.
        """
        cli_path = await self._discover_cli_path_async()
        if not cli_path:
            return False, "BetterDisplay CLI not found"

        try:
            # v251.2: Correct syntax is ``discard -nameLike=...``
            search_name = self.ghost_display_name.replace("_", " ")
            proc = await asyncio.create_subprocess_exec(
                cli_path, "discard", f"-nameLike={search_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                self._ghost_display_info = None
                logger.info(f"[v68.0] Destroyed Ghost Display '{self.ghost_display_name}'")
                return True, None

            return False, "Failed to discard display"

        except Exception as e:
            return False, str(e)

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        return {
            **self._stats,
            "ghost_display_name": self.ghost_display_name,
            "preferred_resolution": self.preferred_resolution,
            "cli_path": self._cached_cli_path,
            "cli_version": self._cli_version
        }


# =============================================================================
# v68.1: DISPLAY PRESSURE CONTROLLER — State Machine for Shedding / Recovery
# =============================================================================

class DisplayPressureController:
    """State machine for pressure-driven ghost display resolution management.

    Implements the shedding ladder (ACTIVE -> DEGRADED_1 -> DEGRADED_2 ->
    MINIMUM -> DISCONNECTED) and recovery ladder (reverse, one step at a time).
    All transitions use a two-phase protocol (prepare -> apply -> verify ->
    commit/rollback).

    Flap guards:
        - Dwell timer: minimum seconds between transitions
        - Cooldown: extra delay after any transition
        - Rate limit: max transitions per hour
        - Failure budget with quarantine per transition path

    Calibration:
        - Before/after memory snapshot deltas
        - Per-resolution exponential moving average (EMA)

    Events emitted (via broker._emit_event):
        DISPLAY_DEGRADE_REQUESTED, DISPLAY_DEGRADED,
        DISPLAY_DISCONNECT_REQUESTED, DISPLAY_DISCONNECTED,
        DISPLAY_RECOVERY_REQUESTED, DISPLAY_RECOVERED,
        DISPLAY_ACTION_FAILED, DISPLAY_ACTION_PHASE
    """

    # -- class-level tables (populated on first access) --

    _RESOLUTION_MAP: Dict[str, str] = {}     # DisplayState -> resolution
    _ESTIMATE_MAP: Dict[str, int] = {
        "1920x1080": 32_000_000,
        "1600x900": 22_000_000,
        "1280x720": 14_000_000,
        "1024x576": 9_000_000,
    }

    _SHED_ORDER: Dict[str, str] = {}         # state -> next-shed state
    _RECOVER_ORDER: Dict[str, str] = {}      # state -> next-recover state
    _SHED_TRIGGER: Dict[str, int] = {}       # target -> PressureTier threshold
    _CLEAR_TRIGGER: Dict[str, int] = {}      # current -> PressureTier clear level

    _tables_initialized = False

    @classmethod
    def _init_tables(cls):
        """Populate lookup tables once memory_types are loaded."""
        if cls._tables_initialized:
            return
        _ensure_memory_types()
        cls._RESOLUTION_MAP = {
            DisplayState.ACTIVE: "1920x1080",
            DisplayState.DEGRADED_1: "1600x900",
            DisplayState.DEGRADED_2: "1280x720",
            DisplayState.MINIMUM: "1024x576",
        }
        cls._SHED_ORDER = {
            DisplayState.ACTIVE: DisplayState.DEGRADED_1,
            DisplayState.DEGRADED_1: DisplayState.DEGRADED_2,
            DisplayState.DEGRADED_2: DisplayState.MINIMUM,
            DisplayState.MINIMUM: DisplayState.DISCONNECTED,
        }
        cls._RECOVER_ORDER = {
            DisplayState.DISCONNECTED: DisplayState.MINIMUM,
            DisplayState.MINIMUM: DisplayState.DEGRADED_2,
            DisplayState.DEGRADED_2: DisplayState.DEGRADED_1,
            DisplayState.DEGRADED_1: DisplayState.ACTIVE,
        }
        cls._SHED_TRIGGER = {
            DisplayState.DEGRADED_1: PressureTier.CONSTRAINED,
            DisplayState.DEGRADED_2: PressureTier.CRITICAL,
            DisplayState.MINIMUM: PressureTier.CRITICAL,
            DisplayState.DISCONNECTED: PressureTier.EMERGENCY,
        }
        cls._CLEAR_TRIGGER = {
            DisplayState.DEGRADED_1: PressureTier.OPTIMAL,
            DisplayState.DEGRADED_2: PressureTier.ELEVATED,
            DisplayState.MINIMUM: PressureTier.CONSTRAINED,
            DisplayState.DISCONNECTED: PressureTier.ELEVATED,
        }
        cls._tables_initialized = True

    # -- instance lifecycle --

    def __init__(self, phantom_mgr, broker) -> None:
        _ensure_memory_types()
        self.__class__._init_tables()

        self._phantom_mgr = phantom_mgr
        self._broker = broker
        self._state = DisplayState.INACTIVE
        self._lease_id: Optional[str] = None
        self._current_resolution: str = phantom_mgr.preferred_resolution
        self._last_transition_time: float = 0.0
        self._transition_timestamps: list = []
        self._failure_counts: Dict[str, int] = {}
        self._quarantined_until: Dict[str, float] = {}
        self._calibration_ema: Dict[str, float] = {}
        self._sequence_no: int = 0

        # All tunables are env-driven; no hardcoded magic numbers.
        self._degrade_dwell_s = float(
            os.environ.get("JARVIS_DISPLAY_DEGRADE_DWELL_S", "30"))
        self._recovery_dwell_s = float(
            os.environ.get("JARVIS_DISPLAY_RECOVERY_DWELL_S", "60"))
        self._cooldown_s = float(
            os.environ.get("JARVIS_DISPLAY_COOLDOWN_S", "20"))
        self._max_transitions_1h = int(
            os.environ.get("JARVIS_DISPLAY_MAX_TRANSITIONS_1H", "6"))
        self._lockout_duration_s = float(
            os.environ.get("JARVIS_DISPLAY_LOCKOUT_DURATION_S", "600"))
        self._verify_window_s = float(
            os.environ.get("JARVIS_DISPLAY_VERIFY_WINDOW_S", "5"))
        self._failure_budget = int(
            os.environ.get("JARVIS_DISPLAY_FAILURE_BUDGET", "3"))
        self._quarantine_duration_s = float(
            os.environ.get("JARVIS_DISPLAY_QUARANTINE_DURATION_S", "300"))
        self._latched_dep_s = float(
            os.environ.get("JARVIS_DISPLAY_LATCHED_DEPENDENCY_S", "30"))
        self._scale_factor = float(
            os.environ.get("JARVIS_DISPLAY_SCALE_FACTOR", "1.0"))
        self._refresh_factor = float(
            os.environ.get("JARVIS_DISPLAY_REFRESH_FACTOR", "1.0"))
        self._compositor_overhead = float(
            os.environ.get("JARVIS_DISPLAY_COMPOSITOR_OVERHEAD", "0.3"))

        broker.register_pressure_observer(self._on_pressure_change)

    # -- public properties --

    @property
    def state(self):
        """Current display lifecycle state."""
        return self._state

    # -- byte estimation --

    def estimate_bytes(self, resolution: str) -> int:
        """Return estimated framebuffer bytes for *resolution*.

        Uses calibration EMA when available, otherwise falls back to the
        static estimate map adjusted by scale / refresh / compositor factors.
        """
        if resolution in self._calibration_ema:
            return int(self._calibration_ema[resolution])
        base = self._ESTIMATE_MAP.get(resolution, 32_000_000)
        return int(
            base * self._scale_factor
            * self._refresh_factor
            * (1 + self._compositor_overhead)
        )

    # -- shedding target computation --

    def _compute_target_state(self, snapshot):
        """Determine the next shedding target, or None if no action needed.

        Enforces one-step-at-a-time, dwell timer, cooldown, and rate limit.
        """
        now = time.monotonic()

        # Rate limit: prune old timestamps, reject if over max.
        recent = [t for t in self._transition_timestamps if now - t < 3600]
        self._transition_timestamps = recent
        if len(recent) >= self._max_transitions_1h:
            return None

        # Dwell & cooldown: skip if no prior transition (sentinel 0).
        if self._last_transition_time > 0:
            dwell = self._degrade_dwell_s
            if now - self._last_transition_time < dwell:
                return None
            if now - self._last_transition_time < self._cooldown_s:
                return None

        tier = snapshot.pressure_tier
        thrash = str(
            getattr(snapshot.thrash_state, "value", snapshot.thrash_state)
        ).lower()

        # One step only: look at the immediate next shed state.
        next_state = self._SHED_ORDER.get(self._state)
        if next_state is None:
            return None

        trigger = self._SHED_TRIGGER.get(next_state)
        if trigger is None:
            return None

        # MINIMUM has a tighter gate — requires thrashing signal too.
        if next_state == DisplayState.MINIMUM:
            if (tier >= PressureTier.CRITICAL
                    and thrash in ("thrashing", "emergency")):
                return next_state
            return None

        # General case: shed if tier >= trigger.
        if tier >= trigger:
            return next_state

        return None

    # -- recovery target computation --

    def _compute_recovery_target(self, snapshot):
        """Determine the next recovery target, or None.

        Recovery is more conservative than shedding: longer dwell, requires
        swap hysteresis to be clear and pressure trend to not be rising.
        """
        now = time.monotonic()

        if self._last_transition_time > 0:
            if now - self._last_transition_time < self._recovery_dwell_s:
                return None

        if snapshot.swap_hysteresis_active:
            return None
        trend = str(
            getattr(snapshot.pressure_trend, "value", snapshot.pressure_trend)
        ).lower()
        if trend == "rising":
            return None

        tier = snapshot.pressure_tier
        clear_tier = self._CLEAR_TRIGGER.get(self._state)
        if clear_tier is None:
            return None

        if tier <= clear_tier:
            return self._RECOVER_ORDER.get(self._state)

        return None

    # -- dependency-aware disconnect --

    def _check_disconnect_dependencies(self):
        """Return (blocked: bool, reason: str).

        Scans active leases for ``requires_display`` metadata.
        """
        try:
            active = self._broker.get_active_leases()
            blocking = []
            for lease in active:
                meta = getattr(lease, "metadata", None) or {}
                if meta.get("requires_display"):
                    if not getattr(lease.state, "is_terminal", False):
                        blocking.append(lease.component_id)
            if blocking:
                return True, f"Blocked by: {', '.join(blocking)}"
        except Exception as e:
            logger.warning("Dependency check failed: %s", e)
            return True, f"Dependency check error: {e}"
        return False, ""

    # -- pressure observer callback --

    async def _on_pressure_change(self, tier, snapshot) -> None:
        """Called by the broker when pressure tier changes."""
        if self._state == DisplayState.INACTIVE:
            return
        if self._state.is_transitional:
            return

        target = self._compute_target_state(snapshot)
        if target is not None:
            await self._execute_transition(
                target, snapshot, direction="degrade")
            return

        target = self._compute_recovery_target(snapshot)
        if target is not None:
            await self._execute_transition(
                target, snapshot, direction="recover")

    # -- two-phase transition execution --

    async def _execute_transition(
        self, target, snapshot, *, direction: str,
    ) -> bool:
        """Execute a two-phase transition (prepare -> apply -> verify ->
        commit/rollback).

        Returns True on success, False on failure (with rollback).
        """
        from_state = self._state
        action_id = f"act_{self._sequence_no:04d}"
        self._sequence_no += 1

        transition_key = f"{from_state.value}->{target.value}"

        # Quarantine check.
        if time.monotonic() < self._quarantined_until.get(transition_key, 0):
            return False

        # Dependency gate for disconnect.
        if target == DisplayState.DISCONNECTED:
            blocked, reason = self._check_disconnect_dependencies()
            if blocked:
                self._emit_display_event(
                    MemoryBudgetEventType.DISPLAY_ACTION_FAILED,
                    from_state, target, snapshot, action_id,
                    failure_code="DEPENDENCY_BLOCKED",
                    extra={"dependency_reason": reason},
                )
                return False

        # Phase 1: enter transitional state.
        transitional = {
            "degrade": DisplayState.DEGRADING,
            "recover": DisplayState.RECOVERING,
        }.get(direction, DisplayState.DEGRADING)
        if target == DisplayState.DISCONNECTED:
            transitional = DisplayState.DISCONNECTING

        self._state = transitional
        pre_free = getattr(snapshot, "physical_free", 0)

        # Emit *_REQUESTED event.
        req_event = {
            MemoryBudgetEventType.DISPLAY_DEGRADE_REQUESTED: (
                direction == "degrade"
                and target != DisplayState.DISCONNECTED
            ),
            MemoryBudgetEventType.DISPLAY_RECOVERY_REQUESTED: (
                direction == "recover"
            ),
            MemoryBudgetEventType.DISPLAY_DISCONNECT_REQUESTED: (
                target == DisplayState.DISCONNECTED
            ),
        }
        for evt, condition in req_event.items():
            if condition:
                self._emit_display_event(
                    evt, from_state, target, snapshot, action_id)
                break

        # Phase 2: apply the hardware action.
        success = False
        try:
            if target == DisplayState.DISCONNECTED:
                success = await self._phantom_mgr.disconnect_async()
            elif target in self._RESOLUTION_MAP:
                target_res = self._RESOLUTION_MAP[target]
                if from_state == DisplayState.DISCONNECTED:
                    success = await self._phantom_mgr.reconnect_async(
                        target_res)
                else:
                    success = await self._phantom_mgr.set_resolution_async(
                        target_res)
            else:
                success = False
        except Exception as e:
            logger.error("Display action failed: %s", e)
            success = False

        if not success:
            self._state = from_state
            self._failure_counts[transition_key] = (
                self._failure_counts.get(transition_key, 0) + 1
            )
            if self._failure_counts[transition_key] >= self._failure_budget:
                self._quarantined_until[transition_key] = (
                    time.monotonic() + self._quarantine_duration_s
                )
            self._emit_display_event(
                MemoryBudgetEventType.DISPLAY_ACTION_FAILED,
                from_state, target, snapshot, action_id,
                failure_code="CLI_ERROR",
            )
            return False

        # Phase 3: verify the action took effect.
        await asyncio.sleep(self._verify_window_s)
        mode = await self._phantom_mgr.get_current_mode_async()
        verify_ok = True
        if target == DisplayState.DISCONNECTED:
            verify_ok = not mode.get("connected", True)
        elif target in self._RESOLUTION_MAP:
            expected_res = self._RESOLUTION_MAP[target]
            actual_res = mode.get("resolution", "")
            verify_ok = (
                expected_res in actual_res or actual_res in expected_res
            )

        if not verify_ok:
            # Rollback.
            self._state = from_state
            self._failure_counts[transition_key] = (
                self._failure_counts.get(transition_key, 0) + 1
            )
            if self._failure_counts[transition_key] >= self._failure_budget:
                self._quarantined_until[transition_key] = (
                    time.monotonic() + self._quarantine_duration_s
                )
            self._emit_display_event(
                MemoryBudgetEventType.DISPLAY_ACTION_FAILED,
                from_state, target, snapshot, action_id,
                failure_code="VERIFY_MISMATCH",
            )
            return False

        # Phase 4: commit.
        self._state = target
        self._current_resolution = self._RESOLUTION_MAP.get(target, "")
        self._last_transition_time = time.monotonic()
        self._transition_timestamps.append(time.monotonic())
        self._failure_counts.pop(transition_key, None)

        # Amend or release the lease.
        if self._lease_id and target in self._RESOLUTION_MAP:
            new_bytes = self.estimate_bytes(self._RESOLUTION_MAP[target])
            try:
                await self._broker.amend_lease_bytes(
                    self._lease_id, new_bytes)
            except Exception as e:
                logger.warning("Failed to amend lease bytes: %s", e)
        elif self._lease_id and target == DisplayState.DISCONNECTED:
            try:
                await self._broker.release(self._lease_id)
                self._lease_id = None
            except Exception as e:
                logger.warning("Failed to release display lease: %s", e)

        # Emit success event.
        success_events = {
            "degrade": MemoryBudgetEventType.DISPLAY_DEGRADED,
            "recover": MemoryBudgetEventType.DISPLAY_RECOVERED,
        }
        if target == DisplayState.DISCONNECTED:
            evt = MemoryBudgetEventType.DISPLAY_DISCONNECTED
        else:
            evt = success_events.get(
                direction, MemoryBudgetEventType.DISPLAY_DEGRADED)
        self._emit_display_event(
            evt, from_state, target, snapshot, action_id)

        # Calibration: capture post-transition memory delta.
        try:
            from backend.core.memory_quantizer import (
                get_memory_quantizer_instance,
            )
            _mq = get_memory_quantizer_instance()
            if _mq is not None:
                post_snap = await _mq.snapshot()
                post_free = getattr(post_snap, "physical_free", 0)
                delta = post_free - pre_free
                res = self._RESOLUTION_MAP.get(target, "unknown")
                if res in self._calibration_ema:
                    self._calibration_ema[res] = (
                        0.8 * self._calibration_ema[res] + 0.2 * abs(delta)
                    )
                else:
                    self._calibration_ema[res] = (
                        abs(delta) if delta != 0
                        else self._ESTIMATE_MAP.get(res, 0)
                    )
        except Exception:
            pass

        return True

    # -- event emission --

    def _emit_display_event(
        self, event_type, from_state, to_state, snapshot, action_id,
        *, failure_code=None, extra=None,
    ) -> None:
        """Emit a structured display lifecycle event via the broker."""
        data = {
            "from_state": (
                from_state.value
                if hasattr(from_state, "value") else str(from_state)
            ),
            "to_state": (
                to_state.value
                if hasattr(to_state, "value") else str(to_state)
            ),
            "trigger_tier": (
                snapshot.pressure_tier.name
                if hasattr(snapshot.pressure_tier, "name")
                else str(snapshot.pressure_tier)
            ),
            "snapshot_id": getattr(snapshot, "snapshot_id", "unknown"),
            "lease_id": self._lease_id,
            "action_id": action_id,
            "sequence_no": self._sequence_no,
            "from_resolution": self._current_resolution,
            "to_resolution": (
                self._RESOLUTION_MAP.get(to_state, "none")
                if hasattr(to_state, "value") else "none"
            ),
            "ts_monotonic": time.monotonic(),
            "event_schema_version": "1.0",
            "state_machine_version": "1.0",
        }
        if failure_code:
            data["failure_code"] = failure_code
        if extra:
            data.update(extra)
        self._broker._emit_event(event_type, data)

    # -- public lifecycle helpers --

    async def activate(self, lease_id: str, resolution: str) -> None:
        """Mark the controller as active with a granted lease."""
        _ensure_memory_types()
        self._lease_id = lease_id
        self._state = DisplayState.ACTIVE
        self._current_resolution = resolution
        self._last_transition_time = time.monotonic()

    async def shutdown(self) -> None:
        """Unregister from pressure observer and release any held lease."""
        _ensure_memory_types()
        self._broker.unregister_pressure_observer(self._on_pressure_change)
        if self._lease_id:
            try:
                await self._broker.release(self._lease_id)
            except Exception:
                pass
            self._lease_id = None
        self._state = DisplayState.INACTIVE


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

_phantom_manager_instance: Optional[PhantomHardwareManager] = None


def get_phantom_manager() -> PhantomHardwareManager:
    """Get the singleton PhantomHardwareManager instance."""
    global _phantom_manager_instance
    if _phantom_manager_instance is None:
        _phantom_manager_instance = PhantomHardwareManager()
    return _phantom_manager_instance


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

async def ensure_ghost_display() -> Tuple[bool, Optional[str]]:
    """Convenience function to ensure Ghost Display exists."""
    manager = get_phantom_manager()
    return await manager.ensure_ghost_display_exists_async()


async def get_phantom_status() -> PhantomHardwareStatus:
    """Get current Phantom Hardware status."""
    manager = get_phantom_manager()
    return await manager.get_status_async()
