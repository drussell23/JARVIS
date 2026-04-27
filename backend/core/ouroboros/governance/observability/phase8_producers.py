"""Phase 9.5 Part B — Phase 8 producer-wiring hooks.

Per PRD §3.6.6 brutal review: "Phase 8 shipped the dashboard before
it shipped the data feed." The 5 substrate modules + 8 GET endpoints
+ 5 SSE event types are all in place but **none of the substrate
ledgers are being recorded into in production code**. This module
ships the lightweight NEVER-raises producer hooks that the orchestrator
+ classifiers + phase-timing instrumentation can call to feed the
substrate.

## Why a wrapping module instead of "just call the substrate"

The orchestrator hot path runs millions of times per session. Three
constraints make a thin producer wrapper the right shape:

  1. **Performance**: substrate calls cost ~microseconds when the
     master flag is on (JSONL append + flock). When off, the calls
     return instantly. Producers shouldn't have to know either way.
  2. **NEVER-raises contract**: if a producer call ever raises, the
     orchestrator must keep running. Each substrate module is already
     NEVER-raises, but **the import itself can fail** (e.g. pre-
     installed test fixture's monkey-patched module). We catch that
     here so producers don't have to.
  3. **Composability with SSE bridges**: producer hooks ALSO publish
     to the Phase 8 SSE bridge (Slice 2) when both master flags
     align — operators get live ping AND ledger row from one call.

## Producer hook surface

Five hooks matching the 5 Phase 8 substrate modules:

  * ``record_decision(op_id, phase, decision, factors, weights, rationale)``
  * ``record_confidence(classifier_name, confidence, threshold, outcome, op_id)``
  * ``record_phase_latency(phase, latency_s)``
  * ``check_breach_and_publish(phase)``  — combines detector.check_breach +
    SSE publish_slo_breached
  * ``check_flag_changes_and_publish()`` — combines monitor.check +
    SSE publish_flag_changed per delta

## Authority posture (locked + AST-pinned)

  * **Read/write only over the 5 substrate modules** — no imports
    from gate / execution modules.
  * **Stdlib + typing only** at top level (substrate + SSE bridge
    imported lazily inside helpers).
  * **NEVER raises** — every code path returns ``Optional[result]``.
  * **No master flag** at this layer — substrate's own master flags
    govern. Producers pay the same cost as direct substrate calls.
  * **Bounded** payload sizes via the substrate's existing caps;
    this layer adds no extra rate-limiting.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision-trace producer
# ---------------------------------------------------------------------------


def record_decision(
    *,
    op_id: str,
    phase: str,
    decision: str,
    factors: Optional[Mapping[str, Any]] = None,
    weights: Optional[Mapping[str, float]] = None,
    rationale: str = "",
) -> bool:
    """Record one decision-trace row + best-effort SSE publish.

    Returns True on substrate success. NEVER raises.
    """
    ok = False
    try:
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            get_default_ledger,
        )
        ledger = get_default_ledger()
        ok, _detail = ledger.record(
            op_id=op_id,
            phase=phase,
            decision=decision,
            factors=dict(factors or {}),
            weights=dict(weights or {}),
            rationale=rationale,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[Phase8Producers] record_decision raised", exc_info=True,
        )
        return False
    # Best-effort SSE publish (Slice 2 bridge handles its own master flag).
    try:
        from backend.core.ouroboros.governance.observability.sse_bridge import (  # noqa: E501
            publish_decision_recorded,
        )
        publish_decision_recorded(
            op_id=op_id, phase=phase, decision=decision,
            rationale=rationale,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Phase8Producers] decision SSE publish raised",
            exc_info=True,
        )
    return ok


# ---------------------------------------------------------------------------
# Confidence-ring producer
# ---------------------------------------------------------------------------


def record_confidence(
    *,
    classifier_name: str,
    confidence: float,
    threshold: float,
    outcome: str,
    op_id: str = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Record one classifier confidence observation + SSE publish.

    Returns True on substrate success. NEVER raises.
    """
    ok = False
    try:
        from backend.core.ouroboros.governance.observability.latent_confidence_ring import (  # noqa: E501
            get_default_ring,
        )
        ring = get_default_ring()
        ok, _detail = ring.record(
            classifier_name=classifier_name,
            confidence=confidence,
            threshold=threshold,
            outcome=outcome,
            extra=dict(extra or {}),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Phase8Producers] record_confidence raised", exc_info=True,
        )
        return False
    try:
        from backend.core.ouroboros.governance.observability.sse_bridge import (  # noqa: E501
            publish_confidence_observed,
        )
        publish_confidence_observed(
            classifier_name=classifier_name,
            confidence=confidence,
            threshold=threshold,
            outcome=outcome,
            op_id=op_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Phase8Producers] confidence SSE publish raised",
            exc_info=True,
        )
    return ok


