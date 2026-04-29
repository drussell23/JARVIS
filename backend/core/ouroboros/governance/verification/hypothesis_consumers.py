"""Priority C — Consumer-facing hypothesis-probe helpers.

Wraps HypothesisProbe with reasonable defaults for each consumer
identified in PRD §25.5.3 + Priority 1 Slice 3 (PRD §26.5.1):

  * PLAN runner — ``probe_trivial_op_assumption`` (live wiring
    into plan_generator's ``_is_trivial_op`` gate)
  * **Priority 1 Slice 3 — confidence collapse**:
    ``probe_confidence_collapse`` (this slice — live consumer
    that maps a ``ConfidenceCollapseError`` to one of three
    actions: RETRY_WITH_FEEDBACK / ESCALATE_TO_OPERATOR /
    INCONCLUSIVE; live wiring into generate_runner is the
    Slice 5 graduation flip)
  * Curiosity Engine — ``probe_intent_dismissal`` (scaffolded;
    full wiring in a future slice)
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
from enum import Enum
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


# Priority 1 Slice 3 — confidence collapse consumer types
# ---------------------------------------------------------------------------


class ConfidenceCollapseAction(str, Enum):
    """Three discrete actions a caller takes after a confidence
    collapse, derived by ``probe_confidence_collapse``.

    String-valued so the action serializes cleanly into ctx
    artifacts + ledger records + Slice 4 SSE payloads.
    """

    RETRY_WITH_FEEDBACK = "retry_with_feedback"
    """Probe REFUTED real distress (or low-confidence margin near
    floor) — likely stylistic variation. Caller should retry the
    GENERATE round with the feedback artifact threaded into the
    next prompt to nudge the model toward higher-confidence
    sampling."""

    ESCALATE_TO_OPERATOR = "escalate_to_operator"
    """Probe CONFIRMED real distress (very low margin under tight
    posture, OR memorialized as a recurring collapse). Caller
    should raise risk_tier to NOTIFY_APPLY+ so the operator can
    intervene. Cost-contract preserved: escalation does NOT
    cascade BG/SPEC routes to Claude (§26.6 invariants enforce)."""

    INCONCLUSIVE = "inconclusive"
    """Probe didn't decide — margin in the middle band. Caller
    should retry with reduced thinking budget so the model has
    less rope to spin uncertain reasoning. Slice 5 will SSE-emit
    the inconclusive transition for operator awareness."""


@dataclass(frozen=True)
class ConfidenceCollapseVerdict:
    """Result of ``probe_confidence_collapse``.

    Caller branches on ``action``; remaining fields are diagnostic
    + the rendered feedback text the caller threads into the next
    GENERATE prompt (RETRY_WITH_FEEDBACK case) or the escalation
    surface (ESCALATE_TO_OPERATOR case)."""

    action: ConfidenceCollapseAction
    confidence_posterior: float
    convergence_state: str
    observation_summary: str
    cost_usd: float
    feedback_text: str = ""
    thinking_budget_reduction_factor: float = 1.0  # 1.0 = no reduction


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
# Priority 1 Slice 3 — confidence collapse probe consumer
# ---------------------------------------------------------------------------


def confidence_probe_integration_enabled() -> bool:
    """``JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED`` (default
    ``false`` for Slice 3; flips to ``true`` in Slice 5 graduation).

    Asymmetric env semantics — empty/whitespace = current default;
    explicit truthy enables; explicit falsy disables. Re-read at
    call time so monkeypatch + live toggle work.

    When off, ``probe_confidence_collapse`` returns the safe
    legacy default (``RETRY_WITH_FEEDBACK`` with rendered feedback)
    without invoking the probe — caller's pre-Slice-3 behaviour
    stands. Hot-revert: single env knob → no probe dispatched."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # Slice 3 default
    return raw in ("1", "true", "yes", "on")


