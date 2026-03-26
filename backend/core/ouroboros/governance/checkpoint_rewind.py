"""
CheckpointRewind — Interactive checkpoint rewind with git-based snapshots.

Creates named git refs (via ``git stash create``) that capture file state
*without* modifying the working tree, then restores files on demand.

Checkpoints are persisted as JSON in ``~/.jarvis/ouroboros/checkpoints/``
so they survive process restarts.

Env vars:
  JARVIS_CHECKPOINT_DIR      — override persistence directory
  JARVIS_MAX_CHECKPOINTS     — max retained checkpoints (default 20)
  JARVIS_CHECKPOINT_TIMEOUT  — subprocess timeout in seconds (default 30)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CHECKPOINT_DIR = Path(
    os.environ.get(
        "JARVIS_CHECKPOINT_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "checkpoints"),
    )
)
_MAX_CHECKPOINTS = int(os.environ.get("JARVIS_MAX_CHECKPOINTS", "20"))
_SUBPROCESS_TIMEOUT = float(os.environ.get("JARVIS_CHECKPOINT_TIMEOUT", "30"))


# ---------------------------------------------------------------------------
# Checkpoint dataclass
# ---------------------------------------------------------------------------

@dataclass
class Checkpoint:
    """Immutable record of a workspace snapshot.

    Parameters
    ----------
    checkpoint_id:
        Unique identifier (UUID4 hex).
    op_id:
        The governance operation that triggered this checkpoint.
    description:
        Human-readable summary of what the operation did.
    created_at:
        Epoch timestamp.
    git_stash_ref:
        The SHA returned by ``git stash create`` — a dangling commit
        that captures the working-tree state without touching the stash list.
    files_modified:
        Paths (relative to project root) that were dirty at snapshot time.
    """
    checkpoint_id: str
    op_id: str
    description: str
    created_at: float = field(default_factory=time.time)
    git_stash_ref: Optional[str] = None
    files_modified: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Git stash-based checkpoint/rewind manager.

    All subprocess calls use argv form (no shell).  ``git stash create``
    produces a dangling commit object that captures the index + working tree
    without pushing onto the stash list, so the working directory is never
    disturbed.
    """

    def __init__(
        self,
        project_root: Path,
        max_checkpoints: int = _MAX_CHECKPOINTS,
    ) -> None:
        self._project_root = Path(project_root)
        self._max_checkpoints = max_checkpoints
        self._checkpoints: List[Checkpoint] = []
        self._persistence_dir = _CHECKPOINT_DIR
        self._load_persisted()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        op_id: str,
        description: str,
        files_modified: List[str],
    ) -> Checkpoint:
        """Create a checkpoint capturing the current working-tree state.

        Uses ``git stash create`` to get a commit ref without modifying
        the working tree or the stash list.
        """
        stash_ref = await self._git_stash_create()

        cp = Checkpoint(
            checkpoint_id=uuid.uuid4().hex,
            op_id=op_id,
            description=description,
            created_at=time.time(),
            git_stash_ref=stash_ref,
            files_modified=list(files_modified),
        )

        self._checkpoints.append(cp)
        self.prune(self._max_checkpoints)
        self._persist(cp)

        logger.info(
            "[CheckpointRewind] Created %s for op %s (%d files, ref=%s)",
            cp.checkpoint_id[:12],
            op_id[:12],
            len(files_modified),
            (stash_ref or "none")[:12],
        )
        return cp

    async def rewind_to(self, checkpoint_id: str) -> bool:
        """Restore files from the checkpoint identified by *checkpoint_id*.

        Returns True on success, False if the checkpoint is not found or
        the git restore fails.
        """
        cp = self._find(checkpoint_id)
        if cp is None:
            logger.warning("[CheckpointRewind] Checkpoint %s not found", checkpoint_id)
            return False

        if not cp.git_stash_ref:
            logger.warning(
                "[CheckpointRewind] Checkpoint %s has no git ref — nothing to restore",
                checkpoint_id,
            )
            return False

        if not cp.files_modified:
            logger.info("[CheckpointRewind] Checkpoint %s has no modified files", checkpoint_id)
            return True

        # Restore specific files from the stash ref
        cmd = ["git", "checkout", cp.git_stash_ref, "--"] + cp.files_modified
        returncode, _, stderr = await self._run_git(cmd)

        if returncode != 0:
            logger.error(
                "[CheckpointRewind] Rewind to %s failed: %s",
                checkpoint_id,
                stderr[:300],
            )
            return False

        logger.info(
            "[CheckpointRewind] Rewound to %s (%d files restored)",
            checkpoint_id[:12],
            len(cp.files_modified),
        )
        return True

    async def rewind_last(self, n: int = 1) -> List[Checkpoint]:
        """Rewind the last *n* checkpoints in reverse chronological order.

        Returns the list of checkpoints that were successfully rewound.
        """
        ordered = sorted(self._checkpoints, key=lambda c: c.created_at, reverse=True)
        targets = ordered[:n]
        rewound: List[Checkpoint] = []

        for cp in targets:
            ok = await self.rewind_to(cp.checkpoint_id)
            if ok:
                rewound.append(cp)
            else:
                logger.warning(
                    "[CheckpointRewind] Stopping rewind — failed on %s",
                    cp.checkpoint_id[:12],
                )
                break

        return rewound

    def list_checkpoints(self) -> List[Checkpoint]:
        """All checkpoints, newest first."""
        return sorted(self._checkpoints, key=lambda c: c.created_at, reverse=True)

    def prune(self, keep: int = _MAX_CHECKPOINTS) -> None:
        """Remove the oldest checkpoints beyond *keep*.

        Also removes the corresponding JSON files on disk.
        """
        if len(self._checkpoints) <= keep:
            return
        # Sort oldest-first, drop the head
        self._checkpoints.sort(key=lambda c: c.created_at)
        to_remove = self._checkpoints[: len(self._checkpoints) - keep]
        self._checkpoints = self._checkpoints[len(self._checkpoints) - keep :]

        for cp in to_remove:
            json_path = self._persistence_dir / f"{cp.checkpoint_id}.json"
            try:
                json_path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.debug("[CheckpointRewind] Pruned %d old checkpoints", len(to_remove))

    @staticmethod
    def format_for_display(checkpoints: List[Checkpoint]) -> str:
        """Render a human-readable summary of checkpoints."""
        if not checkpoints:
            return "(no checkpoints)"

        lines: List[str] = []
        for i, cp in enumerate(checkpoints, 1):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cp.created_at))
            ref_short = (cp.git_stash_ref or "none")[:10]
            n_files = len(cp.files_modified)
            lines.append(
                f"  {i}. [{ts}] {cp.checkpoint_id[:12]}  "
                f"op={cp.op_id[:12]}  ref={ref_short}  "
                f"files={n_files}  {cp.description}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    async def _git_stash_create(self) -> Optional[str]:
        """Run ``git stash create`` and return the commit SHA, or None."""
        returncode, stdout, stderr = await self._run_git(["git", "stash", "create"])
        ref = stdout.strip()
        if returncode != 0:
            logger.warning("[CheckpointRewind] git stash create failed: %s", stderr[:200])
            return None
        if not ref:
            # Empty ref means nothing to stash (clean tree) — not an error
            logger.debug("[CheckpointRewind] git stash create returned empty ref (clean tree)")
            return None
        return ref

    async def _run_git(self, cmd: List[str]) -> tuple:
        """Run a git command via argv, returning (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(), timeout=_SUBPROCESS_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("[CheckpointRewind] git command timed out: %s", " ".join(cmd))
            return (1, "", "timeout")

        return (
            proc.returncode or 0,
            raw_out.decode(errors="replace"),
            raw_err.decode(errors="replace"),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, cp: Checkpoint) -> None:
        """Write a single checkpoint to disk as JSON."""
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / f"{cp.checkpoint_id}.json"
            path.write_text(json.dumps(asdict(cp), indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("[CheckpointRewind] Failed to persist checkpoint: %s", exc)

    def _load_persisted(self) -> None:
        """Load all checkpoints from the persistence directory."""
        if not self._persistence_dir.is_dir():
            return
        loaded = 0
        for path in sorted(self._persistence_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cp = Checkpoint(
                    checkpoint_id=data["checkpoint_id"],
                    op_id=data["op_id"],
                    description=data["description"],
                    created_at=data.get("created_at", 0.0),
                    git_stash_ref=data.get("git_stash_ref"),
                    files_modified=data.get("files_modified", []),
                )
                self._checkpoints.append(cp)
                loaded += 1
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                logger.debug("[CheckpointRewind] Skipping corrupt checkpoint %s: %s", path.name, exc)
        if loaded:
            logger.info("[CheckpointRewind] Loaded %d persisted checkpoints", loaded)
            self.prune(self._max_checkpoints)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def _find(self, checkpoint_id: str) -> Optional[Checkpoint]:
        """Find a checkpoint by full or prefix ID."""
        for cp in self._checkpoints:
            if cp.checkpoint_id == checkpoint_id:
                return cp
        # Allow prefix match (first 12 chars is common in display)
        for cp in self._checkpoints:
            if cp.checkpoint_id.startswith(checkpoint_id):
                return cp
        return None
