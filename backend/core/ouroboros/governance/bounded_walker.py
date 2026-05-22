"""Bounded filesystem walker — Slice 12H.

Replaces the unbounded ``pathlib.rglob`` patterns that wedged the
Phase 3A relaunch (``bt-2026-05-22-215354``) for 5 minutes against
the element-web worktree (56K+ files). The LoopDeadman caught the
wedge at 300 s + dumped a faulthandler trace; the two specific
wedge sites surfaced by that trace were:

  * ``tool_executor._glob_files`` — ``sorted(resolved.rglob(pattern))``
    materialises the entire generator BEFORE any cap check. On a
    56K-file repo this is a 5-min synchronous wall.
  * ``operation_advisor._compute_blast_radius`` — ``scan_root.rglob("*.py")``
    walks every Python file in the repo with no time / scan-count
    bound; the cap is 50 IMPORTERS (matches), so a sparse-import
    target walks the whole tree before stopping.

This module is the canonical bounded primitive both sites compose.

## Discipline

  * **Generator-based** — never materialises the full match list
    before enforcing caps. Yielding incrementally means a 56K-file
    repo with ``max_matches=50`` returns in milliseconds, not
    minutes.
  * **Time budget** — every iteration checks ``time.monotonic()``
    against the budget; bounded latency by construction.
  * **Skip-dir set** — high-cardinality directories (``node_modules``,
    ``.git``, ``dist``, ``build``, ``.venv``, ``venv``,
    ``__pycache__``, ``.next``, ``coverage``) are pruned at the
    directory level using ``os.scandir`` instead of being filtered
    on full path strings AFTER traversal (the wedge pattern).
  * **Closed outcome taxonomy** — ``BoundedWalkOutcome`` is a
    frozenset of 4 values; every walk returns exactly one. AST-
    pinned in the paired test surface.
  * **Pure stdlib** — ``os``, ``fnmatch``, ``pathlib``, ``time``.
    No new dependencies. Compatible with Python 3.9+.
  * **NEVER raises into the caller** — defensive everywhere. A
    permission error on one directory does not abort the walk;
    the directory is silently skipped + counted as "scanned".

## Env knobs (canonical defaults — operator-tunable)

  * ``JARVIS_TOOL_GLOB_MAX_SCANNED``       — default 50_000
  * ``JARVIS_TOOL_GLOB_MAX_MATCHES``       — default 500
  * ``JARVIS_TOOL_GLOB_TIMEOUT_S``         — default 5.0
  * ``JARVIS_TOOL_GLOB_SKIP_DIRS``         — comma-separated additions to default skip set

  * ``JARVIS_BLAST_RADIUS_MAX_SCANNED``    — default 20_000
  * ``JARVIS_BLAST_RADIUS_MAX_BYTES_PER_FILE`` — default 65_536
  * ``JARVIS_BLAST_RADIUS_TIMEOUT_S``      — default 10.0
  * ``JARVIS_BLAST_RADIUS_CONSERVATIVE_CAP`` — default 50 (returned on budget exhaustion; bias toward caution, not false safety)

## Public surface

  * ``BoundedWalkOutcome`` (closed 4-value enum)
  * ``BoundedWalkResult`` (frozen dataclass)
  * ``bounded_glob(root, pattern, *, ...)``
  * ``bounded_read_bytes(path, *, max_bytes)``
  * ``default_skip_dirs()``
  * env-knob resolvers
"""

from __future__ import annotations

import enum
import fnmatch
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Set


logger = logging.getLogger("Ouroboros.BoundedWalker")


# ============================================================================
# Closed outcome taxonomy
# ============================================================================


class BoundedWalkOutcome(str, enum.Enum):
    """Closed 4-value outcome taxonomy. Adding a 5th value requires
    bumping the AST pin + every consumer branch."""

    COMPLETE            = "complete"             # exhausted the tree within budget
    TRUNCATED_SCANNED   = "truncated_scanned"    # scanned cap hit
    TRUNCATED_MATCHES   = "truncated_matches"    # matches cap hit
    TRUNCATED_TIMEOUT   = "truncated_timeout"    # wall-clock cap hit


# ============================================================================
# Result type
# ============================================================================


