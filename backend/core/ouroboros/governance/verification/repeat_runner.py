"""Phase 2 Slice 2.2 — RepeatRunner (statistical re-verification).

Closes the second half of Slice 2.1's ``PropertyOracle`` foundation.
Where the Oracle gives a deterministic single-run verdict, the
RepeatRunner runs the verification N times against fresh evidence
and aggregates the results into a Bayesian posterior — turning
"this test passed once" into "this property holds with confidence
P".

ROOT PROBLEM SOLVED:

A single PASSED verdict isn't enough for flaky tests. A single
FAILED verdict could be variance noise. To claim "this property
holds" with confidence, the system must:

  1. Run the verification N times (each producing fresh evidence)
  2. Aggregate the verdicts via Bayesian update
  3. Stop early when confidence threshold is crossed
  4. Report the final posterior + confidence interval

Symptoms today (pre-Slice-2.2):
  * "fixes flaky test Y" → VERIFY runs once and passes → op closes →
    Y still flakes 5% in the wild.
  * "improves runtime by 20%" → one run hits 18% (variance) → op
    closes mistaking variance for gain.
  * "regression-free refactor" → test suite passes once → caller
    can't tell if the affected path is even covered.

LAYERING (no duplication):

  Slice 2.1 (mine, merged) — PropertyOracle: pure single-run
                              dispatcher with 4-valued verdict
  Slice 2.2 (this slice)    — RepeatRunner: Bayesian aggregator
                              over N Oracle dispatches
  Antigravity adaptation    — exploration_calculus: Bayesian
                              primitives (bayesian_update, entropy,
                              verdict_to_likelihood_ratio)

The RepeatRunner IMPORTS Antigravity's primitives directly.
Zero re-implementation of Bayesian math.

Mapping from Slice 2.1's VerdictKind to Antigravity's LR:

  PASSED                → "CONFIRMED" → LR ~3.0 (env-tunable)
  FAILED                → "REFUTED"   → LR ~0.33
  INSUFFICIENT_EVIDENCE → "INCONCLUSIVE" → LR 1.0 (no update)
  EVALUATOR_ERROR       → "INCONCLUSIVE" → LR 1.0 (no update)

OPERATOR'S DESIGN CONSTRAINTS APPLIED:

  * Asynchronous — evidence_collector is async; runs execute in
    parallel batches via asyncio.gather. Per-batch concurrency
    env-tunable.
  * Dynamic — confidence threshold, min/max runs, batch size all
    env-readable at call time. Prior + evidence_collector are
    free-form callables (no enum, no hardcoded property kinds).
  * Adaptive — early-stop when confidence threshold crossed; max
    runs as worst-case ceiling; insufficient/error verdicts don't
    update the belief (preserve Bayesian validity).
  * Intelligent — Bayesian update via Antigravity's bayesian_update
    + verdict_to_likelihood_ratio. Posterior automatically clamped
    to [MIN_PRIOR, MAX_PRIOR] to prevent degenerate beliefs.
  * Robust — every public method NEVER raises. Individual run
    failures become EVALUATOR_ERROR verdicts (don't poison the
    belief). asyncio.gather wrapped to swallow per-task exceptions.
  * No hardcoding — every threshold env-tunable; LR values come
    from exploration_calculus's _confirmed_lr / _refuted_lr.
  * Leverages existing — Antigravity's exploration_calculus +
    Slice 2.1 Oracle. Zero duplication.

AUTHORITY INVARIANTS (pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator
  * NEVER imports any phase_runners/* module
  * NEVER imports providers
  * Every public method NEVER raises (except VERIFY-strict in
    decide(), which doesn't apply here)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time
import traceback
from dataclasses import dataclass, field
from typing import (
    Any, Awaitable, Callable, List, Mapping, Optional, Tuple,
)

from backend.core.ouroboros.governance.verification.property_oracle import (
    Property,
    PropertyOracle,
    PropertyVerdict,
    VerdictKind,
    get_default_oracle,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + env-tunable defaults
# ---------------------------------------------------------------------------


def repeat_runner_enabled() -> bool:
    """``JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED`` (default
    ``false``).

    Phase 2 Slice 2.2 master flag. Re-read at call time so monkey-
    patch works in tests + operators can flip live without re-init.
    Default flips to ``true`` at Phase 2 Slice 2.5 graduation.

    When ``false``: callers can still construct + invoke the runner
    (the dispatcher always works); production callers should treat
    output as advisory only. Slice 2.5 flips production wiring at
    the same time as the default flag flip — until then this is
    shadow-mode infrastructure."""
    raw = os.environ.get(
        "JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _default_min_runs() -> int:
    """``JARVIS_VERIFICATION_REPEAT_MIN_RUNS`` (default 5).

    The minimum number of runs required before early-stop is
    eligible. Prevents the runner from declaring "PASSED with high
    confidence" after a single PASS that happened to land in the
    right region of the prior."""
    try:
        return max(1, int(
            os.environ.get(
                "JARVIS_VERIFICATION_REPEAT_MIN_RUNS", "5",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 5


def _default_max_runs() -> int:
    """``JARVIS_VERIFICATION_REPEAT_MAX_RUNS`` (default 50).

    Hard ceiling on runs regardless of belief state. Bounds cost +
    wall time. Operators with deeper budgets can raise this; the
    Bayesian math gracefully tightens the credible interval as N
    grows but each run has cost."""
    try:
        return max(1, int(
            os.environ.get(
                "JARVIS_VERIFICATION_REPEAT_MAX_RUNS", "50",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 50


def _default_confidence_threshold() -> float:
    """``JARVIS_VERIFICATION_REPEAT_CONFIDENCE`` (default 0.95).

    Posterior threshold for early-stop. The runner stops when:
      * posterior >= threshold (high-confidence PASS), OR
      * (1 - posterior) >= threshold (high-confidence FAIL)

    Default 0.95 → 95% credible interval that the property holds."""
    try:
        v = float(
            os.environ.get(
                "JARVIS_VERIFICATION_REPEAT_CONFIDENCE", "0.95",
            ).strip()
        )
        # Clamp to (0.5, 1.0) — values <= 0.5 would be meaningless
        return max(0.51, min(0.999, v))
    except (ValueError, TypeError):
        return 0.95


def _default_concurrency() -> int:
    """``JARVIS_VERIFICATION_REPEAT_CONCURRENCY`` (default 4).

    Number of evidence_collector calls launched in parallel per
    batch. After each batch the runner aggregates verdicts +
    decides whether to launch another batch (early-stop check).
    Higher concurrency → faster wall-time but more peak load on
    the collector."""
    try:
        return max(1, int(
            os.environ.get(
                "JARVIS_VERIFICATION_REPEAT_CONCURRENCY", "4",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 4


# ---------------------------------------------------------------------------
# RunBudget — caller-overridable runtime config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunBudget:
    """Per-call configuration. Frozen — pass an explicit instance
    to override the env-tunable defaults.

    All fields are validated + clamped to safe ranges. NEVER raises
    on bad input."""
    min_runs: int = -1  # sentinel: read from env
    max_runs: int = -1
    confidence_threshold: float = -1.0
    early_stop: bool = True
    parallel_concurrency: int = -1
    initial_prior: float = 0.5

    def resolved_min_runs(self) -> int:
        return self.min_runs if self.min_runs > 0 else _default_min_runs()

    def resolved_max_runs(self) -> int:
        return self.max_runs if self.max_runs > 0 else _default_max_runs()

    def resolved_confidence(self) -> float:
        return (
            self.confidence_threshold
            if 0.5 < self.confidence_threshold <= 1.0
            else _default_confidence_threshold()
        )

    def resolved_concurrency(self) -> int:
        return (
            self.parallel_concurrency
            if self.parallel_concurrency > 0
            else _default_concurrency()
        )

    def resolved_prior(self) -> float:
        # Clamp to (0, 1) per Antigravity's MIN_PRIOR/MAX_PRIOR
        # convention — exact 0 or 1 are non-updatable beliefs.
        return max(0.001, min(0.999, self.initial_prior))


# ---------------------------------------------------------------------------
# RepeatVerdict schema
# ---------------------------------------------------------------------------


REPEAT_VERDICT_SCHEMA_VERSION = "repeat_verdict.1"


@dataclass(frozen=True)
class RepeatVerdict:
    """Aggregated verdict over N runs. Frozen + hashable.

    Fields:
      * runs_completed — actual number of evidence collections
        (could be < max_runs if early-stopped; always >= min_runs
        unless individual runs raised before reaching min)
      * pass/fail/insufficient/error counts — verdict tally
      * initial_prior — what the runner started with
      * posterior — final Bayesian posterior P(property holds)
      * confidence — distance from 0.5 (max(p, 1-p)); 0.5 = max
        uncertainty, 1.0 = max certainty
      * final_verdict — PASSED if posterior >= threshold; FAILED
        if (1-posterior) >= threshold; INSUFFICIENT_EVIDENCE
        otherwise
      * early_stopped — true if the runner halted before max_runs
        because confidence threshold was crossed
      * halted_reason — diagnostic string (converged_pass /
        converged_fail / max_runs_reached / collector_exhausted)
      * individual_verdicts — per-run verdicts for forensics

    Hashable — safe to use as dict keys + write to ledger."""
    property_name: str
    kind: str
    runs_completed: int
    pass_count: int
    fail_count: int
    insufficient_count: int
    error_count: int
    initial_prior: float
    posterior: float
    confidence: float
    final_verdict: VerdictKind
    early_stopped: bool
    halted_reason: str
    individual_verdicts: Tuple[PropertyVerdict, ...] = field(
        default_factory=tuple,
    )
    schema_version: str = REPEAT_VERDICT_SCHEMA_VERSION
    started_unix: float = 0.0
    completed_unix: float = 0.0

    @property
    def passed(self) -> bool:
        """Convenience: True iff final_verdict is PASSED."""
        return self.final_verdict is VerdictKind.PASSED

    @property
    def is_terminal(self) -> bool:
        """True iff final_verdict is PASSED or FAILED.
        INSUFFICIENT_EVIDENCE is non-terminal — caller may want to
        retry with more runs or fall back."""
        return self.final_verdict in (
            VerdictKind.PASSED, VerdictKind.FAILED,
        )

    @property
    def total_decisive_runs(self) -> int:
        """Pass+fail. Excludes insufficient/error since those don't
        update the belief."""
        return self.pass_count + self.fail_count


# ---------------------------------------------------------------------------
# Lazy adapters for Antigravity primitives (defensive)
# ---------------------------------------------------------------------------


def _bayesian_update_safely(
    prior: float, verdict: VerdictKind,
) -> float:
    """Lazy import of Antigravity's bayesian_update. Falls back to
    a stdlib-only Bernoulli update if the adaptation module is
    unavailable. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            bayesian_update,
            verdict_to_likelihood_ratio,
        )
        verdict_str = _verdict_to_calculus_str(verdict)
        lr = verdict_to_likelihood_ratio(verdict_str)
        return bayesian_update(prior, lr)
    except Exception:  # noqa: BLE001 — defensive
        # Fallback: pure-stdlib LR convention (matches Antigravity's
        # defaults). Operator who customized exploration_calculus
        # via env will see drift, but the fallback math is sound.
        return _fallback_bayesian_update(prior, verdict)


