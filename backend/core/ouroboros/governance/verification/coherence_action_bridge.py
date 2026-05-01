"""Priority #1 Slice 4 — auto_action_router bridge with monotonic-tightening.

Translates ``BehavioralDriftVerdict`` (Slice 1) into operator-visible
advisory records under the **monotonic-tightening contract** (Phase
C / AdaptationLedger §4.1): coherence drift can ONLY propose to
TIGHTEN budgets, never to loosen them. Loosening requires the
operator's explicit Pass B amend path.

Architecture decisions:

  1. Coherence has its OWN 6-value ``CoherenceAdvisoryAction``
     vocabulary, distinct from Move 3's per-op ``AdvisoryActionType``.
     Move 3 routes per-op decisions; coherence is a CROSS-OP /
     CROSS-WINDOW observation. Forcing one taxonomy onto the other
     would be a workaround. The 1:1 mapping from
     ``BehavioralDriftKind`` → ``CoherenceAdvisoryAction`` is
     structural (J.A.R.M.A.T.R.I.X. closed enum, 6×6).

  2. Monotonic tightening is REUSED, not redefined. The bridge
     imports ``MonotonicTighteningVerdict`` from
     ``adaptation.ledger`` (Phase C) and stamps every advisory
     with the canonical verdict string. AST-pinned by Slice 5:
     the bridge MUST reference this symbol — catches a refactor
     that bypasses the universal cage rule.

  3. Persistence path mirrors Slice 2's audit log discipline —
     append-only ``.jarvis/coherence_advisory.jsonl`` via Tier 1
     #3 ``flock_append_line``. NO read-modify-write path means
     the chain cannot corrupt structurally.

  4. Numeric vs non-numeric drift kinds:
       * ``BEHAVIORAL_ROUTE_DRIFT``, ``RECURRENCE_DRIFT``,
         ``CONFIDENCE_DRIFT`` → numeric tightening intent
         (current_value, proposed_value, direction). Proposer
         computes proposed = current × (1 - tighten_factor)
         clamped to env floor.
       * ``POSTURE_LOCKED``, ``SYMBOL_FLUX_DRIFT``,
         ``POLICY_DEFAULT_DRIFT`` → ``NEUTRAL_NOTIFICATION``
         (no numeric tightening — operator-only review).

  5. Cost contract preservation: bridge is read-only on phase
     state; advisories are STRICTLY ADVISORY (no auto-flag-flip
     path). Operator approval via the future ``/coherence``
     REPL (Slice 5) is the only way an advisory translates to
     state mutation. AST-pinned: bridge MUST NOT import
     orchestrator / iron_gate / policy / providers / etc.

Direct-solve principles:

  * **Asynchronous-ready** — sync record/read APIs are short-
    running; Slice 5's REPL + GET routes will wrap via
    ``asyncio.to_thread`` (mirrors Slice 2's discipline).

  * **Dynamic** — `tighten_factor`, floor budgets, dedup window
    are env-tunable with floor+ceiling clamps. NO hardcoded
    multipliers in tightening logic.

  * **Adaptive** — ``TighteningProposer`` is an injectable
    Protocol; default uses env-driven multiplier; tests inject
    fixed proposers for deterministic verification.

  * **Intelligent** — every advisory carries both its own
    ``TighteningProposalStatus`` (4-value, bridge-local) and the
    canonical ``MonotonicTighteningVerdict`` string from
    AdaptationLedger. Operators can correlate advisories with
    Pass C ledger entries via this shared vocabulary even though
    they live in separate files.

  * **Robust** — every public function NEVER raises. Garbage
    inputs / disk failures all collapse to FAILED outcomes.
    The advisory ID is sha256-derived so duplicate detection is
    stable across processes.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + Slice 1 (coherence_auditor) +
    ``adaptation.ledger`` (``MonotonicTighteningVerdict`` only —
    NO ``AdaptationLedger`` instance methods) + Tier 1 #3
    (``cross_process_jsonl``).
  * MUST reference ``MonotonicTighteningVerdict`` (load-bearing
    structural pin — catches refactor that drops the universal
    cage rule integration).
  * MUST reference ``flock_append_line`` (cross-process safety
    on advisory log append path).
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * No mutation tools.
  * No exec/eval/compile.
  * No async (Slice 4 is sync; Slice 5 surfaces will wrap async).
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

from backend.core.ouroboros.governance.adaptation.ledger import (
    MonotonicTighteningVerdict,
)
from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (
    BehavioralDriftFinding,
    BehavioralDriftKind,
    BehavioralDriftVerdict,
    CoherenceOutcome,
    DriftBudgets,
    DriftSeverity,
)

logger = logging.getLogger(__name__)


COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION: str = (
    "coherence_action_bridge.1"
)


# ---------------------------------------------------------------------------
# Sub-gate flag
# ---------------------------------------------------------------------------


def coherence_action_bridge_enabled() -> bool:
    """``JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED`` (default false
    until Slice 5). Master flag
    (``JARVIS_COHERENCE_AUDITOR_ENABLED``) must also be true for
    advisories to fire. Asymmetric env semantics."""
    raw = os.environ.get(
        "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env knobs — every numeric clamped
# ---------------------------------------------------------------------------


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def tighten_factor() -> float:
    """``JARVIS_COHERENCE_TIGHTEN_FACTOR`` (default 0.8, floor
    0.5, ceiling 0.95).

    Multiplier for the default tightening proposer:
    ``proposed = current * tighten_factor`` (for "smaller is
    tighter" parameters). 0.8 means propose 20% reduction. Floor
    0.5 prevents catastrophic tightening; ceiling 0.95 ensures
    proposals are at least minimally tighter."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_TIGHTEN_FACTOR",
        0.8, floor=0.5, ceiling=0.95,
    )


