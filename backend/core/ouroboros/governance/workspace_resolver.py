"""WorkspaceResolver — the single authoritative ``.git``-anchored repo root.

THE RUN-#12 BUG (A1 live soak, pinned)
--------------------------------------
``test_runner._normalize`` does ``resolved.relative_to(repo_root.resolve())``
and raises ``BlockedPathError`` ("Path ... resolves outside repo root ...")
whenever the ``repo_root`` it was handed does **not** match the directory the
changed-file path anchors to. On the live node the repository lived at
``/opt/trinity/jarvis`` but the boot-hydration path computed / passed a
``repo_root`` that did not agree (the TestWatcher's ``repo_path`` defaulted to
``"."`` — the process CWD — and the sensor's ``_repo_root`` inherited that
default), so a perfectly valid ``tests/...py`` was rejected -> the resolver
fell back to the whole ``pytest tests/`` suite -> 180s SIGKILL -> the injected
chaos bug was **never detected**.

ROOT CAUSE: the repo root was computed *inconsistently* across the pipeline —
CWD here, ``__file__`` there, an env var somewhere else. There was no single
source of truth, so local and node could (and did) disagree.

THE FIX (this module)
---------------------
One deterministic, pure, fail-soft resolver: walk PARENTS from a start path
(default: this module's own ``__file__``, then the CWD as a fallback) until a
``.git`` directory **or** file is found, and return that directory fully
``.resolve()``d. The TestWatcher boot-hydration AND ``TestRunner`` /
``resolve_affected_tests`` both anchor to the **same** value from
:func:`resolve_repo_root`, and the boot-hydration ``git diff --name-only HEAD``
is run with ``cwd=repo_root`` so its output paths are repo-root-relative and
``_normalize`` agrees.

Design contract:

* **Zero hardcoding** — the root comes from the real ``.git`` anchor. There is
  no literal ``/opt/trinity`` or ``/Users/...`` anywhere; the only env override
  is the operator-set ``JARVIS_REPO_PATH`` (honored first when it itself
  resolves to a real directory, matching the existing pipeline convention).
* **Linked-worktree aware** — ``git worktree add`` creates a ``.git`` *file*
  (``gitdir: <path>``), not a directory. Both forms anchor a repo root, so the
  walk accepts ``.git`` whether it is a file or a directory.
* **Pure + deterministic** — same inputs, same output. No I/O beyond stat.
* **Cached** — the default (no-``start``) resolution is memoized; the cache is
  keyed by the resolved start path so an explicit ``start`` (e.g. a tmp fixture
  repo in a hermetic test) is never poisoned by, nor poisons, the default.
* **Fail-soft** — no ``.git`` anywhere up the tree -> return the CWD
  (``.resolve()``d). Never raises, never returns ``None``.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Optional

__all__ = ["resolve_repo_root", "clear_cache"]

# Operator override env var — the SAME one the existing TestWatcher /
# TestFailureSensor already read, so this resolver is a drop-in single source
# of truth rather than a competing convention.
_ENV_REPO_PATH = "JARVIS_REPO_PATH"

# Cache: resolved-start-path -> resolved repo root. Bounded by the number of
# distinct start anchors (effectively 1-2 in production: the module path and
# the CWD). Guarded so concurrent boot tasks can't race a half-written entry.
_CACHE: Dict[str, Path] = {}
_CACHE_LOCK = threading.Lock()


def _has_git_anchor(directory: Path) -> bool:
    """Return True iff *directory* holds a ``.git`` anchor (dir OR file).

    A ``.git`` directory is the main repo root; a ``.git`` file
    (``gitdir: <path>``) is a linked ``git worktree`` checkout. Both are
    legitimate repo roots that anchor relative-path math. Fail-soft: any
    OSError (permission, race, odd path) is treated as "no anchor here".
    """
    try:
        return (directory / ".git").exists()
    except OSError:
        return False


def _walk_to_git_root(start: Path) -> Optional[Path]:
    """Walk *start* and its parents until a ``.git`` anchor is found.

    Returns the anchoring directory (already ``.resolve()``d by the caller's
    start) or ``None`` if no anchor exists anywhere up the tree. Never raises.
    """
    try:
        # If *start* is a file, begin the walk at its containing directory;
        # if it is a directory, include it as the first candidate.
        first = start if start.is_dir() else start.parent
    except OSError:
        first = start.parent

    for ancestor in (first, *first.parents):
        if _has_git_anchor(ancestor):
            return ancestor
    return None


def resolve_repo_root(start: Optional[Path] = None) -> Path:
    """Return the authoritative ``.git``-anchored repository root.

    Resolution order (first hit wins), all fully ``.resolve()``d:

    1. **Explicit start** — when *start* is provided, walk its parents to the
       ``.git`` anchor. This is the hermetic-test / multi-repo entry point
       (e.g. a tmp fixture repo). No env override is consulted for an explicit
       start so the caller's intent is honored exactly.
    2. **Operator env override** — ``JARVIS_REPO_PATH`` when it itself resolves
       to a real directory (matches the existing TestWatcher convention). The
       value is still ``.resolve()``d so a relative override is anchored.
    3. **This module's ``__file__``** — walk up to the ``.git`` anchor. This is
       the production default and is robust to the process CWD (the run-#12
       failure vector).
    4. **CWD** — walk up from the current working directory.
    5. **Fail-soft** — no anchor found anywhere -> the ``.resolve()``d CWD.

    Pure + deterministic + cached. NEVER raises, NEVER returns ``None``.
    """
    if start is not None:
        try:
            anchored = start.resolve()
        except OSError:
            anchored = start
        cache_key = f"start::{anchored}"
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached
        found = _walk_to_git_root(anchored)
        result = found if found is not None else _safe_cwd()
        with _CACHE_LOCK:
            _CACHE[cache_key] = result
        return result

    # Default (no explicit start) — memoized under a stable key.
    cache_key = "default"
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    result = _resolve_default()
    with _CACHE_LOCK:
        _CACHE[cache_key] = result
    return result


def _resolve_default() -> Path:
    """Compute the production default root (env -> __file__ -> cwd -> cwd)."""
    # 2. Operator env override, if it points at a real directory.
    raw = os.environ.get(_ENV_REPO_PATH, "").strip()
    if raw:
        try:
            candidate = Path(raw).expanduser().resolve()
            if candidate.is_dir():
                # Walk from the override too — it may point *into* the repo
                # rather than exactly at the root; the ``.git`` anchor decides.
                found = _walk_to_git_root(candidate)
                if found is not None:
                    return found
                # Override is a real dir but not under any .git — honor it
                # verbatim (operator intent), still resolved.
                return candidate
        except OSError:
            pass

    # 3. This module's own location — CWD-independent (the run-#12 fix).
    try:
        here = Path(__file__).resolve()
        found = _walk_to_git_root(here)
        if found is not None:
            return found
    except (OSError, NameError):
        pass

    # 4. CWD walk.
    found = _walk_to_git_root(_safe_cwd())
    if found is not None:
        return found

    # 5. Fail-soft.
    return _safe_cwd()


def _safe_cwd() -> Path:
    """Return the resolved CWD, never raising."""
    try:
        return Path.cwd().resolve()
    except OSError:
        return Path(".")


def clear_cache() -> None:
    """Drop the memoized resolutions (test isolation / env-flip support)."""
    with _CACHE_LOCK:
        _CACHE.clear()
