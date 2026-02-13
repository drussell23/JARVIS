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
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


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
    _lock = asyncio.Lock()

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

        # Stats
        self._stats = {
            "displays_created": 0,
            "cli_discoveries": 0,
            "registration_waits": 0,
            "total_queries": 0
        }

        logger.info("[v68.0] 👻 PHANTOM HARDWARE: Manager initialized")

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
        # STEP 1: Discover BetterDisplay CLI
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
        # STEP 2: Verify BetterDisplay.app is Running
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
                logger.warning(f"[v68.0] {error_msg}")
                return False, error_msg

            # Wait for app to initialize
            await asyncio.sleep(2.0)

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
            space_id = await self._wait_for_display_registration_async(
                max_wait_seconds
            )

            if space_id is None:
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
                    return True, None

                # "already exists" is success
                if "already exists" in combined.lower():
                    self._ghost_display_info = VirtualDisplayInfo(
                        name=self.ghost_display_name,
                        resolution=self.preferred_resolution,
                        is_active=True,
                        is_jarvis_ghost=True,
                    )
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

        while (time.time() - start_time) < max_wait_seconds:
            ghost_space = yabai.get_ghost_display_space()

            if ghost_space is not None:
                elapsed = time.time() - start_time
                logger.info(
                    f"[v68.0] ✅ Display registered with yabai (Space {ghost_space}) "
                    f"after {elapsed:.1f}s"
                )
                return ghost_space

            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 2.0)  # Exponential backoff

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
