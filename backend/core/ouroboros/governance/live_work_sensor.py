"""LiveWorkSensor — detect when a human is actively editing a file.

This sensor lets Venom/Ouroboros pause or escalate autonomous writes
against files the human is currently working on, so the autonomous
loop never stomps on uncommitted human edits.

Manifesto alignment: §7 absolute observability (we see what the human
is doing) + §6 zero-shortcut (never silently overwrite live work).

Signals (evaluated in order, cheapest first):

1. **Git dirty state** — ``git status --porcelain``. Any unstaged or
   staged-but-uncommitted change marks the file as "human touched".
   This is the strongest signal and the cheapest — one subprocess per
   cache window.

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
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Set, Tuple

logger = logging.getLogger(__name__)


_ENABLED = os.environ.get("JARVIS_LIVE_WORK_SENSOR_ENABLED", "true").lower() != "false"
_DEFAULT_ACTIVE_WINDOW_S = float(os.environ.get("JARVIS_LIVE_WORK_ACTIVE_WINDOW_S", "180"))
_DEFAULT_GIT_CACHE_TTL_S = float(os.environ.get("JARVIS_LIVE_WORK_GIT_CACHE_TTL_S", "2.0"))


def is_enabled() -> bool:
    """Return True iff the sensor is currently active via env."""
    return _ENABLED


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

    def is_human_active(self, rel_path: str) -> Tuple[bool, Optional[str]]:
        """Return ``(active, reason)`` for a candidate file.

        ``rel_path`` is interpreted relative to ``repo_root``. ``reason``
        describes the first signal that fired, or ``None`` when the file
        appears idle. Any unexpected error returns ``(False, None)`` —
        we prefer to not block autonomous work on sensor malfunctions.
        """
        if not _ENABLED:
            return False, None
        if not rel_path:
            return False, None

        normalized = rel_path.replace("\\", "/").lstrip("./")

        # 1. Git dirty state — cheapest reliable signal.
        try:
            dirty = self._git_dirty_set()
            if normalized in dirty:
                return True, f"git status: {normalized} has uncommitted changes"
        except Exception as exc:  # noqa: BLE001 — sensor must not crash caller
            logger.debug("[LiveWork] git status unavailable: %s", exc)

        abs_path = (self._repo_root / normalized).resolve()

        # 2. Recent mtime — captures unsaved / in-flight edits.
        try:
            if abs_path.exists() and abs_path.is_file():
                age = time.time() - abs_path.stat().st_mtime
                if 0 <= age <= self._active_window_s:
                    return True, f"mtime: modified {int(age)}s ago (window={int(self._active_window_s)}s)"
        except OSError as exc:
            logger.debug("[LiveWork] stat failed for %s: %s", normalized, exc)

        # 3. IDE lock / swap file artefacts next to target.
        try:
            lock = self._find_ide_lock(abs_path)
            if lock is not None:
                return True, f"ide-lock: {lock}"
        except OSError as exc:
            logger.debug("[LiveWork] lock-scan failed for %s: %s", normalized, exc)

        return False, None

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

    def _find_ide_lock(self, abs_path: Path) -> Optional[str]:
        """Scan the target file's parent directory for IDE lock artefacts.

        Only checked if ``abs_path`` has an existing parent — avoids
        touching paths that don't belong to the repo.
        """
        parent = abs_path.parent
        if not parent.exists() or not parent.is_dir():
            return None
        target_name = abs_path.name
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
        return None
