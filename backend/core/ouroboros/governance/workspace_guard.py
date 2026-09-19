"""Refuse work that is running in the wrong tree.

## The failure this prevents

This host carries TWO checkouts of the same repository by design: the native
WSL tree (`/home/jarvis_svc/jarvis`) where authoring and soaks run, and the
Windows tree (`/mnt/c/...`) that VS Code renders and that `ov`'s editable
install executes. A post-commit hook mirrors the first into the second.

That topology is sound, and it has one sharp edge: a worker, subagent or
scanner that resolves a path relative to its own ``cwd`` writes into whichever
tree it happens to be standing in. Ledgers are the part that does not forgive
this — `.jarvis/goal_reconciliation_ledger.jsonl` in one tree and the same file
in the other are different files with the same name, and a hash-chained,
MAC'd, append-only record split across two of them is not recoverable by
reading either.

## Why there is no new environment variable here

The obvious shape — export ``JARVIS_AUTHORITATIVE_WORKSPACE`` and compare
against it — would mint a SECOND declaration of a fact the codebase already
declares. ``autonomous_workspace.effective_execution_root`` is explicit that it
is "THE canonical execution-root seam" and that "duplicating this logic
anywhere else is a review-rejectable offense (Run-21 root cause was exactly
such a split-truth)".

So this module adds no truth. It asks the existing resolver and compares. When
the two trees disagree about which is authoritative, there is exactly one
answer to be wrong about instead of two — which is the whole point of a guard
against split state.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Ouroboros.WorkspaceGuard")

__all__ = [
    "WorkspaceVerdict",
    "authoritative_workspace",
    "workspace_verdict",
    "guard_enabled",
]

_ENV_ENABLED = "JARVIS_WORKSPACE_GUARD_ENABLED"


def guard_enabled() -> bool:
    """Default ON. It only ever REFUSES work that is already mis-rooted, and
    the failure it prevents (a ledger split across two checkouts) is not
    recoverable by reading either half."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


@dataclass(frozen=True)
class WorkspaceVerdict:
    """Whether this process may act on the tree it is standing in."""

    ok: bool
    reason: str
    authoritative: str = ""
    actual: str = ""

    @property
    def should_shed(self) -> bool:
        return not self.ok


def authoritative_workspace(project_root: Optional[Path] = None) -> Optional[Path]:
    """The tree mutation is supposed to happen in, or ``None`` if unknowable.

    Delegates to the canonical seam. ``None`` means "cannot establish", which
    the verdict below treats as "do not gate" — a guard that refuses work it
    cannot adjudicate is worse than the corruption it is guarding against,
    because it stops everything rather than the wrong thing.
    """
    try:
        from backend.core.ouroboros.governance.autonomous_workspace import (
            effective_execution_root,
        )
        root = project_root or Path.cwd()
        return Path(effective_execution_root(root)).resolve()
    except Exception:  # noqa: BLE001 — an unresolvable root gates nothing
        logger.debug("[WorkspaceGuard] could not resolve", exc_info=True)
        return None


def _same_tree(a: Path, b: Path) -> bool:
    """Whether two paths name the same checkout.

    Compared by RESOLVED path rather than by device+inode: the two trees here
    live on different filesystems (ext4 and DrvFS), so inode identity is
    meaningless across them, and a symlinked worktree legitimately resolves
    into its parent.
    """
    try:
        ra, rb = a.resolve(), b.resolve()
        return ra == rb or ra in rb.parents or rb in ra.parents
    except Exception:  # noqa: BLE001
        return False


def workspace_verdict(
    *,
    cwd: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> WorkspaceVerdict:
    """May the caller act here? NEVER raises.

    ``ok`` is True when the guard is off, when the authoritative tree cannot be
    established, or when *cwd* is inside it. It is False only when both are
    known AND they name different checkouts — the one case that corrupts a
    ledger.
    """
    try:
        if not guard_enabled():
            return WorkspaceVerdict(True, "workspace guard disabled")
        here = Path(cwd or Path.cwd()).resolve()
        auth = authoritative_workspace(project_root)
        if auth is None:
            return WorkspaceVerdict(
                True, "authoritative workspace unknown — not gating",
                actual=str(here),
            )
        if _same_tree(here, auth):
            return WorkspaceVerdict(
                True, "in the authoritative workspace",
                authoritative=str(auth), actual=str(here),
            )
        return WorkspaceVerdict(
            False,
            (
                f"WorkspaceBoundaryViolation: running in {here} but the "
                f"authoritative tree is {auth}. A ledger written here would "
                f"be a second, divergent copy of a hash-chained record."
            ),
            authoritative=str(auth), actual=str(here),
        )
    except Exception:  # noqa: BLE001 — a broken guard never blocks work
        logger.debug("[WorkspaceGuard] verdict degraded", exc_info=True)
        return WorkspaceVerdict(True, "guard degraded — not gating")
