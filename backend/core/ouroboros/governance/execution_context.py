"""Sovereign Execution Boundary — canonical execution-context detection.

The boundary that keeps the autonomous Ouroboros loop out of the operator's
PRIMARY working tree needs two deterministic, spoof-resistant predicates.
This module is the single canonical home for both (Stage A); the OCA Iron
Gate and (later) the boot-time worktree router compose them — no duplication.

  * :func:`is_primary_checkout` — git ``--git-dir`` vs ``--git-common-dir``.
    A linked worktree's git-dir lives under ``<common>/worktrees/<name>`` so
    it differs from the common dir; the primary checkout's git-dir *equals*
    the common dir. Returns ``True`` ONLY on an affirmative match — any git
    error / ambiguity → ``False`` (we never block a commit on an unprovable
    "this is primary" claim).

  * :func:`is_autonomous` — CRYPTOGRAPHIC, not a fragile env boolean. The
    loop is autonomous iff there is NO valid HMAC-signed operator-presence
    marker for ``(repo_root, branch)``. Reuses
    :func:`operator_commit_authority.valid_operator_presence` — the exact
    primitive OCA's channel resolver uses — so an autonomous agent cannot
    forge operator status (the per-machine HMAC secret lives outside the
    repo). Lazy import avoids an import cycle (OCA imports this module for
    the gate).

Authority posture: pure detection, stdlib-only at module top, NEVER raises.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional


def _run_git(args: List[str], cwd: Path) -> Optional[str]:
    """Run ``git <args>`` in ``cwd``; return stripped stdout or None on
    any failure. NEVER raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001 — git absent / cwd gone / timeout
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def is_primary_checkout(repo_root: Optional[Path] = None) -> bool:
    """Return ``True`` iff ``repo_root`` is affirmatively the PRIMARY git
    checkout (``git-dir == git-common-dir``), ``False`` for a linked
    worktree OR on any git error / non-git dir.

    Deny-on-proof, never-on-doubt: the boundary that consumes this only
    blocks an autonomous commit when we are SURE it targets the primary
    tree, so a transient git hiccup can never wedge a legitimate worktree
    commit. NEVER raises."""
    cwd = Path(repo_root) if repo_root is not None else Path.cwd()
    git_dir = _run_git(["rev-parse", "--git-dir"], cwd)
    common_dir = _run_git(["rev-parse", "--git-common-dir"], cwd)
    if not git_dir or not common_dir:
        return False
    try:
        gd = Path(git_dir)
        cd = Path(common_dir)
        gd = (gd if gd.is_absolute() else cwd / gd).resolve()
        cd = (cd if cd.is_absolute() else cwd / cd).resolve()
        return gd == cd
    except Exception:  # noqa: BLE001
        return False


def is_autonomous(
    repo_root: Optional[Path] = None,
    branch: str = "",
) -> bool:
    """Return ``True`` iff running AUTONOMOUSLY — i.e. there is NO valid
    HMAC-signed operator-presence marker for ``(repo_root, branch)``.

    Reuses :func:`operator_commit_authority.valid_operator_presence` (the
    same cryptographic primitive OCA's channel resolver trusts) rather
    than any environment boolean an autonomous agent could set. Fail-safe:
    if the OCA substrate is unavailable we cannot PROVE operator presence,
    so we conservatively treat the session as autonomous. NEVER raises."""
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    try:
        from backend.core.ouroboros.governance import (
            operator_commit_authority as _oca,
        )
    except Exception:  # noqa: BLE001 — substrate unavailable
        return True
    try:
        return not bool(_oca.valid_operator_presence(root, branch))
    except Exception:  # noqa: BLE001
        return True


__all__ = ["is_autonomous", "is_primary_checkout"]
