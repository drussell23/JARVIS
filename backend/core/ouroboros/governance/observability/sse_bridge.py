"""Phase 8 surface wiring Slice 2 — SSE event bridges.

Wires the 5 Phase 8 substrate modules onto the existing Gap #6
:class:`StreamEventBroker` via 5 best-effort publish helpers.
Producers (orchestrator code that records decisions, classifiers
that record confidences, the periodic monitors for flags + latency
SLOs) call these helpers AFTER the substrate's ``record`` /
``check`` operation succeeds. The bridge:

  * Lazy-imports the broker so this module can be imported without
    forcing aiohttp side effects.
  * Masks/sanitizes payloads — secrets in flag values never leave
    via SSE (mirrors the Slice 1 GET endpoint contract).
  * Bounds payload sizes so a runaway producer cannot flood the
    broker queue with a single mega-event.
  * Never raises into the producer — every error path is logged
    once and swallowed.

Authority posture (locked):

  * **Read-only over the broker** — bridges only ``publish``; they
    never subscribe or mutate broker state.
  * **Deny-by-default** at TWO levels:
    - Master flag ``JARVIS_PHASE8_SSE_BRIDGE_ENABLED`` (default
      ``false``) gates ALL bridges. When off, every helper is a
      no-op that returns ``None`` silently.
    - Per-event sub-flags
      (``JARVIS_PHASE8_SSE_BRIDGE_DECISION_RECORDED``,
      ``..._CONFIDENCE_OBSERVED``, ``..._CONFIDENCE_DROP_DETECTED``,
      ``..._SLO_BREACHED``, ``..._FLAG_CHANGED``)
      let operators silence individual streams without disabling
      the whole bridge. Default each = ``true`` (so flipping the
      master flag is enough to enable everything).
  * **No imports from gate / execution modules.** Pinned by
    ``test_phase8_sse_bridge_does_not_import_gate_modules``.
  * **Lazy substrate + broker imports** — top-level imports are
    pure stdlib + this module's own logger.

## Event vocabulary (5 types added in
``ide_observability_stream._VALID_EVENT_TYPES`` by this slice)

  * ``decision_recorded``       — DecisionTraceLedger.record() OK
  * ``confidence_observed``     — LatentConfidenceRing.record() OK
  * ``confidence_drop_detected``— ring.confidence_drop_indicators
                                  reports ``drop_detected=True``
  * ``slo_breached``            — detector.check_breach returned a
                                  non-None LatencySLOBreachEvent
  * ``flag_changed``            — monitor.check returned a delta
                                  (one event per delta; values masked)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Bounded payload caps — defends against a producer wedging an
# entire model-output blob into a payload field.
MAX_PAYLOAD_STRING_CHARS: int = 1_000
MAX_PAYLOAD_KEYS: int = 16
MAX_RATIONALE_CHARS: int = 200


# ---------------------------------------------------------------------------
# Master + per-event flags
# ---------------------------------------------------------------------------


def is_bridge_enabled() -> bool:
    """Master flag — ``JARVIS_PHASE8_SSE_BRIDGE_ENABLED`` (default
    ``false`` until graduation). When off, EVERY bridge helper is a
    no-op."""
    return os.environ.get(
        "JARVIS_PHASE8_SSE_BRIDGE_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _per_event_enabled(env_name: str) -> bool:
    """Per-event sub-flag. Default ``true`` (master is the gate;
    sub-flags exist for granular silencing)."""
    raw = os.environ.get(env_name)
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


def is_decision_recorded_enabled() -> bool:
    return _per_event_enabled(
        "JARVIS_PHASE8_SSE_BRIDGE_DECISION_RECORDED",
    )


def is_confidence_observed_enabled() -> bool:
    return _per_event_enabled(
        "JARVIS_PHASE8_SSE_BRIDGE_CONFIDENCE_OBSERVED",
    )


def is_confidence_drop_detected_enabled() -> bool:
    return _per_event_enabled(
        "JARVIS_PHASE8_SSE_BRIDGE_CONFIDENCE_DROP_DETECTED",
    )


def is_slo_breached_enabled() -> bool:
    return _per_event_enabled(
        "JARVIS_PHASE8_SSE_BRIDGE_SLO_BREACHED",
    )


def is_flag_changed_enabled() -> bool:
    return _per_event_enabled(
        "JARVIS_PHASE8_SSE_BRIDGE_FLAG_CHANGED",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _truncate_string(value: Any) -> Any:
    """Truncate string values to MAX_PAYLOAD_STRING_CHARS. Non-string
    values pass through unchanged. None/empty → "" / value."""
    if isinstance(value, str) and len(value) > MAX_PAYLOAD_STRING_CHARS:
        return value[: MAX_PAYLOAD_STRING_CHARS - 14] + "...(truncated)"
    return value


def _bound_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Cap key count + truncate string values. Defends against
    runaway producers."""
    out: Dict[str, Any] = {}
    for i, (k, v) in enumerate(payload.items()):
        if i >= MAX_PAYLOAD_KEYS:
            break
        out[str(k)[:64]] = _truncate_string(v)
    return out


