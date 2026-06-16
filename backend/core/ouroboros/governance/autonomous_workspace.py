"""Sovereign Execution Boundary (Stage B) — autonomous file isolation.

Stage A (the OCA Iron Gate) blocks the autonomous loop from *committing* in
the operator's primary checkout. Stage B closes the remaining *file-mutation*
vector: it routes the loop's GENERATE/VALIDATE/APPLY/branch-ops into an
isolated worktree so the loop never touches the operator's working tree at all.

## How (zero global cwd state, no os.chdir)

The orchestrator and all four mutation delegates — ChangeEngine (file writes),
BranchManager (`git checkout -b`), TestRunner (pytest subprocess), ToolExecutor
(Venom bash) — derive their working dir from a `project_root` passed in at
construction. So a SINGLE dynamic redirect of the orchestrator's `project_root`
(at config-construction time in the harness boot) carries to every mutation.
This module computes that root.

:func:`resolve_loop_project_root` returns:
  * ``repo_root`` unchanged when file isolation is off (default) OR the session
    is not autonomous → byte-identical boot, human sessions use the primary
    checkout.
  * an isolated worktree path when file isolation is on AND the session is
    autonomous. The worktree uses the SAME ``ouroboros/auto/<session>`` naming
    as the Ledger-Sovereignty commit-workspace phase, so the two converge on
    ONE worktree and the existing ``WorktreeManager.reap_orphans`` (which
    already sweeps ``ouroboros/auto/*``) flushes orphaned quarantine zones on
    the next boot.

Authority posture: gated by ``JARVIS_FILE_ISOLATION_ENABLED`` (default off),
composes with the Stage A boundary, NEVER raises (falls back to ``repo_root``
— and the Stage A commit-gate still blocks any autonomous commit there, so a
fallback can't silently corrupt the operator's tree).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

_TRUTHY = ("1", "true", "yes", "on")
_ENV_FILE_ISOLATION = "JARVIS_FILE_ISOLATION_ENABLED"
_ENV_COMMIT_WORKSPACE = "JARVIS_AUTO_COMMIT_WORKSPACE"
_ENV_SESSION_ID = "JARVIS_OUROBOROS_SESSION_ID"


def file_isolation_enabled() -> bool:
    """Master flag — ``JARVIS_FILE_ISOLATION_ENABLED`` (default false).
    Off → :func:`resolve_loop_project_root` is a pure pass-through (the
    boot is byte-identical)."""
    return os.environ.get(_ENV_FILE_ISOLATION, "").strip().lower() in _TRUTHY


def workspace_branch(session_id: str) -> str:
    """Quarantine branch name. Intentionally identical to the
    Ledger-Sovereignty commit-workspace branch so file + commit isolation
    converge on ONE worktree, swept by the existing ``ouroboros/auto/*``
    reaper."""
    return f"ouroboros/auto/{session_id}"


async def resolve_loop_project_root(
    repo_root: Any,
    *,
    session_id: str,
    worktree_manager: Optional[Any] = None,
) -> Path:
    """Resolve the orchestrator's effective ``project_root`` for this
    session (see module docstring). NEVER raises — any failure falls back
    to ``repo_root``."""
    root = Path(repo_root)
    if not file_isolation_enabled():
        return root
    # Cryptographic autonomy check (Stage A primitive) — human sessions
    # keep the primary checkout.
    try:
        from backend.core.ouroboros.governance import execution_context as ec
        if not ec.is_autonomous(root):
            return root
    except Exception:  # noqa: BLE001 — can't prove human → be conservative
        pass
    try:
        mgr = worktree_manager
        if mgr is None:
            from backend.core.ouroboros.governance.worktree_manager import (
                WorktreeManager,
            )
            mgr = WorktreeManager(repo_root=root)
        wt_path = Path(await mgr.create(workspace_branch(session_id)))
    except Exception:  # noqa: BLE001 — fail-safe: stay in primary
        return root
    # Unify with the EXISTING commit-workspace handoff idiom (the same env
    # the Ledger-Sovereignty phase sets) so AutoCommitter + ChangeEngine +
    # the orchestrator all converge on this one worktree. This is the
    # established workspace-handoff env, NOT process-cwd mutation.
    os.environ[_ENV_COMMIT_WORKSPACE] = str(wt_path)
    os.environ.setdefault(_ENV_SESSION_ID, str(session_id))
    return wt_path


__all__ = [
    "file_isolation_enabled",
    "resolve_loop_project_root",
    "workspace_branch",
]
