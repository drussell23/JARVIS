"""DirectionInferrer — deterministic signal → posture inference.

Consumes a ``SignalBundle`` (10 + 2 structured signals from ambient
telemetry) and emits a ``PostureReading`` naming the current strategic
posture of the organism. Pure function: same bundle in, same reading
out (modulo ``inferred_at`` timestamp), verified via ``signal_bundle_hash``.

Math flow (all in ``infer()``, no I/O):
  1. Normalize raw signals into the ``[-1.0, +1.0]`` weighted-sum domain
     via per-signal documented transforms (see ``_normalize``).
  2. Compute one score per posture as ``sum(normalized_i * weight_i)``
     across the 12-row ``DEFAULT_WEIGHTS`` table.
  3. Confidence = ``(top_score - second_score) / max(|top_score|, eps)``,
     clamped to ``[0.0, 1.0]``.
  4. If ``confidence < JARVIS_POSTURE_CONFIDENCE_FLOOR`` (default 0.35),
     fall back to ``MAINTAIN`` — but preserve the actual ``evidence``
     list so ``/posture explain`` still shows what was near-tied.
  5. Deterministic tie-break: alphabetic on posture name — so identical
     scores resolve as CONSOLIDATE > EXPLORE > HARDEN > MAINTAIN.

Authority invariant (grep-enforced in Slice 4 graduation): this module
imports **nothing** from the orchestrator / policy / Iron Gate / risk-
tier / candidate generator / gate / change-engine axis. The enforcement
pin is a regression test, not a runtime assert, so importing for type
annotations is also forbidden.

Manifesto alignment:
  * §5 Tier 0 — pure deterministic math, < 10ms, zero LLM
  * §1 Boundary Principle — produces advisory ``PostureReading`` only
  * §8 Observability — every reading carries hash + per-signal evidence
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.arc_context import ArcContextSignal
from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
    SignalBundle,
    SignalContribution,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env configuration
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Master switch.

    Default: **``true``** (graduated 2026-04-21 via Slice 4 after
    Slices 1-3 shipped the primitive + observer/store/prompt injection +
    /posture REPL + IDE GET + SSE bridge with 165 governance tests +
    3 live-fire proofs on real repo state). Explicit ``"false"`` reverts
    to the Slice 1 deny-by-default posture so operators retain a
    runtime kill switch — when the flag is explicitly ``"false"`` every
    surface disables in lockstep:

      * PostureObserver.start() becomes a no-op
      * StrategicDirection.format_for_prompt() omits the posture section
      * GET /observability/posture returns 403 (port scanners see no
        signal about what's behind the route)
      * /posture REPL rejects operational verbs (help still works)
      * SSE publish_posture_event() returns None (drops silently)

    The authority invariants (grep-pinned zero imports of
    orchestrator/policy/iron_gate/risk_tier/change_engine/candidate_generator),
    loopback-only GET binding, rate-limit caps, CORS allowlist, and
    confidence-floor fallback all remain in force regardless of this
    flag — graduation flips opt-in friction, NOT authority surface.
    """
    return _env_bool("JARVIS_DIRECTION_INFERRER_ENABLED", True)


def arc_context_enabled() -> bool:
    """P0.5 Slice 2 — when on, ``DirectionInferrer.infer(arc_context=...)``
    applies bounded score nudges (≤ ``MAX_NUDGE_PER_POSTURE`` per posture)
    derived from recent git momentum + last-session summary.

    Default: ``false``. Slice 3 graduation flips this default-off → on
    after the same evidence pattern P0 used. When off, ``arc_context``
    kwargs are still observed (carried through to ``PostureReading`` +
    surfaced in the posture log line) but contribute zero to scoring —
    this is the "observation-only" mode that lets live-cadence sessions
    measure the would-be effect before flipping the default.
    """
    return _env_bool("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", False)


def confidence_floor() -> float:
    """Minimum spread between top and second-scoring posture for the
    inference to commit. Below this → MAINTAIN fallback."""
    raw = _env_float("JARVIS_POSTURE_CONFIDENCE_FLOOR", 0.35)
    if raw < 0.0:
        return 0.0
    if raw > 1.0:
        return 1.0
    return raw


# ---------------------------------------------------------------------------
# Default weights — hypothesis, tuned post-graduation from live distribution
# ---------------------------------------------------------------------------

