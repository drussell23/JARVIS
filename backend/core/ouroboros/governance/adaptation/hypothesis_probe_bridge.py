"""Item #3 — bridges from Phase 7.6 ProbeResult to ledgers.

The Phase 7.6 HypothesisProbe runner returns a terminal
``ProbeResult``. Per the PRD spec:

  > Bridge to Pass C: confirmed hypotheses become adaptation
  > proposals (feeds Slice 2 + 3 mining surfaces). Refuted
  > hypotheses become POSTMORTEMs (feeds PostmortemRecall).

This module materializes those bridges as two pure helpers:

  * ``bridge_confirmed_to_adaptation_ledger(probe_result, hypothesis,
    surface, kind, payload)`` — when probe verdict is CONFIRMED,
    propose an adaptation to the AdaptationLedger
  * ``bridge_to_hypothesis_ledger(probe_result, hypothesis_id,
    hypothesis_ledger)`` — record probe outcome on every terminal
    verdict (CONFIRMED → validated=True; REFUTED → validated=False;
    INCONCLUSIVE_* → validated=None)

Both bridges are best-effort (return BridgeResult with structured
status; NEVER raise into caller).

## Design constraints (load-bearing)

  * **Master flag**: ``JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED``
    (default false). When off, both bridges no-op + return
    SKIPPED_MASTER_OFF.
  * **Skip non-terminal verdicts**: SKIPPED_MASTER_OFF /
    SKIPPED_NO_PROBER / SKIPPED_EMPTY_HYPOTHESIS verdicts produce
    no ledger writes (the probe didn't actually run).
  * **AdaptationLedger bridge**: only fires on CONFIRMED — refused
    + inconclusive verdicts don't propose adaptations.
  * **HypothesisLedger bridge**: fires on CONFIRMED + REFUTED +
    INCONCLUSIVE_* (the probe ran and reached SOME terminal state).
  * **Stdlib + adaptation/hypothesis_ledger only** import surface.
  * **NEVER raises**.

## Default-off

``JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED`` (default false).
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
    ProbeResult,
    ProbeVerdict,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationSurface,
    ProposeStatus,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


def is_bridges_enabled() -> bool:
    """Master flag — ``JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED``
    (default false until graduation cadence)."""
    return os.environ.get(
        "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Bridge result shape
# ---------------------------------------------------------------------------


class BridgeStatus(str, enum.Enum):
    OK = "ok"
    SKIPPED_MASTER_OFF = "skipped_master_off"
    SKIPPED_NON_TERMINAL = "skipped_non_terminal"
    SKIPPED_NOT_CONFIRMED = "skipped_not_confirmed"
    SKIPPED_NO_HYPOTHESIS = "skipped_no_hypothesis"
    LEDGER_REJECTED = "ledger_rejected"
    LEDGER_FAILED = "ledger_failed"
    HYPOTHESIS_NOT_FOUND = "hypothesis_not_found"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True)
class BridgeResult:
    """Terminal result of a bridge call. Structured + frozen so
    callers can persist verbatim into observability ledgers."""

    status: BridgeStatus
    detail: str = ""
    proposal_id: Optional[str] = None
    hypothesis_id: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status is BridgeStatus.OK

    @property
    def is_skipped(self) -> bool:
        return self.status in (
            BridgeStatus.SKIPPED_MASTER_OFF,
            BridgeStatus.SKIPPED_NON_TERMINAL,
            BridgeStatus.SKIPPED_NOT_CONFIRMED,
            BridgeStatus.SKIPPED_NO_HYPOTHESIS,
        )


# ---------------------------------------------------------------------------
# Bridge 1: confirmed → AdaptationLedger.propose
# ---------------------------------------------------------------------------


def bridge_confirmed_to_adaptation_ledger(
    probe_result: ProbeResult,
    *,
    surface: AdaptationSurface,
    proposal_kind: str,
    proposal_id: str,
    current_state_hash: str,
    proposed_state_hash: str,
    proposed_state_payload: Optional[Dict[str, Any]] = None,
    summary: Optional[str] = None,
    hypothesis_id: Optional[str] = None,
    ledger: Optional[AdaptationLedger] = None,
) -> BridgeResult:
    """Materialize a CONFIRMED probe result as an adaptation proposal.

    Pre-checks (in order):
      1. Master flag off → SKIPPED_MASTER_OFF
      2. probe_result.verdict != CONFIRMED → SKIPPED_NOT_CONFIRMED
      3. proposal_id empty → INVALID_INPUT

    Otherwise: builds an ``AdaptationEvidence`` from the probe result
    + summary + calls ``AdaptationLedger.propose()``. The propose
    call's substrate validators (monotonic-tightening, surface
    validators, etc.) still apply — bridge cannot bypass them.

    NEVER raises.
    """
    if not is_bridges_enabled():
        return BridgeResult(
            status=BridgeStatus.SKIPPED_MASTER_OFF,
            detail="master_off",
            hypothesis_id=hypothesis_id,
        )
    if probe_result.verdict is not ProbeVerdict.CONFIRMED:
        return BridgeResult(
            status=BridgeStatus.SKIPPED_NOT_CONFIRMED,
            detail=f"verdict={probe_result.verdict.value}",
            hypothesis_id=hypothesis_id,
        )
    pid = (proposal_id or "").strip()
    if not pid:
        return BridgeResult(
            status=BridgeStatus.INVALID_INPUT,
            detail="proposal_id_empty",
            hypothesis_id=hypothesis_id,
        )

    if ledger is None:
        from backend.core.ouroboros.governance.adaptation.ledger import (
            get_default_ledger,
        )
        ledger = get_default_ledger()
    assert ledger is not None  # for type-checker

    evidence_summary = (
        summary
        or f"hypothesis confirmed via probe in {probe_result.rounds} round(s); "
           f"final_evidence_chars={len(probe_result.final_evidence)}"
    )
    evidence = AdaptationEvidence(
        window_days=1,
        observation_count=max(1, probe_result.rounds),
        source_event_ids=tuple(probe_result.evidence_hashes),
        summary=evidence_summary,
    )
    try:
        result = ledger.propose(
            proposal_id=pid,
            surface=surface,
            proposal_kind=proposal_kind,
            evidence=evidence,
            current_state_hash=current_state_hash,
            proposed_state_hash=proposed_state_hash,
            proposed_state_payload=proposed_state_payload,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[HypothesisProbeBridge] adaptation propose raised %s "
            "for proposal_id=%s — bridge skipped",
            type(exc).__name__, pid,
        )
        return BridgeResult(
            status=BridgeStatus.LEDGER_FAILED,
            detail=f"raised:{type(exc).__name__}",
            proposal_id=pid,
            hypothesis_id=hypothesis_id,
        )
    if result.status is ProposeStatus.OK:
        logger.info(
            "[HypothesisProbeBridge] confirmed → adaptation proposal_id=%s "
            "surface=%s kind=%s", pid, surface.value, proposal_kind,
        )
        return BridgeResult(
            status=BridgeStatus.OK,
            detail=f"propose_status={result.status.value}",
            proposal_id=pid,
            hypothesis_id=hypothesis_id,
        )
    return BridgeResult(
        status=BridgeStatus.LEDGER_REJECTED,
        detail=f"propose_status={result.status.value}:{result.detail}",
        proposal_id=pid,
        hypothesis_id=hypothesis_id,
    )


# ---------------------------------------------------------------------------
# Bridge 2: probe outcome → HypothesisLedger.record_outcome
# ---------------------------------------------------------------------------


_TERMINAL_VERDICTS = frozenset({
    ProbeVerdict.CONFIRMED,
    ProbeVerdict.REFUTED,
    ProbeVerdict.INCONCLUSIVE_BUDGET,
    ProbeVerdict.INCONCLUSIVE_TIMEOUT,
    ProbeVerdict.INCONCLUSIVE_DIMINISHING,
    ProbeVerdict.INCONCLUSIVE_PROBER_ERROR,
})


def _verdict_to_validated_flag(
    verdict: ProbeVerdict,
) -> Optional[bool]:
    if verdict is ProbeVerdict.CONFIRMED:
        return True
    if verdict is ProbeVerdict.REFUTED:
        return False
    return None  # inconclusive verdicts


def bridge_to_hypothesis_ledger(
    probe_result: ProbeResult,
    hypothesis_id: str,
    hypothesis_ledger: Any,  # HypothesisLedger; lazy-typed to avoid import
) -> BridgeResult:
    """Record the probe outcome on the HypothesisLedger.

    Mapping:
      * CONFIRMED → record_outcome(actual_outcome=evidence,
        validated=True)
      * REFUTED → record_outcome(actual_outcome=evidence,
        validated=False)
      * INCONCLUSIVE_* → record_outcome(actual_outcome=notes,
        validated=None)
      * SKIPPED_* → SKIPPED_NON_TERMINAL (probe didn't run)

    Pre-checks (in order):
      1. Master flag off → SKIPPED_MASTER_OFF
      2. verdict not in _TERMINAL_VERDICTS → SKIPPED_NON_TERMINAL
      3. hypothesis_id empty → INVALID_INPUT
      4. ledger.find_by_id returns None → HYPOTHESIS_NOT_FOUND

    NEVER raises.
    """
    if not is_bridges_enabled():
        return BridgeResult(
            status=BridgeStatus.SKIPPED_MASTER_OFF,
            detail="master_off",
            hypothesis_id=hypothesis_id,
        )
    if probe_result.verdict not in _TERMINAL_VERDICTS:
        return BridgeResult(
            status=BridgeStatus.SKIPPED_NON_TERMINAL,
            detail=f"verdict={probe_result.verdict.value}",
            hypothesis_id=hypothesis_id,
        )
    hid = (hypothesis_id or "").strip()
    if not hid:
        return BridgeResult(
            status=BridgeStatus.INVALID_INPUT,
            detail="hypothesis_id_empty",
        )

    validated = _verdict_to_validated_flag(probe_result.verdict)
    if probe_result.verdict is ProbeVerdict.CONFIRMED:
        outcome = (
            probe_result.final_evidence
            or "(probe confirmed without final evidence)"
        )
    elif probe_result.verdict is ProbeVerdict.REFUTED:
        outcome = (
            probe_result.final_evidence
            or "(probe refuted without final evidence)"
        )
    else:
        outcome = (
            probe_result.notes
            or f"inconclusive:{probe_result.verdict.value}"
        )

    try:
        ok = hypothesis_ledger.record_outcome(
            hid, outcome, validated,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[HypothesisProbeBridge] hypothesis_ledger.record_outcome "
            "raised %s for hypothesis_id=%s — bridge skipped",
            type(exc).__name__, hid,
        )
        return BridgeResult(
            status=BridgeStatus.LEDGER_FAILED,
            detail=f"raised:{type(exc).__name__}",
            hypothesis_id=hid,
        )
    if not ok:
        return BridgeResult(
            status=BridgeStatus.HYPOTHESIS_NOT_FOUND,
            detail="record_outcome_returned_false",
            hypothesis_id=hid,
        )
    logger.info(
        "[HypothesisProbeBridge] hypothesis_id=%s recorded "
        "verdict=%s validated=%s",
        hid, probe_result.verdict.value, validated,
    )
    return BridgeResult(
        status=BridgeStatus.OK,
        detail=f"verdict={probe_result.verdict.value}_validated={validated}",
        hypothesis_id=hid,
    )


__all__ = [
    "BridgeResult",
    "BridgeStatus",
    "bridge_confirmed_to_adaptation_ledger",
    "bridge_to_hypothesis_ledger",
    "is_bridges_enabled",
]