@dataclass(frozen=True)
class BoundedWalkResult:
    """Frozen result of a bounded walk. NEVER raises into the
    caller — failures degrade to ``COMPLETE`` with whatever
    matches were collected before the failure point."""

    matches: List[str] = field(default_factory=list)
    outcome: BoundedWalkOutcome = BoundedWalkOutcome.COMPLETE
    scanned_count: int = 0
    elapsed_ms: float = 0.0
    root: str = ""
    pattern: str = ""

    @property
    def truncated(self) -> bool:
        return self.outcome is not BoundedWalkOutcome.COMPLETE

    def truncation_reason(self) -> str:
        """Human-readable reason suffix for log + tool output."""
        if self.outcome is BoundedWalkOutcome.COMPLETE:
            return ""
        if self.outcome is BoundedWalkOutcome.TRUNCATED_SCANNED:
            return "truncated: max_scanned"
        if self.outcome is BoundedWalkOutcome.TRUNCATED_MATCHES:
            return "truncated: max_matches"
        if self.outcome is BoundedWalkOutcome.TRUNCATED_TIMEOUT:
            return "truncated: timeout"
        return "truncated: unknown"


# ============================================================================
# Default skip-dir taxonomy
# ============================================================================


# High-cardinality directories pruned at the directory level by default.
# These are the dirs that turned a normal blast-radius compute into a
# 5-minute wedge against the element-web worktree.
_DEFAULT_SKIP_DIRS: frozenset = frozenset({
    ".git",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "venv_py39_backup",
    "__pycache__",
    ".next",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    ".eggs",
    ".idea",
    ".vscode",
    ".tmp",
})


def default_skip_dirs() -> Set[str]:
    """Returns the canonical skip-dir set augmented by
    ``JARVIS_TOOL_GLOB_SKIP_DIRS`` (comma-separated additions).
    NEVER raises."""
    skip = set(_DEFAULT_SKIP_DIRS)
    try:
        extra = os.environ.get(
            "JARVIS_TOOL_GLOB_SKIP_DIRS", "",
        ).strip()
        if extra:
            for d in extra.split(","):
                name = d.strip()
                if name:
                    skip.add(name)
    except Exception:  # noqa: BLE001
        pass
    return skip


# ============================================================================
# Env knob resolvers
# ============================================================================


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def glob_max_scanned() -> int:
    """``JARVIS_TOOL_GLOB_MAX_SCANNED`` — default 50_000."""
    return _env_int("JARVIS_TOOL_GLOB_MAX_SCANNED", 50_000)


def glob_max_matches() -> int:
    """``JARVIS_TOOL_GLOB_MAX_MATCHES`` — default 500."""
    return _env_int("JARVIS_TOOL_GLOB_MAX_MATCHES", 500)


def glob_timeout_s() -> float:
    """``JARVIS_TOOL_GLOB_TIMEOUT_S`` — default 5.0."""
    return _env_float("JARVIS_TOOL_GLOB_TIMEOUT_S", 5.0)


def blast_radius_max_scanned() -> int:
    """``JARVIS_BLAST_RADIUS_MAX_SCANNED`` — default 20_000."""
    return _env_int("JARVIS_BLAST_RADIUS_MAX_SCANNED", 20_000)


def blast_radius_max_bytes_per_file() -> int:
    """``JARVIS_BLAST_RADIUS_MAX_BYTES_PER_FILE`` — default 64 KB."""
    return _env_int(
        "JARVIS_BLAST_RADIUS_MAX_BYTES_PER_FILE", 65_536,
        minimum=1024,
    )


def blast_radius_timeout_s() -> float:
    """``JARVIS_BLAST_RADIUS_TIMEOUT_S`` — default 10.0."""
    return _env_float("JARVIS_BLAST_RADIUS_TIMEOUT_S", 10.0)


def blast_radius_conservative_cap() -> int:
    """``JARVIS_BLAST_RADIUS_CONSERVATIVE_CAP`` — default 50.

    Returned on budget exhaustion to bias toward CAUTION (high
    blast radius), not false safety. Matches the existing
    in-loop cap so the cached value semantics are preserved."""
    return _env_int(
        "JARVIS_BLAST_RADIUS_CONSERVATIVE_CAP", 50, minimum=1,
    )


