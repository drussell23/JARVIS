"""Priority 1 Slice 4 — Confidence-aware execution observability.

Pure-helper publishers that bridge confidence-monitor verdicts +
route-advisor proposals into the existing IDE observability stream
(``ide_observability_stream.py``). Slice 1 captures, Slice 2
evaluates, Slice 3 maps to actions; this slice broadcasts the
verdict transitions as first-class SSE events for the IDE +
operator surfaces.

Architecture
------------

  * Three severity-tiered confidence-drop event publishers:
      P1 — ``publish_confidence_drop_event``: BELOW_FLOOR + abort
           condition; the breaker just fired.
      P2 — ``publish_confidence_approaching_event``: APPROACHING
           band; early warning before abort.
      P3 — ``publish_sustained_low_confidence_event``: trend
           detector; cumulative low-confidence pattern across
           multiple ops, posture-nudge candidate.
  * One advisory route-proposal publisher:
      ``publish_route_proposal_event``: emitted by
      ``confidence_route_advisor`` when the rolling-confidence
      pattern suggests a cost-side route change. ADVISORY ONLY —
      this event NEVER carries BG/SPEC → STANDARD/COMPLEX/IMMEDIATE
      escalation; cost-contract enforcement at the advisor.

Master flag
-----------

``JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED`` (default ``false`` for
Slice 4; flips to ``true`` in Slice 5 graduation). Asymmetric env
semantics. When off, every publisher returns ``None`` immediately —
the broker isn't even consulted, so the stream stays byte-for-byte
identical to pre-Slice-4 behavior.

Cost-contract preservation
--------------------------

This module imports zero provider modules. Route-proposal payloads
NEVER signal an escalation route — the route advisor + §26.6
defense layers enforce that. AST-pinned by tests.

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runners / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider / urgency_router.
  * Pure stdlib (``logging``, ``os``) + the IDE stream broker only.
  * NEVER raises out of any public method.
  * Read-only on inputs — never modifies the verdict / advisor
    inputs.
  * No control-flow influence — broadcasts only. Slice 5 wires
    the producers.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_MODEL_CONFIDENCE_APPROACHING,
    EVENT_TYPE_MODEL_CONFIDENCE_DROP,
    EVENT_TYPE_MODEL_SUSTAINED_LOW_CONFIDENCE,
    EVENT_TYPE_ROUTE_PROPOSAL,
    get_default_broker,
    stream_enabled,
)

logger = logging.getLogger(__name__)


CONFIDENCE_OBSERVABILITY_SCHEMA_VERSION: str = "confidence_observability.1"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def confidence_observability_enabled() -> bool:
    """``JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED`` (default ``true`` —
    graduated in Priority 1 Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy disables.
    Re-read at call time so monkeypatch + live toggle work.

    Hot-revert: ``export JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED=false``
    short-circuits every publisher to a pure no-op."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5 — was false in Slice 4)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Defensive payload builder — extracts fields from a verdict-shaped object
# ---------------------------------------------------------------------------


def _safe_str(value: Any, default: str = "") -> str:
    try:
        if value is None:
            return default
        return str(value)
    except Exception:  # noqa: BLE001
        return default


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float coerce; non-finite / non-numeric → None."""
    try:
        if value is None:
            return None
        v = float(value)
        # NaN check
        if v != v:
            return None
        # Inf is a valid signal in some cases (margin = -inf), keep it.
        return v
    except (TypeError, ValueError):
        return None
    except Exception:  # noqa: BLE001
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    except Exception:  # noqa: BLE001
        return default


def _build_confidence_payload(
    *,
    verdict: Any = None,
    rolling_margin: Any = None,
    floor: Any = None,
    effective_floor: Any = None,
    posture: Any = None,
    window_size: Any = None,
    observations_count: Any = None,
    op_id: Any = None,
    provider: Any = None,
    model_id: Any = None,
) -> Dict[str, Any]:
    """Build a JSON-friendly payload from confidence-verdict fields.
    Defensive on every field. NEVER raises.

    The verdict argument is interpreted permissively: an enum-like
    object with ``.value`` attribute, or a string, or ``None``."""
    verdict_str = ""
    try:
        if hasattr(verdict, "value"):
            verdict_str = _safe_str(verdict.value)
        elif verdict is not None:
            verdict_str = _safe_str(verdict)
    except Exception:  # noqa: BLE001
        verdict_str = ""
    return {
        "schema_version": CONFIDENCE_OBSERVABILITY_SCHEMA_VERSION,
        "verdict": verdict_str,
        "rolling_margin": _safe_float(rolling_margin),
        "floor": _safe_float(floor),
        "effective_floor": _safe_float(effective_floor),
        "posture": _safe_str(posture, default=""),
        "window_size": _safe_int(window_size, default=0),
        "observations_count": _safe_int(observations_count, default=0),
        "op_id": _safe_str(op_id, default=""),
        "provider": _safe_str(provider, default=""),
        "model_id": _safe_str(model_id, default=""),
    }


# ---------------------------------------------------------------------------
# P1 — confidence drop (BELOW_FLOOR + abort condition)
# ---------------------------------------------------------------------------


def _record_verdict_for_auto_action_router(
    *, op_id: Any, verdict_str: str, rolling_margin: Any = None,
) -> None:
    """Move 3 Slice 3 — bridge to ``auto_action_router``'s
    process-local verdict ring buffer.

    Called from each per-op verdict publish site (P1 drop, P2
    approaching). Best-effort — any failure (module not installed,
    buffer full, malformed input) is swallowed at the auto-action
    router side. This wrapper just guards the import for tests
    that exercise confidence_observability without the router."""
    if not isinstance(op_id, str) or not op_id:
        return
    try:
        from backend.core.ouroboros.governance.auto_action_router import (
            record_confidence_verdict,
        )
        record_confidence_verdict(
            op_id=op_id,
            verdict=verdict_str,
            rolling_margin=_safe_float(rolling_margin) or 0.0,
        )
    except Exception:  # noqa: BLE001 — bridge MUST NOT propagate.
        logger.debug(
            "[ConfidenceObservability] auto_action_router bridge "
            "swallowed exception", exc_info=True,
        )


def publish_confidence_drop_event(
    *,
    verdict: Any = None,
    rolling_margin: Any = None,
    floor: Any = None,
    effective_floor: Any = None,
    posture: Any = None,
    window_size: Any = None,
    observations_count: Any = None,
    op_id: Any = None,
    provider: Any = None,
    model_id: Any = None,
    severity: str = "P1",
) -> Optional[str]:
    """P1 — confidence-drop event. Fires when the monitor observes
    BELOW_FLOOR mid-stream (with or without ENFORCE-driven abort).

    Returns the broker's frame_id when published, ``None`` when
    suppressed by master flag / stream master / broker exception.
    NEVER raises."""
    if not confidence_observability_enabled():
        return None
    if not stream_enabled():
        return None
    try:
        payload = _build_confidence_payload(
            verdict=verdict,
            rolling_margin=rolling_margin,
            floor=floor,
            effective_floor=effective_floor,
            posture=posture,
            window_size=window_size,
            observations_count=observations_count,
            op_id=op_id,
            provider=provider,
            model_id=model_id,
        )
        payload["severity"] = "P1"
        del severity  # unused; severity is structurally fixed at P1
        # Move 3 Slice 3 — feed the auto-action router's verdict
        # ring buffer. P1 = BELOW_FLOOR → ESCALATE in the
        # router's vocabulary.
        _record_verdict_for_auto_action_router(
            op_id=op_id,
            verdict_str="BELOW_FLOOR",
            rolling_margin=rolling_margin,
        )
        return get_default_broker().publish(
            EVENT_TYPE_MODEL_CONFIDENCE_DROP,
            _safe_str(provider, default="confidence_monitor"),
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ConfidenceObservability] publish_confidence_drop_event "
            "swallowed exception", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# P2 — approaching floor (early warning)
# ---------------------------------------------------------------------------


def publish_confidence_approaching_event(
    *,
    verdict: Any = None,
    rolling_margin: Any = None,
    floor: Any = None,
    effective_floor: Any = None,
    posture: Any = None,
    window_size: Any = None,
    observations_count: Any = None,
    op_id: Any = None,
    provider: Any = None,
    model_id: Any = None,
) -> Optional[str]:
    """P2 — confidence-approaching-floor event. Fires when the
    monitor's rolling margin is in (effective_floor, effective_floor
    × approaching_factor). Warns observers that an abort may be
    imminent without actually triggering the breaker.

    Returns the broker's frame_id when published, ``None`` when
    suppressed. NEVER raises."""
    if not confidence_observability_enabled():
        return None
    if not stream_enabled():
        return None
    try:
        payload = _build_confidence_payload(
            verdict=verdict,
            rolling_margin=rolling_margin,
            floor=floor,
            effective_floor=effective_floor,
            posture=posture,
            window_size=window_size,
            observations_count=observations_count,
            op_id=op_id,
            provider=provider,
            model_id=model_id,
        )
        payload["severity"] = "P2"
        # Move 3 Slice 3 — feed the auto-action router's verdict
        # ring buffer. P2 = APPROACHING_FLOOR → RETRY in the
        # router's vocabulary (early warning, not yet escalating).
        _record_verdict_for_auto_action_router(
            op_id=op_id,
            verdict_str="APPROACHING_FLOOR",
            rolling_margin=rolling_margin,
        )
        return get_default_broker().publish(
            EVENT_TYPE_MODEL_CONFIDENCE_APPROACHING,
            _safe_str(provider, default="confidence_monitor"),
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ConfidenceObservability] publish_confidence_approaching_"
            "event swallowed exception", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# P3 — sustained low confidence (cross-op trend, posture nudge candidate)
# ---------------------------------------------------------------------------


def publish_sustained_low_confidence_event(
    *,
    op_count_in_window: Any = None,
    low_confidence_count: Any = None,
    rate: Any = None,
    posture: Any = None,
    provider: Any = None,
    model_id: Any = None,
) -> Optional[str]:
    """P3 — sustained-low-confidence trend event. Fires when a
    rolling-window-of-ops detector observes that the rate of
    confidence-collapse events exceeds a posture-relevant threshold.

    Slice 4 ships the publisher; the trend detector that calls it
    is a Slice 5+ wiring concern. Operator surface: this event is
    a candidate for a posture nudge toward HARDEN.

    Returns the broker's frame_id when published, ``None`` when
    suppressed. NEVER raises."""
    if not confidence_observability_enabled():
        return None
    if not stream_enabled():
        return None
    try:
        payload = {
            "schema_version": CONFIDENCE_OBSERVABILITY_SCHEMA_VERSION,
            "severity": "P3",
            "op_count_in_window": _safe_int(op_count_in_window, default=0),
            "low_confidence_count": _safe_int(
                low_confidence_count, default=0,
            ),
            "rate": _safe_float(rate),
            "posture": _safe_str(posture, default=""),
            "provider": _safe_str(provider, default=""),
            "model_id": _safe_str(model_id, default=""),
        }
        return get_default_broker().publish(
            EVENT_TYPE_MODEL_SUSTAINED_LOW_CONFIDENCE,
            _safe_str(provider, default="confidence_monitor"),
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ConfidenceObservability] publish_sustained_low_confidence_"
            "event swallowed exception", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Advisory route-proposal publisher
# ---------------------------------------------------------------------------


def publish_route_proposal_event(
    *,
    proposed_route: Any = None,
    current_route: Any = None,
    reason_code: Any = None,
    confidence_basis: Any = None,
    rolling_margin: Any = None,
    op_id: Any = None,
    provider: Any = None,
    model_id: Any = None,
    advisory: bool = True,
) -> Optional[str]:
    """ADVISORY route-proposal event. Cost-contract preservation:
    payloads from the route advisor NEVER carry BG/SPEC →
    STANDARD/COMPLEX/IMMEDIATE escalation. The advisor's AST-pinned
    guard + §26.6 runtime CostContractViolation enforce structurally.

    The ``advisory`` flag is hardcoded True at publish time —
    callers cannot mark a proposal as "auto-applied". Any
    consumer that wants to act on a proposal must do so through
    operator-approval-bound surfaces (Slice 5+ wiring).

    Returns the broker's frame_id when published, ``None`` when
    suppressed. NEVER raises."""
    if not confidence_observability_enabled():
        return None
    if not stream_enabled():
        return None
    try:
        payload = {
            "schema_version": CONFIDENCE_OBSERVABILITY_SCHEMA_VERSION,
            "advisory": True,  # hardcoded — see docstring above
            "proposed_route": _safe_str(proposed_route, default=""),
            "current_route": _safe_str(current_route, default=""),
            "reason_code": _safe_str(reason_code, default=""),
            "confidence_basis": _safe_str(
                confidence_basis, default="",
            ),
            "rolling_margin": _safe_float(rolling_margin),
            "op_id": _safe_str(op_id, default=""),
            "provider": _safe_str(provider, default=""),
            "model_id": _safe_str(model_id, default=""),
        }
        del advisory  # unused; structurally pinned True above
        return get_default_broker().publish(
            EVENT_TYPE_ROUTE_PROPOSAL,
            _safe_str(provider, default="confidence_route_advisor"),
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ConfidenceObservability] publish_route_proposal_event "
            "swallowed exception", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_OBSERVABILITY_SCHEMA_VERSION",
    "confidence_observability_enabled",
    "publish_confidence_approaching_event",
    "publish_confidence_drop_event",
    "publish_route_proposal_event",
    "publish_sustained_low_confidence_event",
]
