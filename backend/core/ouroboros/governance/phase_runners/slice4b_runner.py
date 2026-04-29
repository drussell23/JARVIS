"""Slice4bRunner — APPROVE + APPLY + VERIFY as one combined runner.

Wave 2 item (5) Slice 4b. Extracts orchestrator.py lines ~6141-7293
(~1152 lines spanning APPROVE, APPLY with 7.5 INFRA, and VERIFY with
8a scoped tests, 8b auto-commit, 8b2 hot-reload, 8c self-critique,
8d visual VERIFY) into a single :class:`PhaseRunner` behind
``JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** Verbatim transcription with
``self.`` → ``orch.`` substitutions.

## Why a single combined runner for 4b

Mirror of Slice 3's combined-gate pattern. The three phases are deeply
interleaved:

* APPROVE's tail (pre-APPLY narrator + cancel-check + DRY_RUN gate)
  runs on every path, not just APPROVAL_REQUIRED
* APPLY consumes APPROVE's local state (``best_candidate``,
  ``_checkpoint``, ``_ckpt_mgr``, ``_t_apply``)
* VERIFY consumes APPLY's local state (``_t_apply``, ``_checkpoint``,
  ``_verify_test_*``, ``_committed_hash``)

Separate runners would require 6-way artifact threading; one combined
runner preserves inline semantics with a single flag and a single
reindent. Per-phase decomposition arrives with Slice 6 dispatcher
cutover.

## ~14 terminal exit paths

APPROVE:
* ``pending_pr_review`` — Orange PR async review path (opt-in)
* ``approval_required_but_no_provider``
* ``approval_expired``
* ``approval_rejected`` — with session lesson + NegativeConstraintStore +
  UserPreferenceStore persistence
* ``user_cancelled`` — pre-APPLY cooperative cancel
* ``dry_run_session`` — JARVIS_DRY_RUN kill switch

APPLY:
* ``change_engine_error`` (multi-file or single-file)
* ``change_engine_failed`` (result.success=False, with rollback)
* ``infrastructure_failed`` — Phase 7.5 INFRA hook failure
* ``human_active_on_target`` — LiveWorkSensor defer
* Cross-repo saga via ``_execute_saga_apply`` (terminal)

VERIFY:
* ``verify_regression`` — scoped-test + benchmark gate + rollback +
  checkpoint restore (POSTMORTEM)
* L2 cancel/fatal during VERIFY → terminal (POSTMORTEM)
* Visual VERIFY L2 cancel/fatal → terminal

## Success path

``next_phase = COMPLETE`` with ``t_apply`` in ``artifacts`` (COMPLETERunner
consumes it for canary latency calculation).

## Cross-phase artifact — ``t_apply``

Recorded at APPLY start via ``time.monotonic()``. COMPLETE uses it to
compute operation latency. Threaded via ``PhaseResult.artifacts["t_apply"]``.

## Dependencies injected via constructor

* ``orchestrator`` — reads _many_ attributes (_stack.*, _config.*,
  _approval_provider, _pre_action_narrator, _is_cancel_requested,
  _iter_candidate_files, _materialize_execution_graph_candidate,
  _execute_saga_apply, _build_change_request, _apply_multi_file_candidate,
  _record_ledger, _record_canary_for_ctx, _publish_outcome,
  _add_session_lesson, _infra_applicator, _hot_reloader, _critique_engine,
  _validation_runner, _config.repair_engine, _l2_hook, _run_benchmark)
* ``serpent`` — pipeline serpent handle (optional)
* ``best_candidate`` — from VALIDATE/GATE artifacts
* ``risk_tier`` — from GATE artifacts (post-mutations)

## Authority invariant

Runner imports match inline block's surface. No
``candidate_generator`` / ``iron_gate`` / ``change_engine`` (latter
accessed via ``orch._stack.change_engine`` — same as inline).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.test_runner import BlockedPathError

if TYPE_CHECKING:  # pragma: no cover
    from backend.core.ouroboros.governance.orchestrator import Orchestrator


logger = logging.getLogger("Ouroboros.Orchestrator")

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class Slice4bRunner(PhaseRunner):
    """Combined APPROVE + APPLY + VERIFY verbatim runner (~1150 lines)."""

    # Phase attribute represents the ENTRY phase (APPROVE).
    # Runner spans APPROVE → APPLY → VERIFY → (COMPLETE advance handled
    # by COMPLETERunner at orchestrator end).
    phase = OperationPhase.APPROVE

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
        best_candidate: Optional[Any],
        risk_tier: RiskTier,
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent
        self._best_candidate = best_candidate
        self._risk_tier = risk_tier

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent
        best_candidate = self._best_candidate
        risk_tier = self._risk_tier

        # t_apply captured at APPLY start (used by COMPLETERunner for canary latency)
        _t_apply: float = 0.0

        # ---- VERBATIM transcription of orchestrator.py 6141-7293 ----

        # ==================== Phase 6: APPROVE (conditional) ====================
        if risk_tier is RiskTier.APPROVAL_REQUIRED:
            try:
                from backend.core.ouroboros.governance.orange_pr_reviewer import (
                    OrangePRReviewer,
                    is_orange_pr_enabled,
                )
                _orange_pr_on = is_orange_pr_enabled()
            except Exception:
                _orange_pr_on = False

            if _orange_pr_on:
                try:
                    _files_for_pr = orch._iter_candidate_files(best_candidate)
                    _reviewer = OrangePRReviewer(orch._config.project_root)
                    _pr_result = await _reviewer.create_review_pr(
                        op_id=ctx.op_id,
                        description=ctx.description,
                        files=_files_for_pr,
                        evidence={
                            "risk_tier": risk_tier.name,
                            "target_files": list(ctx.target_files),
                            "file_count": len(_files_for_pr),
                        },
                        risk_tier_name=risk_tier.name,
                    )
                except Exception:
                    logger.exception(
                        "[Orchestrator] Orange PR reviewer raised for op=%s; "
                        "falling back to CLI approval",
                        ctx.op_id,
                    )
                    _pr_result = None

                if _pr_result is not None:
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="pending_pr_review",
                    )
                    await orch._record_ledger(
                        ctx,
                        OperationState.GATING,
                        {
                            "event": "orange_pr_created",
                            "pr_url": _pr_result.url,
                            "branch": _pr_result.branch,
                            "base_branch": _pr_result.base_branch,
                            "risk_tier": risk_tier.name,
                        },
                    )
                    logger.info(
                        "[Orchestrator] op=%s handed off to async PR review: %s",
                        ctx.op_id, _pr_result.url,
                    )
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                        reason="pending_pr_review",
                        artifacts={"t_apply": _t_apply},
                    )
                logger.warning(
                    "[Orchestrator] op=%s Orange PR creation failed; "
                    "using CLI approval fallback",
                    ctx.op_id,
                )

            if orch._approval_provider is None:
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="approval_required_but_no_provider",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "approval_required_but_no_provider"},
                )
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="approval_required_but_no_provider",
                    artifacts={"t_apply": _t_apply},
                )

            ctx = ctx.advance(OperationPhase.APPROVE)
            await orch._record_ledger(
                ctx,
                OperationState.GATING,
                {"waiting_approval": True, "risk_tier": risk_tier.name},
            )

            try:
                await orch._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id,
                    phase="approve",
                    progress_pct=0.0,
                )
            except Exception:
                logger.debug(
                    "Comm heartbeat failed for op=%s", ctx.op_id, exc_info=True
                )

            request_id = await orch._approval_provider.request(ctx)
            decision: ApprovalResult = await orch._approval_provider.await_decision(
                request_id, orch._config.approval_timeout_s
            )

            if decision.status is ApprovalStatus.EXPIRED:
                ctx = ctx.advance(
                    OperationPhase.EXPIRED,
                    terminal_reason_code="approval_expired",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "approval_expired"},
                )
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="approval_expired",
                    artifacts={"t_apply": _t_apply},
                )

            if decision.status is ApprovalStatus.REJECTED:
                _reject_reason = getattr(decision, "reason", "") or ""
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="approval_rejected",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "approval_rejected",
                        "approver": decision.approver,
                        "rejection_reason": _reject_reason,
                    },
                )

                _files_short = ", ".join(
                    p.rsplit("/", 1)[-1] for p in ctx.target_files[:3]
                )
                _reason_tag = _reject_reason[:80] if _reject_reason else "no reason given"
                orch._add_session_lesson(
                    "code",
                    f"[REJECTED] {ctx.description[:60]} ({_files_short}) "
                    f"— human rejected: {_reason_tag}. "
                    f"Avoid this approach in future operations.",
                    op_id=ctx.op_id,
                )

                if _reject_reason:
                    try:
                        from backend.core.ouroboros.governance.self_evolution import (
                            NegativeConstraintStore,
                        )
                        from backend.core.ouroboros.governance.entropy_calculator import (
                            extract_domain_key as _rej_edk,
                        )
                        _rej_domain = _rej_edk(ctx.target_files, ctx.description)
                        _ns = NegativeConstraintStore()
                        _ns.add_constraint(
                            _rej_domain,
                            f"Human rejected: {_reject_reason[:120]}",
                            f"Op {ctx.op_id} on {_files_short} was rejected at Iron Gate",
                            source_op_id=ctx.op_id,
                            severity="hard",
                        )
                    except Exception:
                        pass

                if _reject_reason:
                    try:
                        from backend.core.ouroboros.governance.user_preference_memory import (
                            get_default_store,
                        )
                        get_default_store().record_approval_rejection(
                            op_id=ctx.op_id,
                            description=ctx.description,
                            target_files=list(ctx.target_files),
                            reason=_reject_reason,
                            approver=getattr(decision, "approver", "human") or "human",
                        )
                    except Exception:
                        pass

                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="approval_rejected",
                    artifacts={"t_apply": _t_apply},
                )
            # APPROVED -- continue to APPLY

        # ── PreActionNarrator: voice WHAT before APPLY ──
        if orch._pre_action_narrator is not None:
            try:
                _tf = list(ctx.target_files)[0] if ctx.target_files else "unknown"
                await orch._pre_action_narrator.narrate_phase("APPLY", {"target_file": _tf})
            except Exception:
                pass

        # ── Cooperative cancellation check (pre-APPLY) ──
        if orch._is_cancel_requested(ctx.op_id):
            ctx = ctx.advance(OperationPhase.CANCELLED, terminal_reason_code="user_cancelled")
            await orch._record_ledger(ctx, OperationState.FAILED, {"reason": "user_cancelled"})
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason="user_cancelled",
                artifacts={"t_apply": _t_apply},
            )

        # ── Session-scoped dry-run gate ──
        if os.environ.get("JARVIS_DRY_RUN", "").strip().lower() in _TRUTHY:
            logger.info(
                "[Orchestrator] DRY_RUN: op=%s would APPLY %d file(s) — "
                "skipping disk writes (set JARVIS_DRY_RUN=0 or /plan off)",
                ctx.op_id,
                len(ctx.target_files) if ctx.target_files else 0,
            )
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                terminal_reason_code="dry_run_session",
            )
            await orch._record_ledger(
                ctx, OperationState.FAILED,
                {"reason": "dry_run_session"},
            )
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason="dry_run_session",
                artifacts={"t_apply": _t_apply},
            )

        # ==================== Phase 7: APPLY ====================
        ctx = ctx.advance(OperationPhase.APPLY)

        # ── Pre-APPLY git checkpoint (Manifesto §6: Iron Gate) ──
        _checkpoint = None
        _ckpt_mgr = None
        try:
            from backend.core.ouroboros.governance.workspace_checkpoint import WorkspaceCheckpointManager
            _ckpt_mgr = WorkspaceCheckpointManager(orch._config.project_root)
            _checkpoint = await _ckpt_mgr.create_checkpoint(
                ctx.op_id, f"pre-apply: {ctx.description[:80]}"
            )
        except Exception:
            logger.debug("[Orchestrator] Pre-APPLY checkpoint skipped", exc_info=True)

        # Heartbeat: APPLY phase starting (Manifesto §7)
        try:
            _apply_target = list(ctx.target_files)[0] if ctx.target_files else ""
            await orch._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="APPLY", progress_pct=80.0,
                target_file=_apply_target,
            )
        except Exception:
            pass

        # Deploy gate: canary preflight
        try:
            from backend.core.ouroboros.governance.deploy_gate import DeployGate
            _canary = getattr(orch._stack, "canary_controller", None)
            if _canary is not None:
                _gate = DeployGate(canary=_canary)
                _preflight = _gate.preflight(
                    service=ctx.primary_repo,
                    target_files=list(ctx.target_files),
                )
                if not _preflight.passed:
                    logger.warning(
                        "[Orchestrator] DeployGate preflight FAILED: %s [%s]",
                        _preflight.reason, ctx.op_id,
                    )
        except Exception:
            logger.debug("[Orchestrator] DeployGate not available", exc_info=True)

        # Cross-repo saga path
        if ctx.cross_repo:
            if "execution_graph" in best_candidate:
                ctx, best_candidate = await orch._materialize_execution_graph_candidate(
                    ctx,
                    best_candidate,
                )
            _saga_ctx = await orch._execute_saga_apply(ctx, best_candidate)
            # Saga handles its own terminal advance; return whatever it produced.
            return PhaseResult(
                next_ctx=_saga_ctx,
                next_phase=(None if _saga_ctx.phase in (
                    OperationPhase.COMPLETE, OperationPhase.CANCELLED,
                    OperationPhase.EXPIRED, OperationPhase.POSTMORTEM,
                ) else OperationPhase.COMPLETE),
                status="ok" if _saga_ctx.phase is OperationPhase.COMPLETE else "fail",
                reason="saga_applied",
                artifacts={"t_apply": _t_apply},
            )

        # ── Stale-exploration guard ──
        _stale_files: list = []
        if ctx.generate_file_hashes:
            for _ghf, _ghash in ctx.generate_file_hashes:
                if not _ghash:
                    continue
                _ghf_path = orch._config.project_root / _ghf
                try:
                    _now_hash = hashlib.sha256(_ghf_path.read_bytes()).hexdigest()
                except (OSError, IOError):
                    continue
                if _now_hash != _ghash:
                    _stale_files.append(_ghf)
            if _stale_files:
                logger.warning(
                    "[Orchestrator] Stale-exploration: %d file(s) changed between GENERATE and APPLY: %s [%s]",
                    len(_stale_files), _stale_files[:3], ctx.op_id[:12],
                )
                await orch._record_ledger(ctx, OperationState.APPLYING, {
                    "event": "stale_exploration_detected",
                    "stale_files": _stale_files,
                })

        # ── LiveWorkSensor ──
        try:
            from backend.core.ouroboros.governance.live_work_sensor import (
                LiveWorkSensor,
                is_enabled as _lws_enabled,
            )
            if _lws_enabled() and ctx.risk_tier is not RiskTier.APPROVAL_REQUIRED:
                _lws = LiveWorkSensor(orch._config.project_root)
                _active_hit: Optional[Tuple[str, str]] = None
                _scan_targets: set = set(ctx.target_files)
                for _cf, _ in orch._iter_candidate_files(best_candidate):
                    if _cf:
                        _scan_targets.add(_cf)
                for _tf in sorted(_scan_targets):
                    _is_active, _reason = _lws.is_human_active(str(_tf))
                    if _is_active:
                        _active_hit = (str(_tf), _reason or "human active")
                        break
                if _active_hit is not None:
                    _hit_file, _hit_reason = _active_hit
                    logger.warning(
                        "[Orchestrator] LiveWorkSensor: human is active on %s (%s) — deferring APPLY [%s]",
                        _hit_file, _hit_reason, ctx.op_id[:12],
                    )
                    await orch._record_ledger(ctx, OperationState.FAILED, {
                        "reason": "human_active_on_target",
                        "file": _hit_file,
                        "signal": _hit_reason,
                    })
                    ctx = ctx.advance(
                        OperationPhase.POSTMORTEM,
                        terminal_reason_code="human_active_on_target",
                    )
                    await orch._publish_outcome(ctx, OperationState.FAILED, "human_active_on_target")
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                        reason="human_active_on_target",
                        artifacts={"t_apply": _t_apply},
                    )
        except Exception:
            logger.debug("[Orchestrator] LiveWorkSensor check skipped", exc_info=True)

        # Pre-apply snapshots
        snapshots: Dict[str, str] = {}
        _snapshot_targets: set = {str(f) for f in ctx.target_files}
        for _cf, _ in orch._iter_candidate_files(best_candidate):
            if _cf:
                _snapshot_targets.add(_cf)
        for f in _snapshot_targets:
            fpath = Path(f) if Path(f).is_absolute() else orch._config.project_root / f
            if fpath.exists():
                try:
                    snapshots[str(f)] = fpath.read_text(errors="replace")
                except OSError:
                    pass
        if snapshots:
            ctx = ctx.with_pre_apply_snapshots(snapshots)

        # Multi-file vs single-file apply
        _candidate_files = orch._iter_candidate_files(best_candidate)
        _files_field = best_candidate.get("files") if isinstance(
            best_candidate, dict
        ) else None
        _has_files_key = isinstance(_files_field, list) and len(_files_field) > 0
        _multi_enabled = (
            os.environ.get("JARVIS_MULTI_FILE_GEN_ENABLED", "true").lower()
            not in ("false", "0", "no", "off")
        )
        _apply_mode = "multi" if len(_candidate_files) > 1 else "single"
        _file_basenames = [
            (fp.rsplit("/", 1)[-1] if "/" in fp else fp)
            for fp, _ in _candidate_files
        ]
        logger.info(
            "[Orchestrator] APPLY mode=%s candidate_files=%d "
            "files_list_present=%s multi_enabled=%s targets=[%s] op=%s",
            _apply_mode,
            len(_candidate_files),
            _has_files_key,
            _multi_enabled,
            ",".join(_file_basenames),
            ctx.op_id[:16],
        )

        if len(_candidate_files) > 1:
            _t_apply = time.monotonic()
            try:
                change_result = await orch._apply_multi_file_candidate(
                    ctx, best_candidate, _candidate_files, snapshots,
                )
            except Exception as exc:
                logger.error(
                    "Multi-file change engine raised for %s: %s", ctx.op_id, exc
                )
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="change_engine_error",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "change_engine_error", "error": str(exc), "multi_file": True},
                )
                orch._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply)
                await orch._publish_outcome(ctx, OperationState.FAILED, "change_engine_error")
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="change_engine_error",
                    artifacts={"t_apply": _t_apply},
                )
            change_request = None
        else:
            change_request = orch._build_change_request(ctx, best_candidate)
            _t_apply = time.monotonic()
            # Priority F2 — snapshot target_files content BEFORE the
            # change_engine writes anything. This pre-state is the
            # other half of the diff_text computation that fires
            # on success; without it diff is empty and no_new_-
            # credential_shapes evaluates to INSUFFICIENT.
            try:
                from backend.core.ouroboros.governance.verification.evidence_capture import (
                    stamp_target_files_pre,
                )
                stamp_target_files_pre(ctx)
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[Slice4b] stamp_target_files_pre failed",
                    exc_info=True,
                )
            try:
                change_result = await orch._stack.change_engine.execute(change_request)
            except Exception as exc:
                logger.error(
                    "Change engine raised for %s: %s", ctx.op_id, exc
                )
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="change_engine_error",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "change_engine_error", "error": str(exc)},
                )
                orch._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply)
                await orch._publish_outcome(ctx, OperationState.FAILED, "change_engine_error")
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="change_engine_error",
                    artifacts={"t_apply": _t_apply},
                )

        if not change_result.success:
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="change_engine_failed",
                rollback_occurred=change_result.rolled_back,
            )
            await orch._record_ledger(
                ctx,
                OperationState.FAILED,
                {
                    "reason": "change_engine_failed",
                    "rolled_back": change_result.rolled_back,
                },
            )
            orch._record_canary_for_ctx(
                ctx, False, time.monotonic() - _t_apply,
                rolled_back=change_result.rolled_back,
            )
            await orch._publish_outcome(ctx, OperationState.FAILED, "change_engine_failed")
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason="change_engine_failed",
                artifacts={"t_apply": _t_apply},
            )

        # Priority F2 — APPLY succeeded; capture full post-state
        # evidence so the F1 gatherers find rich pre-stamped data
        # instead of falling back to self-gather. Stamps:
        #   * ctx.target_files_post  (file_parses_after_change)
        #   * ctx.test_files_post    (test_set_hash_stable)
        #   * ctx.diff_text          (no_new_credential_shapes)
        # Best-effort, never raises.
        try:
            from backend.core.ouroboros.governance.verification.evidence_capture import (
                stamp_apply_evidence_post,
            )
            _stamp_diag = stamp_apply_evidence_post(
                ctx, target_dir=str(orch._config.project_root),
            )
            logger.info(
                "[Slice4b] apply_evidence_stamped op=%s "
                "target_files_post=%d test_files_post=%d "
                "diff_text_bytes=%d",
                ctx.op_id,
                _stamp_diag.get("target_files_post", 0),
                _stamp_diag.get("test_files_post", 0),
                _stamp_diag.get("diff_text_bytes", 0),
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[Slice4b] stamp_apply_evidence_post failed",
                exc_info=True,
            )

        # ==================== Phase 7.5: INFRASTRUCTURE ====================
        if orch._infra_applicator is not None and orch._infra_applicator.is_enabled:
            infra_results = await orch._infra_applicator.execute_post_apply(
                modified_files=ctx.target_files,
                op_id=ctx.op_id,
            )
            if infra_results and not orch._infra_applicator.all_succeeded(infra_results):
                _failed = [r for r in infra_results if not r.success]
                logger.error(
                    "[Orchestrator] Infrastructure hook failed for %s: %s",
                    ctx.op_id,
                    "; ".join(f"{r.file_trigger}: exit={r.exit_code}" for r in _failed),
                )
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="infrastructure_failed",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "infrastructure_failed",
                        "infra_results": [
                            {
                                "file": r.file_trigger,
                                "command": r.command,
                                "exit_code": r.exit_code,
                                "stderr": r.stderr_tail[:500],
                            }
                            for r in _failed
                        ],
                    },
                )
                orch._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply)
                await orch._publish_outcome(ctx, OperationState.FAILED, "infrastructure_failed")
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="infrastructure_failed",
                    artifacts={"t_apply": _t_apply},
                )

            for r in infra_results:
                logger.info(
                    "[Orchestrator] Infrastructure: %s completed in %.1fs (op=%s)",
                    r.file_trigger, r.duration_s, ctx.op_id,
                )

        if _serpent: _serpent.update_phase("APPLY")

        # OpsDigestObserver v1.1a — APPLY milestone
        try:
            from backend.core.ouroboros.governance.ops_digest_observer import (
                APPLY_MODE_MULTI,
                APPLY_MODE_SINGLE,
                get_ops_digest_observer,
            )
            _apply_file_count = len(ctx.target_files or ())
            _apply_mode_tag = (
                APPLY_MODE_MULTI if _apply_file_count > 1 else APPLY_MODE_SINGLE
            )
            get_ops_digest_observer().on_apply_succeeded(
                op_id=ctx.op_id,
                mode=_apply_mode_tag,
                files=_apply_file_count,
            )
        except Exception:
            logger.debug(
                "[Orchestrator] on_apply_succeeded observer call failed",
                exc_info=True,
            )

        # ==================== Phase 8: VERIFY ====================
        if _serpent: _serpent.update_phase("VERIFY")
        ctx = ctx.advance(OperationPhase.VERIFY)

        try:
            await orch._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="verify", progress_pct=92.0,
            )
        except Exception:
            pass

        await orch._record_ledger(
            ctx,
            OperationState.APPLIED,
            {"op_id": ctx.op_id},
        )

        # ---- Phase 8a: Scoped post-apply test run ----
        _verify_test_passed = True
        _verify_test_total = 0
        _verify_test_failures = 0
        _verify_failed_names: Tuple[str, ...] = ()

        if orch._validation_runner is not None and ctx.target_files:
            _changed = tuple(
                orch._config.project_root / f for f in ctx.target_files
            )
            _files_str = ", ".join(str(f) for f in list(ctx.target_files)[:3])

            try:
                await orch._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id, phase="verify",
                    verify_test_starting=True,
                    verify_target_files=list(ctx.target_files),
                )
            except Exception:
                pass

            _verify_budget_s = min(
                60.0,
                float(os.environ.get("JARVIS_VERIFY_TIMEOUT_S", "60")),
            )
            try:
                _multi = await asyncio.wait_for(
                    orch._validation_runner.run(
                        changed_files=_changed,
                        sandbox_dir=None,
                        timeout_budget_s=_verify_budget_s,
                        op_id=ctx.op_id,
                    ),
                    timeout=_verify_budget_s + 5.0,
                )
                _verify_test_passed = _multi.passed
                for _ar in _multi.adapter_results:
                    _verify_test_total += _ar.test_result.total
                    _verify_test_failures += _ar.test_result.failed
                    _verify_failed_names += _ar.test_result.failed_tests
                if _verify_test_total == 0 and _verify_test_failures == 0:
                    _verify_test_passed = True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("[Orchestrator] Verify scoped test timed out [%s]", ctx.op_id)
                _verify_test_passed = False
                _verify_test_failures = 1
            except BlockedPathError:
                pass
            except Exception as exc:
                logger.debug("[Orchestrator] Verify scoped test error: %s", exc)

            try:
                await orch._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id, phase="verify",
                    verify_test_passed=_verify_test_passed,
                    verify_test_total=_verify_test_total,
                    verify_test_failures=_verify_test_failures,
                    verify_target_files=list(ctx.target_files),
                )
            except Exception:
                pass

            try:
                from backend.core.ouroboros.governance.ops_digest_observer import (
                    get_ops_digest_observer,
                )
                _verify_passed_count = max(
                    0, _verify_test_total - _verify_test_failures,
                )
                get_ops_digest_observer().on_verify_completed(
                    op_id=ctx.op_id,
                    passed=_verify_passed_count,
                    total=_verify_test_total,
                    scoped_to_applied_op=True,
                )
            except Exception:
                logger.debug(
                    "[Orchestrator] on_verify_completed observer call failed",
                    exc_info=True,
                )

            # On failure: L2 repair before rollback
            if not _verify_test_passed and orch._config.repair_engine is not None:
                logger.info(
                    "[Orchestrator] VERIFY test failed (%d/%d) — routing to L2 repair [%s]",
                    _verify_test_failures, _verify_test_total, ctx.op_id,
                )
                _pl_deadline = ctx.pipeline_deadline or (
                    datetime.now(timezone.utc) + timedelta(seconds=60)
                )
                _synth_val = ValidationResult(
                    passed=False,
                    best_candidate=best_candidate,
                    validation_duration_s=0.0,
                    error=f"post-apply verify: {_verify_test_failures}/{_verify_test_total} failing",
                    failure_class="test",
                    short_summary=f"verify: {', '.join(_verify_failed_names[:3])}",
                    adapter_names_run=(),
                )
                try:
                    directive = await orch._l2_hook(ctx, _synth_val, _pl_deadline)
                    if directive[0] == "break":
                        _l2_candidate = directive[1]
                        _l2_change = orch._build_change_request(ctx, _l2_candidate)
                        try:
                            _l2_result = await orch._stack.change_engine.execute(_l2_change)
                            if _l2_result.success:
                                _verify_test_passed = True
                                _verify_test_failures = 0
                                logger.info(
                                    "[Orchestrator] L2 repair applied in VERIFY phase [%s]",
                                    ctx.op_id,
                                )
                            else:
                                logger.warning(
                                    "[Orchestrator] L2 repair candidate failed to apply [%s]",
                                    ctx.op_id,
                                )
                        except Exception as _apply_exc:
                            logger.debug("[Orchestrator] L2 repair apply error: %s", _apply_exc)
                    elif directive[0] in ("cancel", "fatal"):
                        ctx = directive[1]
                        logger.info(
                            "[Orchestrator] L2 escaped VERIFY phase — "
                            "op ctx advanced to %s [%s]",
                            ctx.phase.name, ctx.op_id,
                        )
                        return PhaseResult(
                            next_ctx=ctx, next_phase=None, status="fail",
                            reason=ctx.terminal_reason_code or "l2_escape_verify",
                            artifacts={"t_apply": _t_apply},
                        )
                except Exception as _l2_exc:
                    logger.debug(
                        "[Orchestrator] L2 repair in VERIFY failed: %s: %s",
                        type(_l2_exc).__name__, _l2_exc,
                    )

        ctx = await orch._run_benchmark(ctx, [])

        # ---- Verify Gate: enforce regression thresholds ----
        _verify_error = None
        try:
            from backend.core.ouroboros.governance.verify_gate import (
                enforce_verify_thresholds,
                rollback_files,
            )
            _br = getattr(ctx, "benchmark_result", None)
            if _br is not None:
                _baseline_cov = None
                _snapshots = getattr(ctx, "pre_apply_snapshots", {})
                if isinstance(_snapshots, dict):
                    _baseline_cov = _snapshots.get("_coverage_baseline")
                _verify_error = enforce_verify_thresholds(_br, baseline_coverage=_baseline_cov)
        except Exception as exc:
            logger.debug("[Orchestrator] Verify gate skipped: %s", exc)

        if _verify_error is None and not _verify_test_passed:
            _verify_error = f"scoped verify: {_verify_test_failures}/{_verify_test_total} tests failing"

        if _verify_error is not None:
            logger.warning(
                "[Orchestrator] VERIFY regression gate fired: %s [%s]",
                _verify_error, ctx.op_id,
            )
            try:
                await orch._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause=f"verify_regression: {_verify_error}",
                    failed_phase="VERIFY",
                    target_files=list(ctx.target_files),
                )
            except Exception:
                pass
            try:
                _snapshots = getattr(ctx, "pre_apply_snapshots", {})
                if _snapshots:
                    from backend.core.ouroboros.governance.verify_gate import (
                        rollback_files as _rollback_files,
                    )
                    _rollback_files(
                        pre_apply_snapshots=_snapshots,
                        target_files=list(ctx.target_files),
                        repo_root=orch._config.project_root,
                    )
            except Exception as exc:
                logger.error("[Orchestrator] Verify rollback failed: %s", exc)

            if _checkpoint is not None and _ckpt_mgr is not None:
                try:
                    await _ckpt_mgr.restore_checkpoint(_checkpoint.checkpoint_id)
                    logger.info(
                        "[Orchestrator] Git checkpoint restored: %s [%s]",
                        _checkpoint.checkpoint_id, ctx.op_id,
                    )
                except Exception:
                    logger.debug("[Orchestrator] Checkpoint restore failed", exc_info=True)

            if _serpent: _serpent.update_phase("POSTMORTEM")
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="verify_regression",
                rollback_occurred=True,
            )
            await orch._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": "verify_regression", "detail": _verify_error, "rollback_occurred": True},
            )
            orch._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply, rolled_back=True)
            await orch._publish_outcome(ctx, OperationState.FAILED, "verify_regression")
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason="verify_regression",
                artifacts={"t_apply": _t_apply},
            )

        # ---- Phase 8b: Auto-commit ----
        _committed_hash: Optional[str] = None
        try:
            from backend.core.ouroboros.governance.auto_committer import AutoCommitter
            _committer = AutoCommitter(repo_root=orch._config.project_root)
            _gen = ctx.generation
            _provider = getattr(_gen, "provider_name", "") if _gen else ""
            _cost = 0.0
            if _gen:
                _in_tok = getattr(_gen, "total_input_tokens", 0) or 0
                _out_tok = getattr(_gen, "total_output_tokens", 0) or 0
                _cost = (_in_tok * 0.0000001 + _out_tok * 0.0000004)
            _commit_result = await asyncio.wait_for(
                _committer.commit(
                    op_id=ctx.op_id,
                    description=ctx.description,
                    target_files=ctx.target_files,
                    risk_tier=ctx.risk_tier,
                    provider_name=_provider,
                    generation_cost=_cost,
                    signal_source=getattr(ctx, "signal_source", ""),
                    signal_urgency=getattr(ctx, "signal_urgency", ""),
                    rationale=ctx.description,
                ),
                timeout=30.0,
            )
            if _commit_result.committed:
                _committed_hash = _commit_result.commit_hash
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="commit",
                        progress_pct=98.0,
                        commit_hash=_commit_result.commit_hash,
                        commit_pushed=_commit_result.pushed,
                        commit_branch=_commit_result.push_branch,
                    )
                except Exception:
                    pass
                logger.info(
                    "[Orchestrator] Auto-committed %s for op=%s",
                    _commit_result.commit_hash, ctx.op_id,
                )

                try:
                    from backend.core.ouroboros.governance.ops_digest_observer import (
                        get_ops_digest_observer,
                    )
                    get_ops_digest_observer().on_commit_succeeded(
                        op_id=ctx.op_id,
                        commit_hash=_commit_result.commit_hash or "",
                    )
                except Exception:
                    logger.debug(
                        "[Orchestrator] on_commit_succeeded observer call failed",
                        exc_info=True,
                    )
            elif _commit_result.skipped_reason:
                logger.debug(
                    "[Orchestrator] Auto-commit skipped: %s",
                    _commit_result.skipped_reason,
                )
        except ImportError:
            logger.debug("[Orchestrator] AutoCommitter not available")
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Auto-commit failed for op=%s: %s; "
                "change is applied but not committed",
                ctx.op_id, exc,
            )

        # ---- Phase 8b2: In-process hot-reload ----
        if orch._hot_reloader is not None:
            try:
                _hr_batch = orch._hot_reloader.reload_for_op(
                    op_id=ctx.op_id,
                    target_files=ctx.target_files,
                )
                if _hr_batch.overall_status == "success":
                    _reloaded_names = [
                        o.module_name.rsplit(".", 1)[-1]
                        for o in _hr_batch.outcomes
                        if o.status == "reloaded"
                    ]
                    logger.info(
                        "[Orchestrator] Hot-reloaded %d module(s) for op=%s: %s",
                        len(_reloaded_names), ctx.op_id, _reloaded_names,
                    )
                    try:
                        await orch._stack.comm.emit_heartbeat(
                            op_id=ctx.op_id, phase="hot_reload",
                            progress_pct=99.0,
                            reloaded_modules=_reloaded_names,
                            reload_count=orch._hot_reloader.reload_count,
                        )
                    except Exception:
                        pass
                elif _hr_batch.overall_status in ("reload_failed", "preflight_failed"):
                    logger.warning(
                        "[Orchestrator] Hot-reload failed for op=%s: %s; "
                        "restart will be queued",
                        ctx.op_id, _hr_batch.restart_reason,
                    )
                elif _hr_batch.restart_required:
                    logger.info(
                        "[Orchestrator] Hot-reload deferred to restart for op=%s: %s",
                        ctx.op_id, _hr_batch.restart_reason,
                    )
            except Exception as exc:
                logger.warning(
                    "[Orchestrator] Hot-reload hook raised for op=%s: %s",
                    ctx.op_id, exc,
                )

        # ---- Phase 8c: Self-critique ----
        if orch._critique_engine is not None:
            try:
                _test_summary = "(no test summary captured)"
                _vr = ctx.validation
                if _vr is not None:
                    _passed = getattr(_vr, "tests_passed", 0) or 0
                    _total = getattr(_vr, "tests_total", 0) or 0
                    if _total:
                        _test_summary = f"{_passed}/{_total} tests passed"
                    elif _passed:
                        _test_summary = f"{_passed} tests passed"
                _critique_result = await asyncio.wait_for(
                    orch._critique_engine.critique_op(
                        op_id=ctx.op_id,
                        description=ctx.description,
                        target_files=ctx.target_files,
                        risk_tier=ctx.risk_tier,
                        commit_hash=_committed_hash,
                        test_summary=_test_summary,
                    ),
                    timeout=float(os.environ.get("JARVIS_CRITIQUE_TIMEOUT_S", "30")) + 5.0,
                )
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id,
                        phase="critique",
                        progress_pct=99.0,
                        critique_rating=int(getattr(_critique_result, "rating", 0)),
                        critique_matches_goal=bool(
                            getattr(_critique_result, "matches_goal", True)
                        ),
                        critique_rationale=str(
                            getattr(_critique_result, "rationale", "")
                        )[:200],
                        critique_provider=str(
                            getattr(_critique_result, "provider_name", "")
                        ),
                        critique_parse_ok=bool(
                            getattr(_critique_result, "parse_ok", True)
                        ),
                    )
                except Exception:
                    pass
                if (
                    getattr(_critique_result, "parse_ok", False)
                    and getattr(_critique_result, "is_poor", False)
                ):
                    _files_short = ", ".join(
                        p.rsplit("/", 1)[-1] for p in ctx.target_files[:3]
                    )
                    orch._add_session_lesson(
                        "code",
                        f"[CRITIQUE POOR {getattr(_critique_result, 'rating', '?')}/5] "
                        f"{ctx.description[:60]} ({_files_short}): "
                        f"{str(getattr(_critique_result, 'rationale', ''))[:120]}",
                        op_id=ctx.op_id,
                    )
            except asyncio.TimeoutError:
                logger.info(
                    "[Orchestrator] Self-critique timed out for op=%s — "
                    "non-blocking, continuing to COMPLETE",
                    ctx.op_id,
                )
            except Exception as exc:
                logger.debug(
                    "[Orchestrator] Self-critique failed for op=%s: %s",
                    ctx.op_id, exc,
                )

        # ---- Phase 8d: Visual VERIFY ----
        try:
            from backend.core.ouroboros.governance.visual_verify import (
                run_post_verify,
            )
            _vv_outcome = run_post_verify(
                target_files=ctx.target_files,
                attachments=ctx.attachments,
                op_id=ctx.op_id,
                op_description=ctx.description,
                plan_ui_affected=False,
                test_targets_resolved=(
                    ctx.validation.adapter_names_run if ctx.validation else None
                ),
                risk_tier=(
                    ctx.risk_tier.name.lower() if ctx.risk_tier else ""
                ),
                test_runner_result="passed",
            )
            if _vv_outcome.ran:
                _vv_verdict = (
                    _vv_outcome.result.verdict if _vv_outcome.result else "?"
                )
                logger.info(
                    "[Orchestrator] Visual VERIFY outcome=%s "
                    "l2_triggered=%s [%s] %s",
                    _vv_verdict, _vv_outcome.l2_triggered,
                    ctx.op_id, _vv_outcome.reasoning,
                )
                try:
                    ctx = ctx.advance(OperationPhase.VISUAL_VERIFY)
                except ValueError as _adv_exc:
                    logger.debug(
                        "[Orchestrator] VISUAL_VERIFY advance rejected "
                        "(ctx at %s): %s", ctx.phase.name, _adv_exc,
                    )

                _vv_fail = (
                    _vv_outcome.l2_triggered
                    or (
                        _vv_outcome.result is not None
                        and _vv_outcome.result.verdict == "fail"
                    )
                )
                if _vv_fail and orch._config.repair_engine is not None:
                    logger.info(
                        "[Orchestrator] Visual VERIFY fail/advisory — "
                        "routing to L2 repair [%s]", ctx.op_id,
                    )
                    _vv_deadline = ctx.pipeline_deadline or (
                        datetime.now(timezone.utc) + timedelta(seconds=60)
                    )
                    _vv_synth_val = ValidationResult(
                        passed=False,
                        best_candidate=best_candidate,
                        validation_duration_s=0.0,
                        error=f"visual_verify: {_vv_outcome.reasoning}",
                        failure_class="test",
                        short_summary=(
                            f"visual_verify: "
                            f"{_vv_outcome.result.check if _vv_outcome.result else 'advisory'}"
                        ),
                        adapter_names_run=(),
                    )
                    try:
                        _vv_directive = await orch._l2_hook(
                            ctx, _vv_synth_val, _vv_deadline,
                        )
                        if _vv_directive[0] == "break":
                            _vv_l2_candidate = _vv_directive[1]
                            _vv_l2_change = orch._build_change_request(
                                ctx, _vv_l2_candidate,
                            )
                            try:
                                _vv_l2_result = (
                                    await orch._stack.change_engine.execute(
                                        _vv_l2_change
                                    )
                                )
                                if _vv_l2_result.success:
                                    logger.info(
                                        "[Orchestrator] Visual VERIFY L2 "
                                        "repair applied [%s]", ctx.op_id,
                                    )
                                else:
                                    logger.warning(
                                        "[Orchestrator] Visual VERIFY L2 "
                                        "repair candidate failed to apply [%s]",
                                        ctx.op_id,
                                    )
                            except Exception as _vv_apply_exc:
                                logger.debug(
                                    "[Orchestrator] Visual VERIFY L2 apply "
                                    "error: %s", _vv_apply_exc,
                                )
                        elif _vv_directive[0] in ("cancel", "fatal"):
                            ctx = _vv_directive[1]
                            logger.info(
                                "[Orchestrator] L2 escaped Visual VERIFY — "
                                "op ctx advanced to %s [%s]",
                                ctx.phase.name, ctx.op_id,
                            )
                            return PhaseResult(
                                next_ctx=ctx, next_phase=None, status="fail",
                                reason=ctx.terminal_reason_code or "l2_escape_visual_verify",
                                artifacts={"t_apply": _t_apply},
                            )
                    except Exception as _vv_l2_exc:
                        logger.debug(
                            "[Orchestrator] Visual VERIFY L2 failed: "
                            "%s: %s",
                            type(_vv_l2_exc).__name__, _vv_l2_exc,
                        )
        except Exception as _vv_exc:
            logger.debug(
                "[Orchestrator] Visual VERIFY dispatch error: %s: %s",
                type(_vv_exc).__name__, _vv_exc,
            )
        # ---- end verbatim transcription ----

        # Success — hand off to COMPLETE via COMPLETERunner-equivalent.
        # The orchestrator hook at this point will either call
        # COMPLETERunner (if slice 1 flag on) or inline COMPLETE code.
        return PhaseResult(
            next_ctx=ctx,
            next_phase=OperationPhase.COMPLETE,
            status="ok",
            reason="applied_and_verified",
            artifacts={"t_apply": _t_apply},
        )


__all__ = ["Slice4bRunner"]
