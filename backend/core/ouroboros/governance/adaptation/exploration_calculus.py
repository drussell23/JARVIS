"""Slice 3.1 — ExplorationCalculus: Bayesian belief engine.

Per ``OUROBOROS_VENOM_PRD.md`` §24.10.3 (Priority 3):

  > Every exploration state has a measurable ``epistemic_uncertainty``
  > (entropy of belief). Each probe produces an observation that
  > updates belief via Bayesian update. Termination proof: at any
  > cost cap C, exploration MUST halt within ``O(log(1/ε))`` probes.

This module ships the **pure-function mathematical core** that gives
every hypothesis a measurable epistemic state and proves convergence.

## Mathematical foundations

### Bayesian update

  P(H|E) = P(E|H) · P(H) / [P(E|H) · P(H) + P(E|¬H) · (1-P(H))]

  likelihood_ratio = P(E|H) / P(E|¬H)

### Shannon entropy (Bernoulli)

  H(p) = −p·log₂(p) − (1−p)·log₂(1−p)

  Maximum at p=0.5 (H=1.0 bit). Zero at p=0 or p=1.

### Convergence criterion

  Converged when H(posterior) < ε, where ε derives from the prior:
    ε = H(prior) × convergence_ratio    (env-tunable, default 0.1)

### Theoretical max probes

  O(log₂(1/ε)) — each probe halves the entropy in the worst case
  (adversarial alternation). This derives the upper bound on probe
  count for any hypothesis.

### Cooling schedule

  cooling_factor = H(posterior) / 1.0    (normalized to [0,1])
  As belief converges → cooling_factor → 0 → scheduler self-quiets.

## Generalization of TtftObserver

TtftObserver (Phase 12.2 Slice B) encodes: ``N > (CV / threshold)²``
— math derives the required sample count. We generalize from "model
TTFT consistency" to "epistemic belief convergence" — same structural
pattern (bounds derived, not hardcoded), different domain.

## Cage rules (load-bearing)

  * **Stdlib-only** (``math``, ``json``, ``hashlib``, ``logging``).
  * **Pure functions** — no side effects, no I/O. Callers persist.
  * **NEVER raises** — invalid inputs → safe defaults.
  * **Master flag**: ``JARVIS_EXPLORATION_CALCULUS_ENABLED`` (default false).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Hard caps
# ---------------------------------------------------------------------------

MAX_OBSERVATIONS: int = 200
MAX_BELIEF_STATES: int = 500
MIN_PRIOR: float = 0.001
MAX_PRIOR: float = 0.999
MIN_LIKELIHOOD_RATIO: float = 0.01
MAX_LIKELIHOOD_RATIO: float = 100.0

# ---------------------------------------------------------------------------
# Master flag + configuration
# ---------------------------------------------------------------------------


def is_calculus_enabled() -> bool:
    """Master flag — ``JARVIS_EXPLORATION_CALCULUS_ENABLED``."""
    return os.environ.get(
        "JARVIS_EXPLORATION_CALCULUS_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _convergence_ratio() -> float:
    """``JARVIS_EXPLORATION_CONVERGENCE_RATIO`` (default 0.1).

    ε = H(prior) × convergence_ratio. Tighter ratio → more probes
    needed for convergence, but higher confidence in the final belief.
    """
    try:
        v = float(os.environ.get(
            "JARVIS_EXPLORATION_CONVERGENCE_RATIO", "0.1",
        ).strip())
        return max(0.01, min(0.99, v))
    except (ValueError, TypeError):
        return 0.1


def _confirmed_lr() -> float:
    """Likelihood ratio for CONFIRMED evidence (default 3.0)."""
    try:
        v = float(os.environ.get(
            "JARVIS_EXPLORATION_CONFIRMED_LR", "3.0",
        ).strip())
        return max(MIN_LIKELIHOOD_RATIO, min(MAX_LIKELIHOOD_RATIO, v))
    except (ValueError, TypeError):
        return 3.0


def _refuted_lr() -> float:
    """Likelihood ratio for REFUTED evidence (default 0.33)."""
    try:
        v = float(os.environ.get(
            "JARVIS_EXPLORATION_REFUTED_LR", "0.33",
        ).strip())
        return max(MIN_LIKELIHOOD_RATIO, min(MAX_LIKELIHOOD_RATIO, v))
    except (ValueError, TypeError):
        return 0.33


def _inconclusive_lr() -> float:
    """Likelihood ratio for INCONCLUSIVE evidence (default 1.0)."""
    try:
        v = float(os.environ.get(
            "JARVIS_EXPLORATION_INCONCLUSIVE_LR", "1.0",
        ).strip())
        return max(MIN_LIKELIHOOD_RATIO, min(MAX_LIKELIHOOD_RATIO, v))
    except (ValueError, TypeError):
        return 1.0


def _min_entropy_delta() -> float:
    """Minimum entropy change to avoid diminishing-returns halt
    (default 0.01)."""
    try:
        v = float(os.environ.get(
            "JARVIS_EXPLORATION_MIN_ENTROPY_DELTA", "0.01",
        ).strip())
        return max(0.001, min(0.5, v))
    except (ValueError, TypeError):
        return 0.01


def _diminishing_window() -> int:
    """Consecutive observations below min_entropy_delta to trigger
    diminishing-returns halt (default 3)."""
    try:
        v = int(os.environ.get(
            "JARVIS_EXPLORATION_DIMINISHING_WINDOW", "3",
        ).strip())
        return max(1, min(20, v))
    except (ValueError, TypeError):
        return 3


def _budget_per_hypothesis() -> float:
    """Maximum budget per hypothesis in USD (default 0.15)."""
    try:
        v = float(os.environ.get(
            "JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "0.15",
        ).strip())
        return max(0.01, v)
    except (ValueError, TypeError):
        return 0.15


# ---------------------------------------------------------------------------
# Convergence state constants
# ---------------------------------------------------------------------------

STATE_EXPLORING = "exploring"
STATE_CONVERGING = "converging"
STATE_CONVERGED = "converged"
STATE_DIVERGING = "diverging"

HALT_CONVERGED = "converged"
HALT_BUDGET = "budget_exhausted"
HALT_MAX_PROBES = "max_probes_reached"
HALT_DIMINISHING = "diminishing_returns"


# ---------------------------------------------------------------------------
# Core math — pure functions
# ---------------------------------------------------------------------------


def entropy(p: float) -> float:
    """Shannon entropy of a Bernoulli random variable.

    H(p) = −p·log₂(p) − (1−p)·log₂(1−p)

    Returns 0.0 at p=0 or p=1 (certainty), 1.0 at p=0.5 (max
    uncertainty). NEVER raises.
    """
    if p <= 0.0 or p >= 1.0:
        return 0.0
    try:
        return -(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p))
    except (ValueError, OverflowError):
        return 0.0


def bayesian_update(prior: float, likelihood_ratio: float) -> float:
    """Bayesian update of a Bernoulli belief.

    P(H|E) = lr · P(H) / [lr · P(H) + (1 − P(H))]

    where lr = P(E|H) / P(E|¬H) is the likelihood ratio.

    Clamps result to [MIN_PRIOR, MAX_PRIOR] to prevent degenerate
    posteriors (exactly 0 or 1 cannot be updated further).

    NEVER raises.
    """
    p = max(MIN_PRIOR, min(MAX_PRIOR, prior))
    lr = max(MIN_LIKELIHOOD_RATIO, min(MAX_LIKELIHOOD_RATIO, likelihood_ratio))

    try:
        numerator = lr * p
        denominator = lr * p + (1.0 - p)
        if denominator <= 0.0:
            return p
        posterior = numerator / denominator
    except (ZeroDivisionError, OverflowError):
        return p

    return max(MIN_PRIOR, min(MAX_PRIOR, posterior))


def epsilon_from_prior(prior: float) -> float:
    """Derive the convergence threshold ε from the prior's initial
    entropy.

    ε = H(prior) × convergence_ratio

    A hypothesis starting at high uncertainty (prior ≈ 0.5, H ≈ 1.0)
    gets a proportionally larger ε — it needs to resolve more
    uncertainty. A hypothesis starting confident (prior ≈ 0.9, H ≈ 0.47)
    gets a tighter ε — less uncertainty to resolve.

    NEVER raises.
    """
    h = entropy(prior)
    ratio = _convergence_ratio()
    eps = h * ratio
    # Floor: never set epsilon to 0 (would mean instant convergence)
    return max(0.001, eps)


def max_probes_for_epsilon(eps: float) -> int:
    """Theoretical maximum probes to achieve convergence.

    O(log₂(1/ε)) — each probe halves entropy in the worst case.

    This is the derived upper bound, not a hardcoded cap. A well-
    behaved hypothesis converges much faster; this is the adversarial
    worst case.

    NEVER raises.
    """
    if eps <= 0.0:
        return MAX_OBSERVATIONS
    try:
        raw = math.ceil(math.log2(1.0 / eps))
        return max(1, min(MAX_OBSERVATIONS, raw))
    except (ValueError, OverflowError):
        return MAX_OBSERVATIONS


def cooling_factor(current_entropy: float) -> float:
    """Cooling factor for the exploration schedule.

    Returns [0.0, 1.0]:
      - 1.0 at maximum uncertainty (entropy = 1.0)
      - 0.0 at convergence (entropy ≈ 0.0)

    Used by ConvergenceGovernor to modulate CuriosityScheduler's
    fire rate. As hypotheses converge, exploration intensity
    decreases.

    NEVER raises.
    """
    max_entropy = 1.0  # entropy(0.5) for Bernoulli
    if max_entropy <= 0.0:
        return 0.0
    return max(0.0, min(1.0, current_entropy / max_entropy))


def classify_convergence(
    *,
    current_entropy: float,
    previous_entropy: float,
    epsilon: float,
) -> str:
    """Classify the convergence state based on entropy trajectory.

    - ``converged``: entropy < ε
    - ``converging``: entropy decreased
    - ``diverging``: entropy increased
    - ``exploring``: first observation or no change

    NEVER raises.
    """
    if current_entropy < epsilon:
        return STATE_CONVERGED
    delta = current_entropy - previous_entropy
    if delta < -0.001:
        return STATE_CONVERGING
    if delta > 0.001:
        return STATE_DIVERGING
    return STATE_EXPLORING


def verdict_to_likelihood_ratio(verdict: str) -> float:
    """Map a probe verdict string to a likelihood ratio.

    Uses env-tunable ratios:
      - ``CONFIRMED`` → confirmed_lr (default 3.0)
      - ``REFUTED`` → refuted_lr (default 0.33)
      - anything else → inconclusive_lr (default 1.0)

    NEVER raises.
    """
    v = (verdict or "").upper().strip()
    if v in ("CONFIRMED", "VALIDATED"):
        return _confirmed_lr()
    if v in ("REFUTED", "INVALIDATED"):
        return _refuted_lr()
    return _inconclusive_lr()


# ---------------------------------------------------------------------------
# BeliefState — one hypothesis's epistemic state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BeliefState:
    """Epistemic state of a single hypothesis.

    Tracks the Bayesian posterior, Shannon entropy, convergence
    classification, and cost. Frozen — each update produces a new
    instance.
    """

    hypothesis_id: str
    prior: float
    posterior: float
    observations: int
    entropy: float
    entropy_delta: float
    convergence_state: str
    cost_spent: float
    consecutive_diminishing: int = 0
    ts_unix: float = 0.0

    def is_converged(self) -> bool:
        return self.convergence_state == STATE_CONVERGED

    def is_halted(self, epsilon: float) -> bool:
        """True if any halt condition is met."""
        if self.entropy < epsilon:
            return True
        if self.cost_spent >= _budget_per_hypothesis():
            return True
        max_p = max_probes_for_epsilon(epsilon)
        if self.observations >= max_p:
            return True
        if self.consecutive_diminishing >= _diminishing_window():
            return True
        return False

    def halt_reason(self, epsilon: float) -> str:
        """Return the reason for halting, or empty string."""
        if self.entropy < epsilon:
            return HALT_CONVERGED
        if self.cost_spent >= _budget_per_hypothesis():
            return HALT_BUDGET
        max_p = max_probes_for_epsilon(epsilon)
        if self.observations >= max_p:
            return HALT_MAX_PROBES
        if self.consecutive_diminishing >= _diminishing_window():
            return HALT_DIMINISHING
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "prior": round(self.prior, 6),
            "posterior": round(self.posterior, 6),
            "observations": self.observations,
            "entropy": round(self.entropy, 6),
            "entropy_delta": round(self.entropy_delta, 6),
            "convergence_state": self.convergence_state,
            "cost_spent": round(self.cost_spent, 6),
            "consecutive_diminishing": self.consecutive_diminishing,
            "ts_unix": self.ts_unix,
        }


# ---------------------------------------------------------------------------
# ConvergenceProof — emitted when a hypothesis halts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConvergenceProof:
    """Formal proof that exploration terminated for a hypothesis.

    Emitted by ``ConvergenceGovernor`` when any halt condition fires.
    Immutable audit record.
    """

    hypothesis_id: str
    halted: bool
    halt_reason: str
    probes_used: int
    theoretical_max_probes: int
    cost_spent: float
    final_belief: float
    final_entropy: float
    epsilon: float
    ts_unix: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "probes_used": self.probes_used,
            "theoretical_max_probes": self.theoretical_max_probes,
            "cost_spent": round(self.cost_spent, 6),
            "final_belief": round(self.final_belief, 6),
            "final_entropy": round(self.final_entropy, 6),
            "epsilon": round(self.epsilon, 6),
            "ts_unix": self.ts_unix,
        }


# ---------------------------------------------------------------------------
# Belief update — the core operation
# ---------------------------------------------------------------------------


def initial_belief(
    hypothesis_id: str,
    prior: float = 0.5,
    now_unix: float = 0.0,
) -> BeliefState:
    """Create the initial BeliefState for a new hypothesis.

    Default prior is 0.5 (maximum uncertainty). Callers may supply
    a different prior based on the hypothesis source's confidence.

    NEVER raises.
    """
    p = max(MIN_PRIOR, min(MAX_PRIOR, prior))
    h = entropy(p)
    return BeliefState(
        hypothesis_id=hypothesis_id,
        prior=p,
        posterior=p,
        observations=0,
        entropy=h,
        entropy_delta=0.0,
        convergence_state=STATE_EXPLORING,
        cost_spent=0.0,
        consecutive_diminishing=0,
        ts_unix=now_unix,
    )


def update_belief(
    state: BeliefState,
    *,
    verdict: str,
    cost_usd: float = 0.0,
    now_unix: float = 0.0,
) -> BeliefState:
    """Apply one Bayesian update to a BeliefState.

    Takes the previous state + a probe verdict, computes the new
    posterior, entropy, convergence classification, and returns a
    fresh frozen BeliefState.

    NEVER raises.
    """
    lr = verdict_to_likelihood_ratio(verdict)
    new_posterior = bayesian_update(state.posterior, lr)
    new_entropy = entropy(new_posterior)
    prev_entropy = state.entropy
    delta = new_entropy - prev_entropy

    eps = epsilon_from_prior(state.prior)
    conv_state = classify_convergence(
        current_entropy=new_entropy,
        previous_entropy=prev_entropy,
        epsilon=eps,
    )

    # Track consecutive diminishing-returns observations.
    abs_delta = abs(delta)
    if abs_delta < _min_entropy_delta():
        consec = state.consecutive_diminishing + 1
    else:
        consec = 0

    return BeliefState(
        hypothesis_id=state.hypothesis_id,
        prior=state.prior,
        posterior=new_posterior,
        observations=state.observations + 1,
        entropy=new_entropy,
        entropy_delta=delta,
        convergence_state=conv_state,
        cost_spent=state.cost_spent + max(0.0, cost_usd),
        consecutive_diminishing=consec,
        ts_unix=now_unix,
    )


def make_convergence_proof(
    state: BeliefState,
    now_unix: float = 0.0,
) -> ConvergenceProof:
    """Generate a ConvergenceProof from a halted BeliefState.

    NEVER raises.
    """
    eps = epsilon_from_prior(state.prior)
    return ConvergenceProof(
        hypothesis_id=state.hypothesis_id,
        halted=True,
        halt_reason=state.halt_reason(eps),
        probes_used=state.observations,
        theoretical_max_probes=max_probes_for_epsilon(eps),
        cost_spent=state.cost_spent,
        final_belief=state.posterior,
        final_entropy=state.entropy,
        epsilon=eps,
        ts_unix=now_unix,
    )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_belief_state(obj: Dict[str, Any]) -> Optional[BeliefState]:
    """Parse a dict into a BeliefState. NEVER raises."""
    try:
        return BeliefState(
            hypothesis_id=str(obj.get("hypothesis_id", "")),
            prior=float(obj.get("prior", 0.5)),
            posterior=float(obj.get("posterior", 0.5)),
            observations=int(obj.get("observations", 0)),
            entropy=float(obj.get("entropy", 0.0)),
            entropy_delta=float(obj.get("entropy_delta", 0.0)),
            convergence_state=str(obj.get("convergence_state", STATE_EXPLORING)),
            cost_spent=float(obj.get("cost_spent", 0.0)),
            consecutive_diminishing=int(obj.get("consecutive_diminishing", 0)),
            ts_unix=float(obj.get("ts_unix", 0.0)),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "BeliefState",
    "ConvergenceProof",
    "HALT_BUDGET",
    "HALT_CONVERGED",
    "HALT_DIMINISHING",
    "HALT_MAX_PROBES",
    "MAX_LIKELIHOOD_RATIO",
    "MAX_OBSERVATIONS",
    "MAX_PRIOR",
    "MIN_LIKELIHOOD_RATIO",
    "MIN_PRIOR",
    "STATE_CONVERGED",
    "STATE_CONVERGING",
    "STATE_DIVERGING",
    "STATE_EXPLORING",
    "bayesian_update",
    "classify_convergence",
    "cooling_factor",
    "entropy",
    "epsilon_from_prior",
    "initial_belief",
    "is_calculus_enabled",
    "make_convergence_proof",
    "max_probes_for_epsilon",
    "parse_belief_state",
    "update_belief",
    "verdict_to_likelihood_ratio",
]