# Signal names used as dict keys — must match SignalBundle field names
# exactly for ``_normalize`` / ``_score`` indexing.
_SIGNAL_NAMES: Tuple[str, ...] = (
    "feat_ratio",
    "fix_ratio",
    "refactor_ratio",
    "test_docs_ratio",
    "postmortem_failure_rate",
    "iron_gate_reject_rate",
    "l2_repair_rate",
    "open_ops_normalized",
    "session_lessons_infra_ratio",
    "time_since_last_graduation_inv",
    "cost_burn_normalized",
    "worktree_orphan_count",
)


# Row-oriented: DEFAULT_WEIGHTS[signal_name][posture] = weight
DEFAULT_WEIGHTS: Dict[str, Dict[Posture, float]] = {
    "feat_ratio": {
        Posture.EXPLORE: +1.0, Posture.CONSOLIDATE: -0.3,
        Posture.HARDEN: -0.2, Posture.MAINTAIN: 0.0,
    },
    "fix_ratio": {
        Posture.EXPLORE: -0.4, Posture.CONSOLIDATE: 0.0,
        Posture.HARDEN: +1.0, Posture.MAINTAIN: 0.0,
    },
    "refactor_ratio": {
        Posture.EXPLORE: -0.2, Posture.CONSOLIDATE: +0.8,
        Posture.HARDEN: 0.0, Posture.MAINTAIN: 0.0,
    },
    "test_docs_ratio": {
        Posture.EXPLORE: -0.2, Posture.CONSOLIDATE: +0.4,
        Posture.HARDEN: +0.2, Posture.MAINTAIN: +0.3,
    },
    "postmortem_failure_rate": {
        Posture.EXPLORE: -0.8, Posture.CONSOLIDATE: 0.0,
        Posture.HARDEN: +1.2, Posture.MAINTAIN: 0.0,
    },
    "iron_gate_reject_rate": {
        Posture.EXPLORE: -0.5, Posture.CONSOLIDATE: +0.2,
        Posture.HARDEN: +0.9, Posture.MAINTAIN: 0.0,
    },
    "l2_repair_rate": {
        Posture.EXPLORE: -0.3, Posture.CONSOLIDATE: +0.3,
        Posture.HARDEN: +0.6, Posture.MAINTAIN: 0.0,
    },
    "open_ops_normalized": {
        Posture.EXPLORE: +0.4, Posture.CONSOLIDATE: -0.2,
        Posture.HARDEN: 0.0, Posture.MAINTAIN: 0.0,
    },
    "session_lessons_infra_ratio": {
        Posture.EXPLORE: -0.2, Posture.CONSOLIDATE: +0.2,
        Posture.HARDEN: +0.7, Posture.MAINTAIN: 0.0,
    },
    "time_since_last_graduation_inv": {
        Posture.EXPLORE: +0.3, Posture.CONSOLIDATE: -0.4,
        Posture.HARDEN: 0.0, Posture.MAINTAIN: 0.0,
    },
    "cost_burn_normalized": {
        Posture.EXPLORE: +0.2, Posture.CONSOLIDATE: -0.3,
        Posture.HARDEN: -0.2, Posture.MAINTAIN: 0.0,
    },
    "worktree_orphan_count": {
        # Orphan count normalized by /10 in _normalize
        Posture.EXPLORE: -0.2, Posture.CONSOLIDATE: +0.5,
        Posture.HARDEN: +0.2, Posture.MAINTAIN: 0.0,
    },
}


# Alphabetic order of posture names — used for deterministic tie-break.
# Lower index wins ties. CONSOLIDATE > EXPLORE > HARDEN > MAINTAIN.
_TIE_BREAK_ORDER: Tuple[Posture, ...] = (
    Posture.CONSOLIDATE,
    Posture.EXPLORE,
    Posture.HARDEN,
    Posture.MAINTAIN,
)


def _load_weight_override() -> Optional[Dict[str, Dict[Posture, float]]]:
    """Parse JSON override from env. Returns None when unset or malformed.

    Shape: ``{"signal_name": {"POSTURE": weight, ...}, ...}`` — only the
    signals / postures present are overridden; the rest stay at default.
    """
    raw = os.environ.get("JARVIS_POSTURE_WEIGHTS_OVERRIDE")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("DirectionInferrer: weight override is not valid JSON; ignoring")
        return None
    if not isinstance(parsed, dict):
        logger.warning("DirectionInferrer: weight override must be dict; ignoring")
        return None
    out: Dict[str, Dict[Posture, float]] = {}
    for signal, row in parsed.items():
        if signal not in _SIGNAL_NAMES:
            logger.warning(
                "DirectionInferrer: weight override unknown signal=%s; skipping",
                signal,
            )
            continue
        if not isinstance(row, dict):
            continue
        row_out: Dict[Posture, float] = {}
        for posture_name, weight in row.items():
            try:
                posture = Posture.from_str(posture_name)
            except ValueError:
                continue
            try:
                row_out[posture] = float(weight)
            except (TypeError, ValueError):
                continue
        if row_out:
            out[signal] = row_out
    return out or None


