"""M9 Slice 5 — Curiosity producer bridge (PRD §30.5.1).

Single import-point that converts producer-side signals
(GENERATE-phase logprobs / post-VERIFY Prophecy outcomes /
Coherence Auditor RECURRENCE_DRIFT findings) into
:class:`CuriosityCollector` observations. Mirrors the
:mod:`epistemic_budget_provider_bridge` pattern from Upgrade 1
Slice 5 — providers / phase runners / auditors lazy-import
this module; the bridge itself owns all the M9-touching policy
(master-flag check, cluster-id resolution, defensive
exception isolation).

Three entry points (one per :class:`CuriositySource`):

  * :func:`feed_logprob_entropy` — consumed at GENERATE phase
    by :mod:`phase_runners.generate_runner` (Slice 5b follow-up
    wires this; bridge is ready). ``entropy_normalized`` MUST
    be in ``[0, 1]`` (caller normalizes via per-window
    ``H / max_H_observed``).

  * :func:`feed_prophecy_error` — consumed post-VERIFY by
    :mod:`phase_runners.verify_runner`. ``predicted_risk`` is
    from :meth:`ProphecyEngine.get_risk_scores` (range
    ``[0, 1]``); ``verify_passed`` is the actual outcome.
    Bridge computes ``error = abs(predicted_risk -
    actual_outcome_indicator)``.

  * :func:`feed_recurrence_drift` — consumed by
    :mod:`verification.coherence_auditor` at the
    ``BehavioralDriftKind.RECURRENCE_DRIFT`` emission site
    (Slice 5 wires this directly). ``recurrence_count`` is
    log-scale-normalized at the collector via
    :func:`_scoring_primitives.weight_score`.

Architectural locks (operator mandate):

  * **Decision X** lazy-import — every producer site does
    ``from .curiosity_producer_bridge import feed_*`` inside
    a ``try/except`` block. ImportError or any runtime
    exception → silent no-op. M9 dormant when not graduated
    OR when collector is misconfigured.
  * **Master-flag-gated** at every entry point — bridge
    calls :func:`curiosity_gradient_enabled` and returns
    immediately when off.
  * **Cluster-id resolution via existing helper** — uses
    :func:`curiosity_collector.resolve_cluster_id` so
    SemanticIndex-optional Decision A3 is preserved
    end-to-end (no parallel resolution logic).
  * **NEVER raises** — every function is exception-isolated.
    Producers ignore the return value; the bridge's only
    contract is "it ran without breaking my caller."
  * **Authority asymmetry** (AST-pinned at Slice 5) — bridge
    MUST NOT import orchestrator / iron_gate / providers /
    urgency_router / candidate_generator / sensor_governor /
    tool_executor / change_engine / strategic_direction.
    Bridge IS allowed to import the M9 modules
    (``curiosity_collector`` + ``curiosity_gradient``) plus
    ``ide_observability_stream`` for SSE publication.

The SSE publication side: when the bridge feeds an observation
that crosses a transition boundary, it fires
:func:`publish_curiosity_event` so operators see a live trail.
Specifically:

  * Cold-start → not-cold-start ⇒ ``samples_milestone``
  * Magnitude crosses 0.5 (neutral pivot) ⇒
    ``threshold_crossed``
  * Decay reason flips NONE → STALE_FOCUS / RECURRENCE_LOOP
    ⇒ ``decay_applied``

This keeps SSE chatter bounded — only meaningful transitions
emit events, not every observation.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _normalize_outcome_indicator(verify_passed: bool) -> float:
    """Convert pass/fail boolean to numeric indicator in
    ``[0, 1]``. Pass → 0.0 (no failure); Fail → 1.0 (failure)."""
    return 0.0 if verify_passed else 1.0


def _detect_transition_kind(
    prior_score: Optional[Any],
    new_score: Optional[Any],
) -> Optional[str]:
    """Closed-string-set dispatch for transition kind. Returns
    ``None`` when no meaningful transition occurred (collector
    chatter suppression). NEVER raises."""
    if new_score is None:
        return None
    try:
        # Decay flip is the strongest signal — fire first
        prior_decay = (
            prior_score.decay_reason.value
            if prior_score is not None
            else "none"
        )
        new_decay = new_score.decay_reason.value
        if prior_decay == "none" and new_decay != "none":
            return "decay_applied"
        # Cold-start exit
        prior_cold = (
            prior_score.is_cold_start()
            if prior_score is not None
            else True
        )
        new_cold = new_score.is_cold_start()
        if prior_cold and not new_cold:
            return "samples_milestone"
        # 0.5 pivot threshold
        prior_mag = (
            float(prior_score.magnitude)
            if prior_score is not None
            else 0.0
        )
        new_mag = float(new_score.magnitude)
        if (prior_mag < 0.5) != (new_mag < 0.5):
            return "threshold_crossed"
    except Exception:  # noqa: BLE001 — defensive
        return None
    return None


def _publish_if_significant(
    prior_score: Optional[Any],
    new_score: Optional[Any],
) -> None:
    """Best-effort SSE publication. NEVER raises."""
    try:
        kind = _detect_transition_kind(prior_score, new_score)
        if kind is None or new_score is None:
            return
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_curiosity_event,
            )
        except Exception:  # noqa: BLE001 — defensive
            return
        publish_curiosity_event(
            cluster_id=new_score.cluster_id,
            transition_kind=kind,
            magnitude=float(new_score.magnitude),
            confidence=float(new_score.confidence),
            dominant_source=new_score.dominant_source.value,
            decay_reason=new_score.decay_reason.value,
            samples_count=int(new_score.samples_count),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[curiosity_producer_bridge] _publish_if_"
            "significant raised", exc_info=True,
        )


def _record_with_publish(
    *,
    record_method_name: str,
    region_or_path: Any,
    value: Any,
    op_id: str = "",
    semantic_index: Optional[Any] = None,
) -> bool:
    """Internal — resolve cluster_id, snapshot prior score,
    invoke the named record method, snapshot new score,
    publish SSE on significant transitions. Returns True on
    success, False on master-off / any exception."""
    try:
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            curiosity_gradient_enabled,
        )
        if not curiosity_gradient_enabled():
            return False
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            get_default_collector,
            resolve_cluster_id,
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        cluster_id = resolve_cluster_id(
            region_or_path,
            semantic_index=semantic_index,
        )
        collector = get_default_collector()
        prior_score = collector.score_for_cluster(cluster_id)
        method = getattr(collector, record_method_name, None)
        if method is None:
            return False
        new_score = method(cluster_id, value, op_id=op_id)
        _publish_if_significant(prior_score, new_score)
        return new_score is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[curiosity_producer_bridge] _record_with_publish "
            "raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Public producer API — three entry points (one per CuriositySource)
# ---------------------------------------------------------------------------


def feed_logprob_entropy(
    *,
    region_or_path: Any,
    entropy_normalized: float,
    op_id: str = "",
    semantic_index: Optional[Any] = None,
) -> bool:
    """Producer entry: GENERATE-phase logprob entropy.
    ``entropy_normalized`` MUST be in ``[0, 1]`` (caller
    normalizes via ``H / max_H_per_window``); collector clamps
    defensively. NEVER raises."""
    return _record_with_publish(
        record_method_name="record_logprob_entropy",
        region_or_path=region_or_path,
        value=entropy_normalized,
        op_id=op_id,
        semantic_index=semantic_index,
    )


def feed_prophecy_error(
    *,
    region_or_path: Any,
    predicted_risk: float,
    verify_passed: bool,
    op_id: str = "",
    semantic_index: Optional[Any] = None,
) -> bool:
    """Producer entry: post-VERIFY Prophecy outcome. Bridge
    computes ``error_magnitude = abs(predicted_risk -
    actual_outcome_indicator)`` where actual is
    ``0.0`` (verify passed) or ``1.0`` (verify failed).

    A high prophecy_error means the heuristic was *wrong*
    about this region — high curiosity. A low error means
    the heuristic was right — low curiosity. NEVER raises."""
    try:
        actual = _normalize_outcome_indicator(
            bool(verify_passed),
        )
        error = abs(float(predicted_risk) - actual)
        if error < 0.0:
            error = 0.0
        elif error > 1.0:
            error = 1.0
    except Exception:  # noqa: BLE001 — defensive
        return False
    return _record_with_publish(
        record_method_name="record_prophecy_error",
        region_or_path=region_or_path,
        value=error,
        op_id=op_id,
        semantic_index=semantic_index,
    )


def feed_recurrence_drift(
    *,
    region_or_path: Any,
    recurrence_count: int,
    op_id: str = "",
    semantic_index: Optional[Any] = None,
) -> bool:
    """Producer entry: Coherence Auditor's
    ``BehavioralDriftKind.RECURRENCE_DRIFT`` finding.
    ``recurrence_count`` is normalized by the collector via
    :func:`_scoring_primitives.weight_score` (log-scale
    saturating). NEVER raises."""
    return _record_with_publish(
        record_method_name="record_recurrence_drift",
        region_or_path=region_or_path,
        value=int(recurrence_count),
        op_id=op_id,
        semantic_index=semantic_index,
    )


__all__ = [
    "feed_logprob_entropy",
    "feed_prophecy_error",
    "feed_recurrence_drift",
]