def advisory_path_default() -> Path:
    """``JARVIS_COHERENCE_ADVISORY_PATH`` (default
    ``.jarvis/coherence_advisory.jsonl``)."""
    raw = os.environ.get(
        "JARVIS_COHERENCE_ADVISORY_PATH", "",
    ).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # Default under the same base dir Slice 2 uses
    try:
        from backend.core.ouroboros.governance.verification.coherence_window_store import (  # noqa: E501
            coherence_base_dir,
        )
        return coherence_base_dir() / "coherence_advisory.jsonl"
    except Exception:  # noqa: BLE001 — defensive
        return Path(".jarvis/coherence_advisory.jsonl").resolve()


# ---------------------------------------------------------------------------
# Closed taxonomies (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class CoherenceAdvisoryAction(str, enum.Enum):
    """6-value closed taxonomy. Maps 1:1 from
    ``BehavioralDriftKind``. DISTINCT from Move 3's
    ``AdvisoryActionType`` (per-op routing); coherence drift is
    a cross-window observation domain requiring its own
    vocabulary.

    ``TIGHTEN_RISK_BUDGET``           — BEHAVIORAL_ROUTE_DRIFT
                                        (route distribution
                                        rotated; tighten the
                                        TVD budget)
    ``OPERATOR_NOTIFICATION_POSTURE`` — POSTURE_LOCKED (operator
                                        sees + may override
                                        posture; no auto-tighten)
    ``RAISE_RISK_TIER_FOR_MODULE``    — SYMBOL_FLUX_DRIFT
                                        (off-graduation flux;
                                        operator escalates next
                                        op against the module)
    ``OPERATOR_NOTIFICATION_POLICY``  — POLICY_DEFAULT_DRIFT (env
                                        vs registry mismatch;
                                        operator reconciles)
    ``INJECT_POSTMORTEM_RECALL_HINT`` — RECURRENCE_DRIFT (forward-
                                        compat: Priority #2
                                        PostmortemRecall consumer
                                        wires here when shipped)
    ``TIGHTEN_CONFIDENCE_BUDGET``     — CONFIDENCE_DRIFT (p99
                                        rising; tighten rise
                                        budget)"""

    TIGHTEN_RISK_BUDGET = "tighten_risk_budget"
    OPERATOR_NOTIFICATION_POSTURE = "operator_notification_posture"
    RAISE_RISK_TIER_FOR_MODULE = "raise_risk_tier_for_module"
    OPERATOR_NOTIFICATION_POLICY = "operator_notification_policy"
    INJECT_POSTMORTEM_RECALL_HINT = (
        "inject_postmortem_recall_hint"
    )
    TIGHTEN_CONFIDENCE_BUDGET = "tighten_confidence_budget"


