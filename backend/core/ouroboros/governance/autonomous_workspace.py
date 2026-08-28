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

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Per-process cache: stable nonce per session_id so repeated workspace_branch()
# calls within ONE boot converge on a single branch (file + commit isolation),
# while different boots never collide.
_BRANCH_NONCE_CACHE: Dict[str, str] = {}


def compute_workspace_nonce(session_id: str, salt: str) -> str:
    """Task 3 — a 6-char SHA-256 hex of ``session_id:salt``. Collision-proof
    branch suffix that PRESERVES forensic evidence (it never resets or destroys
    a prior crashed run's branch -- it just guarantees a fresh, distinct one)."""
    return hashlib.sha256(
        ("%s:%s" % (session_id, salt)).encode("utf-8")
    ).hexdigest()[:6]


def _session_branch_nonce(session_id: str) -> str:
    nonce = _BRANCH_NONCE_CACHE.get(session_id)
    if nonce is None:
        nonce = compute_workspace_nonce(session_id, str(time.time()))
        _BRANCH_NONCE_CACHE[session_id] = nonce
    return nonce

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


class ExecutionRootInvalid(RuntimeError):
    """Raised when ``JARVIS_AUTO_COMMIT_WORKSPACE`` is SET but does not
    name a usable git work-area (Slice 11 review C2 — fail CLOSED).

    An armed-but-invalid workspace has no safe answer: honoring the raw
    path writes into a rogue tree; falling back writes into the
    operator's live checkout UNQUARANTINED (the Slice-56 leak class) —
    and a mid-session flip lets APPLY and VERIFY resolve different trees
    within one op. The only correct behavior is a loud typed refusal so
    the op fails instead of mutating either tree.
    """

    def __init__(self, override: str, reason: str) -> None:
        super().__init__(
            "JARVIS_AUTO_COMMIT_WORKSPACE=%r is armed but unusable (%s) — "
            "refusing to resolve an execution root (fail-closed; fix or "
            "unset the env)" % (override, reason)
        )
        self.override = override
        self.reason = reason


def effective_execution_root(project_root: Any) -> Path:
    """THE canonical execution-root seam (Slice 11 Task 1, mandate 1).

    Resolve the tree that mutation/judgment operates on — APPLY writes,
    scoped VERIFY, PatchBenchmarker, verify-gate rollback, AutoCommit —
    as distinct from the *observation* root (``GovernedLoopConfig.
    project_root``: sensors/TestWatcher/intake, which must keep watching
    the operator's real tree).

    Resolution is strictly presence + validity of
    ``JARVIS_AUTO_COMMIT_WORKSPACE`` (exported by the ledger-sovereignty
    bootloader, ``harness._boot_ledger_sovereignty_workspace``):

      * env absent/blank → ``project_root`` (byte-identical legacy)
      * env set to a real git work-area (a dir containing ``.git`` —
        FILE for linked worktrees, DIR for full checkouts) → that tree
      * env set to anything else → raise :class:`ExecutionRootInvalid`
        (review C2: a silent fallback routed APPLY bytes into the
        operator's live tree unquarantined; a raw honor wrote a rogue
        tree — an armed-but-broken workspace must fail LOUD, not pick
        a tree). The ``.git`` requirement is review C1: AutoCommitter
        always demanded it, so a plain dir made VERIFY judge one tree
        while commit fell back to another — the validity rule now lives
        HERE, once, for all four consumers.

    Pure read-time resolution — no caching, no polling. Single source of
    truth consumed by ``ChangeEngine._effective_write_root`` and
    ``AutoCommitter._effective_repo_root`` (delegates) and the
    ``execution_root`` dynamic properties on GovernedLoopConfig /
    OrchestratorConfig; duplicating this logic anywhere else is a
    review-rejectable offense (Run-21 root cause was exactly such a
    split-truth).
    """
    root = Path(project_root)
    override = (os.environ.get(_ENV_COMMIT_WORKSPACE) or "").strip()
    if not override:
        return root
    try:
        candidate = Path(override)
        if candidate.is_dir():
            if is_valid_git_work_area(candidate):
                return candidate
            raise ExecutionRootInvalid(override, "no .git in workspace dir")
        raise ExecutionRootInvalid(override, "not a directory")
    except ExecutionRootInvalid:
        raise
    except (OSError, ValueError) as exc:  # ENAMETOOLONG, NUL bytes, …
        raise ExecutionRootInvalid(override, repr(exc)) from exc


