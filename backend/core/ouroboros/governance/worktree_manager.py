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


# Slice 44 — campaign-debris worktree prefixes reaped at boot, in addition to
# the L3 isolation ``unit-`` prefix. ``ouroboros__auto__bt-*`` are auto-soak
# worktrees (full repo checkouts) that the original reaper (``unit-`` only)
# never swept, so they accumulated to 492k files / 13GB and starved the loop.
# Env-tunable (comma-separated) so the set stays dynamic with no hardcoding.
# NOTE the slash form ``ouroboros/auto/bt-`` is FIRST and load-bearing: the
# auto-soak worktree's git BRANCH is ``ouroboros/auto/bt-<session>`` (slashes),
# while its on-disk DIR is ``ouroboros__auto__bt-<session>`` (slashes→``__``).
# ``git worktree list --porcelain`` is repo-global, so matching the branch form
# reaps the debris via ``branch_matches`` regardless of which ``worktree_base``
# the boot WorktreeManager was constructed with (the prod base differs from the
# repo-root ``.worktrees`` where the debris actually lives). The ``__`` dir form
# + ``soak-`` cover the unregistered-on-disk-dir path under this manager's base.
_DEFAULT_REAP_EXTRA_PREFIXES = (
    "ouroboros/auto/bt-", "ouroboros__auto__bt-", "soak-",
)