def _verdict_to_calculus_str(v: VerdictKind) -> str:
    if v is VerdictKind.PASSED:
        return "CONFIRMED"
    if v is VerdictKind.FAILED:
        return "REFUTED"
    return "INCONCLUSIVE"


def _fallback_bayesian_update(
    prior: float, verdict: VerdictKind,
) -> float:
    """Pure-stdlib Bernoulli posterior. Used only when
    exploration_calculus unavailable. Matches the default LR
    constants (CONFIRMED=3.0, REFUTED=0.33, else 1.0).

    NEVER raises."""
    p = max(0.001, min(0.999, float(prior)))
    if v_lr := _DEFAULT_LR.get(verdict):
        lr = v_lr
    else:
        lr = 1.0
    try:
        num = lr * p
        den = lr * p + (1.0 - p)
        if den <= 0.0:
            return p
        post = num / den
        return max(0.001, min(0.999, post))
    except (ZeroDivisionError, OverflowError):
        return p


_DEFAULT_LR = {
    VerdictKind.PASSED: 3.0,
    VerdictKind.FAILED: 0.33,
    # INSUFFICIENT_EVIDENCE / EVALUATOR_ERROR → no entry → 1.0 (no update)
}


# ---------------------------------------------------------------------------
# RepeatRunner — async batched aggregator
# ---------------------------------------------------------------------------


