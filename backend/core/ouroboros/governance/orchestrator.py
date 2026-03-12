"""
Governed Pipeline Orchestrator
===============================

Central coordinator for the governed self-programming pipeline.  Ties
together the risk engine, candidate generator, approval provider, change
engine, and operation ledger into a single deterministic pipeline:

.. code-block:: text

    CLASSIFY -> ROUTE -> [CONTEXT_EXPANSION] -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE

The orchestrator owns **no domain logic** -- only phase transitions and
error handling.  Every code path ends in a terminal phase (COMPLETE,
CANCELLED, EXPIRED, or POSTMORTEM).

Key guarantees:
- All unhandled exceptions are caught and transition to POSTMORTEM
- Retries are bounded by ``OrchestratorConfig`` limits
- BLOCKED operations are short-circuited at CLASSIFY
- APPROVAL_REQUIRED operations pause at APPROVE and wait for human decision
- Ledger entries are recorded at every significant lifecycle event
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import tempfile
import time
from dataclasses import asdict as _dc_asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

from backend.core.ouroboros.governance.test_runner import BlockedPathError
from backend.core.ouroboros.governance.context_expander import ContextExpander
from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.change_engine import ChangeRequest
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
from backend.core.ouroboros.governance.learning_bridge import OperationOutcome
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskClassification,
    RiskTier,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy
from backend.core.ouroboros.governance.saga.cross_repo_verifier import CrossRepoVerifier
from backend.core.ouroboros.governance.saga.saga_types import RepoPatch, SagaTerminalState
from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult, PatchBenchmarker
from backend.core.ouroboros.integration import PerformanceRecord, TaskDifficulty

logger = logging.getLogger("Ouroboros.Orchestrator")


# ---------------------------------------------------------------------------
# OrchestratorConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorConfig:
    """Frozen configuration for the governed pipeline orchestrator.

    Parameters
    ----------
    project_root:
        Root directory of the project being modified (jarvis repo).
    repo_registry:
        Optional multi-repo registry. When set, cross-repo saga applies
        resolve each repo's local_path from the registry instead of using
        project_root for all repos. Defaults to None (single-repo mode).
    generation_timeout_s:
        Maximum seconds for candidate generation (per attempt).
    validation_timeout_s:
        Maximum seconds for candidate validation (per attempt).
    approval_timeout_s:
        Maximum seconds to wait for human approval.
    max_generate_retries:
        Number of additional generation attempts after the first failure.
    max_validate_retries:
        Number of additional validation attempts after the first failure.
    """

    project_root: Path
    repo_registry: Optional["RepoRegistry"] = None  # Forward ref avoids circular import; resolved at type-check time
    generation_timeout_s: float = 120.0
    validation_timeout_s: float = 60.0
    approval_timeout_s: float = 600.0
    max_generate_retries: int = 1
    max_validate_retries: int = 2
    context_expansion_enabled: bool = True
    context_expansion_timeout_s: float = 30.0

    # Saga message bus (passive observability — created by GLS at startup)
    message_bus: Optional[Any] = None

    # Benchmarking
    benchmark_enabled: bool = True
    benchmark_timeout_s: float = 60.0

    # Model attribution
    model_attribution_enabled: bool = True
    model_attribution_lookback_n: int = 20
    model_attribution_min_sample_size: int = 3

    # Curriculum
    curriculum_enabled: bool = True
    curriculum_publish_interval_s: float = 3600.0
    curriculum_window_n: int = 50
    curriculum_top_k: int = 5
    curriculum_impact_weights: Dict[str, float] = field(default_factory=dict)

    # Reactor event polling
    reactor_event_poll_interval_s: float = 30.0

    # L2 self-repair engine (disabled by default)
    # Set by GovernedLoopService._build_components() when JARVIS_L2_ENABLED=true.
    repair_engine: Optional[Any] = None

    def resolve_repo_roots(
        self,
        repo_scope: Tuple[str, ...],
        op_id: str,
    ) -> Dict[str, Path]:
        """Resolve per-repo filesystem roots from registry; fallback to project_root.

        Parameters
        ----------
        repo_scope:
            Tuple of repo names from OperationContext.
        op_id:
            Operation ID for structured warning on missing registry keys.

        Returns
        -------
        Dict mapping repo name -> absolute Path.
        Missing keys fall back to project_root with a warning (never raise).
        """
        roots: Dict[str, Path] = {}
        for repo in repo_scope:
            if self.repo_registry is not None:
                try:
                    roots[repo] = Path(self.repo_registry.get(repo).local_path)
                except (KeyError, AttributeError, TypeError):
                    # repo_registry may be a duck-typed substitute; catch all lookup failures
                    logger.warning(
                        "[OrchestratorConfig] repo=%s not in registry for op_id=%s; "
                        "falling back to project_root=%s",
                        repo, op_id, self.project_root,
                    )
                    roots[repo] = self.project_root
            else:
                roots[repo] = self.project_root
        return roots


# ---------------------------------------------------------------------------
# GovernedOrchestrator
# ---------------------------------------------------------------------------


class GovernedOrchestrator:
    """Central coordinator for the governed self-programming pipeline.

    Delegates to existing governance components (risk_engine, change_engine,
    ledger, canary via can_write).  Owns NO domain logic -- only phase
    transitions and error handling.

    Parameters
    ----------
    stack:
        GovernanceStack providing risk_engine, ledger, comm, change_engine,
        and the can_write() gate.
    generator:
        CandidateGenerator for code generation (has generate(context, deadline)).
    approval_provider:
        Optional ApprovalProvider for human-in-the-loop gate (has request(),
        await_decision()).
    config:
        Orchestrator configuration.
    """

    def __init__(
        self,
        stack: Any,
        generator: Any,
        approval_provider: Any,
        config: OrchestratorConfig,
        validation_runner: Any = None,  # LanguageRouter | duck-typed for testing
    ) -> None:
        self._stack = stack
        self._generator = generator
        self._approval_provider = approval_provider
        self._config = config
        self._validation_runner = validation_runner
        self._oracle_update_lock: asyncio.Lock = asyncio.Lock()

    async def run(self, ctx: OperationContext) -> OperationContext:
        """Execute the full governed pipeline, returning the terminal context.

        Top-level try/except catches ALL unhandled exceptions and transitions
        to POSTMORTEM.  Every code path ends in a terminal phase (COMPLETE,
        CANCELLED, EXPIRED, or POSTMORTEM).

        Parameters
        ----------
        ctx:
            The initial OperationContext in CLASSIFY phase.

        Returns
        -------
        OperationContext
            The terminal context after pipeline completion or failure.
        """
        try:
            return await self._run_pipeline(ctx)
        except Exception as exc:
            logger.error(
                "Unhandled exception in pipeline for %s: %s",
                ctx.op_id,
                exc,
                exc_info=True,
            )
            # Try to advance to POSTMORTEM from current phase.
            # If we can't (e.g. already terminal), just return ctx.
            try:
                ctx = ctx.advance(OperationPhase.POSTMORTEM)
            except ValueError:
                # POSTMORTEM not legal from this phase — fall back to CANCELLED
                # (legal from all non-terminal phases except VERIFY).
                try:
                    ctx = ctx.advance(OperationPhase.CANCELLED)
                except ValueError:
                    pass  # Already terminal — safe to return as-is
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"error": str(exc), "phase": ctx.phase.name},
            )
            return ctx

    # ------------------------------------------------------------------
    # Pipeline implementation
    # ------------------------------------------------------------------

    async def _run_pipeline(self, ctx: OperationContext) -> OperationContext:
        """Internal pipeline logic -- phases 1 through 8."""

        # ---- Phase 1: CLASSIFY ----
        profile = self._build_profile(ctx)
        classification = self._stack.risk_engine.classify(profile)
        risk_tier = classification.tier

        if risk_tier is RiskTier.BLOCKED:
            ctx = ctx.advance(OperationPhase.CANCELLED, risk_tier=risk_tier)
            await self._record_ledger(
                ctx,
                OperationState.BLOCKED,
                {
                    "reason_code": classification.reason_code,
                    "risk_tier": risk_tier.name,
                },
            )
            return ctx

        # Announce operation start — VoiceNarrator fires here (INTENT type)
        try:
            await self._stack.comm.emit_intent(
                op_id=ctx.op_id,
                goal=ctx.description,
                target_files=list(ctx.target_files),
                risk_tier=risk_tier.name,
                blast_radius=len(ctx.target_files),
            )
        except Exception:
            logger.debug("emit_intent failed for op=%s", ctx.op_id, exc_info=True)

        # Advance to ROUTE with risk_tier set
        ctx = ctx.advance(OperationPhase.ROUTE, risk_tier=risk_tier)

        # ---- Phase 2: ROUTE ----
        if self._config.context_expansion_enabled:
            ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)

            # ---- Phase 2b: CONTEXT_EXPANSION ----
            try:
                expansion_deadline = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=self._config.context_expansion_timeout_s
                )
                expander = ContextExpander(
                    generator=self._generator,
                    repo_root=self._config.project_root,
                    oracle=getattr(self._stack, "oracle", None),
                )
                ctx = await asyncio.wait_for(
                    expander.expand(ctx, expansion_deadline),
                    timeout=self._config.context_expansion_timeout_s,
                )
            except Exception as exc:
                logger.warning(
                    "[Orchestrator] Context expansion failed for op=%s: %s; "
                    "continuing to GENERATE",
                    ctx.op_id, exc,
                )

            ctx = ctx.advance(OperationPhase.GENERATE)
        else:
            # Expansion disabled: skip directly from ROUTE to GENERATE
            ctx = ctx.advance(OperationPhase.GENERATE)

        # ---- Phase 3: GENERATE (with retry) ----
        generation: Optional[GenerationResult] = None
        generate_retries_remaining = self._config.max_generate_retries

        for attempt in range(1 + self._config.max_generate_retries):
            try:
                deadline = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=self._config.generation_timeout_s
                )
                generation = await self._generator.generate(ctx, deadline)

                # is_noop=True means the model signalled the change is already present.
                # Empty candidates is correct in this case — do not treat as a failure.
                if generation is not None and generation.is_noop:
                    break

                if generation is None or len(generation.candidates) == 0:
                    generation = None
                    raise RuntimeError("no_candidates_returned")

                # Success -- break out of retry loop
                break

            except Exception as exc:
                logger.warning(
                    "Generation attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    1 + self._config.max_generate_retries,
                    ctx.op_id,
                    exc,
                )
                generate_retries_remaining -= 1
                if generate_retries_remaining < 0:
                    # All retries exhausted
                    ctx = ctx.advance(OperationPhase.CANCELLED)
                    await self._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {"reason": "generation_failed", "error": str(exc)},
                    )
                    return ctx
                # Retry: advance to GENERATE_RETRY
                ctx = ctx.advance(OperationPhase.GENERATE_RETRY)

        assert generation is not None  # guaranteed by loop logic

        # L1: emit tool execution audit records to ledger stream.
        # This runs BEFORE the noop guard so that tool records are always
        # persisted regardless of whether the response was a noop.
        for _rec in generation.tool_execution_records:
            try:
                _entry = LedgerEntry(
                    op_id=ctx.op_id,
                    state=OperationState.SANDBOXING,
                    data={"kind": "tool_exec.v1", **_dc_asdict(_rec)},
                    entry_id=_rec.call_id,
                )
                await self._stack.ledger.append(_entry)
            except asyncio.CancelledError:
                raise
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "tool_exec ledger emit failed op=%s record=%s: %s",
                    ctx.op_id, getattr(_rec, "call_id", "?"), _exc,
                )  # ledger failure must never abort governance pipeline

        # Short-circuit: model signalled the change is already present
        if generation.is_noop:
            logger.info(
                "[Orchestrator] op=%s is_noop=True (provider=%s) — skipping APPLY",
                ctx.op_id,
                generation.provider_name,
            )
            ctx = ctx.advance(OperationPhase.COMPLETE, generation=generation)
            await self._record_ledger(
                ctx,
                OperationState.APPLIED,
                {"reason": "noop", "provider": generation.provider_name},
            )
            return ctx

        # Store generation result in context
        ctx = ctx.advance(OperationPhase.VALIDATE, generation=generation)

        # ---- Phase 4: VALIDATE ----
        best_candidate: Optional[Dict[str, Any]] = None
        best_validation: Optional[ValidationResult] = None
        validate_retries_remaining = self._config.max_validate_retries

        for _ in range(1 + self._config.max_validate_retries):
            # Compute remaining budget from pipeline_deadline
            if ctx.pipeline_deadline is not None:
                remaining_s = (
                    ctx.pipeline_deadline - datetime.now(tz=timezone.utc)
                ).total_seconds()
            else:
                remaining_s = self._config.validation_timeout_s  # fallback

            if remaining_s <= 0.0:
                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "validation_budget_exhausted"},
                )
                return ctx

            # Try each candidate; pick first that passes
            for candidate in generation.candidates:
                _t_validate_start = time.monotonic()
                validation = await self._run_validation(ctx, candidate, remaining_s)
                _validate_duration_s = time.monotonic() - _t_validate_start

                # Per-candidate ledger entry — always, pass or fail
                await self._record_ledger(ctx, OperationState.GATING, {
                    "event": "candidate_validated",
                    "candidate_id": candidate.get("candidate_id", "unknown"),
                    "candidate_hash": candidate.get("candidate_hash", ""),
                    "validation_outcome": "pass" if validation.passed else "fail",
                    "failure_class": validation.failure_class,
                    "duration_s": round(_validate_duration_s, 3),
                    "provider": generation.provider_name,
                    "model": getattr(generation, "model_id", ""),
                })

                if validation.passed:
                    best_candidate = candidate
                    best_validation = validation
                    break

                # Infra failure: non-retryable — escalate immediately
                if validation.failure_class == "infra":
                    ctx = ctx.advance(OperationPhase.POSTMORTEM, validation=validation)
                    await self._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {
                            "reason": "validation_infra_failure",
                            "failure_class": "infra",
                            "adapter_names_run": list(validation.adapter_names_run),
                            "validation_duration_s": validation.validation_duration_s,
                            "short_summary": validation.short_summary,
                        },
                    )
                    return ctx

                # Budget failure: non-retryable
                if validation.failure_class == "budget":
                    ctx = ctx.advance(OperationPhase.CANCELLED, validation=validation)
                    await self._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {"reason": "validation_budget_exhausted"},
                    )
                    return ctx

                # test/build failure: track for ledger; try next candidate
                best_validation = validation

            if best_candidate is not None:
                break  # at least one candidate passed

            # All candidates failed this attempt
            validate_retries_remaining -= 1
            if validate_retries_remaining < 0:
                # ── L2 self-repair dispatch ───────────────────────────────────
                if self._config.repair_engine is not None and best_validation is not None:
                    _pl_deadline = ctx.pipeline_deadline or (
                        datetime.now(timezone.utc) + timedelta(seconds=self._config.generation_timeout_s)
                    )
                    directive = await self._l2_hook(ctx, best_validation, _pl_deadline)
                    if directive[0] == "break":
                        best_candidate, best_validation = directive[1], directive[2]
                        break  # fall through to GATE
                    elif directive[0] in ("cancel", "fatal"):
                        return directive[1]  # ctx was advanced inside _l2_hook
                # ── end L2 dispatch ───────────────────────────────────────────

                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason_code": "no_candidate_valid",
                        "candidates_tried": [
                            c.get("candidate_id", "?") for c in generation.candidates
                        ],
                        "failure_class": best_validation.failure_class if best_validation else "test",
                        "adapter_names_run": list(best_validation.adapter_names_run) if best_validation else [],
                        "validation_duration_s": best_validation.validation_duration_s if best_validation else 0.0,
                        "short_summary": best_validation.short_summary if best_validation else "",
                    },
                )
                return ctx

            # Retry: advance to VALIDATE_RETRY
            ctx = ctx.advance(OperationPhase.VALIDATE_RETRY)

        assert best_candidate is not None  # guaranteed by loop logic
        assert best_validation is not None

        # Source-drift check: file must not have changed since generation
        drift_hash = self._check_source_drift(best_candidate, self._config.project_root)
        if drift_hash is not None:
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason_code": "source_drift_detected",
                "file_path": best_candidate.get("file_path"),
                "expected_source_hash": best_candidate.get("source_hash"),
                "actual_source_hash": drift_hash,
            })
            return ctx

        # Winner traceability ledger entry
        await self._record_ledger(ctx, OperationState.GATING, {
            "event": "validation_complete",
            "winning_candidate_id": best_candidate.get("candidate_id"),
            "winning_candidate_hash": best_candidate.get("candidate_hash"),
            "winning_file_path": best_candidate.get("file_path"),
            "source_hash": best_candidate.get("source_hash"),
            "source_path": best_candidate.get("source_path"),
            "provider": generation.provider_name,
            "model": getattr(generation, "model_id", ""),
            "total_candidates_tried": len(generation.candidates),
        })

        # Store compact validation result in context; full output is in ledger
        ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)

        # ---- Phase 5: GATE ----
        allowed, reason = self._stack.can_write(
            {"files": list(ctx.target_files)}
        )
        if not allowed:
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._record_ledger(
                ctx,
                OperationState.BLOCKED,
                {"reason": f"gate_blocked:{reason}"},
            )
            return ctx

        # Autonomy tier gate: frozen at submit() to prevent TrustGraduator race.
        # "observe" → force APPROVAL_REQUIRED regardless of risk_tier.
        _frozen_tier = getattr(ctx, "frozen_autonomy_tier", "governed")
        if _frozen_tier == "observe" and risk_tier is not RiskTier.APPROVAL_REQUIRED:
            risk_tier = RiskTier.APPROVAL_REQUIRED
            logger.info(
                "[Orchestrator] GATE: frozen_tier=observe → APPROVAL_REQUIRED; op=%s",
                ctx.op_id,
            )

        # ---- Phase 6: APPROVE (conditional) ----
        if risk_tier is RiskTier.APPROVAL_REQUIRED:
            if self._approval_provider is None:
                # No approval provider available -> CANCELLED
                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "approval_required_but_no_provider"},
                )
                return ctx

            ctx = ctx.advance(OperationPhase.APPROVE)
            await self._record_ledger(
                ctx,
                OperationState.GATING,
                {"waiting_approval": True, "risk_tier": risk_tier.name},
            )

            # Notify via comm channel (TUI + voice will receive this)
            try:
                await self._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id,
                    phase="approve",
                    progress_pct=0.0,
                )
            except Exception:
                logger.debug(
                    "Comm heartbeat failed for op=%s", ctx.op_id, exc_info=True
                )

            request_id = await self._approval_provider.request(ctx)
            decision: ApprovalResult = await self._approval_provider.await_decision(
                request_id, self._config.approval_timeout_s
            )

            if decision.status is ApprovalStatus.EXPIRED:
                ctx = ctx.advance(OperationPhase.EXPIRED)
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "approval_expired"},
                )
                return ctx

            if decision.status is ApprovalStatus.REJECTED:
                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "approval_rejected",
                        "approver": decision.approver,
                    },
                )
                return ctx

            # APPROVED -- continue to APPLY

        # ---- Phase 7: APPLY ----
        ctx = ctx.advance(OperationPhase.APPLY)

        # Cross-repo saga path
        if ctx.cross_repo:
            return await self._execute_saga_apply(ctx, best_candidate)

        # Capture pre-apply snapshots for complexity baseline
        snapshots: Dict[str, str] = {}
        for f in ctx.target_files:
            fpath = self._config.project_root / f
            if fpath.exists():
                try:
                    snapshots[str(f)] = fpath.read_text(errors="replace")
                except OSError:
                    pass
        if snapshots:
            ctx = ctx.with_pre_apply_snapshots(snapshots)

        change_request = self._build_change_request(ctx, best_candidate)

        _t_apply = time.monotonic()
        try:
            change_result = await self._stack.change_engine.execute(change_request)
        except Exception as exc:
            logger.error(
                "Change engine raised for %s: %s", ctx.op_id, exc
            )
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": "change_engine_error", "error": str(exc)},
            )
            self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply)
            await self._publish_outcome(ctx, OperationState.FAILED, "change_engine_error")
            return ctx

        if not change_result.success:
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {
                    "reason": "change_engine_failed",
                    "rolled_back": change_result.rolled_back,
                },
            )
            self._record_canary_for_ctx(
                ctx, False, time.monotonic() - _t_apply,
                rolled_back=change_result.rolled_back,
            )
            await self._publish_outcome(ctx, OperationState.FAILED, "change_engine_failed")
            return ctx

        # ---- Phase 8: VERIFY ----
        ctx = ctx.advance(OperationPhase.VERIFY)
        await self._record_ledger(
            ctx,
            OperationState.APPLIED,
            {"op_id": ctx.op_id},
        )
        ctx = await self._run_benchmark(ctx, [])
        ctx = ctx.advance(OperationPhase.COMPLETE)
        self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
        await self._publish_outcome(ctx, OperationState.APPLIED)
        await self._persist_performance_record(ctx)
        applied_files = [Path(p).resolve() for p in ctx.target_files]
        await self._oracle_incremental_update(applied_files)
        return ctx

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_canary_for_ctx(
        self,
        ctx: OperationContext,
        success: bool,
        latency_s: float,
        rolled_back: bool = False,
    ) -> None:
        """Record canary telemetry for every file in ctx.target_files."""
        for f in ctx.target_files:
            self._stack.canary.record_operation(
                file_path=str(f),
                success=success,
                latency_s=latency_s,
                rolled_back=rolled_back,
            )

    async def _publish_outcome(
        self,
        ctx: OperationContext,
        final_state: OperationState,
        error_pattern: Optional[str] = None,
    ) -> None:
        """Publish operation outcome to LearningBridge. Fault-isolated -- never raises."""
        if self._stack.learning_bridge is None:
            return
        try:
            outcome = OperationOutcome(
                op_id=ctx.op_id,
                goal=ctx.description,
                target_files=list(ctx.target_files),
                final_state=final_state,
                error_pattern=error_pattern,
            )
            await self._stack.learning_bridge.publish(outcome)
        except Exception:
            logger.exception(
                "[Orchestrator] LearningBridge.publish failed for op %s; outcome not recorded",
                ctx.op_id,
            )

    async def _run_benchmark(
        self,
        ctx: OperationContext,
        applied_files: Sequence[Path],
    ) -> OperationContext:
        """Run PatchBenchmarker. Fault-isolated — never raises, never alters terminal state."""
        if not self._config.benchmark_enabled:
            return ctx
        try:
            benchmarker = PatchBenchmarker(
                project_root=self._config.project_root,
                timeout_s=self._config.benchmark_timeout_s,
                pre_apply_snapshots=getattr(ctx, "pre_apply_snapshots", {}),
            )
            result = await asyncio.wait_for(
                benchmarker.benchmark(ctx),
                timeout=self._config.benchmark_timeout_s,
            )
            return ctx.with_benchmark_result(result)
        except asyncio.CancelledError:
            logger.debug(
                "[Orchestrator] Benchmark cancelled for op=%s; continuing without metrics",
                ctx.op_id,
            )
            return ctx
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Benchmark failed for op=%s: %s; continuing without metrics",
                ctx.op_id, exc,
            )
            return ctx

    async def _persist_performance_record(self, ctx: OperationContext) -> None:
        """Write PerformanceRecord to persistence. Fault-isolated — never raises."""
        if self._stack.performance_persistence is None:
            return
        try:
            br = getattr(ctx, "benchmark_result", None)
            record = PerformanceRecord(
                model_id=getattr(ctx, "model_id", None) or "unknown",
                task_type=br.task_type if br else "code_improvement",
                difficulty=getattr(ctx, "difficulty", TaskDifficulty.MODERATE),
                success=ctx.phase == OperationPhase.COMPLETE,
                latency_ms=getattr(ctx, "elapsed_ms", 0.0),
                iterations_used=getattr(ctx, "iterations_used", 1),
                code_quality_score=br.quality_score if br else 0.0,
                op_id=ctx.op_id,
                patch_hash=br.patch_hash if br else "",
                pass_rate=br.pass_rate if br else 0.0,
                lint_violations=br.lint_violations if br else 0,
                coverage_pct=br.coverage_pct if br else 0.0,
                complexity_delta=br.complexity_delta if br else 0.0,
            )
            await self._stack.performance_persistence.save_record(record)
        except Exception as exc:
            logger.warning(
                "[Orchestrator] PerformanceRecord persist failed for op=%s: %s",
                ctx.op_id, exc,
            )

    async def _oracle_incremental_update(
        self,
        applied_files: Sequence[Path],
    ) -> None:
        """Notify Oracle of changed files after successful COMPLETE. Fault-isolated — never raises."""
        oracle = getattr(self._stack, "oracle", None)
        if oracle is None:
            return
        try:
            async with self._oracle_update_lock:
                await asyncio.wait_for(
                    oracle.incremental_update(applied_files),
                    timeout=30.0,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "[Orchestrator] Oracle incremental_update timed out (>30s); "
                "background oracle loop still running"
            )
        except asyncio.CancelledError:
            pass  # swallow — oracle update is non-blocking; don't abort COMPLETE
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Oracle incremental_update failed: %s", exc
            )

    def _build_profile(self, ctx: OperationContext) -> OperationProfile:
        """Build an OperationProfile from the context's target files.

        Uses conservative defaults for blast radius and security surface
        detection since the orchestrator doesn't have deep code analysis.
        Real implementations would enrich this via blast-radius adapters.
        """
        target_paths = [Path(f) for f in ctx.target_files]

        # Conservative heuristics for profile fields
        touches_supervisor = any(
            "supervisor" in str(p).lower() for p in target_paths
        )
        touches_security = any(
            any(kw in str(p).lower() for kw in ("auth", "secret", "cred", "token", "encrypt"))
            for p in target_paths
        )
        is_core = any(
            any(kw in str(p).lower() for kw in ("router", "controller", "engine", "orchestrator"))
            for p in target_paths
        )

        return OperationProfile(
            files_affected=target_paths,
            change_type=ChangeType.MODIFY,
            blast_radius=len(target_paths),
            crosses_repo_boundary=False,
            touches_security_surface=touches_security,
            touches_supervisor=touches_supervisor,
            test_scope_confidence=0.8,
            is_dependency_change=False,
            is_core_orchestration_path=is_core,
        )

    @staticmethod
    def _ast_preflight(content: str) -> Optional[str]:
        """Return a short error string if content fails ast.parse, else None.

        Parameters
        ----------
        content:
            Python source code to parse.

        Returns
        -------
        Optional[str]
            ``None`` if the content parses cleanly, or a human-readable error
            string (e.g. ``"SyntaxError: invalid syntax (<unknown>, line 1)"``).
        """
        try:
            ast.parse(content)
            return None
        except SyntaxError as exc:
            return f"SyntaxError: {exc}"

    @staticmethod
    def _check_source_drift(
        candidate: Dict[str, Any],
        project_root: Path,
    ) -> Optional[str]:
        """Return None if source unchanged; return current hash if drift detected.

        Compares candidate["source_hash"] (hash at generation time) against the
        current file content hash.  Returns None if no source_hash recorded
        (skip check) or file not found (let APPLY handle).

        Parameters
        ----------
        candidate:
            Candidate dict containing ``source_hash`` (hash at generation time)
            and ``file_path`` (relative path from project root).
        project_root:
            Root directory of the project being modified.

        Returns
        -------
        Optional[str]
            ``None`` if no drift (source unchanged or check skipped), or the
            current file's SHA-256 hex digest if drift was detected.
        """
        import hashlib as _hl
        source_hash = candidate.get("source_hash", "")
        if not source_hash:
            return None  # nothing to compare — skip
        file_path = project_root / candidate.get("file_path", "")
        try:
            current_content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None  # file not found — let APPLY handle
        current_hash = _hl.sha256(current_content.encode()).hexdigest()
        return current_hash if current_hash != source_hash else None

    async def _l2_hook(
        self,
        ctx: "OperationContext",
        best_validation: "ValidationResult",
        deadline: datetime,
    ) -> tuple:
        """Run the L2 repair engine; return a directive tuple to the caller.

        Returns:
            ("break", candidate, canonical_val)  → L2 converged; caller breaks to GATE
            ("cancel", ctx)                      → L2 stopped or canonical validate failed; ctx is advanced
            ("fatal", ctx)                       → non-CancelledError exception; ctx is advanced
        Raises:
            asyncio.CancelledError — if engine.run() was cancelled (POSTMORTEM recorded first)
        """
        try:
            l2_result = await self._config.repair_engine.run(ctx, best_validation, deadline)
        except asyncio.CancelledError:
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(ctx, OperationState.FAILED, {"reason": "l2_cancelled"})
            raise
        except Exception as exc:
            logger.error("[Orchestrator] L2 engine error: %s", exc, exc_info=True)
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(ctx, OperationState.FAILED,
                {"reason": f"l2_fatal:{type(exc).__name__}"})
            return ("fatal", ctx)

        if l2_result.terminal == "L2_CONVERGED" and l2_result.candidate is not None:
            _remaining_s = (deadline - datetime.now(timezone.utc)).total_seconds()
            canonical_val = await self._run_validation(ctx, l2_result.candidate, _remaining_s)
            if canonical_val.passed:
                await self._record_ledger(ctx, OperationState.SANDBOXING, {
                    "event": "l2_converged",
                    "iterations": len(l2_result.iterations),
                    **l2_result.summary,
                })
                return ("break", l2_result.candidate, canonical_val)
            else:
                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(ctx, OperationState.FAILED, {
                    "reason": "l2_canonical_validate_failed",
                    **l2_result.summary,
                })
                return ("cancel", ctx)

        elif l2_result.terminal == "L2_STOPPED":
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason": "l2_stopped",
                "stop_reason": l2_result.stop_reason,
                **l2_result.summary,
            })
            return ("cancel", ctx)

        else:  # L2_CONVERGED with no candidate (shouldn't happen in practice)
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason": "l2_no_candidate",
                **l2_result.summary,
            })
            return ("fatal", ctx)

    async def _run_validation(
        self,
        ctx: OperationContext,
        candidate: Dict[str, Any],
        remaining_s: float,
    ) -> ValidationResult:
        """Run the full validation pipeline for a single candidate.

        Steps:
          1. AST preflight (fast, no subprocess)
          2. Budget guard (remaining_s <= 0 → budget failure)
          3. Write candidate to temp sandbox dir
          4. validation_runner.run() with op_id continuity
          5. Map MultiAdapterResult → compact ValidationResult

        The full adapter stdout/stderr is recorded in the ledger separately;
        ValidationResult holds only a ≤300-char summary.

        Parameters
        ----------
        ctx:
            Current operation context (used for op_id tracing).
        candidate:
            Candidate dict with ``file`` and ``content`` keys.
        remaining_s:
            Remaining pipeline budget in seconds.

        Returns
        -------
        ValidationResult
            Compact, immutable result suitable for embedding in the context.
        """
        content = candidate.get("full_content", "")
        target_file_str = candidate.get(
            "file_path",
            str(ctx.target_files[0]) if ctx.target_files else "unknown.py",
        )

        # Step 1: AST preflight — fast gate, no subprocess (Python files only)
        syntax_error = self._ast_preflight(content) if target_file_str.endswith(".py") else None
        if syntax_error:
            return ValidationResult(
                passed=False,
                best_candidate=None,
                validation_duration_s=0.0,
                error=syntax_error,
                failure_class="build",
                short_summary=syntax_error[:300],
                adapter_names_run=(),
            )

        # Non-code files (docs, configs, etc.) need no test/syntax runner
        _RUNNABLE_EXTENSIONS = {".py", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp"}
        if Path(target_file_str).suffix not in _RUNNABLE_EXTENSIONS:
            return ValidationResult(
                passed=True,
                best_candidate=candidate,
                validation_duration_s=0.0,
                error=None,
                failure_class=None,
                short_summary="validation skipped: non-code file",
                adapter_names_run=(),
            )

        # When no runner is configured, skip test execution (dry-run / test mode)
        if self._validation_runner is None:
            return ValidationResult(
                passed=True,
                best_candidate=candidate,
                validation_duration_s=0.0,
                error=None,
                failure_class=None,
                short_summary="validation skipped: no runner configured",
                adapter_names_run=(),
            )

        # Step 2: Budget guard
        if remaining_s <= 0.0:
            return ValidationResult(
                passed=False,
                best_candidate=None,
                validation_duration_s=0.0,
                error="pipeline budget exhausted before validation",
                failure_class="budget",
                short_summary="Budget exhausted",
                adapter_names_run=(),
            )

        # Step 3: Write to temp sandbox
        multi = None
        t0 = time.monotonic()
        target_path = Path(target_file_str)
        target_name = target_path.name

        with tempfile.TemporaryDirectory(prefix="ouroboros_validate_") as sandbox_str:
            sandbox = Path(sandbox_str)
            sandbox_file = sandbox / target_name
            sandbox_file.write_text(content, encoding="utf-8")

            # Step 4: Run LanguageRouter (or any duck-typed runner)
            try:
                multi = await self._validation_runner.run(
                    changed_files=(sandbox_file,),
                    sandbox_dir=sandbox,
                    timeout_budget_s=remaining_s,
                    op_id=ctx.op_id,
                )
            except BlockedPathError as exc:
                # Security gate rejection → failure_class="security" → CANCELLED (not POSTMORTEM)
                return ValidationResult(
                    passed=False,
                    best_candidate=None,
                    validation_duration_s=time.monotonic() - t0,
                    error=str(exc),
                    failure_class="security",
                    short_summary=f"BlockedPathError: {str(exc)[:280]}",
                    adapter_names_run=(),
                )
            except Exception as exc:
                return ValidationResult(
                    passed=False,
                    best_candidate=None,
                    validation_duration_s=time.monotonic() - t0,
                    error=str(exc),
                    failure_class="infra",
                    short_summary=f"runner exception: {str(exc)[:200]}",
                    adapter_names_run=(),
                )

        # Step 5: Map to compact ValidationResult (sandbox dir is now cleaned up)
        assert multi is not None
        duration = time.monotonic() - t0
        adapter_names = tuple(r.adapter for r in multi.adapter_results)
        summary_parts = []
        for r in multi.adapter_results:
            tail = (r.test_result.stdout or "")[-150:] if r.test_result else ""
            summary_parts.append(f"[{r.adapter}:{'PASS' if r.passed else 'FAIL'}] {tail}")
        short_summary = " | ".join(summary_parts)[:300]

        return ValidationResult(
            passed=multi.passed,
            best_candidate=candidate if multi.passed else None,
            validation_duration_s=duration,
            error=None if multi.passed else f"validation failed: {multi.failure_class}",
            failure_class=None if multi.passed else multi.failure_class,
            short_summary=short_summary,
            adapter_names_run=adapter_names,
        )

    def _build_change_request(
        self, ctx: OperationContext, candidate: Dict[str, Any]
    ) -> ChangeRequest:
        """Build a ChangeRequest from the context and best candidate.

        Parameters
        ----------
        ctx:
            The current operation context.
        candidate:
            The validated candidate dict with ``file`` and ``content`` keys.
        """
        target_file = Path(
            candidate.get("file_path", str(ctx.target_files[0] if ctx.target_files else "unknown.py"))
        )
        proposed_content = candidate.get("full_content", "")

        profile = self._build_profile(ctx)

        return ChangeRequest(
            goal=ctx.description,
            target_file=target_file,
            proposed_content=proposed_content,
            profile=profile,
            op_id=ctx.op_id,
        )

    async def _execute_saga_apply(
        self,
        ctx: OperationContext,
        best_candidate: dict,
    ) -> OperationContext:
        """Execute multi-repo saga apply + three-tier verify.

        Selected when ctx.cross_repo is True. Single-repo path is unchanged.
        """
        # Build patch_map from best_candidate["patches"] or fall back to empty per-repo patches
        patch_map: Dict[str, RepoPatch] = {}
        if best_candidate and "patches" in best_candidate:
            patch_map = best_candidate["patches"]
        else:
            for repo in ctx.repo_scope:
                patch_map[repo] = RepoPatch(repo=repo, files=())

        # Resolve per-repo filesystem roots from registry (fallback to project_root)
        repo_roots = self._config.resolve_repo_roots(
            repo_scope=ctx.repo_scope,
            op_id=ctx.op_id,
        )

        strategy = SagaApplyStrategy(
            repo_roots=repo_roots,
            ledger=self._stack.ledger,
            message_bus=getattr(self._config, "message_bus", None),
            branch_isolation=os.environ.get(
                "JARVIS_SAGA_BRANCH_ISOLATION", "false"
            ).lower() in ("1", "true", "yes"),
            keep_failed_saga_branches=os.environ.get(
                "JARVIS_SAGA_KEEP_FORENSICS_BRANCHES", "true"
            ).lower() in ("1", "true", "yes"),
        )
        _t_saga = time.monotonic()
        apply_result = await strategy.execute(ctx, patch_map)

        if apply_result.terminal_state == SagaTerminalState.SAGA_ABORTED:
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id},
            )
            self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
            await self._publish_outcome(ctx, OperationState.FAILED, apply_result.reason_code)
            return ctx

        if apply_result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED:
            verifier = CrossRepoVerifier(
                repo_roots=repo_roots,
            )
            verify_result = await verifier.verify(
                repo_scope=ctx.repo_scope,
                patch_map=patch_map,
                dependency_edges=ctx.dependency_edges,
            )

            if not verify_result.passed:
                comp_ok = await strategy.compensate_after_verify_failure(
                    saga_result=apply_result,
                    patch_map=patch_map,
                    op_id=ctx.op_id,
                    reason_code=verify_result.reason_code,
                )
                # Emit SAGA_FAILED to bus if available
                _bus = getattr(strategy, "_bus", None)
                if _bus is not None:
                    try:
                        from backend.core.ouroboros.governance.autonomy.saga_messages import (
                            SagaMessage, SagaMessageType, MessagePriority,
                        )
                        _bus.send(SagaMessage(
                            message_type=SagaMessageType.SAGA_FAILED,
                            saga_id=apply_result.saga_id,
                            correlation_id=apply_result.saga_id,
                            priority=MessagePriority.HIGH,
                            payload={
                                "schema_version": "1.0",
                                "op_id": ctx.op_id,
                                "saga_id": apply_result.saga_id,
                                "reason_code": "verify_failed",
                                "failed_phase": "VERIFY",
                            },
                        ))
                    except Exception:
                        pass
                ctx = ctx.advance(OperationPhase.POSTMORTEM)
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": verify_result.reason_code,
                        "saga_id": apply_result.saga_id,
                        "compensated": comp_ok,
                    },
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
                await self._publish_outcome(ctx, OperationState.FAILED, verify_result.reason_code)
                return ctx

            # B+ mode: promote ephemeral branches before declaring success
            promote_state, promoted_shas = await strategy.promote_all(
                apply_order=list(ctx.repo_scope),
                saga_id=apply_result.saga_id,
                op_id=ctx.op_id,
            )

            if promote_state == SagaTerminalState.SAGA_PARTIAL_PROMOTE:
                try:
                    await self._stack.comm.emit_postmortem(
                        op_id=ctx.op_id,
                        root_cause="saga_partial_promote",
                        failed_phase="PROMOTE",
                        next_safe_action="human_intervention_required",
                    )
                except Exception:
                    pass
                try:
                    await self._stack.controller.pause(scope="cross_repo_saga")
                except TypeError:
                    await self._stack.controller.pause()
                except Exception:
                    logger.exception(
                        "[Orchestrator] controller.pause() failed for partial promote %s",
                        ctx.op_id,
                    )
                ctx = ctx.advance(OperationPhase.POSTMORTEM)
                await self._record_ledger(
                    ctx, OperationState.FAILED,
                    {"reason": "saga_partial_promote", "saga_id": apply_result.saga_id, "promoted_repos": promoted_shas},
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
                await self._publish_outcome(ctx, OperationState.FAILED, "saga_partial_promote")
                return ctx

            # SAGA_SUCCEEDED
            ctx = ctx.advance(OperationPhase.VERIFY)
            await self._record_ledger(
                ctx,
                OperationState.APPLIED,
                {"saga_id": apply_result.saga_id},
            )
            ctx = await self._run_benchmark(ctx, [])
            ctx = ctx.advance(OperationPhase.COMPLETE)
            self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_saga)
            await self._publish_outcome(ctx, OperationState.APPLIED)
            await self._persist_performance_record(ctx)
            try:
                saga_applied: Sequence[Path] = [
                    (Path(self._config.repo_registry.get(repo).local_path) / rel_path).resolve()
                    for repo, patch in patch_map.items()
                    for rel_path, _ in patch.new_content
                ] if self._config.repo_registry is not None else []
            except Exception:
                saga_applied = []
            await self._oracle_incremental_update(saga_applied)
            return ctx

        if apply_result.terminal_state == SagaTerminalState.SAGA_STUCK:
            # Compensation failed: data may be inconsistent — emit postmortem
            try:
                await self._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause="saga_stuck",
                    failed_phase="APPLY",
                    next_safe_action="human_intervention_required",
                )
            except Exception:
                pass
            # Halt intake: dirty state requires human review before next op
            try:
                await self._stack.controller.pause()
            except Exception:
                logger.exception(
                    "[Orchestrator] controller.pause() failed for stuck saga %s; "
                    "manual pause may be required",
                    ctx.op_id,
                )
            else:
                logger.warning(
                    "[Orchestrator] Safe pause triggered after SAGA_STUCK on %s",
                    ctx.op_id,
                )
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id},
            )
            self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
            await self._publish_outcome(ctx, OperationState.FAILED, "saga_stuck")
            return ctx

        # SAGA_ROLLED_BACK: clean rollback — change not applied, system is clean
        # Do NOT advance to POSTMORTEM; record as a cancelled/failed op and return
        await self._record_ledger(
            ctx,  # still in APPLY phase
            OperationState.FAILED,
            {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id, "rolled_back": True},
        )
        self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga, rolled_back=True)
        await self._publish_outcome(ctx, OperationState.FAILED, apply_result.reason_code)
        return ctx

    async def _record_ledger(
        self,
        ctx: OperationContext,
        state: OperationState,
        data: Dict[str, Any],
    ) -> None:
        """Append a ledger entry, logging errors without raising.

        Awaits the ledger append inline so that entries are committed
        before the pipeline continues.  Errors are logged but never
        propagate -- ledger failures must not crash the pipeline.
        """
        entry = LedgerEntry(
            op_id=ctx.op_id,
            state=state,
            data=data,
        )
        try:
            await self._stack.ledger.append(entry)
        except Exception as exc:
            logger.error(
                "Ledger append failed: op_id=%s state=%s error=%s",
                entry.op_id,
                entry.state.value,
                exc,
            )


# Alias so tests can import `Orchestrator` as well as `GovernedOrchestrator`
Orchestrator = GovernedOrchestrator
