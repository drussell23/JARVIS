"""Move 3 Slice 1 — auto_action_router primitive regression suite.

Pins the contract for the advisory router that closes the
verification → action loop gap (PRD §27.4.3):

  * 5-value ``AdvisoryActionType`` enum — explicit state modeling,
    NO ``None``, NO implicit fall-through (J.A.R.M.A.T.R.I.X.
    operator binding).
  * Master + enforce flags both default-false in Slice 1.
  * Public dispatcher ``propose_advisory_action`` ALWAYS returns
    an ``AdvisoryAction`` (never ``None``); only raises
    ``CostContractViolation`` from the structural guard.
  * Decision precedence: master-off → ESCALATE verdicts →
    family failure rate (DEMOTE / DEFER) → failed category
    (RAISE_FLOOR) → NO_ACTION.
  * Cost contract structural guard — AST pinned + behavioral —
    forbids BG/SPEC ops from carrying ``proposed_risk_tier``
    that would imply a route escalation.
  * Authority invariants — no orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router imports.

Authority Invariant
-------------------
Tests import only the module under test + cost_contract_assertion +
stdlib. No orchestrator / governance internals.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — Module surface + schema
# -----------------------------------------------------------------------


def test_schema_version_pinned():
    from backend.core.ouroboros.governance.auto_action_router import (
        AUTO_ACTION_ROUTER_SCHEMA_VERSION,
    )
    assert AUTO_ACTION_ROUTER_SCHEMA_VERSION == "auto_action_router.1"


def test_action_type_has_exactly_five_values():
    """Operator binding: exactly 5 action types, including
    NO_ACTION as an explicit happy-path value."""
    from backend.core.ouroboros.governance.auto_action_router import (
        AdvisoryActionType,
    )
    values = {v.value for v in AdvisoryActionType}
    assert values == {
        "no_action",
        "defer_op_family",
        "demote_risk_tier",
        "route_to_notify_apply",
        "raise_exploration_floor",
    }
    assert len(values) == 5


def test_advisory_action_is_frozen():
    from backend.core.ouroboros.governance.auto_action_router import (
        AdvisoryAction, AdvisoryActionType,
    )
    a = AdvisoryAction(
        action_type=AdvisoryActionType.NO_ACTION,
        reason_code="test",
        evidence="test",
    )
    with pytest.raises(Exception):
        a.reason_code = "mutated"  # type: ignore[misc]


def test_input_dataclasses_are_frozen():
    from backend.core.ouroboros.governance.auto_action_router import (
        RecentOpOutcome,
        RecentConfidenceVerdict,
        RecentAdaptationProposal,
        AutoActionContext,
    )
    o = RecentOpOutcome(
        op_id="op-1", op_family="x", success=True, risk_tier="SAFE_AUTO",
    )
    with pytest.raises(Exception):
        o.success = False  # type: ignore[misc]

    v = RecentConfidenceVerdict(op_id="op-1", verdict="RETRY")
    with pytest.raises(Exception):
        v.verdict = "ESCALATE"  # type: ignore[misc]

    p = RecentAdaptationProposal(
        proposal_id="p-1", surface="X", operator_outcome="approved",
    )
    with pytest.raises(Exception):
        p.operator_outcome = "rejected"  # type: ignore[misc]

    c = AutoActionContext()
    with pytest.raises(Exception):
        c.current_op_family = "x"  # type: ignore[misc]


# -----------------------------------------------------------------------
# § B — Master flag + enforce flag defaults (SLICE 1)
# -----------------------------------------------------------------------


def test_master_flag_default_true_post_graduation(monkeypatch):
    """Move 3 Slice 4 (2026-04-30) graduated this flag from false
    → true. Asymmetric env semantics — empty/whitespace = unset =
    graduated default-true; explicit falsy hot-reverts."""
    monkeypatch.delenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", raising=False)
    import backend.core.ouroboros.governance.auto_action_router as m
    importlib.reload(m)
    assert m.auto_action_router_enabled() is True


def test_master_flag_explicit_empty_string_post_graduation(monkeypatch):
    """Asymmetric env semantics — explicit empty string is treated
    as unset and returns the graduated default-true."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "")
    import backend.core.ouroboros.governance.auto_action_router as m
    importlib.reload(m)
    assert m.auto_action_router_enabled() is True


