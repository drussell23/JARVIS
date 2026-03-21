# backend/core/ouroboros/governance/worktree_manager.py
"""
WorktreeManager
===============
Async lifecycle manager for git worktrees used by SubagentScheduler to give
each subagent unit an isolated filesystem branch.

Design notes
------------
- All git operations use asyncio.create_subprocess_exec (never shell=True)
  so branch names cannot inject shell commands.
- create() derives a deterministic path from the branch name under
  worktree_base (default <repo_root>/.worktrees).
- cleanup() attempts git worktree remove --force first, then falls back
  to shutil.rmtree if git fails or the path is outside a git worktree list
  (e.g. was never registered with git, or the repo itself was already deleted).
- Both methods are safe to call concurrently for different branch names.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorktreeManager:
    """Manages git worktree creation and cleanup for subagent isolation.

    Parameters
    ----------
    repo_root:
        Absolute path to the git repository root.
    worktree_base:
        Directory under which worktrees are created.  Defaults to
        <repo_root>/.worktrees.  Created on first use if absent.
    """

    def __init__(
        self,
        repo_root: Path,
        worktree_base: Optional[Path] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._worktree_base: Path = (
            Path(worktree_base) if worktree_base is not None
            else self._repo_root / ".worktrees"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(self, branch_name: str) -> Path:
        """Create a new git worktree for branch_name.

        The worktree is placed at <worktree_base>/<safe_name> where slashes
        in branch names are replaced with __ to produce a safe directory name.

        Parameters
        ----------
        branch_name:
            Git branch to create inside the worktree.  The branch must not
            already exist in the repository.

        Returns
        -------
        Path
            Absolute path to the freshly created worktree directory.

        Raises
        ------
        RuntimeError
            If git worktree add exits with a non-zero status.
        """
        self._worktree_base.mkdir(parents=True, exist_ok=True)

        safe_name = branch_name.replace("/", "__").replace(" ", "_")
        wt_path = self._worktree_base / safe_name

        cmd = [
            "git",
            "-C", str(self._repo_root),
            "worktree", "add",
            "-b", branch_name,
            str(wt_path),
        ]
        logger.debug("WorktreeManager.create: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed (rc={proc.returncode}) "
                f"for branch '{branch_name}': {stderr.decode().strip()}"
            )

        logger.info("WorktreeManager: created worktree at %s", wt_path)
        return wt_path

    async def cleanup(self, worktree_path: Path) -> None:
        """Remove worktree_path from git's worktree list and delete it.

        Safe to call even if worktree_path does not exist or was never
        registered with git -- both cases are silently ignored.

        Parameters
        ----------
        worktree_path:
            Path returned by a previous call to create(), or any path
            that should be cleaned up.
        """
        worktree_path = Path(worktree_path)

        if not worktree_path.exists():
            logger.debug(
                "WorktreeManager.cleanup: path does not exist, nothing to do: %s",
                worktree_path,
            )
            return

        # Attempt git-level deregistration first so the repo's internal
        # worktree list stays consistent.
        git_ok = await self._git_worktree_remove(worktree_path)

        # If git could not remove it (e.g. worktree was never registered, or
        # git is unavailable), fall back to a plain directory removal so we
        # never leave stale directories behind.
        if not git_ok and worktree_path.exists():
            logger.warning(
                "WorktreeManager: git worktree remove failed, falling back to "
                "shutil.rmtree for %s",
                worktree_path,
            )
            try:
                shutil.rmtree(worktree_path)
            except OSError as exc:
                logger.error(
                    "WorktreeManager: shutil.rmtree(%s) failed: %s",
                    worktree_path,
                    exc,
                )

        logger.info("WorktreeManager: cleaned up worktree at %s", worktree_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _git_worktree_remove(self, worktree_path: Path) -> bool:
        """Run git worktree remove --force <path>.

        Returns True on success (rc == 0), False otherwise.
        The caller is responsible for falling back to shutil.rmtree.
        """
        cmd = [
            "git",
            "-C", str(self._repo_root),
            "worktree", "remove",
            "--force",
            str(worktree_path),
        ]
        logger.debug("WorktreeManager._git_worktree_remove: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.debug(
                    "WorktreeManager: git worktree remove exited %d: %s",
                    proc.returncode,
                    stderr.decode().strip(),
                )
                return False
            return True
        except OSError as exc:
            logger.debug("WorktreeManager: git worktree remove OSError: %s", exc)
            return False
