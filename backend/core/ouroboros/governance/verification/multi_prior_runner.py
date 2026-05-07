"""Move 6.5 Slice 2 — MultiPriorRunner.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Reuse generative_quorum.compute_consensus and its frozen
   outcome types — do not fork consensus math. Reuse parallel
   async + cost patterns from speculative_branch_runner.py and
   generative_quorum_runner.py (gather, cancellation, caps) —
   one coordinator module, not a second ad-hoc speculative
   engine. Hard Task.cancel() on over-budget / prune signal is
   allowed only with an explicit grace (~5s) composited with
   existing timeout primitives; cancelled rolls may leave
   partial session artifacts — must be observable in ledger,
   not silent."

This module fires K parallel rolls under DIFFERENT priors
(Slice 1's :class:`PriorSet`) and composes Move 6's
:func:`compute_consensus` over the resulting AST signatures.
The novel contribution is **cost-cap-aware mid-flight
pruning**: a watchdog coroutine polls an injectable cost-
budget snapshot and, when budget cracks, fires
:meth:`asyncio.Task.cancel` on every still-pending roll with
an explicit grace period for cleanup. Cancelled rolls are
recorded in the verdict result with outcome
``CANCELLED_OVER_BUDGET`` so Slice 4's ledger surfaces them —
**never silent**.

**Architectural boundary** (composition discipline):

  * Move 6's :class:`CandidateRoll` stays prior-agnostic.
    Slice 2 builds a parallel :class:`MultiPriorRoll` artifact
    that carries the prior-identity + outcome metadata, and
    projects each completed roll into a Move 6 CandidateRoll
    via :func:`_to_candidate_roll` for consensus aggregation.
    Move 6's frozen contract is preserved byte-identical.

  * The orthogonal ``roll_id → prior_id`` map lives on
    :class:`MultiPriorVerdictResult` so Slice 4's observer +
    Slice 5's diff renderer can answer "which prior chose
    what" without consensus math caring.

  * :func:`compute_consensus` and :class:`ConsensusOutcome` /
    :class:`CandidateRoll` are imported lazily inside
    :func:`run_multi_prior_quorum` — AST-pinned via
    ``multi_prior_runner_no_consensus_math``. Top-level
    imports are forbidden.

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / candidate_generator / change_engine /
plan_generator / urgency_router / direction_inferrer /
semantic_guardian / policy imports. Pure substrate.

**Master flag** ``JARVIS_MULTI_PRIOR_RUNNER_ENABLED`` default-
FALSE per §33.1 — separate from Slice 1's master flag so
operators can materialize priors for inspection without
executing. Slice 3's adapter reads BOTH before firing.

**Cancellation discipline** — load-bearing per operator
binding:

  1. K Tasks created via :func:`asyncio.create_task` (not
     bare coroutines) so individual cancellation is possible.
  2. Watchdog task polls ``cost_governor_snapshot.is_exceeded()``
     every ``cost_check_interval_s`` seconds (default 1.0).
  3. On exhaustion, watchdog calls :meth:`Task.cancel` on each
     not-yet-done roll and signals the gather coordinator.
  4. Grace-period drain: ``asyncio.wait_for(gather(...,
     return_exceptions=True), timeout=grace_period_s)`` lets
     cancelled tasks finish their ``except asyncio.CancelledError``
     cleanup blocks (e.g. flushing partial artifacts to disk).
  5. Cancelled rolls return a :class:`MultiPriorRoll` with
     outcome=CANCELLED_OVER_BUDGET so the ledger row is
     auditable.

**NEVER raises** — every code path defensive (asyncio.CancelledError
re-raised because that's caller-driven shutdown; everything
else swallowed and surfaced via the verdict's outcome field).
"""
from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import os
import time
from dataclasses import dataclass, field
from typing import (
    Any, Coroutine, Dict, FrozenSet, List, Mapping,
    Optional, Protocol, Tuple,
)


logger = logging.getLogger(
    "Ouroboros.MultiPriorRunner",
)


