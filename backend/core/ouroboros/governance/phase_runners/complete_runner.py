"""COMPLETERunner — pilot extraction of the orchestrator's COMPLETE phase.

Slice 1 of Wave 2 item (5) — PhaseRunner extraction. Extracts the
~60-line terminal block at ``orchestrator.py`` line 7073–7132 into a
:class:`PhaseRunner` subclass behind
``JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** The runner body is a verbatim
transcription of the inline block. Parity tests
(``tests/governance/phase_runner/test_complete_runner_parity.py``)
pin byte-identical observable output across inline and runner paths
on the same input ctx.

## Why COMPLETE for the pilot

- Smallest of the 11 phases (60 lines vs GENERATE's 1,926)
- Terminal — no ``next_phase`` to orchestrate
- Linear — no retry loops, no conditional branches beyond
  null-guards on optional orchestrator attributes
- Read-only on ctx beyond the single ``ctx.advance()`` at entry
- Four orchestrator helpers only — clean dependency surface

## Dependencies

Injected via constructor rather than pulled from module globals:

* ``orchestrator`` — the :class:`Orchestrator` instance. The runner
  calls five of its methods + reads three of its optional attributes:
    - ``_stack.comm.emit_heartbeat``
    - ``_record_canary_for_ctx``
    - ``_publish_outcome``
    - ``_persist_performance_record``
    - ``_oracle_incremental_update``
    - ``_reasoning_narrator`` (optional)
    - ``_dialogue_store`` (optional)
    - ``_rsi_score_function`` (optional)
* ``serpent`` — per-op serpent animation handle (may be ``None``);
  captured as a local in ``_run_pipeline`` before COMPLETE fires
* ``t_apply`` — monotonic timestamp recorded at APPLY start, used
  for the canary latency calculation

## Authority invariant

This module imports nothing from ``candidate_generator`` /
``iron_gate`` / ``change_engine`` / ``gate`` / ``policy`` /
``risk_tier``. Grep-pinned at extraction graduation.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

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


logger = logging.getLogger(__name__)


class COMPLETERunner(PhaseRunner):
    """Terminal phase: advance ctx to COMPLETE, record outcome, emit
    telemetry, update oracle index. Mirror of orchestrator.py 7073-7132.
    """

    phase = OperationPhase.COMPLETE

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
        t_apply: float,
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent
        self._t_apply = t_apply

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent
        _t_apply = self._t_apply

        # ---- VERBATIM transcription of orchestrator.py 7073-7132 -------
        # Any divergence from the inline block is a parity-test failure.
        if _serpent:
            _serpent.update_phase("COMPLETE")
        ctx = ctx.advance(
            OperationPhase.COMPLETE, terminal_reason_code="complete",
        )

        # Heartbeat: COMPLETE (Manifesto §7)
        try:
            await orch._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="complete", progress_pct=100.0,
            )
        except Exception:
            pass

        orch._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
        await orch._publish_outcome(ctx, OperationState.APPLIED)
        await orch._persist_performance_record(ctx)
        applied_files = [Path(p).resolve() for p in ctx.target_files]
        await orch._oracle_incremental_update(applied_files)

        # ── P0 Wiring: Complete ReasoningNarrator + OperationDialogue ────
        if orch._reasoning_narrator is not None:
            try:
                orch._reasoning_narrator.record_outcome(
                    ctx.op_id, True, "Applied successfully",
                )
                await orch._reasoning_narrator.narrate_completion(ctx.op_id)
            except Exception:
                pass
        if orch._dialogue_store is not None:
            try:
                _d = orch._dialogue_store.get_active(ctx.op_id)
                if _d:
                    _d.add_entry("COMPLETE", "Applied successfully")
                orch._dialogue_store.complete_dialogue(ctx.op_id, "success")
            except Exception:
                pass

        # ── RSI Convergence: compute composite score ──────────────────
        if orch._rsi_score_function is not None:
            try:
                _score = orch._rsi_score_function.compute(
                    op_id=ctx.op_id,
                    test_pass_rate_before=getattr(ctx, "test_pass_rate_before", 0.0),
                    test_pass_rate_after=1.0 if getattr(ctx, "validation_passed", False) else 0.0,
                    coverage_before=getattr(ctx, "coverage_before", 0.0),
                    coverage_after=getattr(ctx, "coverage_after", 0.0),
                    complexity_before=getattr(ctx, "complexity_before", 0.0),
                    complexity_after=getattr(ctx, "complexity_after", 0.0),
                    lint_before=getattr(ctx, "lint_before", 0),
                    lint_after=getattr(ctx, "lint_after", 0),
                    blast_radius_total=getattr(ctx, "blast_radius_total", 0),
                )
                logger.info(
                    "[RSI Score] op=%s composite=%.4f",
                    ctx.op_id, _score.composite,
                )
            except Exception:
                logger.debug("RSI score computation failed", exc_info=True)

        # ── Ouroboros Serpent: stop animation ──
        if _serpent:
            try:
                await _serpent.stop(success=True)
            except Exception:
                pass
        # ---- end verbatim transcription --------------------------------

        # Phase 2 Slice 2.4 — verification postmortem (loop-closing).
        # Walk recorded claims (Slice 2.3) for this op, evaluate each
        # via the Oracle (Slice 2.1) using ctx-derived evidence,
        # aggregate into a VerificationPostmortem, persist it via
        # Slice 1.3's capture_phase_decision so the lesson is
        # permanent in the per-session ledger.
        #
        # Defensive at every layer — postmortem failure NEVER blocks
        # op closure. The op already succeeded; this is consequence
        # tracking, not gate enforcement (auto-revert on blocking
        # failures is a future-phase concern).
        try:
            from backend.core.ouroboros.governance.verification.postmortem import (
                log_postmortem_summary,
                persist_postmortem,
                produce_verification_postmortem,
            )
            _pm = await produce_verification_postmortem(
                op_id=ctx.op_id, ctx=ctx,
            )
            log_postmortem_summary(_pm)
            await persist_postmortem(pm=_pm, op_id=ctx.op_id, ctx=ctx)
            if _pm.has_blocking_failures:
                # Surface the blocking signal at WARNING — operator
                # sees that must_hold claims diverged. Future phase
                # may auto-revert; for now, audit only.
                logger.warning(
                    "[Orchestrator] verification postmortem reports "
                    "blocking failures: op=%s must_hold_failed=%d/%d",
                    ctx.op_id, _pm.must_hold_failed, _pm.must_hold_count,
                )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[Orchestrator] verification postmortem failed for "
                "op=%s — op closure unaffected",
                ctx.op_id, exc_info=True,
            )

        return PhaseResult(
            next_ctx=ctx,
            next_phase=None,
            status="ok",
            reason="complete",
        )


__all__ = ["COMPLETERunner"]
