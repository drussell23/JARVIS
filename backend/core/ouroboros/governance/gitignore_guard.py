"""GitignoreGuard -- Slice 1 of AutoCommitterIgnoreGuard arc.
=============================================================

Pure-stdlib subprocess primitive that asks ``git`` whether a
given path matches a ``.gitignore`` rule -- INDEPENDENTLY of
whether the path is currently tracked. Closes the AutoCommitter
sovereignty gap surfaced in soak v4 where 93 ``.pyc`` files were
about to land in main because they were already tracked despite
``.gitignore``.

Why ``.gitignore`` is silent for tracked files: once a path is
in the index, ``git add <path>`` will continue to stage
modifications to it. ``git check-ignore --quiet -- <path>``
returns 0 if the path matches a ``.gitignore`` pattern,
regardless of tracked status -- this is the load-bearing
property the guard relies on.

Architectural decisions
-----------------------

* **Fail-open primitive, fail-closed pipeline.** This module
  returns ``False`` on subprocess failure (treats the file as
  "not ignored" and lets the existing pipeline proceed). Slice 2
  adds a post-staging validator that catches anything that
  slipped past the primitive -- the two layers compose into a
  fail-closed contract.
* **Batch subprocess by default.** ``git check-ignore --`` accepts
  N paths in one call and is dramatically cheaper than N
  subprocess calls. :func:`find_ignored_targets` issues one call;
  :func:`find_tracked_but_ignored` chunks ``git ls-files`` output.
* **Pure-stdlib.** Only ``subprocess`` + ``pathlib``. Mirrors the
  semantic_index ``git log`` discipline (5s default timeout, hard-
  clamped, FileNotFoundError-tolerant).
* **No state.** Every call is a fresh subprocess; no cache. This
  is intentional -- ``.gitignore`` can change mid-session, and a
  stale cache would silently re-introduce the breach we're
  closing.

Reverse-Russian-Doll posture
----------------------------

* O+V's commit engine becomes provably sovereign within repo
  boundaries. The guard is the immune cell that enforces
  "AutoCommitter cannot stage what ``.gitignore`` would refuse."
* Antivenom scales: closed-5 outcome enum, frozen violation
  dataclass, NEVER-raise IO discipline, master flag default-off
  until Slice 3 graduation.
"""
from __future__ import annotations

import enum
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.GitignoreGuard")


GITIGNORE_GUARD_SCHEMA_VERSION: str = "gitignore_guard.v1"


# Hard cap on per-batch file count for ``git check-ignore --``.
# Git's command-line argument length is OS-dependent (Linux ARG_MAX
# is typically 2 MiB; macOS is 1 MiB). 500 paths * ~200 char avg
# = 100 KB, well under either ceiling.
_BATCH_SIZE: int = 500


# ---------------------------------------------------------------------------
# Master flag + env knobs
# ---------------------------------------------------------------------------