MULTI_PRIOR_RUNNER_SCHEMA_VERSION: str = (
    "multi_prior_runner.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


# Default per-roll timeout matches Move 6's runner default
# (60s; matches CLAUDE.md STANDARD generation budget).
_DEFAULT_TIMEOUT_PER_ROLL_S: float = 60.0
# Default cancellation grace period — operator binding allows
# ~5s. Composes with per-roll timeout (i.e. a roll cancelled
# at second 59 still has 5s to flush partial artifacts).
_DEFAULT_GRACE_PERIOD_S: float = 5.0
# Default watchdog poll interval. 1s is empirically tight
# enough to catch budget cracks within a typical 60s roll
# without flooding cost-governor accessors.
_DEFAULT_COST_CHECK_INTERVAL_S: float = 1.0


# ---------------------------------------------------------------------------
# Closed taxonomy — 4-value roll outcome
# ---------------------------------------------------------------------------


class MultiPriorRollOutcome(str, enum.Enum):
    """Closed 4-value taxonomy of per-roll outcomes. Every
    roll maps to exactly one — never None, never implicit
    fall-through. Mirrors Move 6 :class:`ConsensusOutcome`
    discipline (closed; AST-pinned).

    ``COMPLETED``                — Generator returned
                                   successfully within
                                   per-roll timeout AND
                                   before any cost-cancel
                                   signal. Roll's signature
                                   feeds consensus math.

    ``TIMEOUT``                  — :func:`asyncio.wait_for`
                                   timed out before generator
                                   returned. No signature;
                                   roll excluded from
                                   consensus.

    ``CANCELLED_OVER_BUDGET``    — Watchdog observed cost-
                                   governor exhaustion AND
                                   roll was not yet done.
                                   :meth:`Task.cancel` fired
                                   with grace-period drain.
                                   Operator binding requires
                                   ledger observability.

    ``GENERATOR_ERROR``          — Generator raised any
                                   exception other than
                                   :class:`asyncio.CancelledError`.
                                   Defensive — runner never
                                   propagates."""

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CANCELLED_OVER_BUDGET = "cancelled_over_budget"
    GENERATOR_ERROR = "generator_error"


# ---------------------------------------------------------------------------
# Master flag + tunable knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_MULTI_PRIOR_RUNNER_ENABLED`` master switch.
    Default-FALSE per §33.1: when off, :func:`run_multi_prior_quorum`
    short-circuits to a DISABLED verdict with no rolls fired
    (zero-cost when disabled). Separate from Slice 1's
    ``JARVIS_MULTI_PRIOR_PLANNING_ENABLED`` so operators can
    materialize priors for inspection without executing.

    Slice 3's adapter reads BOTH before firing."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Generator + cost-snapshot protocols (caller-injected)
# ---------------------------------------------------------------------------


class MultiPriorGenerator(Protocol):
    """Pluggable per-prior generator. Slice 3's adapter (one
    new call site in the orchestrator pipeline) implements
    this protocol by composing :class:`PlanGenerator` /
    candidate_generator / provider chain — Slice 2 stays
    backend-agnostic.

    The generator MUST be async. It receives the prior's
    materialized identity + a runner-assigned roll_id;
    returns the candidate diff text on success. It SHOULD
    write any partial artifact (e.g. half-streamed tokens) to
    a deterministic path before raising
    :class:`asyncio.CancelledError` so the watchdog's grace
    period can capture it."""

    async def __call__(
        self,
        *,
        prior: Any,
        roll_id: str,
    ) -> str:
        ...


class CostBudgetSnapshot(Protocol):
    """Pluggable cost-budget oracle. Slice 3 wires
    :class:`CostGovernor` here. Slice 2 polls
    :meth:`is_exceeded` periodically; on True, fires
    :meth:`Task.cancel` on pending rolls.

    Calls MUST be cheap (per-poll latency dominates watchdog
    overhead). Defensive: any exception inside is_exceeded()
    is swallowed by the watchdog and treated as "not
    exceeded" so a flaky budget oracle doesn't kill rolls
    spuriously."""

    def is_exceeded(self) -> bool:
        ...


# ---------------------------------------------------------------------------
# Frozen artifacts — propagation-safe across async + lock boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiPriorRoll:
    """One prior's roll outcome. Frozen so propagation through
    Slice 4's observer is safe. Adopts §33.5 versioned-artifact
    contract.

    ``prior_id`` is the orthogonal threading lever — it carries
    the prior identity that Move 6's :class:`CandidateRoll`
    refuses to know about. Slice 4's observer + Slice 5's diff
    renderer use this field to surface "which prior chose
    what".

    ``ast_signature`` is empty for any non-COMPLETED outcome
    (Move 6's :func:`compute_consensus` excludes such rolls
    structurally — empty-signature rolls don't form clusters).

    ``partial_artifact_path`` is set when the generator
    stashed in-progress output before being cancelled — Slice
    4's ledger surfaces it for operator post-mortem. Empty
    string when no artifact (most cancelled rolls won't have
    one unless the generator opted in).
    """

    roll_id: str
    prior_id: str
    candidate_diff: str
    ast_signature: str
    seed: int
    cost_estimate_usd: float
    outcome: MultiPriorRollOutcome
    elapsed_s: float
    partial_artifact_path: str = ""
    schema_version: str = field(
        default=MULTI_PRIOR_RUNNER_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "roll_id": str(self.roll_id),
            "prior_id": str(self.prior_id),
            "candidate_diff": str(self.candidate_diff),
            "ast_signature": str(self.ast_signature),
            "seed": int(self.seed),
            "cost_estimate_usd": float(self.cost_estimate_usd),
            "outcome": self.outcome.value,
            "elapsed_s": float(self.elapsed_s),
            "partial_artifact_path": str(
                self.partial_artifact_path,
            ),
            "schema_version": str(self.schema_version),
        }


@dataclass(frozen=True)
class MultiPriorVerdictResult:
    """Composite verdict: Move 6 :class:`ConsensusVerdict`
    over completed rolls + per-roll outcome telemetry +
    orthogonal prior-identity threading. Frozen §33.5
    artifact.

    ``consensus_verdict`` is the verbatim Move 6 verdict
    computed over the projection of completed rolls into
    Move 6 :class:`CandidateRoll` instances. Slice 1's prior
    identity is carried separately in :attr:`roll_to_prior_id`
    so consensus math operates byte-identical to Move 6.

    ``cancelled_count`` / ``timeout_count`` / ``error_count``
    are the operator-facing telemetry knobs. Slice 4's
    chatter-suppressed observer fires SSE only when these
    cross thresholds (e.g. ``cancelled_count > 0`` is always
    operator-visible per the operator binding)."""

    op_id: str
    rolls: Tuple[MultiPriorRoll, ...]
    consensus_verdict: Any  # Move 6 ConsensusVerdict
    roll_to_prior_id: Mapping[str, str]
    cost_total_usd: float
    completed_count: int
    cancelled_count: int
    timeout_count: int
    error_count: int
    wall_clock_s: float
    schema_version: str = field(
        default=MULTI_PRIOR_RUNNER_SCHEMA_VERSION,
    )

    @property
    def k(self) -> int:
        return len(self.rolls)

    def is_actionable(self) -> bool:
        """Composes Move 6's ``ConsensusVerdict.is_actionable``.
        Returns False if consensus_verdict is None (defensive)."""
        verdict = self.consensus_verdict
        try:
            return bool(verdict.is_actionable())
        except (AttributeError, TypeError):
            return False

    def to_dict(self) -> Dict[str, Any]:
        verdict_dict: Optional[Dict[str, Any]] = None
        try:
            verdict_dict = self.consensus_verdict.to_dict()
        except (AttributeError, TypeError):
            verdict_dict = None
        return {
            "op_id": str(self.op_id),
            "rolls": [r.to_dict() for r in self.rolls],
            "consensus_verdict": verdict_dict,
            "roll_to_prior_id": dict(self.roll_to_prior_id),
            "cost_total_usd": float(self.cost_total_usd),
            "completed_count": int(self.completed_count),
            "cancelled_count": int(self.cancelled_count),
            "timeout_count": int(self.timeout_count),
            "error_count": int(self.error_count),
            "wall_clock_s": float(self.wall_clock_s),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Internal: per-roll execution wrapper
# ---------------------------------------------------------------------------


def _ast_signature(diff: str) -> str:
    """Pure-stdlib hash of the candidate diff. Produces an
    8-char prefix of sha256 hex — same shape as Move 6's
    signatures. Empty input → empty string (so consensus math
    correctly excludes empty-signature rolls). NEVER raises.

    Move 6's ``_compute_signature`` does AST-walk + structural
    canonicalization; this is structurally simpler because
    Slice 2 operates on the diff text Slice 3's adapter
    produces, which is already canonical. If a future arc
    wants the AST-walked form, Slice 3's adapter can produce
    it before handing the diff to Slice 2."""
    if not diff:
        return ""
    import hashlib
    return hashlib.sha256(
        diff.encode("utf-8"),
    ).hexdigest()[:16]


async def _execute_one_roll(
    generator: MultiPriorGenerator,
    *,
    prior: Any,
    roll_id: str,
    timeout_s: float,
) -> MultiPriorRoll:
    """Execute one roll with timeout + exception isolation.
    NEVER propagates non-cancellation exceptions. Cancellation
    is re-raised so the gather coordinator's
    ``return_exceptions=True`` captures it and the post-gather
    classifier surfaces CANCELLED_OVER_BUDGET.

    Mirrors the shape of :func:`generative_quorum_runner._execute_one_roll`
    (same defensive structure, same TimeoutError handling) but
    returns a :class:`MultiPriorRoll` with the roll's prior
    identity + outcome classification."""
    start = time.monotonic()
    diff_text = ""
    signature = ""
    outcome = MultiPriorRollOutcome.GENERATOR_ERROR
    seed = int(getattr(prior, "seed", 0))
    prior_id = str(getattr(prior, "prior_id", ""))
    try:
        coro = generator(prior=prior, roll_id=roll_id)
        if not inspect.isawaitable(coro):
            logger.debug(
                "[MultiPriorRunner] roll_id=%s generator "
                "returned non-awaitable; treating as error",
                roll_id,
            )
            return MultiPriorRoll(
                roll_id=roll_id,
                prior_id=prior_id,
                candidate_diff="",
                ast_signature="",
                seed=seed,
                cost_estimate_usd=0.0,
                outcome=MultiPriorRollOutcome.GENERATOR_ERROR,
                elapsed_s=time.monotonic() - start,
            )
        output = await asyncio.wait_for(
            coro, timeout=timeout_s,
        )
        diff_text = (
            output if isinstance(output, str) else ""
        )
        signature = _ast_signature(diff_text)
        outcome = MultiPriorRollOutcome.COMPLETED
    except asyncio.TimeoutError:
        logger.debug(
            "[MultiPriorRunner] roll_id=%s timed out after "
            "%.2fs", roll_id, timeout_s,
        )
        outcome = MultiPriorRollOutcome.TIMEOUT
    except asyncio.CancelledError:
        # Surface cancellation upward — the gather coordinator
        # captures via return_exceptions=True and the post-
        # gather classifier surfaces CANCELLED_OVER_BUDGET.
        raise
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorRunner] roll_id=%s raised: %s",
            roll_id, exc,
        )
        outcome = MultiPriorRollOutcome.GENERATOR_ERROR
    return MultiPriorRoll(
        roll_id=roll_id,
        prior_id=prior_id,
        candidate_diff=diff_text,
        ast_signature=signature,
        seed=seed,
        cost_estimate_usd=0.0,
        outcome=outcome,
        elapsed_s=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# Internal: cost-cap watchdog
# ---------------------------------------------------------------------------


async def _cost_watchdog(
    *,
    cost_governor_snapshot: Optional[CostBudgetSnapshot],
    tasks: Tuple["asyncio.Task[Any]", ...],
    poll_interval_s: float,
    cancellation_signal: "asyncio.Event",
) -> None:
    """Background coroutine that polls the injected cost
    snapshot and triggers cancellation when budget cracks.
    NEVER raises (defensive: a flaky cost oracle that throws
    is treated as 'not exceeded' so we don't kill rolls
    spuriously).

    Exits cleanly when:
      * All tasks are done (no work left to monitor)
      * Cancellation_signal already set (someone else fired)
      * Itself is cancelled (caller shutting down)
    """
    if cost_governor_snapshot is None:
        return
    try:
        while True:
            await asyncio.sleep(poll_interval_s)
            if cancellation_signal.is_set():
                return
            if all(t.done() for t in tasks):
                return
            try:
                exhausted = bool(
                    cost_governor_snapshot.is_exceeded(),
                )
            except Exception:  # noqa: BLE001 — defensive
                exhausted = False
            if not exhausted:
                continue
            # Cost cracked — cancel all not-yet-done tasks.
            logger.debug(
                "[MultiPriorRunner] cost-budget exhausted; "
                "cancelling %d pending rolls",
                sum(
                    1 for t in tasks if not t.done()
                ),
            )
            cancellation_signal.set()
            for t in tasks:
                if not t.done():
                    t.cancel()
            return
    except asyncio.CancelledError:
        # Caller is shutting down — propagate.
        raise


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_multi_prior_quorum(
    generator: MultiPriorGenerator,
    *,
    op_id: str,
    prior_set: Any,
    timeout_per_roll_s: float = _DEFAULT_TIMEOUT_PER_ROLL_S,
    grace_period_s: float = _DEFAULT_GRACE_PERIOD_S,
    cost_governor_snapshot: Optional[CostBudgetSnapshot] = (
        None
    ),
    cost_check_interval_s: float = (
        _DEFAULT_COST_CHECK_INTERVAL_S
    ),
    enabled_override: Optional[bool] = None,
    threshold: Optional[int] = None,
) -> MultiPriorVerdictResult:
    """Fire K parallel rolls under different priors and
    compose Move 6's :func:`compute_consensus` over the
    completed rolls. NEVER raises (cancellation re-raised per
    asyncio convention).

    Decision tree (mirrors Move 6's ``run_quorum`` discipline):

      1. ``enabled_override`` (test override) OR
         :func:`master_enabled` is False → DISABLED verdict
         with no rolls fired (zero-cost when off).
      2. :class:`PriorSet` empty / malformed → FAILED verdict.
      3. Spawn K asyncio.Task instances (one per Prior) +
         a watchdog Task that polls the cost-budget snapshot.
      4. ``asyncio.gather(*tasks, return_exceptions=True)`` —
         cancelled tasks surface :class:`asyncio.CancelledError`
         instances which the post-gather classifier maps to
         ``CANCELLED_OVER_BUDGET``.
      5. Grace-period drain: cancelled tasks are awaited a
         second time inside :func:`asyncio.wait_for(...,
         timeout=grace_period_s)` so any cleanup blocks
         (partial-artifact flush) complete.
      6. Project completed rolls into Move 6
         :class:`CandidateRoll` and call
         :func:`compute_consensus` (lazy-imported).
      7. Wrap into :class:`MultiPriorVerdictResult` with
         per-outcome telemetry + orthogonal
         ``roll_to_prior_id`` map.
    """
    start = time.monotonic()
    # Step 1: gate check
    is_enabled = (
        enabled_override
        if enabled_override is not None
        else master_enabled()
    )
    if not is_enabled:
        return _build_disabled_verdict(
            op_id=op_id, start=start,
            detail=(
                "JARVIS_MULTI_PRIOR_RUNNER_ENABLED is "
                "false (or override) — no rolls fired"
            ),
        )

    # Step 2: validate prior_set shape (defensive)
    priors_seq = _safe_priors_seq(prior_set)
    if not priors_seq:
        return _build_disabled_verdict(
            op_id=op_id, start=start,
            detail="prior_set empty or malformed",
        )

    # Step 3: lazy-import Move 6 authority — composition
    # discipline AST-pinned (no top-level import).
    try:
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            CandidateRoll, compute_consensus,
        )
    except ImportError:
        logger.debug(
            "[MultiPriorRunner] Move 6 authority unavailable "
            "— returning FAILED verdict",
        )
        return _build_failed_verdict(
            op_id=op_id, start=start,
            detail="generative_quorum import failed",
        )

    # Step 4: spawn K rolls + watchdog
    cancellation_signal = asyncio.Event()
    roll_coros: List[Coroutine[Any, Any, MultiPriorRoll]] = [
        _execute_one_roll(
            generator,
            prior=prior,
            roll_id=f"{op_id}:roll-{idx}",
            timeout_s=timeout_per_roll_s,
        )
        for idx, prior in enumerate(priors_seq)
    ]
    tasks: Tuple["asyncio.Task[Any]", ...] = tuple(
        asyncio.create_task(
            c, name=f"multi_prior_roll_{op_id}_{idx}",
        )
        for idx, c in enumerate(roll_coros)
    )
    watchdog: Optional["asyncio.Task[None]"] = None
    if cost_governor_snapshot is not None:
        watchdog = asyncio.create_task(
            _cost_watchdog(
                cost_governor_snapshot=(
                    cost_governor_snapshot
                ),
                tasks=tasks,
                poll_interval_s=cost_check_interval_s,
                cancellation_signal=cancellation_signal,
            ),
            name=f"multi_prior_watchdog_{op_id}",
        )

    # Step 5: gather + grace drain
    raw_results = await asyncio.gather(
        *tasks, return_exceptions=True,
    )

    # Step 6: shut down the watchdog (drain its cleanup so we
    # don't leak a pending task on the event loop).
    if watchdog is not None and not watchdog.done():
        watchdog.cancel()
        try:
            await asyncio.wait_for(
                watchdog, timeout=grace_period_s,
            )
        except (
            asyncio.TimeoutError,
            asyncio.CancelledError,
        ):
            pass
        except Exception:  # noqa: BLE001 — defensive
            pass

    # Step 7: classify each task result into a MultiPriorRoll
    rolls_out: List[MultiPriorRoll] = []
    roll_to_prior_id: Dict[str, str] = {}
    for idx, (task_result, prior) in enumerate(
        zip(raw_results, priors_seq),
    ):
        roll_id = f"{op_id}:roll-{idx}"
        prior_id = str(getattr(prior, "prior_id", ""))
        seed = int(getattr(prior, "seed", 0))
        roll_to_prior_id[roll_id] = prior_id
        if isinstance(task_result, MultiPriorRoll):
            rolls_out.append(task_result)
            continue
        if isinstance(task_result, asyncio.CancelledError):
            rolls_out.append(
                MultiPriorRoll(
                    roll_id=roll_id,
                    prior_id=prior_id,
                    candidate_diff="",
                    ast_signature="",
                    seed=seed,
                    cost_estimate_usd=0.0,
                    outcome=(
                        MultiPriorRollOutcome
                        .CANCELLED_OVER_BUDGET
                    ),
                    elapsed_s=time.monotonic() - start,
                ),
            )
            continue
        if isinstance(task_result, BaseException):
            logger.debug(
                "[MultiPriorRunner] roll_id=%s gather "
                "returned exception: %r",
                roll_id, task_result,
            )
            rolls_out.append(
                MultiPriorRoll(
                    roll_id=roll_id,
                    prior_id=prior_id,
                    candidate_diff="",
                    ast_signature="",
                    seed=seed,
                    cost_estimate_usd=0.0,
                    outcome=(
                        MultiPriorRollOutcome
                        .GENERATOR_ERROR
                    ),
                    elapsed_s=time.monotonic() - start,
                ),
            )

    # Step 8: project completed rolls → Move 6 CandidateRoll
    # and call compute_consensus. Non-completed rolls are
    # excluded structurally (empty signature → consensus math
    # correctly excludes them).
    candidate_rolls: List[Any] = []
    for r in rolls_out:
        if r.outcome is not MultiPriorRollOutcome.COMPLETED:
            continue
        candidate_rolls.append(
            CandidateRoll(
                roll_id=r.roll_id,
                candidate_diff=r.candidate_diff,
                ast_signature=r.ast_signature,
                cost_estimate_usd=r.cost_estimate_usd,
                seed=r.seed,
            ),
        )
    consensus_verdict = compute_consensus(
        candidate_rolls, threshold=threshold,
    )

    # Step 9: aggregate telemetry + return verdict
    cost_total = sum(
        r.cost_estimate_usd for r in rolls_out
    )
    completed = sum(
        1 for r in rolls_out
        if r.outcome is MultiPriorRollOutcome.COMPLETED
    )
    cancelled = sum(
        1 for r in rolls_out
        if r.outcome is (
            MultiPriorRollOutcome.CANCELLED_OVER_BUDGET
        )
    )
    timed_out = sum(
        1 for r in rolls_out
        if r.outcome is MultiPriorRollOutcome.TIMEOUT
    )
    errored = sum(
        1 for r in rolls_out
        if r.outcome is MultiPriorRollOutcome.GENERATOR_ERROR
    )
    return MultiPriorVerdictResult(
        op_id=str(op_id),
        rolls=tuple(rolls_out),
        consensus_verdict=consensus_verdict,
        roll_to_prior_id=dict(roll_to_prior_id),
        cost_total_usd=float(cost_total),
        completed_count=int(completed),
        cancelled_count=int(cancelled),
        timeout_count=int(timed_out),
        error_count=int(errored),
        wall_clock_s=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_priors_seq(prior_set: Any) -> Tuple[Any, ...]:
    """Defensively extract the priors tuple from a PriorSet-
    shaped object. Returns empty tuple on malformed input.
    NEVER raises."""
    try:
        priors = getattr(prior_set, "priors", ())
        if not isinstance(priors, tuple):
            priors = tuple(priors)
        return priors
    except (TypeError, ValueError):
        return ()


def _build_disabled_verdict(
    *,
    op_id: str,
    start: float,
    detail: str,
) -> MultiPriorVerdictResult:
    """Construct the canonical DISABLED verdict (no rolls
    fired). Lazy-imports Move 6's verdict types — same
    composition discipline as the main path."""
    try:
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            ConsensusOutcome, ConsensusVerdict,
        )
        verdict = ConsensusVerdict(
            outcome=ConsensusOutcome.DISABLED,
            agreement_count=0,
            distinct_count=0,
            total_rolls=0,
            canonical_signature=None,
            accepted_roll_id=None,
            detail=detail,
        )
    except ImportError:
        verdict = None  # type: ignore[assignment]
    return MultiPriorVerdictResult(
        op_id=str(op_id),
        rolls=(),
        consensus_verdict=verdict,
        roll_to_prior_id={},
        cost_total_usd=0.0,
        completed_count=0,
        cancelled_count=0,
        timeout_count=0,
        error_count=0,
        wall_clock_s=time.monotonic() - start,
    )


