"""ROUTERunner — Slice 3 of Wave 2 item (5) — PhaseRunner extraction.

Extracts the ROUTE phase body from ``orchestrator.py`` (roughly
lines 2048–2141 / 2257) into a :class:`PhaseRunner` subclass behind
``JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** Runner body is a verbatim
transcription with ``self.`` → ``orch.`` substitutions.

## What ROUTE does

1. **Telemetry host-binding enforcement** — split-brain guard for
   remote routes (GCP_PRIME / REMOTE)
2. **Urgency-aware provider routing** (Manifesto §5 Tier 0) — stamps
   ``provider_route`` + ``provider_route_reason`` on ctx, emits a
   CommProtocol DECISION event for observability
3. **Per-op cost governor start** — cap derived from stamped route +
   task_complexity
4. **Transition dispatch** — if ``config.context_expansion_enabled``,
   advances to ``CONTEXT_EXPANSION`` (with PreActionNarrator +
   serpent update); else advances directly to ``PLAN``

## Two success paths

* **next_phase = CONTEXT_EXPANSION** — when expansion is enabled;
  ContextExpansionRunner will take it from there
* **next_phase = PLAN** — when expansion is disabled; PLANRunner next

No fail/terminal paths — ROUTE is deterministic on success.

## Dependencies injected via constructor

* ``orchestrator`` — reads:
    - ``_stack.comm`` (emit_decision)
    - ``_config.context_expansion_enabled``
    - ``_cost_governor`` (start)
    - ``_pre_action_narrator`` (optional)
* ``serpent`` — pipeline serpent handle (optional ``None``)

## Authority invariant

Imports: ``op_context``, ``phase_runner``, plus function-local imports
of ``telemetry_contextualizer`` and ``urgency_router`` (same as inline
block). No execution-authority widening.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

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


class ROUTERunner(PhaseRunner):
    """Verbatim transcription of orchestrator.py ROUTE block (~2048-2141/2257)."""

    phase = OperationPhase.ROUTE

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent

        # ---- VERBATIM transcription of orchestrator.py 2048-2141/2257 ----

        # Telemetry host-binding enforcement for remote routes (split-brain guard)
        _routing = getattr(ctx, "routing", None)
        if _routing is not None and str(getattr(_routing, "name", "")).upper() in ("GCP_PRIME", "REMOTE"):
            try:
                from backend.core.ouroboros.governance.telemetry_contextualizer import (
                    TelemetryContextualizer,
                )
                _tc = TelemetryContextualizer()
                _exec_host = str(getattr(_routing, "endpoint", "local"))
                _tel_host = str(getattr(ctx, "telemetry_host", _exec_host))
                await _tc.assert_host_binding(
                    execution_host=_exec_host,
                    telemetry_host=_tel_host,
                )
            except RuntimeError as _bind_err:
                logger.warning(
                    "[Orchestrator] Telemetry host-binding violation: %s [%s]",
                    _bind_err, ctx.op_id,
                )
            except Exception:
                logger.debug("[Orchestrator] TelemetryContextualizer not available", exc_info=True)

        # ── Urgency-aware provider routing (Manifesto §5 Tier 0) ──
        try:
            from backend.core.ouroboros.governance.urgency_router import (
                UrgencyRouter,
            )
            _urgency_router = UrgencyRouter()
            _provider_route, _route_reason = _urgency_router.classify(ctx)
            object.__setattr__(ctx, "provider_route", _provider_route.value)
            object.__setattr__(ctx, "provider_route_reason", _route_reason)
            logger.info(
                "[Orchestrator] \U0001f6e4️  Route: %s (%s) [%s]",
                _provider_route.value, _route_reason, ctx.op_id,
            )
            if hasattr(orch._stack, "comm") and orch._stack.comm is not None:
                try:
                    from backend.core.ouroboros.governance.urgency_router import (
                        UrgencyRouter as _UR,
                    )
                    await orch._stack.comm.emit_decision(
                        op_id=ctx.op_id,
                        outcome=_provider_route.value,
                        reason_code=f"urgency_route:{_route_reason}",
                        route=_provider_route.value,
                        route_reason=_route_reason,
                        budget_profile=_UR.route_budget_profile(_provider_route),
                        details={
                            "route": _provider_route.value,
                            "route_description": _UR.describe_route(_provider_route),
                            "signal_urgency": getattr(ctx, "signal_urgency", ""),
                            "signal_source": getattr(ctx, "signal_source", ""),
                            "task_complexity": getattr(ctx, "task_complexity", ""),
                            "budget_profile": _UR.route_budget_profile(_provider_route),
                        },
                    )
                except Exception:
                    pass
        except Exception:
            logger.debug("[Orchestrator] UrgencyRouter not available", exc_info=True)

        # ── Start per-op cost governor ──
        try:
            orch._cost_governor.start(
                op_id=ctx.op_id,
                route=getattr(ctx, "provider_route", "") or "",
                complexity=getattr(ctx, "task_complexity", "") or "",
                is_read_only=bool(getattr(ctx, "is_read_only", False)),
            )
        except Exception:
            logger.debug("[Orchestrator] CostGovernor.start failed", exc_info=True)

        # Transition dispatch — expansion enabled ⇒ advance to CONTEXT_EXPANSION;
        # disabled ⇒ advance directly to PLAN. PreActionNarrator narrates the
        # upcoming CONTEXT_EXPANSION phase BEFORE the advance (inline parity).
        if orch._config.context_expansion_enabled:
            if orch._pre_action_narrator is not None:
                try:
                    await orch._pre_action_narrator.narrate_phase(
                        "CONTEXT_EXPANSION",
                        {"target_file": list(ctx.target_files)[0] if ctx.target_files else "unknown"},
                    )
                except Exception:
                    pass
            if _serpent: _serpent.update_phase("CONTEXT_EXPANSION")
            ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
            next_phase = OperationPhase.CONTEXT_EXPANSION
        else:
            ctx = ctx.advance(OperationPhase.PLAN)
            next_phase = OperationPhase.PLAN
        # ---- end verbatim transcription ----

        return PhaseResult(
            next_ctx=ctx,
            next_phase=next_phase,
            status="ok",
            reason="routed",
        )


__all__ = ["ROUTERunner"]
