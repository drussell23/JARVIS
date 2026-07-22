# backend/core/ouroboros/governance/workspace_promoter.py
"""
WorkspacePromoter — governed landing of verified workspace commits (Slice 11).

Closes the Run-21 promotion gap: with Ledger Sovereignty armed, APPLY writes
and AutoCommit land in the session workspace worktree (Slice 56 alignment),
and — before this module — nothing ever moved them to the operator's tree;
TestWatcher kept polling an unfixed tree forever ("autonomous commits stay
quarantined by design", worktree_manager.py).

This is the POLICY half; the git mechanism is Task 3's
``WorktreeManager.promote_commits`` (async, fail-closed, non-destructive).
Composition per the operator-locked mandates:

* Integrated into the EXISTING AutoCommit 8b terminal sequence (both the
  live Slice4bRunner and the inline orchestrator twin) — not a new phase.
* LiveWork consult REUSES ``Orchestrator._live_work_apply_gate`` (the
  promotion target is the operator's tree — this is the gate's natural
  home; scanning happens against the same root the gate always scanned).
* Drift re-check REUSES ``state_drift.should_block_apply`` with the op's
  GENERATE-time hashes against the TARGET tree: operator commits made since
  the model read those files refuse promotion even when git would merge
  cleanly (semantic drift beats textual mergeability).
* Fail-closed everywhere: any refusal/failure leaves the workspace branch
  untouched as the quarantined reviewable artifact and the op rides the
  existing POSTMORTEM fail path (``failure_class='promotion'`` semantics at
  the call sites).

Master: ``JARVIS_WORKSPACE_PROMOTION_ENABLED`` (default **false** — the
production posture stays quarantine + Orange PR; the A1 driver opts in).
Knob: ``JARVIS_PROMOTION_LIVE_WORK_CONSULT`` (default true).

All git flows through WorktreeManager — this module composes; it never
spawns processes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from backend.core.ouroboros.governance.worktree_manager import (
    PromotionError,
    WorktreeManager,
)

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def promotion_enabled() -> bool:
    """Master flag — ``JARVIS_WORKSPACE_PROMOTION_ENABLED`` (default false)."""
    return os.environ.get(
        "JARVIS_WORKSPACE_PROMOTION_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _consult_enabled() -> bool:
    return os.environ.get(
        "JARVIS_PROMOTION_LIVE_WORK_CONSULT", "true",
    ).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class PromotionOutcome:
    """What the promoter did (or declined to do) for one op.

    ``attempted=False`` states (``disabled``, ``noop_same_root``) mean the
    op proceeds exactly as pre-Slice-11. ``attempted=True, promoted=False``
    states are fail-closed refusals the caller must surface as op failure.
    """

    attempted: bool
    promoted: bool
    state: str
    shas: Tuple[str, ...] = ()
    detail: str = ""


async def _derive_operator_root(
    mgr: "WorktreeManager", exec_root: Path,
) -> Optional[Path]:
    """The operator checkout that owns ``exec_root``, from git topology.

    A linked worktree's ``git rev-parse --git-common-dir`` resolves to
    ``<operator>/.git`` (the shared object store lives under the primary
    checkout); its parent IS the operator root. A primary checkout's
    common dir is its own ``.git`` — the caller treats that as
    "genuinely same root". Read-time, env-free, hardcode-free. Returns
    ``None`` on any git failure (fail-soft — caller falls back to the
    legacy noop). NEVER raises.
    """
    try:
        rc, out, _err = await mgr._run_git_rc(
            exec_root, ["rev-parse", "--git-common-dir"],
        )
        common = (out or "").strip()
        if rc != 0 or not common:
            return None
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (exec_root / common_path)
        common_path = Path(os.path.realpath(common_path))
        # <operator>/.git → operator root is its parent. Defensive: a
        # bare/odd layout that doesn't end in .git is not derivable.
        if common_path.name != ".git":
            return None
        return common_path.parent
    except Exception:  # noqa: BLE001 — derivation is fail-soft
        logger.debug(
            "[WorkspacePromoter] operator-root derivation failed for %s",
            exec_root, exc_info=True,
        )
        return None


async def run_workspace_promotion(
    orch: Any,
    ctx: Any,
    committed_hash: Optional[str],
    best_candidate: Optional[dict],
    *,
    manager: Optional[WorktreeManager] = None,
    commit_skipped_reason: Optional[str] = None,
) -> PromotionOutcome:
    """Promote ``committed_hash`` from the workspace branch onto the
    operator tree, under governance. Never raises — returns a typed
    outcome; the call sites translate refusals into the existing fail path.
    """
    if not promotion_enabled():
        return PromotionOutcome(False, False, "disabled")

    project_root = Path(orch._config.project_root)
    exec_root = Path(orch._config.execution_root)

    mgr = manager if manager is not None else WorktreeManager(
        repo_root=project_root,
    )

    if exec_root == Path(os.path.realpath(project_root)) or (
        exec_root == project_root
    ):
        # Isolation-collapsed posture (2026-07-22, soak
        # bt-2026-07-22-042548): FileIsolation
        # (autonomous_workspace.resolve_loop_project_root) routes the
        # LOOP's project_root INTO the sovereignty worktree at arming,
        # so config carries worktree == worktree and the OPERATOR tree
        # vanishes from every config field — the old "noop_same_root"
        # early-return made promotion structurally unreachable (it
        # silently skipped on every op of every isolation-armed soak).
        # Git itself still knows the operator checkout: a linked
        # worktree's ``--git-common-dir`` lives under it. Derive the
        # TRUE promotion target from git topology at read time —
        # env-free, hardcode-free, and inert for a genuinely-primary
        # checkout (whose common dir is its own ``.git``).
        _derived = await _derive_operator_root(mgr, exec_root)
        if _derived is None or _derived == Path(
            os.path.realpath(exec_root)
        ):
            # Genuinely the primary checkout — commit already lives on
            # the operator tree; nothing to promote.
            return PromotionOutcome(False, False, "noop_same_root")
        logger.info(
            "[WorkspacePromoter] isolation-collapsed config detected — "
            "derived operator root %s from worktree git topology "
            "(exec_root=%s)",
            _derived, exec_root,
        )
        project_root = _derived

    op_id = getattr(ctx, "op_id", "")
    comm = getattr(getattr(orch, "_stack", None), "comm", None)

    async def _fail(state: str, detail: str = "") -> PromotionOutcome:
        logger.warning(
            "[WorkspacePromoter] op=%s promotion refused/failed: %s %s",
            op_id, state, detail[:200],
        )
        if comm is not None:
            try:
                await comm.emit_decision(
                    op_id=op_id, outcome="promotion_failed",
                    reason_code=state,
                    target_files=list(getattr(ctx, "target_files", ())),
                )
            except Exception:  # noqa: BLE001 — telemetry must not mask state
                pass
        return PromotionOutcome(True, False, state, detail=detail)

    if not committed_hash:
        # Review P3: classify AutoCommit's documented NON-FATAL outcomes
        # instead of blanket-failing verified-green ops. 'nothing_to_stage'
        # means the op produced NO net diff — there is genuinely nothing to
        # land; failing it would publish a successful no-op as FAILED. Every
        # other missing-hash case (transient git lock, fenced generation,
        # commit exception) stays fail-closed: with promotion armed, landing
        # IS part of the op's contract, and the skip reason is surfaced
        # verbatim for diagnosability.
        if (commit_skipped_reason or "").strip() == "nothing_to_stage":
            logger.info(
                "[WorkspacePromoter] op=%s no net diff to promote "
                "(nothing_to_stage) — no-op, op proceeds", op_id,
            )
            return PromotionOutcome(False, False, "no_change_to_promote")
        return await _fail(
            "no_commit",
            "workspace promotion enabled but AutoCommit produced no hash "
            "(skipped_reason=%r)" % (commit_skipped_reason or ""),
        )

    # ---- LiveWork consult (reused gate; the target IS the tree it scans) --
    # Review P5: a DEDICATED small wait budget — at ~98% op progress the
    # pipeline deadline is spent, so the gate's default budgeting would turn
    # any momentary human edit into an instant wait-infeasible kill of a
    # verified+committed op; and a deadline-less ctx would grant a second
    # full FILE_LOCK_TTL wait holding the worker.
    if _consult_enabled():
        try:
            _consult_budget_s = float(os.environ.get(
                "JARVIS_PROMOTION_LIVE_WORK_MAX_WAIT_S", "30",
            ))
        except ValueError:
            _consult_budget_s = 30.0
        try:
            _gate_res = await orch._live_work_apply_gate(
                ctx, best_candidate,
                max_wait_override_s=_consult_budget_s,
                # Consult contract: "the target IS the tree it scans" —
                # the DERIVED operator root, never the collapsed config
                # root (which is the worktree the op itself just wrote;
                # soak bt-2026-07-22-050025 refused its own promotion
                # as live_work_active on its own 33s-old APPLY write).
                scan_root_override=project_root,
            )
            if getattr(_gate_res, "active_hit", None):
                return await _fail(
                    "live_work_active",
                    "human activity on the target tree at promotion time: %r"
                    % (getattr(_gate_res, "active_hit", None),),
                )
        except Exception as exc:  # noqa: BLE001 — fail CLOSED at promotion
            return await _fail(
                "live_work_gate_error",
                "%s: %s" % (type(exc).__name__, exc),
            )

    # ---- GENERATE-time hash drift vs the TARGET tree (reused checker) -----
    _hashes = getattr(ctx, "generate_file_hashes", None) or {}
    if _hashes:
        try:
            from backend.core.ouroboros.governance.state_drift import (
                should_block_apply,
            )
            _blocked, _stale = should_block_apply(_hashes, project_root)
        except Exception as exc:  # noqa: BLE001 — fail CLOSED at promotion
            return await _fail(
                "drift_check_error", "%s: %s" % (type(exc).__name__, exc),
            )
        if _blocked:
            return await _fail(
                "target_drift",
                "operator tree drifted from GENERATE-time hashes: %s"
                % list(_stale)[:3],
            )

    # (mgr constructed above, before the operator-root derivation.)

    # Branch discovery from the workspace checkout itself — self-contained,
    # no coupling to session-id/nonce derivation.
    rc, _branch_out, _err = await mgr._run_git_rc(
        exec_root, ["rev-parse", "--abbrev-ref", "HEAD"],
    )
    _branch = _branch_out.strip()
    if rc != 0 or not _branch or _branch == "HEAD":
        return await _fail(
            "branch_missing",
            "cannot resolve workspace branch at %s: %s"
            % (exec_root, _err.strip()[:200]),
        )

    # Slice 12: forward the op's GENERATE-time baselines — the SAME object
    # the drift check consumed — so the dirty-target exemption can prove
    # (sha256, state_drift.file_sha256) that target dirt is the defect
    # state this repair supersedes. No second baseline source exists.
    _baseline_map: dict = {}
    for _entry in (getattr(ctx, "generate_file_hashes", None) or ()):
        try:
            _rel, _hash = _entry
        except (TypeError, ValueError):
            continue
        if _rel and _hash:
            _baseline_map[str(_rel)] = str(_hash)

    try:
        result = await mgr.promote_commits(
            project_root, _branch, [committed_hash],
            baseline_hashes=_baseline_map,
        )
    except PromotionError as exc:
        return await _fail(exc.state, exc.detail)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED, never crash 8b
        return await _fail(
            "git_failure", "%s: %s" % (type(exc).__name__, exc),
        )

    # Review P4: surface the LANDED shas (cherry-pick creates NEW commit
    # objects on the operator branch; workspace shas don't exist there).
    _landed = result.landed_shas or result.promoted_shas
    logger.info(
        "[WorkspacePromoter] op=%s PROMOTED %s -> %s (mode=%s landed=%s)",
        op_id, committed_hash[:12], project_root, result.mode,
        [s[:12] for s in _landed],
    )
    if comm is not None:
        try:
            await comm.emit_decision(
                op_id=op_id, outcome="promoted",
                reason_code="workspace_promotion",
                diff_summary="promoted %s (%s) onto %s as %s"
                % (committed_hash[:12], result.mode, project_root,
                   ",".join(s[:12] for s in _landed)),
                target_files=list(getattr(ctx, "target_files", ())),
            )
        except Exception:  # noqa: BLE001
            pass
    return PromotionOutcome(
        True, True, "promoted", shas=_landed,
    )