# An evidence_collector is an async callable that, given a run
# index, returns the evidence mapping for that run. The collector
# encapsulates the side effect (running pytest, taking a
# measurement, querying state, etc.) — RepeatRunner orchestrates
# the collector + Oracle dance N times with budget control.
EvidenceCollector = Callable[[int], Awaitable[Mapping[str, Any]]]


class RepeatRunner:
    """Statistical re-verification orchestrator.

    Stateless. Construction is cheap. Safe to share across threads /
    async tasks. All state lives in the (immutable) ``RepeatVerdict``
    return value.

    Workflow:
      1. Resolve RunBudget from env if defaults requested
      2. Initialize posterior to prior
      3. Loop:
         a. Compute next batch size (min(concurrency, remaining))
         b. Launch batch via asyncio.gather (per-task defensive)
         c. For each verdict: increment counters, update posterior
         d. Check early-stop (after min_runs)
         e. If converged or out of budget, exit loop
      4. Determine final_verdict from posterior
      5. Return frozen RepeatVerdict
    """

    async def run(
        self,
        *,
        prop: Property,
        evidence_collector: EvidenceCollector,
        budget: Optional[RunBudget] = None,
        oracle: Optional[PropertyOracle] = None,
    ) -> RepeatVerdict:
        """Execute repeat verification. NEVER raises.

        Parameters
        ----------
        prop : Property
            The claim. Must have a registered ``kind`` in the Oracle
            (otherwise every run returns INSUFFICIENT_EVIDENCE).
        evidence_collector : EvidenceCollector
            ``async (run_index: int) -> Mapping[str, Any]``. Called
            once per run with a 0-indexed run number. The collector
            owns the side effect of producing fresh evidence (e.g.,
            spawning a subprocess to run pytest, taking a fresh
            latency measurement).
        budget : RunBudget, optional
            Override env-tunable defaults. ``None`` → read from env.
        oracle : PropertyOracle, optional
            Override the Oracle instance. ``None`` → use the
            module-level singleton.

        Returns
        -------
        RepeatVerdict (always — never raises)."""
        started = _time.time()
        oracle = oracle or get_default_oracle()
        budget = budget or RunBudget()

        if prop is None:
            return self._build_verdict(
                prop_name="<None>", kind="<None>",
                pass_count=0, fail_count=0, insuff_count=0,
                err_count=0, prior=0.5, posterior=0.5,
                final_verdict=VerdictKind.EVALUATOR_ERROR,
                early_stopped=False,
                halted_reason="property_is_none",
                individual=tuple(),
                started=started,
            )

        prior = budget.resolved_prior()
        max_runs = budget.resolved_max_runs()
        min_runs = min(budget.resolved_min_runs(), max_runs)
        confidence = budget.resolved_confidence()
        concurrency = budget.resolved_concurrency()

        posterior = prior
        pass_count = fail_count = insuff_count = err_count = 0
        individual: List[PropertyVerdict] = []
        early_stopped = False
        halted_reason = "max_runs_reached"

        runs_done = 0
        while runs_done < max_runs:
            remaining = max_runs - runs_done
            batch_size = min(concurrency, remaining)

            verdicts = await self._run_batch(
                oracle=oracle,
                prop=prop,
                evidence_collector=evidence_collector,
                start_index=runs_done,
                batch_size=batch_size,
            )

            for v in verdicts:
                individual.append(v)
                if v.verdict is VerdictKind.PASSED:
                    pass_count += 1
                elif v.verdict is VerdictKind.FAILED:
                    fail_count += 1
                elif v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE:
                    insuff_count += 1
                else:  # EVALUATOR_ERROR
                    err_count += 1
                posterior = _bayesian_update_safely(posterior, v.verdict)

            runs_done += batch_size

            # Early-stop check (after min_runs floor satisfied)
            if budget.early_stop and runs_done >= min_runs:
                if posterior >= confidence:
                    early_stopped = True
                    halted_reason = "converged_pass"
                    break
                if (1.0 - posterior) >= confidence:
                    early_stopped = True
                    halted_reason = "converged_fail"
                    break

        # Determine final verdict from posterior + counts
        final_verdict = self._classify_final(
            posterior=posterior, confidence=confidence,
            pass_count=pass_count, fail_count=fail_count,
            insuff_count=insuff_count, err_count=err_count,
        )

        return self._build_verdict(
            prop_name=prop.name, kind=prop.kind,
            pass_count=pass_count, fail_count=fail_count,
            insuff_count=insuff_count, err_count=err_count,
            prior=prior, posterior=posterior,
            final_verdict=final_verdict,
            early_stopped=early_stopped,
            halted_reason=halted_reason,
            individual=tuple(individual),
            started=started,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_batch(
        self,
        *,
        oracle: PropertyOracle,
        prop: Property,
        evidence_collector: EvidenceCollector,
        start_index: int,
        batch_size: int,
    ) -> List[PropertyVerdict]:
        """Launch a batch of runs in parallel. Returns one verdict
        per slot. Per-task exceptions become EVALUATOR_ERROR
        verdicts so the batch always returns batch_size results."""
        tasks = [
            self._single_run(
                oracle=oracle, prop=prop,
                evidence_collector=evidence_collector,
                run_index=start_index + i,
            )
            for i in range(batch_size)
        ]
        return await asyncio.gather(*tasks)

    async def _single_run(
        self,
        *,
        oracle: PropertyOracle,
        prop: Property,
        evidence_collector: EvidenceCollector,
        run_index: int,
    ) -> PropertyVerdict:
        """Collect evidence + dispatch to Oracle. Defensive everywhere.
        Returns a PropertyVerdict regardless of what happens."""
        try:
            evidence = await evidence_collector(run_index)
        except Exception as exc:  # noqa: BLE001 — defensive
            tb = traceback.format_exc(limit=3)
            return PropertyVerdict(
                property_name=prop.name, kind=prop.kind,
                verdict=VerdictKind.EVALUATOR_ERROR,
                confidence=0.0,
                reason=(
                    f"evidence_collector raised {type(exc).__name__} "
                    f"on run_index={run_index}: {exc}\n{tb}"
                ),
            )
        if not isinstance(evidence, Mapping):
            return PropertyVerdict(
                property_name=prop.name, kind=prop.kind,
                verdict=VerdictKind.EVALUATOR_ERROR,
                confidence=0.0,
                reason=(
                    f"evidence_collector returned "
                    f"{type(evidence).__name__} (expected Mapping) "
                    f"on run_index={run_index}"
                ),
            )
        return oracle.evaluate(prop=prop, evidence=evidence)

    @staticmethod
    def _classify_final(
        *,
        posterior: float,
        confidence: float,
        pass_count: int,
        fail_count: int,
        insuff_count: int,
        err_count: int,
    ) -> VerdictKind:
        """Map posterior → final VerdictKind."""
        if posterior >= confidence:
            return VerdictKind.PASSED
        if (1.0 - posterior) >= confidence:
            return VerdictKind.FAILED
        # Below threshold either way — uncertain.
        # If insuff/err dominated, attribute to insufficient.
        # If pass+fail dominated but neither hit threshold, also
        # insufficient (we ran but couldn't decide).
        return VerdictKind.INSUFFICIENT_EVIDENCE

    def _build_verdict(
        self,
        *,
        prop_name: str,
        kind: str,
        pass_count: int,
        fail_count: int,
        insuff_count: int,
        err_count: int,
        prior: float,
        posterior: float,
        final_verdict: VerdictKind,
        early_stopped: bool,
        halted_reason: str,
        individual: Tuple[PropertyVerdict, ...],
        started: float,
    ) -> RepeatVerdict:
        runs_completed = (
            pass_count + fail_count + insuff_count + err_count
        )
        confidence = max(posterior, 1.0 - posterior)
        return RepeatVerdict(
            property_name=prop_name,
            kind=kind,
            runs_completed=runs_completed,
            pass_count=pass_count,
            fail_count=fail_count,
            insufficient_count=insuff_count,
            error_count=err_count,
            initial_prior=prior,
            posterior=posterior,
            confidence=confidence,
            final_verdict=final_verdict,
            early_stopped=early_stopped,
            halted_reason=halted_reason,
            individual_verdicts=individual,
            started_unix=started,
            completed_unix=_time.time(),
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_default_runner = RepeatRunner()


def get_default_runner() -> RepeatRunner:
    """Public accessor for the module-level RepeatRunner. Stateless,
    so the singleton is just for cache locality + test mocking."""
    return _default_runner


__all__ = [
    "EvidenceCollector",
    "REPEAT_VERDICT_SCHEMA_VERSION",
    "RepeatRunner",
    "RepeatVerdict",
    "RunBudget",
    "get_default_runner",
    "repeat_runner_enabled",
]
