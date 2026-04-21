"""Tests for phase_cost (Slice 1)."""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.phase_cost import (
    CANONICAL_PHASE_ORDER,
    PHASE_COST_SCHEMA_VERSION,
    PhaseCostBreakdown,
    PhaseCostEntry,
    aggregate_entries,
    breakdown_from_mappings,
    render_phase_cost_breakdown,
)


# ===========================================================================
# Schema + vocabulary
# ===========================================================================


def test_schema_version_pinned():
    assert PHASE_COST_SCHEMA_VERSION == "phase_cost.v1"


def test_canonical_phase_order_is_tuple():
    assert isinstance(CANONICAL_PHASE_ORDER, tuple)
    assert "GENERATE" in CANONICAL_PHASE_ORDER
    assert "VERIFY" in CANONICAL_PHASE_ORDER
    assert "CLASSIFY" in CANONICAL_PHASE_ORDER
    # Order matters — CLASSIFY comes before GENERATE
    assert (
        CANONICAL_PHASE_ORDER.index("CLASSIFY")
        < CANONICAL_PHASE_ORDER.index("GENERATE")
    )


# ===========================================================================
# PhaseCostEntry frozen value type
# ===========================================================================


def test_phase_cost_entry_frozen():
    e = PhaseCostEntry(
        op_id="op-1", phase="GENERATE", provider="claude",
        amount_usd=0.10,
    )
    with pytest.raises((AttributeError, TypeError)):
        e.amount_usd = 999.0  # type: ignore[misc]


def test_phase_cost_entry_project_is_json_safe():
    e = PhaseCostEntry(
        op_id="op-1", phase="GENERATE", provider="claude",
        amount_usd=0.10, timestamp_mono=12345.678,
    )
    blob = json.dumps(e.project())
    assert "op-1" in blob
    assert "GENERATE" in blob


# ===========================================================================
# PhaseCostBreakdown
# ===========================================================================


def test_breakdown_frozen():
    b = PhaseCostBreakdown(op_id="op-1", total_usd=0.0)
    with pytest.raises((AttributeError, TypeError)):
        b.total_usd = 999.0  # type: ignore[misc]


def test_breakdown_empty_has_no_data():
    b = PhaseCostBreakdown(op_id="op-1", total_usd=0.0)
    assert not b.has_data


def test_breakdown_with_total_has_data():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=0.5, call_count=3,
        by_phase={"GENERATE": 0.5},
    )
    assert b.has_data


def test_breakdown_schema_version_pinned():
    b = PhaseCostBreakdown(op_id="op-1", total_usd=0.0)
    assert b.schema_version == "phase_cost.v1"


def test_breakdown_project_is_json_safe():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=0.8234,
        by_phase={"GENERATE": 0.45, "VERIFY": 0.37},
        by_provider={"claude": 0.8},
        by_phase_provider={"GENERATE": {"claude": 0.45}},
        call_count=12,
    )
    blob = json.dumps(b.project())
    parsed = json.loads(blob)
    assert parsed["total_usd"] == 0.8234
    assert parsed["by_phase"]["GENERATE"] == 0.45
    assert parsed["schema_version"] == "phase_cost.v1"


def test_breakdown_top_phase_returns_max():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=1.0,
        by_phase={"GENERATE": 0.80, "VERIFY": 0.20},
    )
    top = b.top_phase()
    assert top == ("GENERATE", 0.80)


def test_breakdown_top_phase_returns_none_when_empty():
    b = PhaseCostBreakdown(op_id="op-1", total_usd=0.0)
    assert b.top_phase() is None


def test_breakdown_top_phase_returns_none_when_all_zero():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=0.0, by_phase={"GENERATE": 0.0},
    )
    assert b.top_phase() is None


# ===========================================================================
# aggregate_entries — pure rollup function
# ===========================================================================


def test_aggregate_empty_list_returns_empty_breakdown():
    b = aggregate_entries("op-1", [])
    assert b.total_usd == 0.0
    assert b.by_phase == {}
    assert b.call_count == 0


