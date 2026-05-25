"""Slice 3B.1 — Self-tuning TTFT floor constraint.

Slice 3B's soak bt-2026-05-25-015301 proved the Layer 1 gate works
(no more stream rupture mid-flight) but revealed the gate OVER-FIRES
under plentiful budget:

  remaining=328.59s rounds_left=9 projected_per_round=30.00s
    min_ttft_floor=45.00s — bailing pre-call

With 328s of wall budget remaining (ample!), fair_share = 36.5s
which CLAMPS to ``max_per_round_s = 30s``. The 30s ceiling is BELOW
the 45s floor → gate fires every time, regardless of budget health.

# Root cause

``is_next_round_viable`` checked ``per_round_timeout < floor``. But
``per_round_timeout`` returns ``min(max_per_round_s, fair_share)`` —
when ``max_per_round_s`` is the dominant clamp (ample budget), the
check fires even though the squeeze isn't real.

# Fix — self-tuning floor

The floor must be capped at ``max_per_round_s``. If the operator has
configured ``max_per_round_s = 30s``, that IS the effective ceiling
on per-round duration — the floor only matters when fair_share
squeezes BELOW that ceiling.

Effective floor: ``min(min_ttft_floor_s, max_per_round_s)``.

Equivalent gate predicate: gate fires iff
``per_round_timeout < min(min_ttft_floor_s, max_per_round_s)``.

  * Ample budget at ceiling (per_round = max = 30s,
    floor = 45s):  effective_floor = min(45, 30) = 30; 30 >= 30 → OK
  * Tight budget below ceiling (per_round = fair_share = 5s,
    floor = 45s, max = 30s): effective_floor = 30; 5 < 30 → gate fires
  * Tight budget above ceiling (per_round = max = 30s,
    fair_share = 200s, floor = 45s): same as ample budget — OK
  * Operator-bumped ceiling (max = 60s, fair_share = 200s,
    floor = 45s): effective_floor = min(45, 60) = 45; 60 >= 45 → OK

Self-tuning: no env knob, no hardcoding. Adapts to operator's
``max_per_round_s`` (env: ``JARVIS_GOVERNED_TOOL_TIMEOUT_S``).

# Test surface (1 AST pin + 5 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_EXECUTOR_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "tool_executor.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PIN — is_next_round_viable references max_per_round_s
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_is_next_round_viable_caps_floor_at_max_per_round() -> None:
    """``BudgetPlan.is_next_round_viable`` body must reference BOTH
    ``min_ttft_floor_s`` AND ``max_per_round_s`` — the self-tuning
    invariant requires the floor to be capped at the operator's
    configured max. Without this, the gate over-fires when the
    operator's max_per_round_s is below the global floor."""
    tree = _parse(TOOL_EXECUTOR_FILE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "BudgetPlan":
            continue
        for sub in node.body:
            if not isinstance(sub, ast.FunctionDef):
                continue
            if sub.name != "is_next_round_viable":
                continue
            body_src = ast.unparse(sub)
            assert "min_ttft_floor_s" in body_src, (
                "is_next_round_viable does not reference min_ttft_floor_s"
            )
            assert "max_per_round_s" in body_src, (
                "is_next_round_viable does not reference max_per_round_s — "
                "Slice 3B.1's self-tuning invariant requires capping the "
                "floor at max_per_round_s. Without this, the gate fires "
                "under ample budget at the ceiling (the trap surfaced "
                "by soak bt-2026-05-25-015301)."
            )
            return
    pytest.fail("BudgetPlan.is_next_round_viable not found")


# ──────────────────────────────────────────────────────────────────────
# Spine — the soak's failure mode is now PASSING
# ──────────────────────────────────────────────────────────────────────

def test_spine_ample_budget_at_max_per_round_ceiling_is_viable() -> None:
    """The EXACT soak condition: budget=328s, rounds_left=9,
    max_per_round_s=30s (default), min_ttft_floor=45s.

    fair_share = 328/9 = 36.4s; clamps to max=30s.
    Effective floor = min(45, 30) = 30s.
    30 >= 30 → viable=True.

    This is the regression test for the over-fire trap."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=358.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,  # legacy default
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    # Pre-Slice-3B.1: this returned False (the soak's failure)
    # Post-Slice-3B.1: returns True (operator's max ceiling is honored)
    assert plan.is_next_round_viable(remaining_s=328.0, remaining_rounds=9) is True


def test_spine_tight_budget_below_ceiling_still_starves() -> None:
    """Genuine starvation: budget=30s, rounds_left=10, max=30s,
    floor=45s. fair_share = 2s; clamps to min=3s. 3 < 30 → gate fires.

    This is the original Slice 3B target — preserved post-3B.1."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=300.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    # fair_share = 20/10 = 2s, clamps to 3s
    # effective_floor = min(45, 30) = 30
    # 3 < 30 → starved
    assert plan.is_next_round_viable(remaining_s=30.0, remaining_rounds=10) is False


def test_spine_operator_bumped_ceiling_above_floor() -> None:
    """Operator sets max=60s (above floor=45). With ample budget,
    per_round = max = 60s. effective_floor = min(45, 60) = 45.
    60 >= 45 → viable."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=600.0,
        hard_max_rounds=10,
        max_per_round_s=60.0,  # operator-bumped
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    assert plan.is_next_round_viable(remaining_s=600.0, remaining_rounds=10) is True


def test_spine_soak_starvation_at_5s_still_caught() -> None:
    """The ORIGINAL Slice 3B soak's failure mode (bt-2026-05-25-012206):
    budget=61.6s, rounds_left=10. fair_share=5.16s, max=30s, floor=45s.
    effective_floor = min(45, 30) = 30. 5.16 < 30 → starved.

    Slice 3B.1 doesn't regress Slice 3B's win — genuine sub-floor
    starvation still gates correctly."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=358.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    assert plan.is_next_round_viable(remaining_s=61.6, remaining_rounds=10) is False


def test_spine_legacy_min_per_round_gate_preserved() -> None:
    """The legacy unclamped-fair-share < min_per_round_s gate (Condition 1
    of is_next_round_viable) must STILL work — Slice 3B.1 only refines
    Condition 2 (the TTFT floor)."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=100.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=10.0,  # high min — easy to starve
        final_write_reserve_s=10.0,
    )
    # remaining=20, rounds=10: unclamped_fair_share = 10/10 = 1s.
    # 1 < min_per_round_s=10 → legacy condition 1 starves.
    assert plan.is_next_round_viable(remaining_s=20.0, remaining_rounds=10) is False
