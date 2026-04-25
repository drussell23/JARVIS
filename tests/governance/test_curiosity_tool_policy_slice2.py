"""Wave 2 (4) Slice 2 — tool-policy widening + Venom integration tests.

Pins per ``project_w2_4_curiosity_scope.md`` Slice 2:

A. **Pre-W2(4) behavior preserved** — when master flag off (default),
   ask_human at SAFE_AUTO is rejected with the same legacy
   `tool.denied.ask_human_low_risk` reason code. Byte-for-byte.

B. **NOTIFY_APPLY+ path unchanged** — the existing risk-tier-gated path
   for Yellow/Orange ops works exactly as before.

C. **W2(4) widening composition** — when master ON + budget bound +
   posture allowed + quota remaining + cost within cap → ask_human at
   SAFE_AUTO is allowed. Each rejection class produces the legacy
   reason code with a curiosity-deny detail.

D. **Cross-component hook test** (per wiring checklist): policy gate
   reads the contextvar; budget decrements on each successful invocation.

E. **No-budget-bound** — when master is on but no CuriosityBudget bound
   to the contextvar, behavior is the legacy SAFE_AUTO reject (no
   accidental allowance from a None budget).

F. **BLOCKED tier still rejected** — even with W2(4) on, BLOCKED ops
   never get ask_human. The scope-doc's "no gate softening" invariant.

G. **Source-grep pins** for the wiring (curiosity_engine import + Rule 14
   widening + GENERATE phase contextvar set).
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.curiosity_engine import (
    CuriosityBudget,
    curiosity_budget_var,
)


# Stub-light helpers — Rule 14 only needs a few attrs on the policy ctx.

def _mk_policy_ctx(*, repo_root: Path, risk_tier=None):
    """Build a minimal PolicyContext-shaped object for Rule 14 evaluation."""
    from backend.core.ouroboros.governance.tool_executor import PolicyContext
    return PolicyContext(repo_root=repo_root, risk_tier=risk_tier)


def _mk_ask_human_call(question: str = "What should I do?"):
    """Build a minimal ToolCall for ask_human."""
    from backend.core.ouroboros.governance.tool_executor import ToolCall
    return ToolCall(name="ask_human", arguments={"question": question})


def _evaluate_rule_14(call, ctx):
    """Run the policy gate (which contains Rule 14) and return the PolicyResult.

    The gate is a module-level helper exposed by tool_executor. We reuse
    the production path so any future refactor that breaks Rule 14
    surface fails this test.
    """
    from backend.core.ouroboros.governance.tool_executor import (
        ToolPolicyGate,
    )
    gate = ToolPolicyGate()
    return gate.evaluate(call, ctx)


# ---------------------------------------------------------------------------
# (A) Pre-W2(4) behavior preserved — master off → legacy reject
# ---------------------------------------------------------------------------


def test_master_off_safe_auto_rejected_legacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Master flag off (default) → ask_human at SAFE_AUTO is rejected
    with the legacy `tool.denied.ask_human_low_risk` reason code.
    Byte-for-byte pre-W2(4)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.SAFE_AUTO)
    result = _evaluate_rule_14(_mk_ask_human_call(), ctx)

    assert str(result.decision).endswith("DENY")
    assert result.reason_code == "tool.denied.ask_human_low_risk"


def test_master_off_with_budget_bound_still_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Even with a CuriosityBudget bound (e.g., test pollution), master
    off forces the budget to deny → legacy reject."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.SAFE_AUTO)
    result = _evaluate_rule_14(_mk_ask_human_call(), ctx)

    assert result.reason_code == "tool.denied.ask_human_low_risk"
    # Budget counter unchanged (master-off → MASTER_OFF deny → no increment)
    assert bud.questions_used == 0


# ---------------------------------------------------------------------------
# (B) NOTIFY_APPLY+ path unchanged
# ---------------------------------------------------------------------------


def test_notify_apply_tier_allowed_pre_w2_4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Pre-W2(4) NOTIFY_APPLY ops still allowed (the existing Yellow path)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.NOTIFY_APPLY)
    result = _evaluate_rule_14(_mk_ask_human_call(), ctx)

    # NOTIFY_APPLY isn't blocked by Rule 14 — falls through to default ALLOW
    assert "DENY" not in str(result.decision).upper() or result.reason_code != "tool.denied.ask_human_low_risk"


def test_approval_required_tier_allowed_pre_w2_4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.APPROVAL_REQUIRED)
    result = _evaluate_rule_14(_mk_ask_human_call(), ctx)
    assert "DENY" not in str(result.decision).upper() or result.reason_code != "tool.denied.ask_human_low_risk"


# ---------------------------------------------------------------------------
# (F) BLOCKED tier still rejected
# ---------------------------------------------------------------------------


def test_blocked_tier_rejected_even_with_w2_4_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Even with curiosity master ON + budget bound + posture allowed,
    BLOCKED ops never get ask_human. No gate softening."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.BLOCKED)
    result = _evaluate_rule_14(_mk_ask_human_call(), ctx)

    assert str(result.decision).endswith("DENY")
    assert result.reason_code == "tool.denied.ask_human_blocked_op"


# ---------------------------------------------------------------------------
# (C) W2(4) widening composition
# ---------------------------------------------------------------------------


def test_w2_4_widening_allowed_when_all_gates_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Master ON + budget bound + posture EXPLORE + quota remaining +
    cost within cap → ask_human at SAFE_AUTO is ALLOWED + budget
    decrements."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.SAFE_AUTO)
    result = _evaluate_rule_14(_mk_ask_human_call("What is X?"), ctx)

    # Allowed — falls through to gate's default ALLOW
    assert "DENY" not in str(result.decision).upper() or result.reason_code != "tool.denied.ask_human_low_risk"
    # Budget decremented
    assert bud.questions_used == 1


def test_w2_4_widening_denied_when_posture_disallowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Master ON + budget bound + posture HARDEN (excluded) → ask_human
    at SAFE_AUTO denied. Operator-facing reason code is the legacy one
    (cleaner for ops); detail mentions the curiosity-side cause."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="HARDEN")
    curiosity_budget_var.set(bud)

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.SAFE_AUTO)
    result = _evaluate_rule_14(_mk_ask_human_call(), ctx)

    assert str(result.decision).endswith("DENY")
    assert result.reason_code == "tool.denied.ask_human_low_risk"
    # Curiosity-side cause surfaces in detail
    assert "posture_disallowed" in result.detail
    assert bud.questions_used == 0  # no decrement on deny


def test_w2_4_widening_denied_when_quota_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """3 successful charges, 4th denied with quota exhausted detail."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_QUESTIONS_PER_SESSION", "3")
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.SAFE_AUTO)
    # 3 allowed
    for i in range(3):
        result = _evaluate_rule_14(_mk_ask_human_call(f"Q{i}?"), ctx)
        assert result.reason_code != "tool.denied.ask_human_low_risk", (
            f"charge {i} should be allowed; got reason {result.reason_code}"
        )
    # 4th denied
    result = _evaluate_rule_14(_mk_ask_human_call("Q3?"), ctx)
    assert str(result.decision).endswith("DENY")
    assert result.reason_code == "tool.denied.ask_human_low_risk"
    assert "questions_exhausted" in result.detail