def test_aggregate_multiple_phases_separates_buckets():
    entries = [
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="claude",
            amount_usd=0.50,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="VALIDATE", provider="claude",
            amount_usd=0.20,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="VERIFY", provider="doubleword",
            amount_usd=0.15,
        ),
    ]
    b = aggregate_entries("op-1", entries)
    assert b.total_usd == 0.85
    assert b.by_phase == {
        "GENERATE": 0.50, "VALIDATE": 0.20, "VERIFY": 0.15,
    }
    assert b.by_provider == {"claude": 0.70, "doubleword": 0.15}
    assert b.call_count == 3


def test_aggregate_entries_for_other_op_ids_are_filtered():
    entries = [
        PhaseCostEntry(
            op_id="op-a", phase="GENERATE", provider="claude",
            amount_usd=0.30,
        ),
        PhaseCostEntry(
            op_id="op-b", phase="GENERATE", provider="claude",
            amount_usd=0.20,
        ),
    ]
    b = aggregate_entries("op-a", entries)
    assert b.total_usd == 0.30
    assert b.by_phase == {"GENERATE": 0.30}


def test_aggregate_drops_zero_and_negative_amounts():
    entries = [
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="claude",
            amount_usd=0.10,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="claude",
            amount_usd=0.0,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="claude",
            amount_usd=-0.05,
        ),
    ]
    b = aggregate_entries("op-1", entries)
    assert b.call_count == 1
    assert b.total_usd == 0.10


def test_aggregate_unknown_phase_goes_to_unknown_bucket():
    entries = [
        PhaseCostEntry(
            op_id="op-1", phase="", provider="claude",
            amount_usd=0.20,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="claude",
            amount_usd=0.30,
        ),
    ]
    b = aggregate_entries("op-1", entries)
    assert b.total_usd == 0.50
    assert b.unknown_phase_usd == 0.20
    assert b.by_phase == {"GENERATE": 0.30}
    # by_provider still includes the untagged spend
    assert b.by_provider["claude"] == 0.50


def test_aggregate_populates_phase_by_provider_matrix():
    entries = [
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="claude",
            amount_usd=0.40,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="doubleword",
            amount_usd=0.05,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="VALIDATE", provider="claude",
            amount_usd=0.20,
        ),
    ]
    b = aggregate_entries("op-1", entries)
    assert b.by_phase_provider == {
        "GENERATE": {"claude": 0.40, "doubleword": 0.05},
        "VALIDATE": {"claude": 0.20},
    }


def test_aggregate_missing_provider_coerces_to_unknown():
    entries = [
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="",
            amount_usd=0.10,
        ),
    ]
    b = aggregate_entries("op-1", entries)
    assert b.by_provider == {"unknown": 0.10}


# ===========================================================================
# breakdown_from_mappings — governor projection
# ===========================================================================


def test_breakdown_from_mappings_matches_aggregate():
    """Mappings path should produce the same result as the entries path."""
    phase_totals = {"GENERATE": 0.50, "VALIDATE": 0.20}
    phase_by_provider = {
        "GENERATE": {"claude": 0.50},
        "VALIDATE": {"claude": 0.15, "doubleword": 0.05},
    }
    b = breakdown_from_mappings(
        "op-1", phase_totals, phase_by_provider,
        call_count=3,
    )
    assert b.total_usd == 0.70
    assert b.by_phase == {"GENERATE": 0.50, "VALIDATE": 0.20}
    assert b.by_provider == {"claude": 0.65, "doubleword": 0.05}
    assert b.call_count == 3


def test_breakdown_from_mappings_with_unknown_phase():
    b = breakdown_from_mappings(
        "op-1",
        phase_totals={"GENERATE": 0.30},
        phase_by_provider={"GENERATE": {"claude": 0.30}},
        call_count=2,
        unknown_phase_usd=0.10,
    )
    assert b.total_usd == 0.40
    assert b.unknown_phase_usd == 0.10


