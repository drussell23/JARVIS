"""
WorkspaceCheckpoint — System-wide save/restore via git stash.

Gap 3: System-wide checkpoint before governance operations.
Uses git stash (argv, no shell) for O(1) snapshots.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_CHECKPOINTS = int(os.environ.get("JARVIS_MAX_CHECKPOINTS", "20"))


@dataclass
class Checkpoint:
    checkpoint_id: str
    op_id: str
    description: str
    stash_ref: str
    files_snapshot: List[str]
    created_at: float = field(default_factory=time.time)


class WorkspaceCheckpointManager:
    """Git stash-based workspace checkpointing. All subprocess calls are argv-based."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._checkpoints: List[Checkpoint] = []

    async def create_checkpoint(self, op_id: str, description: str = "") -> Optional[Checkpoint]:
        dirty = await self._get_dirty_files()
        if not dirty:
            return None

        msg = f"ouroboros-checkpoint:{op_id}:{description[:50]}"
        proc = await asyncio.create_subprocess_exec(
            "git", "stash", "push", "-m", msg, "--include-untracked",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        if proc.returncode != 0:
            return None

        # Re-apply so working tree stays intact
        proc2 = await asyncio.create_subprocess_exec(
            "git", "stash", "apply",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
        )
        await asyncio.wait_for(proc2.communicate(), timeout=30.0)

        cp = Checkpoint(
            checkpoint_id=f"cp-{op_id[:8]}-{int(time.time())}",
            op_id=op_id, description=description,
            stash_ref="stash@{0}", files_snapshot=dirty,
        )
        self._checkpoints.append(cp)
        if len(self._checkpoints) > _MAX_CHECKPOINTS:
            self._checkpoints = self._checkpoints[-_MAX_CHECKPOINTS:]

        logger.info("[Checkpoint] Created %s: %d files", cp.checkpoint_id, len(dirty))
        return cp

    async def restore_checkpoint(self, checkpoint_id: str) -> bool:
        cp = next((c for c in self._checkpoints if c.checkpoint_id == checkpoint_id), None)
        if not cp:
            return False

        idx = await self._find_stash_index(cp.op_id)
        if idx is None:
            return False

        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", "--", ".",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)

        proc2 = await asyncio.create_subprocess_exec(
            "git", "stash", "apply", f"stash@{{{idx}}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
        )
        _, stderr = await asyncio.wait_for(proc2.communicate(), timeout=30.0)
        if proc2.returncode == 0:
            logger.info("[Checkpoint] Restored %s", checkpoint_id)
            return True
        logger.warning("[Checkpoint] Restore failed: %s", stderr.decode()[:200])
        return False

    def list_checkpoints(self) -> List[Checkpoint]:
        return list(self._checkpoints)

    async def _get_dirty_files(self) -> List[str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        return [l[3:].strip() for l in stdout.decode().strip().split("\n") if l.strip()]

    async def _find_stash_index(self, op_id: str) -> Optional[int]:
        proc = await asyncio.create_subprocess_exec(
            "git", "stash", "list",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        for line in stdout.decode().strip().split("\n"):
            if op_id in line:
                match = re.search(r"stash@\{(\d+)\}", line)
                if match:
                    return int(match.group(1))
        return None
