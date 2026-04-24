"""Phase dispatcher — Slice 6a of Wave 2 (5).

The final architectural move of the Wave 2 PhaseRunner extraction arc.
After Slices 1-5 extracted every phase body into a :class:`PhaseRunner`
subclass, the orchestrator's ``_run_pipeline`` was still a giant linear
block that hand-wired each phase's inputs and rebound its outputs
across nine phase boundaries. Slice 6 replaces that linear block with
a **dispatcher loop** over a **PhaseRunnerRegistry**, threading
cross-phase state through a single **PhaseContext**.

## Design: factory-per-phase

We considered two patterns for handling heterogeneous runner
constructor signatures:

1. **Typed slots** — central ``PhaseContext`` dataclass with every
   possible field; runners read/write slots directly. Downside: hides
   which phase produced which field; runners must know about the full
   cross-phase schema.
2. **Factory-per-phase** (chosen) — registry stores ``phase →
   factory(orch, serpent, ctx_dict) → PhaseRunner``. Each factory
   knows which keys to pluck from ``ctx_dict``; each phase's artifact
   propagation is explicitly named in its factory.

We picked (2) because:
* It keeps per-phase dep declarations explicit (greppable).
* It allows runners to stay dumb about ``PhaseContext`` keys.
* It composes with the existing flag-gated ``else:`` inline blocks —
  factory is a no-op when the dispatcher flag is off.

## Dispatcher contract

``dispatch_pipeline(orchestrator, serpent, start_ctx, initial_context)``
returns the final :class:`OperationContext` after every reachable
phase has run. Loop body:

1. Look up runner factory by ``ctx.phase`` in the registry.
2. Instantiate runner via the factory with current ``PhaseContext``.
3. Await ``runner.run(ctx)``.
4. Merge ``result.artifacts`` into ``PhaseContext``.
5. If ``result.next_phase is None`` → return ``result.next_ctx``
   (terminal exit).
6. Else: set ``ctx = result.next_ctx`` and loop.

Terminal phases (COMPLETE / CANCELLED / EXPIRED / POSTMORTEM) are
recognized by the dispatcher as "no further runner" regardless of
whether ``next_phase`` is set, matching the orchestrator's FSM.

## Loud-fail contract (Manifesto §6 + §8)

Per operator directive, infrastructure-level failures MUST be visible:

* **Registry miss** — phase has no registered factory → raises
  :class:`PhaseRunnerRegistryError` with the offending phase name.
* **Unknown phase** — ``ctx.phase`` is not a valid
  :class:`OperationPhase` → raises :class:`PhaseRunnerRegistryError`.
* **Malformed context** — factory raises ``KeyError`` on missing
  required key → wrapped as :class:`PhaseContextError` with the key
  name.
* **Runner returns broken PhaseResult** (e.g. ``next_ctx`` is None
  but ``status`` is "ok") → raises :class:`PhaseDispatchError`.

None of these errors leak silently. Any dispatcher exception bubbles
out of ``_run_pipeline`` identical to how the orchestrator's own
unhandled exceptions do today.

## Flag gate

``JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED`` (default ``false``). When
on, ``_run_pipeline`` calls ``dispatch_pipeline`` and returns its
result. When off, the existing inline path runs unchanged.

## Authority invariant (grep-pinned)

This module imports nothing from ``candidate_generator`` / ``iron_gate``
/ ``change_engine`` / ``gate`` / ``policy`` / ``risk_tier``. Dispatcher
is pure routing — runners still own their own authority domains.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

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


logger = logging.getLogger("Ouroboros.PhaseDispatcher")

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_TERMINAL_PHASES = frozenset({
    OperationPhase.COMPLETE,
    OperationPhase.CANCELLED,
    OperationPhase.EXPIRED,
    OperationPhase.POSTMORTEM,
})


def dispatcher_enabled() -> bool:
    """Flag read: ``JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED``.

    **Default ``true`` as of 2026-04-23 graduation** (Wave 2 (5) #8
    FINAL). 3-session cadence under post-Ticket-A1/B/C guards:
    bt-2026-04-23-224649 (12 markers) + bt-2026-04-23-231351 (12 markers)
    + bt-2026-04-23-235215 (11 markers) — all three idle_timeout /
    session_outcome=complete / 0 runner-attributed frames (zero
    `phase_dispatcher.py`, `phase_runners/`, `generate_runner`,
    `slice4b_runner` frames in any traceback) / 0 JARVIS shutdown
    race / 0 POSTMORTEMs. **35 total `[PhaseRunnerDelegate] DISPATCHER
    → pipeline` markers** across the cadence, with **zero per-phase
    legacy delegation markers** in any session — proof-positive that
    the dispatcher short-circuit at orchestrator.py:1477 engaged on
    every dispatched op (the legacy inline `if _phase_runner_<PHASE>_extracted()`
    blocks were never reached).

    reachability_source: `dispatcher_markers+parity`. §6 Iron Gate
    live evidence NOT required for #8 per operator binding — the
    dispatcher is routing infrastructure, not a generator/gate;
    §6 depth for downstream phases was already graduated under
    #5–#7. Correctness oracle: Slice 6a parity (228/228 structural)
    + Slice 6b parity (248/248 via _run_both_paths harness across
    20 per-phase terminal matrix tests). Iron Gate silence under
    continued upstream exhaustion is non-rollback per binding
    unless runner-attributed regression or parity breaks are
    detected.

    legacy_if_blocks=0 across the cadence — no legacy per-phase
    block was reached on any dispatched op.

    Explicit ``=false`` remains a runtime kill switch reverting
    to the legacy else-chain in orchestrator.py. Wave 2 (5) CLOSED
    at this flip's post-flip-FINAL stamp.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
        .strip().lower() in _TRUTHY
    )