# 1:1 mapping — closed taxonomy pin verified by Slice 5 graduation
_KIND_TO_ACTION: Dict[
    BehavioralDriftKind, CoherenceAdvisoryAction,
] = {
    BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT: (
        CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET
    ),
    BehavioralDriftKind.POSTURE_LOCKED: (
        CoherenceAdvisoryAction.OPERATOR_NOTIFICATION_POSTURE
    ),
    BehavioralDriftKind.SYMBOL_FLUX_DRIFT: (
        CoherenceAdvisoryAction.RAISE_RISK_TIER_FOR_MODULE
    ),
    BehavioralDriftKind.POLICY_DEFAULT_DRIFT: (
        CoherenceAdvisoryAction.OPERATOR_NOTIFICATION_POLICY
    ),
    BehavioralDriftKind.RECURRENCE_DRIFT: (
        CoherenceAdvisoryAction.INJECT_POSTMORTEM_RECALL_HINT
    ),
    BehavioralDriftKind.CONFIDENCE_DRIFT: (
        CoherenceAdvisoryAction.TIGHTEN_CONFIDENCE_BUDGET
    ),
}


# Drift kinds with numeric tightening proposals (the others are
# operator-notification-only).
_NUMERIC_TIGHTENING_KINDS: frozenset = frozenset({
    BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
    BehavioralDriftKind.RECURRENCE_DRIFT,
    BehavioralDriftKind.CONFIDENCE_DRIFT,
})


class TighteningProposalStatus(str, enum.Enum):
    """4-value closed taxonomy of tightening-verification
    outcomes. Bridge-local; cross-references AdaptationLedger's
    ``MonotonicTighteningVerdict`` via the per-advisory
    ``monotonic_tightening_verdict`` field.

    ``PASSED``               — proposed value is strictly tighter.
    ``WOULD_LOOSEN``         — proposed value loosens; bridge
                               REJECTS structurally (NEVER
                               persisted as actionable).
    ``NEUTRAL_NOTIFICATION`` — non-numeric drift kind; no
                               tightening proposal — operator
                               review only.
    ``FAILED``               — defensive sentinel."""

    PASSED = "passed"
    WOULD_LOOSEN = "would_loosen"
    NEUTRAL_NOTIFICATION = "neutral_notification"
    FAILED = "failed"


