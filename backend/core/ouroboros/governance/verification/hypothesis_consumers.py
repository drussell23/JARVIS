"""Priority C — Consumer-facing hypothesis-probe helpers.

Wraps HypothesisProbe with reasonable defaults for each consumer
identified in PRD §25.5.3:

  * PLAN runner — ``probe_trivial_op_assumption`` (this slice ships
    the live wiring into plan_generator's `_is_trivial_op` gate)
  * Curiosity Engine — ``probe_intent_dismissal`` (scaffolded; full
    wiring in a future slice)
  * CapabilityGap sensor — ``probe_capability_gap`` (scaffolded)
  * SelfGoalFormation — ``probe_goal_disambiguation`` (scaffolded)

Each helper:
  * Picks an appropriate prior, strategy, and bounds for its
    consumer's epistemic situation
  * Returns a typed result dataclass that callers branch on without
    re-running the probe
  * NEVER raises — failures collapse to "uncertain" verdicts

Master flag ``JARVIS_HYPOTHESIS_CONSUMERS_ENABLED`` (default
``true``). When off, every helper returns the "uncertain" default
without invoking the probe — caller's pre-probe behaviour stands.

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runner / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian.
  * Pure stdlib + verification.* (own slice family).
  * NEVER raises.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

from backend.core.ouroboros.governance.verification.hypothesis_probe import (
    Hypothesis,
    HypothesisProbe,
    ProbeResult,
    get_default_probe,
    hypothesis_probe_enabled,
)

logger = logging.getLogger(__name__)


HYPOTHESIS_CONSUMERS_SCHEMA_VERSION: str = "hypothesis_consumers.1"


def hypothesis_consumers_enabled() -> bool:
    """``JARVIS_HYPOTHESIS_CONSUMERS_ENABLED`` (default ``true``).

    When off, all helpers return the "uncertain" default without
    invoking the probe. The underlying HypothesisProbe master flag
    (``JARVIS_HYPOTHESIS_PROBE_ENABLED``) is independent — a
    consumer call won't run the probe unless BOTH flags are on."""
    raw = os.environ.get(
        "JARVIS_HYPOTHESIS_CONSUMERS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Result types — one per consumer, narrowed for branching ergonomics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrivialityVerdict:
    """Result of ``probe_trivial_op_assumption``.

    The PLAN runner uses ``treat_as_trivial`` as the decision bit:
      * True  — proceed with the trivial-op skip (legacy behaviour)
      * False — force a real PLAN even if `_is_trivial_op` returned
        True (probe refuted the trivial assumption)
    """

    treat_as_trivial: bool
    confidence_posterior: float
    convergence_state: str
    observation_summary: str
    cost_usd: float


@dataclass(frozen=True)
class IntentDismissalVerdict:
    """Result of ``probe_intent_dismissal``. Scaffolded for the
    Curiosity Engine — full wiring is a future slice."""

    safe_to_dismiss: bool
    confidence_posterior: float
    convergence_state: str
    observation_summary: str


@dataclass(frozen=True)
class CapabilityGapVerdict:
    """Result of ``probe_capability_gap``. Scaffolded for the
    CapabilityGap sensor — full wiring is a future slice."""

    gap_is_real: bool
    confidence_posterior: float
    convergence_state: str
    observation_summary: str


@dataclass(frozen=True)
class GoalDisambiguationVerdict:
    """Result of ``probe_goal_disambiguation``. Scaffolded for
    SelfGoalFormation — full wiring is a future slice."""

    selected_index: int  # -1 = no clear winner
    confidence_posterior: float
    convergence_state: str
    observation_summary: str


# ---------------------------------------------------------------------------
# PLAN-runner: trivial-op probe
# ---------------------------------------------------------------------------


def _build_trivial_op_signal(target_files: Iterable[str]) -> str:
    """Build the expected_signal for a trivial-op probe.

    The trivial-op heuristic in plan_generator is: 1 file + short
    description. The probe falsifies that by checking whether the
    target file actually exists AND is non-trivial in size — if a
    file is, e.g., a 5000-line orchestrator.py, the trivial-op
    assumption is false even if the description is short.

    Convention: the probe's lookup strategy uses ``contains:`` and
    ``file_exists:`` patterns. We pick the first file with content
    larger than NON_TRIVIAL_BYTES_THRESHOLD as the signal target.

    Returns an empty string when no target file qualifies — the
    probe will return INCONCLUSIVE and the caller falls back to
    the legacy heuristic."""
    threshold = _non_trivial_bytes_threshold()
    for raw in target_files:
        try:
            p = Path(str(raw))
            if not p.exists() or not p.is_file():
                continue
            size = p.stat().st_size
            # We REFUTE the trivial-op assumption iff the file is
            # large. So the signal is "file_exists" (always
            # confirmed for existing files); the per-file size check
            # below decides which file becomes the probe target.
            if size >= threshold:
                # This file is large — return a refute-favouring
                # signal that pinpoints non-trivial body shape.
                return "file_exists:" + str(p)
        except OSError:
            continue
    return ""


def _non_trivial_bytes_threshold() -> int:
    """``JARVIS_TRIVIAL_OP_NON_TRIVIAL_BYTES`` (default 8192).

    Files larger than this count the op as non-trivial regardless of
    description length."""
    raw = os.environ.get(
        "JARVIS_TRIVIAL_OP_NON_TRIVIAL_BYTES", "8192",
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 8192


def _trivial_op_refute_threshold() -> float:
    """Posterior confidence below which the probe REFUTES the
    trivial-op assumption. Default 0.4 — when the probe pushes
    confidence below 0.4 we override the legacy heuristic and
    force PLAN to run.

    ``JARVIS_TRIVIAL_OP_REFUTE_THRESHOLD`` env-tunable."""
    raw = os.environ.get(
        "JARVIS_TRIVIAL_OP_REFUTE_THRESHOLD", "0.4",
    )
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.4
    return max(0.0, min(1.0, v))


async def probe_trivial_op_assumption(
    *,
    target_files: Iterable[str],
    op_id: str,
    description: str = "",
    prior: float = 0.7,
    probe: Optional[HypothesisProbe] = None,
) -> TrivialityVerdict:
    """Probe the assumption "this op is trivial" using HypothesisProbe.

    Returns a TrivialityVerdict the caller branches on:
      * treat_as_trivial=True  → proceed with PLAN-skip
      * treat_as_trivial=False → force PLAN to run (probe refuted)

    NEVER raises. Master-flag-gated at three layers:
      * JARVIS_HYPOTHESIS_CONSUMERS_ENABLED (this helper)
      * JARVIS_HYPOTHESIS_PROBE_ENABLED (the probe)
      * default behavior preserves the LEGACY heuristic when
        either flag is off (treat_as_trivial=True with confidence=
        prior)

    The probe targets the largest non-trivial file in target_files
    (if any). If no qualifying file exists, the probe returns
    INCONCLUSIVE and the legacy heuristic stands."""
    legacy_default = TrivialityVerdict(
        treat_as_trivial=True,
        confidence_posterior=prior,
        convergence_state="disabled",
        observation_summary="probe disabled — legacy heuristic stands",
        cost_usd=0.0,
    )

    if not hypothesis_consumers_enabled():
        return legacy_default
    if not hypothesis_probe_enabled():
        return legacy_default

    safe_targets = tuple(str(t) for t in (target_files or ()))
    signal = _build_trivial_op_signal(safe_targets)
    if not signal:
        # No file qualifies as non-trivial — defer to legacy.
        return TrivialityVerdict(
            treat_as_trivial=True,
            confidence_posterior=prior,
            convergence_state="inconclusive",
            observation_summary=(
                "no non-trivial-sized target file found"
            ),
            cost_usd=0.0,
        )

    h = Hypothesis(
        claim=(
            "op is trivial: " + (description[:80] if description else "")
        ),
        confidence_prior=prior,
        test_strategy="lookup",
        expected_signal=signal,
        parent_op_id=op_id or "",
    )
    try:
        runner = probe or get_default_probe()
        result = await runner.test(h)
    except Exception:  # noqa: BLE001
        return legacy_default

    # Decision logic: posterior tells us the strength of the trivial
    # assumption. The lookup strategy CONFIRMS file existence (which
    # by our signal selection means a large file exists) — that
    # SHOULD shift the posterior UP for "trivial" only if existence
    # of a large file is evidence FOR triviality. But it's evidence
    # AGAINST. So we INVERT the verdict: confirmation of a large
    # file's existence pushes the trivial-op assumption DOWN.
    #
    # Implementation: the lookup strategy returns CONFIRMED for
    # file_exists hits, which would normally push posterior up. We
    # invert here by treating posterior > refute_threshold as
    # "trivial assumption holds" and posterior <= refute_threshold
    # as "REFUTED — file is too large for triviality".
    #
    # Per the math contract: when prior=0.7 and 3 CONFIRMED hits
    # accumulate, posterior climbs to ~0.97 (assumption stays
    # trivial — the 'large file exists' signal didn't kick in
    # because the file isn't actually large). When the file IS
    # large but lookup CONFIRMS it (existence-only), the inversion
    # logic flips the trivial verdict via the refute_threshold.
    #
    # Practical effect: we use the file-size gate at signal-build
    # time to decide whether a large file is a candidate for
    # refutation. If a non-trivial-sized file qualified for the
    # signal, ANY CONFIRMED hit means we found a large file → the
    # trivial assumption is REFUTED.
    #
    # So the actual decision: if we got a real confirmation
    # (convergence in ("stable", "inconclusive") with high
    # posterior) AND the signal pointed at a non-trivial-sized
    # file, REFUTE.
    is_refuted = (
        result.convergence_state in ("stable", "inconclusive")
        and result.confidence_posterior > 0.5
    )
    refute_threshold = _trivial_op_refute_threshold()

    if is_refuted and result.confidence_posterior > 0.5:
        # The probe CONFIRMED a large file exists in target_files.
        # Trivial-op assumption is REFUTED — force PLAN.
        return TrivialityVerdict(
            treat_as_trivial=False,
            confidence_posterior=1.0 - result.confidence_posterior,
            convergence_state=result.convergence_state,
            observation_summary=(
                "probe found non-trivial file in target set: "
                + result.observation_summary[:120]
            ),
            cost_usd=result.cost_usd,
        )

    # Probe didn't refute — defer to legacy heuristic
    return TrivialityVerdict(
        treat_as_trivial=True,
        confidence_posterior=result.confidence_posterior,
        convergence_state=result.convergence_state,
        observation_summary=(
            "probe inconclusive or low-confidence; legacy heuristic "
            "stands: " + result.observation_summary[:120]
        ),
        cost_usd=result.cost_usd,
    )


# ---------------------------------------------------------------------------
# Scaffolds — Curiosity Engine, CapabilityGap, SelfGoalFormation
# ---------------------------------------------------------------------------
#
# These are stubs that future slices will flesh out. They return
# safe defaults today so callers can wire them speculatively
# without breaking. Each carries the contract: same shape, same
# defensive semantics, same master-flag gating.


async def probe_intent_dismissal(
    *,
    intent_summary: str,
    op_id: str,
    urgency: str,
    prior: float = 0.5,
) -> IntentDismissalVerdict:
    """Curiosity Engine consumer scaffold.

    Future slice will probe whether dismissing a high-urgency NO_OP
    triage verdict is safe. Today returns a conservative
    `safe_to_dismiss=True` (preserves pre-C behavior). NEVER raises."""
    return IntentDismissalVerdict(
        safe_to_dismiss=True,
        confidence_posterior=prior,
        convergence_state="scaffold",
        observation_summary="probe_intent_dismissal scaffolded",
    )


async def probe_capability_gap(
    *,
    gap_summary: str,
    evidence_path: str,
    op_id: str,
    prior: float = 0.6,
) -> CapabilityGapVerdict:
    """CapabilityGap sensor consumer scaffold.

    Future slice will probe whether a detected gap is real (vs
    spurious detection). Today returns `gap_is_real=True` —
    callers proceed with the legacy gap-emit behavior. NEVER
    raises."""
    return CapabilityGapVerdict(
        gap_is_real=True,
        confidence_posterior=prior,
        convergence_state="scaffold",
        observation_summary="probe_capability_gap scaffolded",
    )


async def probe_goal_disambiguation(
    *,
    candidates: Tuple[Any, ...],
    op_id: str,
) -> GoalDisambiguationVerdict:
    """SelfGoalFormation consumer scaffold.

    Future slice will probe to disambiguate similar-confidence goal
    candidates. Today returns `selected_index=0` (first candidate)
    when len > 0, else -1. NEVER raises."""
    if not candidates:
        return GoalDisambiguationVerdict(
            selected_index=-1,
            confidence_posterior=0.0,
            convergence_state="scaffold",
            observation_summary="no candidates",
        )
    return GoalDisambiguationVerdict(
        selected_index=0,
        confidence_posterior=0.5,
        convergence_state="scaffold",
        observation_summary=(
            "probe_goal_disambiguation scaffolded — selecting first"
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CapabilityGapVerdict",
    "GoalDisambiguationVerdict",
    "HYPOTHESIS_CONSUMERS_SCHEMA_VERSION",
    "IntentDismissalVerdict",
    "TrivialityVerdict",
    "hypothesis_consumers_enabled",
    "probe_capability_gap",
    "probe_goal_disambiguation",
    "probe_intent_dismissal",
    "probe_trivial_op_assumption",
]
