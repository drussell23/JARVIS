"""Tests for recovery_advisor (Slice 1)."""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.recovery_advisor import (
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    RECOVERY_PLAN_SCHEMA_VERSION,
    FailureContext,
    RecoveryPlan,
    RecoverySuggestion,
    STOP_APPROVAL_REQUIRED,
    STOP_APPROVAL_TIMEOUT,
    STOP_ASCII_GATE,
    STOP_CANCELLED_BY_OPERATOR,
    STOP_COST_CAP,
    STOP_EXPLORATION_INSUFFICIENT,
    STOP_IDLE_TIMEOUT,
    STOP_IRON_GATE_REJECT,
    STOP_L2_EXHAUSTED,
    STOP_MULTI_FILE_COVERAGE,
    STOP_POLICY_DENIED,
    STOP_PROVIDER_EXHAUSTED,
    STOP_UNHANDLED_EXCEPTION,
    STOP_VALIDATION_EXHAUSTED,
    advise,
    known_stop_reasons,
    rule_count,
)


# ===========================================================================
# Schema
# ===========================================================================


def test_schema_version_pinned():
    assert RECOVERY_PLAN_SCHEMA_VERSION == "recovery_plan.v1"


def test_known_stop_reasons_is_tuple():
    r = known_stop_reasons()
    assert isinstance(r, tuple)
    assert STOP_COST_CAP in r
    assert STOP_UNHANDLED_EXCEPTION in r


# ===========================================================================
# Frozen value types
# ===========================================================================


def test_failure_context_frozen():
    ctx = FailureContext(op_id="op-1")
    with pytest.raises((AttributeError, TypeError)):
        ctx.op_id = "op-other"  # type: ignore[misc]


def test_recovery_suggestion_frozen():
    s = RecoverySuggestion(title="try it")
    with pytest.raises((AttributeError, TypeError)):
        s.title = "other"  # type: ignore[misc]


def test_recovery_plan_frozen():
    p = RecoveryPlan(op_id="op-1", failure_summary="x")
    with pytest.raises((AttributeError, TypeError)):
        p.op_id = "op-other"  # type: ignore[misc]


# ===========================================================================
# Type guard on advise()
# ===========================================================================


def test_advise_rejects_non_failure_context():
    with pytest.raises(TypeError):
        advise({"op_id": "op-1", "stop_reason": STOP_COST_CAP})  # type: ignore[arg-type]


# ===========================================================================
# Rule coverage — every known stop_reason produces a non-generic plan
# ===========================================================================


@pytest.mark.parametrize("stop_reason", [
    STOP_COST_CAP, STOP_VALIDATION_EXHAUSTED, STOP_L2_EXHAUSTED,
    STOP_APPROVAL_REQUIRED, STOP_APPROVAL_TIMEOUT, STOP_IRON_GATE_REJECT,
    STOP_EXPLORATION_INSUFFICIENT, STOP_ASCII_GATE,
    STOP_MULTI_FILE_COVERAGE, STOP_PROVIDER_EXHAUSTED, STOP_POLICY_DENIED,
    STOP_CANCELLED_BY_OPERATOR, STOP_IDLE_TIMEOUT,
    STOP_UNHANDLED_EXCEPTION,
])
def test_every_known_stop_reason_has_dedicated_rule(stop_reason: str):
    ctx = FailureContext(op_id="op-1", stop_reason=stop_reason)
    plan = advise(ctx)
    assert plan.matched_rule != "generic", (
        f"stop_reason={stop_reason} fell through to generic"
    )
    assert plan.has_suggestions
    assert len(plan.suggestions) <= 3


def test_rule_count_is_stable():
    # Pin the rule count so additions are intentional
    assert rule_count() >= 14


# ===========================================================================
# Unknown stop_reason → generic plan
# ===========================================================================


def test_unknown_stop_reason_yields_generic_plan():
    ctx = FailureContext(op_id="op-1", stop_reason="something_never_seen")
    plan = advise(ctx)
    assert plan.matched_rule == "generic"
    assert plan.has_suggestions
    # Generic plan still points at debug.log
    texts = [s.title.lower() for s in plan.suggestions]
    assert any("debug.log" in t or "debug" in t for t in texts)


def test_completely_empty_context_yields_generic_plan():
    plan = advise(FailureContext())
    assert plan.matched_rule == "generic"
    assert plan.has_suggestions


# ===========================================================================
# Exception fallback
# ===========================================================================


def test_exception_type_triggers_unhandled_exception_rule():
    ctx = FailureContext(
        op_id="op-1",
        exception_type="ValueError",
        exception_message="bad input",
    )
    plan = advise(ctx)
    assert plan.matched_rule == "unhandled_exception"
    assert "ValueError" in plan.failure_summary


def test_exception_type_without_stop_reason_still_matches_rule():
    ctx = FailureContext(op_id="op-1", exception_type="KeyError")
    plan = advise(ctx)
    assert plan.matched_rule == "unhandled_exception"


# ===========================================================================
# Cost cap rule detail
# ===========================================================================


def test_cost_cap_rule_embeds_op_id_in_command():
    ctx = FailureContext(
        op_id="op-abc", stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80, cost_cap_usd=0.50,
    )
    plan = advise(ctx)
    assert plan.matched_rule == "cost_cap"
    # Top suggestion should be the /cost drill-down
    top = plan.top_suggestion()
    assert top is not None
    assert "/cost op-abc" in top.command
    assert top.priority == PRIORITY_HIGH