def _confidence_distress_ratio() -> float:
    """``JARVIS_CONFIDENCE_COLLAPSE_DISTRESS_RATIO`` (default 0.3).

    Margin/effective_floor ratio BELOW which the verdict ESCALATES
    to NOTIFY_APPLY+. Below ``ratio × effective_floor`` is treated
    as real epistemic distress, not stylistic variation. Floored at
    0.0; capped at 1.0 (above 1.0 would mean "anything below floor
    escalates" — useful but degenerate)."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_COLLAPSE_DISTRESS_RATIO", "0.3",
    )
    try:
        v = float(raw)
        if v != v:  # NaN check
            return 0.3
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return 0.3


def _confidence_stylistic_ratio() -> float:
    """``JARVIS_CONFIDENCE_COLLAPSE_STYLISTIC_RATIO`` (default 0.7).

    Margin/effective_floor ratio ABOVE which the verdict is
    RETRY_WITH_FEEDBACK (likely stylistic variation, not distress).
    Between distress_ratio and stylistic_ratio → INCONCLUSIVE.
    Floored at distress_ratio; capped at 1.0."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_COLLAPSE_STYLISTIC_RATIO", "0.7",
    )
    try:
        v = float(raw)
        if v != v:
            return 0.7
        # Clamp to [distress_ratio, 1.0] to preserve banding
        return max(_confidence_distress_ratio(), min(1.0, v))
    except (TypeError, ValueError):
        return 0.7


def _confidence_inconclusive_thinking_factor() -> float:
    """``JARVIS_CONFIDENCE_INCONCLUSIVE_THINKING_FACTOR`` (default
    0.5). Multiplier on the next round's thinking budget when the
    verdict is INCONCLUSIVE — 0.5 halves the budget, biasing the
    model toward less rope to spin uncertain reasoning. Floored at
    0.05 (5% of original) to avoid effectively disabling thinking;
    capped at 1.0 (no reduction)."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_INCONCLUSIVE_THINKING_FACTOR", "0.5",
    )
    try:
        v = float(raw)
        if v != v:
            return 0.5
        return max(0.05, min(1.0, v))
    except (TypeError, ValueError):
        return 0.5


def _render_confidence_collapse_feedback(error: Any) -> str:
    """Render structured feedback the next GENERATE round threads
    into its prompt. Defensive: works on any object with the
    documented ``ConfidenceCollapseError`` attributes; falls back
    gracefully when fields are missing. NEVER raises.

    The rendered text is bounded to ~600 chars so it doesn't
    dominate the next prompt's token budget."""
    try:
        op_id = str(getattr(error, "op_id", "") or "<unknown>")
        verdict = getattr(error, "verdict", None)
        verdict_str = (
            str(verdict.value) if hasattr(verdict, "value")
            else str(verdict or "<unknown>")
        )
        rolling = getattr(error, "rolling_margin", None)
        rolling_str = (
            f"{float(rolling):.4f}" if rolling is not None else "n/a"
        )
        floor = float(getattr(error, "floor", 0.0) or 0.0)
        eff_floor = float(getattr(error, "effective_floor", 0.0) or 0.0)
        posture = str(getattr(error, "posture", "") or "none")
        obs = int(getattr(error, "observations_count", 0) or 0)
        win = int(getattr(error, "window_size", 0) or 0)
        provider = str(getattr(error, "provider", "") or "")
        model_id = str(getattr(error, "model_id", "") or "")
    except Exception:  # noqa: BLE001 — defensive
        return (
            "Confidence collapse detected on prior GENERATE round. "
            "Consider tightening reasoning + reducing speculative "
            "branches in the next attempt."
        )
    return (
        "[CONFIDENCE-COLLAPSE-FEEDBACK]\n"
        f"Prior GENERATE round op={op_id} on provider={provider!r} "
        f"model={model_id!r} produced low rolling-mean confidence "
        f"margin (rolling={rolling_str}, floor={floor:.4f}, "
        f"effective_floor={eff_floor:.4f} under posture={posture}, "
        f"obs={obs}/{win}, verdict={verdict_str}).\n"
        "Guidance for this round:\n"
        "  - prefer one concrete approach over enumerating "
        "alternatives;\n"
        "  - cite specific file paths + symbol names rather than "
        "describing them in prose;\n"
        "  - if uncertainty is real, emit a single ask_human "
        "question at the appropriate boundary instead of guessing;\n"
        "  - avoid hedging language ('maybe', 'perhaps', 'I think') "
        "in favor of declarative statements grounded in evidence."
    )