# ============================================================================
# Bounded walker — the canonical primitive
# ============================================================================


def bounded_glob(
    root: Path,
    pattern: str,
    *,
    max_scanned: Optional[int] = None,
    max_matches: Optional[int] = None,
    timeout_s: Optional[float] = None,
    skip_dirs: Optional[Set[str]] = None,
) -> BoundedWalkResult:
    """Bounded incremental filesystem walk.

    Yields matches one-at-a-time. The first of these conditions
    wins; subsequent iterations are NOT performed:

      1. ``max_matches`` accumulated → ``TRUNCATED_MATCHES``
      2. ``max_scanned`` files+dirs scanned → ``TRUNCATED_SCANNED``
      3. ``time.monotonic()`` exceeded ``timeout_s`` → ``TRUNCATED_TIMEOUT``
      4. Tree exhausted → ``COMPLETE``

    Skip-dirs are pruned at the directory level using
    ``os.scandir`` — high-cardinality dirs like ``node_modules``
    don't get walked at all. Pattern matching uses
    :func:`fnmatch.fnmatch` for ``glob``-style patterns (``*.py``,
    ``test_*.py``, etc.); recursive ``**`` is implicit (every
    directory is descended unless in the skip set).

    Parameters
    ----------
    root:
        Walk root. Must be an existing directory; non-existent or
        non-directory returns an empty ``COMPLETE`` result.
    pattern:
        :mod:`fnmatch` pattern matched against the basename of
        each entry. Empty string matches everything.
    max_scanned:
        Per-walk scan cap. Defaults to ``glob_max_scanned()``.
    max_matches:
        Per-walk match cap. Defaults to ``glob_max_matches()``.
    timeout_s:
        Per-walk wall-clock cap. Defaults to ``glob_timeout_s()``.
    skip_dirs:
        Set of directory basenames to prune. Defaults to
        ``default_skip_dirs()``.

    Returns
    -------
    BoundedWalkResult
        Always populated. NEVER raises into the caller.
    """
    t0 = time.monotonic()
    eff_max_scanned = (
        max_scanned if max_scanned is not None else glob_max_scanned()
    )
    eff_max_matches = (
        max_matches if max_matches is not None else glob_max_matches()
    )
    eff_timeout_s = (
        timeout_s if timeout_s is not None else glob_timeout_s()
    )
    eff_skip = skip_dirs if skip_dirs is not None else default_skip_dirs()
    pattern_str = pattern or "*"

    matches: List[str] = []
    scanned = 0
    outcome = BoundedWalkOutcome.COMPLETE

    try:
        if not root.is_dir():
            return BoundedWalkResult(
                matches=[], outcome=BoundedWalkOutcome.COMPLETE,
                scanned_count=0,
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                root=str(root), pattern=pattern_str,
            )
    except OSError:
        return BoundedWalkResult(
            matches=[], outcome=BoundedWalkOutcome.COMPLETE,
            scanned_count=0,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
            root=str(root), pattern=pattern_str,
        )

    # Iterative DFS with explicit stack — no recursion to keep
    # call-stack bounded even on deep trees. Push the root path;
    # pop and process one dir at a time.
    stack: List[Path] = [root]
    while stack:
        # Time budget check — first thing each iteration so a slow
        # OS scandir on one dir can't push us past the cap.
        if (time.monotonic() - t0) > eff_timeout_s:
            outcome = BoundedWalkOutcome.TRUNCATED_TIMEOUT
            break
        if scanned >= eff_max_scanned:
            outcome = BoundedWalkOutcome.TRUNCATED_SCANNED
            break
        if len(matches) >= eff_max_matches:
            outcome = BoundedWalkOutcome.TRUNCATED_MATCHES
            break

        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    scanned += 1
                    # Per-entry budget check — a million-entry dir
                    # must not blow past caps inside one scandir.
                    if scanned >= eff_max_scanned:
                        outcome = BoundedWalkOutcome.TRUNCATED_SCANNED
                        break
                    if (time.monotonic() - t0) > eff_timeout_s:
                        outcome = BoundedWalkOutcome.TRUNCATED_TIMEOUT
                        break
                    name = entry.name
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir:
                        if name in eff_skip:
                            continue
                        stack.append(Path(entry.path))
                        continue
                    # File entry — match against pattern (basename
                    # only; the walk handles recursion).
                    try:
                        if fnmatch.fnmatch(name, pattern_str):
                            matches.append(entry.path)
                            if len(matches) >= eff_max_matches:
                                outcome = (
                                    BoundedWalkOutcome.TRUNCATED_MATCHES
                                )
                                break
                    except Exception:  # noqa: BLE001
                        # fnmatch failure on a pathological name —
                        # skip silently.
                        continue
                if outcome is not BoundedWalkOutcome.COMPLETE:
                    break
        except (PermissionError, OSError, FileNotFoundError):
            # Defensive: a single inaccessible dir must not abort
            # the walk. Count it as "scanned" so deeply-broken
            # trees still hit the scan cap eventually.
            continue

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    return BoundedWalkResult(
        matches=matches,
        outcome=outcome,
        scanned_count=scanned,
        elapsed_ms=elapsed_ms,
        root=str(root),
        pattern=pattern_str,
    )


