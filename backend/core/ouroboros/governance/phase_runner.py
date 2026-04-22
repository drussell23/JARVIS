"""PhaseRunner — contract for the Wave 2 (5) orchestrator phase extraction.

The Ouroboros orchestrator's `_run_pipeline()` is a 5,867-line sequential
block implementing 11 phases inline: CLASSIFY, ROUTE, CONTEXT_EXPANSION,
PLAN, GENERATE, VALIDATE, GATE, APPROVE, APPLY, VERIFY, COMPLETE. Wave 2
item (5) is a mechanical extraction: move each phase's body into a
`PhaseRunner` subclass with a common `async run(ctx) -> PhaseResult`
shape, **with zero behavior change per slice**.

§1 (Boundary Principle): extraction does NOT widen execution authority.
A `PhaseRunner` owns the same side effects as the inline block it
replaces — same ledger writes, same telemetry, same gate decisions.

§3 (Disciplined Concurrency) prep: clear phase boundaries are the
prerequisite for Wave 3 (6) fan-out rework + (7) mid-token `/cancel`.
Those are gated on Wave 2 (5) stability.

§8 (Observability): every extraction is flag-gated
(`JARVIS_PHASE_RUNNER_<PHASE>_EXTRACTED`) with parity tests verifying
inline-path and runner-path produce byte-identical observable output
on the same `ctx` input. Graduation path: flag flips to `true` after
3 clean sessions → inline code later removed.

Authority invariant: this module imports nothing from
``candidate_generator`` / ``iron_gate`` / ``change_engine`` / ``gate``
/ ``policy`` / ``risk_tier``. Phase runners MAY import them (they
inherited the inline block's dependencies) but the contract file is
pure.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only
    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
        OperationPhase,
    )


PHASE_RUNNER_SCHEMA_VERSION = "1.0"


# Valid PhaseResult.status values. Runners must pick one.
PhaseResultStatus = Literal["ok", "retry", "skip", "fail"]


@dataclass(frozen=True)
class PhaseResult:
    """Uniform return shape across all PhaseRunner implementations.

    Fields:

    ``next_ctx``
        The updated :class:`OperationContext` after the phase ran.
        Phase runners produce a new ctx via ``ctx.advance(...)`` rather
        than mutating the input (ctx is frozen + hash-chained).

    ``next_phase``
        The phase the pipeline should transition to next. ``None``
        means terminal (the pipeline exits after this phase).

    ``status``
        ``ok``    — phase completed normally, proceed to ``next_phase``
        ``retry`` — phase wants a bounded retry (e.g. GENERATE_RETRY)
        ``skip``  — phase elected to skip (e.g. PLAN on trivial ops)
        ``fail``  — phase hit a terminal failure; pipeline should exit
                     via the error path (caller inspects ``reason``)

    ``reason``
        Optional short code / human-readable reason (e.g.
        ``"plan_rejected"``, ``"governor_throttled"``). Used by the
        orchestrator for logging and by terminal-phase artifact
        recording.

    ``artifacts``
        Phase-specific bounded bag of extra output (e.g. GENERATE
        might stash candidate metadata here). Must be JSON-serializable
        for §8 audit trails. Default empty dict.
    """

    next_ctx: "OperationContext"
    next_phase: Optional["OperationPhase"]
    status: PhaseResultStatus
    reason: Optional[str] = None
    artifacts: Mapping[str, Any] = field(default_factory=dict)


class PhaseRunner(ABC):
    """One phase of the Ouroboros pipeline, extracted for clarity.

    Subclasses must:

    1. Set the class attribute ``phase`` to the :class:`OperationPhase`
       value this runner implements.
    2. Implement ``async run(ctx) -> PhaseResult`` with behavior
       byte-identical to the inline block it replaces — parity tests
       (see ``tests/governance/phase_runner/``) pin this discipline.
    3. Never mutate ``ctx`` — produce the new ctx via ``ctx.advance(...)``
       so the hash chain stays intact.
    4. Never raise into the dispatcher path — catch exceptions, emit
       telemetry, and return ``PhaseResult(status="fail", ...)``.

    Dependency injection: runners take whatever orchestrator state
    they need via constructor arguments (e.g. the orchestrator
    instance, per-op local state like a serpent animation handle).
    This is deliberately **not** a fancy DI system — phases are
    extracted AS-IS and the runner just carries their inline
    dependency shape forward.

    The ``phase`` class attribute is declared as a type hint rather
    than a required classvar so subclasses can set it at class-define
    time without the ABC machinery objecting on abstract instantiation.
    """

    phase: "OperationPhase"

    @abstractmethod
    async def run(self, ctx: "OperationContext") -> PhaseResult:
        """Execute the phase body on ``ctx`` and return the transition."""
        raise NotImplementedError


__all__ = [
    "PHASE_RUNNER_SCHEMA_VERSION",
    "PhaseResult",
    "PhaseResultStatus",
    "PhaseRunner",
]
