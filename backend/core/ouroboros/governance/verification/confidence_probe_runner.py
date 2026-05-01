"""Move 5 Slice 3 — Async convergence runner.

Orchestrates K probes in parallel-with-early-stop, feeding answers
to Slice 1's ``compute_convergence`` as they arrive. Cancels
pending probes when ``ConvergenceVerdict.is_actionable()`` becomes
True (CONVERGED or DIVERGED).

Direct-solve principles (per the operator directive):

  * **Asynchronous** — runner is async; sync ``QuestionResolver``
    instances (Slice 2's ``ReadonlyEvidenceProber``) execute via
    ``asyncio.to_thread`` so the streaming hot path is not
    blocked. Slice 4's wire-up calls this from the
    confidence-collapse async surface.

  * **Dynamic** — wall-clock cap, max-probes, convergence quorum
    all env-tunable. Cap structure with floor + ceiling
    (``min(ceiling, max(floor, value))``) enforces structural
    safety operators cannot loosen below.

  * **Adaptive** — parallel-with-early-stop saves cost on the
    common case (early convergence). Worst case uses full
    K-probe budget; typical case 1-2 probes when answers
    quickly agree.

  * **Intelligent** — uses ``asyncio.as_completed`` to process
    probe answers in completion order (not submission order),
    enabling immediate convergence detection. Cancellation
    propagates cleanly via ``Task.cancel()`` + ``asyncio.gather
    (return_exceptions=True)``.

  * **Robust** — never raises. Every code path returns a valid
    ``ConvergenceVerdict``. Wall-clock timeout, prober exception,
    generator failure, empty context, master-flag-off all
    produce defined outcomes.

  * **No hardcoding** — wall-clock cap env-tunable; all probe
    budget composition derived from existing Slice 1 + Slice 2
    knobs. Zero magic constants in behavior logic.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + verification.confidence_probe_bridge
    (Slice 1) + verification.confidence_probe_generator
    (Slice 2) + verification.readonly_evidence_prober (Slice 2)
    ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * NEVER references mutation tool names in code (Name +
    Attribute nodes; docstring strings allowed).
  * Never raises out of any public method.

Master flag: this runner inherits gating from
``confidence_probe_bridge.bridge_enabled()`` (Slice 1's master).
When off, ``run_probe_loop`` returns
``ConvergenceVerdict(outcome=DISABLED, ...)`` immediately.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    ConvergenceVerdict,
    ProbeAnswer,
    ProbeOutcome,
    ProbeQuestion,
    bridge_enabled,
    compute_convergence,
    convergence_quorum,
    make_probe_answer,
    max_questions,
)
from backend.core.ouroboros.governance.verification.confidence_probe_generator import (  # noqa: E501
    AmbiguityContext,
    generate_probes,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (  # noqa: E501
    QuestionResolver,
    get_default_prober,
)

logger = logging.getLogger(__name__)


CONFIDENCE_PROBE_RUNNER_SCHEMA_VERSION: str = (
    "confidence_probe_runner.1"
)


# ---------------------------------------------------------------------------
# Env knobs — wall-clock cap unique to runner; others inherited from Slice 1
# ---------------------------------------------------------------------------


_DEFAULT_WALL_CLOCK_S: float = 30.0
_WALL_CLOCK_FLOOR_S: float = 5.0
_WALL_CLOCK_CEILING_S: float = 120.0


def probe_wall_clock_s() -> float:
    """``JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S`` (default 30s,
    floor 5s, ceiling 120s).

    Maximum wall-clock seconds the runner will wait for the K
    probes to complete. Composes with Phase 7.6's per-probe
    timeout (each probe inherits its own bound). When this hits
    before convergence, runner cancels remaining tasks and
    returns the current ``compute_convergence`` verdict.

    Cap structure: ``min(ceiling, max(floor, value))`` enforces
    structural safety. Operator cannot loosen below 5s (would
    starve probes) or exceed 120s (would block streaming hot
    path indefinitely)."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S", "",
    ).strip()
    if not raw:
        return _DEFAULT_WALL_CLOCK_S
    try:
        v = float(raw)
        return min(_WALL_CLOCK_CEILING_S, max(_WALL_CLOCK_FLOOR_S, v))
    except (TypeError, ValueError):
        return _DEFAULT_WALL_CLOCK_S