def test_cost_cap_summary_includes_spent_and_cap():
    ctx = FailureContext(
        op_id="op-1", stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80, cost_cap_usd=0.50,
    )
    plan = advise(ctx)
    assert "$0.8000" in plan.failure_summary
    assert "$0.5000" in plan.failure_summary


def test_cost_cap_summary_omits_cap_when_unknown():
    ctx = FailureContext(
        op_id="op-1", stop_reason=STOP_COST_CAP, cost_spent_usd=0.80,
    )
    plan = advise(ctx)
    assert "$0.8000" in plan.failure_summary
    assert "/" not in plan.failure_summary.split("at ")[1]


# ===========================================================================
# Approval rule — op_id threaded through commands
# ===========================================================================


def test_approval_required_plan_uses_op_id():
    ctx = FailureContext(
        op_id="op-xyz", stop_reason=STOP_APPROVAL_REQUIRED,
    )
    plan = advise(ctx)
    assert plan.matched_rule == "approval_required"
    top = plan.top_suggestion()
    assert top is not None
    assert top.priority == PRIORITY_CRITICAL
    assert "/plan approve op-xyz" in top.command


def test_approval_plan_without_op_id_uses_placeholder():
    ctx = FailureContext(stop_reason=STOP_APPROVAL_REQUIRED)
    plan = advise(ctx)
    top = plan.top_suggestion()
    assert top is not None
    assert "<op-id>" in top.command


# ===========================================================================
# Max suggestions clamp
# ===========================================================================


def test_max_suggestions_clamp():
    ctx = FailureContext(op_id="op-1", stop_reason=STOP_COST_CAP)
    plan = advise(ctx, max_suggestions=1)
    assert len(plan.suggestions) == 1


def test_max_suggestions_floor_is_1():
    ctx = FailureContext(op_id="op-1", stop_reason=STOP_COST_CAP)
    plan = advise(ctx, max_suggestions=0)
    assert len(plan.suggestions) == 1


def test_max_suggestions_ceiling_is_5():
    ctx = FailureContext(op_id="op-1", stop_reason=STOP_COST_CAP)
    plan = advise(ctx, max_suggestions=99)
    # Cost cap rule defines 3 suggestions so the ceiling doesn't add more
    assert len(plan.suggestions) == 3


# ===========================================================================
# Priority ordering
# ===========================================================================


def test_top_suggestion_respects_priority():
    p = RecoveryPlan(
        op_id="op-1", failure_summary="x",
        suggestions=(
            RecoverySuggestion(title="low", priority=PRIORITY_LOW),
            RecoverySuggestion(title="crit", priority=PRIORITY_CRITICAL),
            RecoverySuggestion(title="med", priority=PRIORITY_MEDIUM),
        ),
    )
    top = p.top_suggestion()
    assert top is not None
    assert top.title == "crit"


def test_top_suggestion_returns_none_for_empty_plan():
    p = RecoveryPlan(op_id="op-1", failure_summary="x")
    assert p.top_suggestion() is None


# ===========================================================================
# Session-id threading
# ===========================================================================


def test_generic_rule_uses_session_id_in_command():
    ctx = FailureContext(op_id="op-1", session_id="bt-2026")
    plan = advise(ctx)
    texts = " ".join(s.command for s in plan.suggestions)
    assert "bt-2026" in texts


def test_iron_gate_rule_uses_session_id():
    ctx = FailureContext(
        op_id="op-1", session_id="bt-xyz",
        stop_reason=STOP_IRON_GATE_REJECT,
    )
    plan = advise(ctx)
    assert any("bt-xyz" in s.command for s in plan.suggestions)


# ===========================================================================
# Project output is JSON-safe
# ===========================================================================


def test_plan_project_round_trips_json():
    ctx = FailureContext(
        op_id="op-1", stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80, cost_cap_usd=0.50,
    )
    plan = advise(ctx)
    blob = json.dumps(plan.project())
    parsed = json.loads(blob)
    assert parsed["op_id"] == "op-1"
    assert parsed["schema_version"] == "recovery_plan.v1"
    assert parsed["matched_rule"] == "cost_cap"
    assert len(parsed["suggestions"]) == 3


def test_plan_project_includes_context_projection():
    ctx = FailureContext(
        op_id="op-1", stop_reason=STOP_COST_CAP,
        exception_message="x" * 1000,
    )
    plan = advise(ctx)
    p = plan.project()
    assert p["context"]["op_id"] == "op-1"
    # Exception message truncated to 500 by FailureContext.project
    assert len(p["context"]["exception_message"]) <= 500


# ===========================================================================
# Determinism
# ===========================================================================


def test_advise_is_deterministic_same_context_same_plan():
    ctx = FailureContext(
        op_id="op-1", stop_reason=STOP_VALIDATION_EXHAUSTED,
        failure_class="test",
    )
    plan_a = advise(ctx)
    plan_b = advise(ctx)
    assert plan_a.project() == plan_b.project()


# ===========================================================================
# Plan.has_suggestions
# ===========================================================================


def test_has_suggestions_true_when_populated():
    p = RecoveryPlan(
        op_id="op-1", failure_summary="x",
        suggestions=(RecoverySuggestion(title="t"),),
    )
    assert p.has_suggestions


def test_has_suggestions_false_when_empty():
    p = RecoveryPlan(op_id="op-1", failure_summary="x")
    assert not p.has_suggestions
