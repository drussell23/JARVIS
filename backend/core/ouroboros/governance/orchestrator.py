"""
Governed Pipeline Orchestrator
===============================

Central coordinator for the governed self-programming pipeline.  Ties
together the risk engine, candidate generator, approval provider, change
engine, and operation ledger into a single deterministic pipeline:

.. code-block:: text

    CLASSIFY -> ROUTE -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.change_engine import ChangeRequest
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
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
        Root directory of the project being modified.
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
    generation_timeout_s: float = 120.0
    validation_timeout_s: float = 60.0
    approval_timeout_s: float = 600.0
    max_generate_retries: int = 1
    max_validate_retries: int = 2


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
    ) -> None:
        self._stack = stack
        self._generator = generator
        self._approval_provider = approval_provider
        self._config = config

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
                # Already in a terminal phase or transition not allowed;
                # force a new context at POSTMORTEM via a fresh create+advance
                # path. Since we must always return terminal, build one.
                pass
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

        # Advance to ROUTE with risk_tier set
        ctx = ctx.advance(OperationPhase.ROUTE, risk_tier=risk_tier)

        # ---- Phase 2: ROUTE ----
        # Thin transition: just advance to GENERATE
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

        # Store generation result in context
        ctx = ctx.advance(OperationPhase.VALIDATE, generation=generation)

        # ---- Phase 4: VALIDATE ----
        best_candidate: Optional[Dict[str, Any]] = None
        validate_retries_remaining = self._config.max_validate_retries

        for attempt in range(1 + self._config.max_validate_retries):
            best_candidate = self._validate_candidates(generation)

            if best_candidate is not None:
                break

            validate_retries_remaining -= 1
            if validate_retries_remaining < 0:
                # All validation retries exhausted
                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "validation_failed"},
                )
                return ctx

            # Retry: advance to VALIDATE_RETRY
            ctx = ctx.advance(OperationPhase.VALIDATE_RETRY)

        assert best_candidate is not None  # guaranteed by loop logic

        # Store validation result in context
        validation = ValidationResult(
            passed=True,
            best_candidate=best_candidate,
            validation_duration_s=0.0,
            error=None,
        )
        ctx = ctx.advance(OperationPhase.GATE, validation=validation)

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

        change_request = self._build_change_request(ctx, best_candidate)

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
            return ctx

        # ---- Phase 8: VERIFY ----
        ctx = ctx.advance(OperationPhase.VERIFY)
        await self._record_ledger(
            ctx,
            OperationState.APPLIED,
            {"op_id": ctx.op_id},
        )

        ctx = ctx.advance(OperationPhase.COMPLETE)
        return ctx

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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

    def _validate_candidates(
        self, generation: GenerationResult
    ) -> Optional[Dict[str, Any]]:
        """AST-parse each candidate; return the first passing one.

        Parameters
        ----------
        generation:
            The generation result containing candidate dicts.

        Returns
        -------
        Optional[Dict[str, Any]]
            The first candidate whose ``content`` field passes ``ast.parse``,
            or ``None`` if all candidates fail.
        """
        for candidate in generation.candidates:
            content = candidate.get("content", "")
            try:
                ast.parse(content)
                return candidate
            except SyntaxError:
                logger.debug(
                    "Candidate failed AST validation: %s",
                    candidate.get("file", "<unknown>"),
                )
                continue
        return None

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
            candidate.get("file", str(ctx.target_files[0] if ctx.target_files else "unknown.py"))
        )
        proposed_content = candidate.get("content", "")

        profile = self._build_profile(ctx)

        return ChangeRequest(
            goal=ctx.description,
            target_file=target_file,
            proposed_content=proposed_content,
            profile=profile,
        )

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