# ---------------------------------------------------------------------------
# Internal: wrap a sync resolver call in asyncio.to_thread
# ---------------------------------------------------------------------------


async def _resolve_one(
    resolver: QuestionResolver,
    question: ProbeQuestion,
) -> ProbeAnswer:
    """Run one ``QuestionResolver.resolve`` call in a thread
    executor so the async runner doesn't block on sync I/O.
    NEVER raises — all resolver exceptions caught at this
    boundary."""
    try:
        return await asyncio.to_thread(
            resolver.resolve, question,
        )
    except asyncio.CancelledError:
        # Propagate cancellation up to the gather() / as_completed()
        # boundary so the runner can clean up cleanly.
        raise
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ConfidenceProbeRunner] resolver raised on %r: %s",
            question.question if question else "?", exc,
        )
        # Return an empty answer — convergence detector will treat
        # it as a non-signal (empty fingerprint, won't cluster).
        return make_probe_answer(
            question=question.question if question else "",
            answer_text="",
            tool_rounds_used=0,
        )


async def _cancel_pending(
    tasks: List[asyncio.Task],
) -> None:
    """Cancel all unfinished tasks + await cleanup. NEVER raises."""
    for t in tasks:
        if not t.done():
            t.cancel()
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_probe_loop(
    ambiguity_context: AmbiguityContext,
    *,
    resolver: Optional[QuestionResolver] = None,
    quorum: Optional[int] = None,
    max_probes: Optional[int] = None,
    wall_clock_s: Optional[float] = None,
) -> ConvergenceVerdict:
    """Async parallel-with-early-stop probe runner.

    Decision sequence (every input maps to exactly one
    ``ConvergenceVerdict``):

      1. Master flag off → ``DISABLED``.
      2. ``ambiguity_context`` wrong type → ``FAILED`` (defensive
         sentinel — tests catch).
      3. Generator returns empty tuple → ``EXHAUSTED`` ("no probe
         questions generated for context").
      4. Spawn K probe tasks via ``asyncio.create_task``.
         Each task wraps the sync resolver in ``asyncio.to_thread``.
      5. ``asyncio.as_completed`` — for each completing task:
         * Append answer to running list
         * Call ``compute_convergence`` with current answers
         * If ``is_actionable()`` → cancel pending tasks, return
           verdict (CONVERGED or DIVERGED)
      6. All K tasks completed without convergence → return
         current verdict (typically EXHAUSTED, possibly DIVERGED
         if all distinct).
      7. Wall-clock timeout reached → cancel pending, return
         current verdict.

    NEVER raises out. Any unexpected exception → returns
    ``ConvergenceVerdict(outcome=FAILED, ...)`` with detail.

    Production callers (Slice 4) pass ``None`` for resolver;
    default singleton is used. Tests inject capturing fakes."""
    # Step 1: master flag off → DISABLED (zero cost)
    if not bridge_enabled():
        return ConvergenceVerdict(
            outcome=ProbeOutcome.DISABLED,
            agreement_count=0,
            distinct_count=0,
            total_answers=0,
            canonical_answer=None,
            canonical_fingerprint=None,
            detail="bridge master flag off",
        )

    # Step 2: defensive type check
    if not isinstance(ambiguity_context, AmbiguityContext):
        return ConvergenceVerdict(
            outcome=ProbeOutcome.FAILED,
            agreement_count=0,
            distinct_count=0,
            total_answers=0,
            canonical_answer=None,
            canonical_fingerprint=None,
            detail=(
                "ambiguity_context not an AmbiguityContext "
                "instance"
            ),
        )

    try:
        # Step 3: generate questions
        effective_max = (
            int(max_probes) if max_probes is not None and max_probes > 0
            else max_questions()
        )
        effective_max = max(1, effective_max)

        questions = generate_probes(
            ambiguity_context,
            max_questions_override=effective_max,
        )
        if not questions:
            return ConvergenceVerdict(
                outcome=ProbeOutcome.EXHAUSTED,
                agreement_count=0,
                distinct_count=0,
                total_answers=0,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail=(
                    "no probe questions generated for context"
                ),
            )

        # Step 4: spawn K probe tasks
        target_resolver = (
            resolver if resolver is not None
            else get_default_resolver()
        )
        tasks: List[asyncio.Task] = [
            asyncio.create_task(
                _resolve_one(target_resolver, q),
            )
            for q in questions
        ]

        # Step 5: process answers as they complete with wall-clock
        deadline_s = (
            float(wall_clock_s) if wall_clock_s and wall_clock_s > 0
            else probe_wall_clock_s()
        )
        deadline_s = max(_WALL_CLOCK_FLOOR_S, deadline_s)

        answers: List[ProbeAnswer] = []
        effective_quorum = (
            int(quorum) if quorum is not None and quorum >= 1
            else convergence_quorum()
        )

        try:
            for done_coro in asyncio.as_completed(
                tasks, timeout=deadline_s,
            ):
                answer = await done_coro
                answers.append(answer)
                verdict = compute_convergence(
                    answers,
                    quorum=effective_quorum,
                    max_probes=len(questions),
                )
                if verdict.is_actionable():
                    # Early-stop: cancel pending tasks
                    await _cancel_pending(tasks)
                    return verdict
        except asyncio.TimeoutError:
            # Step 7: wall-clock timeout — cancel + return current
            logger.debug(
                "[ConfidenceProbeRunner] wall-clock %s exceeded; "
                "%d/%d answers gathered",
                deadline_s, len(answers), len(questions),
            )
            await _cancel_pending(tasks)
            current = compute_convergence(
                answers,
                quorum=effective_quorum,
                max_probes=len(questions),
            )
            # If still EXHAUSTED but timeout fired, surface that
            # in detail (caller may prefer EXHAUSTED-with-timeout
            # vs EXHAUSTED-with-budget; we keep the outcome and
            # annotate detail).
            if current.outcome is ProbeOutcome.EXHAUSTED:
                return ConvergenceVerdict(
                    outcome=ProbeOutcome.EXHAUSTED,
                    agreement_count=current.agreement_count,
                    distinct_count=current.distinct_count,
                    total_answers=current.total_answers,
                    canonical_answer=None,
                    canonical_fingerprint=None,
                    detail=(
                        f"wall-clock timeout after {deadline_s:.1f}s "
                        f"with {current.total_answers}/"
                        f"{len(questions)} answers"
                    ),
                )
            return current

        # Step 6: all K completed without early-stop → final verdict
        return compute_convergence(
            answers,
            quorum=effective_quorum,
            max_probes=len(questions),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[ConfidenceProbeRunner] run_probe_loop raised: %s",
            exc,
        )
        return ConvergenceVerdict(
            outcome=ProbeOutcome.FAILED,
            agreement_count=0,
            distinct_count=0,
            total_answers=0,
            canonical_answer=None,
            canonical_fingerprint=None,
            detail=f"runner raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Default resolver provider — production callers pass None; we wire
# the default singleton from Slice 2's prober. Indirection lets tests
# patch this at the module scope without touching Slice 2's singleton.
# ---------------------------------------------------------------------------


def get_default_resolver() -> QuestionResolver:
    """Return the default ``QuestionResolver`` for the runner.
    Slice 2's ``get_default_prober()`` is the singleton; this
    indirection lets tests monkey-patch the runner's resolver
    source without touching Slice 2's state. NEVER raises."""
    try:
        return get_default_prober()
    except Exception:  # noqa: BLE001 — defensive
        # Fall through to a None-resolver-equivalent: returns
        # empty answers; convergence will be EXHAUSTED.
        return _NullQuestionResolver()


class _NullQuestionResolver:
    """Defensive sentinel — when the default prober singleton
    cannot be constructed, the runner uses this, which produces
    empty answers (no convergence)."""

    def resolve(
        self,
        question: ProbeQuestion,
        *,
        max_tool_rounds: Optional[int] = None,
    ) -> ProbeAnswer:
        del max_tool_rounds
        return make_probe_answer(
            question=question.question if question else "",
            answer_text="",
            tool_rounds_used=0,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_PROBE_RUNNER_SCHEMA_VERSION",
    "get_default_resolver",
    "probe_wall_clock_s",
    "run_probe_loop",
]