def test_master_flag_explicit_truthy(monkeypatch):
    import backend.core.ouroboros.governance.auto_action_router as m
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", val)
        importlib.reload(m)
        assert m.auto_action_router_enabled() is True


def test_master_flag_explicit_falsy(monkeypatch):
    import backend.core.ouroboros.governance.auto_action_router as m
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", val)
        importlib.reload(m)
        assert m.auto_action_router_enabled() is False


def test_enforce_flag_locked_off(monkeypatch):
    """Operator binding: enforce stays off until separately
    authorized. Default false; only explicit truthy turns on."""
    monkeypatch.delenv("JARVIS_AUTO_ACTION_ENFORCE", raising=False)
    import backend.core.ouroboros.governance.auto_action_router as m
    importlib.reload(m)
    assert m.auto_action_enforce() is False
    # Explicit truthy works (so the future graduation arc has a
    # path) but the default remains locked off.
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_AUTO_ACTION_ENFORCE", val)
        importlib.reload(m)
        assert m.auto_action_enforce() is True


# -----------------------------------------------------------------------
# § C — Knob clamping
# -----------------------------------------------------------------------


def test_history_k_default(monkeypatch):
    monkeypatch.delenv("JARVIS_AUTO_ACTION_HISTORY_K", raising=False)
    from backend.core.ouroboros.governance.auto_action_router import (
        auto_action_history_k,
    )
    assert auto_action_history_k() == 8


def test_history_k_floor_at_2(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_ACTION_HISTORY_K", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        auto_action_history_k,
    )
    assert auto_action_history_k() == 2


def test_failure_rate_trip_clamped(monkeypatch):
    from backend.core.ouroboros.governance.auto_action_router import (
        auto_action_failure_rate_trip,
    )
    monkeypatch.setenv("JARVIS_AUTO_ACTION_FAILURE_RATE_TRIP", "1.5")
    assert auto_action_failure_rate_trip() == 1.0
    monkeypatch.setenv("JARVIS_AUTO_ACTION_FAILURE_RATE_TRIP", "-0.5")
    assert auto_action_failure_rate_trip() == 0.0


def test_failure_rate_trip_handles_garbage(monkeypatch):
    from backend.core.ouroboros.governance.auto_action_router import (
        auto_action_failure_rate_trip,
    )
    monkeypatch.setenv("JARVIS_AUTO_ACTION_FAILURE_RATE_TRIP", "not-a-number")
    assert auto_action_failure_rate_trip() == 0.5


# -----------------------------------------------------------------------
# § D — Decision precedence
# -----------------------------------------------------------------------


def _ctx(**kwargs):
    """Helper to construct AutoActionContext with defaults."""
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionContext,
    )
    return AutoActionContext(**kwargs)