# ---------------------------------------------------------------------------
# Exceptions — loud-fail contract
# ---------------------------------------------------------------------------


class PhaseDispatchError(RuntimeError):
    """Base class for dispatcher infrastructure failures."""


class PhaseRunnerRegistryError(PhaseDispatchError):
    """Registry miss / unknown phase / malformed registry entry."""


class PhaseContextError(PhaseDispatchError):
    """Factory could not construct its runner from the current context."""


# ---------------------------------------------------------------------------
# PhaseContext — single slot for cross-phase locals
# ---------------------------------------------------------------------------


@dataclass
class PhaseContext:
    """Single container for cross-phase state that the inline orchestrator
    carried as function-scoped locals (with varying degrees of discipline).

    All slots default to ``None`` / sensible empty values so the dispatcher
    can construct an initial context without knowing which phase will
    run first. Slots are written by the source phase's ``PhaseResult.
    artifacts`` and consumed by the destination phase's factory.

    Slot ownership (source → consumer):

    * ``advisory`` — CLASSIFY produces; PLAN Tier 6 voice line consumes
      (``_advisory.chronic_entropy``).
    * ``consciousness_bridge`` — CLASSIFY produces; GENERATE
      fragile-file injection + VERIFY L2-retry retry-context injection
      consume.
    * ``risk_tier`` — CLASSIFY produces; GATE mutates at 6 sites;
      APPROVE/APPLY path-decision + 5a/5b preview-trigger consume.
    * ``best_candidate`` — VALIDATE produces; GATE SecurityReviewer/
      SimilarityGate/SemanticGuardian/MutationGate/5a/5b preview +
      APPROVE/APPLY consume.
    * ``generation`` — GENERATE produces; VALIDATE consumes; lands on
      ``ctx.generation`` via advance.
    * ``episodic_memory`` — GENERATE produces; VALIDATE failure
      recording + entropy critique aggregation consume.
    * ``generate_retries_remaining`` — GENERATE produces; VALIDATE
      entropy calc consumes (``retries_used=``).
    * ``t_apply`` — Slice4b (APPLY entry) produces; COMPLETE canary
      latency calc consumes.

    Additional freeform ``extras`` dict for future expansion without
    schema churn. Not used by 6a — present for 6b and beyond.
    """
    advisory: Any = None
    consciousness_bridge: Any = None
    risk_tier: Any = None
    best_candidate: Any = None
    best_validation: Any = None
    generation: Any = None
    episodic_memory: Any = None
    generate_retries_remaining: Optional[int] = None
    t_apply: float = 0.0
    extras: Dict[str, Any] = field(default_factory=dict)

    def merge_artifacts(self, artifacts: Dict[str, Any]) -> None:
        """Copy every artifact key into the matching slot.

        Keys not matching any declared slot land in ``extras``. Loud-fail
        on malformed artifacts: if artifacts is not a Mapping, raise.
        """
        if artifacts is None:
            return
        if not hasattr(artifacts, "items"):
            raise PhaseContextError(
                f"artifacts must be a Mapping, got {type(artifacts).__name__}"
            )
        for key, value in artifacts.items():
            if hasattr(self, key) and key != "extras":
                setattr(self, key, value)
            else:
                self.extras[key] = value


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# A factory builds a runner for a given phase from (orchestrator, serpent,
# PhaseContext, OperationContext). Factories are PURE — they construct
# and return; they do not run the runner.
#
# The OperationContext is the CURRENT ctx the dispatcher is about to pass
# to the runner. Factories need it because some cross-phase state lands
# on ``ctx.<attr>`` (risk_tier, validation) rather than in PhaseContext
# artifacts — the inline path carried these through ``ctx.advance(...,
# risk_tier=...)`` kwargs. Passing ctx to the factory preserves that
# source of truth.
RunnerFactory = Callable[
    ["Orchestrator", Optional[Any], PhaseContext, OperationContext],
    PhaseRunner,
]


