"""§31 U2 empirical wiring — Slice 3 chatter-suppressed advisory
observer.

Composes :func:`causality_consumer.compute_op_causal_features`
+ :data:`EVENT_TYPE_CAUSAL_ADVISORY_EMITTED` SSE to surface
advice TRANSITIONS to operators via the canonical broker.

The observer mirrors the §37 Slice 5 / Slice 8 chatter-
suppressed band-observer pattern (closed-table candidate for
§33's 6th meta-pattern):

  * Per-key (record_id) state tracking — last observed advice
  * Same-advice → same-advice : silent (chatter suppressed
    structurally via early-return AST-pinned)
  * NEUTRAL → NEUTRAL transitions: silent (first observation
    at NEUTRAL also silent — mirrors cost_warning_observer's
    first-observation-at-OK rule)
  * Any cross-advice transition: emit ONE event with
    ``from_advice`` + ``to_advice`` + features payload

Architectural locks (AST-pinned):

  * **Authority asymmetry** — observer NEVER imports orchestrator
    / iron_gate / policy / candidate_generator / urgency_router
    / change_engine / semantic_guardian; advisory only.
  * **Composes Slice 1** — observer MUST compose
    :func:`compute_op_causal_features`; substrate may not
    duplicate the feature-extraction logic.
  * **Chatter suppression** — observer MUST early-return when
    ``prev_advice == new_advice`` (same-band suppression).
  * **NEVER raises** — every public method swallows exceptions
    so a bad observer can never break the orchestrator.

Master switch — ``JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED``
(default-FALSE per §33.1; flips after Slice 5 graduation
contract reports ready). Distinct from the Slice 1 master
``JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED`` so the substrate
can be observed without being injected (or vice versa) during
operator-paced empirical cadence.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


CAUSAL_ADVISORY_OBSERVATION_SCHEMA_VERSION: str = (
    "causal_advisory_observation.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_observer_enabled() -> bool:
    """Master switch — ``JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED``.
    Default-FALSE per §33.1 graduation contract pattern."""
    return os.environ.get(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Versioned observation artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CausalAdvisoryObservation:
    """One advice-transition observation. Frozen — emitted as
    SSE payload + telemetry."""

    schema_version: str
    session_id: str
    record_id: str
    from_advice: str
    to_advice: str
    ancestor_count: int
    sibling_count: int
    recurrence_score: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "record_id": self.record_id,
            "from_advice": self.from_advice,
            "to_advice": self.to_advice,
            "ancestor_count": int(self.ancestor_count),
            "sibling_count": int(self.sibling_count),
            "recurrence_score": float(self.recurrence_score),
        }


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class CausalAdvisoryObserver:
    """Per-(session_id, record_id) advice-transition tracker.

    The observer composes :func:`compute_op_causal_features` to
    derive the current advice for an op, compares against the
    last observed advice for the same key, and emits the SSE
    event only on TRANSITIONS. Same-advice observations are
    silent (chatter suppression — the cost-band / circuit-
    breaker pattern).

    Thread-safety: an internal :class:`threading.RLock` guards
    the per-key state map. Concurrent ``record()`` calls are
    serialized.

    NEVER raises — every public method is exception-swallowing.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # (session_id, record_id) → last observed advice value
        self._last_advice: Dict[tuple, str] = {}

    def record(
        self,
        *,
        session_id: str,
        record_id: str,
    ) -> Optional[CausalAdvisoryObservation]:
        """Observe one op's current causal advice. Emits SSE on
        transition; returns the observation artifact (None on
        suppressed / disabled / no-signal paths).

        Composes :func:`compute_op_causal_features` — never
        re-derives features locally."""
        if not is_observer_enabled():
            return None
        sid = str(session_id or "").strip()
        rid = str(record_id or "").strip()
        if not sid or not rid:
            return None
        try:
            from backend.core.ouroboros.governance.causality_consumer import (  # noqa: E501
                CausalDecisionAdvice,
                compute_op_causal_features,
            )
        except ImportError:
            return None
        try:
            features = compute_op_causal_features(
                session_id=sid, record_id=rid,
            )
        except Exception:  # noqa: BLE001 — defensive
            return None
        if features is None:
            return None
        new_advice = features.advice
        # Substrate disabled (slice 1 master flag off) → silent.
        if new_advice is CausalDecisionAdvice.DISABLED:
            return None
        new_advice_value = new_advice.value
        key = (sid, rid)
        with self._lock:
            prev_advice_value = self._last_advice.get(key)
            # Chatter suppression — same-advice early-return.
            # First observation at NEUTRAL is also silent
            # (mirrors first-observation-at-OK from §37 Slice 5).
            if prev_advice_value is None:
                if new_advice is CausalDecisionAdvice.NEUTRAL:
                    self._last_advice[key] = new_advice_value
                    return None
            elif prev_advice_value == new_advice_value:
                return None
            self._last_advice[key] = new_advice_value
        # Build observation artifact.
        observation = CausalAdvisoryObservation(
            schema_version=(
                CAUSAL_ADVISORY_OBSERVATION_SCHEMA_VERSION
            ),
            session_id=sid,
            record_id=rid,
            from_advice=prev_advice_value or "",
            to_advice=new_advice_value,
            ancestor_count=int(features.ancestor_count),
            sibling_count=int(features.sibling_count),
            recurrence_score=float(features.recurrence_score),
        )
        # Emit SSE — best-effort, fail-silent. The publish
        # broker is the canonical surface; if unavailable the
        # observation still flowed through telemetry.
        _publish_causal_advisory_event(observation)
        return observation

    def reset_for_tests(self) -> None:
        """Test-only — clear the per-key state map."""
        with self._lock:
            self._last_advice.clear()


# ---------------------------------------------------------------------------
# SSE publish helper — composes canonical broker
# ---------------------------------------------------------------------------