# ---------------------------------------------------------------------------
# Latency SLO producer + breach-check + publish
# ---------------------------------------------------------------------------


def record_phase_latency(phase: str, latency_s: float) -> bool:
    """Record one phase-latency sample. NEVER raises.

    Per latency_slo_detector contract: requires MIN_SAMPLES_FOR_BREACH
    samples before any breach can fire — so call this at the end of
    every phase, not just on long ones.
    """
    try:
        from backend.core.ouroboros.governance.observability.latency_slo_detector import (  # noqa: E501
            get_default_detector,
        )
        detector = get_default_detector()
        ok, _detail = detector.record(phase, latency_s)
        return bool(ok)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Phase8Producers] record_phase_latency raised",
            exc_info=True,
        )
        return False


def check_breach_and_publish(phase: str) -> bool:
    """Run a single-phase breach check + publish to SSE on positive.

    Returns True iff a breach event was published. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.observability.latency_slo_detector import (  # noqa: E501
            get_default_detector,
        )
        detector = get_default_detector()
        breach = detector.check_breach(phase)
        if breach is None:
            return False
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Phase8Producers] check_breach raised", exc_info=True,
        )
        return False
    try:
        from backend.core.ouroboros.governance.observability.sse_bridge import (  # noqa: E501
            publish_slo_breached,
        )
        publish_slo_breached(
            phase=breach.phase,
            p95_s=breach.p95_s,
            slo_s=breach.slo_s,
            sample_count=breach.sample_count,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Phase8Producers] slo_breach SSE publish raised",
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Flag-change producer + delta publishing
# ---------------------------------------------------------------------------


def check_flag_changes_and_publish() -> int:
    """Run the FlagChangeMonitor.check() tick + publish each delta
    to SSE (masked, per Slice 2 contract).

    Returns the number of deltas published. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
            get_default_monitor,
        )
        monitor = get_default_monitor()
        deltas = monitor.check()
        if not deltas:
            return 0
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Phase8Producers] check_flag_changes raised",
            exc_info=True,
        )
        return 0
    published = 0
    for delta in deltas:
        try:
            from backend.core.ouroboros.governance.observability.sse_bridge import (  # noqa: E501
                publish_flag_change_event,
            )
            publish_flag_change_event(delta)
            published += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Phase8Producers] flag SSE publish raised",
                exc_info=True,
            )
    return published


# ---------------------------------------------------------------------------
# Multi-op timeline producer (write-side; reads via observability/ide_routes)
# ---------------------------------------------------------------------------


def append_timeline_event(
    *,
    op_id: str,
    event_type: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Append one event to the multi_op_timeline registry (when
    master flag is on). Currently the substrate's `merge_streams`
    is pull-based (reads decision-trace per op_id) — this hook is
    a placeholder for when a write-side timeline registry lands.

    For Phase 9.5 it logs to debug + always returns False. NEVER
    raises. Documented surface for future producers."""
    logger.debug(
        "[Phase8Producers] append_timeline_event (no-op): "
        "op_id=%r event_type=%r", op_id, event_type,
    )
    return False


# ---------------------------------------------------------------------------
# Helper: snapshot all substrate flag states (used by /observability)
# ---------------------------------------------------------------------------


def substrate_flag_snapshot() -> Dict[str, bool]:
    """Read-only snapshot of every Phase 8 substrate master flag.

    Returns ``{flag_name: enabled_bool}``. NEVER raises — modules
    that fail to import return False for that slot.
    """
    out: Dict[str, bool] = {
        "decision_trace_ledger": False,
        "latent_confidence_ring": False,
        "flag_change_emitter": False,
        "latency_slo_detector": False,
        "multi_op_timeline": False,
    }
    try:
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            is_ledger_enabled,
        )
        out["decision_trace_ledger"] = bool(is_ledger_enabled())
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core.ouroboros.governance.observability.latent_confidence_ring import (  # noqa: E501
            is_ring_enabled,
        )
        out["latent_confidence_ring"] = bool(is_ring_enabled())
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
            is_emitter_enabled,
        )
        out["flag_change_emitter"] = bool(is_emitter_enabled())
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core.ouroboros.governance.observability.latency_slo_detector import (  # noqa: E501
            is_detector_enabled,
        )
        out["latency_slo_detector"] = bool(is_detector_enabled())
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core.ouroboros.governance.observability.multi_op_timeline import (  # noqa: E501
            is_timeline_enabled,
        )
        out["multi_op_timeline"] = bool(is_timeline_enabled())
    except Exception:  # noqa: BLE001
        pass
    return out


__all__ = [
    "append_timeline_event",
    "check_breach_and_publish",
    "check_flag_changes_and_publish",
    "record_confidence",
    "record_decision",
    "record_phase_latency",
    "substrate_flag_snapshot",
]
