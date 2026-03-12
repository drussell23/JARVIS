"""backend/core/ouroboros/governance/autonomy/snapshot_manager.py

Pre-operation snapshot management for L3 SafetyNet.

Adapted from the deprecated ``protector.py`` (quarantined 2026-03-11).
Provides in-memory file snapshots and named restore points so that L3
can advise L1 on rollback without touching the filesystem directly.

Key design decisions:
    - In-memory only -- no disk I/O (SafetyNet is advisory, never writes).
    - Content stored as strings (L1 provides content, L3 stores it).
    - SHA-256 hashes for integrity verification.
    - Bounded capacity with automatic pruning of oldest entries.
    - ``monotonic_ns`` timestamps per C+ convention.
    - No async needed -- pure in-memory operations.
    - Single-writer invariant: this module NEVER mutates op_context, ledger,
      filesystem, or trust tiers directly.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.SnapshotManager")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FileSnapshot:
    """A snapshot of a single file's content."""

    snapshot_id: str
    file_path: str
    content_hash: str  # SHA-256 hex digest
    content: str
    timestamp_ns: int = field(default_factory=time.monotonic_ns)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize *without* content (safe for logging/telemetry)."""
        return {
            "snapshot_id": self.snapshot_id,
            "file_path": self.file_path,
            "content_hash": self.content_hash,
            "timestamp_ns": self.timestamp_ns,
            "metadata": self.metadata,
        }


@dataclass
class RestorePoint:
    """A named collection of file snapshots for atomic rollback."""

    restore_id: str
    name: str
    snapshots: List[FileSnapshot] = field(default_factory=list)
    timestamp_ns: int = field(default_factory=time.monotonic_ns)
    metadata: Dict[str, Any] = field(default_factory=dict)
    git_ref: Optional[str] = None  # git commit hash at time of snapshot

    @property
    def file_count(self) -> int:
        """Number of files captured in this restore point."""
        return len(self.snapshots)

    def get_snapshot(self, file_path: str) -> Optional[FileSnapshot]:
        """Find the snapshot for a specific file path, or ``None``."""
        for snap in self.snapshots:
            if snap.file_path == file_path:
                return snap
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging/telemetry (excludes raw content)."""
        return {
            "restore_id": self.restore_id,
            "name": self.name,
            "file_count": self.file_count,
            "timestamp_ns": self.timestamp_ns,
            "metadata": self.metadata,
            "git_ref": self.git_ref,
            "files": [s.file_path for s in self.snapshots],
        }


# ---------------------------------------------------------------------------
# SnapshotManager
# ---------------------------------------------------------------------------


class SnapshotManager:
    """Manages pre-operation snapshots for L3 SafetyNet.

    Creates and stores snapshots of files before autonomous operations
    so they can be restored if the operation fails.

    In-memory only (no disk persistence) -- snapshots are transient
    within the current process lifecycle.
    """

    def __init__(
        self,
        max_restore_points: int = 50,
        max_snapshots_per_point: int = 20,
    ) -> None:
        self._restore_points: Dict[str, RestorePoint] = {}
        self._max_restore_points = max_restore_points
        self._max_snapshots_per_point = max_snapshots_per_point
        # Ordered list of restore_ids for FIFO pruning
        self._insertion_order: List[str] = []

    # ------------------------------------------------------------------
    # Standalone snapshot creation
    # ------------------------------------------------------------------

    def create_snapshot(
        self,
        file_path: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FileSnapshot:
        """Create a standalone file snapshot.

        Computes SHA-256 hash of *content* and generates a unique
        ``snapshot_id``.
        """
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
        snap = FileSnapshot(
            snapshot_id=snapshot_id,
            file_path=file_path,
            content_hash=content_hash,
            content=content,
            metadata=metadata or {},
        )
        logger.debug(
            "Created snapshot %s for %s (hash=%s)",
            snapshot_id,
            file_path,
            content_hash[:12],
        )
        return snap

    # ------------------------------------------------------------------
    # Restore point lifecycle
    # ------------------------------------------------------------------

    def create_restore_point(
        self,
        name: str,
        files: Dict[str, str],  # {file_path: content}
        git_ref: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RestorePoint:
        """Create a restore point containing snapshots of multiple files.

        Generates a unique ``restore_id``.  Prunes oldest restore points
        when at capacity.

        Raises:
            ValueError: If *files* exceeds ``max_snapshots_per_point``.
        """
        if len(files) > self._max_snapshots_per_point:
            raise ValueError(
                f"{len(files)} files exceeds max "
                f"{self._max_snapshots_per_point} snapshots per restore point"
            )

        restore_id = f"rp_{uuid.uuid4().hex[:12]}"
        snapshots: List[FileSnapshot] = []
        for file_path, content in files.items():
            snap = self.create_snapshot(file_path, content)
            snapshots.append(snap)

        rp = RestorePoint(
            restore_id=restore_id,
            name=name,
            snapshots=snapshots,
            git_ref=git_ref,
            metadata=metadata or {},
        )

        self._restore_points[restore_id] = rp
        self._insertion_order.append(restore_id)

        # Auto-prune if over capacity
        self._prune_oldest()

        logger.info(
            "Created restore point %s '%s' with %d files (git_ref=%s)",
            restore_id,
            name,
            rp.file_count,
            git_ref,
        )
        return rp

    def get_restore_point(self, restore_id: str) -> Optional[RestorePoint]:
        """Retrieve a restore point by ID, or ``None`` if not found."""
        return self._restore_points.get(restore_id)

    def list_restore_points(self) -> List[Dict[str, Any]]:
        """List all restore points (serialized via ``to_dict``, no content)."""
        return [rp.to_dict() for rp in self._restore_points.values()]

    def get_file_content(
        self, restore_id: str, file_path: str
    ) -> Optional[str]:
        """Retrieve the content of a specific file from a restore point.

        Returns ``None`` if the restore point or file is not found.
        """
        rp = self._restore_points.get(restore_id)
        if rp is None:
            return None
        snap = rp.get_snapshot(file_path)
        if snap is None:
            return None
        return snap.content

    # ------------------------------------------------------------------
    # Capacity management
    # ------------------------------------------------------------------

    def _prune_oldest(self) -> None:
        """Remove oldest restore points to stay under max capacity."""
        while len(self._restore_points) > self._max_restore_points:
            if not self._insertion_order:
                break  # pragma: no cover — defensive
            oldest_id = self._insertion_order.pop(0)
            removed = self._restore_points.pop(oldest_id, None)
            if removed is not None:
                logger.debug(
                    "Pruned restore point %s '%s' (capacity: %d/%d)",
                    oldest_id,
                    removed.name,
                    len(self._restore_points),
                    self._max_restore_points,
                )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Summary for telemetry / dashboards."""
        total_snapshots = sum(
            rp.file_count for rp in self._restore_points.values()
        )
        return {
            "total_restore_points": len(self._restore_points),
            "total_snapshots": total_snapshots,
            "max_restore_points": self._max_restore_points,
            "max_snapshots_per_point": self._max_snapshots_per_point,
            "restore_points": [
                rp.to_dict() for rp in self._restore_points.values()
            ],
        }
