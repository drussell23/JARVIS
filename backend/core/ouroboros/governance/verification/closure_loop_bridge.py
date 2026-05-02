"""Q4 Priority #2 Slice 3 — closure-loop wiring bridge.

Composes the three real adapters that close the loop end-to-end:

    Coherence Auditor (Priority #1)
            │
            ▼ CoherenceAdvisory (read by closure-loop observer)
    ┌──────────────────────────────────────────────────────────┐
    │ default_tightening_validator(advisory) → (ok, detail)   │
    │   Defense-in-depth structural cage on the advisory's    │
    │   intent shape. The full AdaptationLedger cage runs at  │
    │   propose-time inside ``AdaptationLedger.propose``.     │
    ├──────────────────────────────────────────────────────────┤
    │ default_replay_validator(advisory) → ReplayVerdict|None │
    │   Builds a ReplayTarget if the drift kind has a clean   │
    │   DecisionOverrideKind mapping; calls                   │
    │   ``compute_replay_outcome``. Drift kinds without a     │
    │   mapping return None → orchestrator collapses to       │
    │   SKIPPED_REPLAY_REJECTED (honest: no empirical         │
    │   evidence, no proposal).                               │
    ├──────────────────────────────────────────────────────────┤
    │ default_propose_callback(record) → bool                 │
    │   Only fires when ``record.outcome is PROPOSED``.       │
    │   Builds an AdaptationProposal on the                   │
    │   COHERENCE_AUDITOR_BUDGETS surface and submits via     │
    │   ``AdaptationLedger.propose`` (PENDING for operator    │
    │   approval). NEVER calls ``.approve``.                  │
    └──────────────────────────────────────────────────────────┘
            │
            ▼ AdaptationLedger.propose
    Operator approval via /adapt OR VSCode confidencePolicyPanel
            │
            ▼
    yaml_writer.write          ← OPERATOR-AUTHORIZED ONLY

Authority invariant (AST-pinned in Slice 4):
  The bridge module imports nothing from ``yaml_writer``,
  ``orchestrator``, ``iron_gate``, ``risk_tier``,
  ``change_engine``, ``candidate_generator``, or ``gate``. It
  imports ``AdaptationLedger`` (for ``.propose``) but the AST test
  in Slice 4 will pin that NO ``.approve`` call exists in the
  module body. Operator approval remains the sole path to
  policy mutation.

Cost contract (preserved by construction, mirrors Priority #3
``counterfactual_replay``): every component is zero-LLM.
``compute_closure_outcome`` is pure; the cage validators are
deterministic; the replay engine is AST-pinned no-LLM. The
chain runs at the observer's cadence (default 600s), not per-op.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Optional, Tuple

# Triggers auto-registration of the COHERENCE_AUDITOR_BUDGETS
# surface validator at import (mirror of the four sibling
# tighteners' install_surface_validator() pattern).
from backend.core.ouroboros.governance.adaptation import (
    coherence_budget_tightener as _coherence_budget_tightener,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationSurface,
    ProposeStatus,
    get_default_ledger,
)
from backend.core.ouroboros.governance.verification.closure_loop_observer import (  # noqa: E501
    ClosureLoopObserver,
    get_default_observer,
)
from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
    ClosureLoopRecord,
    ClosureOutcome,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    CoherenceAdvisory,
    TighteningIntent,
    TighteningProposalStatus,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    BehavioralDriftKind,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    BranchSnapshot,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
    compute_replay_outcome,
)

logger = logging.getLogger(__name__)


CLOSURE_LOOP_BRIDGE_SCHEMA_VERSION = "closure_loop_bridge.v1"


# ---------------------------------------------------------------------------
# Drift-kind → DecisionOverrideKind dispatch (for replay target
# construction). Closed mapping; drift kinds outside this set produce
# None replay verdicts → SKIPPED_REPLAY_REJECTED at the orchestrator.
# Keeping this explicit + small lets Slice 4's AST test pin the literal
# vocabulary against silent expansion.
# ---------------------------------------------------------------------------


_DRIFT_TO_OVERRIDE: dict = {
    # Recurrence drift suggests boosting Priority #2 PostmortemRecall —
    # that's the empirical question we'd ask a counterfactual replay.
    BehavioralDriftKind.RECURRENCE_DRIFT: (
        DecisionOverrideKind.RECURRENCE_BOOST
    ),
    # Confidence drift suggests evaluating Move 6 Quorum effects on
    # past sessions — was the quorum invocation justified empirically?
    BehavioralDriftKind.CONFIDENCE_DRIFT: (
        DecisionOverrideKind.QUORUM_INVOCATION
    ),
    # Behavioral route drift maps to GATE_DECISION (the verdict the
    # router would have produced under tighter budgets).
    BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT: (
        DecisionOverrideKind.GATE_DECISION
    ),
    # The remaining three drift kinds (POSTURE_LOCKED,
    # SYMBOL_FLUX_DRIFT, POLICY_DEFAULT_DRIFT) are operator-
    # notification-only — the orchestrator already filters them at
    # SKIPPED_NO_INTENT before reaching this dispatch.
}


# ---------------------------------------------------------------------------
# Default tightening validator (defense-in-depth structural cage)
# ---------------------------------------------------------------------------


def default_tightening_validator(
    advisory: CoherenceAdvisory,
) -> Tuple[bool, str]:
    """Defense-in-depth structural cage on the advisory's intent
    shape. The full AdaptationLedger cage runs at propose-time
    inside ``AdaptationLedger.propose`` — this validator catches
    malformed advisories BEFORE the chain wastes a replay-engine
    call on them.

    Decision tree (top-down):

      1. ``tightening_status`` is PASSED.
      2. ``tightening_intent`` is a :class:`TighteningIntent`.
      3. ``parameter_name`` ∈ the budget validator's allowlist.
      4. ``direction`` is a known token.
      5. Monotonic-tightening obeyed per direction.

    Returns ``(ok, detail)``. NEVER raises."""
    try:
        if (
            advisory.tightening_status
            is not TighteningProposalStatus.PASSED
        ):
            return (
                False,
                f"advisory_status_not_passed:"
                f"{advisory.tightening_status.value}",
            )
        intent = advisory.tightening_intent
        if not isinstance(intent, TighteningIntent):
            return (
                False,
                "advisory_intent_missing_or_wrong_type",
            )
        valid_params = (
            _coherence_budget_tightener._VALID_PARAMETER_NAMES
        )
        if intent.parameter_name not in valid_params:
            return (
                False,
                f"intent_parameter_not_in_allowlist:"
                f"{intent.parameter_name}",
            )
        valid_dirs = _coherence_budget_tightener._DIRECTIONS_VALID
        if intent.direction not in valid_dirs:
            return (
                False,
                f"intent_direction_unknown:{intent.direction}",
            )
        # Monotonic-tightening direction check.
        try:
            cur = float(intent.current_value)
            prop = float(intent.proposed_value)
        except (TypeError, ValueError):
            return (False, "intent_values_not_numeric")
        if intent.direction == "smaller_is_tighter":
            if not (prop < cur):
                return (
                    False,
                    f"intent_not_strictly_smaller:"
                    f"current={cur} proposed={prop}",
                )
        else:  # larger_is_tighter
            if not (prop > cur):
                return (
                    False,
                    f"intent_not_strictly_larger:"
                    f"current={cur} proposed={prop}",
                )
        return (True, "advisory_intent_validated")
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ClosureLoopBridge] tightening_validator internal: %s",
            exc,
        )
        return (
            False,
            f"validator_internal_error:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Default replay validator (real Counterfactual Replay)
# ---------------------------------------------------------------------------


# Slice 3 ships the wiring; the actual session lookup (which
# ``BranchSnapshot`` to compare against) is the responsibility of a
# downstream record-discovery layer the bridge doesn't own. Until that
# lands, the default replay validator builds a ReplayTarget but DOES
# NOT execute against real recorded sessions — it returns ``None`` so
# the orchestrator collapses to SKIPPED_REPLAY_REJECTED, which is
# honest (no evidence → no proposal). Tests inject a real engine via
# ``replay_engine`` parameter.


async def default_replay_validator(
    advisory: CoherenceAdvisory,
    *,
    branch_pair_provider: Optional[
        "BranchPairProvider"
    ] = None,
) -> Optional[ReplayVerdict]:
    """Build a ReplayTarget for the advisory's drift kind, then
    invoke ``compute_replay_outcome`` against any branch pair the
    ``branch_pair_provider`` returns.

    When ``branch_pair_provider`` is None (Slice 3 default), the
    function returns ``None`` immediately — no recorded sessions
    available, no proposal warranted. Slice 4 graduation may wire a
    real provider that scans ``.ouroboros/sessions/`` for matching
    sessions; tests inject a stub.

    NEVER raises."""
    try:
        override = _DRIFT_TO_OVERRIDE.get(advisory.drift_kind)
        if override is None:
            # Operator-only drift kind (already filtered upstream).
            return None
        target = ReplayTarget(
            session_id=advisory.advisory_id,  # advisory_id used
                                                # as session correlation
                                                # id when no real
                                                # provider supplies one
            swap_at_phase="GATE",
            swap_decision_kind=override,
            swap_decision_payload={
                "parameter_name": (
                    advisory.tightening_intent.parameter_name
                    if advisory.tightening_intent is not None
                    else ""
                ),
            },
        )
        if branch_pair_provider is None:
            # No recorded session lookup wired yet — honest empty
            # path. Slice 4 may wire this; until then, the chain
            # collapses to SKIPPED_REPLAY_REJECTED.
            return None
        try:
            pair = await branch_pair_provider(advisory)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[ClosureLoopBridge] branch_pair_provider raised: %s",
                exc,
            )
            return None
        if pair is None:
            return None
        original, counterfactual = pair
        return compute_replay_outcome(
            target=target,
            original=original,
            counterfactual=counterfactual,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ClosureLoopBridge] replay_validator internal: %s",
            exc,
        )
        return None


# Branch pair lookup Protocol — not yet wired to a real session
# scanner; Slice 4 (or a follow-up) ships the implementation.
from typing import Awaitable, Callable

BranchPairProvider = Callable[
    [CoherenceAdvisory],
    Awaitable[
        Optional[Tuple[BranchSnapshot, BranchSnapshot]]
    ],
]


# ---------------------------------------------------------------------------
# Default propose callback — real AdaptationLedger.propose
# ---------------------------------------------------------------------------


def _state_hash(payload: Any) -> str:
    """Stable sha256:<hex64> for an arbitrary JSON-serializable
    payload. Mirrors ``ConfidencePolicy.state_hash`` shape so the
    universal cage's hash-comparison invariant works uniformly."""
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(text).hexdigest()
    except Exception:  # noqa: BLE001 — defensive
        return "sha256:" + ("0" * 64)


