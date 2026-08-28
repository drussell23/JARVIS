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
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slice 11 — workspace-commit promotion primitives (pure git layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionResult:
    """Successful promotion: which commits landed, how, and where.

    ``promoted_shas`` are the SOURCE (workspace-branch) commits, full-id
    normalized. ``landed_shas`` are the commits that actually appeared on
    the target branch — identical to the source for ff, NEW objects for
    cherry-pick (review P4: telemetry keyed on workspace shas pointed at
    commits that do not exist on the operator branch).
    """

    promoted_shas: Tuple[str, ...]
    mode: str  # 'ff' | 'cherry-pick' | 'none'
    target_root: str
    landed_shas: Tuple[str, ...] = ()


class PromotionError(RuntimeError):
    """Typed fail-closed promotion failure (Slice 11 mandate 2).

    ``state`` is one of: ``target_dirty``, ``conflict_aborted``,
    ``branch_missing``, ``commit_budget_exceeded``, ``git_failure``.
    On ANY failure the workspace branch is untouched — it remains the
    quarantined, reviewable artifact (Sovereign Execution Boundary).
    """

    def __init__(self, state: str, detail: str = "") -> None:
        super().__init__(
            "promotion %s: %s" % (state, detail) if detail
            else "promotion %s" % state
        )
        self.state = state
        self.detail = detail


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


#: The in-repo worktree root. Kept as a constant so the several modules that
#: currently spell `.worktrees` as a literal have one name to converge on.
_IN_REPO_WORKTREE_DIRNAME = ".worktrees"


def worktree_sweep_roots(repo_root: object, *extra: object) -> "Tuple[Path, ...]":
    """Every directory this system materializes worktrees in.

    There is more than one, on purpose, and that is the point. The L3 subagent
    base defaults to ``$HOME/.jarvis/ouroboros/worktrees`` — deliberately off
    the repo, because on this host the repo lives on a 9p ``/mnt/c`` mount
    where checkouts are pathologically slow. The session workspace base is
    ``<repo_root>/.worktrees``, because ``resolve_loop_project_root`` builds
    its ``WorktreeManager`` with no explicit base and takes the default.

    Both placements are correct. What was NOT correct is that the boot sweep
    knew about only one of them: ``reap_orphans`` ran on the L3 manager, so it
    iterated the home base and never saw in-repo debris. Measured on
    bt-2026-08-28-083617 — one registered orphan reaped (loop 1 finds those via
    ``git worktree list``, which reports absolute paths regardless of base)
    while three marker-only husks under ``<repo>/.worktrees`` survived
    untouched, as they had for days. That is the unfinished half of Slice 44,
    whose whole purpose was reclaiming exactly this debris.

    So the fix is NOT to force one base — that would either drag L3 checkouts
    onto the slow mount or move session workspaces out of the repo, breaking a
    deliberate decision in each case. The fix is to stop having a sweep that
    knows about a subset of the places its own system writes to.

    Sources, deduped: the caller's own base(s), the in-repo default, and
    ``JARVIS_WORKTREE_BASE`` (which ``posture_observer.worktree_orphan_count``
    already reads as authoritative — a third module with a fourth opinion,
    which is the smell this helper exists to remove).

    Deduped by ``os.path.realpath`` so ``/mnt/c/...`` versus a symlinked or
    trailing-slash spelling of the same directory is swept once, not twice.
    Only extant directories are returned. NEVER raises.
    """
    seen: Set[str] = set()
    roots: List[Path] = []

    def _add(candidate: object) -> None:
        if candidate in (None, ""):
            return
        try:
            resolved = os.path.realpath(str(candidate))
        except (OSError, TypeError, ValueError):
            return
        if resolved in seen:
            return
        seen.add(resolved)
        path = Path(resolved)
        try:
            if path.is_dir():
                roots.append(path)
        except OSError:
            pass

    for candidate in extra:
        _add(candidate)
    try:
        _add(Path(str(repo_root)) / _IN_REPO_WORKTREE_DIRNAME)
    except (TypeError, ValueError):
        pass
    _add(os.environ.get("JARVIS_WORKTREE_BASE"))
    return tuple(roots)


# ---------------------------------------------------------------------------
# Workspace liveness lock
# ---------------------------------------------------------------------------
#
# Answers ONE question for the reaper: "is a live process using this tree?"
#
# Placed under `.jarvis/` rather than at the worktree root because `.jarvis/`
# is already gitignored — a lock file at the root would show as untracked in
# every `git status` run inside the worktree, and a workspace that reports
# itself dirty is a workspace whose APPLY/rollback gates start lying.
#
# Why not reuse the Ledger-Sovereignty ownership marker, which already carries
# creator_pid and created_at: `ledger_sovereignty.master_enabled()` defaults to
# FALSE (§33.1), so that marker is absent in a default configuration. Whether
# one session may delete another's live workspace cannot be contingent on an
# unrelated feature flag. Both are consulted when reaping (see `_live_owner`);
# only this one is guaranteed to exist.
_WORKSPACE_LOCK_RELATIVE = (".jarvis", ".workspace_lock")
_WORKSPACE_LOCK_SCHEMA = "workspace_lock.1"


def workspace_lock_path(wt_path: "Path") -> "Path":
    """Absolute path of the liveness lock for *wt_path*."""
    return Path(wt_path, *_WORKSPACE_LOCK_RELATIVE)


def _process_start_time(pid: int) -> Optional[float]:
    """Process start time, or None when it cannot be established.

    This is what makes the PID check trustworthy. A bare `os.kill(pid, 0)`
    cannot distinguish "my session is alive" from "my session died and the
    OS handed that number to something unrelated" — and on a long-lived host
    reaping on a reused PID would skip real debris forever, while reaping on
    a stale one destroys live work. Comparing start times settles it.
    """
    try:
        import psutil  # noqa: PLC0415 — optional; absent → no reuse check
        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 — psutil missing, or process gone
        return None


def _write_workspace_lock(wt_path: "Path", branch_name: str) -> bool:
    """Atomically stamp the liveness lock. NEVER raises.

    Atomic because the reaper may read it at any moment, including while it
    is being written: a torn read must be impossible, so the payload is
    written to a temp sibling, fsynced, then `os.replace`d into place —
    rename within a directory is atomic on POSIX and on NTFS.

    Returns True on success. A False here is not fatal: a missing lock means
    "unprovable", and the reaper treats unprovable as reapable, which is the
    pre-existing behaviour.
    """
    lock = workspace_lock_path(wt_path)
    pid = os.getpid()
    payload = {
        "schema_version": _WORKSPACE_LOCK_SCHEMA,
        "pid": pid,
        # Wall-clock, for humans reading the file and for age heuristics.
        "created_at": time.time(),
        # The PID-reuse discriminator. None when psutil is unavailable —
        # the reader degrades to a plain liveness probe rather than failing.
        "proc_start": _process_start_time(pid),
        "session_id": os.environ.get("JARVIS_OUROBOROS_SESSION_ID", ""),
        "branch_name": branch_name,
    }
    tmp = lock.with_name(lock.name + f".{pid}.tmp")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, lock)
        return True
    except Exception as exc:  # noqa: BLE001 — a lock is best-effort
        logger.debug(
            "WorktreeManager: workspace lock write failed for %s: %r",
            wt_path, exc,
        )
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def read_workspace_lock(wt_path: "Path") -> Optional[dict]:
    """Read the liveness lock, or None when absent/unreadable. NEVER raises."""
    try:
        raw = workspace_lock_path(wt_path).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — absent / torn / malformed → unprovable
        return None