def _effective_weights() -> Dict[str, Dict[Posture, float]]:
    """Merge DEFAULT_WEIGHTS with env override. Override only replaces
    the keys it explicitly sets."""
    override = _load_weight_override()
    if not override:
        return DEFAULT_WEIGHTS
    merged: Dict[str, Dict[Posture, float]] = {}
    for signal, defaults in DEFAULT_WEIGHTS.items():
        if signal in override:
            row = dict(defaults)
            row.update(override[signal])
            merged[signal] = row
        else:
            merged[signal] = dict(defaults)
    return merged


# ---------------------------------------------------------------------------
# Inference primitive
# ---------------------------------------------------------------------------


class DirectionInferrer:
    """Pure signal-bundle → posture-reading primitive.

    No instance state beyond the weight table. Construct once; call
    ``infer()`` many times. Thread-safe by construction.
    """

    def __init__(
        self,
        weights: Optional[Dict[str, Dict[Posture, float]]] = None,
    ) -> None:
        # Snapshot weights at construction; env override re-resolved per
        # infer() call to support test-time mutations without re-instantiation.
        self._static_weights = weights

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
        if value < low:
            return low
        if value > high:
            return high
        return value

    def _normalize(self, bundle: SignalBundle) -> Dict[str, float]:
        """Per-signal raw → normalized transform.

        Ratios 0..1 map to 0..1 directly. Integer counts get divided by
        a documented scale and clipped. Output domain is ``[-1.0, +1.0]``
        (negative unused in v1 — reserved for future anti-signals).
        """
        return {
            "feat_ratio": self._clip(bundle.feat_ratio, 0.0, 1.0),
            "fix_ratio": self._clip(bundle.fix_ratio, 0.0, 1.0),
            "refactor_ratio": self._clip(bundle.refactor_ratio, 0.0, 1.0),
            "test_docs_ratio": self._clip(bundle.test_docs_ratio, 0.0, 1.0),
            "postmortem_failure_rate": self._clip(bundle.postmortem_failure_rate, 0.0, 1.0),
            "iron_gate_reject_rate": self._clip(bundle.iron_gate_reject_rate, 0.0, 1.0),
            "l2_repair_rate": self._clip(bundle.l2_repair_rate, 0.0, 1.0),
            "open_ops_normalized": self._clip(bundle.open_ops_normalized, 0.0, 1.0),
            "session_lessons_infra_ratio": self._clip(bundle.session_lessons_infra_ratio, 0.0, 1.0),
            "time_since_last_graduation_inv": self._clip(bundle.time_since_last_graduation_inv, 0.0, 1.0),
            "cost_burn_normalized": self._clip(bundle.cost_burn_normalized, 0.0, 1.0),
            # Orphan count saturates at 10 — beyond that, boring asymptote
            "worktree_orphan_count": self._clip(bundle.worktree_orphan_count / 10.0, 0.0, 1.0),
        }

    def _resolve_weights(self) -> Dict[str, Dict[Posture, float]]:
        if self._static_weights is not None:
            return self._static_weights
        return _effective_weights()

    def _score(
        self,
        normalized: Dict[str, float],
        weights: Dict[str, Dict[Posture, float]],
    ) -> Dict[Posture, float]:
        """Weighted sum per posture."""
        scores: Dict[Posture, float] = {p: 0.0 for p in Posture}
        for signal, value in normalized.items():
            row = weights.get(signal)
            if row is None:
                continue
            for posture, weight in row.items():
                scores[posture] += value * weight
        return scores

    def _build_evidence(
        self,
        winning: Posture,
        normalized: Dict[str, float],
        bundle: SignalBundle,
        weights: Dict[str, Dict[Posture, float]],
    ) -> Tuple[SignalContribution, ...]:
        """Top contributors to the winning posture, sorted by
        contribution magnitude (descending)."""
        contribs: List[SignalContribution] = []
        for signal in _SIGNAL_NAMES:
            norm = normalized.get(signal, 0.0)
            row = weights.get(signal, {})
            weight = row.get(winning, 0.0)
            contrib = norm * weight
            raw = getattr(bundle, signal, 0.0)
            contribs.append(
                SignalContribution(
                    signal_name=signal,
                    raw_value=float(raw),
                    normalized=norm,
                    weight=weight,
                    contributed_to=winning,
                    contribution_score=contrib,
                )
            )
        # Sort by absolute contribution descending. Stable for ties
        # because Python sort is stable and _SIGNAL_NAMES order is fixed.
        contribs.sort(key=lambda c: abs(c.contribution_score), reverse=True)
        return tuple(contribs)

    @staticmethod
    def _pick_winner(scores: Dict[Posture, float]) -> Tuple[Posture, float, float]:
        """Returns (winner, top_score, second_score). Alphabetic tie-break
        via ``_TIE_BREAK_ORDER`` — CONSOLIDATE > EXPLORE > HARDEN > MAINTAIN.
        """
        # Sort postures: descending score, ascending tie-break index.
        tie_index = {p: i for i, p in enumerate(_TIE_BREAK_ORDER)}
        ordered = sorted(
            scores.items(),
            key=lambda kv: (-kv[1], tie_index[kv[0]]),
        )
        winner, top_score = ordered[0]
        second_score = ordered[1][1] if len(ordered) > 1 else 0.0
        return winner, top_score, second_score

    @staticmethod
    def _confidence(top: float, second: float) -> float:
        """``(top - second) / max(|top|, eps)``, clamped to ``[0, 1]``.

        Using absolute value of top in the denominator keeps the metric
        well-defined when all scores are negative (theoretically possible
        if future anti-signals use negative normalization).
        """
        eps = 1e-6
        denom = max(abs(top), eps)
        raw = (top - second) / denom
        if raw < 0.0:
            return 0.0
        if raw > 1.0:
            return 1.0
        return raw

    # ---- public API -------------------------------------------------------

    def infer(
        self,
        bundle: SignalBundle,
        arc_context: Optional[ArcContextSignal] = None,
    ) -> PostureReading:
        """Deterministic pure inference. Always returns a PostureReading
        (never None, never raises on well-formed input).

        Raises ``ValueError`` on schema_version mismatch — v1 reader
        reading v2+ bundle must reject, not coerce.

        ``arc_context`` (P0.5 Slice 2): optional ``ArcContextSignal`` from
        ``arc_context.build_arc_context``. When provided, it is always
        carried through to the returned ``PostureReading`` for
        observability — but the score adjustment is **only applied when**
        ``JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED`` is on (default
        off). Each per-posture nudge is bounded to
        ``arc_context.MAX_NUDGE_PER_POSTURE`` (0.10) so existing weights
        still dominate.
        """
        from backend.core.ouroboros.governance.posture import SCHEMA_VERSION as _SV

        if bundle.schema_version != _SV:
            raise ValueError(
                f"SignalBundle schema_version mismatch: got {bundle.schema_version!r}, "
                f"inferrer supports {_SV!r}"
            )

        normalized = self._normalize(bundle)
        weights = self._resolve_weights()
        scores = self._score(normalized, weights)

        # P0.5 Slice 2 — apply bounded arc-context nudge when flag is on.
        # When the flag is off OR arc_context is None, scores are
        # byte-for-byte unchanged (back-compat with all existing pins).
        arc_nudge_applied: Dict[Posture, float] = {p: 0.0 for p in Posture}
        if arc_context is not None and arc_context_enabled():
            arc_nudge_applied = arc_context.suggest_nudge()
            for posture, nudge in arc_nudge_applied.items():
                if nudge:
                    scores[posture] = scores[posture] + nudge

        winner, top, second = self._pick_winner(scores)
        confidence = self._confidence(top, second)

        # Confidence floor → MAINTAIN fallback, but preserve evidence
        # for the *inferred* winner so `/posture explain` shows what was
        # near-tied instead of a trivially empty MAINTAIN evidence list.
        inferred_winner = winner
        if confidence < confidence_floor():
            inferred_winner = Posture.MAINTAIN

        evidence = self._build_evidence(winner, normalized, bundle, weights)

        all_scores: Tuple[Tuple[Posture, float], ...] = tuple(
            sorted(scores.items(), key=lambda kv: -kv[1])
        )

        return PostureReading(
            posture=inferred_winner,
            confidence=confidence,
            evidence=evidence,
            inferred_at=time.time(),
            signal_bundle_hash=bundle.hash(),
            all_scores=all_scores,
            arc_context=arc_context,
        )


__all__ = [
    "DirectionInferrer",
    "DEFAULT_WEIGHTS",
    "is_enabled",
    "confidence_floor",
]
