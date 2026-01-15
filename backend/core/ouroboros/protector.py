"""
Rollback Protector Module for Ouroboros
========================================

Provides safety mechanisms for code evolution:
- Git-based snapshots
- Restore points
- Atomic operations
- Change history tracking

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.engine import OuroborosConfig


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Snapshot:
    """A snapshot of file state."""
    id: str
    file_path: Path
    content: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "file_path": str(self.file_path),
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "content_hash": hash(self.content),
        }


@dataclass
class RestorePoint:
    """A restore point containing multiple snapshots."""
    id: str
    name: str
    snapshots: List[Snapshot] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    git_commit: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "git_commit": self.git_commit,
            "snapshot_count": len(self.snapshots),
            "files": [str(s.file_path) for s in self.snapshots],
        }


# =============================================================================
# ROLLBACK PROTECTOR
# =============================================================================

class RollbackProtector:
    """
    Manages rollback protection for code changes.

    Features:
    - Create snapshots before changes
    - Restore from snapshots
    - Git integration for version control
    - History tracking
    """

    def __init__(
        self,
        snapshot_dir: Path = OuroborosConfig.SNAPSHOT_PATH,
        max_snapshots: int = 100,
        use_git: bool = True,
    ):
        self.snapshot_dir = snapshot_dir
        self.max_snapshots = max_snapshots
        self.use_git = use_git

        self._snapshots: Dict[str, Snapshot] = {}
        self._restore_points: Dict[str, RestorePoint] = {}
        self._lock = asyncio.Lock()

        # Ensure directory exists
        snapshot_dir.mkdir(parents=True, exist_ok=True)

    async def create_snapshot(
        self,
        file_path: Path,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Snapshot:
        """
        Create a snapshot of a file.

        Args:
            file_path: Path to the file
            metadata: Optional metadata to store

        Returns:
            Snapshot object
        """
        async with self._lock:
            content = await asyncio.to_thread(file_path.read_text)

            snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
            snapshot = Snapshot(
                id=snapshot_id,
                file_path=file_path,
                content=content,
                timestamp=time.time(),
                metadata=metadata or {},
            )

            # Save to disk
            await self._save_snapshot(snapshot)

            self._snapshots[snapshot_id] = snapshot

            # Cleanup old snapshots
            await self._cleanup_old_snapshots()

            return snapshot

    async def create_restore_point(
        self,
        name: str,
        files: List[Path],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RestorePoint:
        """
        Create a restore point for multiple files.

        Args:
            name: Name for the restore point
            files: List of files to snapshot
            metadata: Optional metadata

        Returns:
            RestorePoint object
        """
        async with self._lock:
            restore_id = f"rp_{uuid.uuid4().hex[:12]}"
            snapshots = []

            for file_path in files:
                if file_path.exists():
                    content = await asyncio.to_thread(file_path.read_text)
                    snapshot = Snapshot(
                        id=f"{restore_id}_{file_path.name}",
                        file_path=file_path,
                        content=content,
                        timestamp=time.time(),
                    )
                    snapshots.append(snapshot)

            # Get git commit if using git
            git_commit = None
            if self.use_git:
                git_commit = await self._get_current_commit(files[0].parent if files else Path.cwd())

            restore_point = RestorePoint(
                id=restore_id,
                name=name,
                snapshots=snapshots,
                metadata=metadata or {},
                git_commit=git_commit,
            )

            # Save restore point
            await self._save_restore_point(restore_point)

            self._restore_points[restore_id] = restore_point

            return restore_point

    async def restore_snapshot(self, snapshot_id: str) -> bool:
        """
        Restore a file from a snapshot.

        Args:
            snapshot_id: ID of the snapshot to restore

        Returns:
            True if successful
        """
        async with self._lock:
            snapshot = self._snapshots.get(snapshot_id)

            if not snapshot:
                # Try to load from disk
                snapshot = await self._load_snapshot(snapshot_id)

            if not snapshot:
                return False

            await asyncio.to_thread(snapshot.file_path.write_text, snapshot.content)
            return True

    async def restore_point(self, restore_point_id: str) -> bool:
        """
        Restore all files from a restore point.

        Args:
            restore_point_id: ID of the restore point

        Returns:
            True if successful
        """
        async with self._lock:
            rp = self._restore_points.get(restore_point_id)

            if not rp:
                # Try to load from disk
                rp = await self._load_restore_point(restore_point_id)

            if not rp:
                return False

            for snapshot in rp.snapshots:
                await asyncio.to_thread(snapshot.file_path.write_text, snapshot.content)

            return True

    async def git_checkpoint(
        self,
        working_dir: Path,
        message: str = "Ouroboros checkpoint",
    ) -> Optional[str]:
        """
        Create a git checkpoint (stash or commit).

        Args:
            working_dir: Git repository directory
            message: Checkpoint message

        Returns:
            Checkpoint identifier or None
        """
        if not self.use_git:
            return None

        try:
            # Check for uncommitted changes
            result = await asyncio.create_subprocess_shell(
                "git status --porcelain",
                stdout=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            stdout, _ = await result.communicate()

            if not stdout.decode().strip():
                # No changes
                return await self._get_current_commit(working_dir)

            # Create stash
            result = await asyncio.create_subprocess_shell(
                f'git stash push -m "{message}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            await result.wait()

            if result.returncode == 0:
                return f"stash:{message}"

            return None

        except Exception as e:
            return None

    async def git_restore(
        self,
        working_dir: Path,
        checkpoint: str,
    ) -> bool:
        """
        Restore from a git checkpoint.

        Args:
            working_dir: Git repository directory
            checkpoint: Checkpoint identifier

        Returns:
            True if successful
        """
        if not self.use_git:
            return False

        try:
            if checkpoint.startswith("stash:"):
                # Pop stash
                result = await asyncio.create_subprocess_shell(
                    "git stash pop",
                    cwd=working_dir,
                )
                await result.wait()
                return result.returncode == 0
            else:
                # Checkout commit
                result = await asyncio.create_subprocess_shell(
                    f"git checkout {checkpoint}",
                    cwd=working_dir,
                )
                await result.wait()
                return result.returncode == 0

        except Exception:
            return False

    async def _get_current_commit(self, working_dir: Path) -> Optional[str]:
        """Get current git commit hash."""
        try:
            result = await asyncio.create_subprocess_shell(
                "git rev-parse HEAD",
                stdout=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            stdout, _ = await result.communicate()
            return stdout.decode().strip()[:12] if result.returncode == 0 else None
        except Exception:
            return None

    async def _save_snapshot(self, snapshot: Snapshot) -> None:
        """Save snapshot to disk."""
        snapshot_file = self.snapshot_dir / f"{snapshot.id}.json"
        content_file = self.snapshot_dir / f"{snapshot.id}.content"

        data = snapshot.to_dict()
        await asyncio.to_thread(snapshot_file.write_text, json.dumps(data, indent=2))
        await asyncio.to_thread(content_file.write_text, snapshot.content)

    async def _load_snapshot(self, snapshot_id: str) -> Optional[Snapshot]:
        """Load snapshot from disk."""
        snapshot_file = self.snapshot_dir / f"{snapshot_id}.json"
        content_file = self.snapshot_dir / f"{snapshot_id}.content"

        if not snapshot_file.exists() or not content_file.exists():
            return None

        try:
            data = json.loads(await asyncio.to_thread(snapshot_file.read_text))
            content = await asyncio.to_thread(content_file.read_text)

            return Snapshot(
                id=data["id"],
                file_path=Path(data["file_path"]),
                content=content,
                timestamp=data["timestamp"],
                metadata=data.get("metadata", {}),
            )
        except Exception:
            return None

    async def _save_restore_point(self, rp: RestorePoint) -> None:
        """Save restore point to disk."""
        rp_dir = self.snapshot_dir / rp.id
        rp_dir.mkdir(parents=True, exist_ok=True)

        # Save metadata
        meta_file = rp_dir / "metadata.json"
        await asyncio.to_thread(meta_file.write_text, json.dumps(rp.to_dict(), indent=2))

        # Save each snapshot
        for snapshot in rp.snapshots:
            content_file = rp_dir / f"{snapshot.file_path.name}.content"
            await asyncio.to_thread(content_file.write_text, snapshot.content)

    async def _load_restore_point(self, rp_id: str) -> Optional[RestorePoint]:
        """Load restore point from disk."""
        rp_dir = self.snapshot_dir / rp_id
        meta_file = rp_dir / "metadata.json"

        if not meta_file.exists():
            return None

        try:
            data = json.loads(await asyncio.to_thread(meta_file.read_text))

            # Load snapshots
            snapshots = []
            for file_path_str in data.get("files", []):
                file_path = Path(file_path_str)
                content_file = rp_dir / f"{file_path.name}.content"
                if content_file.exists():
                    content = await asyncio.to_thread(content_file.read_text)
                    snapshots.append(Snapshot(
                        id=f"{rp_id}_{file_path.name}",
                        file_path=file_path,
                        content=content,
                        timestamp=data["timestamp"],
                    ))

            return RestorePoint(
                id=data["id"],
                name=data["name"],
                snapshots=snapshots,
                timestamp=data["timestamp"],
                metadata=data.get("metadata", {}),
                git_commit=data.get("git_commit"),
            )
        except Exception:
            return None

    async def _cleanup_old_snapshots(self) -> None:
        """Remove old snapshots to stay under limit."""
        if len(self._snapshots) <= self.max_snapshots:
            return

        # Sort by timestamp and remove oldest
        sorted_snapshots = sorted(
            self._snapshots.values(),
            key=lambda s: s.timestamp,
        )

        to_remove = len(self._snapshots) - self.max_snapshots
        for snapshot in sorted_snapshots[:to_remove]:
            del self._snapshots[snapshot.id]

            # Remove from disk
            snapshot_file = self.snapshot_dir / f"{snapshot.id}.json"
            content_file = self.snapshot_dir / f"{snapshot.id}.content"

            for f in [snapshot_file, content_file]:
                if f.exists():
                    await asyncio.to_thread(f.unlink)

    def get_snapshot_history(self, file_path: Path, limit: int = 10) -> List[Snapshot]:
        """Get snapshot history for a file."""
        file_snapshots = [
            s for s in self._snapshots.values()
            if s.file_path == file_path
        ]
        sorted_snapshots = sorted(file_snapshots, key=lambda s: s.timestamp, reverse=True)
        return sorted_snapshots[:limit]

    def list_restore_points(self) -> List[Dict[str, Any]]:
        """List all restore points."""
        return [rp.to_dict() for rp in self._restore_points.values()]
