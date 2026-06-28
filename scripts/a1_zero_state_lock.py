# scripts/a1_zero_state_lock.py
"""
A1 Zero-State Lock — Cryptographic Pristine Assertion for A1 Cloud-Confirm runs.

Computes a deterministic SHA256 over the ENTIRE active git working-tree state
and hard-ABORTs if it deviates from the computed pristine baseline.  No silent
failures, no ghost state, no hardcoded expected digest.

Usage (CLI)::

    python3 scripts/a1_zero_state_lock.py [--repo-root PATH] [--no-sweep] [--json]

Exit codes:
    0  PRISTINE  — ZERO_STATE_ASSERTED sha=<digest>
    1  NOT PRISTINE — ZERO_STATE_ABORT expected=<x> actual=<y> deviations=[...]
    2  INTERNAL ERROR — fail-closed (unknown state, treat as ABORT)
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import FrozenSet, List, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ZeroStateResult:
    """Result of a pristine-state assertion."""

    pristine: bool
    expected_digest: str
    actual_digest: str
    deviations: List[str]


# ---------------------------------------------------------------------------
# Git helpers (no shell=True, explicit cwd via -C)
# ---------------------------------------------------------------------------

def _git(repo_root: Path, *args: str) -> str:
    """Run ``git -C <repo_root> <args>`` and return stdout.

    Raises ``RuntimeError`` on non-zero exit (fail-closed).
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _parse_worktree_paths(porcelain: str) -> List[str]:
    """Parse ``git worktree list --porcelain`` output into an ordered list of paths.

    The first entry is always the main worktree; subsequent entries are linked
    worktrees.  Order is preserved so the caller can identify the main checkout.
    """
    paths: List[str] = []
    for line in porcelain.splitlines():
        line = line.strip()
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):])
    return paths


# ---------------------------------------------------------------------------
# Orphan directory detection
# ---------------------------------------------------------------------------

# Legacy prefix list — kept for reference / backward-compat callers that lack
# worktree-list data.  The primary code path now uses set-difference (I4).
_ORPHAN_DIR_PREFIXES = ("unit-", "ouroboros__auto__bt-", "soak-")


def _get_orphan_dirs(
    worktrees_dir: Path,
    registered_basenames: Optional[FrozenSet[str]] = None,
) -> List[str]:
    """Return sorted list of orphan directory NAMES under ``.worktrees/``.

    I4 fix: when *registered_basenames* is provided (the set of basenames from
    ``git worktree list --porcelain``), returns ALL child directory names that
    are NOT in that set — a true set-difference, independent of naming prefix.
    An unregistered orphan dir with any name is caught.

    When *registered_basenames* is None (legacy / backward-compat callers),
    falls back to the prefix-based filter.

    Only directory names (not full paths) are returned for stable serialization
    regardless of repo location.
    """
    if not worktrees_dir.is_dir():
        return []
    dirs: List[str] = []
    for child in worktrees_dir.iterdir():
        if not child.is_dir():
            continue
        if registered_basenames is not None:
            # Set-difference: any on-disk dir NOT in the registered set is orphan.
            if child.name not in registered_basenames:
                dirs.append(child.name)
        else:
            # Fallback: prefix-based filter.
            if any(child.name.startswith(p) for p in _ORPHAN_DIR_PREFIXES):
                dirs.append(child.name)
    return sorted(dirs)


# ---------------------------------------------------------------------------
# Canonical serialization + digest
# ---------------------------------------------------------------------------

def _canonical_string(
    head: str,
    status_lines: List[str],
    worktree_paths: List[str],
    orphan_dirs: List[str],
    chaos_manifest: bool,
) -> str:
    """Build the stable, newline-joined canonical string that is SHA256-hashed.

    All variable-length lists are sorted before serialization so the output is
    independent of subprocess ordering or filesystem ordering.  No timestamps,
    PIDs, or random values are included.

    Fields:
        head:<git-HEAD-sha>
        status:<porcelain-line>  (one per dirty/untracked entry, sorted)
        worktree:<path>          (one per registered worktree, sorted)
        orphan_dir:<name>        (one per orphan dir under .worktrees/, sorted)
        chaos_manifest:present|absent
    """
    parts: List[str] = []
    parts.append(f"head:{head.strip()}")
    for line in sorted(status_lines):
        parts.append(f"status:{line}")
    for path in sorted(worktree_paths):
        parts.append(f"worktree:{path}")
    for name in sorted(orphan_dirs):
        parts.append(f"orphan_dir:{name}")
    parts.append(f"chaos_manifest:{'present' if chaos_manifest else 'absent'}")
    return "\n".join(parts)


