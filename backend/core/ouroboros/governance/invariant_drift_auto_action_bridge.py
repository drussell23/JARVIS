"""Move 4 Slice 4 — InvariantDriftObserver → auto_action_router bridge.

Slice 3 ships drift detection that emits to a pluggable sink; the
default sink is a no-op. Slice 4 is that sink: it translates drift
records into ``AdvisoryAction`` proposals in the existing Move 3
ledger so drift inherits the full operator-review surface for free
(``/auto-action`` REPL, ``GET /observability/auto-action[/stats]``,
``EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED`` SSE).

This is a **bridge**, not a re-implementation: zero modifications to
``auto_action_router.py`` — the bridge consumes that module's public
API (plus ``_propose_action`` for the §26.6 cost-contract structural
guard, intentionally — single source of truth for the guard).

Severity-aware mapping (env-overridable, no hardcoding):

    CRITICAL → ROUTE_TO_NOTIFY_APPLY  (force human review of next op)
    WARNING  → RAISE_EXPLORATION_FLOOR (defensive tightening)
    INFO     → NO_ACTION              (informational; ledger skips)

The aggregate severity of a drift set is the *highest* among its
records (CRITICAL > WARNING > INFO). One drift bundle = one proposal
(NOT one per record) — drift records from the same cycle share a
root cause and should be operator-reviewed as one unit.

Cost contract preservation (§26.6 + Move 3 scope):

  * Drift detection is **out-of-band of any per-op route**. The
    bridge passes ``current_route="drift_bridge"`` — a sentinel
    route NOT in ``COST_GATED_ROUTES = (BG_ROUTE, SPEC_ROUTE)``,
    so the structural guard in ``_propose_action`` naturally
    bypasses by *contract*, not by accident. The cost guard remains
    enforced for op-bound proposals (postmortem / confidence /
    adaptation readers). Drift is metadata, not routing.
  * Should the guard ever fire (it cannot today, but a future
    refactor might), ``CostContractViolation`` is caught defensively
    and logged — the bridge swallows the error rather than letting
    it propagate up the observer's emit path.

Master flag default-false until Slice 5 graduation:
``JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED``.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + auto_action_router + invariant_drift_auditor
    + invariant_drift_observer ONLY.
  * NEVER imports orchestrator / phase_runners / candidate_generator
    / iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / subagent_scheduler.
  * Never raises out of any public method.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import (
    Mapping,
    Optional,
    Tuple,
)

from backend.core.ouroboros.governance.auto_action_router import (
    AdvisoryAction,
    AdvisoryActionType,
    AutoActionProposalLedger,
    _propose_action,
    get_default_ledger,
    publish_auto_action_proposal_emitted,
)
from backend.core.ouroboros.governance.invariant_drift_auditor import (
    DriftSeverity,
    InvariantDriftRecord,
    InvariantSnapshot,
)
from backend.core.ouroboros.governance.invariant_drift_observer import (
    InvariantDriftSignalEmitter,
    register_signal_emitter,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bridge route sentinel — drift signals are out-of-band of any op route.
# Cost-contract guard is naturally bypassed because this value is NOT in
# COST_GATED_ROUTES = (BG_ROUTE, SPEC_ROUTE).
# ---------------------------------------------------------------------------


_BRIDGE_ROUTE: str = "drift_bridge"


# ---------------------------------------------------------------------------
# Master flag — default false until Slice 5 graduation
# ---------------------------------------------------------------------------


def bridge_enabled() -> bool:
    """``JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED``
    (**graduated 2026-04-30 Slice 5 — default ``true``**).

    Asymmetric env semantics — empty/whitespace = unset = current
    default (post-graduation = ``true``); explicit ``0`` / ``false``
    / ``no`` / ``off`` hot-reverts."""
    raw = os.environ.get(
        "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default — Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Default severity → action mapping (env-overridable)
# ---------------------------------------------------------------------------


_DEFAULT_SEVERITY_MAPPING: Mapping[DriftSeverity, AdvisoryActionType] = {
    DriftSeverity.CRITICAL: AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY,
    DriftSeverity.WARNING: AdvisoryActionType.RAISE_EXPLORATION_FLOOR,
    DriftSeverity.INFO: AdvisoryActionType.NO_ACTION,
}


def severity_to_action_mapping() -> Mapping[
    DriftSeverity, AdvisoryActionType,
]:
    """Read ``JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING`` JSON env. Maps
    severity-string keys (``"critical"`` / ``"warning"`` / ``"info"``)
    to AdvisoryActionType-string values
    (``"route_to_notify_apply"`` etc.).

    Malformed JSON, unknown enum values, or non-dict payloads → fall
    back to defaults (defaults preserved for any unmapped key).
    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING", "",
    ).strip()
    if not raw:
        return dict(_DEFAULT_SEVERITY_MAPPING)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "[InvariantDriftBridge] mapping env is not valid JSON; "
            "using defaults",
        )
        return dict(_DEFAULT_SEVERITY_MAPPING)
    if not isinstance(parsed, dict):
        return dict(_DEFAULT_SEVERITY_MAPPING)
    out = dict(_DEFAULT_SEVERITY_MAPPING)
    for k, v in parsed.items():
        try:
            severity = DriftSeverity(str(k).lower())
            action = AdvisoryActionType(str(v).lower())
        except (ValueError, TypeError):
            continue
        out[severity] = action
    return out


