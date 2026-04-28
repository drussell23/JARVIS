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


# Phase 1 Slice 1.3 — register the route-decision adapter at module
# load. The adapter converts a ``(ProviderRoute, reason_str)`` tuple
# into a JSON-friendly dict for storage, and reconstitutes the tuple
# on REPLAY so callers receive the same Python shape they'd have
# received from the live UrgencyRouter call.
def _register_route_adapter() -> None:
    """Idempotent — safe to import multiple times. Defensive
    (NEVER raises) so a missing determinism module doesn't break
    the route runner import chain."""
    try:
        from backend.core.ouroboros.governance.determinism.phase_capture import (
            OutputAdapter,
            register_adapter,
        )
        from backend.core.ouroboros.governance.urgency_router import (
            ProviderRoute,
        )

        def _serialize(route_tuple: Any) -> Any:
            try:
                route, reason = route_tuple
                return {
                    "route": str(route.value) if hasattr(route, "value")
                    else str(route),
                    "reason": str(reason or ""),
                }
            except Exception:  # noqa: BLE001 — defensive
                return {"route": "", "reason": str(route_tuple)[:200]}

        def _deserialize(stored: Any) -> Any:
            try:
                if not isinstance(stored, dict):
                    return stored
                route_str = str(stored.get("route", ""))
                reason = str(stored.get("reason", ""))
                # ProviderRoute is a str-Enum so this constructor
                # accepts the raw string value.
                return (ProviderRoute(route_str), reason)
            except (ValueError, KeyError, TypeError):
                return stored

        register_adapter(
            phase="ROUTE",
            kind="route_assignment",
            adapter=OutputAdapter(
                serialize=_serialize,
                deserialize=_deserialize,
                name="route_assignment_adapter",
            ),
        )
    except Exception:  # noqa: BLE001 — defensive (import-time)
        # Determinism module unavailable — wiring still works as a
        # pure passthrough via capture_phase_decision's internal
        # short-circuit. No log spam at import time.
        pass


_register_route_adapter()


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
                ProviderRoute,
                UrgencyRouter,
            )
            _urgency_router = UrgencyRouter()

            # Phase 1 Slice 1.3 — wrap the route decision in
            # capture_phase_decision so RECORD/REPLAY/VERIFY work.
            # When the master flag is off, this is a pure passthrough
            # that calls _urgency_router.classify(ctx) directly with
            # negligible overhead. Adapter is registered at module
            # load below.
            try:
                from backend.core.ouroboros.governance.determinism.phase_capture import (
                    capture_phase_decision,
                )

                async def _classify_route() -> Any:
                    return _urgency_router.classify(ctx)

                _route_tuple = await capture_phase_decision(
                    op_id=ctx.op_id,
                    phase="ROUTE",
                    kind="route_assignment",
                    ctx=ctx,
                    compute=_classify_route,
                )
                _provider_route, _route_reason = _route_tuple
            except Exception:  # noqa: BLE001 — defensive
                # Capture wrapper failed → fall back to direct call.
                # Determinism is best-effort; routing must always
                # succeed. Operators see the warning in capture's
                # internal logging.
                logger.debug(
                    "[Orchestrator] capture_phase_decision failed; "
                    "falling back to direct UrgencyRouter call",
                    exc_info=True,
                )
                _provider_route, _route_reason = (
                    _urgency_router.classify(ctx)
                )

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