def _publish(
    event_type: str, op_id: str, payload: Mapping[str, Any],
) -> Optional[str]:
    """Lazy-import + best-effort publish. Returns event_id on
    success, None on any failure (master off / sub-flag off /
    broker raise / unknown event_type)."""
    if not is_bridge_enabled():
        return None
    try:
        # Lazy import so this module's import surface stays cage-clean.
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[Phase8Bridge] broker import failed", exc_info=True,
        )
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type, op_id, _bound_payload(payload),
        )
    except Exception:  # noqa: BLE001 — defensive, never raise
        logger.debug(
            "[Phase8Bridge] publish exception event_type=%r",
            event_type, exc_info=True,
        )
        return None


def _mask_flag_value(v: Optional[str]) -> Optional[str]:
    """Mirror Slice 1 GET endpoint masking — never echo raw env
    values. None passes through (signals removal/no-prev-value)."""
    if v is None:
        return None
    return "<set>" if v else "<empty>"


# ---------------------------------------------------------------------------
# Public bridge helpers
# ---------------------------------------------------------------------------


def publish_decision_recorded(
    *,
    op_id: str,
    phase: str,
    decision: str,
    rationale: str = "",
) -> Optional[str]:
    """Producer hook for DecisionTraceLedger.record() success.

    Call AFTER ``ledger.record(...)`` returns ``(True, "ok")``. The
    payload mirrors the documented row shape but truncates rationale
    + omits factors/weights (those carry rich data — operators
    fetch via ``GET /observability/decisions/{op_id}``)."""
    if not is_decision_recorded_enabled():
        return None
    return _publish(
        "decision_recorded", op_id,
        {
            "phase": phase or "",
            "decision": decision or "",
            "rationale": (rationale or "")[:MAX_RATIONALE_CHARS],
        },
    )


def publish_confidence_observed(
    *,
    classifier_name: str,
    confidence: float,
    threshold: float,
    outcome: str,
    op_id: str = "",
) -> Optional[str]:
    """Producer hook for LatentConfidenceRing.record() success.

    ``op_id`` is OPTIONAL — confidence events aren't always op-
    scoped. When unset, the broker accepts an empty op_id (filter
    consumers see the event under the global stream)."""
    if not is_confidence_observed_enabled():
        return None
    try:
        conf_f = float(confidence)
        thr_f = float(threshold)
    except (TypeError, ValueError):
        # Skip rather than publish garbage.
        return None
    return _publish(
        "confidence_observed", op_id or "",
        {
            "classifier_name": classifier_name or "",
            "confidence": conf_f,
            "threshold": thr_f,
            "below_threshold": conf_f < thr_f,
            "outcome": outcome or "",
        },
    )