class RecordOutcome(str, enum.Enum):
    """5-value closed taxonomy of advisory persistence outcomes."""

    RECORDED = "recorded"
    DEDUPED = "deduped"
    REJECTED_LOOSEN = "rejected_loosen"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TighteningIntent:
    """Numerical tightening proposal. Frozen.

    ``direction`` is one of:
      * ``"smaller_is_tighter"`` — proposed < current is tighter
        (e.g., budget_route_drift_pct: 25% → 20%)
      * ``"larger_is_tighter"`` — proposed > current is tighter
        (e.g., min_confidence_threshold: 0.7 → 0.8)"""

    parameter_name: str
    current_value: float
    proposed_value: float
    direction: str
    schema_version: str = COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parameter_name": self.parameter_name,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "direction": self.direction,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CoherenceAdvisory:
    """One advisory record. Frozen, append-only — the persisted
    representation in ``.jarvis/coherence_advisory.jsonl``."""

    advisory_id: str
    drift_signature: str
    drift_kind: BehavioralDriftKind
    action: CoherenceAdvisoryAction
    severity: DriftSeverity
    detail: str
    recorded_at_ts: float
    tightening_status: TighteningProposalStatus
    tightening_intent: Optional[TighteningIntent] = None
    monotonic_tightening_verdict: str = (
        MonotonicTighteningVerdict.PASSED.value
    )
    schema_version: str = (
        COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "advisory_id": self.advisory_id,
            "drift_signature": self.drift_signature,
            "drift_kind": self.drift_kind.value,
            "action": self.action.value,
            "severity": self.severity.value,
            "detail": self.detail,
            "recorded_at_ts": self.recorded_at_ts,
            "tightening_status": self.tightening_status.value,
            "tightening_intent": (
                self.tightening_intent.to_dict()
                if self.tightening_intent is not None else None
            ),
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["CoherenceAdvisory"]:
        """Schema-tolerant reconstruction. Returns ``None`` on
        schema mismatch / malformed shape. NEVER raises."""
        try:
            if (
                payload.get("schema_version")
                != COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION
            ):
                return None
            kind = BehavioralDriftKind(payload["drift_kind"])
            action = CoherenceAdvisoryAction(payload["action"])
            severity = DriftSeverity(payload["severity"])
            tstatus = TighteningProposalStatus(
                payload["tightening_status"],
            )
            intent_raw = payload.get("tightening_intent")
            intent: Optional[TighteningIntent] = None
            if isinstance(intent_raw, Mapping):
                try:
                    intent = TighteningIntent(
                        parameter_name=str(
                            intent_raw["parameter_name"],
                        ),
                        current_value=float(
                            intent_raw["current_value"],
                        ),
                        proposed_value=float(
                            intent_raw["proposed_value"],
                        ),
                        direction=str(intent_raw["direction"]),
                    )
                except (KeyError, TypeError, ValueError):
                    intent = None
            return cls(
                advisory_id=str(payload["advisory_id"]),
                drift_signature=str(payload["drift_signature"]),
                drift_kind=kind,
                action=action,
                severity=severity,
                detail=str(payload.get("detail", "")),
                recorded_at_ts=float(payload["recorded_at_ts"]),
                tightening_status=tstatus,
                tightening_intent=intent,
                monotonic_tightening_verdict=str(
                    payload.get(
                        "monotonic_tightening_verdict",
                        MonotonicTighteningVerdict.PASSED.value,
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def is_actionable(self) -> bool:
        """True iff status is PASSED — operator can apply the
        proposed tightening. NEUTRAL_NOTIFICATION advisories are
        informational only."""
        return self.tightening_status is (
            TighteningProposalStatus.PASSED
        )


# ---------------------------------------------------------------------------
# TighteningProposer Protocol — injectable for tests
# ---------------------------------------------------------------------------


class TighteningProposer(Protocol):
    """Returns a tightening intent for a finding, or None for
    non-numeric drift kinds. NEVER raises (defensive contract)."""

    def propose(
        self,
        finding: BehavioralDriftFinding,
        budgets: DriftBudgets,
    ) -> Optional[TighteningIntent]:
        ...


class _DefaultTighteningProposer:
    """Default proposer. For numeric kinds, multiplies current
    budget by ``tighten_factor()`` (env-tunable). For non-numeric
    kinds, returns None."""

    def propose(
        self,
        finding: BehavioralDriftFinding,
        budgets: DriftBudgets,
    ) -> Optional[TighteningIntent]:
        try:
            factor = tighten_factor()
            if (
                finding.kind
                is BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT
            ):
                current = float(budgets.route_drift_pct)
                proposed = max(5.0, current * factor)
                if proposed >= current:
                    return None  # already at floor
                return TighteningIntent(
                    parameter_name="route_drift_pct",
                    current_value=current,
                    proposed_value=proposed,
                    direction="smaller_is_tighter",
                )
            if finding.kind is BehavioralDriftKind.RECURRENCE_DRIFT:
                current = float(budgets.recurrence_count)
                # Floor 2 (Slice 1's recurrence_count floor)
                proposed = max(2.0, current - 1.0)
                if proposed >= current:
                    return None
                return TighteningIntent(
                    parameter_name="recurrence_count",
                    current_value=current,
                    proposed_value=proposed,
                    direction="smaller_is_tighter",
                )
            if finding.kind is BehavioralDriftKind.CONFIDENCE_DRIFT:
                current = float(budgets.confidence_rise_pct)
                proposed = max(10.0, current * factor)
                if proposed >= current:
                    return None
                return TighteningIntent(
                    parameter_name="confidence_rise_pct",
                    current_value=current,
                    proposed_value=proposed,
                    direction="smaller_is_tighter",
                )
            # Non-numeric drift kinds — no tightening intent
            return None
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# Internal: monotonic-tightening verification
# ---------------------------------------------------------------------------


def _verify_monotonic_tightening(
    intent: Optional[TighteningIntent],
) -> Tuple[TighteningProposalStatus, str]:
    """Verify the tightening intent obeys monotonic-tightening
    contract. Returns (bridge_status, ledger_verdict_string).
    NEVER raises.

    The ``ledger_verdict_string`` is the canonical
    ``MonotonicTighteningVerdict`` value from
    ``adaptation.ledger`` — operators correlate bridge advisories
    with Pass C ledger entries via this shared vocabulary."""
    try:
        if intent is None:
            return (
                TighteningProposalStatus.NEUTRAL_NOTIFICATION,
                # Notifications don't claim a tightening verdict
                # — use PASSED to indicate "no constraint
                # violated" (it's not loosening because there's
                # nothing to loosen).
                MonotonicTighteningVerdict.PASSED.value,
            )
        cur = float(intent.current_value)
        prop = float(intent.proposed_value)
        direction = str(intent.direction or "").lower()
        if direction == "smaller_is_tighter":
            if prop < cur:
                return (
                    TighteningProposalStatus.PASSED,
                    MonotonicTighteningVerdict.PASSED.value,
                )
            return (
                TighteningProposalStatus.WOULD_LOOSEN,
                (
                    MonotonicTighteningVerdict
                    .REJECTED_WOULD_LOOSEN.value
                ),
            )
        if direction == "larger_is_tighter":
            if prop > cur:
                return (
                    TighteningProposalStatus.PASSED,
                    MonotonicTighteningVerdict.PASSED.value,
                )
            return (
                TighteningProposalStatus.WOULD_LOOSEN,
                (
                    MonotonicTighteningVerdict
                    .REJECTED_WOULD_LOOSEN.value
                ),
            )
        return (
            TighteningProposalStatus.FAILED,
            (
                MonotonicTighteningVerdict
                .REJECTED_WOULD_LOOSEN.value
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return (
            TighteningProposalStatus.FAILED,
            (
                MonotonicTighteningVerdict
                .REJECTED_WOULD_LOOSEN.value
            ),
        )


def _compute_advisory_id(
    drift_signature: str,
    action: CoherenceAdvisoryAction,
    recorded_at_ts: float,
) -> str:
    """Stable sha256[:16] over the advisory's identity tuple.
    Same drift_signature + action + ts produces same id —
    enables idempotent replay verification."""
    try:
        payload = (
            f"{drift_signature}|{action.value}|"
            f"{recorded_at_ts:.6f}"
        )
        return hashlib.sha256(
            payload.encode("utf-8"),
        ).hexdigest()[:16]
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public: propose_coherence_action
# ---------------------------------------------------------------------------


def propose_coherence_action(
    verdict: BehavioralDriftVerdict,
    *,
    current_budgets: Optional[DriftBudgets] = None,
    proposer: Optional[TighteningProposer] = None,
    enabled_override: Optional[bool] = None,
    now_ts: Optional[float] = None,
) -> Tuple[CoherenceAdvisory, ...]:
    """Translate a verdict into one advisory PER finding. Returns
    empty tuple when bridge is disabled or verdict has no
    findings. NEVER raises.

    Each advisory carries:
      * 1:1 ``CoherenceAdvisoryAction`` from the finding's kind
      * ``TighteningIntent`` for numeric kinds (None otherwise)
      * ``TighteningProposalStatus`` (bridge-local 4-value enum)
      * Canonical ``MonotonicTighteningVerdict`` string from
        AdaptationLedger (load-bearing structural reuse)
      * Stable ``advisory_id`` (sha256[:16] over signature +
        action + ts) — operators dedup across replay/restart"""
    try:
        is_enabled = (
            enabled_override if enabled_override is not None
            else coherence_action_bridge_enabled()
        )
        if not is_enabled:
            return tuple()
        if not isinstance(verdict, BehavioralDriftVerdict):
            return tuple()
        # Only DRIFT_DETECTED produces advisories. COHERENT,
        # INSUFFICIENT_DATA, DISABLED, FAILED — no advisory.
        if verdict.outcome is not CoherenceOutcome.DRIFT_DETECTED:
            return tuple()
        if not verdict.findings:
            return tuple()

        budgets = (
            current_budgets if current_budgets is not None
            else DriftBudgets.from_env()
        )
        prop = proposer if proposer is not None else (
            _DefaultTighteningProposer()
        )
        import time as _time
        ts = float(now_ts) if now_ts is not None else _time.time()

        out: List[CoherenceAdvisory] = []
        for finding in verdict.findings:
            try:
                action = _KIND_TO_ACTION.get(finding.kind)
                if action is None:
                    # Should never happen — _KIND_TO_ACTION is
                    # exhaustive over BehavioralDriftKind.
                    continue
                intent = prop.propose(finding, budgets)
                tstatus, ledger_verdict = (
                    _verify_monotonic_tightening(intent)
                )
                # WOULD_LOOSEN intents are NOT persisted as
                # actionable — they become NEUTRAL_NOTIFICATION
                # for operator review (the proposer made a
                # mistake; structural reject prevents corruption
                # of the audit chain).
                if tstatus is TighteningProposalStatus.WOULD_LOOSEN:
                    intent = None
                    tstatus = (
                        TighteningProposalStatus.NEUTRAL_NOTIFICATION
                    )
                    ledger_verdict = (
                        MonotonicTighteningVerdict
                        .REJECTED_WOULD_LOOSEN.value
                    )
                advisory_id = _compute_advisory_id(
                    verdict.drift_signature, action, ts,
                )
                out.append(CoherenceAdvisory(
                    advisory_id=advisory_id,
                    drift_signature=verdict.drift_signature,
                    drift_kind=finding.kind,
                    action=action,
                    severity=finding.severity,
                    detail=finding.detail,
                    recorded_at_ts=ts,
                    tightening_status=tstatus,
                    tightening_intent=intent,
                    monotonic_tightening_verdict=(
                        ledger_verdict
                    ),
                ))
            except Exception:  # noqa: BLE001 — per-finding defensive
                continue
        return tuple(out)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceActionBridge] propose_coherence_action "
            "raised: %s", exc,
        )
        return tuple()


# ---------------------------------------------------------------------------
# Public: record_coherence_advisory
# ---------------------------------------------------------------------------


def record_coherence_advisory(
    advisory: CoherenceAdvisory,
    *,
    path: Optional[Path] = None,
) -> RecordOutcome:
    """Append an advisory to ``.jarvis/coherence_advisory.jsonl``
    via Tier 1 #3 ``flock_append_line``. Append-only — NEVER
    rotates. Caller validates via
    ``advisory.tightening_status``: WOULD_LOOSEN advisories
    cannot reach this path because ``propose_coherence_action``
    structurally converts them to NEUTRAL_NOTIFICATION before
    return. NEVER raises.

    Returns:
      * ``RECORDED``         — append succeeded
      * ``REJECTED_LOOSEN``  — defensive guard (should be
                               unreachable; bridge converts
                               loosen→neutral upstream)
      * ``DISABLED``         — bridge sub-gate off
      * ``FAILED``           — disk failure / serialize error"""
    try:
        if not coherence_action_bridge_enabled():
            return RecordOutcome.DISABLED
        if not isinstance(advisory, CoherenceAdvisory):
            return RecordOutcome.FAILED
        # Defensive: should be unreachable but pin the contract
        if (
            advisory.tightening_status
            is TighteningProposalStatus.WOULD_LOOSEN
        ):
            return RecordOutcome.REJECTED_LOOSEN

        target = (
            Path(path).expanduser().resolve()
            if path is not None
            else advisory_path_default()
        )

        try:
            line = json.dumps(
                advisory.to_dict(), separators=(",", ":"),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[CoherenceActionBridge] advisory serialize "
                "failed: %s", exc,
            )
            return RecordOutcome.FAILED

        ok = flock_append_line(target, line)
        if not ok:
            return RecordOutcome.FAILED
        return RecordOutcome.RECORDED
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceActionBridge] record_coherence_advisory "
            "raised: %s", exc,
        )
        return RecordOutcome.FAILED


# ---------------------------------------------------------------------------
# Public: read_coherence_advisories
# ---------------------------------------------------------------------------


def read_coherence_advisories(
    *,
    since_ts: float = 0.0,
    path: Optional[Path] = None,
    limit: Optional[int] = None,
    drift_kind: Optional[BehavioralDriftKind] = None,
) -> Tuple[CoherenceAdvisory, ...]:
    """Read advisories with ``recorded_at_ts >= since_ts``.
    Optional ``drift_kind`` filter. Schema-mismatched lines
    silently dropped. NEVER raises."""
    try:
        target = (
            Path(path).expanduser().resolve()
            if path is not None
            else advisory_path_default()
        )
        if not target.exists():
            return tuple()
        try:
            lines = [
                ln for ln in target.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines()
                if ln.strip()
            ]
        except OSError:
            return tuple()

        advisories: List[CoherenceAdvisory] = []
        for ln in lines:
            try:
                payload = json.loads(ln)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            adv = CoherenceAdvisory.from_dict(payload)
            if adv is None:
                continue
            if adv.recorded_at_ts < since_ts:
                continue
            if (
                drift_kind is not None
                and adv.drift_kind is not drift_kind
            ):
                continue
            advisories.append(adv)

        # Sort ascending by recorded_at_ts
        advisories.sort(key=lambda a: a.recorded_at_ts)
        if limit is not None and limit >= 0:
            advisories = advisories[-limit:]
        return tuple(advisories)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceActionBridge] read_coherence_advisories "
            "raised: %s", exc,
        )
        return tuple()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION",
    "CoherenceAdvisory",
    "CoherenceAdvisoryAction",
    "RecordOutcome",
    "TighteningIntent",
    "TighteningProposalStatus",
    "TighteningProposer",
    "advisory_path_default",
    "coherence_action_bridge_enabled",
    "propose_coherence_action",
    "read_coherence_advisories",
    "record_coherence_advisory",
    "tighten_factor",
]