def _build_evidence_summary(record: ClosureLoopRecord) -> str:
    """Human-readable evidence summary that satisfies the
    ``coherence_budget_tightener`` validator's
    ``→`` indicator requirement."""
    cur = (
        f"{record.current_value:.4g}"
        if record.current_value is not None else "?"
    )
    prop = (
        f"{record.proposed_value:.4g}"
        if record.proposed_value is not None else "?"
    )
    replay = (
        record.replay_verdict.value
        if record.replay_verdict is not None else "n/a"
    )
    return (
        f"closure_loop drift={record.drift_kind.value} "
        f"param={record.parameter_name} {cur} → {prop} "
        f"replay_verdict={replay} "
        f"fingerprint={record.record_fingerprint}"
    )


def default_propose_callback(
    record: ClosureLoopRecord,
    *,
    ledger: Optional[AdaptationLedger] = None,
) -> bool:
    """Submit a PROPOSED record to ``AdaptationLedger.propose`` for
    operator approval.

    Authority invariant:
      This function calls ``.propose`` ONLY. It NEVER calls
      ``.approve``. Operator approval via ``/adapt`` REPL or
      VSCode panel remains the sole path to apply.

    Returns True when the proposal was accepted (or already
    DUPLICATE — idempotent), False on REJECTED / DISABLED /
    capacity / shape errors. NEVER raises.

    Records that aren't ``ClosureOutcome.PROPOSED`` are no-ops —
    Slice 2 already persists them to the closure_loop_history ring
    buffer. The propose path is reserved for actionable outcomes."""
    try:
        if record.outcome is not ClosureOutcome.PROPOSED:
            return False
        if (
            record.parameter_name == ""
            or record.current_value is None
            or record.proposed_value is None
        ):
            logger.debug(
                "[ClosureLoopBridge] propose skipped — record lacks "
                "intent fields: outcome=%s id=%s",
                record.outcome.value, record.advisory_id,
            )
            return False
        # Reconstruct an intent-shaped object so we can reuse the
        # tightener's payload builder (avoids hand-coding the shape
        # again — single source of truth).
        intent_proxy = type("intent_proxy", (), {
            "parameter_name": record.parameter_name,
            "current_value": float(record.current_value),
            "proposed_value": float(record.proposed_value),
            "direction": (
                "smaller_is_tighter"  # all current advisory params
                                       # are "smaller is tighter";
                                       # the cage validator pins this.
            ),
        })()
        payload = (
            _coherence_budget_tightener
            .build_proposed_state_payload_for_intent(intent_proxy)
        )
        evidence = AdaptationEvidence(
            window_days=1,
            observation_count=1,
            source_event_ids=(record.advisory_id,),
            summary=_build_evidence_summary(record),
        )
        proposal_id = (
            f"closure-loop-{record.record_fingerprint or uuid.uuid4().hex[:16]}"
        )
        current_hash = _state_hash(payload.get("current"))
        proposed_hash = _state_hash(payload.get("proposed"))
        active_ledger = ledger or get_default_ledger()
        result = active_ledger.propose(
            proposal_id=proposal_id,
            surface=AdaptationSurface.COHERENCE_AUDITOR_BUDGETS,
            proposal_kind=record.parameter_name,
            evidence=evidence,
            current_state_hash=current_hash,
            proposed_state_hash=proposed_hash,
            proposed_state_payload=payload,
        )
        if result.status in (
            ProposeStatus.OK,
            ProposeStatus.DUPLICATE_PROPOSAL_ID,
        ):
            logger.info(
                "[ClosureLoopBridge] proposed advisory=%s "
                "param=%s status=%s proposal_id=%s",
                record.advisory_id, record.parameter_name,
                result.status.value, proposal_id,
            )
            return True
        logger.info(
            "[ClosureLoopBridge] propose rejected advisory=%s "
            "status=%s detail=%s",
            record.advisory_id, result.status.value,
            result.detail or "",
        )
        return False
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ClosureLoopBridge] propose_callback internal: %s",
            exc,
        )
        return False