# ---------------------------------------------------------------------------
# Drift → AdvisoryAction translation
# ---------------------------------------------------------------------------


_SEVERITY_RANK: Mapping[DriftSeverity, int] = {
    DriftSeverity.CRITICAL: 2,
    DriftSeverity.WARNING: 1,
    DriftSeverity.INFO: 0,
}


def aggregate_severity(
    drift_records: Tuple[InvariantDriftRecord, ...],
) -> Optional[DriftSeverity]:
    """Return the *highest* severity among ``drift_records``, or
    ``None`` for an empty tuple. Used as the input to the
    severity-action mapping. NEVER raises."""
    if not drift_records:
        return None
    try:
        return max(
            (r.severity for r in drift_records),
            key=lambda s: _SEVERITY_RANK.get(s, -1),
        )
    except Exception:  # noqa: BLE001 — defensive
        return None


def drift_to_action_type(
    drift_records: Tuple[InvariantDriftRecord, ...],
) -> AdvisoryActionType:
    """Map a drift bundle to a single AdvisoryActionType. Empty
    bundle → ``NO_ACTION``. Severity → action via the
    env-overridable mapping table. NEVER raises."""
    severity = aggregate_severity(drift_records)
    if severity is None:
        return AdvisoryActionType.NO_ACTION
    mapping = severity_to_action_mapping()
    return mapping.get(severity, AdvisoryActionType.NO_ACTION)