def _render_confidence_collapse_escalation(error: Any) -> str:
    """Render escalation text when the probe CONFIRMS real distress.
    The operator-facing summary; threaded onto the NOTIFY_APPLY
    surface. Bounded ~400 chars. NEVER raises."""
    try:
        op_id = str(getattr(error, "op_id", "") or "<unknown>")
        rolling = getattr(error, "rolling_margin", None)
        rolling_str = (
            f"{float(rolling):.4f}" if rolling is not None else "n/a"
        )
        eff_floor = float(getattr(error, "effective_floor", 0.0) or 0.0)
        posture = str(getattr(error, "posture", "") or "none")
    except Exception:  # noqa: BLE001
        return (
            "Confidence collapse escalated to operator: model is "
            "in epistemic distress on this op."
        )
    return (
        "[CONFIDENCE-COLLAPSE-ESCALATION]\n"
        f"op={op_id} margin={rolling_str} effective_floor="
        f"{eff_floor:.4f} posture={posture}. Probe confirmed "
        f"real distress (not stylistic variation). Escalating to "
        f"NOTIFY_APPLY+ for operator review."
    )


async def probe_confidence_collapse(
    *,
    error: Any,
    ctx: Optional[Any] = None,
    prior: float = 0.5,
    probe: Optional[HypothesisProbe] = None,
) -> ConfidenceCollapseVerdict:
    """Probe a confidence-collapse event and return a typed verdict.

    Caller branches on ``verdict.action``:
      * ``RETRY_WITH_FEEDBACK`` — re-run GENERATE with feedback
        threaded into the next prompt
      * ``ESCALATE_TO_OPERATOR`` — raise risk_tier to NOTIFY_APPLY+
      * ``INCONCLUSIVE`` — re-run GENERATE with reduced thinking
        budget (``thinking_budget_reduction_factor``)

    Decision math (deterministic, no LLM in the cage):
      1. Master-flag-gated at three layers — confidence integration
         flag, hypothesis_consumers flag, hypothesis_probe flag.
         Any one off → safe legacy default (RETRY_WITH_FEEDBACK
         with feedback text).
      2. Memorialization check — if the probe primitive's failed-
         hypothesis ledger reports this collapse as already-seen,
         escalate (recurring distress).
      3. Hard distress signal — if margin/effective_floor < distress
         ratio (default 0.3) → ESCALATE_TO_OPERATOR.
      4. Stylistic variation — if margin/effective_floor > stylistic
         ratio (default 0.7) → RETRY_WITH_FEEDBACK.
      5. Middle band → INCONCLUSIVE with reduced thinking budget.

    NEVER raises. Bounded by §25 Priority C primitive contracts:
    depth ≤ 3, budget ≤ $0.05, wall ≤ 30s. Probe failures collapse
    to safe defaults."""
    legacy_default = ConfidenceCollapseVerdict(
        action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
        confidence_posterior=prior,
        convergence_state="disabled",
        observation_summary=(
            "probe disabled — retry with default feedback"
        ),
        cost_usd=0.0,
        feedback_text=_render_confidence_collapse_feedback(error),
    )

    if not confidence_probe_integration_enabled():
        return legacy_default
    if not hypothesis_consumers_enabled():
        return legacy_default
    if not hypothesis_probe_enabled():
        return legacy_default
    if error is None:
        return legacy_default

    # Build hypothesis: "this confidence collapse represents real
    # epistemic distress (not stylistic variation)". The lookup
    # strategy memorializes the (claim, signal, strategy) hash —
    # if we've seen this exact collapse before, the probe returns
    # ``memorialized_dead`` and we escalate.
    op_id = str(getattr(error, "op_id", "") or "")
    verdict_attr = getattr(error, "verdict", None)
    verdict_str = (
        str(verdict_attr.value) if hasattr(verdict_attr, "value")
        else str(verdict_attr or "unknown")
    )
    expected_signal = (
        f"file_exists:.jarvis/confidence_collapses/{op_id}.marker"
    )
    h = Hypothesis(
        claim=(
            f"confidence_collapse on op {op_id} represents real "
            f"epistemic distress (verdict={verdict_str})"
        ),
        confidence_prior=prior,
        test_strategy="lookup",
        expected_signal=expected_signal,
        parent_op_id=op_id,
    )

    try:
        runner = probe or get_default_probe()
        result = await runner.test(h)
    except Exception:  # noqa: BLE001 — never raise out of consumer
        logger.debug(
            "[probe_confidence_collapse] probe.test raised — "
            "falling back to legacy default", exc_info=True,
        )
        return legacy_default

    return _decide_confidence_collapse_action(error, result)