def _publish_causal_advisory_event(
    observation: CausalAdvisoryObservation,
) -> None:
    """Best-effort SSE publish via the canonical broker.
    NEVER raises — failures are swallowed."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_CAUSAL_ADVISORY_EMITTED,
            get_default_broker,
        )
    except ImportError:
        return
    try:
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            event_type=EVENT_TYPE_CAUSAL_ADVISORY_EMITTED,
            op_id=observation.record_id,
            payload=observation.to_dict(),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CausalAdvisoryObserver] SSE publish raised: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[CausalAdvisoryObserver] = None
_DEFAULT_OBSERVER_LOCK = threading.RLock()


def get_default_observer() -> CausalAdvisoryObserver:
    """First-instance-wins singleton. Per the §37 Tier 1
    Singleton + Read-API Extension Pattern — consumers read via
    this accessor without coupling to construction site."""
    global _DEFAULT_OBSERVER
    with _DEFAULT_OBSERVER_LOCK:
        if _DEFAULT_OBSERVER is None:
            _DEFAULT_OBSERVER = CausalAdvisoryObserver()
        return _DEFAULT_OBSERVER


def reset_default_observer_for_tests() -> None:
    """Test-only — destroy the singleton + recreate fresh."""
    global _DEFAULT_OBSERVER
    with _DEFAULT_OBSERVER_LOCK:
        _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``causal_advisory_observer_authority_asymmetry`` —
         observer purity. Forbids orchestrator+iron_gate+policy+
         providers+candidate_generator+urgency_router+
         change_engine+semantic_guardian imports.
      2. ``causal_advisory_observer_composes_slice_1`` — observer
         MUST compose ``compute_op_causal_features``; substrate
         may not duplicate feature-extraction logic.
      3. ``causal_advisory_observer_chatter_suppressed`` —
         ``CausalAdvisoryObserver.record`` MUST contain a
         same-advice early-return guarding against advice-band
         chatter (mirrors §37 Slice 5 / 8 pattern).
      4. ``causal_advisory_observer_master_flag_default_false``
         — :func:`is_observer_enabled` reads
         ``JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED``; default
         FALSE per §33.1 graduation contract.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "causal_advisory_observer.py"
    )

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"causal_advisory_observer.py "
                            f"MUST NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_slice_1(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        found_compose = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "causality_consumer" in node.module
                ):
                    for alias in node.names:
                        if (
                            alias.name
                            == "compute_op_causal_features"
                        ):
                            found_compose = True
        if not found_compose:
            violations.append(
                "observer MUST compose "
                "causality_consumer.compute_op_causal_features "
                "(Slice 1 substrate); no parallel feature "
                "extraction allowed"
            )
        return tuple(violations)

    def _validate_chatter_suppressed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """The record() method MUST early-return when
        prev_advice_value == new_advice_value (chatter
        suppression). AST scan looks for the comparison +
        immediate `return None`."""
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "record"
            ):
                found_chatter_guard = False
                for sub in ast.walk(node):
                    # Look for `if prev_advice_value == new_advice_value: return None`
                    if isinstance(sub, ast.If):
                        test = sub.test
                        if isinstance(test, ast.Compare):
                            ops = test.ops
                            if any(isinstance(o, ast.Eq) for o in ops):
                                # Check at least one branch returns None
                                for stmt in sub.body:
                                    if (
                                        isinstance(stmt, ast.Return)
                                        and (
                                            stmt.value is None
                                            or (
                                                isinstance(
                                                    stmt.value,
                                                    ast.Constant,
                                                )
                                                and stmt.value.value is None
                                            )
                                        )
                                    ):
                                        # Check left side touches advice
                                        # (heuristic — the AST pin is for
                                        # presence not exact shape)
                                        src_chunk = ast.dump(test)
                                        if "advice" in src_chunk:
                                            found_chatter_guard = True
                if not found_chatter_guard:
                    violations.append(
                        "observer.record() MUST contain a "
                        "same-advice early-return (chatter "
                        "suppression) — `if prev == new: "
                        "return None`"
                    )
                return tuple(violations)
        return tuple(violations)

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "is_observer_enabled"
            ):
                found_canonical_read = False
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        if (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "get"
                            and sub.args
                            and isinstance(
                                sub.args[0], ast.Constant,
                            )
                            and sub.args[0].value
                            == "JARVIS_CAUSAL_ADVISORY_"
                               "OBSERVER_ENABLED"
                        ):
                            found_canonical_read = True
                if not found_canonical_read:
                    violations.append(
                        "is_observer_enabled MUST read "
                        "os.environ.get('JARVIS_CAUSAL_"
                        "ADVISORY_OBSERVER_ENABLED', '') — "
                        "no parallel flag-name path"
                    )
                return tuple(violations)
        violations.append(
            "is_observer_enabled function missing"
        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "causal_advisory_observer_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 3 — observer substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_advisory_observer_composes_slice_1"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 3 — single pipeline; composes "
                "compute_op_causal_features only."
            ),
            validate=_validate_composes_slice_1,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_advisory_observer_chatter_suppressed"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 3 — chatter suppression "
                "structural: same-advice early-return."
            ),
            validate=_validate_chatter_suppressed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_advisory_observer_master_flag_"
                "default_false"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 3 — master flag default-FALSE "
                "per §33.1 graduation contract."
            ),
            validate=_validate_master_flag_default_false,
        ),
    ]


__all__ = [
    "CAUSAL_ADVISORY_OBSERVATION_SCHEMA_VERSION",
    "CausalAdvisoryObservation",
    "CausalAdvisoryObserver",
    "get_default_observer",
    "is_observer_enabled",
    "register_shipped_invariants",
    "reset_default_observer_for_tests",
]