def is_valid_git_work_area(path: Any) -> bool:
    """THE C1 validity rule as a reusable predicate: a usable execution
    workspace is a directory containing ``.git`` (FILE for linked worktrees,
    DIR for full checkouts). Extracted for the ARMING layer (Slice 2 workspace-
    arming integrity, 2026-07-18): bt-2026-07-18-200502 proved a workspace can
    be reaped between arming and APPLY, leaving ``JARVIS_AUTO_COMMIT_WORKSPACE``
    pointing at a marker-only husk — every op then died fail-closed at the APPLY
    boundary. Armers MUST validate with this predicate (and clear the env when
    it fails) so "armed" always implies "usable"; the read side
    (:func:`effective_execution_root`) keeps failing LOUD as designed. NEVER
    raises — any probe fault is "not valid"."""
    try:
        p = Path(path)
        return p.is_dir() and (p / ".git").exists()
    except Exception:  # noqa: BLE001 — defensive
        return False


def workspace_branch(session_id: str) -> str:
    """Quarantine branch name. Intentionally identical to the
    Ledger-Sovereignty commit-workspace branch so file + commit isolation
    converge on ONE worktree, swept by the existing ``ouroboros/auto/*``
    reaper. A per-boot cryptographic nonce (Task 3) makes the name collision-
    proof across runs without resetting a prior crashed run's branch."""
    return "ouroboros/auto/%s-%s" % (session_id, _session_branch_nonce(session_id))


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
    # A create that RETURNS is not proof the workspace is usable, and this is
    # the seam where that matters most: the comment below calls this the SINGLE
    # canonical materialization point for the env, and the Ledger-Sovereignty
    # boot phase explicitly "reuses whatever lands here". Whatever lands here
    # therefore becomes every consumer's execution root.
    #
    # Measured twice on this host (bt-2026-08-28-061124, bt-2026-08-28-065825):
    # `create` returned `.worktrees/ouroboros__auto__<session>`, a directory
    # holding only `.jarvis/` and no `.git`, which `git worktree list` never
    # knew about. `effective_execution_root` (line ~185, this module) then
    # raises `ExecutionRootInvalid` by design — and it raises at the APPLY
    # boundary, so it killed the ops that had travelled FURTHEST: through
    # GENERATE on the local lane, VALIDATE, GATE and REVIEW-SHADOW. 8 of 80,
    # then 5 of 77.
    #
    # `is_valid_git_work_area` (defined above in this module) states the rule:
    # "Armers MUST validate with this predicate ... so 'armed' always implies
    # 'usable'". Arming was the only step that skipped it, which left the read
    # side failing loud about a promise the write side never checked.
    #
    # `return root` on failure is this function's OWN established fail-safe —
    # the same move the `except` above makes. Staying in the primary checkout
    # is a documented posture; pointing every consumer at a husk is not.
    if not is_valid_git_work_area(wt_path):
        logger.warning(
            "[FileIsolation] worktree create returned %s but it is not a "
            "usable git work-area (no .git) — staying in the primary checkout "
            "%s rather than arming an unusable execution root "
            "(session=%s branch=%s)",
            wt_path, root, session_id, workspace_branch(session_id),
        )
        return root
    # Unify with the EXISTING commit-workspace handoff idiom (the same env
    # the Ledger-Sovereignty phase sets) so AutoCommitter + ChangeEngine +
    # the orchestrator all converge on this one worktree. This is the
    # established workspace-handoff env, NOT process-cwd mutation.
    #
    # This is the SINGLE canonical materialization seam for
    # JARVIS_AUTO_COMMIT_WORKSPACE (the Ledger-Sovereignty boot phase in
    # harness.py reuses whatever lands here via its own already-set check).
    # setdefault, NOT unconditional assignment: an operator-pinned workspace
    # must survive this call untouched (durability-substrate Task 3).
    os.environ.setdefault(_ENV_COMMIT_WORKSPACE, str(wt_path))
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
