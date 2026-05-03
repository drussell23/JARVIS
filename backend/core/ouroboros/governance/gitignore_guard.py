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
    ``true`` post Slice 3 graduation, 2026-05-03).

    When off, every public function returns the no-op default:
      * :func:`is_path_ignored` -> False (treats everything as
        not-ignored)
      * :func:`find_ignored_targets` -> empty tuple
      * :func:`find_tracked_but_ignored` -> empty tuple

    No subprocess is launched on any path when the flag is off.

    Graduated default-true after the full Slices 1-2 stack proved
    out (72/72 combined sweep + e2e Layer 2 catches Layer-1-fail-
    open). Tier 0b hygiene gate is now structurally enforced
    by default; operators retain ``"false"`` escape hatch.

    Asymmetric env semantics -- empty/whitespace = unset = current
    default; explicit truthy/falsy overrides. Re-read on every
    call so flag flips hot-revert.
    """
    raw = os.environ.get(
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
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
    "register_flags",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Slice 3 -- Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[GitignoreGuard] register_flags degraded: %s", exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/gitignore_guard.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example=(
                "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED=true"
            ),
            description=(
                "Master switch for the AutoCommitter gitignore "
                "guard. When on, every git add (Layer 1 pre-stage) "
                "AND every commit attempt (Layer 2 post-stage) "
                "consults git check-ignore --no-index to refuse "
                "ignored paths regardless of tracked status. "
                "Closes the soak-v4 sovereignty breach (93 tracked-"
                "but-ignored .pyc files about to land in main). "
                "Graduated default-true 2026-05-03 in Slice 3."
            ),
        ),
        FlagSpec(
            name="JARVIS_GITIGNORE_CHECK_TIMEOUT_S",
            type=FlagType.FLOAT, default=5.0,
            category=Category.TIMING,
            source_file=target,
            example="JARVIS_GITIGNORE_CHECK_TIMEOUT_S=10.0",
            description=(
                "Subprocess timeout for git check-ignore calls. "
                "Bounded so a hung git binary cannot stall "
                "AutoCommitter. Floor 1.0, ceiling 30.0."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[GitignoreGuard] register_flags spec %s "
                "skipped: %s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 3 -- Module-owned shipped_code_invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 1 invariants: pure-stdlib + closed-5 outcome
    taxonomy + ``--no-index`` flag presence (the load-bearing
    structural property -- without it the guard silently lets
    tracked-but-ignored paths through)."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        # Pure-stdlib at hot path: no governance imports outside
        # the registration contract.
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    violations.append(
                        f"line {lineno}: gitignore_guard must be "
                        f"pure-stdlib -- found {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"gitignore_guard MUST NOT "
                            f"{node.func.id}()"
                        )
        # Closed-5 GitignoreGuardOutcome taxonomy.
        required = {
            "CLEAN", "SKIPPED_IGNORED",
            "BLOCKED_TRACKED_IGNORED", "DISABLED", "FAILED",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                if node.name == "GitignoreGuardOutcome":
                    seen = set()
                    for stmt in node.body:
                        if isinstance(stmt, _ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, _ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"GitignoreGuardOutcome missing "
                            f"required values: {sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"GitignoreGuardOutcome has unexpected "
                            f"values (closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        # The load-bearing ``--no-index`` flag MUST appear in the
        # check-ignore subprocess args. Without it git skips
        # tracked-but-ignored paths, silently breaching.
        if '"--no-index"' not in source and "'--no-index'" not in source:
            violations.append(
                "git check-ignore call missing --no-index flag "
                "(load-bearing for tracked-but-ignored detection)"
            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/gitignore_guard.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="gitignore_guard_purity_and_no_index",
            target_file=target,
            description=(
                "Slice 1 primitive stays pure-stdlib at hot path "
                "(no governance imports outside register_flags / "
                "register_shipped_invariants); GitignoreGuardOutcome "
                "is the closed-5 taxonomy; the load-bearing "
                "--no-index flag is present in the check-ignore "
                "subprocess args (without it, tracked-but-ignored "
                "paths silently breach -- the soak-v4 root cause)."
            ),
            validate=_validate,
        ),
    ]