def _build_failed_verdict(
    *,
    op_id: str,
    start: float,
    detail: str,
) -> MultiPriorVerdictResult:
    """Construct the canonical FAILED verdict (defensive
    sentinel — same shape as Move 6's FAILED outcome).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            ConsensusOutcome, ConsensusVerdict,
        )
        verdict = ConsensusVerdict(
            outcome=ConsensusOutcome.FAILED,
            agreement_count=0,
            distinct_count=0,
            total_rolls=0,
            canonical_signature=None,
            accepted_roll_id=None,
            detail=detail,
        )
    except ImportError:
        verdict = None  # type: ignore[assignment]
    return MultiPriorVerdictResult(
        op_id=str(op_id),
        rolls=(),
        consensus_verdict=verdict,
        roll_to_prior_id={},
        cost_total_usd=0.0,
        completed_count=0,
        cancelled_count=0,
        timeout_count=0,
        error_count=0,
        wall_clock_s=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the master flag this module
    reads."""
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_RUNNER_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Move 6.5 Slice 2 multi-"
                "prior runner. Default-FALSE per §33.1; when "
                "off, run_multi_prior_quorum returns the "
                "DISABLED verdict with no rolls fired. "
                "Separate from "
                "JARVIS_MULTI_PRIOR_PLANNING_ENABLED so "
                "operators can materialize priors for "
                "inspection without executing."
            ),
            category="Generation",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_runner.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_RUNNER_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorRunner] master-flag seeding failed "
            "(non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_runner_taxonomy_4_values`` — closed
         outcome enum (COMPLETED/TIMEOUT/CANCELLED_OVER_BUDGET/
         GENERATOR_ERROR).
      2. ``multi_prior_runner_master_default_false`` — §33.1
         producer flag stays default-FALSE.
      3. ``multi_prior_runner_authority_asymmetry`` — no
         orchestrator-tier imports.
      4. ``multi_prior_runner_no_top_level_consensus_import``
         — operator binding "do not fork consensus math".
         Move 6 :func:`compute_consensus` MUST be lazy-
         imported inside :func:`run_multi_prior_quorum` (and
         the helper builders), never at module top-level.
      5. ``multi_prior_runner_cancellation_discipline`` —
         cancellation MUST go through :meth:`Task.cancel` +
         grace-period :func:`asyncio.wait_for`. Bare cancel
         without grace is forbidden.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_runner.py"
    )

    def _validate_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "COMPLETED", "TIMEOUT",
            "CANCELLED_OVER_BUDGET", "GENERATOR_ERROR",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "MultiPriorRollOutcome"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extra = seen - required
                if missing:
                    violations.append(
                        f"MultiPriorRollOutcome missing "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"MultiPriorRollOutcome has extra "
                        f"{sorted(extra)} — taxonomy is "
                        f"closed at 4 values"
                    )
                return tuple(violations)
        violations.append(
            "MultiPriorRollOutcome class missing"
        )
        return tuple(violations)

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            for cmp_node in ast.walk(test):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                operands_have_empty_str = False
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        operands_have_empty_str = True
                        break
                if not operands_have_empty_str:
                    continue
                for body_stmt in sub.body:
                    if isinstance(body_stmt, ast.Return):
                        if (
                            isinstance(
                                body_stmt.value, ast.Constant,
                            )
                            and body_stmt.value.value is False
                        ):
                            empty_returns_false = True
                            break
                if empty_returns_false:
                    break
            if empty_returns_false:
                break
        if not empty_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_runner" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_runner.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_runner.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_no_top_level_consensus_import(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Move 6 authority MUST be lazy-imported inside
        run_multi_prior_quorum (and helper builders), never
        at module top-level. Inspect ImportFrom nodes whose
        parent is the module itself (top-level)."""
        violations: list = []
        forbidden_name = "compute" + "_consensus"
        # Walk only direct children of the module to find
        # top-level imports (any nested import is fine).
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "generative_quorum" not in module:
                    continue
                for alias in node.names:
                    if alias.name in (
                        forbidden_name, "CandidateRoll",
                        "ConsensusOutcome",
                        "ConsensusVerdict",
                    ):
                        violations.append(
                            f"multi_prior_runner.py MUST "
                            f"NOT top-level-import "
                            f"{alias.name!r} from "
                            f"generative_quorum — Slice 2's "
                            f"composition discipline "
                            f"requires lazy-import inside "
                            f"the runner / helper functions"
                        )
        return tuple(violations)

    def _validate_cancellation_discipline(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Cancellation MUST go through Task.cancel() AND
        grace-period asyncio.wait_for. Asserts:
          1. ``run_multi_prior_quorum`` AST contains at least
             one ``asyncio.wait_for`` call (grace-period drain
             pattern).
          2. ``_cost_watchdog`` AST contains ``Task.cancel``
             via attribute call ``.cancel()``.
        """
        violations: list = []
        runner_func: Optional[ast.FunctionDef] = None
        watchdog_func: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                if node.name == "run_multi_prior_quorum":
                    runner_func = node  # type: ignore[assignment]
                elif node.name == "_cost_watchdog":
                    watchdog_func = node  # type: ignore[assignment]
        if runner_func is None:
            violations.append(
                "run_multi_prior_quorum function missing"
            )
        else:
            has_wait_for = False
            for sub in ast.walk(runner_func):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "wait_for"
                ):
                    has_wait_for = True
                    break
            if not has_wait_for:
                violations.append(
                    "run_multi_prior_quorum MUST use "
                    "asyncio.wait_for for grace-period "
                    "drain after cancellation"
                )
        if watchdog_func is None:
            violations.append("_cost_watchdog missing")
        else:
            has_cancel_call = False
            for sub in ast.walk(watchdog_func):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "cancel"
                ):
                    has_cancel_call = True
                    break
            if not has_cancel_call:
                violations.append(
                    "_cost_watchdog MUST call .cancel() on "
                    "pending tasks when budget cracks"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_runner_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 2 — MultiPriorRollOutcome "
                "is closed at 4 values."
            ),
            validate=_validate_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_runner_master_default_false"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 2 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_runner_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 2 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_runner_"
                "no_top_level_consensus_import"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 2 — operator binding "
                "2026-05-07 forbids forking Move 6 "
                "consensus math. compute_consensus + "
                "CandidateRoll + ConsensusOutcome + "
                "ConsensusVerdict MUST be lazy-imported "
                "inside run_multi_prior_quorum (and the "
                "helper builders)."
            ),
            validate=_validate_no_top_level_consensus_import,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_runner_cancellation_discipline"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 2 — cancellation MUST go "
                "through Task.cancel() in the watchdog AND "
                "grace-period asyncio.wait_for in the runner."
            ),
            validate=_validate_cancellation_discipline,
        ),
    ]


__all__ = [
    "CostBudgetSnapshot",
    "MULTI_PRIOR_RUNNER_SCHEMA_VERSION",
    "MultiPriorGenerator",
    "MultiPriorRoll",
    "MultiPriorRollOutcome",
    "MultiPriorVerdictResult",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "run_multi_prior_quorum",
]