def compute_state_digest(repo_root: "str | Path") -> str:
    """Compute and return a deterministic SHA256 hex digest of the full
    git working-tree state at ``repo_root``.

    This is a pure read — it does not modify anything.  Raises ``RuntimeError``
    if any git subprocess fails (fail-closed).
    """
    repo_root = Path(repo_root).resolve()

    head = _git(repo_root, "rev-parse", "HEAD").strip()

    status_raw = _git(
        repo_root, "status", "--porcelain=v1", "--untracked-files=all"
    )
    status_lines = [ln for ln in status_raw.splitlines() if ln.strip()]

    worktree_porcelain = _git(repo_root, "worktree", "list", "--porcelain")
    worktree_paths = _parse_worktree_paths(worktree_porcelain)

    worktrees_dir = repo_root / ".worktrees"
    # I4: pass registered basenames so ALL unregistered dirs are caught.
    registered_wt_basenames: FrozenSet[str] = frozenset(
        Path(p).name for p in worktree_paths
    )
    orphan_dirs = _get_orphan_dirs(worktrees_dir, registered_wt_basenames)

    chaos_manifest = (repo_root / ".jarvis" / "chaos_manifest.json").exists()

    canonical = _canonical_string(
        head, status_lines, worktree_paths, orphan_dirs, chaos_manifest
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Orphan sweep (reuses WorktreeManager.reap_orphans)
# ---------------------------------------------------------------------------

async def _sweep(repo_root: Path) -> int:
    """Invoke WorktreeManager.reap_orphans to remove orphaned worktrees.

    A single call with branch_prefix="unit-" is sufficient: the manager
    internally expands to ALL campaign-debris prefixes via
    _resolve_reap_prefixes(), sweeping:
        unit-              (L3 isolation prefix)
        ouroboros/auto/bt- (branch form of auto-soak worktrees)
        ouroboros__auto__bt- (on-disk dir form of auto-soak worktrees)
        soak-              (legacy soak worktrees)
    plus any extras in JARVIS_WORKTREE_REAP_PREFIXES.

    Returns the count of worktrees reaped.  Raises ImportError / RuntimeError
    on failure (caller may warn and continue — the assert step is authoritative).
    """
    from backend.core.ouroboros.governance.worktree_manager import WorktreeManager  # lazy import

    mgr = WorktreeManager(repo_root=repo_root)
    return await mgr.reap_orphans(branch_prefix="unit-")


# ---------------------------------------------------------------------------
# Pristine assertion
# ---------------------------------------------------------------------------

def assert_pristine(
    repo_root: "str | Path",
    *,
    sweep: bool = True,
) -> ZeroStateResult:
    """Assert the repo at ``repo_root`` is in a pristine zero state.

    When ``sweep=True`` (default), the WorktreeManager orphan reaper runs
    FIRST to remove recoverable ghost worktrees; the state is then re-sampled
    before computing the digest.  If anything remains non-pristine after sweep,
    that is REAL ghost state and we ABORT.

    The **pristine baseline is derived, never hardcoded**.  The expected digest
    is computed from the SAME HEAD as the actual tree, but with empty status
    (no dirty/untracked files), only the main worktree, no orphan dirs, and no
    chaos manifest.  This means the expected digest rotates with HEAD, so the
    lock is always relative to the current commit — not a fixed SHA.

    Returns a :class:`ZeroStateResult`.  Raises ``RuntimeError`` on any
    internal error (fail-closed — caller should treat this as ABORT rc=2).
    """
    repo_root = Path(repo_root).resolve()

    if sweep:
        try:
            reaped = asyncio.run(_sweep(repo_root))
            # M2: always surface the count — a money-gate must be observable.
            print(
                f"swept {reaped} orphan worktree(s) before assertion",
                file=sys.stderr,
            )
        except Exception as exc:  # pragma: no cover
            # Sweep failure is non-fatal: log a warning and proceed to assert.
            # If orphans survive they will be caught by the digest mismatch.
            print(
                f"WARN: a1_zero_state_lock: orphan sweep failed ({exc}); "
                "proceeding to assertion",
                file=sys.stderr,
            )

    # --- Sample actual state ---
    head = _git(repo_root, "rev-parse", "HEAD").strip()

    status_raw = _git(
        repo_root, "status", "--porcelain=v1", "--untracked-files=all"
    )
    status_lines = [ln for ln in status_raw.splitlines() if ln.strip()]

    worktree_porcelain = _git(repo_root, "worktree", "list", "--porcelain")
    worktree_paths = _parse_worktree_paths(worktree_porcelain)

    worktrees_dir = repo_root / ".worktrees"
    # I4: set-difference — any dir in .worktrees/ not in registered set is orphan.
    registered_wt_basenames: FrozenSet[str] = frozenset(
        Path(p).name for p in worktree_paths
    )
    orphan_dirs = _get_orphan_dirs(worktrees_dir, registered_wt_basenames)

    chaos_manifest = (repo_root / ".jarvis" / "chaos_manifest.json").exists()

    # --- Compute actual digest ---
    actual_canonical = _canonical_string(
        head, status_lines, worktree_paths, orphan_dirs, chaos_manifest
    )
    actual_digest = hashlib.sha256(actual_canonical.encode()).hexdigest()

    # --- Compute expected (pristine) digest ---
    # Expected state: same HEAD, no dirty files, only the main worktree, no
    # orphan dirs, no chaos manifest.  Main worktree is always the first entry
    # from `git worktree list`; we derive it from the actual output so the
    # path is never hardcoded.
    main_wt: List[str] = worktree_paths[:1]  # empty list if somehow missing
    expected_canonical = _canonical_string(head, [], main_wt, [], False)
    expected_digest = hashlib.sha256(expected_canonical.encode()).hexdigest()

    # --- Collect human-readable deviations ---
    deviations: List[str] = []
    for line in sorted(status_lines):
        deviations.append(f"dirty_or_untracked: {line.strip()}")
    for wt in sorted(worktree_paths[1:]):  # extras beyond the main worktree
        deviations.append(f"extra_worktree: {wt}")
    for name in orphan_dirs:
        deviations.append(f"orphan_dir: {name}")
    if chaos_manifest:
        deviations.append("chaos_manifest: .jarvis/chaos_manifest.json present")

    pristine = actual_digest == expected_digest

    return ZeroStateResult(
        pristine=pristine,
        expected_digest=expected_digest,
        actual_digest=actual_digest,
        deviations=deviations,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: "Optional[List[str]]" = None) -> int:
    """CLI entry point.

    Returns:
        0  — pristine (ZERO_STATE_ASSERTED)
        1  — not pristine (ZERO_STATE_ABORT)
        2  — internal error (fail-closed, treat as ABORT)
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Assert A1 zero-state: repo must be cryptographically pristine "
                    "before a real-money cloud run fires.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Absolute path to the git repository root.  "
             "Default: parent directory of this script (i.e. <repo_root>/scripts/../).",
    )
    parser.add_argument(
        "--no-sweep",
        action="store_true",
        help="Skip the orphan-worktree reaper before asserting.  "
             "Assert the state exactly as-is.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output a machine-readable JSON object to stdout.",
    )
    args = parser.parse_args(argv)

    # Resolve repo root: explicit flag > derive from __file__
    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        # This script lives at <repo_root>/scripts/a1_zero_state_lock.py
        repo_root = Path(__file__).resolve().parent.parent

    try:
        result = assert_pristine(repo_root, sweep=not args.no_sweep)
    except Exception as exc:
        payload = {"status": "ERROR", "error": str(exc)}
        if args.json:
            print(json.dumps(payload), file=sys.stdout)
        else:
            print(f"ZERO_STATE_ABORT error={exc}", file=sys.stderr)
        return 2  # fail-closed: unknown internal error → ABORT

    if args.json:
        payload = {
            "status": "PRISTINE" if result.pristine else "NOT_PRISTINE",
            "expected": result.expected_digest,
            "actual": result.actual_digest,
            "deviations": result.deviations,
        }
        print(json.dumps(payload, indent=2))
    else:
        if result.pristine:
            print(f"ZERO_STATE_ASSERTED sha={result.actual_digest}")
        else:
            print(
                f"ZERO_STATE_ABORT "
                f"expected={result.expected_digest} "
                f"actual={result.actual_digest} "
                f"deviations={result.deviations}",
                file=sys.stderr,
            )

    return 0 if result.pristine else 1


if __name__ == "__main__":
    sys.exit(main())