def _pid_is_live(pid: object, proc_start: object = None) -> bool:
    """True ONLY on positive proof that *pid* is a live process.

    ``proc_start`` (when both recorded and observable) must match, so a
    recycled PID reads as dead rather than as a live owner.

    Every uncertainty resolves to False — "not provably alive". That polarity
    is deliberate and is the OPPOSITE of the one
    :meth:`reap_dangling_auto_branches` uses. There, an auto branch may hold
    real unpushed work, so the default is "don't touch unless proven dead".
    Here the caller is a debris sweeper whose whole purpose is reclaiming
    disk (Slice 44: 62 checkouts, 492k files, 13GB); defaulting to "don't
    touch" would silently restore that problem. So: reap unless proven alive.
    """
    try:
        pid_i = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    # No short-circuit for our OWN pid. It is trivially live, but "the lock
    # names my pid" and "the lock was written by me" are different claims —
    # a dead session whose number the OS later handed to this process would
    # satisfy the first and not the second. The start-time comparison below
    # is what separates them, so our own pid goes through it like any other.
    try:
        os.kill(pid_i, 0)
    except ProcessLookupError:
        return False           # proven gone
    except PermissionError:
        pass                   # exists, owned by another user → alive
    except Exception:  # noqa: BLE001 — OverflowError on an out-of-range pid,
        # OSError on an unprobeable one. Anything that is not a clean "yes"
        # is "not provably alive", which keeps debris reapable.
        return False
    if proc_start is None:
        return True            # alive; reuse undetectable, accept
    observed = _process_start_time(pid_i)
    if observed is None:
        return True            # alive; cannot compare → accept
    try:
        # 1s tolerance: create_time() resolution differs across platforms
        # and a clock adjustment must not read as a reused PID.
        return abs(float(observed) - float(proc_start)) <= 1.0
    except (TypeError, ValueError):
        return True


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

        # Liveness lock — stamped UNCONDITIONALLY, unlike the sovereignty
        # marker above. That marker carries the same facts (creator_pid,
        # created_at) and would have been the DRY answer, except
        # `master_enabled()` defaults to FALSE (§33.1), so in a default
        # configuration it is never written. Whether one session may destroy
        # another's workspace must not depend on an unrelated feature flag
        # being switched on, so this one has no master.
        _write_workspace_lock(wt_path, branch_name)

        logger.info("WorktreeManager: created worktree at %s", wt_path)
        return wt_path

    async def create_or_reclaim(self, branch_name: str) -> Path:
        """:meth:`create`, self-healing the SELF-DEBRIS collision class.

        Slice 2 workspace-arming integrity (2026-07-18, bt-2026-07-18-200502):
        an earlier arming stage's worktree can be reaped mid-boot while its
        BRANCH survives — the later ``create`` for the same session then dies
        ``fatal: a branch named '<x>' already exists`` and the session runs
        armed-but-unusable, killing every op at the APPLY boundary.

        Because the branch name is session-nonced (``workspace_branch``:
        session_id + collision-proof nonce), a colliding branch of the SAME
        name is provably this session's own debris — no other session can mint
        it. Reclaim: prune stale registrations, delete the debris branch,
        remove a husk directory if present, then retry ``create`` exactly once.

        CONTRACT: callers MUST pass a branch this session exclusively owns
        (the nonced ``workspace_branch``). Never call with a shared or
        human-owned branch name — reclaim deletes it. Raises like ``create``
        when the retry also fails; any non-collision failure propagates
        unchanged (no blanket retry)."""
        try:
            return await self.create(branch_name)
        except RuntimeError as first_err:
            if "already exists" not in str(first_err):
                raise                       # not the collision class — no retry
            logger.warning(
                "WorktreeManager.create_or_reclaim: self-debris collision for "
                "%r — pruning registrations, deleting debris branch, retrying "
                "once (%s)", branch_name, first_err,
            )
            # 1) Clear stale administrative records (a registration whose dir
            #    vanished blocks both branch-delete and re-add).
            await self._run_git_capture(["worktree", "prune"])
            # 2) Delete the debris branch (session-nonced — provably ours).
            await self._run_git_capture(["branch", "-D", branch_name])
            # 3) Remove a husk directory (e.g. marker-only leftovers) so
            #    `git worktree add` doesn't refuse a non-empty target.
            safe_name = branch_name.replace("/", "__").replace(" ", "_")
            husk = self._worktree_base / safe_name
            try:
                if husk.is_dir() and not (husk / ".git").exists():
                    shutil.rmtree(husk, ignore_errors=True)
            except Exception:  # noqa: BLE001 — best-effort
                pass
            return await self.create(branch_name)

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

    async def reap_orphans(
        self,
        branch_prefix: str = "unit-",
        *,
        protect_paths: Optional[Sequence[object]] = None,
    ) -> int:
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

        # Every root this system writes worktrees into, not just this
        # manager's own. See :func:`worktree_sweep_roots` for why there is
        # legitimately more than one and why forcing a single base would be
        # the wrong repair.
        sweep_roots = worktree_sweep_roots(self._repo_root, self._worktree_base)
        logger.debug(
            "WorktreeManager.reap_orphans: sweeping %d root(s): %s",
            len(sweep_roots), ", ".join(str(r) for r in sweep_roots) or "(none)",
        )

        # ------------------------------------------------------------------
        # Self-protection: never reap the workspace THIS process is using.
        #
        # Slice 44 added the ``ouroboros__auto__bt-`` prefix here to clear real
        # debris (62 stale checkouts, 492k files, 13GB). The current session's
        # own workspace carries that same prefix, and nothing distinguished
        # "previous session's corpse" from "the tree I am standing in".
        #
        # Measured, bt-2026-08-28-065825:
        #   23:59:37  [FileIsolation] routed project_root -> ...-065825-174b22
        #   23:59:41  _git_worktree_remove: git -C ...-065825-174b22 remove
        #   23:59:58  reap_orphans: reaped 1 orphan worktree(s) at boot
        #   00:08:25  every op reaching APPLY: "armed but unusable (no .git)"
        # Four seconds after arming a VALID worktree the boot reaper deleted
        # it, leaving the `.jarvis` marker — the husk. Because
        # ``effective_execution_root`` fails closed at the APPLY boundary, the
        # ops it destroyed were the ones that had travelled furthest.
        #
        # This is why validating at ARMING time cannot fix it: the workspace
        # was valid when armed and was destroyed afterwards. The reaper is the
        # root cause; arming-time validation only makes an unrelated race safe.
        #
        # Derived from state rather than passed in, so no call site can forget
        # it — ``reap_orphans()`` is invoked at boot with no arguments today.
        # ``reap_dangling_auto_branches`` already carries the same idea as an
        # explicit ``current_branch``; this is that concept applied where it
        # was missing.
        protected_paths: Set[str] = set()
        protected_branches: Set[str] = set()
        for _p in (protect_paths or ()):
            try:
                protected_paths.add(str(Path(_p).resolve()))
            except (OSError, TypeError, ValueError):
                continue
        _live_ws = (os.environ.get("JARVIS_AUTO_COMMIT_WORKSPACE") or "").strip()
        if _live_ws:
            try:
                protected_paths.add(str(Path(_live_ws).resolve()))
            except OSError:
                protected_paths.add(_live_ws)
        _live_session = (os.environ.get("JARVIS_OUROBOROS_SESSION_ID") or "").strip()
        if _live_session:
            try:
                from backend.core.ouroboros.governance.autonomous_workspace import (
                    workspace_branch,  # noqa: PLC0415 — lazy: avoids an import cycle
                )
                protected_branches.add(workspace_branch(_live_session))
            except Exception:  # noqa: BLE001 — protection is best-effort, never fatal
                pass

        def _locked_by_live_process(path: Optional[Path]) -> Optional[str]:
            """Reason string when a LIVE process holds this tree, else None.

            Two independent sources, because neither alone is sufficient:

              * the workspace lock (`.jarvis/.workspace_lock`) — stamped
                unconditionally at materialization, so it is the one that
                exists in a default configuration;
              * the Ledger-Sovereignty ownership marker — richer, but absent
                unless `master_enabled()` (default FALSE).

            This is what makes cross-process destruction structurally
            impossible: the env-derived protection above only knows about the
            workspace THIS process armed, so a second worker's tree was still
            fair game. A lock on disk is visible to every reaper.
            """
            if path is None:
                return None
            lock = read_workspace_lock(path)
            if lock and _pid_is_live(lock.get("pid"), lock.get("proc_start")):
                return (
                    f"workspace lock held by live pid={lock.get('pid')} "
                    f"session={lock.get('session_id') or '?'}"
                )
            try:
                from backend.core.ouroboros.governance.ledger_sovereignty import (
                    read_ownership,  # noqa: PLC0415 — optional, master-gated
                )
                record = read_ownership(path)
            except Exception:  # noqa: BLE001 — absent marker → unprovable
                record = None
            if record is not None and _pid_is_live(
                getattr(record, "creator_pid", 0), None,
            ):
                return (
                    f"sovereignty marker held by live pid="
                    f"{getattr(record, 'creator_pid', '?')}"
                )
            return None

        def _is_protected(path: Optional[Path], branch: str = "") -> bool:
            """True when this path/branch belongs to the LIVE session."""
            if branch and branch in protected_branches:
                return True
            if path is None:
                return False
            try:
                if str(path.resolve()) in protected_paths:
                    return True
            except OSError:
                if str(path) in protected_paths:
                    return True
            held = _locked_by_live_process(path)
            if held:
                # Remember it so the branch sweep (loop 3) cannot delete the
                # ref out from under a tree we just refused to remove.
                lock = read_workspace_lock(path)
                _b = (lock or {}).get("branch_name") or branch
                if _b:
                    protected_branches.add(str(_b))
                logger.info(
                    "WorktreeManager.reap_orphans: SKIPPING %s — %s",
                    path, held,
                )
                return True
            return False

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
                _parent = wt_path.parent.resolve()
                lives_under_base = any(
                    _parent == _root for _root in sweep_roots
                )
            except OSError:
                lives_under_base = False
            name_matches = any(wt_path.name.startswith(p) for p in prefixes)
            branch_matches = any(branch_short.startswith(p) for p in prefixes)
            if not (branch_matches or (lives_under_base and name_matches)):
                continue
            if _is_protected(wt_path, branch_short):
                logger.info(
                    "WorktreeManager.reap_orphans: SKIPPING %s (branch=%s) — "
                    "it is the live session's own workspace, not orphan debris",
                    wt_path, branch_short or "?",
                )
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

        for _root in sweep_roots:
            try:
                _children = list(_root.iterdir())
            except OSError as exc:
                logger.warning(
                    "WorktreeManager.reap_orphans: cannot list %s: %s",
                    _root, exc,
                )
                continue
            for child in _children:
                if not child.is_dir():
                    continue
                if not any(child.name.startswith(p) for p in prefixes):
                    continue
                if str(child) in reaped:
                    continue
                if _is_protected(child):
                    # The husk case specifically: git has already lost the
                    # registration, so loop 1 cannot see it, and an rmtree here
                    # would delete the live session's `.jarvis` marker too.
                    logger.info(
                        "WorktreeManager.reap_orphans: SKIPPING unregistered "
                        "dir %s — live session workspace", child,
                    )
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
                if not name.startswith(_p):
                    continue
                if name in protected_branches:
                    # Deleting the live session's branch out from under its own
                    # worktree is the "branch already exists" class that
                    # `reap_dangling_auto_branches(current_branch=...)` exists
                    # to avoid — the same exclusion, applied here.
                    logger.info(
                        "WorktreeManager.reap_orphans: SKIPPING branch %s — "
                        "live session", name,
                    )
                    continue
                await self._git_delete_branch(name)

        await self._run_git_capture(["worktree", "prune"])

        if reaped:
            logger.info(
                "WorktreeManager.reap_orphans: reaped %d orphan worktree(s)",
                len(reaped),
            )
        return len(reaped)

    async def reap_dangling_auto_branches(
        self,
        *,
        current_branch: Optional[str] = None,
        branch_prefix: str = "ouroboros/auto/",
    ) -> int:
        """ov cockpit silence Slice 2 Task 5 (F4) — sweep dangling
        ``ouroboros/auto/<session>-<nonce>`` branches + their
        registered worktrees left behind by a dead session, BEFORE the
        current session's own ``WorktreeManager.create(branch_name)``
        call. The observed failure was ``[ledger_sovereignty]
        auto-commit worktree create failed: fatal: a branch named
        '...' already exists`` at boot — after which AutoCommitter
        refuses commits for the rest of the session (the create-path
        fail-open catch in ``_boot_ledger_sovereignty_workspace``
        never retries).

        Unlike :meth:`reap_orphans` (``unit-*`` — safe to blind-sweep,
        since L3 work units never survive a process boundary by
        design), an ``ouroboros/auto/*`` branch IS the Ledger-
        Sovereignty commit workspace: ``AutoCommitter``'s default
        posture (``JARVIS_AUTO_PUSH_BRANCH=""``) never pushes it
        anywhere, and nothing in this codebase merges or cherry-picks
        it back into the operator's checkout — autonomous commits stay
        quarantined by design (Sovereign Execution Boundary). A dead
        session's branch CAN therefore hold real unpushed, unreviewed
        work. This method reaps in two independently-gated steps:

          1. **Worktree directory** — removed once the creating
             session is PROVEN dead via the ``ledger_sovereignty``
             ownership marker's ``creator_pid``, probed with
             ``os.kill(pid, 0)`` (the same PID-liveness idiom
             ``_cleanup_stale_router_lock`` already uses in the boot
             script). The branch ref alone keeps any commits
             reachable, so removing only the checkout never loses
             data. A missing/unreadable marker, a live PID, or a
             permission error probing the PID all mean "leave it
             alone" — never touch anything we can't PROVE is dead.
          2. **Branch ref** — only deleted when its tip commit is
             reachable from some OTHER ref too (``git for-each-ref
             --contains <branch>`` returns more than the branch
             itself). A dead session that never reached APPLY+VERIFY+
             commit (the overwhelmingly common case) leaves a branch
             tip identical to the commit it forked from — trivially
             reachable from ``main``/other branches, so deleting the
             label loses nothing. A branch with unique, unreachable
             commits is intentionally LEFT ALONE (forensic evidence
             over cleanliness); the worktree-directory removal above
             already reclaims the disk debris, and the next session
             mints a freshly-nonced branch name regardless, so a rare
             orphaned-but-unique branch does not block boot.

        ``current_branch`` (the branch this session is about to
        create, or already owns) is NEVER touched — exact string
        match. Anything not matching ``branch_prefix`` is untouched:
        ``unit-*`` worktrees are structurally out of scope here
        (reaped separately by :meth:`reap_orphans`).

        NEVER raises — any per-entry failure is logged and skipped.
        Returns the count of worktree directories reaped (mirrors
        :meth:`reap_orphans`'s return contract; branch-only reaps of
        already-worktree-less refs are not counted).
        """
        reaped: Set[str] = set()

        try:
            from backend.core.ouroboros.governance.ledger_sovereignty import (  # noqa: E501
                read_ownership,
            )
        except Exception:  # noqa: BLE001 — defensive import
            read_ownership = None  # type: ignore[assignment]

        def _owner_is_dead(wt_path: Path) -> bool:
            """True ONLY when we have positive proof the creating PID
            is gone. Any uncertainty returns False — never touch."""
            if read_ownership is None:
                return False
            try:
                record = read_ownership(wt_path)
            except Exception:  # noqa: BLE001
                return False
            if record is None:
                return False
            pid = record.creator_pid
            if not pid or pid <= 0:
                return False
            if pid == os.getpid():
                return False  # this process itself — never reap our own
            try:
                os.kill(pid, 0)
                return False  # alive — existence probe succeeded
            except ProcessLookupError:
                return True  # definitively dead
            except PermissionError:
                return False  # different-user live process — conservative
            except OSError:
                return False  # unknown — conservative

        try:
            porcelain = await self._run_git_capture(
                ["worktree", "list", "--porcelain"],
            )
        except Exception:  # noqa: BLE001 — defensive
            porcelain = ""

        entries = _parse_worktree_porcelain(porcelain)
        known_registered = {
            e.get("branch", "")[len("refs/heads/"):]
            for e in entries
            if e.get("branch", "").startswith("refs/heads/")
        }
        dangling_branches: "list[str]" = []

        for entry in entries:
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
            if not branch_short.startswith(branch_prefix):
                continue
            if current_branch and branch_short == current_branch:
                continue
            if not _owner_is_dead(wt_path):
                continue  # live, unknown, or unowned — never touch

            git_ok = await self._git_worktree_remove(wt_path)
            if git_ok:
                reaped.add(str(wt_path))
            elif wt_path.exists():
                try:
                    shutil.rmtree(wt_path)
                    reaped.add(str(wt_path))
                except OSError as exc:
                    logger.warning(
                        "WorktreeManager.reap_dangling_auto_branches: "
                        "rmtree(%s) failed: %s", wt_path, exc,
                    )
                    continue  # dir removal failed — don't touch the branch
            dangling_branches.append(branch_short)

        # Bare branch refs matching the prefix that have NO registered
        # worktree at all (e.g. a prior worktree was already removed
        # but the ref survived). A branch that still has a LIVE
        # (not-just-reaped) registered worktree is protected here too.
        try:
            refs_out = await self._run_git_capture(
                ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{branch_prefix}*"],
            )
        except Exception:  # noqa: BLE001 — defensive
            refs_out = ""
        for name in refs_out.splitlines():
            name = name.strip()
            if not name.startswith(branch_prefix):
                continue
            if current_branch and name == current_branch:
                continue
            if name in known_registered and name not in dangling_branches:
                continue  # still has a live registered worktree
            if name not in dangling_branches:
                dangling_branches.append(name)

        for branch_short in dangling_branches:
            try:
                reachable = await self._branch_reachable_elsewhere(branch_short)
            except Exception:  # noqa: BLE001 — conservative on error
                reachable = False
            if reachable:
                await self._git_delete_branch(branch_short)
            else:
                logger.info(
                    "WorktreeManager.reap_dangling_auto_branches: "
                    "preserving branch %s — tip not reachable from any "
                    "other ref (possible unpushed autonomous commits; "
                    "worktree already reaped, branch left as forensic "
                    "evidence)",
                    branch_short,
                )

        try:
            await self._run_git_capture(["worktree", "prune"])
        except Exception:  # noqa: BLE001 — defensive
            pass

        if reaped:
            logger.info(
                "WorktreeManager.reap_dangling_auto_branches: reaped %d "
                "dangling worktree(s) from dead sessions",
                len(reaped),
            )
        return len(reaped)

    async def _branch_reachable_elsewhere(self, branch: str) -> bool:
        """True iff ``branch``'s tip commit is reachable from some ref
        OTHER than ``branch`` itself — i.e. deleting the branch label
        would not make the commit unreachable (safe to prune). Used to
        decide whether a dangling ``ouroboros/auto/*`` branch's ref
        can be deleted without risking unpushed/unmerged autonomous
        work. NEVER raises — a git failure is treated as "not
        reachable" (conservative — leaves the branch alone)."""
        out = await self._run_git_capture(
            ["for-each-ref", "--format=%(refname:short)", "--contains", branch],
        )
        others = [
            line.strip() for line in out.splitlines()
            if line.strip() and line.strip() != branch
        ]
        return bool(others)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_git_rc(
        self, root: Path, args: Sequence[str],
    ) -> Tuple[int, str, str]:
        """Run ``git -C <root> <args>``; return (rc, stdout, stderr).

        Unlike ``_run_git_capture`` this is rc-faithful (no empty-string
        failure sentinel) and root-parameterized — promotion executes
        against the TARGET checkout, not this manager's repo_root.
        """
        cmd = ["git", "-C", str(root), *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            return (
                proc.returncode or 0,
                out.decode(errors="replace"),
                err.decode(errors="replace"),
            )
        except OSError as exc:
            return 127, "", str(exc)

    async def _run_git_rc_ex(
        self,
        root: Path,
        args: Sequence[str],
        *,
        stdin_data: "Optional[bytes]" = None,
        extra_env: "Optional[Dict[str, str]]" = None,
    ) -> Tuple[int, str, str]:
        """``_run_git_rc`` with per-SPAWN stdin + env overrides.

        Added for the pending-ref object-surgery plumbing (2026-07-22):
        ``hash-object --stdin`` needs a stdin channel, and the
        throwaway-index weave needs ``GIT_INDEX_FILE`` scoped to ONE
        subprocess — mutating ``os.environ`` around awaits would leak
        the override into every concurrently spawned git call on the
        loop (race). The env copy is per-spawn; nothing global changes.
        """
        cmd = ["git", "-C", str(root), *args]
        env = None
        if extra_env:
            env = dict(os.environ)
            env.update(extra_env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin_data is not None else None
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await proc.communicate(input=stdin_data)
            return (
                proc.returncode or 0,
                out.decode(errors="replace"),
                err.decode(errors="replace"),
            )
        except OSError as exc:
            return 127, "", str(exc)

    @staticmethod
    def _baseline_exempt_dirty(
        target: Path,
        dirty_lines: Sequence[str],
        baseline_hashes: "Dict[str, str]",
    ) -> Tuple[bool, List[str]]:
        """Slice 12 — cryptographic dirty-target exemption (sync core, run
        via asyncio.to_thread). Returns ``(all_exempt, dirty_paths)``.

        A dirty touched path is exempt IFF ALL of:
          * its porcelain status is pure-Modify (any D/R/C/U/A/? code — a
            deletion, rename, copy, conflict, or untracked state — is a
            different class and fails closed);
          * the op's GENERATE-time baseline carries the path;
          * ``state_drift.file_sha256`` of the LIVE target file equals that
            baseline EXACTLY (unreadable/missing → None → never equal).

        Proof semantics (mandate 1): a byte-identical match to the GENERATE
        baseline means the "dirt" is precisely the defect state the model
        read and repaired — superseding it is the op's mission. Any human
        edit, however small, changes the hash and fails closed. No file
        paths, no chaos knowledge, no flags enter the decision.
        """
        from backend.core.ouroboros.governance.state_drift import file_sha256

        dirty_paths: List[str] = []
        all_exempt = True
        for line in dirty_lines:
            if len(line) < 4:
                all_exempt = False
                continue
            code, rel = line[:2], line[3:].strip()
            if " -> " in rel:  # rename form — not a pure modify
                all_exempt = False
                dirty_paths.append(rel)
                continue
            dirty_paths.append(rel)
            if not (set(code) <= {"M", " "} and "M" in code):
                all_exempt = False
                continue
            baseline = baseline_hashes.get(rel)
            if not baseline:
                all_exempt = False
                continue
            if file_sha256(target / rel) != baseline:
                all_exempt = False
        return all_exempt, dirty_paths

    async def promote_commits(
        self,
        target_root: Path,
        branch: str,
        commit_shas: Sequence[str],
        *,
        allow_ff: bool = True,
        baseline_hashes: "Optional[Dict[str, str]]" = None,
    ) -> PromotionResult:
        """Promote verified workspace commits onto ``target_root`` (Slice 11).

        The mechanism half of the promotion gap: moves the op's commits from
        the quarantined ``ouroboros/auto/<session>`` branch into the target
        checkout via ``merge --ff-only`` (only when the target head is an
        ancestor AND the branch tip is exactly the last promoted sha) or
        ``cherry-pick`` in sha order. Non-destructive by construction: no
        forced refs, no history rewrites on the target — a conflicted
        cherry-pick is aborted (git-native restore) and surfaced as a typed
        ``PromotionError('conflict_aborted')`` with the target working tree
        byte-identical.

        Fail-closed preflight, all read-only before the first mutating git:
        commit budget (``JARVIS_PROMOTION_MAX_COMMITS``, default 8) →
        branch + sha existence → touched-path dirty check
        (``JARVIS_PROMOTION_REQUIRE_CLEAN_TARGETS``, default true; scoped to
        the paths the promoted commits touch, so unrelated operator dirt
        never blocks). Governance (LiveWork consult, GENERATE-hash drift)
        lives in WorkspacePromoter — this layer is pure git.
        """
        target = Path(os.path.realpath(target_root))
        shas = [s for s in commit_shas if s]
        if not shas:
            return PromotionResult((), "none", str(target))
        try:
            _max = int(os.environ.get("JARVIS_PROMOTION_MAX_COMMITS", "8"))
        except ValueError:
            _max = 8
        if len(shas) > _max:
            raise PromotionError(
                "commit_budget_exceeded", "%d > %d" % (len(shas), _max),
            )

        rc, _, _ = await self._run_git_rc(
            target, ["rev-parse", "--verify", "--quiet",
                     "refs/heads/%s" % branch],
        )
        if rc != 0:
            raise PromotionError("branch_missing", branch)
        # Review P1: NORMALIZE to full ids. AutoCommitter reports SHORT
        # hashes (`rev-parse --short HEAD`); every downstream comparison
        # (ff range equality, landed-sha readback) needs full 40-char forms.
        full_shas: List[str] = []
        for s in shas:
            rc, out, _ = await self._run_git_rc(
                target, ["rev-parse", "--verify", "--quiet", s + "^{commit}"],
            )
            if rc != 0:
                raise PromotionError("git_failure", "unknown commit %s" % s[:12])
            full_shas.append(out.strip())

        touched: Set[str] = set()
        for s in full_shas:
            rc, out, err = await self._run_git_rc(
                target,
                ["diff-tree", "--no-commit-id", "--name-only", "-r", s],
            )
            if rc != 0:
                raise PromotionError(
                    "git_failure",
                    "diff-tree %s: %s" % (s[:12], err.strip()[:200]),
                )
            touched.update(ln for ln in out.splitlines() if ln.strip())

        _require_clean = os.environ.get(
            "JARVIS_PROMOTION_REQUIRE_CLEAN_TARGETS", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
        if _require_clean and touched:
            # Review B3: ':(literal)' pathspec magic — the repo tracks
            # bracket-named files (Next.js dynamic routes like
            # app/api/.../[jobId]/route.ts); raw pathspecs treat [..] as a
            # character class and silently miss dirt on exactly those files.
            rc, out, err = await self._run_git_rc(
                target,
                ["status", "--porcelain", "--",
                 *(":(literal)%s" % p for p in sorted(touched))],
            )
            if rc != 0:
                raise PromotionError(
                    "git_failure", "status: %s" % err.strip()[:200],
                )
            if out.strip():
                # Slice 12 — cryptographic exemption (Run-22 layer: the A1
                # chaos mutation IS uncommitted target dirt by protocol
                # design, and the verified repair was refused here). If
                # EVERY dirty touched path hash-matches the op's GENERATE
                # baseline, the dirt is provably the defect state the
                # repair supersedes: archive it (recoverable), restore the
                # paths to HEAD so ff/cherry-pick can apply, and proceed.
                # Anything unprovable fails closed to target_dirty exactly
                # as before. Kill switch:
                # JARVIS_PROMOTION_BASELINE_EXEMPTION_ENABLED (default on).
                _exempt_on = os.environ.get(
                    "JARVIS_PROMOTION_BASELINE_EXEMPTION_ENABLED", "true",
                ).strip().lower() in ("1", "true", "yes", "on")
                _dirty_lines = [
                    ln for ln in out.splitlines() if ln.strip()
                ]
                _all_exempt = False
                _dirty_paths: List[str] = []
                if _exempt_on and baseline_hashes:
                    _all_exempt, _dirty_paths = await asyncio.to_thread(
                        self._baseline_exempt_dirty,
                        target, _dirty_lines, dict(baseline_hashes),
                    )
                if not _all_exempt:
                    raise PromotionError(
                        "target_dirty", out.strip().splitlines()[0][:200],
                    )
                _archive_root = (
                    target / ".jarvis" / "promotion_dirt_archive"
                    / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                )
                try:
                    for _rel in _dirty_paths:
                        _src = target / _rel
                        _dst = _archive_root / _rel
                        _dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(_src, _dst)
                except OSError as exc:
                    raise PromotionError(
                        "target_dirty",
                        "exempt dirt could not be archived (%s) — "
                        "refusing to displace it" % exc,
                    )
                rc, _, err = await self._run_git_rc(
                    target,
                    ["restore", "--source=HEAD", "--worktree", "--",
                     *(":(literal)%s" % p for p in _dirty_paths)],
                )
                if rc != 0:
                    raise PromotionError(
                        "git_failure",
                        "baseline-exempt restore failed: %s"
                        % err.strip()[:200],
                    )
                logger.info(
                    "WorktreeManager.promote_commits: %d baseline-proven "
                    "dirty path(s) archived to %s and restored for landing",
                    len(_dirty_paths), _archive_root,
                )

        rc, out, _ = await self._run_git_rc(target, ["rev-parse", "HEAD"])
        if rc != 0:
            raise PromotionError("git_failure", "cannot resolve target HEAD")
        _pre_head = out.strip()

        _mode = "cherry-pick"
        _ff_taken = False
        if allow_ff:
            # Review P2: ff is only sound when the branch's commits-ahead
            # are EXACTLY the requested shas — `merge --ff-only` lands the
            # whole HEAD..tip range, so a session branch carrying earlier
            # quarantined (promotion-refused) commits must never ff past
            # the per-sha budget + dirty checks.
            rc_rl, rl_out, _ = await self._run_git_rc(
                target,
                ["rev-list", "--reverse", "HEAD..refs/heads/%s" % branch],
            )
            _range = [ln.strip() for ln in rl_out.splitlines() if ln.strip()]
            if rc_rl == 0 and _range == full_shas:
                rc_ff, _, err_ff = await self._run_git_rc(
                    target, ["merge", "--ff-only", branch],
                )
                if rc_ff == 0:
                    _mode = "ff"
                    _ff_taken = True
                else:
                    logger.debug(
                        "WorktreeManager.promote_commits: ff-only declined "
                        "(%s); falling through to cherry-pick",
                        err_ff.strip(),
                    )
        if not _ff_taken:
            rc_cp, _, err_cp = await self._run_git_rc(
                target, ["cherry-pick", "--allow-empty", *full_shas],
            )
            if rc_cp != 0:
                rc_ab, _, err_ab = await self._run_git_rc(
                    target, ["cherry-pick", "--abort"],
                )
                if rc_ab != 0:
                    raise PromotionError(
                        "git_failure",
                        "cherry-pick failed and abort failed: %s / %s"
                        % (err_cp.strip()[:200], err_ab.strip()[:200]),
                    )
                raise PromotionError("conflict_aborted", err_cp.strip()[:300])

        # Review P4: read back what actually LANDED — cherry-pick creates
        # NEW commit objects; telemetry keyed on workspace shas would point
        # at commits that do not exist on the operator branch.
        rc_lr, lr_out, _ = await self._run_git_rc(
            target, ["rev-list", "--reverse", "%s..HEAD" % _pre_head],
        )
        landed = tuple(
            ln.strip() for ln in lr_out.splitlines() if ln.strip()
        ) if rc_lr == 0 else ()
        logger.info(
            "WorktreeManager.promote_commits: %s %s -> %s "
            "(%d requested, %d landed)",
            _mode, branch, target, len(full_shas), len(landed),
        )
        return PromotionResult(
            tuple(full_shas), _mode, str(target), landed_shas=landed,
        )

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