def publish_confidence_drop_detected(
    *,
    classifier_name: str,
    drop_pct: float,
    recent_mean: float,
    prior_mean: float,
    window_size: int,
) -> Optional[str]:
    """Producer hook for the periodic drop-detector tick.

    Caller invokes ``ring.confidence_drop_indicators(name, ...)``;
    if ``result["drop_detected"] is True`` the caller fires this
    bridge. Operators get an alert when classifier confidence
    drops monotonically over the recent window."""
    if not is_confidence_drop_detected_enabled():
        return None
    try:
        drop_f = float(drop_pct)
        recent_f = float(recent_mean)
        prior_f = float(prior_mean)
        window_i = int(window_size)
    except (TypeError, ValueError):
        return None
    return _publish(
        "confidence_drop_detected", "",
        {
            "classifier_name": classifier_name or "",
            "drop_pct": drop_f,
            "recent_mean": recent_f,
            "prior_mean": prior_f,
            "window_size": window_i,
        },
    )


def publish_slo_breached(
    *,
    phase: str,
    p95_s: float,
    slo_s: float,
    sample_count: int,
) -> Optional[str]:
    """Producer hook for LatencySLODetector.check_breach() returning
    a non-None LatencySLOBreachEvent. Operators get a real-time
    SLO-breach ping rather than waiting for a periodic GET poll."""
    if not is_slo_breached_enabled():
        return None
    try:
        p95_f = float(p95_s)
        slo_f = float(slo_s)
        n = int(sample_count)
    except (TypeError, ValueError):
        return None
    overshoot_s = p95_f - slo_f
    overshoot_pct = (
        (p95_f - slo_f) / slo_f * 100.0 if slo_f > 0 else 0.0
    )
    return _publish(
        "slo_breached", "",
        {
            "phase": phase or "",
            "p95_s": p95_f,
            "slo_s": slo_f,
            "overshoot_s": overshoot_s,
            "overshoot_pct": overshoot_pct,
            "sample_count": n,
        },
    )


def publish_flag_changed(
    *,
    flag_name: str,
    prev_value: Optional[str],
    next_value: Optional[str],
    is_added: bool = False,
    is_removed: bool = False,
    is_changed: bool = False,
) -> Optional[str]:
    """Producer hook for FlagChangeMonitor.check() emitting one
    delta. Caller iterates the deltas list and fires once per delta.

    Values are MASKED — raw env values never leave via SSE.
    Mirrors Slice 1 GET endpoint masking discipline."""
    if not is_flag_changed_enabled():
        return None
    return _publish(
        "flag_changed", "",
        {
            "flag_name": flag_name or "",
            "prev_value": _mask_flag_value(prev_value),
            "next_value": _mask_flag_value(next_value),
            "is_added": bool(is_added),
            "is_removed": bool(is_removed),
            "is_changed": bool(is_changed),
        },
    )


def publish_flag_change_event(event: Any) -> Optional[str]:
    """Convenience wrapper: take a FlagChangeEvent dataclass instance
    and publish it via the masked bridge. Best-effort — if the
    object doesn't have the expected attributes, returns None."""
    try:
        return publish_flag_changed(
            flag_name=getattr(event, "flag_name", ""),
            prev_value=getattr(event, "prev_value", None),
            next_value=getattr(event, "next_value", None),
            is_added=bool(getattr(event, "is_added", False)),
            is_removed=bool(getattr(event, "is_removed", False)),
            is_changed=bool(getattr(event, "is_changed", False)),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[Phase8Bridge] publish_flag_change_event exception",
            exc_info=True,
        )
        return None


__all__ = [
    "MAX_PAYLOAD_KEYS",
    "MAX_PAYLOAD_STRING_CHARS",
    "MAX_RATIONALE_CHARS",
    "is_bridge_enabled",
    "is_confidence_drop_detected_enabled",
    "is_confidence_observed_enabled",
    "is_decision_recorded_enabled",
    "is_flag_changed_enabled",
    "is_slo_breached_enabled",
    "publish_confidence_drop_detected",
    "publish_confidence_observed",
    "publish_decision_recorded",
    "publish_flag_change_event",
    "publish_flag_changed",
    "publish_slo_breached",
]