async def default_propose_callback_async(
    record: ClosureLoopRecord,
) -> None:
    """Async wrapper so the observer's awaitable
    ``on_record_emitted`` contract is satisfied without making the
    sync ledger call awaitable. NEVER raises."""
    try:
        default_propose_callback(record)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ClosureLoopBridge] propose_callback_async internal: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# Wiring helper
# ---------------------------------------------------------------------------


def wire_default_observer(
    *,
    branch_pair_provider: Optional[BranchPairProvider] = None,
    observer: Optional[ClosureLoopObserver] = None,
) -> ClosureLoopObserver:
    """Compose the three default adapters into a closure-loop
    observer. Returns the observer so callers can ``start()`` it.

    Idempotent — wiring an already-wired observer overwrites its
    validator hooks but keeps its singleton identity stable."""
    obs = observer or get_default_observer()

    async def _replay(advisory: CoherenceAdvisory) -> Optional[ReplayVerdict]:
        return await default_replay_validator(
            advisory, branch_pair_provider=branch_pair_provider,
        )

    obs._tightening_validator = default_tightening_validator
    obs._replay_validator = _replay
    obs._on_record_emitted = default_propose_callback_async
    return obs


__all__ = [
    "BranchPairProvider",
    "CLOSURE_LOOP_BRIDGE_SCHEMA_VERSION",
    "default_propose_callback",
    "default_propose_callback_async",
    "default_replay_validator",
    "default_tightening_validator",
    "wire_default_observer",
]
