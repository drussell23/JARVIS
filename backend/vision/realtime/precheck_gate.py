"""
PRECHECK Gate — 5 deterministic guards before any vision action.

Every action must pass ALL guards. Fail-closed: internal error in any guard
→ passed=False with PRECHECK_INTERNAL_ERROR. The action is never executed.

Guards (evaluated in order, all failures accumulated):
1. Freshness     — target frame timestamp within freshness_ms of now
2. Confidence    — fused_confidence >= threshold
3. Risk class    — high-risk actions require approval; degraded mode requires
                   approval for ALL actions
4. Idempotency   — action_id not already committed
5. Intent expiry — user intent within expiry_s of now

Env-var configuration (all have sensible defaults):
    VISION_FRESHNESS_MS          default 500
    VISION_CONFIDENCE_THRESHOLD  default 0.75
    VISION_INTENT_EXPIRY_S       default 2.0

Spec: Section 4 of realtime-vision-action-loop-design.md
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# High-risk task types — must stay in sync with ValidationNode (Step 2 spec)
# ---------------------------------------------------------------------------

HIGH_RISK_TASK_TYPES: frozenset[str] = frozenset(
    {
        "file_delete",
        "file_overwrite",
        "payment",
        "purchase",
        "subscribe",
        "email_compose",
        "message_send",
        "unlock",
        "auth",
        "permission_change",
        "system_shutdown",
        "process_kill",
    }
)

# Elevated-risk task types — do not require approval alone but affect risk_class.
ELEVATED_RISK_TASK_TYPES: frozenset[str] = frozenset(
    {
        "file_create",
        "file_move",
        "form_submit",
        "browser_navigate",
    }
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PrecheckResult:
    """
    Outcome of a single PRECHECK evaluation.

    Fields
    ------
    passed              True only when every guard passes.
    failed_guards       List of guard-failure tokens (empty on full pass).
    action_id           The action_id evaluated.
    frame_age_ms        Frame age passed in (milliseconds).
    fused_confidence    Confidence score passed in (0.0 – 1.0).
    risk_class          "safe" | "elevated" | "high_risk"
    approval_required   True when risk_class is high_risk or is_degraded.
    approval_source     Who/what granted approval (None = not yet granted).
    decision_provenance Per-guard outcome dict for audit / telemetry.
    """

    passed: bool
    failed_guards: List[str]
    action_id: str
    frame_age_ms: float
    fused_confidence: float
    risk_class: str                        # "safe" | "elevated" | "high_risk"
    approval_required: bool
    approval_source: Optional[str]
    decision_provenance: Dict[str, object]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class PrecheckGate:
    """
    Stateful gate that evaluates 5 deterministic guards.

    State
    -----
    _committed_actions   Set of action_ids that have already been executed.
                         Populated by ``commit_action()`` after successful dispatch.

    Thread safety
    -------------
    Not thread-safe by design — call from a single async task or protect
    externally if shared across tasks.
    """

    def __init__(self) -> None:
        # Load thresholds from environment at construction time so tests can
        # patch os.environ before instantiation.
        self._freshness_ms: float = float(
            os.environ.get("VISION_FRESHNESS_MS", 500)
        )
        self._confidence_threshold: float = float(
            os.environ.get("VISION_CONFIDENCE_THRESHOLD", 0.75)
        )
        self._intent_expiry_s: float = float(
            os.environ.get("VISION_INTENT_EXPIRY_S", 2.0)
        )
        self._committed_actions: Set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        *,
        frame_age_ms: float,
        fused_confidence: float,
        action_id: str,
        action_type: str,
        target_task_type: str,
        intent_timestamp: float,
        is_degraded: bool = False,
    ) -> PrecheckResult:
        """
        Run all 5 guards and return a ``PrecheckResult``.

        Parameters are keyword-only to prevent positional confusion.

        Fail-closed: any uncaught exception inside a guard produces
        ``PrecheckResult(passed=False, failed_guards=["PRECHECK_INTERNAL_ERROR"])``.
        """
        # Determine risk_class and approval_required up front so they can be
        # included in the result even when a guard raises.
        risk_class = self._classify_risk(target_task_type)
        approval_required = risk_class == "high_risk" or is_degraded

        try:
            failed_guards: List[str] = []
            provenance: Dict[str, object] = {}

            # 1. Freshness
            freshness_ok, freshness_prov = self._check_freshness(frame_age_ms)
            provenance["freshness"] = freshness_prov
            if not freshness_ok:
                failed_guards.append("STALE_FRAME")

            # 2. Confidence
            confidence_ok, confidence_prov = self._check_confidence(fused_confidence)
            provenance["confidence"] = confidence_prov
            if not confidence_ok:
                failed_guards.append("LOW_CONFIDENCE")

            # 3. Risk
            risk_ok, risk_token, risk_prov = self._check_risk(
                target_task_type, risk_class, is_degraded
            )
            provenance["risk"] = risk_prov
            if not risk_ok:
                failed_guards.append(risk_token)

            # 4. Idempotency
            idempotency_ok, idempotency_prov = self._check_idempotency(action_id)
            provenance["idempotency"] = idempotency_prov
            if not idempotency_ok:
                failed_guards.append("IDEMPOTENCY_HIT")

            # 5. Intent expiry
            expiry_ok, expiry_prov = self._check_intent_expiry(intent_timestamp)
            provenance["intent_expiry"] = expiry_prov
            if not expiry_ok:
                failed_guards.append("INTENT_EXPIRED")

            passed = len(failed_guards) == 0

            return PrecheckResult(
                passed=passed,
                failed_guards=failed_guards,
                action_id=action_id,
                frame_age_ms=frame_age_ms,
                fused_confidence=fused_confidence,
                risk_class=risk_class,
                approval_required=approval_required,
                approval_source=None,
                decision_provenance=provenance,
            )

        except Exception as exc:  # noqa: BLE001 — fail-closed invariant
            return PrecheckResult(
                passed=False,
                failed_guards=["PRECHECK_INTERNAL_ERROR"],
                action_id=action_id,
                frame_age_ms=frame_age_ms,
                fused_confidence=fused_confidence,
                risk_class=risk_class,
                approval_required=approval_required,
                approval_source=None,
                decision_provenance={
                    "internal_error": repr(exc),
                },
            )

    def commit_action(self, action_id: str) -> None:
        """
        Record *action_id* as successfully executed.

        Must be called by the caller after the action has been dispatched
        so that subsequent identical requests are blocked by the idempotency
        guard.
        """
        self._committed_actions.add(action_id)

    # ------------------------------------------------------------------
    # Internal guards
    # ------------------------------------------------------------------

    def _check_freshness(self, frame_age_ms: float):
        """
        Guard 1 — Frame freshness.

        Returns (ok: bool, provenance: dict).
        """
        ok = frame_age_ms <= self._freshness_ms
        prov = {
            "frame_age_ms": frame_age_ms,
            "threshold_ms": self._freshness_ms,
            "passed": ok,
        }
        return ok, prov

    def _check_confidence(self, fused_confidence: float):
        """
        Guard 2 — Fused confidence threshold.

        Returns (ok: bool, provenance: dict).
        """
        ok = fused_confidence >= self._confidence_threshold
        prov = {
            "fused_confidence": fused_confidence,
            "threshold": self._confidence_threshold,
            "passed": ok,
        }
        return ok, prov

    def _check_risk(self, target_task_type: str, risk_class: str, is_degraded: bool):
        """
        Guard 3 — Risk classification.

        Returns (ok: bool, failure_token: str, provenance: dict).
        ``failure_token`` is only meaningful when ok=False.
        """
        if is_degraded:
            prov = {
                "risk_class": risk_class,
                "is_degraded": True,
                "passed": False,
                "reason": "degraded_mode_requires_approval",
            }
            return False, "DEGRADED_REQUIRES_APPROVAL", prov

        if risk_class == "high_risk":
            prov = {
                "risk_class": risk_class,
                "target_task_type": target_task_type,
                "is_degraded": False,
                "passed": False,
                "reason": "high_risk_requires_approval",
            }
            return False, "RISK_REQUIRES_APPROVAL", prov

        prov = {
            "risk_class": risk_class,
            "target_task_type": target_task_type,
            "is_degraded": False,
            "passed": True,
        }
        return True, "", prov

    def _check_idempotency(self, action_id: str):
        """
        Guard 4 — Idempotency.

        Returns (ok: bool, provenance: dict).
        """
        already_committed = action_id in self._committed_actions
        ok = not already_committed
        prov = {
            "action_id": action_id,
            "already_committed": already_committed,
            "committed_count": len(self._committed_actions),
            "passed": ok,
        }
        return ok, prov

    def _check_intent_expiry(self, intent_timestamp: float):
        """
        Guard 5 — Intent expiry.

        *intent_timestamp* is a ``time.time()``-style float (seconds since epoch).

        Returns (ok: bool, provenance: dict).
        """
        now = time.time()
        age_s = now - intent_timestamp
        ok = age_s <= self._intent_expiry_s
        prov = {
            "intent_age_s": age_s,
            "expiry_s": self._intent_expiry_s,
            "passed": ok,
        }
        return ok, prov

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_risk(target_task_type: str) -> str:
        """
        Return "safe", "elevated", or "high_risk" for the given task type.

        Unknown task types default to "elevated" (conservative).
        """
        if target_task_type in HIGH_RISK_TASK_TYPES:
            return "high_risk"
        if target_task_type in ELEVATED_RISK_TASK_TYPES:
            return "elevated"
        # Anything explicitly understood as benign — or unknown (conservative default)
        # Maps well-known safe types to "safe"; unknown → "elevated".
        _KNOWN_SAFE: frozenset[str] = frozenset(
            {
                "click",
                "scroll",
                "hover",
                "screenshot",
                "read",
                "observe",
                "system_command",
                "keyboard_shortcut",
            }
        )
        if target_task_type in _KNOWN_SAFE:
            return "safe"
        return "elevated"
