"""Tests for CostGovernor phase instrumentation (Slice 2).

The CRITICAL pin is at the bottom:
``test_phase_tagging_does_not_alter_budget_behavior`` — proves that
charges without a phase still obey the pre-Slice-2 budget cap path
byte-for-byte.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
)
from backend.core.ouroboros.governance.phase_cost import (
    PhaseCostBreakdown,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _governor() -> CostGovernor:
    return CostGovernor(
        config=CostGovernorConfig(
            enabled=True,
            baseline_usd=0.10,
            max_cap_usd=100.0,
            min_cap_usd=0.01,
            readonly_factor=5.0,
        ),
    )


# ===========================================================================
# Backward compatibility — charge() without phase works unchanged
# ===========================================================================


def test_charge_without_phase_still_works():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    cumulative = g.charge("op-1", 0.05, "claude")
    assert cumulative == pytest.approx(0.05)


def test_charge_without_phase_populates_unknown_phase_bucket():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.05, "claude")
    summary = g.summary("op-1")
    assert summary["unknown_phase_usd"] == pytest.approx(0.05)
    assert summary["phase_totals"] == {}


# ===========================================================================
# Phase tagging — per-phase accounting
# ===========================================================================


def test_charge_with_phase_populates_phase_totals():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude", phase="GENERATE")
    g.charge("op-1", 0.05, "claude", phase="VERIFY")
    summary = g.summary("op-1")
    assert summary["phase_totals"] == {
        "GENERATE": pytest.approx(0.10),
        "VERIFY": pytest.approx(0.05),
    }


def test_charge_same_phase_multiple_times_accumulates():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.03, "claude", phase="GENERATE")
    g.charge("op-1", 0.07, "claude", phase="GENERATE")
    summary = g.summary("op-1")
    assert summary["phase_totals"]["GENERATE"] == pytest.approx(0.10)


def test_phase_by_provider_matrix_built():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.05, "claude", phase="GENERATE")
    g.charge("op-1", 0.02, "doubleword", phase="GENERATE")
    g.charge("op-1", 0.03, "claude", phase="VALIDATE")
    summary = g.summary("op-1")
    assert summary["phase_by_provider"] == {
        "GENERATE": {
            "claude": pytest.approx(0.05),
            "doubleword": pytest.approx(0.02),
        },
        "VALIDATE": {"claude": pytest.approx(0.03)},
    }


def test_mixed_phased_and_unphased_charges_coexist():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude", phase="GENERATE")
    g.charge("op-1", 0.05, "claude")  # no phase
    summary = g.summary("op-1")
    assert summary["phase_totals"] == {"GENERATE": pytest.approx(0.10)}
    assert summary["unknown_phase_usd"] == pytest.approx(0.05)
    # cumulative + per-provider totals still see both
    assert summary["cumulative_usd"] == pytest.approx(0.15)
    assert summary["provider_totals"]["claude"] == pytest.approx(0.15)


def test_empty_phase_string_treated_as_unphased():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.05, "claude", phase="")
    summary = g.summary("op-1")
    assert summary["phase_totals"] == {}
    assert summary["unknown_phase_usd"] == pytest.approx(0.05)


def test_whitespace_phase_treated_as_unphased():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.05, "claude", phase="   ")
    summary = g.summary("op-1")
    assert summary["phase_totals"] == {}


def test_none_phase_treated_as_unphased():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.05, "claude", phase=None)
    summary = g.summary("op-1")
    assert summary["unknown_phase_usd"] == pytest.approx(0.05)


# ===========================================================================
# get_phase_breakdown — projection
# ===========================================================================


def test_get_phase_breakdown_returns_breakdown():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.40, "claude", phase="GENERATE")
    g.charge("op-1", 0.20, "claude", phase="VERIFY")
    b = g.get_phase_breakdown("op-1")
    assert isinstance(b, PhaseCostBreakdown)
    assert b.op_id == "op-1"
    assert b.total_usd == pytest.approx(0.60)
    assert b.by_phase == {
        "GENERATE": pytest.approx(0.40),
        "VERIFY": pytest.approx(0.20),
    }
    assert b.call_count == 2


def test_get_phase_breakdown_returns_none_for_unknown_op():
    g = _governor()
    assert g.get_phase_breakdown("op-ghost") is None


def test_get_phase_breakdown_after_finish_returns_none():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude", phase="GENERATE")
    g.finish("op-1")
    assert g.get_phase_breakdown("op-1") is None


def test_get_phase_breakdown_is_snapshot_not_destructive():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude", phase="GENERATE")
    b1 = g.get_phase_breakdown("op-1")
    b2 = g.get_phase_breakdown("op-1")
    assert b1 == b2
    # Charging after the snapshot still works — entry wasn't removed.
    g.charge("op-1", 0.05, "claude", phase="VERIFY")
    b3 = g.get_phase_breakdown("op-1")
    assert b3.total_usd == pytest.approx(0.15)


def test_get_phase_breakdown_includes_unknown_phase_bucket():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude", phase="GENERATE")
    g.charge("op-1", 0.05, "claude")  # no phase
    b = g.get_phase_breakdown("op-1")
    assert b.total_usd == pytest.approx(0.15)
    assert b.unknown_phase_usd == pytest.approx(0.05)


# ===========================================================================
# snapshot_all_phase_breakdowns
# ===========================================================================


def test_snapshot_all_breakdowns_across_ops():
    g = _governor()
    g.start("op-a", route="standard", complexity="light")
    g.start("op-b", route="complex", complexity="heavy_code")
    g.charge("op-a", 0.10, "claude", phase="GENERATE")
    g.charge("op-b", 0.50, "claude", phase="GENERATE")
    all_ = g.snapshot_all_phase_breakdowns()
    assert set(all_.keys()) == {"op-a", "op-b"}
    assert all_["op-a"].total_usd == pytest.approx(0.10)
    assert all_["op-b"].total_usd == pytest.approx(0.50)


def test_snapshot_all_breakdowns_empty_when_no_ops():
    g = _governor()
    assert g.snapshot_all_phase_breakdowns() == {}


def test_snapshot_all_breakdowns_empty_when_disabled():
    g = CostGovernor(config=CostGovernorConfig(enabled=False))
    assert g.snapshot_all_phase_breakdowns() == {}


# ===========================================================================
# finish() summary carries phase data
# ===========================================================================


def test_finish_summary_includes_phase_data():
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude", phase="GENERATE")
    summary = g.finish("op-1")
    assert summary is not None
    assert summary["phase_totals"] == {"GENERATE": pytest.approx(0.10)}
    assert summary["phase_by_provider"] == {
        "GENERATE": {"claude": pytest.approx(0.10)},
    }


# ===========================================================================
# Disabled governor — phase kwarg is accepted but does nothing
# ===========================================================================


def test_disabled_governor_accepts_phase_kwarg():
    g = CostGovernor(config=CostGovernorConfig(enabled=False))
    result = g.charge("op-1", 0.10, "claude", phase="GENERATE")
    assert result == 0.0
    assert g.summary("op-1") is None


# ===========================================================================
# CRITICAL PIN — budget cap behavior unchanged when phase is omitted
# ===========================================================================


def test_phase_tagging_does_not_alter_budget_behavior():
    """Two parallel governors: one charges with phase, one without.
    Both must land in the same cumulative + exceeded state given
    identical amounts.
    """
    g_phased = _governor()
    g_unphased = _governor()
    # Config with a small cap to force exceeded.
    g_phased.start("op-1", route="standard", complexity="light")
    g_unphased.start("op-1", route="standard", complexity="light")

    charges = [(0.05, "claude"), (0.10, "claude"), (0.25, "doubleword")]
    for amount, provider in charges:
        g_phased.charge("op-1", amount, provider, phase="GENERATE")
        g_unphased.charge("op-1", amount, provider)

    s_phased = g_phased.summary("op-1")
    s_unphased = g_unphased.summary("op-1")
    # Budget-relevant fields MUST be identical.
    assert s_phased["cumulative_usd"] == s_unphased["cumulative_usd"]
    assert s_phased["cap_usd"] == s_unphased["cap_usd"]
    assert s_phased["remaining_usd"] == s_unphased["remaining_usd"]
    assert s_phased["call_count"] == s_unphased["call_count"]
    assert s_phased["exceeded"] == s_unphased["exceeded"]
    assert s_phased["provider_totals"] == s_unphased["provider_totals"]
    # The only difference: phase_totals populated vs empty.
    assert s_phased["phase_totals"] != s_unphased["phase_totals"]
    assert s_phased["unknown_phase_usd"] == pytest.approx(0.0)
    assert s_unphased["unknown_phase_usd"] == pytest.approx(
        s_unphased["cumulative_usd"]
    )


def test_budget_exceeded_fires_at_same_cumulative_with_or_without_phase():
    """Budget cap trip must happen at the same cumulative regardless
    of whether charges carry a phase tag."""
    # Force a tiny cap by setting min_cap to 0.05.
    cfg = CostGovernorConfig(
        enabled=True, baseline_usd=0.05,
        max_cap_usd=0.05, min_cap_usd=0.05,
    )
    g1 = CostGovernor(cfg)
    g2 = CostGovernor(cfg)
    g1.start("op-1", route="standard", complexity="light")
    g2.start("op-1", route="standard", complexity="light")
    # Both drive cumulative over 0.05.
    g1.charge("op-1", 0.06, "claude", phase="GENERATE")
    g2.charge("op-1", 0.06, "claude")
    assert g1.is_exceeded("op-1") == g2.is_exceeded("op-1") is True