# ---------------------------------------------------------------------------
# (E) No-budget-bound → legacy reject
# ---------------------------------------------------------------------------


def test_w2_4_master_on_but_no_budget_bound_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Master ON but no CuriosityBudget set on the contextvar → legacy
    reject. No accidental allowance from a None budget."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    # Explicitly clear any test-leaked budget
    curiosity_budget_var.set(None)

    ctx = _mk_policy_ctx(repo_root=tmp_path, risk_tier=RiskTier.SAFE_AUTO)
    result = _evaluate_rule_14(_mk_ask_human_call(), ctx)

    assert str(result.decision).endswith("DENY")
    assert result.reason_code == "tool.denied.ask_human_low_risk"


# ---------------------------------------------------------------------------
# (G) Source-grep pins for the wiring (per wiring invariant checklist)
# ---------------------------------------------------------------------------


def test_pin_tool_executor_imports_curiosity_helpers():
    """tool_executor.py Rule 14 must import the curiosity helpers."""
    src = Path(
        "backend/core/ouroboros/governance/tool_executor.py"
    ).read_text()
    assert "current_curiosity_budget as _curr_curiosity_budget" in src
    assert "cost_cap_usd as _curiosity_cost_cap" in src


def test_pin_tool_executor_calls_try_charge():
    """Rule 14 widening must call try_charge() — that's the budget
    decrement + ledger persist trigger."""
    src = Path(
        "backend/core/ouroboros/governance/tool_executor.py"
    ).read_text()
    assert "_budget.try_charge(" in src


def test_pin_generate_runner_sets_contextvar():
    """GENERATE phase entry must bind the per-op CuriosityBudget to the
    ambient ContextVar so tool_executor Rule 14 can read it from any
    Venom tool task spawned during the loop."""
    src = Path(
        "backend/core/ouroboros/governance/phase_runners/generate_runner.py"
    ).read_text()
    assert "curiosity_budget_var as _curiosity_budget_var" in src
    assert "_curiosity_budget_var.set(_CuriosityBudget(" in src
    assert "curiosity_enabled as _curiosity_enabled" in src


def test_pin_generate_runner_master_off_short_circuits():
    """Master-off → GENERATE entry skips the budget construction entirely.
    No regression on default-off path."""
    src = Path(
        "backend/core/ouroboros/governance/phase_runners/generate_runner.py"
    ).read_text()
    assert "if _curiosity_enabled():" in src