def test_master_off_returns_no_action(monkeypatch):
    """Post-graduation: env-unset = default-on, so testing the
    master-off path requires explicit JARVIS_AUTO_ACTION_ROUTER_ENABLED=0."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "0")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryActionType,
    )
    result = propose_advisory_action(_ctx())
    assert result.action_type is AdvisoryActionType.NO_ACTION
    assert result.reason_code == "master_flag_off"


def test_no_signal_returns_no_action(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryActionType,
    )
    result = propose_advisory_action(_ctx())
    assert result.action_type is AdvisoryActionType.NO_ACTION
    assert result.reason_code == "no_signal"


def test_escalate_verdicts_propose_route_to_notify_apply(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryActionType,
        RecentConfidenceVerdict,
    )
    verdicts = tuple(
        RecentConfidenceVerdict(op_id=f"op-{i}", verdict="ESCALATE")
        for i in range(4)
    )
    result = propose_advisory_action(
        _ctx(recent_verdicts=verdicts, current_op_family="test_failure")
    )
    assert result.action_type is AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY
    assert result.reason_code == "recurring_confidence_escalation"
    assert result.proposed_risk_tier == "notify_apply"
    assert result.rolling_escalate_rate == 1.0


def test_family_failure_rate_safe_auto_proposes_demote(monkeypatch):
    """SAFE_AUTO + recurring family failure → DEMOTE_RISK_TIER."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryActionType,
        RecentOpOutcome,
    )
    outcomes = (
        RecentOpOutcome(
            op_id="op-1", op_family="doc_staleness",
            success=False, risk_tier="SAFE_AUTO",
        ),
        RecentOpOutcome(
            op_id="op-2", op_family="doc_staleness",
            success=False, risk_tier="SAFE_AUTO",
        ),
        RecentOpOutcome(
            op_id="op-3", op_family="doc_staleness",
            success=True, risk_tier="SAFE_AUTO",
        ),
    )
    result = propose_advisory_action(
        _ctx(
            recent_outcomes=outcomes,
            current_op_family="doc_staleness",
            current_risk_tier="SAFE_AUTO",
        )
    )
    assert result.action_type is AdvisoryActionType.DEMOTE_RISK_TIER
    assert result.reason_code == "op_family_failure_rate_safe_auto"
    assert result.proposed_risk_tier == "notify_apply"
    assert result.target_op_family == "doc_staleness"
    assert result.rolling_failure_rate >= 0.5


def test_family_failure_rate_higher_tier_proposes_defer(monkeypatch):
    """NOTIFY_APPLY+ family failure → DEFER_OP_FAMILY (not demote
    again — already past SAFE_AUTO)."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryActionType,
        RecentOpOutcome,
    )
    outcomes = (
        RecentOpOutcome(
            op_id="op-1", op_family="github_issue",
            success=False, risk_tier="NOTIFY_APPLY",
        ),
        RecentOpOutcome(
            op_id="op-2", op_family="github_issue",
            success=False, risk_tier="NOTIFY_APPLY",
        ),
    )
    result = propose_advisory_action(
        _ctx(
            recent_outcomes=outcomes,
            current_op_family="github_issue",
            current_risk_tier="NOTIFY_APPLY",
        )
    )
    assert result.action_type is AdvisoryActionType.DEFER_OP_FAMILY
    assert result.reason_code == "op_family_failure_rate"
    assert result.target_op_family == "github_issue"


def test_failed_category_proposes_raise_floor(monkeypatch):
    """When no family failure but a category surfaces in failed
    outcomes → RAISE_EXPLORATION_FLOOR for that category."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryActionType,
        RecentOpOutcome,
    )
    # Outcomes don't match the current op_family but do contain a
    # failed category. Below the family failure-rate trip.
    outcomes = (
        RecentOpOutcome(
            op_id="op-1", op_family="other",
            success=False, risk_tier="SAFE_AUTO",
            failed_category="read_file",
        ),
    )
    result = propose_advisory_action(
        _ctx(
            recent_outcomes=outcomes,
            current_op_family="unrelated",
            current_risk_tier="SAFE_AUTO",
        )
    )
    assert result.action_type is AdvisoryActionType.RAISE_EXPLORATION_FLOOR
    assert result.target_category == "read_file"


def test_escalate_takes_precedence_over_family_failure(monkeypatch):
    """Decision precedence: ESCALATE verdicts come before family
    failure rate."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryActionType,
        RecentOpOutcome, RecentConfidenceVerdict,
    )
    verdicts = (
        RecentConfidenceVerdict(op_id="op-1", verdict="ESCALATE"),
        RecentConfidenceVerdict(op_id="op-2", verdict="ESCALATE"),
    )
    outcomes = (
        RecentOpOutcome(
            op_id="op-1", op_family="x",
            success=False, risk_tier="SAFE_AUTO",
        ),
        RecentOpOutcome(
            op_id="op-2", op_family="x",
            success=False, risk_tier="SAFE_AUTO",
        ),
    )
    result = propose_advisory_action(
        _ctx(
            recent_verdicts=verdicts,
            recent_outcomes=outcomes,
            current_op_family="x",
            current_risk_tier="SAFE_AUTO",
        )
    )
    # Escalate path wins
    assert result.action_type is AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY


def test_dispatcher_never_returns_none(monkeypatch):
    """Operator binding: the public dispatcher MUST return an
    AdvisoryAction on every code path. NO_ACTION is the explicit
    happy-path value."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        propose_advisory_action, AdvisoryAction,
    )
    result = propose_advisory_action(_ctx())
    assert result is not None
    assert isinstance(result, AdvisoryAction)