def test_breakdown_from_mappings_handles_empty():
    b = breakdown_from_mappings("op-1", {}, {})
    assert b.total_usd == 0.0
    assert b.by_phase == {}


def test_breakdown_from_mappings_filters_zero_totals():
    b = breakdown_from_mappings(
        "op-1",
        phase_totals={"GENERATE": 0.30, "VALIDATE": 0.0},
        phase_by_provider={},
    )
    assert "VALIDATE" not in b.by_phase
    assert b.by_phase == {"GENERATE": 0.30}


# ===========================================================================
# render_phase_cost_breakdown
# ===========================================================================


def test_render_empty_breakdown_mentions_no_data():
    b = PhaseCostBreakdown(op_id="op-empty", total_usd=0.0)
    out = render_phase_cost_breakdown(b)
    assert "op-empty" in out
    assert "no cost data" in out.lower()


def test_render_shows_total_and_call_count():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=0.8234, call_count=12,
        by_phase={"GENERATE": 0.80},
    )
    out = render_phase_cost_breakdown(b)
    assert "$0.8234" in out
    assert "calls=12" in out


def test_render_orders_phases_by_canonical_pipeline():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=1.0,
        by_phase={
            "VERIFY": 0.10, "GENERATE": 0.50,
            "CLASSIFY": 0.05, "VALIDATE": 0.35,
        },
    )
    out = render_phase_cost_breakdown(b)
    # CLASSIFY appears before GENERATE appears before VALIDATE
    # appears before VERIFY in canonical order.
    pos_classify = out.find("CLASSIFY")
    pos_generate = out.find("GENERATE")
    pos_validate = out.find("VALIDATE")
    pos_verify = out.find("VERIFY")
    assert pos_classify < pos_generate < pos_validate < pos_verify


def test_render_includes_provider_detail_by_default():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=0.55,
        by_phase={"GENERATE": 0.55},
        by_phase_provider={
            "GENERATE": {"claude": 0.50, "doubleword": 0.05},
        },
    )
    out = render_phase_cost_breakdown(b)
    assert "claude" in out
    assert "doubleword" in out


def test_render_can_suppress_provider_detail():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=0.55,
        by_phase={"GENERATE": 0.55},
        by_phase_provider={"GENERATE": {"claude": 0.55}},
    )
    out = render_phase_cost_breakdown(
        b, include_provider_detail=False,
    )
    # "by phase" block lists phases without provider annotation.
    # Check that claude doesn't appear inside the by-phase line
    # (it still shows in "by provider" summary below).
    lines = out.splitlines()
    generate_line = next(l for l in lines if "GENERATE" in l)
    assert "claude" not in generate_line


def test_render_includes_top_phase_footer():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=1.0,
        by_phase={"GENERATE": 0.80, "VERIFY": 0.20},
    )
    out = render_phase_cost_breakdown(b)
    assert "top phase" in out
    assert "GENERATE" in out
    assert "80.0%" in out  # 0.80 / 1.00 * 100


def test_render_surfaces_untagged_spend():
    b = PhaseCostBreakdown(
        op_id="op-1", total_usd=0.50,
        by_phase={"GENERATE": 0.40},
        unknown_phase_usd=0.10,
    )
    out = render_phase_cost_breakdown(b)
    assert "untagged" in out.lower() or "no phase" in out.lower()
    assert "$0.1000" in out


# ===========================================================================
# Determinism
# ===========================================================================


def test_aggregate_is_deterministic():
    entries = [
        PhaseCostEntry(
            op_id="op-1", phase="GENERATE", provider="claude",
            amount_usd=0.30,
        ),
        PhaseCostEntry(
            op_id="op-1", phase="VALIDATE", provider="doubleword",
            amount_usd=0.15,
        ),
    ]
    b1 = aggregate_entries("op-1", entries)
    b2 = aggregate_entries("op-1", entries)
    assert b1.project() == b2.project()
