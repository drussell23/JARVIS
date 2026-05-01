"""Move 6 Slice 3 — K-way parallel candidate runner.

Fires K candidate rolls in parallel via ``asyncio.gather`` with
per-roll timeout, aggregates them into ``CandidateRoll`` instances
with AST signatures (Slice 2), and routes through ``compute_
consensus`` (Slice 1) to produce a ``ConsensusVerdict``.

Slice 3 is a **transport-agnostic primitive**: it accepts a caller-
supplied ``RollGenerator`` Protocol and orchestrates parallel
invocation. Slice 4 is the consumer that wires this through
``candidate_generator`` + ``urgency_router`` + risk-tier gate.

Direct-solve principles:

  * **Asynchronous-ready** — ``async def run_quorum`` uses
    ``asyncio.gather(..., return_exceptions=True)`` so a single
    rolling failure NEVER propagates. Per-roll ``asyncio.wait_for``
    enforces timeout.

  * **Dynamic** — ``k`` and ``threshold`` default to env knobs from
    Slice 1 but accept explicit overrides. ``timeout_per_roll_s``
    + ``seed_base`` are caller-tunable.

  * **Adaptive** — failed/timed-out rolls become ``CandidateRoll``
    with empty signatures; ``compute_consensus`` already treats
    empty as no-signal and degrades gracefully (DISAGREEMENT
    when all empty, MAJORITY when K-1 succeed and agree, etc.).

  * **Intelligent** — single-file vs multi-file dispatch is opaque
    to the runner: caller passes ``is_multi_file=True`` and the
    generator returns ``Mapping[str, str]`` instead of ``str``;
    the runner routes to ``compute_multi_file_signature`` vs
    ``compute_ast_signature`` accordingly.

  * **Robust** — ``run_quorum`` is total: every input maps to
    exactly one ``QuorumRunResult``. Master flag off → DISABLED
    without firing any roll. Generator raises → that roll has
    empty signature; other K-1 still contribute. All K fail →
    FAILED.

  * **No hardcoding** — K + threshold + timeout caller-supplied
    with sensible defaults; cost estimate per roll caller-supplied
    so cost-contract tracking remains end-to-end caller-owned.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + Slice 1 (``generative_quorum``) + Slice 2
    (``ast_canonical``) ONLY. No orchestrator / candidate_
    generator / providers / iron_gate / etc.
  * No mutation tools imported anywhere.
  * Never raises out of ``run_quorum``.
  * No-exec/eval/compile pin — runner orchestrates async calls
    only; never executes generated code.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Tuple,
    Union,
)

from backend.core.ouroboros.governance.verification.ast_canonical import (
    compute_ast_signature,
    compute_multi_file_signature,
)
from backend.core.ouroboros.governance.verification.generative_quorum import (
    CandidateRoll,
    ConsensusOutcome,
    ConsensusVerdict,
    compute_consensus,
    quorum_enabled,
    quorum_k,
)

logger = logging.getLogger(__name__)


GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION: str = (
    "generative_quorum_runner.1"
)


# ---------------------------------------------------------------------------
# Roll-generator typing
# ---------------------------------------------------------------------------


# A roll generator is a caller-supplied async callable producing one
# candidate per invocation. Single-file mode returns ``str``;
# multi-file mode returns ``Mapping[str, str]`` (path → content).
# Either shape is acceptable — caller signals via ``is_multi_file``.
RollOutput = Union[str, Mapping[str, str]]
RollGenerator = Callable[..., Awaitable[RollOutput]]


# ---------------------------------------------------------------------------
# QuorumRunResult — aggregate result + verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuorumRunResult:
    """Aggregate result of one ``run_quorum`` invocation. Frozen
    for safe propagation across async boundaries.

    ``verdict`` is the ``ConsensusVerdict`` from Slice 1. ``rolls``
    is the tuple of all CandidateRoll instances (including failed
    rolls with empty signatures) — caller has full visibility.
    ``failed_roll_ids`` lists which roll_ids encountered exception
    or timeout. ``elapsed_seconds`` is monotonic-clock duration of
    the parallel batch (NOT sum of per-roll times)."""

    verdict: ConsensusVerdict
    rolls: Tuple[CandidateRoll, ...] = field(default_factory=tuple)
    failed_roll_ids: Tuple[str, ...] = field(default_factory=tuple)
    elapsed_seconds: float = 0.0
    schema_version: str = (
        GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.to_dict(),
            "rolls": [r.to_dict() for r in self.rolls],
            "failed_roll_ids": list(self.failed_roll_ids),
            "elapsed_seconds": self.elapsed_seconds,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Internal: signature compute dispatch
# ---------------------------------------------------------------------------


def _compute_signature(
    output: Any, *, is_multi_file: bool,
) -> str:
    """Dispatch to single-file or multi-file signature compute.
    Returns empty string on type mismatch — defensive."""
    try:
        if is_multi_file:
            if not isinstance(output, Mapping):
                return ""
            return compute_multi_file_signature(output)
        if not isinstance(output, str):
            return ""
        return compute_ast_signature(output)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[QuorumRunner] _compute_signature raised: %s", exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Internal: per-roll execution
# ---------------------------------------------------------------------------


async def _execute_one_roll(
    generator: RollGenerator,
    *,
    roll_id: str,
    seed: int,
    timeout_s: float,
    is_multi_file: bool,
    cost_estimate_usd: float,
) -> Tuple[CandidateRoll, bool]:
    """Execute one roll with timeout and exception isolation.
    Returns (roll, succeeded) where ``succeeded`` is False if the
    generator raised, timed out, or returned wrong shape. NEVER
    raises."""
    succeeded = False
    diff_text = ""
    signature = ""
    try:
        coro = generator(roll_id=roll_id, seed=seed)
        if not inspect.isawaitable(coro):
            # Defensive: caller passed a sync function. We require
            # async per Protocol; treat as failure.
            logger.debug(
                "[QuorumRunner] roll_id=%s generator returned "
                "non-awaitable; treating as failure",
                roll_id,
            )
            return (
                CandidateRoll(
                    roll_id=roll_id,
                    candidate_diff="",
                    ast_signature="",
                    cost_estimate_usd=cost_estimate_usd,
                    seed=seed,
                ),
                False,
            )
        output = await asyncio.wait_for(coro, timeout=timeout_s)
        signature = _compute_signature(
            output, is_multi_file=is_multi_file,
        )
        # Preserve diff text for audit. For multi-file we store
        # a deterministic JSON-ish shape; downstream APPLY consumes
        # the original Mapping via callbacks rather than re-parsing.
        if is_multi_file:
            diff_text = ""  # caller retains original mapping
        else:
            diff_text = output if isinstance(output, str) else ""
        succeeded = True
    except asyncio.TimeoutError:
        logger.debug(
            "[QuorumRunner] roll_id=%s timed out after %.2fs",
            roll_id, timeout_s,
        )
    except asyncio.CancelledError:
        # Re-raise cancellation — caller is shutting down
        raise
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[QuorumRunner] roll_id=%s raised: %s", roll_id, exc,
        )
    return (
        CandidateRoll(
            roll_id=roll_id,
            candidate_diff=diff_text,
            ast_signature=signature,
            cost_estimate_usd=cost_estimate_usd,
            seed=seed,
        ),
        succeeded,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_quorum(
    generator: RollGenerator,
    *,
    k: Optional[int] = None,
    threshold: Optional[int] = None,
    timeout_per_roll_s: float = 60.0,
    is_multi_file: bool = False,
    seed_base: int = 0,
    cost_estimate_per_roll_usd: float = 0.0,
    enabled_override: Optional[bool] = None,
) -> QuorumRunResult:
    """Fire K candidate rolls in parallel and compute consensus.
    NEVER raises. Master-flag-off short-circuits to DISABLED with
    no rolls fired (zero-cost when disabled).

    Decision tree:

      1. ``enabled_override`` (test fixture override) OR
         ``quorum_enabled()`` is False → DISABLED, no rolls fired.
      2. Effective ``k = k or quorum_k()`` (Slice 1 already clamps
         to floor 2, ceiling 5 via env knob).
      3. Fire K rolls in parallel via ``asyncio.gather(...,
         return_exceptions=True)``. Each roll has its own
         ``asyncio.wait_for(timeout)``.
      4. Each roll → ``CandidateRoll`` (failed rolls have empty
         signature).
      5. ``compute_consensus(rolls, threshold=threshold)`` →
         ``ConsensusVerdict``.
      6. Wrap in ``QuorumRunResult`` with timing + failed_ids."""
    start = time.monotonic()
    try:
        # Step 1: gate check
        is_enabled = (
            enabled_override if enabled_override is not None
            else quorum_enabled()
        )
        if not is_enabled:
            return QuorumRunResult(
                verdict=ConsensusVerdict(
                    outcome=ConsensusOutcome.DISABLED,
                    agreement_count=0,
                    distinct_count=0,
                    total_rolls=0,
                    canonical_signature=None,
                    accepted_roll_id=None,
                    detail=(
                        "JARVIS_GENERATIVE_QUORUM_ENABLED is "
                        "false (or override) — no rolls fired"
                    ),
                ),
                rolls=tuple(),
                failed_roll_ids=tuple(),
                elapsed_seconds=time.monotonic() - start,
            )

        # Step 2: resolve K
        effective_k = k if k is not None and k >= 2 else quorum_k()
        # Final defensive clamp — runner never fires < 2 rolls
        # (single-roll defeats consensus purpose).
        effective_k = max(2, effective_k)

        # Step 3: fire K rolls in parallel
        coros = [
            _execute_one_roll(
                generator,
                roll_id=f"roll-{i}",
                seed=seed_base + i,
                timeout_s=timeout_per_roll_s,
                is_multi_file=is_multi_file,
                cost_estimate_usd=cost_estimate_per_roll_usd,
            )
            for i in range(effective_k)
        ]
        results = await asyncio.gather(
            *coros, return_exceptions=True,
        )

        # Step 4: collect rolls + failed_ids
        rolls: list = []
        failed: list = []
        for idx, result in enumerate(results):
            if isinstance(result, BaseException):
                # Should not happen — _execute_one_roll catches
                # everything except CancelledError. But defensive.
                logger.debug(
                    "[QuorumRunner] gather returned exception for "
                    "roll-%d: %s", idx, result,
                )
                rolls.append(
                    CandidateRoll(
                        roll_id=f"roll-{idx}",
                        candidate_diff="",
                        ast_signature="",
                        cost_estimate_usd=(
                            cost_estimate_per_roll_usd
                        ),
                        seed=seed_base + idx,
                    ),
                )
                failed.append(f"roll-{idx}")
                continue
            roll, succeeded = result  # type: ignore[misc]
            rolls.append(roll)
            if not succeeded:
                failed.append(roll.roll_id)

        # Step 5: consensus
        verdict = compute_consensus(rolls, threshold=threshold)

        return QuorumRunResult(
            verdict=verdict,
            rolls=tuple(rolls),
            failed_roll_ids=tuple(failed),
            elapsed_seconds=time.monotonic() - start,
        )
    except asyncio.CancelledError:
        # Surface cancellation — caller is shutting down
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[QuorumRunner] run_quorum raised: %s", exc,
        )
        return QuorumRunResult(
            verdict=ConsensusVerdict(
                outcome=ConsensusOutcome.FAILED,
                agreement_count=0,
                distinct_count=0,
                total_rolls=0,
                canonical_signature=None,
                accepted_roll_id=None,
                detail=f"run_quorum raised: {exc!r}",
            ),
            rolls=tuple(),
            failed_roll_ids=tuple(),
            elapsed_seconds=time.monotonic() - start,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION",
    "QuorumRunResult",
    "RollGenerator",
    "RollOutput",
    "run_quorum",
]
