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
import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority 2 Slice 2 — worker identity for L3 fan-out determinism
# ---------------------------------------------------------------------------


def worker_id_for_path(worktree_path: Optional[str] = None) -> str:
    """Derive a stable worker identifier for use in per-worker
    ordinal namespacing (Priority 2 Slice 2 — Causality DAG /
    PRD §26.5.2).

    Combines ``os.getpid()`` (process unique at any moment) with a
    one-way SHA1 prefix of the worktree path (deterministic per
    worker but never leaks the path content). Format: ``"{pid}-{
    8-char-hash}"`` when a worktree path is supplied, or
    ``"{pid}-base"`` when running in the shared tree.

    Pure function — NEVER raises, no I/O at call time. Path hashing
    is an in-memory SHA1 computation. Safe to call from the
    ordinal-assignment hot path.

    Used by ``decision_runtime.DecisionRuntime`` to namespace its
    ordinal counter so concurrent multi-worker writes to a shared
    session ledger produce a stable replayable total order under
    the lexicographic ``(wall_ts, worker_id, sub_ordinal)`` compare.

    Authority invariants (AST-pinned by tests):
      * No imports of orchestrator / phase_runners /
        candidate_generator / iron_gate / change_engine / policy /
        semantic_guardian / providers / urgency_router.
      * Pure stdlib (``hashlib`` + ``os``).
      * NEVER raises out of any input.
      * Path content NEVER appears in the output (only its 8-char
        hash prefix); doesn't leak filesystem layout.
    """
    try:
        pid = os.getpid()
    except Exception:  # noqa: BLE001 — defensive
        pid = 0
    if not worktree_path:
        return f"{pid}-base"
    try:
        path_str = str(worktree_path).strip()
        if not path_str:
            return f"{pid}-base"
        path_hash = hashlib.sha1(
            path_str.encode("utf-8", errors="replace"),
        ).hexdigest()[:8]
        return f"{pid}-{path_hash}"
    except Exception:  # noqa: BLE001 — defensive
        return f"{pid}-base"


def _parse_worktree_porcelain(text: str) -> "list[dict[str, str]]":
    """Parse ``git worktree list --porcelain`` output into per-entry dicts.

    Porcelain format: one ``key value`` (or bare ``key``) per line, with
    entries separated by blank lines. Common keys: ``worktree``,
    ``HEAD``, ``branch``, ``bare``, ``detached``, ``locked``.
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        parts = line.split(None, 1)
        if len(parts) == 1:
            current[parts[0]] = ""
        else:
            current[parts[0]] = parts[1]
    if current:
        entries.append(current)
    return entries


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

    async def reap_orphans(self, branch_prefix: str = "unit-") -> int:
        """Remove leftover subagent worktrees from prior crashed runs.

        Active work units do not survive a process boundary — in-memory
        scheduler state is authoritative during normal operation. Any
        worktree whose branch begins with ``branch_prefix`` (default
        ``"unit-"``) or whose directory lives under ``worktree_base`` with
        a matching name is therefore an orphan at boot, left behind by
        SIGKILL/OOM/power-loss.

        Reap sources (in order):

        1. Entries from ``git worktree list --porcelain`` whose branch
           starts with ``refs/heads/<branch_prefix>`` or whose path lives
           under ``worktree_base`` with a matching directory name. Git
           deregistration first; ``shutil.rmtree`` fallback if git fails.
        2. On-disk directories under ``worktree_base`` matching the
           prefix that git never knew about (e.g. worktree metadata was
           lost but the checkout survived).
        3. Branches matching ``refs/heads/<branch_prefix>*`` left behind
           after their worktree was removed — deleted so a later session
           can re-create the same unit_id without "branch already exists".
        4. Final ``git worktree prune`` to clear stale administrative
           records git holds for worktrees whose directories vanished.

        Returns the count of distinct worktree paths reaped. Idempotent:
        a second call on the same clean repo returns 0.
        """
        reaped: Set[str] = set()

        porcelain = await self._run_git_capture(["worktree", "list", "--porcelain"])
        for entry in _parse_worktree_porcelain(porcelain):
            path_str = entry.get("worktree", "")
            if not path_str:
                continue
            wt_path = Path(path_str)
            branch_ref = entry.get("branch", "")
            branch_short = (
                branch_ref[len("refs/heads/"):]
                if branch_ref.startswith("refs/heads/")
                else ""
            )
            try:
                base_resolved = self._worktree_base.resolve()
                lives_under_base = wt_path.parent.resolve() == base_resolved
            except OSError:
                lives_under_base = False
            name_matches = wt_path.name.startswith(branch_prefix)
            branch_matches = branch_short.startswith(branch_prefix)
            if not (branch_matches or (lives_under_base and name_matches)):
                continue

            git_ok = await self._git_worktree_remove(wt_path)
            if git_ok:
                reaped.add(str(wt_path))
            elif wt_path.exists():
                try:
                    shutil.rmtree(wt_path)
                    reaped.add(str(wt_path))
                except OSError as exc:
                    logger.warning(
                        "WorktreeManager.reap_orphans: rmtree(%s) failed: %s",
                        wt_path, exc,
                    )
            if branch_short:
                await self._git_delete_branch(branch_short)

        if self._worktree_base.exists():
            for child in self._worktree_base.iterdir():
                if not child.is_dir():
                    continue
                if not child.name.startswith(branch_prefix):
                    continue
                if str(child) in reaped:
                    continue
                try:
                    shutil.rmtree(child)
                    reaped.add(str(child))
                    logger.info(
                        "WorktreeManager.reap_orphans: removed unregistered dir %s",
                        child,
                    )
                except OSError as exc:
                    logger.warning(
                        "WorktreeManager.reap_orphans: rmtree(%s) failed: %s",
                        child, exc,
                    )

        branches = await self._run_git_capture(
            ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{branch_prefix}*"]
        )
        for name in branches.splitlines():
            name = name.strip()
            if name.startswith(branch_prefix):
                await self._git_delete_branch(name)

        await self._run_git_capture(["worktree", "prune"])

        if reaped:
            logger.info(
                "WorktreeManager.reap_orphans: reaped %d orphan worktree(s)",
                len(reaped),
            )
        return len(reaped)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_git_capture(self, args: List[str]) -> str:
        """Run ``git -C <repo> <args>`` and return stdout. Empty on failure."""
        cmd = ["git", "-C", str(self._repo_root), *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                logger.debug(
                    "WorktreeManager._run_git_capture: %s exited %d: %s",
                    " ".join(args), proc.returncode, err.decode().strip(),
                )
                return ""
            return out.decode()
        except OSError as exc:
            logger.debug("WorktreeManager._run_git_capture OSError: %s", exc)
            return ""

    async def _git_delete_branch(self, branch_name: str) -> None:
        """Run ``git branch -D <name>``; log and swallow any failure."""
        cmd = [
            "git",
            "-C", str(self._repo_root),
            "branch", "-D", branch_name,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                logger.debug(
                    "WorktreeManager._git_delete_branch(%s) exited %d: %s",
                    branch_name, proc.returncode, err.decode().strip(),
                )
        except OSError as exc:
            logger.debug(
                "WorktreeManager._git_delete_branch(%s) OSError: %s",
                branch_name, exc,
            )

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