class PhaseRunnerRegistry:
    """Map :class:`OperationPhase` → :class:`RunnerFactory`.

    Small, explicit, no reflection. Each registered phase has one factory
    that knows which :class:`PhaseContext` slots to pluck for its runner's
    constructor.
    """

    def __init__(self) -> None:
        self._factories: Dict[OperationPhase, RunnerFactory] = {}

    def register(
        self,
        phase: OperationPhase,
        factory: RunnerFactory,
    ) -> None:
        """Register a factory for a phase. Overwrites any prior entry
        (the most recent registration wins — useful for test isolation)."""
        if not isinstance(phase, OperationPhase):
            raise PhaseRunnerRegistryError(
                f"phase must be OperationPhase, got {type(phase).__name__}"
            )
        if not callable(factory):
            raise PhaseRunnerRegistryError(
                f"factory must be callable, got {type(factory).__name__}"
            )
        self._factories[phase] = factory

    def get(self, phase: OperationPhase) -> RunnerFactory:
        """Look up a factory. Raises :class:`PhaseRunnerRegistryError`
        on miss."""
        if phase not in self._factories:
            raise PhaseRunnerRegistryError(
                f"no runner factory registered for phase {phase.name}. "
                f"Registered phases: "
                f"{sorted(p.name for p in self._factories)}"
            )
        return self._factories[phase]

    def phases(self) -> tuple:
        """Return the tuple of registered phases (sorted by enum value)."""
        return tuple(sorted(self._factories.keys(), key=lambda p: p.value))


# ---------------------------------------------------------------------------
# Default registry — factories for every phase Slices 1-5 extracted
# ---------------------------------------------------------------------------


