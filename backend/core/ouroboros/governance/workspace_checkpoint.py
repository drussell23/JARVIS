"""
WorkspaceCheckpoint — System-wide save/restore via ``git stash create``.

Gap 3: system-wide checkpoint before governance operations.

Uses ``git stash create`` (argv, no shell) for O(1) non-destructive
snapshots. This is strictly cheaper than the old ``stash push``+
``stash apply`` dance because:

1. ``stash create`` writes a tree/commit object and prints its SHA
   without touching the working tree, the index, or the stash list.
2. There is no ``apply`` step, so the working tree cannot race with a
   concurrent patch writer while we hold a snapshot.
3. ``git stash apply <sha>`` takes the raw SHA on restore, so we never
   need to ``store`` the ref in the stash list during the hot path.

Battle-test origin (bt-2026-04-13-031119): the old implementation timed
out on a 30s ``wait_for`` during APPLY (op-019d84d7) and cascaded into
session ``idle_timeout``. Root cause was the combined ``push``+``apply``
cost on a moderately dirty tree. This rewrite makes the hot path
O(stash-create) with an env-driven ceiling that fails *gracefully* —
pre-APPLY checkpointing is an auditability feature, not a correctness
gate. A timeout returns ``None`` and the APPLY proceeds unchecked.

Env:
    JARVIS_CHECKPOINT_TIMEOUT_S: hard ceiling for the create subprocess
        (default 8.0). Falls back to ``None`` on timeout rather than
        raising, so APPLY cannot be starved by a slow git.
    JARVIS_MAX_CHECKPOINTS: ring-buffer size (default 20).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0.0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


_MAX_CHECKPOINTS = _env_int("JARVIS_MAX_CHECKPOINTS", 20)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class Checkpoint:
    checkpoint_id: str
    op_id: str
    description: str
    stash_ref: str  # Raw tree SHA from ``git stash create``.
    files_snapshot: List[str]
    created_at: float = field(default_factory=time.time)


class WorkspaceCheckpointManager:
    """Git stash-based workspace checkpointing. All subprocess calls are argv-based."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._checkpoints: List[Checkpoint] = []

    async def create_checkpoint(
        self, op_id: str, description: str = ""
    ) -> Optional[Checkpoint]:
        """Create a non-destructive snapshot of the working tree.

        Returns ``None`` when:
            - The subprocess times out (logged at WARNING; APPLY proceeds).
            - The working tree is clean (nothing to snapshot).
            - Git produces a non-zero exit (permission, corrupt index).

        Never raises — APPLY is not gated on this function.
        """
        timeout = _env_float("JARVIS_CHECKPOINT_TIMEOUT_S", 8.0)
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "stash", "create", "-u",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Checkpoint] git stash create timed out after %.1fs — skipping",
                timeout,
            )
            if proc is not None:
                with suppress(ProcessLookupError, Exception):
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
            return None
        except asyncio.CancelledError:
            if proc is not None:
                with suppress(ProcessLookupError, Exception):
                    proc.kill()
            raise
        except Exception as exc:
            logger.warning("[Checkpoint] git stash create failed: %s", exc)
            return None

        if proc.returncode != 0:
            return None

        sha = stdout.decode().strip()
        if not sha or not _SHA_RE.fullmatch(sha):
            return None

        cp = Checkpoint(
            checkpoint_id=f"cp-{op_id[:8]}-{int(time.time())}",
            op_id=op_id,
            description=description,
            stash_ref=sha,
            files_snapshot=[],
        )
        self._checkpoints.append(cp)
        if len(self._checkpoints) > _MAX_CHECKPOINTS:
            self._checkpoints = self._checkpoints[-_MAX_CHECKPOINTS:]

        logger.info(
            "[Checkpoint] Created %s (sha=%s)",
            cp.checkpoint_id,
            sha[:12],
        )
        return cp

    async def restore_checkpoint(self, checkpoint_id: str) -> bool:
        """Restore a previously-created snapshot via ``git stash apply <sha>``."""
        cp = next(
            (c for c in self._checkpoints if c.checkpoint_id == checkpoint_id),
            None,
        )
        if not cp or not cp.stash_ref:
            return False

        timeout = _env_float("JARVIS_CHECKPOINT_TIMEOUT_S", 8.0)
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "stash", "apply", cp.stash_ref,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Checkpoint] restore of %s timed out after %.1fs",
                checkpoint_id, timeout,
            )
            if proc is not None:
                with suppress(ProcessLookupError, Exception):
                    proc.kill()
            return False
        except asyncio.CancelledError:
            if proc is not None:
                with suppress(ProcessLookupError, Exception):
                    proc.kill()
            raise
        except Exception as exc:
            logger.warning("[Checkpoint] restore failed: %s", exc)
            return False

        if proc.returncode == 0:
            logger.info("[Checkpoint] Restored %s", checkpoint_id)
            return True
        logger.warning(
            "[Checkpoint] Restore %s failed: %s",
            checkpoint_id,
            stderr.decode(errors="replace")[:200],
        )
        return False

    def list_checkpoints(self) -> List[Checkpoint]:
        return list(self._checkpoints)
