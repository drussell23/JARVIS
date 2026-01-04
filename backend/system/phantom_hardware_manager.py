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
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        # STEP 1: Discover BetterDisplay CLI
        # =================================================================
        cli_path = await self._discover_cli_path_async()

        if not cli_path:
            error_msg = (
                "BetterDisplay CLI not found. Please install BetterDisplay from "
                "https://betterdisplay.pro/ or use a physical HDMI dummy plug."
            )
            logger.warning(f"[v68.0] ❌ {error_msg}")
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
                logger.warning(f"[v68.0] ❌ {error_msg}")
                return False, error_msg

            # Wait for app to initialize
            await asyncio.sleep(2.0)

        # =================================================================
        # STEP 3: Check if Ghost Display Already Exists
        # =================================================================
        existing_display = await self._find_existing_ghost_display_async(cli_path)

        if existing_display:
            logger.info(
                f"[v68.0] ✅ Ghost Display '{self.ghost_display_name}' already exists "
                f"(ID: {existing_display.display_id})"
            )
            self._ghost_display_info = existing_display

            # Verify yabai can see it
            if wait_for_registration:
                space_id = await self._verify_yabai_recognition_async(max_wait_seconds)
                if space_id:
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
            space_id = await self._wait_for_display_registration_async(max_wait_seconds)

            if space_id is None:
                logger.warning(
                    "[v68.0] ⚠️ Display created but yabai hasn't recognized it yet. "
                    "It may appear shortly."
                )

            if self._ghost_display_info:
                self._ghost_display_info.space_id = space_id

        logger.info(f"[v68.0] ✅ Ghost Display '{self.ghost_display_name}' is ready")
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

        logger.warning("[v68.0] BetterDisplay CLI not found")
        return None

    async def _verify_cli_works_async(self, cli_path: str) -> bool:
        """Verify the CLI is executable and responds."""
        try:
            proc = await asyncio.create_subprocess_exec(
                cli_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)

            if proc.returncode == 0:
                self._cli_version = stdout.decode().strip()
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

            return proc.returncode == 0 and stdout.decode().strip()

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
        cli_path: str
    ) -> Optional[VirtualDisplayInfo]:
        """Check if JARVIS Ghost Display already exists."""
        try:
            # Query all virtual displays
            proc = await asyncio.create_subprocess_exec(
                cli_path, "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0:
                return None

            output = stdout.decode()

            # Parse output to find our ghost display
            # BetterDisplay CLI output format varies by version
            if self.ghost_display_name.lower() in output.lower():
                # Found our display - parse details
                return VirtualDisplayInfo(
                    name=self.ghost_display_name,
                    is_active=True,
                    is_jarvis_ghost=True,
                    created_at=datetime.now()
                )

        except Exception as e:
            logger.debug(f"[v68.0] Error checking existing displays: {e}")

        return None

    async def _create_virtual_display_async(
        self,
        cli_path: str
    ) -> Tuple[bool, Optional[str]]:
        """
        v68.0: Create a new virtual display using BetterDisplay CLI.

        The display is created with:
        - Custom name (JARVIS_GHOST)
        - Preferred resolution (1920x1080 default)
        - 16:9 aspect ratio
        """
        logger.info(
            f"[v68.0] Creating virtual display: {self.ghost_display_name} "
            f"({self.preferred_resolution})"
        )

        try:
            # Parse resolution
            width, height = self.preferred_resolution.split('x')

            # Build CLI command
            # BetterDisplay CLI create command syntax may vary by version
            # Try multiple command formats
            commands_to_try = [
                # Modern format
                [cli_path, "create", "-name", self.ghost_display_name,
                 "-resolution", self.preferred_resolution],
                # Alternative format
                [cli_path, "create", "--name", self.ghost_display_name,
                 "--width", width, "--height", height],
                # Simpler format
                [cli_path, "create", self.ghost_display_name,
                 self.preferred_resolution],
            ]

            for cmd in commands_to_try:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

                    if proc.returncode == 0:
                        self._ghost_display_info = VirtualDisplayInfo(
                            name=self.ghost_display_name,
                            resolution=self.preferred_resolution,
                            is_active=True,
                            is_jarvis_ghost=True,
                            created_at=datetime.now()
                        )

                        logger.info(f"[v68.0] ✅ Virtual display created successfully")
                        return True, None

                    # Check for specific errors
                    error_output = stderr.decode() + stdout.decode()
                    if "already exists" in error_output.lower():
                        # Display already exists - this is fine
                        self._ghost_display_info = VirtualDisplayInfo(
                            name=self.ghost_display_name,
                            resolution=self.preferred_resolution,
                            is_active=True,
                            is_jarvis_ghost=True
                        )
                        return True, None

                except asyncio.TimeoutError:
                    logger.debug(f"[v68.0] Command timed out: {cmd}")
                    continue

            # All commands failed
            return False, "Failed to create virtual display - CLI command failed"

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

        # Display status
        if cli_path:
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
            # Try to delete the display
            proc = await asyncio.create_subprocess_exec(
                cli_path, "delete", "-name", self.ghost_display_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                self._ghost_display_info = None
                logger.info(f"[v68.0] Destroyed Ghost Display '{self.ghost_display_name}'")
                return True, None

            return False, "Failed to delete display"

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