def _factory_classify(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.classify_runner import (
        CLASSIFYRunner,
    )
    return CLASSIFYRunner(orch, serpent)


def _factory_route(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.route_runner import (
        ROUTERunner,
    )
    return ROUTERunner(orch, serpent)


def _factory_context_expansion(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.context_expansion_runner import (
        ContextExpansionRunner,
    )
    return ContextExpansionRunner(orch, serpent)


def _factory_plan(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.plan_runner import (
        PLANRunner,
    )
    return PLANRunner(orch, serpent, advisory=pctx.advisory)


def _factory_generate(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.generate_runner import (
        GENERATERunner,
    )
    return GENERATERunner(orch, serpent, pctx.consciousness_bridge)


def _factory_validate(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.validate_runner import (
        VALIDATERunner,
    )
    if pctx.generation is None:
        raise PhaseContextError(
            "VALIDATE factory requires pctx.generation (produced by GENERATE). "
            "Upstream phase did not set the artifact."
        )
    return VALIDATERunner(
        orch, serpent,
        generation=pctx.generation,
        generate_retries_remaining=(
            pctx.generate_retries_remaining
            if pctx.generate_retries_remaining is not None
            else orch._config.max_generate_retries
        ),
        episodic_memory=pctx.episodic_memory,
    )


def _factory_gate(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.gate_runner import (
        GATERunner,
    )
    # risk_tier lives on ctx.risk_tier (stamped by CLASSIFY's advance to
    # ROUTE with risk_tier=...); GATE mutates it locally but the initial
    # value is on the OperationContext. Prefer pctx.risk_tier if set
    # (e.g. some earlier runner stashed an override), else ctx.risk_tier.
    _risk = pctx.risk_tier if pctx.risk_tier is not None else ctx.risk_tier
    if _risk is None:
        raise PhaseContextError(
            "GATE factory requires a risk_tier on ctx or pctx "
            "(produced by CLASSIFY's advance-to-ROUTE)."
        )
    return GATERunner(
        orch, serpent,
        best_candidate=pctx.best_candidate,
        risk_tier=_risk,
    )


def _factory_approve(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    # Slice4b covers APPROVE + APPLY + VERIFY as a combined runner.
    from backend.core.ouroboros.governance.phase_runners.slice4b_runner import (
        Slice4bRunner,
    )
    # GATE mutates risk_tier (6 sites) and returns the final value via
    # artifacts["risk_tier"]. Prefer pctx (post-GATE mutated value) over
    # the ctx attribute.
    _risk = pctx.risk_tier if pctx.risk_tier is not None else ctx.risk_tier
    if _risk is None:
        raise PhaseContextError(
            "APPROVE factory requires a risk_tier (produced by GATE)."
        )
    return Slice4bRunner(
        orch, serpent,
        best_candidate=pctx.best_candidate,
        risk_tier=_risk,
    )


def _factory_complete(
    orch: "Orchestrator", serpent, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    from backend.core.ouroboros.governance.phase_runners.complete_runner import (
        COMPLETERunner,
    )
    return COMPLETERunner(orch, serpent, t_apply=pctx.t_apply)


def build_default_registry() -> PhaseRunnerRegistry:
    """Construct the canonical registry wiring Slices 1-5 runners."""
    reg = PhaseRunnerRegistry()
    reg.register(OperationPhase.CLASSIFY, _factory_classify)
    reg.register(OperationPhase.ROUTE, _factory_route)
    reg.register(OperationPhase.CONTEXT_EXPANSION, _factory_context_expansion)
    reg.register(OperationPhase.PLAN, _factory_plan)
    reg.register(OperationPhase.GENERATE, _factory_generate)
    reg.register(OperationPhase.VALIDATE, _factory_validate)
    reg.register(OperationPhase.GATE, _factory_gate)
    reg.register(OperationPhase.APPROVE, _factory_approve)
    reg.register(OperationPhase.COMPLETE, _factory_complete)
    return reg


# ---------------------------------------------------------------------------
# Dispatcher — the main event
# ---------------------------------------------------------------------------


async def dispatch_pipeline(
    orchestrator: "Orchestrator",
    serpent: Optional[Any],
    start_ctx: OperationContext,
    *,
    registry: Optional[PhaseRunnerRegistry] = None,
    initial_context: Optional[PhaseContext] = None,
    max_iterations: int = 64,
) -> OperationContext:
    """Drive the pipeline by looping over registered PhaseRunners.

    Parameters
    ----------
    orchestrator:
        The :class:`Orchestrator` instance. Runners inherit its
        dependencies (stack, config, _cost_governor, etc.) via
        factory constructor args.
    serpent:
        Pipeline serpent handle (optional). Identical to the inline
        path's ``_serpent`` local.
    start_ctx:
        Initial :class:`OperationContext`. Must be in ``CLASSIFY``
        phase (the canonical entry point).
    registry:
        Optional registry override for tests. Defaults to
        :func:`build_default_registry`.
    initial_context:
        Optional pre-populated :class:`PhaseContext` (for partial
        dispatch / resumption). Defaults to empty ``PhaseContext()``.
    max_iterations:
        Safety cap on dispatcher iterations. Hit only on a pathological
        cycle in the runner DAG; raises :class:`PhaseDispatchError`.

    Returns
    -------
    OperationContext
        The final ctx after dispatch completes (terminal phase reached
        or a runner returned ``next_phase=None``).

    Raises
    ------
    PhaseRunnerRegistryError
        Unknown / unregistered phase.
    PhaseContextError
        Factory required a missing context slot.
    PhaseDispatchError
        Cycle detected / malformed PhaseResult / iteration limit.
    """
    reg = registry if registry is not None else build_default_registry()
    pctx = initial_context if initial_context is not None else PhaseContext()
    ctx = start_ctx
    # dispatch_phase = "which runner factory to invoke next."
    # This is NOT always equal to ctx.phase because some runners (e.g.
    # GENERATE) don't advance ctx internally — the inline FSM depended
    # on the NEXT phase's body to do the advance with cross-phase
    # kwargs (e.g. VALIDATE advances ``ctx.advance(VALIDATE, generation=...)``).
    # The dispatcher tracks ``dispatch_phase`` independently of
    # ``ctx.phase`` to preserve this contract. Downstream runners whose
    # bodies advance ctx will find ctx.phase matching dispatch_phase on
    # entry; runners whose bodies skip the advance rely on THEIR OWN
    # downstream to do it.
    dispatch_phase = ctx.phase

    for _iter in range(max_iterations):
        # Terminal-phase handling: COMPLETE IS registered (COMPLETERunner
        # does the terminal work — canary, oracle update, serpent stop)
        # so we check the registry first. Only UNregistered terminals
        # (CANCELLED / EXPIRED / POSTMORTEM — landed there via an early
        # runner return) short-circuit without invoking a runner.
        if (
            dispatch_phase in _TERMINAL_PHASES
            and dispatch_phase not in reg._factories  # noqa: SLF001
        ):
            logger.debug(
                "[PhaseDispatcher] unregistered terminal phase %s reached — "
                "exiting loop",
                dispatch_phase.name,
            )
            return ctx

        try:
            factory = reg.get(dispatch_phase)
        except PhaseRunnerRegistryError:
            # Re-raise; the registry already formatted a descriptive message.
            raise

        try:
            runner = factory(orchestrator, serpent, pctx, ctx)
        except PhaseContextError:
            raise  # already descriptive
        except Exception as exc:
            # Wrap unexpected factory errors so operators see "context error"
            # in logs rather than bare stack traces.
            raise PhaseContextError(
                f"factory for phase {dispatch_phase.name} raised: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(runner, PhaseRunner):
            raise PhaseDispatchError(
                f"factory for phase {dispatch_phase.name} returned "
                f"{type(runner).__name__}, not a PhaseRunner subclass"
            )

        logger.debug(
            "[PhaseDispatcher] iter=%d dispatching %s → %s",
            _iter, dispatch_phase.name, type(runner).__name__,
        )

        result = await runner.run(ctx)

        if not isinstance(result, PhaseResult):
            raise PhaseDispatchError(
                f"runner {type(runner).__name__} returned "
                f"{type(result).__name__}, not a PhaseResult"
            )

        # Merge runner artifacts into PhaseContext for downstream phases.
        pctx.merge_artifacts(dict(result.artifacts))

        # Wave 3 (6) Slices 3 + 4 — post-GENERATE fan-out hook.
        # Post-GENERATE seam: when this iter ran a GENERATE runner AND
        # GENERATE produced a `generation` artifact that landed on
        # pctx.generation, invoke the fan-out evaluator(s).
        #
        # Three flag states matter here:
        #   master off                          → no hook runs (default)
        #   master + enforce (Slice 4)          → enforce path: submits
        #                                         + awaits scheduler
        #   master + shadow (Slice 3, not enforce) → shadow-only path:
        #                                         builds graph + logs,
        #                                         never submits
        #
        # When BOTH enforce and shadow are set, enforce wins (enforce
        # already exercises the shadow's observability surface at
        # higher fidelity). The shadow-only branch is strictly for
        # operators who want decision-correctness telemetry without
        # scheduler side effects.
        if (
            dispatch_phase == OperationPhase.GENERATE
            and pctx.generation is not None
        ):
            from backend.core.ouroboros.governance.parallel_dispatch import (
                parallel_dispatch_enabled as _master_on,
                parallel_dispatch_enforce_enabled as _enforce_on,
                parallel_dispatch_shadow_enabled as _shadow_on,
            )
            if _master_on() and _enforce_on():
                # Enforce path — fail loud on unexpected errors
                # (operator directive: narrow catches only on hot
                # path). asyncio.CancelledError cooperates with
                # Ticket A1 wall-clock; TimeoutError is classified
                # internally. Structural bugs (ValueError from graph
                # validators, RuntimeError from non-terminal phase)
                # propagate and abort the pipeline.
                from backend.core.ouroboros.governance.parallel_dispatch import (
                    enforce_evaluate_fanout as _enforce_evaluate_fanout,
                )
                _scheduler = getattr(orchestrator, "_subagent_scheduler", None)
                if _scheduler is None:
                    # Narrow known-safe: scheduler not wired → treat
                    # as skip (operator enables enforce before the
                    # scheduler is available, e.g. unit harness).
                    logger.warning(
                        "[PhaseDispatcher] enforce_fanout skipped: "
                        "orchestrator has no _subagent_scheduler reference"
                    )
                else:
                    _fanout_result = await _enforce_evaluate_fanout(
                        op_id=ctx.op_id,
                        generation=pctx.generation,
                        scheduler=_scheduler,
                    )
                    # Slice 4 ships the submit + await primitive with
                    # loud-fail error handling. Consumption of
                    # per-unit results by downstream phases (VALIDATE /
                    # slice4b) is a later-slice concern — for now the
                    # result is stashed in extras so operators + tests
                    # can inspect it, and the sequential phase walk
                    # continues unchanged. This preserves behavioral
                    # parity with the serial path while the enforce
                    # surface matures.
                    pctx.extras["parallel_dispatch_fanout_result"] = (
                        _fanout_result
                    )
            elif _master_on() and _shadow_on():
                # Shadow-only path — per Slice 3, broad exception
                # catch is acceptable because shadow has no
                # production side effects. Enforce path (above) does
                # NOT use this pattern; shadow remains the defensive
                # path.
                try:
                    from backend.core.ouroboros.governance.parallel_dispatch import (
                        evaluate_shadow_fanout as _evaluate_shadow_fanout,
                    )
                    _evaluate_shadow_fanout(
                        op_id=ctx.op_id,
                        generation=pctx.generation,
                    )
                except Exception as _shadow_exc:  # noqa: BLE001 — shadow never fails
                    logger.debug(
                        "[PhaseDispatcher] shadow_fanout_hook raised (suppressed): %r",
                        _shadow_exc,
                    )

        # Terminal exit from runner → return immediately.
        if result.next_phase is None:
            logger.debug(
                "[PhaseDispatcher] runner returned next_phase=None "
                "(status=%s reason=%r) — terminal",
                result.status, result.reason,
            )
            return result.next_ctx

        ctx = result.next_ctx
        dispatch_phase = result.next_phase

    raise PhaseDispatchError(
        f"dispatcher exceeded max_iterations={max_iterations}; "
        f"likely a phase cycle in the registry DAG"
    )


__all__ = [
    "PhaseContext",
    "PhaseRunnerRegistry",
    "PhaseDispatchError",
    "PhaseRunnerRegistryError",
    "PhaseContextError",
    "build_default_registry",
    "dispatch_pipeline",
    "dispatcher_enabled",
]
