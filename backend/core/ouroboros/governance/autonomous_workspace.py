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

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")
_ENV_FILE_ISOLATION = "JARVIS_FILE_ISOLATION_ENABLED"
_ENV_COMMIT_WORKSPACE = "JARVIS_AUTO_COMMIT_WORKSPACE"
_ENV_SESSION_ID = "JARVIS_OUROBOROS_SESSION_ID"


def file_isolation_enabled() -> bool:
    """Master flag — ``JARVIS_FILE_ISOLATION_ENABLED`` (default false).
    Off → :func:`resolve_loop_project_root` is a pure pass-through (the
    boot is byte-identical)."""
    return os.environ.get(_ENV_FILE_ISOLATION, "").strip().lower() in _TRUTHY


def _deterministic_lock_enabled() -> bool:
    """LR-A gate — ``JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED`` (default
    TRUE). Off → :func:`resolve_loop_project_root` reverts to pure legacy
    flag-driven behavior (no forced arming)."""
    import os
    return (
        os.environ.get("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
        or ""
    ).strip().lower() in _TRUTHY


def _deterministic_force(
    root: Any,
    is_primary: bool,
    container: bool,
    autonomous: bool,
) -> bool:
    """LR-A trigger: force isolation iff lock on AND in primary checkout AND
    not a container AND autonomous (no operator present). Pure. Never
    raises."""
    try:
        return bool(
            _deterministic_lock_enabled()
            and is_primary
            and (not container)
            and autonomous
        )
    except Exception:  # noqa: BLE001
        return False


def _arm_boundary_flags() -> None:
    """LR-A: force-arm BOTH flags as a pair, in-process, so downstream Stage A
    (commit denial) and Stage B (isolation) both read armed. Never raises."""
    import os
    try:
        os.environ["JARVIS_FILE_ISOLATION_ENABLED"] = "true"
        os.environ["JARVIS_EXECUTION_BOUNDARY_ENABLED"] = "true"
    except Exception:  # noqa: BLE001
        pass


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
    # LR-A deterministic isolation lock: when O+V boots in the PRIMARY
    # checkout, autonomous (no operator present), and NOT a container, FORCE
    # isolation by arming BOTH boundary flags as a pair and falling through to
    # worktree routing — EVEN WHEN those flags were explicitly false. The
    # autonomy check below re-confirms (and will pass, since we only force when
    # autonomous), so routing proceeds. Off / not-this-situation → byte-
    # identical legacy behavior. Fail-soft: any failure here leaves the legacy
    # early-return intact.
    try:
        from backend.core.ouroboros.governance.execution_context import (
            is_primary_checkout,
            is_autonomous,
            _is_cloud_container,
        )
        _forced = _deterministic_force(
            root,
            is_primary=bool(is_primary_checkout(root)),
            container=bool(_is_cloud_container()),
            autonomous=bool(is_autonomous(root)),
        )
    except Exception:  # noqa: BLE001 — can't prove force → legacy path
        _forced = False
    if _forced:
        _arm_boundary_flags()
        logger.warning(
            "[DeterministicLock] forced isolation+boundary despite env "
            "(primary checkout, autonomous) root=%s session=%s",
            root,
            session_id,
        )
        # DO NOT early-return — fall through to the (now-armed) worktree
        # routing below.
    elif not file_isolation_enabled():
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
    # §7 absolute observability — emit a grep-stable marker so a soak can
    # verify the redirect fired and identify the quarantine zone.
    logger.info(
        "[FileIsolation] routed project_root -> %s "
        "(session=%s branch=%s)",
        wt_path, session_id, workspace_branch(session_id),
    )
    return wt_path


__all__ = [
    "file_isolation_enabled",
    "resolve_loop_project_root",
    "workspace_branch",
]
