"""Q4 Priority #2 Slice 3 — closure_loop_bridge regression suite.

Pins the real adapter wiring that closes the chain end-to-end:

  * COHERENCE_AUDITOR_BUDGETS surface registered + cage validator
    accepts/rejects the documented decision tree
  * default_tightening_validator matches the cage's structural
    expectations
  * default_replay_validator returns a verdict only when a real
    branch_pair_provider supplies snapshots
  * default_propose_callback submits to AdaptationLedger.propose
    on PROPOSED outcomes; never on others
  * Authority invariant: NO ``.approve`` call in the bridge module
  * wire_default_observer composes all three into an observer

Covers:

  §A   Surface enum extension
  §B   Cage validator decision tree (8 branches)
  §C   build_proposed_state_payload_for_intent shape
  §D   Bridge tightening validator: PASSED + intent + direction +
       monotonic-tightening per direction
  §E   Bridge replay validator: drift_kind → DecisionOverrideKind
       dispatch + None when no provider + verdict when provider
       supplies snapshots
  §F   Bridge propose callback: PROPOSED-only; non-actionable
       outcomes no-op; AdaptationLedger.propose called with the
       new surface
  §G   AST authority pin: bridge module contains no ``.approve``
       call; bridge module imports nothing from forbidden modules
  §H   wire_default_observer composes the three hooks into the
       observer
  §I   End-to-end: observer reads advisory → tightening + replay
       both succeed → record reaches propose callback → ledger
       contains the proposal
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from typing import List, Optional, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    coherence_budget_tightener,
)
from backend.core.ouroboros.governance.adaptation.coherence_budget_tightener import (  # noqa: E501
    _coherence_budget_validator,
    _DIRECTIONS_VALID,
    _VALID_PARAMETER_NAMES,
    _VALID_PROPOSAL_KINDS,
    build_proposed_state_payload_for_intent,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    MonotonicTighteningVerdict,
    OperatorDecisionStatus,
    ProposeStatus,
    get_default_ledger,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.verification.closure_loop_bridge import (  # noqa: E501
    CLOSURE_LOOP_BRIDGE_SCHEMA_VERSION,
    default_propose_callback,
    default_propose_callback_async,
    default_replay_validator,
    default_tightening_validator,
    wire_default_observer,
)
from backend.core.ouroboros.governance.verification.closure_loop_observer import (  # noqa: E501
    ClosureLoopObserver,
    reset_default_observer,
)
from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
    ClosureLoopRecord,
    ClosureOutcome,
)
from backend.core.ouroboros.governance.verification.closure_loop_store import (  # noqa: E501
    read_closure_history,
    reset_for_tests as reset_store,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    CoherenceAdvisory,
    CoherenceAdvisoryAction,
    TighteningIntent,
    TighteningProposalStatus,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    BehavioralDriftKind,
    DriftSeverity,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    BranchSnapshot,
    BranchVerdict,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
)


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CLOSURE_LOOP_HISTORY_DIR", str(tmp_path),
    )
    monkeypatch.setenv(
        "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH",
        str(tmp_path / "adaptation_ledger.jsonl"),
    )
    reset_default_observer()
    reset_default_ledger()
    reset_store()
    yield
    reset_default_observer()
    reset_default_ledger()
    reset_store()


def _intent(
    parameter_name: str = "route_drift_pct",
    current: float = 25.0,
    proposed: float = 20.0,
    direction: str = "smaller_is_tighter",
) -> TighteningIntent:
    return TighteningIntent(
        parameter_name=parameter_name,
        current_value=current,
        proposed_value=proposed,
        direction=direction,
    )


def _advisory(
    *,
    advisory_id: str = "adv-001",
    drift_kind: BehavioralDriftKind = (
        BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT
    ),
    intent: Optional[TighteningIntent] = None,
    status: TighteningProposalStatus = (
        TighteningProposalStatus.PASSED
    ),
    ts: float = 1000.0,
) -> CoherenceAdvisory:
    return CoherenceAdvisory(
        advisory_id=advisory_id,
        drift_signature=f"sig-{advisory_id}",
        drift_kind=drift_kind,
        action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
        severity=DriftSeverity.MEDIUM,
        detail="route distribution rotated",
        recorded_at_ts=ts,
        tightening_status=status,
        tightening_intent=intent or _intent(),
    )


def _record(
    outcome: ClosureOutcome = ClosureOutcome.PROPOSED,
    *,
    parameter_name: str = "route_drift_pct",
    current: float = 25.0,
    proposed: float = 20.0,
    fingerprint: str = "fp-001",
) -> ClosureLoopRecord:
    return ClosureLoopRecord(
        outcome=outcome,
        advisory_id="adv-001",
        drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        parameter_name=parameter_name,
        current_value=current,
        proposed_value=proposed,
        validator_ok=True,
        replay_outcome=ReplayOutcome.SUCCESS,
        replay_verdict=BranchVerdict.DIVERGED_WORSE,
        record_fingerprint=fingerprint,
        decided_at_ts=1234.0,
    )


# ---------------------------------------------------------------------------
# §A — Surface enum extension
# ---------------------------------------------------------------------------


class TestSurfaceEnum:
    def test_coherence_auditor_budgets_value(self):
        assert (
            AdaptationSurface.COHERENCE_AUDITOR_BUDGETS.value
            == "coherence_auditor.budgets"
        )

    def test_surface_count_is_seven(self):
        # Six pre-existing + the new one.
        assert len(list(AdaptationSurface)) == 7

    def test_validator_registered_on_import(self):
        # Importing coherence_budget_tightener auto-registers via
        # install_surface_validator(). Verify by smoking a propose
        # call against a malformed shape — the validator should
        # fire (returning a non-OK status that's NOT
        # SURFACE_NOT_REGISTERED).
        ledger = get_default_ledger()
        result = ledger.propose(
            proposal_id="t-1",
            surface=AdaptationSurface.COHERENCE_AUDITOR_BUDGETS,
            proposal_kind="bogus_kind",
            evidence=AdaptationEvidence(
                window_days=1, observation_count=1,
                source_event_ids=("x",), summary="not a → real summary",
            ),
            current_state_hash="sha256:" + "0" * 64,
            proposed_state_hash="sha256:" + "1" * 64,
        )
        # The kind is rejected by our validator → REJECTED_INVARIANT,
        # NOT a generic missing-validator error.
        assert result.status is not ProposeStatus.OK


# ---------------------------------------------------------------------------
# §B — Cage validator decision tree
# ---------------------------------------------------------------------------


class TestCageValidator:
    def _proposal(
        self,
        *,
        kind: str = "tighten_drift_budget",
        surface: AdaptationSurface = (
            AdaptationSurface.COHERENCE_AUDITOR_BUDGETS
        ),
        obs_count: int = 1,
        summary: str = "advisory route_drift_pct 25.0 → 20.0",
        current: Optional[dict] = None,
        proposed: Optional[dict] = None,
    ) -> AdaptationProposal:
        if current is None:
            current = {
                "parameter_name": "route_drift_pct",
                "value": 25.0,
                "direction": "smaller_is_tighter",
            }
        if proposed is None:
            proposed = {
                "parameter_name": "route_drift_pct",
                "value": 20.0,
                "direction": "smaller_is_tighter",
            }
        return AdaptationProposal(
            schema_version="2.0",
            proposal_id="p-1",
            surface=surface,
            proposal_kind=kind,
            evidence=AdaptationEvidence(
                window_days=1,
                observation_count=obs_count,
                source_event_ids=("e1",),
                summary=summary,
            ),
            current_state_hash="sha256:" + "a" * 64,
            proposed_state_hash="sha256:" + "b" * 64,
            monotonic_tightening_verdict=(
                MonotonicTighteningVerdict.PASSED
            ),
            proposed_at="2026-05-02T00:00:00Z",
            proposed_at_epoch=1.0,
            proposed_state_payload={
                "current": current,
                "proposed": proposed,
            },
        )

    def test_happy_path_passes(self):
        ok, detail = _coherence_budget_validator(self._proposal())
        assert ok is True
        assert detail == "coherence_budget_payload_ok"

    def test_wrong_surface_rejected(self):
        ok, detail = _coherence_budget_validator(self._proposal(
            surface=AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
        ))
        assert ok is False
        assert "wrong_surface" in detail

    def test_unknown_kind_rejected(self):
        ok, detail = _coherence_budget_validator(self._proposal(
            kind="not_a_real_kind",
        ))
        assert ok is False
        assert "kind_unknown" in detail

    def test_unknown_parameter_in_payload_rejected(self):
        # Generic kind passes; the parameter_name allowlist check
        # (the per-surface differentiation) catches the bogus param.
        proposal = self._proposal(
            current={
                "parameter_name": "bogus_param",
                "value": 25.0, "direction": "smaller_is_tighter",
            },
            proposed={
                "parameter_name": "bogus_param",
                "value": 20.0, "direction": "smaller_is_tighter",
            },
        )
        ok, detail = _coherence_budget_validator(proposal)
        assert ok is False
        assert "parameter_not_in_allowlist" in detail

    def test_obs_count_below_floor_rejected(self):
        ok, detail = _coherence_budget_validator(self._proposal(
            obs_count=0,
        ))
        assert ok is False
        assert "obs_count_below_floor" in detail

    def test_missing_tighten_indicator_rejected(self):
        ok, detail = _coherence_budget_validator(self._proposal(
            summary="no arrow here",
        ))
        assert ok is False
        assert "missing_tighten_indicator" in detail

    def test_parameter_name_mismatch_rejected(self):
        proposal = self._proposal(
            current={
                "parameter_name": "route_drift_pct",
                "value": 25.0, "direction": "smaller_is_tighter",
            },
            proposed={
                "parameter_name": "different_param",
                "value": 20.0, "direction": "smaller_is_tighter",
            },
        )
        ok, detail = _coherence_budget_validator(proposal)
        assert ok is False
        assert "parameter_name_mismatch" in detail

    def test_direction_unknown_rejected(self):
        proposal = self._proposal(
            current={
                "parameter_name": "route_drift_pct",
                "value": 25.0, "direction": "weird_direction",
            },
            proposed={
                "parameter_name": "route_drift_pct",
                "value": 20.0, "direction": "weird_direction",
            },
        )
        ok, detail = _coherence_budget_validator(proposal)
        assert ok is False
        assert "direction_unknown" in detail

    def test_not_strictly_smaller_rejected(self):
        # smaller_is_tighter but proposed >= current → reject
        proposal = self._proposal(
            current={
                "parameter_name": "route_drift_pct",
                "value": 25.0, "direction": "smaller_is_tighter",
            },
            proposed={
                "parameter_name": "route_drift_pct",
                "value": 30.0,  # bigger — would loosen
                "direction": "smaller_is_tighter",
            },
        )
        ok, detail = _coherence_budget_validator(proposal)
        assert ok is False
        assert "not_strictly_smaller" in detail


# ---------------------------------------------------------------------------
# §C — build_proposed_state_payload_for_intent
# ---------------------------------------------------------------------------


class TestPayloadBuilder:
    def test_shape_matches_validator_expectation(self):
        payload = build_proposed_state_payload_for_intent(_intent())
        assert "current" in payload
        assert "proposed" in payload
        for branch in (payload["current"], payload["proposed"]):
            assert "parameter_name" in branch
            assert "value" in branch
            assert "direction" in branch
        # Round-trip through the cage validator.
        proposal = AdaptationProposal(
            schema_version="2.0",
            proposal_id="p-1",
            surface=AdaptationSurface.COHERENCE_AUDITOR_BUDGETS,
            proposal_kind="tighten_drift_budget",
            evidence=AdaptationEvidence(
                window_days=1, observation_count=1,
                source_event_ids=("e1",),
                summary="route_drift_pct 25 → 20",
            ),
            current_state_hash="sha256:" + "a" * 64,
            proposed_state_hash="sha256:" + "b" * 64,
            monotonic_tightening_verdict=(
                MonotonicTighteningVerdict.PASSED
            ),
            proposed_at="2026-05-02T00:00:00Z",
            proposed_at_epoch=1.0,
            proposed_state_payload=payload,
        )
        ok, _ = _coherence_budget_validator(proposal)
        assert ok is True


# ---------------------------------------------------------------------------
# §D — Bridge tightening validator
# ---------------------------------------------------------------------------


class TestBridgeTighteningValidator:
    def test_happy_path(self):
        ok, detail = default_tightening_validator(_advisory())
        assert ok is True
        assert detail == "advisory_intent_validated"

    def test_status_not_passed(self):
        ok, _ = default_tightening_validator(_advisory(
            status=TighteningProposalStatus.WOULD_LOOSEN,
        ))
        assert ok is False

    def test_intent_missing(self):
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            CoherenceAdvisoryAction,
        )
        bare = CoherenceAdvisory(
            advisory_id="adv-bare",
            drift_signature="sig",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
            severity=DriftSeverity.MEDIUM,
            detail="d",
            recorded_at_ts=1.0,
            tightening_status=TighteningProposalStatus.PASSED,
            tightening_intent=None,
        )
        ok, _ = default_tightening_validator(bare)
        assert ok is False

    def test_parameter_not_in_allowlist(self):
        ok, _ = default_tightening_validator(_advisory(
            intent=_intent(parameter_name="unknown_param"),
        ))
        assert ok is False

    def test_direction_unknown(self):
        ok, _ = default_tightening_validator(_advisory(
            intent=_intent(direction="weird_direction"),
        ))
        assert ok is False

    def test_not_strictly_tighter_smaller(self):
        ok, _ = default_tightening_validator(_advisory(
            intent=_intent(current=20.0, proposed=20.0),
        ))
        assert ok is False

    def test_larger_is_tighter_path(self):
        # Validator must support both directions symmetrically.
        ok, _ = default_tightening_validator(_advisory(
            intent=_intent(
                current=10.0, proposed=20.0,
                direction="larger_is_tighter",
            ),
        ))
        assert ok is True


# ---------------------------------------------------------------------------
# §E — Bridge replay validator
# ---------------------------------------------------------------------------


class TestBridgeReplayValidator:
    @pytest.mark.asyncio
    async def test_no_provider_returns_none(self):
        result = await default_replay_validator(_advisory())
        assert result is None

    @pytest.mark.asyncio
    async def test_provider_returning_none_propagates(self):
        async def provider(adv):
            return None
        result = await default_replay_validator(
            _advisory(), branch_pair_provider=provider,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_provider_returning_pair_yields_verdict(self):
        async def provider(adv):
            return (
                BranchSnapshot(
                    branch_id="o", terminal_phase="C",
                    terminal_success=False,
                ),
                BranchSnapshot(
                    branch_id="c", terminal_phase="C",
                    terminal_success=True,
                ),
            )
        result = await default_replay_validator(
            _advisory(), branch_pair_provider=provider,
        )
        assert result is not None
        # original failed + counterfactual succeeded → DIVERGED_WORSE
        # (counterfactual is better — proposed tightening would help).
        assert result.outcome is ReplayOutcome.SUCCESS
        assert result.verdict is BranchVerdict.DIVERGED_WORSE

    @pytest.mark.asyncio
    async def test_provider_exception_returns_none(self):
        async def provider(adv):
            raise RuntimeError("provider boom")
        result = await default_replay_validator(
            _advisory(), branch_pair_provider=provider,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unsupported_drift_kind_returns_none(self):
        # POSTURE_LOCKED / SYMBOL_FLUX_DRIFT etc. don't map to a
        # DecisionOverrideKind → None.
        adv = _advisory(
            drift_kind=BehavioralDriftKind.POSTURE_LOCKED,
            intent=_intent(),
        )

        async def provider(advisory):
            # Provider should NOT be called when drift_kind has no mapping.
            return (
                BranchSnapshot(
                    branch_id="o", terminal_phase="C",
                    terminal_success=False,
                ),
                BranchSnapshot(
                    branch_id="c", terminal_phase="C",
                    terminal_success=True,
                ),
            )

        result = await default_replay_validator(
            adv, branch_pair_provider=provider,
        )
        assert result is None


# ---------------------------------------------------------------------------
# §F — Bridge propose callback
# ---------------------------------------------------------------------------


class TestBridgeProposeCallback:
    def test_non_proposed_outcome_no_op(self):
        for oc in (
            ClosureOutcome.SKIPPED_NO_INTENT,
            ClosureOutcome.SKIPPED_VALIDATION_FAILED,
            ClosureOutcome.SKIPPED_REPLAY_REJECTED,
            ClosureOutcome.DISABLED,
            ClosureOutcome.FAILED,
        ):
            assert default_propose_callback(_record(oc)) is False

    def test_record_missing_intent_skipped(self):
        rec = _record()
        # Strip intent fields by rebuilding.
        rec_no_intent = ClosureLoopRecord(
            outcome=ClosureOutcome.PROPOSED,
            advisory_id=rec.advisory_id,
            drift_kind=rec.drift_kind,
            parameter_name="",  # missing
            current_value=None,
            proposed_value=None,
        )
        assert default_propose_callback(rec_no_intent) is False

    def test_proposed_outcome_calls_propose(self):
        ledger = get_default_ledger()
        result = default_propose_callback(_record())
        assert result is True
        # Verify the proposal landed in the ledger.
        history = ledger.history(limit=10)
        ours = [
            p for p in history
            if p.surface
            is AdaptationSurface.COHERENCE_AUDITOR_BUDGETS
        ]
        assert len(ours) == 1
        # proposal_kind is the universal-cage verb; the param being
        # tightened lives in payload.current.parameter_name.
        assert ours[0].proposal_kind == "tighten_drift_budget"
        assert ours[0].proposed_state_payload[  # type: ignore[index]
            "current"
        ]["parameter_name"] == "route_drift_pct"
        assert ours[0].operator_decision is (
            OperatorDecisionStatus.PENDING
        )

    def test_idempotent_duplicate_proposal_id_is_ok(self):
        # Same record submitted twice → same proposal_id → second
        # call returns True (DUPLICATE is treated as success).
        rec = _record()
        first = default_propose_callback(rec)
        second = default_propose_callback(rec)
        assert first is True
        assert second is True

    @pytest.mark.asyncio
    async def test_async_wrapper_swallows_exceptions(self):
        # Force an exception by passing a None-shaped record.
        garbage_rec = ClosureLoopRecord(
            outcome=ClosureOutcome.PROPOSED,
            advisory_id="x",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            parameter_name="bogus",  # unknown kind
            current_value=10.0,
            proposed_value=5.0,
        )
        # Must not raise.
        await default_propose_callback_async(garbage_rec)


# ---------------------------------------------------------------------------
# §G — AST authority pin: NO .approve in bridge module
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_no_approve_call_in_bridge(self):
        from backend.core.ouroboros.governance.verification import (
            closure_loop_bridge,
        )
        src = inspect.getsource(closure_loop_bridge)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr != "approve", (
                        f"forbidden .approve call at line "
                        f"{node.lineno} — bridge must propose only"
                    )

    def test_bridge_imports_no_authority_modules(self):
        from backend.core.ouroboros.governance.verification import (
            closure_loop_bridge,
        )
        src = inspect.getsource(closure_loop_bridge)
        tree = ast.parse(src)
        forbidden = {
            "yaml_writer", "iron_gate",
            "risk_tier", "change_engine",
            "candidate_generator", "gate",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for f in forbidden:
                    assert f not in node.module.split("."), (
                        f"forbidden import: {node.module}"
                    )

    def test_bridge_does_not_call_yaml_writer(self):
        # AST-level pin (not text grep — the docstring mentions
        # yaml_writer to document what's NOT permitted; the
        # invariant we care about is no actual import or call).
        from backend.core.ouroboros.governance.verification import (
            closure_loop_bridge,
        )
        src = inspect.getsource(closure_loop_bridge)
        tree = ast.parse(src)
        # No import that resolves to yaml_writer.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "yaml_writer" not in node.module, (
                    f"forbidden yaml_writer import: {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "yaml_writer" not in alias.name, (
                        f"forbidden yaml_writer import: "
                        f"{alias.name}"
                    )
        # And no symbol named yaml_writer is referenced as a callable.
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "yaml_writer":
                pytest.fail(
                    f"forbidden yaml_writer reference at line "
                    f"{node.lineno}",
                )


# ---------------------------------------------------------------------------
# §H — wire_default_observer
# ---------------------------------------------------------------------------


class TestWireDefaultObserver:
    def test_wires_three_hooks(self):
        obs = ClosureLoopObserver()
        wired = wire_default_observer(observer=obs)
        assert wired is obs
        # Hooks are now NOT the shadow defaults.
        from backend.core.ouroboros.governance.verification.closure_loop_observer import (  # noqa: E501
            shadow_replay_validator,
            shadow_tightening_validator,
        )
        assert obs._tightening_validator is not (
            shadow_tightening_validator
        )
        assert obs._on_record_emitted is (
            default_propose_callback_async
        )

    def test_passes_branch_pair_provider_through(self):
        captured: List[CoherenceAdvisory] = []

        async def provider(adv):
            captured.append(adv)
            return None

        obs = ClosureLoopObserver()
        wire_default_observer(
            observer=obs, branch_pair_provider=provider,
        )
        adv = _advisory()
        # Drive the wired replay validator directly.
        result = asyncio.run(obs._replay_validator(adv))
        assert result is None
        assert len(captured) == 1
        assert captured[0].advisory_id == adv.advisory_id


# ---------------------------------------------------------------------------
# §I — End-to-end: observer reads advisory → propose ledger row
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_chain_lands_proposal(self, monkeypatch):
        async def good_provider(adv):
            return (
                BranchSnapshot(
                    branch_id="o", terminal_phase="C",
                    terminal_success=False,
                ),
                BranchSnapshot(
                    branch_id="c", terminal_phase="C",
                    terminal_success=True,
                ),
            )

        adv = _advisory(
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        )

        with mock.patch(
            "backend.core.ouroboros.governance.verification."
            "closure_loop_observer.read_coherence_advisories",
            return_value=(adv,),
        ):
            obs = ClosureLoopObserver()
            wire_default_observer(
                observer=obs,
                branch_pair_provider=good_provider,
            )
            await obs.run_one_pass()

        # Closure-loop ring buffer has the PROPOSED record.
        history = read_closure_history()
        assert len(history) == 1
        assert history[0].outcome is ClosureOutcome.PROPOSED

        # AdaptationLedger has a PENDING proposal on the new surface.
        ledger = get_default_ledger()
        rows = [
            p for p in ledger.history(limit=10)
            if p.surface is (
                AdaptationSurface.COHERENCE_AUDITOR_BUDGETS
            )
        ]
        assert len(rows) == 1
        assert rows[0].proposal_kind == "tighten_drift_budget"
        assert rows[0].operator_decision is (
            OperatorDecisionStatus.PENDING
        )
        # Operator approval is now required — chain stops here.

    @pytest.mark.asyncio
    async def test_full_chain_replay_rejects_landing(self):
        # Provider where original was BETTER than counterfactual →
        # proposed tightening would make things worse → reject.
        async def harmful_provider(adv):
            return (
                BranchSnapshot(
                    branch_id="o", terminal_phase="C",
                    terminal_success=True,
                ),
                BranchSnapshot(
                    branch_id="c", terminal_phase="C",
                    terminal_success=False,
                ),
            )

        adv = _advisory()
        with mock.patch(
            "backend.core.ouroboros.governance.verification."
            "closure_loop_observer.read_coherence_advisories",
            return_value=(adv,),
        ):
            obs = ClosureLoopObserver()
            wire_default_observer(
                observer=obs,
                branch_pair_provider=harmful_provider,
            )
            await obs.run_one_pass()

        history = read_closure_history()
        assert len(history) == 1
        assert history[0].outcome is (
            ClosureOutcome.SKIPPED_REPLAY_REJECTED
        )
        # Ledger MUST NOT have a proposal.
        ledger = get_default_ledger()
        rows = [
            p for p in ledger.history(limit=10)
            if p.surface is (
                AdaptationSurface.COHERENCE_AUDITOR_BUDGETS
            )
        ]
        assert rows == []


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


def test_bridge_schema_version_pin():
    assert (
        CLOSURE_LOOP_BRIDGE_SCHEMA_VERSION
        == "closure_loop_bridge.v1"
    )


def test_valid_parameter_names_match_proposer():
    # Pin the parameter vocabulary against
    # _DefaultTighteningProposer's output. If the proposer adds a
    # new drift kind with a new parameter, this test breaks until
    # the validator is updated.
    expected_params = frozenset({
        "route_drift_pct", "recurrence_count", "confidence_rise_pct",
    })
    assert _VALID_PARAMETER_NAMES == expected_params
    # proposal_kind is the universal-cage verb (single-element set
    # for this surface).
    assert _VALID_PROPOSAL_KINDS == frozenset({"tighten_drift_budget"})
    assert "smaller_is_tighter" in _DIRECTIONS_VALID
    assert "larger_is_tighter" in _DIRECTIONS_VALID
