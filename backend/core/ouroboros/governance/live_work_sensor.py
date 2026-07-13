"""LiveWorkSensor — detect when a human is actively editing a file.

This sensor lets Venom/Ouroboros pause or escalate autonomous writes
against files the human is currently working on, so the autonomous
loop never stomps on uncommitted human edits.

Manifesto alignment: §7 absolute observability (we see what the human
is doing) + §6 zero-shortcut (never silently overwrite live work).

Signals (evaluated in order, cheapest first):

1. **Git dirty state × recency** — ``git status --porcelain``. An
   unstaged or staged-but-uncommitted change marks the file as "human
   touched" ONLY while its mtime is within ``active_window_s`` (Slice
   10 — Run #20: a file dirty for days is not being edited, and a
   chaos-injected file is dirty *by construction* until the repair
   lands, which a timeless signal itself forbids). Dirty + stale mtime
   falls through to signals 2/3. One subprocess per cache window.
   ``JARVIS_LIVE_WORK_DIRTY_REQUIRES_RECENCY=false`` restores the
   legacy timeless-dirty signal.

2. **Recent mtime** — If the file's mtime is within
   ``active_window_s`` (default 180s), treat it as active. Captures
   edits that haven't been saved via git yet.

3. **IDE lock/swap files** — ``.swp`` (vim), ``.swo``, ``.#file``
   (emacs), ``*~`` (many editors), ``.idea/*.iml`` dirty markers.
   Detected only for the specific target file's directory to keep
   the check O(dir_size).

The sensor caches git status per call window (``_git_cache_ttl_s``,
default 2.0s) so repeated ``is_human_active`` calls inside the same
orchestration pass don't re-fork git.

Env gates:
    JARVIS_LIVE_WORK_SENSOR_ENABLED (default "true")
    JARVIS_LIVE_WORK_ACTIVE_WINDOW_S (default "180")
    JARVIS_LIVE_WORK_GIT_CACHE_TTL_S (default "2.0")
    JARVIS_LIVE_WORK_DIRTY_REQUIRES_RECENCY (default "true")
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import NamedTuple, Optional, Set, Tuple

logger = logging.getLogger(__name__)


_ENABLED = os.environ.get("JARVIS_LIVE_WORK_SENSOR_ENABLED", "true").lower() != "false"
_DEFAULT_ACTIVE_WINDOW_S = float(os.environ.get("JARVIS_LIVE_WORK_ACTIVE_WINDOW_S", "180"))
_DEFAULT_GIT_CACHE_TTL_S = float(os.environ.get("JARVIS_LIVE_WORK_GIT_CACHE_TTL_S", "2.0"))


def is_enabled() -> bool:
    """Return True iff the sensor is currently active via env."""
    return _ENABLED


class SignalEval(NamedTuple):
    """One evaluation pass over the three signals for a single file.

    ``horizon_s`` is the sensor's prediction of how long until the file
    reads as idle: ``0.0`` when idle now, ``active_window_s - age`` when
    a recency-based signal holds, ``float("inf")`` when no expiry can be
    predicted (IDE lock present, legacy timeless-dirty, or a dirty file
    whose mtime cannot be read — fail SAFE).
    """

    active: bool
    reason: Optional[str]
    horizon_s: float


def _find_ide_lock_worker(parent_str: str, target_name: str) -> Optional[str]:
    """Module-level worker for :meth:`LiveWorkSensor._find_ide_lock`.

    Dispatched into the shared ``advisor-blast`` thread pool via
    ``cooperative_fs_io.offload`` (fs-hot-tier Batch 3, row 23). Lifted
    to module level so the offload trampoline doesn't capture any
    caller-local state. NEVER raises — an ``OSError`` during the scan
    (permission, race) degrades to ``None`` (no lock found).
    """
    parent = Path(parent_str)
    try:
        for entry in parent.iterdir():
            name = entry.name
            # vim swap: .target.swp / .target.swo / .target.swn
            if name.startswith(f".{target_name}.") and (
                name.endswith(".swp") or name.endswith(".swo") or name.endswith(".swn")
            ):
                return name
            # emacs lock: .#target
            if name == f".#{target_name}":
                return name
            # backup files: target~
            if name == f"{target_name}~":
                return name
    except OSError:
        return None
    return None


class LiveWorkSensor:
    """Detect whether a file is currently being touched by a human.

    One instance per orchestration pass is cheap — git status is
    cached for ``git_cache_ttl_s`` seconds to avoid repeated forks.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        active_window_s: float = _DEFAULT_ACTIVE_WINDOW_S,
        git_cache_ttl_s: float = _DEFAULT_GIT_CACHE_TTL_S,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._active_window_s = active_window_s
        self._git_cache_ttl_s = git_cache_ttl_s
        self._git_cache: Optional[Set[str]] = None
        self._git_cache_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_human_active(self, rel_path: str) -> Tuple[bool, Optional[str]]:
        """Return ``(active, reason)`` for a candidate file.

        ``rel_path`` is interpreted relative to ``repo_root``. ``reason``
        describes the first signal that fired, or ``None`` when the file
        appears idle. Any unexpected error returns ``(False, None)`` —
        we prefer to not block autonomous work on sensor malfunctions.
        """
        result = await self.evaluate(rel_path)
        return result.active, result.reason

    async def evaluate(self, rel_path: str) -> SignalEval:
        """Return the full signal evaluation for ``rel_path`` in ONE pass.

        Slice 10 review I1: single-eval consumers (the orchestrator's
        APPLY defer-wait gate) need ``(active, reason, horizon_s)`` from
        the SAME evaluation — two separate calls can disagree at the
        exact window boundary (active with horizon 0). ``is_human_active``
        and ``seconds_until_quiet`` remain as thin projections of this
        method for existing callers. Same never-raise discipline: the
        env-disabled and empty-path cases read as idle.
        """
        if not _ENABLED:
            return SignalEval(False, None, 0.0)
        if not rel_path:
            return SignalEval(False, None, 0.0)

        return await self._evaluate_signals(rel_path)

    async def seconds_until_quiet(self, rel_path: str) -> float:
        """Predict how long (seconds) until ``rel_path`` reads as idle.

        Mirrors :meth:`is_human_active`'s structure and never-raise
        discipline — both project the SAME signal evaluation. Returns
        ``0.0`` when the file is idle now, ``max(0.0, active_window_s -
        age)`` when a recency-based signal holds, and ``float("inf")``
        when no expiry can be predicted (IDE lock, legacy timeless-dirty,
        or a dirty file whose mtime cannot be read). Gives callers an
        exact, non-polled wait horizon (Slice 10 — the orchestrator's
        APPLY defer-wait).
        """
        result = await self.evaluate(rel_path)
        return result.horizon_s

    async def _evaluate_signals(self, rel_path: str) -> SignalEval:
        """Single evaluation pass behind :meth:`evaluate` — ONE copy of
        the signal logic; :meth:`is_human_active` and
        :meth:`seconds_until_quiet` are thin projections of the result.
        """
        normalized = rel_path.replace("\\", "/").lstrip("./")
        abs_path = (self._repo_root / normalized).resolve()

        # Slice 10 kill switch — read at call time; negative-list idiom
        # (review M2: "=0" must disable too). Any value outside the list
        # keeps recency composition; a listed value restores the legacy
        # timeless-dirty signal exactly.
        dirty_requires_recency = (
            os.environ.get("JARVIS_LIVE_WORK_DIRTY_REQUIRES_RECENCY", "true")
            .strip().lower() not in ("0", "false", "no", "off")
        )

        # 1. Git dirty state — cheapest reliable signal. Slice 10 (Run
        #    #20): dirty now QUALIFIES rather than decides — a file
        #    dirty for days is not being edited, and a chaos-injected
        #    file is dirty by construction until the repair lands, which
        #    a timeless signal itself forbids (deadlock class). Dirty
        #    composes with mtime recency; stale-dirty falls through to
        #    signals 2/3 (both quiet for a stale file).
        try:
            dirty = self._git_dirty_set()
            if normalized in dirty:
                if not dirty_requires_recency:
                    # Legacy: any uncommitted change is active, with no
                    # predictable expiry (dirty until the human commits).
                    return SignalEval(
                        True,
                        f"git status: {normalized} has uncommitted changes",
                        float("inf"),
                    )
                try:
                    age = time.time() - abs_path.stat().st_mtime
                except OSError:
                    # Dirty but mtime unreadable — fail SAFE (treat as
                    # active): the no-stomp guarantee outranks progress.
                    return SignalEval(
                        True,
                        f"git status: {normalized} has uncommitted changes "
                        "(mtime unreadable)",
                        float("inf"),
                    )
                if age < 0:
                    # Review I3: a FUTURE mtime (clock skew, touch -t) on
                    # a DIRTY file must fail SAFE like unreadable-mtime —
                    # legacy-dirty said active; reading it as idle would
                    # fail OPEN. Clean files keep signal-2's plain
                    # recent-window semantics (no-stomp binds to dirty).
                    return SignalEval(
                        True,
                        f"git status: {normalized} has uncommitted changes "
                        f"(mtime {int(-age)}s in the future)",
                        float("inf"),
                    )
                if age <= self._active_window_s:
                    return SignalEval(
                        True,
                        f"git status: {normalized} has uncommitted changes "
                        f"(modified {int(age)}s ago)",
                        max(0.0, self._active_window_s - age),
                    )
        except Exception as exc:  # noqa: BLE001 — sensor must not crash caller
            logger.debug("[LiveWork] git status unavailable: %s", exc)

        # 2. Recent mtime — captures unsaved / in-flight edits.
        try:
            if abs_path.exists() and abs_path.is_file():
                age = time.time() - abs_path.stat().st_mtime
                if 0 <= age <= self._active_window_s:
                    return SignalEval(
                        True,
                        f"mtime: modified {int(age)}s ago (window={int(self._active_window_s)}s)",
                        max(0.0, self._active_window_s - age),
                    )
        except OSError as exc:
            logger.debug("[LiveWork] stat failed for %s: %s", normalized, exc)

        # 3. IDE lock / swap file artefacts next to target.
        try:
            lock = await self._find_ide_lock(abs_path)
            if lock is not None:
                # A lock has no expiry the sensor can predict.
                return SignalEval(True, f"ide-lock: {lock}", float("inf"))
        except OSError as exc:
            logger.debug("[LiveWork] lock-scan failed for %s: %s", normalized, exc)

        return SignalEval(False, None, 0.0)

    def get_active_files(self) -> Set[str]:
        """Return the set of currently-active rel paths (git dirty union).

        We do NOT enumerate mtime or lock-file signals here — those are
        per-file checks and scanning the whole tree would be expensive.
        Use ``is_human_active(path)`` for the comprehensive answer.
        """
        if not _ENABLED:
            return set()
        try:
            return set(self._git_dirty_set())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[LiveWork] get_active_files failed: %s", exc)
            return set()

    def invalidate_cache(self) -> None:
        """Drop the cached git status — forces the next call to re-fork."""
        self._git_cache = None
        self._git_cache_at = 0.0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _git_dirty_set(self) -> Set[str]:
        now = time.monotonic()
        if (
            self._git_cache is not None
            and (now - self._git_cache_at) < self._git_cache_ttl_s
        ):
            return self._git_cache

        result = subprocess.run(  # noqa: S603 — trusted static argv
            ["git", "status", "--porcelain", "-z"],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        dirty: Set[str] = set()
        if result.returncode == 0:
            # -z output is NUL-separated; each record is "XY path" where
            # XY is the two-char status and path may contain spaces.
            for record in result.stdout.split("\x00"):
                if len(record) < 4:
                    continue
                path = record[3:]
                # Rename records carry "old -> new" — we only care about
                # the new path since that's what we'd overwrite.
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                dirty.add(path.replace("\\", "/"))
        else:
            logger.debug(
                "[LiveWork] git status rc=%d stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
        self._git_cache = dirty
        self._git_cache_at = now
        return dirty

    async def _find_ide_lock(self, abs_path: Path) -> Optional[str]:
        """Scan the target file's parent directory for IDE lock artefacts.

        Only checked if ``abs_path`` has an existing parent — avoids
        touching paths that don't belong to the repo.

        fs-hot-tier Batch 3 (row 23): the single-directory iterdir scan
        is dispatched off the asyncio loop via
        ``cooperative_fs_io.offload(cpu_bound=False)`` — thread pool
        (iterdir over one directory is cheap and IO-bound). Fail-soft:
        an ``OffloadError`` degrades to ``None`` — the same "no lock
        found" result an empty/missing directory gives.
        """
        parent = abs_path.parent
        if not parent.exists() or not parent.is_dir():
            return None

        from backend.core.ouroboros.governance.cooperative_fs_io import (
            offload,
            is_offload_error,
        )
        result = await offload(
            _find_ide_lock_worker, str(parent), abs_path.name,
            cpu_bound=False,
        )
        if is_offload_error(result):
            logger.debug(
                "[LiveWork] _find_ide_lock offload failed for %s — "
                "degrading to None", abs_path,
            )
            return None
        return result