def gitignore_guard_enabled() -> bool:
    """``JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED`` (default
    ``false`` until Slice 3 graduation).

    When off, every public function returns the no-op default:
      * :func:`is_path_ignored` -> False (treats everything as
        not-ignored)
      * :func:`find_ignored_targets` -> empty tuple
      * :func:`find_tracked_but_ignored` -> empty tuple

    No subprocess is launched on any path when the flag is off.

    Asymmetric env semantics -- empty/whitespace = unset = current
    default; explicit truthy/falsy overrides. Re-read on every
    call so flag flips hot-revert.
    """
    raw = os.environ.get(
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # pre-graduation default
    return raw in ("1", "true", "yes", "on")


def gitignore_check_timeout_s() -> float:
    """``JARVIS_GITIGNORE_CHECK_TIMEOUT_S`` (default 5.0, floor
    1.0, ceiling 30.0). Subprocess timeout for the ``git
    check-ignore`` call. Bounded so a hung git binary cannot
    stall AutoCommitter."""
    raw = os.environ.get("JARVIS_GITIGNORE_CHECK_TIMEOUT_S", "").strip()
    try:
        n = float(raw) if raw else 5.0
    except ValueError:
        n = 5.0
    return max(1.0, min(30.0, n))


# ---------------------------------------------------------------------------
# Closed-5-value taxonomy (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class GitignoreGuardOutcome(str, enum.Enum):
    """Closed taxonomy for guard verdicts.

    * ``CLEAN`` -- path is not ignored; safe to stage
    * ``SKIPPED_IGNORED`` -- path matches .gitignore + is not
      currently tracked; AutoCommitter should skip silently
    * ``BLOCKED_TRACKED_IGNORED`` -- path matches .gitignore +
      IS currently tracked; AutoCommitter should refuse + log
      a warning so the operator runs ``git rm --cached``
    * ``DISABLED`` -- master flag off; no check performed
    * ``FAILED`` -- subprocess failure / timeout; fail-open
      treated as CLEAN at the caller, but exposes the failure
      via this distinct verdict for telemetry
    """

    CLEAN = "clean"
    SKIPPED_IGNORED = "skipped_ignored"
    BLOCKED_TRACKED_IGNORED = "blocked_tracked_ignored"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen violation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitignoreViolation:
    """One on-disk path that breaches the .gitignore boundary.

    ``outcome`` discriminates between the two breach modes:
    SKIPPED_IGNORED (untracked + ignored -- AutoCommitter would
    have created a brand-new tracked-but-ignored entry) vs
    BLOCKED_TRACKED_IGNORED (already tracked + ignored -- the
    legacy soak v4 case where modifications would re-stage).

    ``source`` carries the matching .gitignore line/file when
    git emits it (passing ``--verbose`` to check-ignore), empty
    string when not available.
    """

    file_path: str
    outcome: GitignoreGuardOutcome
    source: str = ""
    schema_version: str = GITIGNORE_GUARD_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "outcome": self.outcome.value,
            "source": self.source,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Internal subprocess helpers
# ---------------------------------------------------------------------------


def _run_git(
    args: Sequence[str],
    *,
    repo_root: Path,
    timeout_s: float,
) -> Optional[subprocess.CompletedProcess]:
    """Run a git command with bounded timeout. Returns None on
    any subprocess failure (FileNotFoundError, TimeoutExpired,
    OSError). NEVER raises."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug(
            "[GitignoreGuard] git %s degraded: %s", args[0], exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 -- last-resort defensive
        logger.debug(
            "[GitignoreGuard] git %s last-resort degraded: %s",
            args[0], exc,
        )
        return None


def _check_ignore_batch(
    repo_root: Path,
    paths: Sequence[str],
    *,
    timeout_s: float,
) -> Tuple[str, ...]:
    """Run ``git check-ignore -- p1 p2 ...`` on a single batch.
    Returns the subset of ``paths`` that git reports as ignored.

    NEVER raises. Empty input -> empty output. Subprocess failure
    -> empty output (fail-open). Returns paths in their original
    case + slash-direction (git echoes them back verbatim).
    """
    if not paths:
        return ()
    # ``--no-index`` is load-bearing: by default git check-ignore
    # SKIPS tracked files (returns 1 even if they match a rule),
    # which would silently let the AutoCommitter sovereignty
    # breach through for the tracked-but-ignored case (the
    # 425 .pyc files in this repo). With --no-index the check is
    # rule-only, independent of index state.
    result = _run_git(
        ["check-ignore", "--no-index", "--", *paths],
        repo_root=repo_root,
        timeout_s=timeout_s,
    )
    if result is None:
        return ()
    # Returncodes:
    #   0 = at least one path is ignored
    #   1 = no path is ignored
    #   128 = error (e.g., not a git repo, malformed args)
    if result.returncode not in (0, 1):
        logger.debug(
            "[GitignoreGuard] git check-ignore returned %d: %s",
            result.returncode, (result.stderr or "").strip()[:200],
        )
        return ()
    if result.returncode == 1:
        return ()
    # Output: one line per ignored path. Preserve input order +
    # filter against the input set so we never echo back unexpected
    # paths.
    input_set = set(paths)
    out: List[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line in input_set:
            out.append(line)
    return tuple(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_path_ignored(
    repo_root: Path,
    file_path: str,
    *,
    timeout_s: Optional[float] = None,
) -> bool:
    """Return True if ``file_path`` matches a ``.gitignore`` rule
    in ``repo_root``. NEVER raises.

    Independent of tracked status -- ``git check-ignore`` reports
    on rule-matching, not on index state. This is what makes the
    guard load-bearing for the AutoCommitter sovereignty fix:
    even an already-tracked ``.pyc`` file returns True here, so
    AutoCommitter can refuse to stage modifications to it.

    Returns False (fail-open) when:
      * Master flag is off
      * file_path is empty / non-string
      * Subprocess fails (git missing / timeout / non-repo)
      * git returncode is unexpected (not 0 or 1)
    """
    if not gitignore_guard_enabled():
        return False
    if not isinstance(file_path, str) or not file_path.strip():
        return False
    timeout = (
        timeout_s if timeout_s is not None
        else gitignore_check_timeout_s()
    )
    matched = _check_ignore_batch(
        Path(repo_root), [file_path], timeout_s=timeout,
    )
    return file_path in matched


def find_ignored_targets(
    repo_root: Path,
    target_files: Sequence[str],
    *,
    timeout_s: Optional[float] = None,
) -> Tuple[str, ...]:
    """Return the subset of ``target_files`` that match
    ``.gitignore`` rules. Single batch subprocess (cheap).
    NEVER raises.

    Returns empty tuple when:
      * Master flag is off
      * target_files is empty
      * No element is a non-empty string
      * Subprocess fails (fail-open)
    """
    if not gitignore_guard_enabled():
        return ()
    if not target_files:
        return ()
    # Defensive coerce -- skip non-string + empty entries.
    cleaned: List[str] = []
    seen: set = set()
    for f in target_files:
        if not isinstance(f, str):
            continue
        s = f.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    if not cleaned:
        return ()
    timeout = (
        timeout_s if timeout_s is not None
        else gitignore_check_timeout_s()
    )
    return _check_ignore_batch(
        Path(repo_root), cleaned, timeout_s=timeout,
    )


def find_tracked_but_ignored(
    repo_root: Path,
    *,
    timeout_s: Optional[float] = None,
    batch_size: int = _BATCH_SIZE,
) -> Tuple[str, ...]:
    """Return every path that is currently TRACKED in the index
    AND matches a ``.gitignore`` rule. The audit helper for the
    one-shot migration: identifies legacy state (e.g., the 425
    ``.pyc`` files in this repo) so operators can ``git rm
    --cached`` them.

    Strategy:
      1. ``git ls-files`` -> all tracked paths
      2. Chunk into batches of ``batch_size`` (default 500)
      3. ``git check-ignore --`` per batch -> intersection

    NEVER raises. Returns empty tuple when:
      * Master flag is off
      * git binary missing / not a repo
      * Any subprocess in the chain times out
    """
    if not gitignore_guard_enabled():
        return ()
    timeout = (
        timeout_s if timeout_s is not None
        else gitignore_check_timeout_s()
    )
    root = Path(repo_root)
    # Step 1: enumerate tracked files.
    ls = _run_git(["ls-files"], repo_root=root, timeout_s=timeout)
    if ls is None or ls.returncode != 0:
        return ()
    tracked: List[str] = [
        line.strip()
        for line in ls.stdout.splitlines()
        if line.strip()
    ]
    if not tracked:
        return ()
    # Step 2 + 3: batched check-ignore.
    out: List[str] = []
    safe_batch = max(1, min(2000, int(batch_size)))
    for i in range(0, len(tracked), safe_batch):
        chunk = tracked[i: i + safe_batch]
        out.extend(_check_ignore_batch(
            root, chunk, timeout_s=timeout,
        ))
    return tuple(out)


def classify_path(
    repo_root: Path,
    file_path: str,
    *,
    timeout_s: Optional[float] = None,
) -> GitignoreGuardOutcome:
    """Classify ``file_path`` into the closed outcome taxonomy.
    Distinguishes SKIPPED_IGNORED (untracked + ignored, safe to
    silently skip) from BLOCKED_TRACKED_IGNORED (already tracked
    + ignored, AutoCommitter must refuse so operator runs ``git
    rm --cached``).

    Returns ``DISABLED`` when master flag off, ``FAILED`` when
    subprocess errors, ``CLEAN`` when not ignored. NEVER raises.
    """
    if not gitignore_guard_enabled():
        return GitignoreGuardOutcome.DISABLED
    if not isinstance(file_path, str) or not file_path.strip():
        return GitignoreGuardOutcome.FAILED
    timeout = (
        timeout_s if timeout_s is not None
        else gitignore_check_timeout_s()
    )
    root = Path(repo_root)
    try:
        # Is the path .gitignore-matched?
        if not is_path_ignored(root, file_path, timeout_s=timeout):
            return GitignoreGuardOutcome.CLEAN
        # Is it currently tracked?
        ls = _run_git(
            ["ls-files", "--error-unmatch", "--", file_path],
            repo_root=root, timeout_s=timeout,
        )
        if ls is None:
            # is_path_ignored said True but tracked-check failed --
            # be conservative and report FAILED so caller decides
            # (Slice 2 will treat FAILED as a refusal, not as
            # silent-skip).
            return GitignoreGuardOutcome.FAILED
        if ls.returncode == 0:
            return GitignoreGuardOutcome.BLOCKED_TRACKED_IGNORED
        return GitignoreGuardOutcome.SKIPPED_IGNORED
    except Exception as exc:  # noqa: BLE001 -- last-resort
        logger.debug(
            "[GitignoreGuard] classify_path last-resort "
            "degraded for %s: %s", file_path, exc,
        )
        return GitignoreGuardOutcome.FAILED


__all__ = [
    "GITIGNORE_GUARD_SCHEMA_VERSION",
    "GitignoreGuardOutcome",
    "GitignoreViolation",
    "classify_path",
    "find_ignored_targets",
    "find_tracked_but_ignored",
    "gitignore_check_timeout_s",
    "gitignore_guard_enabled",
    "is_path_ignored",
]
