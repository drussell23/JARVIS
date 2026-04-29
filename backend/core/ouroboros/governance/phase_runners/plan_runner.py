"""PLANRunner — Slice 3 of Wave 2 item (5). The big one (~750 lines).

Extracts orchestrator.py lines ~2259-3012 (the PLAN phase body through
pre-GENERATE cancellation check) into a :class:`PhaseRunner` behind
``JARVIS_PHASE_RUNNER_PLAN_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** Verbatim transcription with ``self.``
→ ``orch.`` substitutions and module-level helper references qualified
from the orchestrator module.

## What PLAN does (15 distinct sub-blocks)

1. ``_serpent.update_phase("PLAN")`` + ``plan`` heartbeat (progress 25%)
2. ``PlanGenerator.generate_plan`` with deadline + asyncio.wait_for
3. Phase B PLAN-shadow (observer-only DAG dispatch, gated)
4. Plan-review-required hard terminal when ``_plan_review_required()`` is
   true but no plan is available (``plan_required_unavailable``)
5. Plan Approval Hard Gate (Phase 2d) — complex multi-path gate
6. advance(GENERATE)
7. PreActionNarrator.narrate("GENERATE")
8. Adaptive Learning injection (P2)
9. Test Coverage Enforcer (P0)
10. JARVIS Tier 5 Cross-Domain Intelligence
11. JARVIS Tier 6 Personality voice line (reads ``_advisory`` from CLASSIFY)
12. Advanced Repair (hierarchical localizer + slow/fast thinking + docs)
13. Self-Evolution P0 (runtime adaptations + negative constraints + metrics)
14. Self-Evolution P2 (module-level + auto-documentation)
15. Pre-GENERATE cooperative cancellation check (``user_cancelled``)

## Five terminal exit paths

* ``plan_required_unavailable`` (no plan but review required)
* ``plan_review_unavailable`` (provider missing OR gate infra failed
  in "review required" mode)
* ``plan_rejected`` (human reviewer said no)
* ``plan_approval_expired`` (timeout, strict mode)
* ``user_cancelled`` (cooperative /cancel between PLAN and GENERATE)

## Success path — ``next_phase = GENERATE``

ctx advanced to GENERATE with ``implementation_plan`` stamped on it
when PLAN succeeded (empty on skip/failure).

## Cross-phase artifact consumption

This runner reads ``_advisory`` (from CLASSIFY's ``artifacts``) at
Tier 6 personality voice line (line ~2831). The caller (orchestrator
hook) passes ``advisory`` through the ``advisory`` constructor arg.

## Authority invariant

Imports policy/risk_tier-adjacent modules only through function-local
imports that match the inline block's surface (``plan_generator`` /
``plan_approval`` / ``adaptive_learning`` / ``intelligence_hooks`` /
``jarvis_intelligence`` / ``advanced_repair`` / ``self_evolution`` /
``user_preference_memory`` / ``entropy_calculator``). No
execution-authority widening beyond what the inline block already had.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, List, Optional

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)

if TYPE_CHECKING:  # pragma: no cover
    from backend.core.ouroboros.governance.orchestrator import Orchestrator


logger = logging.getLogger("Ouroboros.Orchestrator")


# ---------------------------------------------------------------------------
# Priority A Slice A3 — Mandatory claim density at every PLAN exit
# ---------------------------------------------------------------------------
#
# Per PRD §25.5.1: every PLAN exit (success path AND every failure
# terminal) MUST capture default must_hold claims so post-APPLY
# verification has signal to evaluate. The pre-A3 wiring captured
# claims ONLY on the success path AND ONLY when _plan_result was
# non-skipped — which silently zeroed claim coverage for the entire
# trivial-op population (every op in soak #3).
#
# This helper is the single chokepoint: every `return PhaseResult(...)`
# that exits PLAN is preceded by `await _capture_default_claims_at_plan_exit`.
# The helper is master-flag-gated at the synthesizer level
# (JARVIS_DEFAULT_CLAIMS_ENABLED → Slice A2's `default_claims_enabled`),
# so when off it is a pure no-op without runner-side branches.


async def _capture_default_claims_at_plan_exit(
    ctx: Any, *, exit_reason: str,
) -> int:
    """Synthesize + persist the default must_hold claims for this op.

    Walks the default-claim registry (verification.default_claims),
    applies per-spec filters (file_pattern + posture), persists each
    surviving claim via Slice 2.3's ``capture_claims`` (which routes
    through Slice 1.3's Merkle DAG ledger).

    NEVER raises. Best-effort — capture failures are logged at DEBUG
    and counted as zero. Master flag ``JARVIS_DEFAULT_CLAIMS_ENABLED``
    governs whether the synthesizer materializes anything; when off,
    this returns 0 without touching the ledger.

    Posture is read best-effort via the posture_observer singleton.
    Unavailable / not-yet-initialized → ``None``, which means any
    posture-filtered specs are skipped on this exit.

    Returns the count of default claims successfully persisted.
    """
    try:
        from backend.core.ouroboros.governance.verification.default_claims import (
            synthesize_default_claims,
        )
        from backend.core.ouroboros.governance.verification.property_capture import (
            capture_claims,
        )
    except Exception:  # noqa: BLE001 — verification package missing
        logger.debug(
            "[PLANRunner] verification module unavailable at exit_reason=%s",
            exit_reason, exc_info=True,
        )
        return 0
    # Best-effort posture read — never raise into the runner path.
    posture: Optional[str] = None
    try:
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_store,
        )
        reading = get_default_store().load_current()
        if reading is not None:
            posture = reading.posture.value
    except Exception:  # noqa: BLE001
        posture = None
    op_id = str(getattr(ctx, "op_id", "") or "")
    if not op_id:
        return 0
    target_files = tuple(getattr(ctx, "target_files", ()) or ())
    try:
        claims = synthesize_default_claims(
            op_id=op_id,
            target_files=target_files,
            posture=posture,
        )
    except Exception:  # noqa: BLE001 — defensive (synthesizer should
        # itself never raise; this is belt-and-suspenders)
        logger.debug(
            "[PLANRunner] synthesize_default_claims raised at "
            "exit_reason=%s op=%s",
            exit_reason, op_id, exc_info=True,
        )
        return 0
    if not claims:
        return 0
    try:
        captured = await capture_claims(
            op_id=op_id, claims=claims, ctx=ctx,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PLANRunner] capture_claims raised at exit_reason=%s op=%s",
            exit_reason, op_id, exc_info=True,
        )
        return 0
    if captured:
        # §8 observability — one structured INFO line per exit so
        # operators can audit "did the loop close?" via grep without
        # reading the determinism ledger.
        logger.info(
            "[PLANRunner] default_claims_captured count=%d "
            "exit_reason=%s op=%s",
            captured, exit_reason, op_id,
        )
    return captured


class PLANRunner(PhaseRunner):
    """Verbatim transcription of orchestrator.py PLAN block (~2259-3012)."""

    phase = OperationPhase.PLAN

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
        advisory: Optional[Any] = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent
        self._advisory = advisory

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent
        _advisory = self._advisory

        # Import orchestrator module-level helper lazily to avoid circular import
        from backend.core.ouroboros.governance.orchestrator import (
            _plan_review_required,
        )

        # Priority F3 — stamp test_files_pre at PLAN entry. Captures
        # the existing test inventory BEFORE any change-engine work
        # so the test_set_hash_stable claim (Priority A) can compute
        # a real pre-vs-post delta at evaluation time. Best-effort,
        # never raises. Master-flag-gated at the helper level.
        try:
            from backend.core.ouroboros.governance.verification.evidence_capture import (
                stamp_test_files_pre,
            )
            stamp_test_files_pre(
                ctx, target_dir=str(orch._config.project_root),
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[PLANRunner] stamp_test_files_pre failed",
                exc_info=True,
            )

        # ---- VERBATIM transcription of orchestrator.py 2263-3012 ----
        if _serpent:
            _serpent.update_phase("PLAN")
        try:
            await orch._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="plan", progress_pct=25.0,
            )
        except Exception:
            pass

        _plan_result: Optional[Any] = None
        _plan_review_required_now = _plan_review_required()
        try:
            from backend.core.ouroboros.governance.plan_generator import (
                PlanGenerator, PLAN_TIMEOUT_S,
            )
            _plan_gen = PlanGenerator(
                generator=orch._generator,
                repo_root=orch._config.project_root,
            )
            _plan_deadline = datetime.now(tz=timezone.utc) + timedelta(
                seconds=PLAN_TIMEOUT_S,
            )
            _plan_result = await asyncio.wait_for(
                _plan_gen.generate_plan(ctx, _plan_deadline),
                timeout=PLAN_TIMEOUT_S + 5.0,
            )

            if not _plan_result.skipped:
                # Store plan in context for injection into GENERATE prompt
                ctx = dataclasses.replace(
                    ctx,
                    implementation_plan=_plan_result.plan_json,
                    previous_hash=ctx.context_hash,
                )
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="plan", progress_pct=28.0,
                        plan_complexity=_plan_result.complexity,
                        plan_changes=len(_plan_result.ordered_changes),
                    )
                except Exception:
                    pass
                logger.info(
                    "[Orchestrator] PLAN complete for op=%s: complexity=%s, "
                    "%d ordered changes, %.1fs",
                    ctx.op_id, _plan_result.complexity,
                    len(_plan_result.ordered_changes),
                    _plan_result.planning_duration_s,
                )
            else:
                logger.debug(
                    "[Orchestrator] PLAN skipped for op=%s: %s",
                    ctx.op_id, _plan_result.skip_reason,
                )
        except ImportError:
            logger.debug("[Orchestrator] PlanGenerator not available, skipping PLAN phase")
        except Exception as exc:
            logger.warning(
                "[Orchestrator] PLAN phase failed for op=%s: %s; "
                "continuing to GENERATE without plan",
                ctx.op_id, exc,
            )

        # Phase B PLAN-shadow (Slice 1b)
        try:
            ctx = await orch._run_plan_shadow(ctx)
        except Exception:
            logger.debug(
                "[Orchestrator] PLAN-shadow wrapper swallowed exception",
                exc_info=True,
            )

        if _plan_review_required_now and (
            _plan_result is None or getattr(_plan_result, "skipped", True)
        ):
            _skip_reason = getattr(_plan_result, "skip_reason", "") or "plan_not_available"
            logger.info(
                "[Orchestrator] Plan review required for op=%s but no plan is "
                "available: %s",
                ctx.op_id,
                _skip_reason,
            )
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                terminal_reason_code="plan_required_unavailable",
            )
            await orch._record_ledger(
                ctx,
                OperationState.FAILED,
                {
                    "reason": "plan_required_unavailable",
                    "detail": _skip_reason,
                },
            )
            await _capture_default_claims_at_plan_exit(
                ctx, exit_reason="plan_required_unavailable",
            )
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason="plan_required_unavailable",
            )

        # ---- Phase 2d: Plan Approval Hard Gate ----
        _plan_gate_enabled = _plan_review_required_now or (
            os.environ.get("JARVIS_PLAN_APPROVAL_ENABLED", "true").lower()
            not in ("false", "0", "no", "off")
        )
        _plan_gate_applied = False
        if (
            _plan_gate_enabled
            and _plan_result is not None
            and not getattr(_plan_result, "skipped", True)
        ):
            _gate_routes = {
                r.strip().lower()
                for r in os.environ.get(
                    "JARVIS_PLAN_APPROVAL_ROUTES", "complex"
                ).split(",")
                if r.strip()
            }
            _gate_complexities = {
                c.strip().lower()
                for c in os.environ.get(
                    "JARVIS_PLAN_APPROVAL_COMPLEXITIES",
                    "complex,heavy_code,architectural",
                ).split(",")
                if c.strip()
            }
            _route = (getattr(ctx, "provider_route", "") or "").lower()
            _task_cx = (getattr(ctx, "task_complexity", "") or "").lower()
            _plan_cx = (getattr(_plan_result, "complexity", "") or "").lower()
            _plan_mode_force = False
            try:
                from backend.core.ouroboros.governance.plan_approval import (
                    should_force_plan_review as _should_force_plan_review,
                )
                _plan_mode_force = _should_force_plan_review(ctx)
            except Exception:  # noqa: BLE001 — optional dep
                _plan_mode_force = False
            _should_gate = (
                _plan_review_required_now
                or _plan_mode_force
                or _route in _gate_routes
                or _task_cx in _gate_complexities
                or _plan_cx in _gate_complexities
            )
            _provider_supports_plan = (
                orch._approval_provider is not None
                and hasattr(orch._approval_provider, "request_plan")
            )
            if _should_gate and not _provider_supports_plan:
                logger_msg = (
                    "[Orchestrator] Plan review required for op=%s but no "
                    "plan approval provider is available"
                    if _plan_review_required_now
                    else "[Orchestrator] Plan Gate skipped for op=%s: "
                    "provider=%s has_request_plan=%s"
                )
                if _plan_review_required_now:
                    logger.info(logger_msg, ctx.op_id)
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="plan_review_unavailable",
                    )
                    await orch._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {
                            "reason": "plan_review_unavailable",
                            "detail": "approval_provider_missing",
                        },
                    )
                    await _capture_default_claims_at_plan_exit(
                        ctx,
                        exit_reason="plan_review_unavailable:provider_missing",
                    )
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                        reason="plan_review_unavailable",
                    )
                logger.debug(
                    logger_msg,
                    ctx.op_id,
                    type(orch._approval_provider).__name__
                    if orch._approval_provider
                    else "None",
                    hasattr(orch._approval_provider, "request_plan"),
                )
            elif _should_gate:
                _plan_gate_applied = True
                _plan_gate_timeout = float(os.environ.get(
                    "JARVIS_PLAN_APPROVAL_TIMEOUT_S", "600.0"
                ))
                _expire_grace = os.environ.get(
                    "JARVIS_PLAN_APPROVAL_EXPIRE_GRACE", "false"
                ).lower() in ("true", "1", "yes", "on")

                try:
                    _plan_markdown = _plan_result.to_prompt_section()
                except Exception:
                    _plan_markdown = _plan_result.plan_json or "(no plan)"

                logger.info(
                    "[Orchestrator] Plan Gate engaged for op=%s "
                    "(route=%r task_cx=%r plan_cx=%r) — awaiting human",
                    ctx.op_id, _route, _task_cx, _plan_cx,
                )
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="plan", progress_pct=30.0,
                        plan_gate_engaged=True,
                    )
                except Exception:
                    pass

                _plan_mirror_registered = False
                try:
                    from backend.core.ouroboros.governance.plan_approval import (
                        get_default_controller as _get_pa_controller,
                    )
                    _pa_controller = _get_pa_controller()
                    if _pa_controller.snapshot(ctx.op_id) is None:
                        _pa_controller.request_approval(
                            ctx.op_id,
                            {
                                "markdown": _plan_markdown,
                                "description": getattr(ctx, "description", ""),
                                "target_files": list(
                                    getattr(ctx, "target_files", []) or [],
                                ),
                                "approach": getattr(
                                    _plan_result, "approach", "",
                                ) or "",
                                "complexity": getattr(
                                    _plan_result, "complexity", "",
                                ) or "",
                                "ordered_changes": list(
                                    getattr(
                                        _plan_result, "ordered_changes", [],
                                    ) or [],
                                ),
                                "risk_factors": list(
                                    getattr(
                                        _plan_result, "risk_factors", [],
                                    ) or [],
                                ),
                                "test_strategy": getattr(
                                    _plan_result, "test_strategy", "",
                                ) or "",
                            },
                            timeout_s=_plan_gate_timeout,
                        )
                        _plan_mirror_registered = True
                except Exception:  # noqa: BLE001 — best-effort mirror
                    logger.debug(
                        "[Orchestrator] PlanApproval mirror register "
                        "best-effort failed for op=%s", ctx.op_id,
                        exc_info=True,
                    )

                try:
                    _plan_req_id = await orch._approval_provider.request_plan(
                        ctx, _plan_markdown,
                    )
                    _plan_decision: ApprovalResult = await (
                        orch._approval_provider.await_decision(
                            _plan_req_id, _plan_gate_timeout,
                        )
                    )
                except Exception as _gate_exc:
                    if _plan_review_required_now:
                        logger.info(
                            "[Orchestrator] Plan review required for op=%s but "
                            "the plan gate failed: %s",
                            ctx.op_id,
                            _gate_exc,
                        )
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="plan_review_unavailable",
                        )
                        await orch._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "plan_review_unavailable",
                                "detail": str(_gate_exc)[:200],
                            },
                        )
                        await _capture_default_claims_at_plan_exit(
                            ctx,
                            exit_reason="plan_review_unavailable:gate_infra",
                        )
                        return PhaseResult(
                            next_ctx=ctx, next_phase=None, status="fail",
                            reason="plan_review_unavailable",
                        )
                    logger.warning(
                        "[Orchestrator] Plan Gate infra failure for op=%s: %s; "
                        "continuing to GENERATE without approval",
                        ctx.op_id, _gate_exc,
                    )
                    _plan_decision = None  # type: ignore[assignment]

                if _plan_decision is not None:
                    if _plan_mirror_registered:
                        try:
                            from backend.core.ouroboros.governance.plan_approval import (
                                get_default_controller as _get_pa_ctrl,
                                PlanApprovalStateError as _PAStateError,
                            )
                            _pa_mirror_ctrl = _get_pa_ctrl()
                            _mirror_approver = (
                                getattr(_plan_decision, "approver", None)
                                or "orchestrator"
                            )
                            try:
                                if _plan_decision.status is ApprovalStatus.APPROVED:
                                    _pa_mirror_ctrl.approve(
                                        ctx.op_id, reviewer=_mirror_approver,
                                    )
                                elif _plan_decision.status is ApprovalStatus.REJECTED:
                                    _pa_mirror_ctrl.reject(
                                        ctx.op_id,
                                        reason=getattr(
                                            _plan_decision, "reason", "",
                                        ) or "",
                                        reviewer=_mirror_approver,
                                    )
                            except _PAStateError:
                                pass
                        except Exception:  # noqa: BLE001 — best-effort
                            logger.debug(
                                "[Orchestrator] PlanApproval mirror terminal "
                                "propagation best-effort failed for op=%s",
                                ctx.op_id, exc_info=True,
                            )
                    if _plan_decision.status is ApprovalStatus.REJECTED:
                        _reject_reason = (
                            getattr(_plan_decision, "reason", "") or ""
                        )
                        logger.info(
                            "[Orchestrator] Plan REJECTED for op=%s: %s",
                            ctx.op_id, _reject_reason,
                        )
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="plan_rejected",
                        )
                        await orch._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "plan_rejected",
                                "approver": _plan_decision.approver,
                                "rejection_reason": _reject_reason,
                                "plan_complexity": _plan_cx,
                            },
                        )
                        if _reject_reason:
                            try:
                                from backend.core.ouroboros.governance.user_preference_memory import (
                                    get_default_store,
                                )
                                get_default_store().record_approval_rejection(
                                    op_id=ctx.op_id,
                                    description=f"[PLAN] {ctx.description}",
                                    target_files=list(ctx.target_files),
                                    reason=_reject_reason,
                                    approver=(
                                        getattr(_plan_decision, "approver", "human")
                                        or "human"
                                    ),
                                )
                            except Exception:
                                pass
                        _files_short = ", ".join(
                            p.rsplit("/", 1)[-1] for p in ctx.target_files[:3]
                        )
                        orch._add_session_lesson(
                            "code",
                            f"[PLAN REJECTED] {ctx.description[:60]} "
                            f"({_files_short}) — human rejected the approach: "
                            f"{_reject_reason[:80] or 'no reason given'}. "
                            f"Reconsider strategy before retry.",
                            op_id=ctx.op_id,
                        )
                        await _capture_default_claims_at_plan_exit(
                            ctx, exit_reason="plan_rejected",
                        )
                        return PhaseResult(
                            next_ctx=ctx, next_phase=None, status="fail",
                            reason="plan_rejected",
                        )

                    if _plan_decision.status is ApprovalStatus.EXPIRED:
                        if _expire_grace and not _plan_review_required_now:
                            logger.warning(
                                "[Orchestrator] Plan Gate expired for op=%s; "
                                "grace mode — continuing to GENERATE",
                                ctx.op_id,
                            )
                        else:
                            logger.info(
                                "[Orchestrator] Plan Gate EXPIRED for op=%s — "
                                "aborting (strict mode)",
                                ctx.op_id,
                            )
                            ctx = ctx.advance(
                                OperationPhase.EXPIRED,
                                terminal_reason_code="plan_approval_expired",
                            )
                            await orch._record_ledger(
                                ctx,
                                OperationState.FAILED,
                                {"reason": "plan_approval_expired"},
                            )
                            await _capture_default_claims_at_plan_exit(
                                ctx, exit_reason="plan_approval_expired",
                            )
                            return PhaseResult(
                                next_ctx=ctx, next_phase=None, status="fail",
                                reason="plan_approval_expired",
                            )

                    if _plan_decision.status is ApprovalStatus.APPROVED:
                        logger.info(
                            "[Orchestrator] Plan APPROVED for op=%s by %s",
                            ctx.op_id, _plan_decision.approver,
                        )
        ctx = ctx.advance(OperationPhase.GENERATE)

        # ── PreActionNarrator: voice WHAT before GENERATE ──
        if orch._pre_action_narrator is not None:
            try:
                _provider_name = getattr(ctx, "routing_actual", None) or "unknown"
                await orch._pre_action_narrator.narrate_phase(
                    "GENERATE",
                    {"provider": str(_provider_name), "thinking_mode": "standard"},
                )
            except Exception:
                pass

        # ── P2: Adaptive Learning — inject consolidated rules + success patterns ──
        try:
            from backend.core.ouroboros.governance.adaptive_learning import (
                LearningConsolidator, SuccessPatternStore,
            )
            from backend.core.ouroboros.governance.entropy_calculator import (
                extract_domain_key as _extract_dk,
            )
            _domain = _extract_dk(ctx.target_files, ctx.description)

            _consolidator = LearningConsolidator()
            _rules_context = _consolidator.format_rules_for_prompt(_domain)

            _success_store = SuccessPatternStore()
            _success_context = _success_store.format_for_prompt(_domain, ctx.target_files)

            if _rules_context or _success_context:
                _existing_mem = getattr(ctx, "strategic_memory_prompt", "") or ""
                _learning_block = ""
                if _rules_context:
                    _learning_block += f"\n\n{_rules_context}"
                if _success_context:
                    _learning_block += f"\n\n{_success_context}"
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing_mem + _learning_block,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Adaptive learning: injected %d rules + %d success "
                    "patterns for domain=%s (op=%s)",
                    len(_consolidator.get_rules_for_domain(_domain)),
                    len(_success_store.get_similar_successes(_domain, ctx.target_files)),
                    _domain, ctx.op_id,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Adaptive learning injection failed", exc_info=True)

        # ── P0: Test Coverage Enforcer (pre-GENERATE) ──
        try:
            from backend.core.ouroboros.governance.intelligence_hooks import (
                TestCoverageEnforcer,
            )
            _coverage_enforcer = TestCoverageEnforcer(orch._config.project_root)
            _coverage_instruction = _coverage_enforcer.check_and_inject(
                ctx.target_files, ctx.description,
            )
            if _coverage_instruction:
                _existing_human = getattr(ctx, "human_instructions", "") or ""
                ctx = dataclasses.replace(
                    ctx,
                    human_instructions=_existing_human + _coverage_instruction,
                    previous_hash=ctx.context_hash,
                )
                logger.info(
                    "[Orchestrator] TestCoverageEnforcer: injected test generation "
                    "instruction for %d uncovered files (op=%s)",
                    _coverage_instruction.count("`"), ctx.op_id,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] TestCoverageEnforcer failed", exc_info=True)

        # ── JARVIS Tier 5: Cross-Domain Intelligence ──
        try:
            from backend.core.ouroboros.governance.jarvis_intelligence import (
                UnifiedIntelligenceLayer,
            )
            _intel = UnifiedIntelligenceLayer(orch._config.project_root)
            _syntheses = _intel.analyze_all_domains()
            _intel_prompt = _intel.format_for_prompt(_syntheses)
            if _intel_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _intel_prompt,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] JARVIS Tier 5: %d cross-domain syntheses injected",
                    len(_syntheses),
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Tier 5 injection failed", exc_info=True)

        # ── Phase 5 P5: AdversarialReviewer pre-GENERATE injection ──
        # Hook deferred from P5 Slice 5 graduation; wires the
        # adversarial-review pipeline into the orchestrator's post-
        # PLAN/pre-GENERATE site. Hook is best-effort + the underlying
        # service has 6 skip paths (master_off / safe_auto / empty_plan
        # / no_provider / provider_error / budget_exhausted), so this
        # NEVER raises and NEVER blocks. PLAN authority is preserved by
        # construction — the hook returns text only, never gates.
        try:
            from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
                review_plan_for_generate_injection,
            )
            _adv_plan_text = getattr(ctx, "implementation_plan", "") or ""
            _adv_risk_name = (
                ctx.risk_tier.name if ctx.risk_tier is not None else None
            )
            _adv_injection = review_plan_for_generate_injection(
                op_id=ctx.op_id,
                plan_text=_adv_plan_text,
                target_files=ctx.target_files or (),
                risk_tier_name=_adv_risk_name,
            )
            if _adv_injection.injection_text:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _adv_injection.injection_text,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] AdversarialReviewer: %d findings injected for "
                    "op=%s (bridge_fed=%s)",
                    len(_adv_injection.review.findings),
                    ctx.op_id,
                    _adv_injection.bridge_fed,
                )
            elif _adv_injection.review.was_skipped:
                logger.debug(
                    "[Orchestrator] AdversarialReviewer skipped for op=%s: %s",
                    ctx.op_id, _adv_injection.review.skip_reason,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug(
                "[Orchestrator] AdversarialReviewer hook injection failed",
                exc_info=True,
            )

        # ── JARVIS Tier 6: Personality voice line ── (reads _advisory from CLASSIFY)
        _gls = getattr(orch._stack, "governed_loop_service", None)
        if _gls is not None:
            _pe = getattr(_gls, "_personality_engine", None)
            if _pe is not None:
                try:
                    _chronic = getattr(_advisory, "chronic_entropy", 0.0) if _advisory else 0.0
                    _emerg = getattr(orch._stack, "_emergency_engine", None)
                    _emerg_lvl = _emerg.current_level.value if _emerg else 0
                    _state = _pe.compute_state(
                        success_rate=_pe.success_rate,
                        chronic_entropy=_chronic,
                        emergency_level=_emerg_lvl,
                    )
                    if orch._reasoning_narrator is not None:
                        _voice = _pe.get_voice_line(_state)
                        orch._reasoning_narrator.record_classify(
                            ctx.op_id, f"personality:{_state.value}", _voice,
                        )
                except Exception:
                    pass

        # ── Advanced Repair ──
        try:
            from backend.core.ouroboros.governance.advanced_repair import (
                HierarchicalFaultLocalizer, SlowFastThinkingRouter, DocAugmentedRepair,
            )
            _apr_blocks: list = []

            _localizer = HierarchicalFaultLocalizer(orch._config.project_root)
            _error_msg = getattr(ctx, "error_pattern", "") or ctx.description
            _locations = _localizer.localize(ctx.target_files, _error_msg)
            _loc_prompt = _localizer.format_for_prompt(_locations)
            if _loc_prompt:
                _apr_blocks.append(_loc_prompt)

            _thinking = SlowFastThinkingRouter.route(
                ctx.description, ctx.target_files,
            )
            _think_prompt = SlowFastThinkingRouter.format_for_prompt(_thinking)
            if _think_prompt:
                _apr_blocks.append(_think_prompt)

            _doc_repair = DocAugmentedRepair(orch._config.project_root)
            _doc_context = _doc_repair.generate_docs_for_repair(ctx.target_files)
            if _doc_context:
                _apr_blocks.append(_doc_context)

            if _apr_blocks:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                _apr_combined = "\n\n".join(_apr_blocks)
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _apr_combined,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Advanced repair: %d blocks (localization=%d locs, "
                    "thinking=%s, docs=%d chars) for op=%s",
                    len(_apr_blocks), len(_locations), _thinking.depth,
                    len(_doc_context), ctx.op_id,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Advanced repair injection failed", exc_info=True)

        # ── Self-Evolution P0 ──
        try:
            from backend.core.ouroboros.governance.self_evolution import (
                RuntimePromptAdapter, NegativeConstraintStore,
                CodeMetricsAnalyzer, MultiVersionEvolutionTracker,
            )
            from backend.core.ouroboros.governance.entropy_calculator import extract_domain_key as _edk

            _se_domain = _edk(ctx.target_files, ctx.description)
            _se_blocks: List[str] = []

            _prompt_adapter = RuntimePromptAdapter()
            _adapted = _prompt_adapter.get_adapted_instructions(_se_domain)
            if _adapted:
                _se_blocks.append(_adapted)

            _neg_store = NegativeConstraintStore()
            _neg_prompt = _neg_store.format_for_prompt(_se_domain)
            if _neg_prompt:
                _se_blocks.append(_neg_prompt)

            for _tf in ctx.target_files[:3]:
                _tf_path = orch._config.project_root / _tf
                if _tf_path.is_dir() or not _tf_path.suffix:
                    continue
                _metrics = CodeMetricsAnalyzer.analyze(_tf_path)
                if _metrics:
                    _mf = CodeMetricsAnalyzer.format_for_prompt(_metrics)
                    if _mf:
                        _se_blocks.append(_mf)

            if _se_blocks:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                _se_combined = "\n\n".join(_se_blocks)
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _se_combined,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Self-evolution: injected %d blocks for domain=%s",
                    len(_se_blocks), _se_domain,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Self-evolution injection failed", exc_info=True)

        # ── Self-Evolution P2 ──
        try:
            from backend.core.ouroboros.governance.self_evolution import (
                ModuleLevelMutator, RepositoryAutoDocumentation,
            )
            _se2_blocks: List[str] = []

            for _tf in ctx.target_files[:3]:
                _tf_path = orch._config.project_root / _tf
                if not _tf_path.is_file() or _tf_path.suffix != ".py":
                    continue
                _funcs = ModuleLevelMutator.list_functions(_tf_path)
                if _funcs:
                    _complex = [f for f in _funcs if f["complexity"] > 5]
                    if _complex:
                        _func_info = ", ".join(
                            f"{f['name']}(CC={f['complexity']}, L{f['start_line']}-{f['end_line']})"
                            for f in sorted(_complex, key=lambda x: x["complexity"], reverse=True)[:5]
                        )
                        _se2_blocks.append(
                            f"## Function-level analysis: {_tf}\n"
                            f"Complex functions (surgical mutation targets): {_func_info}\n"
                            f"Prefer modifying individual functions over full-file rewrites."
                        )

            _auto_doc = RepositoryAutoDocumentation()
            for _tf in ctx.target_files[:3]:
                _tf_path = orch._config.project_root / _tf
                if _tf_path.is_file() and _tf_path.suffix == ".py":
                    _auto_doc.scan_file(_tf_path)
            _doc_prompt = _auto_doc.format_for_prompt(
                [str(orch._config.project_root / tf) for tf in ctx.target_files[:3]]
            )
            if _doc_prompt:
                _se2_blocks.append(_doc_prompt)

            if _se2_blocks:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                _se2_combined = "\n\n".join(_se2_blocks)
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _se2_combined,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Self-evolution P2: injected %d blocks "
                    "(module analysis + doc gaps)",
                    len(_se2_blocks),
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Self-evolution P2 injection failed", exc_info=True)

        # ── Cooperative cancellation check (pre-GENERATE) ──
        if orch._is_cancel_requested(ctx.op_id):
            ctx = ctx.advance(OperationPhase.CANCELLED, terminal_reason_code="user_cancelled")
            await orch._record_ledger(ctx, OperationState.FAILED, {"reason": "user_cancelled"})
            await _capture_default_claims_at_plan_exit(
                ctx, exit_reason="user_cancelled",
            )
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason="user_cancelled",
            )
        # ---- end verbatim transcription ----

        # Phase 2 Slice 2.3 — synthesize + capture property claims
        # from the plan output. Audit-only: the planner's output is
        # not modified; we extract claims via a deterministic pure-
        # function synthesizer, persist them via Slice 1.3's
        # capture_phase_decision (each claim → one ledger record).
        # Closure-over-_plan_result means REPLAY mode does NOT alter
        # the planner; only the claim records are recorded/replayed.
        try:
            from backend.core.ouroboros.governance.verification.property_capture import (
                capture_claims,
                synthesize_claims_from_plan,
            )
            if (
                _plan_result is not None
                and not getattr(_plan_result, "skipped", False)
                and getattr(_plan_result, "plan_json", None)
            ):
                _plan_dict = _plan_result.plan_json
                if isinstance(_plan_dict, dict):
                    _claims = synthesize_claims_from_plan(
                        _plan_dict, op_id=ctx.op_id,
                    )
                    if _claims:
                        await capture_claims(
                            op_id=ctx.op_id,
                            claims=_claims,
                            ctx=ctx,
                        )
        except Exception:  # noqa: BLE001 — defensive
            # Capture failure does NOT propagate — the plan already
            # succeeded; claim audit is best-effort.
            logger.debug(
                "[Orchestrator] property_capture failed for PLAN; "
                "plan still applies",
                exc_info=True,
            )

        # Priority A Slice A3 — default must_hold claims fire on the
        # success path too, additive to whatever synthesize_claims_-
        # from_plan produced above. Trivial-op skips silently captured
        # zero claims pre-A3; this guarantees verification_postmortem
        # records have signal even when the LLM-reasoned plan is
        # absent.
        await _capture_default_claims_at_plan_exit(
            ctx, exit_reason="planned",
        )

        return PhaseResult(
            next_ctx=ctx,
            next_phase=OperationPhase.GENERATE,
            status="ok",
            reason="planned",
        )


__all__ = ["PLANRunner"]
