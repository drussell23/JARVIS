"""Slice 194 — Race-Triage & Cross-Model Rotation Matrix.

The Slice 193 registry made abandoned hedge races VISIBLE (dispatches −
victories); this module makes them ACTIONABLE. When BOTH arms of a proactive
hedge die (e.g. an RT ``RuntimeError`` + a structural batch rejection — the
live op-019eae7b shape), the organism must not blindly re-walk the same dead
model every retry round. The triage engine:

  1. Classifies each arm's exception signature (:func:`classify_arm`) and
     confirms a HARD model/endpoint blockage (:func:`triage_dual_failure`).
     Carve-outs per Slice 185 doctrine: an INTERNAL fault (NameError /
     TypeError / … — OUR bug) never blames the model, and a cancelled or
     absent arm is shutdown noise, not blockage evidence.
  2. Blacklists the model FOR THE SCOPE OF THE CURRENT OPERATION
     (:func:`record_dual_arm_blacklist`), riding the schema_drift_tracker's
     bounded per-op storage (``DriftType.DUAL_ARM_FAILURE``) so dual-arm
     events share the /drift audit surface for free.
  3. Exposes its OWN dispatch skip predicate (:func:`is_blacklisted_for_op`)
     gated by ``JARVIS_RACE_TRIAGE_ENABLED`` (default TRUE — failure-path-
     only, Slice 170 precedent: it engages only after a race ALREADY died,
     so it can only improve the retry). Deliberately INDEPENDENT of the
     drift-rotation master (``JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED``,
     default FALSE) so the soak rotates without extra env flips.

Cross-model rotation is structural, not duplicated: the sentinel walker in
``candidate_generator`` iterates ``ranked_models`` (the brain_selection_policy
/ dw_catalog ranking) — skipping a blacklisted model means the next loop
iteration IS the next-highest-ranked candidate (Qwen → Nemotron → DeepSeek),
within the same execution cycle. No new catalog query, no blind retry.

Authority invariants: counts and skips ONE doomed candidate per op — never
gates an operation, never imports the orchestrator/gate family. NEVER raises
into the dispatch throat.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_RACE_TRIAGE_ENABLED"


def race_triage_enabled() -> bool:
    """Master gate (default TRUE — failure-path-only: engages only after a
    hedge race has already died with no winner). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


class ArmFailureClass(str, Enum):
    """How one arm of an abandoned race died."""

    VENDOR = "vendor"
    """A vendor-lane failure (transport rupture, 429/5xx, structural/parse
    rejection) — evidence against THIS model/endpoint."""

    INTERNAL_FAULT = "internal_fault"
    """A Python logic error — OUR bug (Slice 185 taxonomy). Never blames
    the model; the strict-type segregation upstream crashes it loudly."""

    CANCELLED = "cancelled"
    """The arm was cancelled (race unwind / outer shutdown) — no evidence."""

    ABSENT = "absent"
    """No exception captured for this arm — no evidence."""


@dataclass(frozen=True)
class RaceTriageVerdict:
    """The triage engine's ruling on one abandoned race."""

    hard_blockage: bool
    reason: str
    fast_class: ArmFailureClass
    stable_class: ArmFailureClass


def classify_arm(exc: Optional[BaseException]) -> ArmFailureClass:
    """Classify one arm's exception signature. NEVER raises."""
    try:
        if exc is None:
            return ArmFailureClass.ABSENT
        if isinstance(exc, asyncio.CancelledError):
            return ArmFailureClass.CANCELLED
        try:
            from backend.core.ouroboros.governance.dw_fault_taxonomy import (
                is_internal_fault,
            )
            if is_internal_fault(exc):
                return ArmFailureClass.INTERNAL_FAULT
        except Exception:  # noqa: BLE001 — taxonomy unavailable → vendor lane
            pass
        return ArmFailureClass.VENDOR
    except Exception:  # noqa: BLE001
        return ArmFailureClass.ABSENT


def triage_dual_failure(
    fast_exc: Optional[BaseException],
    stable_exc: Optional[BaseException],
) -> RaceTriageVerdict:
    """Analyze both arms' exception signatures. A HARD blockage requires BOTH
    arms to have failed in the vendor lane — two independent transports dying
    on the same model is the signature of a dead model/endpoint, not a blip.
    NEVER raises."""
    fast_class = classify_arm(fast_exc)
    stable_class = classify_arm(stable_exc)
    if ArmFailureClass.INTERNAL_FAULT in (fast_class, stable_class):
        return RaceTriageVerdict(
            hard_blockage=False,
            reason="internal fault present — our bug, never blame the model",
            fast_class=fast_class, stable_class=stable_class,
        )
    if fast_class is not ArmFailureClass.VENDOR or (
        stable_class is not ArmFailureClass.VENDOR
    ):
        return RaceTriageVerdict(
            hard_blockage=False,
            reason="single-arm evidence only (other arm cancelled/absent)",
            fast_class=fast_class, stable_class=stable_class,
        )
    return RaceTriageVerdict(
        hard_blockage=True,
        reason=(
            f"both transports failed independently "
            f"(fast={type(fast_exc).__name__}, stable={type(stable_exc).__name__})"
        ),
        fast_class=fast_class, stable_class=stable_class,
    )


def record_dual_arm_blacklist(
    op_id: str, model_id: str, verdict: RaceTriageVerdict,
) -> bool:
    """Blacklist ``model_id`` for the scope of ``op_id`` (hard verdicts only).
    Returns True iff recorded. NEVER raises."""
    try:
        if not race_triage_enabled() or not verdict.hard_blockage:
            return False
        if not op_id or not model_id:
            return False
        from backend.core.ouroboros.governance.schema_drift_tracker import (
            DriftType,
            get_default_tracker,
        )
        get_default_tracker().record(
            op_id=op_id,
            model_id=model_id,
            drift_type=DriftType.DUAL_ARM_FAILURE,
            raw_excerpt=verdict.reason,
        )
        logger.warning(
            "[RaceTriage] dual-arm failure CONFIRMED: model=%s blacklisted for "
            "op=%s (%s) — next dispatch rotates to the next ranked candidate",
            model_id, op_id[:16], verdict.reason,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def is_blacklisted_for_op(op_id: str, model_id: str) -> bool:
    """Dispatch skip predicate — has ``model_id`` suffered a confirmed
    dual-arm failure on ``op_id``? Independent of the drift-rotation master
    (this kind's rotation is owned by JARVIS_RACE_TRIAGE_ENABLED). O(events
    for the op), bounded by the tracker's per-op ring. NEVER raises."""
    try:
        if not race_triage_enabled() or not op_id or not model_id:
            return False
        from backend.core.ouroboros.governance.schema_drift_tracker import (
            DriftType,
            get_default_tracker,
        )
        return any(
            ev.model_id == model_id
            and ev.drift_type is DriftType.DUAL_ARM_FAILURE
            for ev in get_default_tracker().events_for(op_id)
        )
    except Exception:  # noqa: BLE001
        return False
