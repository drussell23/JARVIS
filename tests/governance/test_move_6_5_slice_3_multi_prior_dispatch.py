"""Move 6.5 Slice 3 — Dispatch adapter test spine.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Slice 3 must be one new call site, not copy-paste.
   Antivenom: each prior's candidate is a full citizen of
   Iron Gate + SemanticGuardian + risk-tier + mutation budget
   before consensus aggregation. Divergence after gates →
   NOTIFY_APPLY / operator-visible rationale ('which prior
   chose what') — never auto-apply on diverged AST/outcome
   classes."

Pinned coverage (~38 tests):
  * Closed taxonomies (MultiPriorDecision 5-value +
    ConsensusActionRecommendation 4-value) bytes-pinned
  * Master flag default-FALSE (separate from Slice 1 + 2)
  * all_masters_enabled composes 3 flags via logical AND
  * evaluate_dispatch_decision: 5 outcome arms with
    parametrized inputs
  * recommend_action: 5-outcome → 4-action mapping
    (consensus → ACCEPT_CANONICAL / majority →
    CLAMP_TO_NOTIFY_APPLY / disagreement → ESCALATE /
    disabled+failed → FALL_THROUGH)
  * recommend_action defensive: None / malformed verdict
    returns FALL_THROUGH
  * build_rationale: contains consensus header + per-prior
    rows with prior_id + AST signature prefix + diff preview
  * build_rationale defensive: None / empty rolls returns ""
  * CostGovernorAdapter: parameterless is_exceeded() composes
    CostGovernor.is_exceeded(op_id) + binds at construction
  * CostGovernorAdapter defensive: missing governor / flaky
    governor → returns False (no spurious cancellations)
  * CostGovernorAdapter read-only — AST pin enforces (5th pin)
  * dispatch_multi_prior end-to-end: gate → materialize → run
    → recommend → wrap
  * dispatch_multi_prior happy paths: convergent → ACCEPT,
    divergent → ESCALATE, majority → CLAMP
  * dispatch_multi_prior gate failures: master-off → DISABLED
    + FALL_THROUGH; route-fail → SKIP_ROUTE + FALL_THROUGH;
    posture-fail → SKIP_POSTURE + FALL_THROUGH
  * dispatch_multi_prior threads CostGovernorAdapter through
    Slice 2's runner (cancellation works end-to-end)
  * fired property: True iff ENABLED + verdict_result present
  * 5 AST pins clean + each fires on synthetic regression
  * Public API surface complete + register_flags + swallows
    registry errors
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_dispatch.py"
    )


def _enable_all_masters(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_decision_taxonomy_5_values():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        MultiPriorDecision,
    )
    assert {d.name for d in MultiPriorDecision} == {
        "ENABLED", "DISABLED",
        "SKIP_ROUTE", "SKIP_POSTURE", "SKIP_OP_BLANK",
    }


def test_action_taxonomy_4_values():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation,
    )
    assert {
        a.name for a in ConsensusActionRecommendation
    } == {
        "ACCEPT_CANONICAL", "CLAMP_TO_NOTIFY_APPLY",
        "ESCALATE_TO_OPERATOR_REVIEW", "FALL_THROUGH",
    }


# ---------------------------------------------------------------------------
# Master flag composition
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_all_masters_requires_all_three(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        all_masters_enabled,
    )
    # All off
    for k in (
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED",
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED",
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    assert all_masters_enabled() is False

    # Only Slice 3 → still False (Slice 1 + Slice 2 off)
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED", "true",
    )
    assert all_masters_enabled() is False

    # Add Slice 1 → still False (Slice 2 off)
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    assert all_masters_enabled() is False

    # Add Slice 2 → True
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    assert all_masters_enabled() is True


# ---------------------------------------------------------------------------
# evaluate_dispatch_decision — gate matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id, route, posture, expected", [
        ("op-1", "complex", "EXPLORE", "enabled"),
        ("op-1", "standard", "EXPLORE", "skip_route"),
        ("op-1", "immediate", "EXPLORE", "skip_route"),
        ("op-1", "background", "EXPLORE", "skip_route"),
        ("op-1", "speculative", "EXPLORE", "skip_route"),
        ("op-1", "complex", "HARDEN", "skip_posture"),
        ("op-1", "complex", "CONSOLIDATE", "skip_posture"),
        ("op-1", "complex", "MAINTAIN", "skip_posture"),
        ("", "complex", "EXPLORE", "skip_op_blank"),
    ],
)
def test_dispatch_decision_matrix(
    monkeypatch, op_id, route, posture, expected,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        MultiPriorDecision, evaluate_dispatch_decision,
    )
    decision = evaluate_dispatch_decision(
        op_id=op_id, route=route, posture=posture,
    )
    assert decision.value == expected


def test_dispatch_decision_disabled_when_master_off(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        MultiPriorDecision, evaluate_dispatch_decision,
    )
    decision = evaluate_dispatch_decision(
        op_id="op-1", route="complex", posture="EXPLORE",
    )
    assert decision is MultiPriorDecision.DISABLED


# ---------------------------------------------------------------------------
# recommend_action — 5-outcome → 4-action mapping
# ---------------------------------------------------------------------------


def _make_verdict_with_outcome(outcome_value: str):
    """Build a fake MultiPriorVerdictResult-shaped object
    carrying the requested outcome value. We don't need the
    full Move 6 verdict round-trip — recommend_action only
    reads the .consensus_verdict.outcome.value chain."""

    class _Outcome:
        def __init__(self, v):
            self.value = v

    class _Consensus:
        def __init__(self, v):
            self.outcome = _Outcome(v)

    class _Verdict:
        def __init__(self, v):
            self.consensus_verdict = _Consensus(v)

    return _Verdict(outcome_value)


@pytest.mark.parametrize(
    "outcome, expected_action", [
        ("consensus", "accept_canonical"),
        ("majority_consensus", "clamp_to_notify_apply"),
        ("disagreement", "escalate_to_operator_review"),
        ("disabled", "fall_through"),
        ("failed", "fall_through"),
    ],
)
def test_recommend_action_mapping(
    outcome, expected_action,
):
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        recommend_action,
    )
    verdict = _make_verdict_with_outcome(outcome)
    assert recommend_action(verdict).value == expected_action


def test_recommend_action_none_returns_fall_through():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation, recommend_action,
    )
    assert recommend_action(None) is (
        ConsensusActionRecommendation.FALL_THROUGH
    )


def test_recommend_action_unknown_outcome_returns_fall_through():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation, recommend_action,
    )
    verdict = _make_verdict_with_outcome("no-such-outcome")
    assert recommend_action(verdict) is (
        ConsensusActionRecommendation.FALL_THROUGH
    )


def test_recommend_action_malformed_verdict():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation, recommend_action,
    )

    class Bad:
        consensus_verdict = None

    assert recommend_action(Bad()) is (
        ConsensusActionRecommendation.FALL_THROUGH
    )


# ---------------------------------------------------------------------------
# build_rationale — operator-facing
# ---------------------------------------------------------------------------


def test_build_rationale_empty_on_none():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        build_rationale,
    )
    assert build_rationale(None) == ""


@pytest.mark.asyncio
async def test_build_rationale_contains_consensus_header(
    monkeypatch,
):
    """End-to-end: rationale should include consensus
    summary line + per-prior rows with prior_id + sig +
    diff_preview."""
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        build_rationale, dispatch_multi_prior,
    )

    async def divergent(*, prior, roll_id):  # noqa: ARG001
        return f"unique-{prior.prior_id}"

    verdict = await dispatch_multi_prior(
        divergent, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    rationale = build_rationale(verdict.verdict_result)
    assert "consensus=" in rationale
    assert "prior_id=" in rationale
    assert "diff_preview=" in rationale
    # All 4 priors should surface in the rationale
    assert rationale.count("prior_id=") == 4


# ---------------------------------------------------------------------------
# CostGovernorAdapter
# ---------------------------------------------------------------------------


def test_cost_adapter_no_governor_returns_false():
    """Defensive: missing governor → returns False (no
    spurious cancellations)."""
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        CostGovernorAdapter,
    )
    a = CostGovernorAdapter(op_id="op-1", governor=None)
    # Default governor is None in test env
    assert a.is_exceeded() is False


def test_cost_adapter_calls_governor_with_op_id():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        CostGovernorAdapter,
    )
    fake_gov = MagicMock()
    fake_gov.is_exceeded.return_value = True
    a = CostGovernorAdapter(
        op_id="op-1", governor=fake_gov,
    )
    assert a.is_exceeded() is True
    fake_gov.is_exceeded.assert_called_once_with("op-1")


def test_cost_adapter_governor_raises_returns_false():
    """Flaky governor → defensive False (no spurious
    cancellations)."""
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        CostGovernorAdapter,
    )
    fake_gov = MagicMock()
    fake_gov.is_exceeded.side_effect = RuntimeError("boom")
    a = CostGovernorAdapter(
        op_id="op-1", governor=fake_gov,
    )
    assert a.is_exceeded() is False


def test_cost_adapter_binds_op_id():
    """Same governor, two adapters with different op_ids →
    each calls is_exceeded with its own op_id."""
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        CostGovernorAdapter,
    )
    fake_gov = MagicMock()
    fake_gov.is_exceeded.side_effect = lambda oid: (
        oid == "op-A"
    )
    a = CostGovernorAdapter(
        op_id="op-A", governor=fake_gov,
    )
    b = CostGovernorAdapter(
        op_id="op-B", governor=fake_gov,
    )
    assert a.is_exceeded() is True
    assert b.is_exceeded() is False


# ---------------------------------------------------------------------------
# dispatch_multi_prior — end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_master_off_returns_fall_through(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation,
        MultiPriorDecision, dispatch_multi_prior,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    assert v.decision is MultiPriorDecision.DISABLED
    assert v.action_recommendation is (
        ConsensusActionRecommendation.FALL_THROUGH
    )
    assert v.fired is False
    assert v.prior_set is None
    assert v.verdict_result is None


@pytest.mark.asyncio
async def test_dispatch_route_fail_returns_skip_route(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        MultiPriorDecision, dispatch_multi_prior,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="standard", posture="EXPLORE",
    )
    assert v.decision is MultiPriorDecision.SKIP_ROUTE
    assert v.fired is False


@pytest.mark.asyncio
async def test_dispatch_posture_fail_returns_skip_posture(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        MultiPriorDecision, dispatch_multi_prior,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="HARDEN",
    )
    assert v.decision is MultiPriorDecision.SKIP_POSTURE
    assert v.fired is False


@pytest.mark.asyncio
async def test_dispatch_convergent_yields_accept_canonical(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation,
        MultiPriorDecision, dispatch_multi_prior,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    assert v.decision is MultiPriorDecision.ENABLED
    assert v.action_recommendation is (
        ConsensusActionRecommendation.ACCEPT_CANONICAL
    )
    assert v.fired is True
    assert v.prior_set is not None
    assert v.verdict_result is not None
    assert v.verdict_result.completed_count == 4


@pytest.mark.asyncio
async def test_dispatch_divergent_yields_escalate(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation, dispatch_multi_prior,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return f"unique-{prior.prior_id}"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    assert v.action_recommendation is (
        ConsensusActionRecommendation
        .ESCALATE_TO_OPERATOR_REVIEW
    )
    assert v.rationale  # non-empty rationale


@pytest.mark.asyncio
async def test_dispatch_majority_yields_clamp(monkeypatch):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        ConsensusActionRecommendation, dispatch_multi_prior,
    )

    call = {"n": 0}

    async def gen(*, prior, roll_id):  # noqa: ARG001
        idx = call["n"]
        call["n"] += 1
        return "outlier" if idx == 3 else "majority"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    assert v.action_recommendation is (
        ConsensusActionRecommendation.CLAMP_TO_NOTIFY_APPLY
    )


@pytest.mark.asyncio
async def test_dispatch_threads_cost_adapter(monkeypatch):
    """The cost-governor passed to dispatch_multi_prior is
    threaded through the cost adapter to the runner. When the
    governor reports exceeded, all rolls cancel."""
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )

    fake_gov = MagicMock()
    fake_gov.is_exceeded.return_value = True

    async def slow(*, prior, roll_id):  # noqa: ARG001
        await asyncio.sleep(2.0)
        return "x"

    v = await dispatch_multi_prior(
        slow, op_id="op-1",
        route="complex", posture="EXPLORE",
        cost_governor=fake_gov,
        timeout_per_roll_s=10.0,
        cost_check_interval_s=0.05,
        grace_period_s=0.5,
    )
    assert v.fired is True
    assert v.verdict_result.cancelled_count == 4


@pytest.mark.asyncio
async def test_dispatch_enabled_override_bypasses_master(
    monkeypatch,
):
    """enabled_override=True bypasses the gate decision —
    same Slice 2 discipline."""
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED", raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        MultiPriorDecision, dispatch_multi_prior,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="EXPLORE",
        enabled_override=True,
    )
    assert v.decision is MultiPriorDecision.ENABLED
    assert v.fired is True


@pytest.mark.asyncio
async def test_dispatch_to_dict_shape(monkeypatch):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    d = v.to_dict()
    assert d["op_id"] == "op-1"
    assert "decision" in d
    assert "action_recommendation" in d
    assert "rationale" in d
    assert "prior_set" in d
    assert "verdict_result" in d
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_dispatch_decision_taxonomy_5_values",
        "multi_prior_dispatch_action_taxonomy_4_values",
        "multi_prior_dispatch_master_default_false",
        "multi_prior_dispatch_authority_asymmetry",
        "multi_prior_dispatch_cost_adapter_read_only",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_decision_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class MultiPriorDecision:
    ENABLED = "enabled"
    DISABLED = "disabled"
    EXTRA = "extra"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_dispatch_decision_taxonomy_5_values"
        )
    )
    assert pin.validate(tree, bad)


def test_action_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class ConsensusActionRecommendation:
    ACCEPT_CANONICAL = "x"
    EXTRA_ACTION = "y"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_dispatch_action_taxonomy_4_values"
        )
    )
    assert pin.validate(tree, bad)


def test_authority_pin_fires_on_iron_gate_import():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_dispatch_authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_cost_adapter_read_only_pin_fires_on_mutation():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class CostGovernorAdapter:
    def is_exceeded(self):
        self.governor.cap_usd = 0  # forbidden mutation
        return False
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_dispatch_cost_adapter_read_only"
        )
    )
    assert pin.validate(tree, bad)


def test_cost_adapter_read_only_pin_fires_on_other_method():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class CostGovernorAdapter:
    def is_exceeded(self):
        self.governor.reset()  # forbidden non-is_exceeded call
        return False
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_dispatch_cost_adapter_read_only"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_dispatch as mod,
    )
    expected = {
        "MULTI_PRIOR_DISPATCH_SCHEMA_VERSION",
        "ConsensusActionRecommendation",
        "CostGovernorAdapter",
        "DispatchVerdict", "MultiPriorDecision",
        "all_masters_enabled",
        "build_rationale", "dispatch_multi_prior",
        "evaluate_dispatch_decision",
        "master_enabled", "recommend_action",
        "register_flags", "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_master():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 1
    assert (
        registry.register.call_args.kwargs["name"]
        == "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED"
    )


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)