def _build_evidence(
    drift_records: Tuple[InvariantDriftRecord, ...],
    *,
    max_chars: int = 200,
) -> str:
    """Compact human-readable summary for the evidence field. Caps
    at ``max_chars`` so SSE payload + REPL render don't blow up on
    pathological drift bundles. NEVER raises."""
    try:
        if not drift_records:
            return ""
        parts = []
        for r in drift_records:
            kind = getattr(r.drift_kind, "value", str(r.drift_kind))
            sev = getattr(r.severity, "value", str(r.severity))
            parts.append(f"{sev}:{kind}")
        text = "; ".join(parts)
        if len(text) > max_chars:
            text = text[: max_chars - 3] + "..."
        return text
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _build_advisory_action(
    snapshot: InvariantSnapshot,
    drift_records: Tuple[InvariantDriftRecord, ...],
    action_type: AdvisoryActionType,
) -> Optional[AdvisoryAction]:
    """Construct the AdvisoryAction via ``_propose_action`` so the
    §26.6 cost-contract structural guard is inherited. Returns
    ``None`` on any guard violation OR construction failure (both
    are bugs in the bridge but must not propagate to the observer).

    NEVER raises."""
    try:
        return _propose_action(
            action_type=action_type,
            reason_code="invariant_drift_detected",
            evidence=_build_evidence(drift_records),
            current_route=_BRIDGE_ROUTE,
            target_category="invariant_drift",
            op_id=f"drift-{snapshot.snapshot_id}",
            posture=snapshot.posture_value or "",
            history_size=len(drift_records),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        # CostContractViolation, in particular, is fatal-by-design
        # at the producer level — but at the bridge level we log
        # and skip rather than propagate up the observer's emit
        # path. Logged at WARNING so operators see it.
        logger.warning(
            "[InvariantDriftBridge] _propose_action raised; "
            "skipping ledger append: %s", exc,
        )
        return None


# ---------------------------------------------------------------------------
# Bridge — the InvariantDriftSignalEmitter implementation
# ---------------------------------------------------------------------------


class InvariantDriftAutoActionBridge(InvariantDriftSignalEmitter):
    """Translates drift signals into AdvisoryAction proposals.

    Each call to ``emit`` runs through:

      1. Bridge master flag off → no-op.
      2. Compute action_type from drift severity. NO_ACTION → no-op
         (don't pollute the ledger).
      3. Build AdvisoryAction via ``_propose_action`` (cost-contract
         structurally guarded).
      4. Append to the AutoActionProposalLedger (existing Move 3
         ledger).
      5. Publish the existing
         ``EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED`` SSE event.

    Defensive everywhere. Never raises. Best-effort on every step;
    a single-step failure does not derail the others (e.g., ledger
    write fails → SSE still tries to publish)."""

    def __init__(
        self,
        *,
        ledger: Optional[AutoActionProposalLedger] = None,
    ) -> None:
        self._ledger = (
            ledger if ledger is not None else get_default_ledger()
        )
        # Track in-process emission counts for diagnostic surfaces
        self._lock = threading.Lock()
        self._emit_count_total = 0
        self._emit_count_appended = 0
        self._emit_count_skipped_disabled = 0
        self._emit_count_skipped_no_action = 0
        self._emit_count_failed_construction = 0

    @property
    def ledger(self) -> AutoActionProposalLedger:
        return self._ledger

    def stats(self) -> dict:
        with self._lock:
            return {
                "emit_count_total": self._emit_count_total,
                "emit_count_appended": self._emit_count_appended,
                "emit_count_skipped_disabled": (
                    self._emit_count_skipped_disabled
                ),
                "emit_count_skipped_no_action": (
                    self._emit_count_skipped_no_action
                ),
                "emit_count_failed_construction": (
                    self._emit_count_failed_construction
                ),
            }

    def emit(
        self,
        snapshot: InvariantSnapshot,
        drift_records: Tuple[InvariantDriftRecord, ...],
    ) -> None:
        """Translate drift → AdvisoryAction → ledger append + SSE
        publish. NEVER raises."""
        with self._lock:
            self._emit_count_total += 1

        if not bridge_enabled():
            with self._lock:
                self._emit_count_skipped_disabled += 1
            return

        action_type = drift_to_action_type(drift_records)
        if action_type is AdvisoryActionType.NO_ACTION:
            with self._lock:
                self._emit_count_skipped_no_action += 1
            return

        action = _build_advisory_action(
            snapshot, drift_records, action_type,
        )
        if action is None:
            with self._lock:
                self._emit_count_failed_construction += 1
            return

        # Ledger append (best-effort)
        try:
            appended = self._ledger.append(action)
            if appended:
                with self._lock:
                    self._emit_count_appended += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InvariantDriftBridge] ledger append swallowed: %s",
                exc,
            )

        # SSE publish (best-effort, independent of ledger result)
        try:
            publish_auto_action_proposal_emitted(action)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InvariantDriftBridge] SSE publish swallowed: %s",
                exc,
            )


# ---------------------------------------------------------------------------
# Convenience installer — Slice 5 boot wiring will call this.
# ---------------------------------------------------------------------------


_install_lock = threading.Lock()
_installed_bridge: Optional[InvariantDriftAutoActionBridge] = None


def install_auto_action_bridge(
    *,
    ledger: Optional[AutoActionProposalLedger] = None,
) -> InvariantDriftAutoActionBridge:
    """Construct + register the bridge as the process-wide
    InvariantDriftSignalEmitter. Idempotent — repeated calls return
    the same instance.

    Slice 5 graduation will wire this into ``GovernedLoopService.start``
    so drift signals automatically flow into the auto_action_router
    ledger from boot. Until then, callers explicitly invoke this to
    opt in."""
    global _installed_bridge
    with _install_lock:
        if _installed_bridge is None:
            _installed_bridge = InvariantDriftAutoActionBridge(
                ledger=ledger,
            )
            register_signal_emitter(_installed_bridge)
        return _installed_bridge


def reset_installed_bridge_for_tests() -> None:
    """Drop the installed bridge — does NOT call
    ``register_signal_emitter`` to restore the no-op default; tests
    should call ``reset_signal_emitter`` if they need that."""
    global _installed_bridge
    with _install_lock:
        _installed_bridge = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "InvariantDriftAutoActionBridge",
    "aggregate_severity",
    "bridge_enabled",
    "drift_to_action_type",
    "install_auto_action_bridge",
    "reset_installed_bridge_for_tests",
    "severity_to_action_mapping",
]