# -----------------------------------------------------------------------
# § E — Cost contract structural guard
# -----------------------------------------------------------------------


def test_cost_contract_guard_blocks_bg_route_escalation(monkeypatch):
    """If a future caller tries to use _propose_action with a BG
    route + APPROVAL_REQUIRED+ risk tier, raise
    CostContractViolation. AST-pinned + behaviorally tested."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        _propose_action, AdvisoryActionType,
    )
    from backend.core.ouroboros.governance.cost_contract_assertion import (
        CostContractViolation,
    )
    with pytest.raises(CostContractViolation):
        _propose_action(
            action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
            reason_code="test",
            evidence="test",
            current_route="background",
            proposed_risk_tier="approval_required",
        )
    with pytest.raises(CostContractViolation):
        _propose_action(
            action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
            reason_code="test",
            evidence="test",
            current_route="speculative",
            proposed_risk_tier="blocked",
        )


def test_cost_contract_guard_allows_safe_combinations():
    """SAFE_AUTO / NOTIFY_APPLY are below the route-escalation
    threshold; STANDARD/IMMEDIATE routes are not BG/SPEC and so
    have no constraint."""
    from backend.core.ouroboros.governance.auto_action_router import (
        _propose_action, AdvisoryActionType,
    )
    # BG + NOTIFY_APPLY — fine (no route escalation implied)
    a = _propose_action(
        action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
        reason_code="test",
        evidence="test",
        current_route="background",
        proposed_risk_tier="notify_apply",
    )
    assert a.action_type is AdvisoryActionType.DEMOTE_RISK_TIER
    # STANDARD + APPROVAL_REQUIRED — fine (already a higher-cost route)
    b = _propose_action(
        action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
        reason_code="test",
        evidence="test",
        current_route="standard",
        proposed_risk_tier="approval_required",
    )
    assert b.action_type is AdvisoryActionType.DEMOTE_RISK_TIER


def test_cost_contract_guard_ast_pin():
    """Bytes-pin: ``_propose_action`` body MUST contain the
    cost-contract guard pattern. A future refactor that drops the
    guard would defeat the entire structural defense."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/auto_action_router.py"
    ).read_text()
    fn_idx = src.find("def _propose_action(")
    assert fn_idx > 0
    end_idx = src.find("def _failure_rate_for_family(", fn_idx)
    assert end_idx > fn_idx
    body = src[fn_idx:end_idx]
    # The guard reads COST_GATED_ROUTES from cost_contract_assertion
    assert "COST_GATED_ROUTES" in body
    # And raises CostContractViolation
    assert "CostContractViolation" in body
    # On the BG/SPEC current_route path
    assert "cur_norm in COST_GATED_ROUTES" in body


# -----------------------------------------------------------------------
# § F — Authority invariant (AST)
# -----------------------------------------------------------------------


def test_authority_invariant_no_forbidden_imports():
    """The module imports only stdlib + cost_contract_assertion.
    No orchestrator / candidate_generator / providers / etc."""
    import ast
    src = pathlib.Path(
        "backend/core/ouroboros/governance/auto_action_router.py"
    ).read_text()
    tree = ast.parse(src)
    forbidden = {
        "orchestrator",
        "phase_runners",
        "candidate_generator",
        "iron_gate",
        "change_engine",
        "policy",
        "semantic_guardian",
        "semantic_firewall",
        "providers",
        "doubleword_provider",
        "urgency_router",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for tok in forbidden:
                assert tok not in mod, (
                    f"forbidden import in auto_action_router: {mod}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for tok in forbidden:
                    assert tok not in alias.name, (
                        f"forbidden import: {alias.name}"
                    )


def test_test_module_authority():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "providers", "orchestrator", "doubleword_provider",
    )
    for tok in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden: {tok}"
