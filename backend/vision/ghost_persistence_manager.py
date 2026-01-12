"""
Ghost Persistence Manager - Crash Recovery for Window Teleportation
====================================================================

Fixes the "Amnesia" Risk:
- If JARVIS crashes while windows are on the Ghost Display, the in-memory
  geometry cache is lost. Windows become "stranded" with no way to return.

Solution:
- Persist window state to disk BEFORE teleportation
- On startup, detect stranded windows and automatically repatriate them
- Atomic file operations to prevent corruption during crashes

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  GhostPersistenceManager                                            │
    │  ├── StateWriter (atomic JSON writes before teleportation)          │
    │  ├── StateReader (load state on startup)                            │
    │  ├── StrandedWindowAuditor (detect orphaned windows)                │
    │  └── Repatriator (return stranded windows to original spaces)       │
    └─────────────────────────────────────────────────────────────────────┘

State File: ~/.jarvis/ghost_state.json
Format:
{
    "version": "1.0",
    "last_updated": "2024-01-15T10:30:00",
    "session_id": "abc123",
    "windows": {
        "12345": {
            "window_id": 12345,
            "app_name": "Chrome",
            "original_space": 4,
            "original_x": 100,
            "original_y": 200,
            "original_width": 800,
            "original_height": 600,
            "ghost_space": 10,
            "teleported_at": "2024-01-15T10:30:00",
            "z_order": 5
        }
    }
}

Author: JARVIS v27.0 - Crash Recovery
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class PersistedWindowState:
    """Persistent state for a window on the Ghost Display."""
    window_id: int
    app_name: str
    original_space: int
    original_x: int
    original_y: int
    original_width: int
    original_height: int
    ghost_space: int
    teleported_at: str
    z_order: int = 0
    original_display: Optional[int] = None
    was_minimized: bool = False
    was_fullscreen: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PersistedWindowState:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class GhostPersistenceState:
    """Complete persistence state."""
    version: str = "1.0"
    last_updated: str = ""
    session_id: str = ""
    windows: Dict[str, PersistedWindowState] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "last_updated": self.last_updated,
            "session_id": self.session_id,
            "windows": {
                str(k): v.to_dict() for k, v in self.windows.items()
            }
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> GhostPersistenceState:
        windows = {}
        for k, v in data.get("windows", {}).items():
            try:
                windows[k] = PersistedWindowState.from_dict(v)
            except Exception as e:
                logger.warning(f"Failed to load window state {k}: {e}")

        return cls(
            version=data.get("version", "1.0"),
            last_updated=data.get("last_updated", ""),
            session_id=data.get("session_id", ""),
            windows=windows,
        )


@dataclass
class StrandedWindowInfo:
    """Information about a stranded window."""
    window_id: int
    app_name: str
    original_space: int
    original_geometry: Tuple[int, int, int, int]  # x, y, w, h
    time_stranded: str
    still_exists: bool = True
    still_on_ghost: bool = True
    repatriation_possible: bool = True
    repatriation_error: Optional[str] = None


# =============================================================================
# Ghost Persistence Manager
# =============================================================================

class GhostPersistenceManager:
    """
    Manages persistent state for Ghost Display window teleportation.

    Key Features:
    1. Atomic file writes to prevent corruption during crashes
    2. Startup audit to detect stranded windows from previous sessions
    3. Automatic repatriation with narration
    4. Session-based state isolation (prevents stale state accumulation)
    """

    def __init__(
        self,
        state_dir: str = "~/.jarvis",
        state_file: str = "ghost_state.json",
        backup_count: int = 3,
        auto_save_interval: float = 5.0,
    ):
        self.state_dir = Path(os.path.expanduser(state_dir))
        self.state_file = self.state_dir / state_file
        self.backup_count = backup_count
        self.auto_save_interval = auto_save_interval

        # Current session state
        self._session_id = str(uuid.uuid4())[:8]
        self._state = GhostPersistenceState(session_id=self._session_id)
        self._dirty = False
        self._lock = asyncio.Lock()
        self._auto_save_task: Optional[asyncio.Task] = None

        # Ensure state directory exists
        self.state_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"[GhostPersistence] Initialized (session={self._session_id}, "
            f"state_file={self.state_file})"
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def startup(self) -> List[StrandedWindowInfo]:
        """
        Initialize persistence and audit for stranded windows.

        Returns:
            List of stranded windows from previous sessions
        """
        async with self._lock:
            # Load existing state
            await self._load_state()

            # Audit for stranded windows
            stranded = await self._audit_stranded_windows()

            # Start auto-save
            self._start_auto_save()

            return stranded

    async def shutdown(self) -> None:
        """Clean shutdown - save state and stop auto-save."""
        # Stop auto-save
        if self._auto_save_task:
            self._auto_save_task.cancel()
            try:
                await self._auto_save_task
            except asyncio.CancelledError:
                pass

        # Final save
        async with self._lock:
            await self._save_state()

        logger.info("[GhostPersistence] Shutdown complete")

    # =========================================================================
    # State Management
    # =========================================================================

    async def record_teleportation(
        self,
        window_id: int,
        app_name: str,
        original_space: int,
        original_x: int,
        original_y: int,
        original_width: int,
        original_height: int,
        ghost_space: int,
        z_order: int = 0,
        original_display: Optional[int] = None,
        was_minimized: bool = False,
        was_fullscreen: bool = False,
    ) -> None:
        """
        Record a window teleportation BEFORE it happens.

        This is the critical crash-safety mechanism: we persist the state
        BEFORE the teleportation so that if we crash mid-teleport, we know
        where the window should be returned to.
        """
        async with self._lock:
            state = PersistedWindowState(
                window_id=window_id,
                app_name=app_name,
                original_space=original_space,
                original_x=original_x,
                original_y=original_y,
                original_width=original_width,
                original_height=original_height,
                ghost_space=ghost_space,
                teleported_at=datetime.now().isoformat(),
                z_order=z_order,
                original_display=original_display,
                was_minimized=was_minimized,
                was_fullscreen=was_fullscreen,
            )

            self._state.windows[str(window_id)] = state
            self._state.last_updated = datetime.now().isoformat()
            self._dirty = True

            # Synchronous save for crash safety
            await self._save_state()

            logger.debug(
                f"[GhostPersistence] Recorded teleportation: "
                f"Window {window_id} ({app_name}) → Ghost Space {ghost_space}"
            )

    async def record_return(self, window_id: int) -> None:
        """
        Record that a window has been successfully returned.

        Only remove from persistence AFTER successful return.
        """
        async with self._lock:
            key = str(window_id)
            if key in self._state.windows:
                del self._state.windows[key]
                self._state.last_updated = datetime.now().isoformat()
                self._dirty = True

                # Synchronous save
                await self._save_state()

                logger.debug(
                    f"[GhostPersistence] Recorded return: Window {window_id}"
                )

    async def get_window_state(
        self,
        window_id: int
    ) -> Optional[PersistedWindowState]:
        """Get persisted state for a specific window."""
        async with self._lock:
            return self._state.windows.get(str(window_id))

    async def get_all_window_states(self) -> Dict[int, PersistedWindowState]:
        """Get all persisted window states."""
        async with self._lock:
            return {
                int(k): v for k, v in self._state.windows.items()
            }

    # =========================================================================
    # Stranded Window Handling
    # =========================================================================

    async def _audit_stranded_windows(self) -> List[StrandedWindowInfo]:
        """
        Audit for stranded windows from previous sessions.

        A window is stranded if:
        1. It's in our state file (was teleported)
        2. The session ID doesn't match (from a previous session)
        3. The window still exists
        4. The window is still on the Ghost Display (not manually moved)
        """
        stranded = []

        # Check each persisted window
        for key, state in list(self._state.windows.items()):
            # Only audit windows from previous sessions
            if self._state.session_id == self._session_id:
                # Same session - not stranded, just in-progress
                continue

            # Check if window still exists and where it is
            exists, current_space = await self._check_window_location(
                state.window_id
            )

            info = StrandedWindowInfo(
                window_id=state.window_id,
                app_name=state.app_name,
                original_space=state.original_space,
                original_geometry=(
                    state.original_x,
                    state.original_y,
                    state.original_width,
                    state.original_height,
                ),
                time_stranded=state.teleported_at,
                still_exists=exists,
                still_on_ghost=current_space == state.ghost_space if exists else False,
                repatriation_possible=exists and current_space == state.ghost_space,
            )

            if info.still_exists and info.still_on_ghost:
                stranded.append(info)
                logger.info(
                    f"[GhostPersistence] Found stranded window: "
                    f"{state.window_id} ({state.app_name}) from session {self._state.session_id}"
                )
            elif not exists:
                # Window no longer exists - clean up
                del self._state.windows[key]
                self._dirty = True
                logger.debug(
                    f"[GhostPersistence] Cleaned up non-existent window: {state.window_id}"
                )
            elif not info.still_on_ghost:
                # Window was manually moved - clean up
                del self._state.windows[key]
                self._dirty = True
                logger.debug(
                    f"[GhostPersistence] Cleaned up manually moved window: {state.window_id}"
                )

        if self._dirty:
            await self._save_state()

        return stranded

    async def _check_window_location(
        self,
        window_id: int
    ) -> Tuple[bool, Optional[int]]:
        """Check if a window exists and get its current space."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "yabai", "-m", "query", "--windows", f"--window", str(window_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0 or not stdout:
                return (False, None)

            data = json.loads(stdout.decode())
            space = data.get("space")
            return (True, space)

        except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
            logger.debug(f"[GhostPersistence] Window check failed: {e}")
            return (False, None)

    async def repatriate_stranded_windows(
        self,
        stranded: List[StrandedWindowInfo],
        narrate_callback: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """
        Repatriate stranded windows to their original spaces.

        Args:
            stranded: List of stranded windows to repatriate
            narrate_callback: Optional async callback for voice narration

        Returns:
            Result summary with success/failure counts
        """
        if not stranded:
            return {"success": 0, "failed": 0, "skipped": 0}

        success_count = 0
        failed_count = 0
        skipped_count = 0

        # Narrate start
        if narrate_callback:
            await narrate_callback(
                f"I found {len(stranded)} windows stranded on the Ghost Display "
                f"from a previous session. Moving them back to safety."
            )

        for info in stranded:
            if not info.repatriation_possible:
                skipped_count += 1
                continue

            try:
                # Get the persisted state
                state = self._state.windows.get(str(info.window_id))
                if not state:
                    skipped_count += 1
                    continue

                # Move window back to original space
                success = await self._move_window_to_space(
                    info.window_id,
                    state.original_space,
                    state.original_x,
                    state.original_y,
                    state.original_width,
                    state.original_height,
                )

                if success:
                    success_count += 1
                    # Record successful return
                    await self.record_return(info.window_id)
                    logger.info(
                        f"[GhostPersistence] Repatriated window {info.window_id} "
                        f"({info.app_name}) to Space {state.original_space}"
                    )
                else:
                    failed_count += 1
                    info.repatriation_error = "Move command failed"

            except Exception as e:
                failed_count += 1
                info.repatriation_error = str(e)
                logger.error(
                    f"[GhostPersistence] Failed to repatriate window {info.window_id}: {e}"
                )

        # Narrate completion
        if narrate_callback:
            if success_count > 0:
                await narrate_callback(
                    f"Search and Rescue complete! Safely returned {success_count} windows."
                )
            if failed_count > 0:
                await narrate_callback(
                    f"Couldn't recover {failed_count} windows. They may have been closed."
                )

        return {
            "success": success_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "total": len(stranded),
        }

    async def _move_window_to_space(
        self,
        window_id: int,
        space: int,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> bool:
        """
        v34.0: Move a window to a specific space with Display Handoff support.

        For cross-display moves, uses --display instead of --space to bypass
        Scripting Addition requirements.
        """
        try:
            # ═══════════════════════════════════════════════════════════════
            # v34.0: DETECT CROSS-DISPLAY MOVE
            # ═══════════════════════════════════════════════════════════════

            current_display = None
            target_display = None

            # Get window's current display
            try:
                proc = await asyncio.create_subprocess_exec(
                    "yabai", "-m", "query", "--windows", "--window", str(window_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    win_data = json.loads(stdout.decode())
                    current_display = win_data.get("display")
            except Exception:
                pass

            # Get target space's display
            try:
                proc = await asyncio.create_subprocess_exec(
                    "yabai", "-m", "query", "--spaces",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0 and stdout:
                    spaces = json.loads(stdout.decode())
                    for s in spaces:
                        if s.get("index") == space:
                            target_display = s.get("display")
                            break
            except Exception:
                pass

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
                    f"[GhostPersistence] 🌐 DISPLAY HANDOFF: Window {window_id} "
                    f"from Display {current_display} → Display {target_display}"
                )
                proc = await asyncio.create_subprocess_exec(
                    "yabai", "-m", "window", str(window_id),
                    "--display", str(target_display),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                # SAME-DISPLAY: Use standard --space command
                proc = await asyncio.create_subprocess_exec(
                    "yabai", "-m", "window", str(window_id),
                    "--space", str(space),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

            await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0:
                return False

            # Small delay for space switch / texture rehydration
            await asyncio.sleep(0.5 if is_cross_display else 0.3)

            # Restore geometry
            proc = await asyncio.create_subprocess_exec(
                "yabai", "-m", "window", str(window_id),
                "--move", f"abs:{x}:{y}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)

            proc = await asyncio.create_subprocess_exec(
                "yabai", "-m", "window", str(window_id),
                "--resize", f"abs:{width}:{height}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)

            return True

        except Exception as e:
            logger.error(f"[GhostPersistence] Move failed: {e}")
            return False

    # =========================================================================
    # File I/O
    # =========================================================================

    async def _load_state(self) -> None:
        """Load state from disk."""
        if not self.state_file.exists():
            logger.debug("[GhostPersistence] No existing state file")
            return

        try:
            # Read in executor to avoid blocking
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                None,
                self.state_file.read_text,
            )

            data = json.loads(content)
            self._state = GhostPersistenceState.from_dict(data)

            logger.info(
                f"[GhostPersistence] Loaded state: "
                f"{len(self._state.windows)} windows from session {self._state.session_id}"
            )

        except json.JSONDecodeError as e:
            logger.error(f"[GhostPersistence] Corrupted state file: {e}")
            await self._backup_corrupted_state()
        except Exception as e:
            logger.error(f"[GhostPersistence] Failed to load state: {e}")

    async def _save_state(self) -> None:
        """Save state to disk atomically."""
        if not self._dirty:
            return

        try:
            # Update session and timestamp
            self._state.session_id = self._session_id
            self._state.last_updated = datetime.now().isoformat()

            # Serialize
            content = json.dumps(self._state.to_dict(), indent=2)

            # Atomic write: write to temp file, then rename
            loop = asyncio.get_event_loop()

            def atomic_write():
                # Create temp file in same directory (for same-filesystem rename)
                fd, temp_path = tempfile.mkstemp(
                    dir=self.state_dir,
                    suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, 'w') as f:
                        f.write(content)
                        f.flush()
                        os.fsync(f.fileno())  # Ensure data is on disk

                    # Atomic rename
                    shutil.move(temp_path, self.state_file)
                except Exception:
                    # Clean up temp file on error
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                    raise

            await loop.run_in_executor(None, atomic_write)
            self._dirty = False

            logger.debug(
                f"[GhostPersistence] Saved state: {len(self._state.windows)} windows"
            )

        except Exception as e:
            logger.error(f"[GhostPersistence] Failed to save state: {e}")

    async def _backup_corrupted_state(self) -> None:
        """Backup corrupted state file."""
        if not self.state_file.exists():
            return

        try:
            backup_path = self.state_file.with_suffix(
                f".corrupted.{int(time.time())}.json"
            )
            shutil.copy2(self.state_file, backup_path)
            logger.info(f"[GhostPersistence] Backed up corrupted state to {backup_path}")
        except Exception as e:
            logger.error(f"[GhostPersistence] Failed to backup: {e}")

    # =========================================================================
    # Auto-Save
    # =========================================================================

    def _start_auto_save(self) -> None:
        """Start auto-save background task."""
        if self._auto_save_task is not None:
            return

        self._auto_save_task = asyncio.create_task(self._auto_save_loop())
        logger.debug("[GhostPersistence] Auto-save started")

    async def _auto_save_loop(self) -> None:
        """Periodic auto-save."""
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        while time.monotonic() - session_start < max_runtime:
            await asyncio.sleep(self.auto_save_interval)
            async with self._lock:
                if self._dirty:
                    await self._save_state()
        else:
            logger.info("Ghost persistence auto-save loop timeout, stopping")


# =============================================================================
# Module-level convenience functions
# =============================================================================

_manager_instance: Optional[GhostPersistenceManager] = None


def get_persistence_manager(**kwargs) -> GhostPersistenceManager:
    """Get or create the global persistence manager instance."""
    global _manager_instance

    if _manager_instance is None:
        _manager_instance = GhostPersistenceManager(**kwargs)

    return _manager_instance


async def startup_persistence() -> List[StrandedWindowInfo]:
    """Convenience function to start persistence and audit stranded windows."""
    manager = get_persistence_manager()
    return await manager.startup()


async def record_teleportation(**kwargs) -> None:
    """Convenience function to record a teleportation."""
    manager = get_persistence_manager()
    await manager.record_teleportation(**kwargs)


async def record_return(window_id: int) -> None:
    """Convenience function to record a window return."""
    manager = get_persistence_manager()
    await manager.record_return(window_id)