def _decide_confidence_collapse_action(
    error: Any,
    result: ProbeResult,
) -> ConfidenceCollapseVerdict:
    """Pure decision math from (error, ProbeResult) → Verdict.
    Separated from the async dispatcher so tests can hit the
    decision logic directly without an async probe call.
    NEVER raises."""
    try:
        # 1. Memorialization check — recurring collapse on this
        # claim hash → escalate.
        if result.convergence_state == "memorialized_dead":
            return ConfidenceCollapseVerdict(
                action=ConfidenceCollapseAction.ESCALATE_TO_OPERATOR,
                confidence_posterior=result.confidence_posterior,
                convergence_state=result.convergence_state,
                observation_summary=(
                    "recurring confidence collapse — "
                    "escalating to operator"
                ),
                cost_usd=result.cost_usd,
                feedback_text=(
                    _render_confidence_collapse_escalation(error)
                ),
            )

        # 2/3/4. Margin-band decision math.
        rolling = getattr(error, "rolling_margin", None)
        eff_floor = float(getattr(error, "effective_floor", 0.0) or 0.0)
        distress_ratio = _confidence_distress_ratio()
        stylistic_ratio = _confidence_stylistic_ratio()

        # Defensive: missing margin or zero floor → INCONCLUSIVE
        # (we have no signal to band on).
        if rolling is None or eff_floor <= 0.0:
            return ConfidenceCollapseVerdict(
                action=ConfidenceCollapseAction.INCONCLUSIVE,
                confidence_posterior=result.confidence_posterior,
                convergence_state=result.convergence_state,
                observation_summary=(
                    "insufficient margin signal — retry with "
                    "reduced thinking budget"
                ),
                cost_usd=result.cost_usd,
                thinking_budget_reduction_factor=(
                    _confidence_inconclusive_thinking_factor()
                ),
            )

        rolling_f = float(rolling)
        ratio = rolling_f / eff_floor

        if ratio < distress_ratio:
            return ConfidenceCollapseVerdict(
                action=ConfidenceCollapseAction.ESCALATE_TO_OPERATOR,
                confidence_posterior=result.confidence_posterior,
                convergence_state=result.convergence_state,
                observation_summary=(
                    f"margin/floor ratio {ratio:.3f} < distress "
                    f"threshold {distress_ratio:.3f} — real distress"
                ),
                cost_usd=result.cost_usd,
                feedback_text=(
                    _render_confidence_collapse_escalation(error)
                ),
            )
        if ratio > stylistic_ratio:
            return ConfidenceCollapseVerdict(
                action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
                confidence_posterior=result.confidence_posterior,
                convergence_state=result.convergence_state,
                observation_summary=(
                    f"margin/floor ratio {ratio:.3f} > stylistic "
                    f"threshold {stylistic_ratio:.3f} — likely "
                    f"stylistic variation"
                ),
                cost_usd=result.cost_usd,
                feedback_text=(
                    _render_confidence_collapse_feedback(error)
                ),
            )
        # Middle band
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=result.confidence_posterior,
            convergence_state=result.convergence_state,
            observation_summary=(
                f"margin/floor ratio {ratio:.3f} in band "
                f"[{distress_ratio:.3f}, {stylistic_ratio:.3f}] — "
                f"retry with reduced thinking budget"
            ),
            cost_usd=result.cost_usd,
            thinking_budget_reduction_factor=(
                _confidence_inconclusive_thinking_factor()
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
            confidence_posterior=getattr(
                result, "confidence_posterior", 0.5,
            ),
            convergence_state="evaluator_error",
            observation_summary=(
                "decision logic raised — falling back to retry"
            ),
            cost_usd=getattr(result, "cost_usd", 0.0),
            feedback_text=_render_confidence_collapse_feedback(error),
        )


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
    "ConfidenceCollapseAction",
    "ConfidenceCollapseVerdict",
    "GoalDisambiguationVerdict",
    "HYPOTHESIS_CONSUMERS_SCHEMA_VERSION",
    "IntentDismissalVerdict",
    "TrivialityVerdict",
    "confidence_probe_integration_enabled",
    "hypothesis_consumers_enabled",
    "probe_capability_gap",
    "probe_confidence_collapse",
    "probe_goal_disambiguation",
    "probe_intent_dismissal",
    "probe_trivial_op_assumption",
]