def _resolve_reap_prefixes(primary: str) -> "tuple[str, ...]":
    """Return the deduped, order-preserving prefix array the boot reaper
    matches against: the caller's ``primary`` prefix first, then the
    campaign-debris extras (``JARVIS_WORKTREE_REAP_PREFIXES`` override, else
    the defaults). Empty / whitespace entries are dropped. NEVER raises."""
    raw = os.environ.get("JARVIS_WORKTREE_REAP_PREFIXES", "").strip()
    if raw:
        extras = tuple(p.strip() for p in raw.split(",") if p.strip())
    else:
        extras = _DEFAULT_REAP_EXTRA_PREFIXES
    out: list = []
    for p in (primary, *extras):
        if p and p not in out:
            out.append(p)
    return tuple(out)


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
        # Resolve to an absolute, symlink-free path so all git operations and
        # path comparisons use a canonical form regardless of caller CWD or
        # symlinked mount points (the "isomorphic" worktree-in-worktree case
        # where '.' in a Claude-Code worktree resolves to the wrong git root).
        self._repo_root = Path(os.path.realpath(repo_root))
        self._worktree_base: Path = (
            Path(os.path.realpath(worktree_base)) if worktree_base is not None
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

        # Isomorphic Worktree Hydration post-condition (2026-06-27):
        # Verify the checkout is populated for every sentinel directory that
        # exists in the real repo root. An empty worktree (only .jarvis/ present)
        # causes Advisor 0%-coverage blocks. We mirror the repo's own structure:
        # if the repo has 'backend/' it must exist in the worktree too; likewise
        # for 'tests/'. Synthetic repos used by unit tests (no backend/ or tests/)
        # bypass the check entirely — no directories to verify → no assertion.
        _sentinels = ("backend", "tests")
        _missing = [
            d for d in _sentinels
            if (self._repo_root / d).is_dir() and not (wt_path / d).is_dir()
        ]
        if _missing:
            # Attempt best-effort cleanup before raising so we don't leak a
            # partially-constructed worktree entry in git's registry.
            try:
                _cleanup_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", str(self._repo_root),
                    "worktree", "remove", "--force", str(wt_path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await _cleanup_proc.communicate()
            except Exception:  # noqa: BLE001 — best-effort only
                pass
            raise RuntimeError(
                f"git worktree add for branch '{branch_name}' succeeded "
                f"(rc=0) but the checkout is incomplete — "
                f"{_missing} present in repo but absent from {wt_path}. "
                f"The repo_root used was '{self._repo_root}'. "
                "This usually means the repo root was wrong (symlink / "
                "relative-path CWD mismatch). Check JARVIS_REPO_ROOT or "
                "the WorktreeManager constructor call site."
            )

        # P1 Slice 2 — Ledger Sovereignty marker. Stamps a typed
        # ownership record at <wt_path>/.jarvis/ledger_ownership.json
        # so downstream AutoCommitter can structurally verify the
        # commit target is a worktree this loop owns. §33.1 master-
        # FALSE default; off-master path is byte-identical. NEVER
        # raises — mark_owned itself returns None on I/O failure
        # and the downstream assertion surfaces the missing marker.
        self._stamp_ownership_marker(wt_path, branch_name)

        logger.info("WorktreeManager: created worktree at %s", wt_path)
        return wt_path

    def _stamp_ownership_marker(
        self, wt_path: Path, branch_name: str,
    ) -> None:
        """Stamp a Ledger Sovereignty marker at ``wt_path`` under
        the master flag. NEVER raises.

        The session_id comes from ``JARVIS_OUROBOROS_SESSION_ID``
        (set by ``BattleTestHarness`` at boot, mirroring the
        existing ``JARVIS_OUROBOROS_SESSION_DIR`` pattern). Absent
        env → empty session_id; the marker still stamps, the
        cross-session mismatch check just won't fire for this
        worktree.
        """
        try:
            from backend.core.ouroboros.governance.ledger_sovereignty import (  # noqa: E501
                master_enabled,
                mark_owned,
            )
        except Exception:  # noqa: BLE001 — defensive import
            return
        if not master_enabled():
            return
        session_id = os.environ.get(
            "JARVIS_OUROBOROS_SESSION_ID", ""
        )
        try:
            mark_owned(
                wt_path,
                session_id=session_id,
                branch_name=branch_name,
            )
        except Exception as err:  # noqa: BLE001 — defensive
            # mark_owned is itself NEVER-raise; this is paranoia.
            logger.debug(
                "WorktreeManager: mark_owned defensive catch: %r",
                err,
            )

    async def list_worktree_paths(self) -> List[str]:
        """Gap #3 Slice 2 — return git's current worktree path list.

        Reads ``git worktree list --porcelain`` and projects the
        ``worktree`` field of every entry into a flat list of
        absolute path strings. Used by the IDE observability GET
        endpoint to cross-reference scheduler unit_ids with on-disk
        worktrees (orphan detection + has_worktree marking).

        Returns an empty list on git failure, missing repo, or
        unreadable porcelain output. NEVER raises — projection is
        best-effort by design (worktree absence is a valid state).

        Re-uses the existing ``_run_git_capture`` + module-level
        ``_parse_worktree_porcelain`` helpers; does NOT shell out
        beyond a single read-only ``git worktree list`` call.
        """
        try:
            porcelain = await self._run_git_capture(
                ["worktree", "list", "--porcelain"],
            )
        except Exception:  # noqa: BLE001 — defensive
            return []
        if not porcelain:
            return []
        out: List[str] = []
        try:
            for entry in _parse_worktree_porcelain(porcelain):
                p = entry.get("worktree", "")
                if p:
                    out.append(p)
        except Exception:  # noqa: BLE001 — defensive
            return []
        return out

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
        # Slice 44 — match against a multi-prefix array (legacy "unit-" PLUS
        # campaign-debris prefixes ouroboros__auto__bt- / soak-, env-tunable).
        # These auto-soak worktrees (full repo checkouts) were never reaped
        # before — 62 of them accumulated to 492k files / 13GB, which Oracle's
        # _find_python_files indexer recursively walked (its EXCLUDE_PATTERNS
        # lacked .worktrees), holding the GIL and starving the asyncio loop
        # (v38/v39 SidecarProfiler: oracle scan_dir + 51s thread.join wedge).
        # (The file-watch guard already excluded .worktrees at the scheduling
        # layer — Oracle was the scanner.)
        prefixes = _resolve_reap_prefixes(branch_prefix)

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
            name_matches = any(wt_path.name.startswith(p) for p in prefixes)
            branch_matches = any(branch_short.startswith(p) for p in prefixes)
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
                if not any(child.name.startswith(p) for p in prefixes):
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

        for _p in prefixes:
            branches = await self._run_git_capture(
                ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{_p}*"]
            )
            for name in branches.splitlines():
                name = name.strip()
                if name.startswith(_p):
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