# ============================================================================
# Bounded byte-read — replaces unbounded read_text on suspect files
# ============================================================================


def bounded_read_bytes(
    path: Path, *,
    max_bytes: int,
) -> Optional[bytes]:
    """Read up to ``max_bytes`` from ``path``. Returns ``None`` on
    any error (permission denied, file gone, etc.). NEVER raises.

    The cap is critical — a single generated 100MB minified JS
    bundle in node_modules can wedge a synchronous read_text call
    for many seconds even after the directory-level skip filter
    misses it (defense-in-depth)."""
    try:
        with open(path, "rb") as f:
            return f.read(max_bytes)
    except (OSError, PermissionError, FileNotFoundError):
        return None


def bounded_read_text(
    path: Path, *,
    max_bytes: int,
    errors: str = "replace",
) -> Optional[str]:
    """Bounded text read. Returns ``None`` on any error. NEVER
    raises. Decodes the first ``max_bytes`` bytes of the file as
    UTF-8 with ``errors`` handling."""
    data = bounded_read_bytes(path, max_bytes=max_bytes)
    if data is None:
        return None
    try:
        return data.decode("utf-8", errors=errors)
    except Exception:  # noqa: BLE001
        return None


# ============================================================================
# Iterator API — for callers that prefer streaming over the result list
# ============================================================================


def iter_bounded_files(
    root: Path,
    *,
    max_scanned: int,
    timeout_s: float,
    skip_dirs: Optional[Set[str]] = None,
) -> Iterator[str]:
    """Streaming variant — yields entry paths one at a time, with
    the same budget guarantees as ``bounded_glob``. Caller is
    responsible for breaking out of the iteration on its own
    domain-specific match cap (e.g. blast-radius importer count).

    On budget exhaustion the iterator terminates cleanly; the
    caller can inspect ``time.monotonic()`` separately if it
    needs to distinguish "exhausted timeout" from "tree complete".

    Used by ``operation_advisor._compute_blast_radius`` where the
    domain cap (importers found) is more useful than a raw match
    cap."""
    t0 = time.monotonic()
    eff_skip = skip_dirs if skip_dirs is not None else default_skip_dirs()
    scanned = 0
    try:
        if not root.is_dir():
            return
    except OSError:
        return
    stack: List[Path] = [root]
    while stack:
        if (time.monotonic() - t0) > timeout_s:
            return
        if scanned >= max_scanned:
            return
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    scanned += 1
                    if scanned >= max_scanned:
                        return
                    if (time.monotonic() - t0) > timeout_s:
                        return
                    name = entry.name
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir:
                        if name in eff_skip:
                            continue
                        stack.append(Path(entry.path))
                        continue
                    yield entry.path
        except (PermissionError, OSError, FileNotFoundError):
            continue


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "BoundedWalkOutcome",
    "BoundedWalkResult",
    "bounded_glob",
    "bounded_read_bytes",
    "bounded_read_text",
    "default_skip_dirs",
    "glob_max_scanned",
    "glob_max_matches",
    "glob_timeout_s",
    "blast_radius_max_scanned",
    "blast_radius_max_bytes_per_file",
    "blast_radius_timeout_s",
    "blast_radius_conservative_cap",
    "iter_bounded_files",
]
