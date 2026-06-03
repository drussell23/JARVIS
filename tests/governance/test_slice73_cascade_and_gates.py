"""Slice 73 — Structural Transport Short-Circuit + Adaptive Turn Allocation.

Empirical harvest (bt-2026-06-03-053511 debug.log) drove the re-scope:
  * DW Qwen generation = 100% ``live_transport:RuntimeError`` (transport DOWN,
    zero TTFT samples) — so a TTFT-σ cold-start predictor would be inert. The
    real waste is the route trying BOTH dead DW models (~30s each) before
    cascading, starving the Claude fallback (``deadline_exhausted_pre_fallback``).
  * 16 × ``tool_loop_starved_below_min_ttft_floor`` bails with a HEALTHY 148s
    budget — the viability gate divided remaining by ALL rounds_left
    (148/8≈18.5s < 45s floor) and bailed, truncating the model to tiny
    non-patch responses.

Phase 1 — sever the DW lane on a STRUCTURAL transport break (affects the whole
endpoint; sibling models share the dead transport) and cascade immediately.
Phase 2 — the viability gate assesses the IMMEDIATE turn against the actual
remaining budget (the round receives the full deadline post-Slice-71, NOT
per_round_timeout), not a fair-share across hypothetical future rounds.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.topology_sentinel import FailureSource
from backend.core.ouroboros.governance.candidate_generator import (
    should_sever_dw_lane,
    structural_fast_cascade_enabled,
)
from backend.core.ouroboros.governance.tool_executor import BudgetPlan


# --- Phase 1: structural transport short-circuit predicate ---

def test_severs_dw_lane_on_live_transport():
    """A transport/socket break affects the whole DW endpoint → sever, don't
    waste ~30s trying a sibling model on the same dead transport."""
    assert should_sever_dw_lane(FailureSource.LIVE_TRANSPORT) is True


def test_does_not_sever_on_model_specific_failures():
    """429 (rate limit) / 5xx / parse are model-specific — still rotate to the
    next ranked model (a sibling may be healthy)."""
    assert should_sever_dw_lane(FailureSource.LIVE_HTTP_429) is False
    assert should_sever_dw_lane(FailureSource.LIVE_HTTP_5XX) is False
    assert should_sever_dw_lane(FailureSource.LIVE_PARSE_ERROR) is False


def test_structural_fast_cascade_default_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_STRUCTURAL_FAST_CASCADE_ENABLED", raising=False)
    assert structural_fast_cascade_enabled() is True
    monkeypatch.setenv("JARVIS_DW_STRUCTURAL_FAST_CASCADE_ENABLED", "false")
    assert structural_fast_cascade_enabled() is False


# --- Phase 2: adaptive turn allocation gate ---

def _plan() -> BudgetPlan:
    # Mirrors the soak: floor 45s, ceiling 30s, up to 10 rounds.
    return BudgetPlan.build(
        total_budget_s=160.0, hard_max_rounds=10, max_per_round_s=30.0,
        min_ttft_floor_s=45.0,
    )


def test_healthy_budget_with_many_rounds_is_viable(monkeypatch):
    """The exact false-positive: 148s remaining, 8 rounds_left. Old gate bailed
    (148/8=18.5 < floor); the immediate turn easily fits → must be viable."""
    monkeypatch.delenv("JARVIS_TOOL_LOOP_ADAPTIVE_TURN_GATE_ENABLED", raising=False)
    assert _plan().is_next_round_viable(remaining_s=148.0, remaining_rounds=8) is True


def test_genuinely_starved_budget_still_bails():
    """When the WHOLE remaining budget can't fit one floor-sized turn, bail."""
    # effective_floor = min(45, max_per_round=30) = 30. remaining 20 < 30.
    assert _plan().is_next_round_viable(remaining_s=20.0, remaining_rounds=2) is False


def test_budget_exactly_at_floor_is_viable():
    # remaining == effective_floor (30) → the immediate turn fits.
    assert _plan().is_next_round_viable(remaining_s=30.0, remaining_rounds=9) is True


def test_flag_off_restores_legacy_fairshare_gate(monkeypatch):
    """Master flag off → byte-identical legacy behavior (fair-share bail)."""
    monkeypatch.setenv("JARVIS_TOOL_LOOP_ADAPTIVE_TURN_GATE_ENABLED", "false")
    # Legacy: per_round_timeout(148,8)=min(30,18.5)=18.5 < floor 30 → bail.
    assert _plan().is_next_round_viable(remaining_s=148.0, remaining_rounds=8) is False


# --- AST-pins: both fixes wired into the live paths ---

def test_candidate_generator_severs_on_structural_failure():
    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/governance/candidate_generator.py").read_text()
    assert "should_sever_dw_lane(" in src, "dispatch loop must consult the sever predicate"
    # The break must be present in the dispatch loop (not just continue).
    assert "structural_fast_cascade_enabled()" in src


def test_tool_executor_uses_adaptive_turn_gate():
    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/governance/tool_executor.py").read_text()
    assert "_adaptive_turn_gate_enabled" in src
