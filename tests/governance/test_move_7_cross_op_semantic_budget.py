"""Move 7 — Cross-op Semantic Budget Slice 1 regression spine
(PRD §29.4, 2026-05-05).

Verifies:

  * 5-value `SemanticBudgetVerdict` closed taxonomy
  * Master flag default-FALSE per §33.1 graduation contract
  * Pure-stdlib cosine_distance correctness (4-corner cases)
  * Verdict ladder: DISABLED / INSUFFICIENT_DATA / WITHIN_BUDGET
    / APPROACHING / EXCEEDED
  * Frozen `OpSemanticCentroid` adopts §33.5 versioned-artifact
    contract (round-trip + defensive parse)
  * Frozen `SemanticBudgetReport` schema + `to_dict()` projection
  * Pure-function semantics (no I/O when caller supplies inputs)
  * Defensive paths — malformed centroids skipped, NEVER raises
  * 3 AST pins auto-registered + green
  * Authority asymmetry — pure substrate
  * Public API stability
"""
from __future__ import annotations

from typing import Tuple

import pytest


# ---------------------------------------------------------------------------
# Closed-enum taxonomy
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_is_closed_5_values():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict,
    )
    expected = {
        "within_budget", "approaching", "exceeded",
        "insufficient_data", "disabled",
    }
    assert {v.value for v in SemanticBudgetVerdict} == expected
    assert len(SemanticBudgetVerdict) == 5


# ---------------------------------------------------------------------------
# Master flag — default-FALSE per §33.1
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cross_op_semantic_budget_enabled,
    )
    assert cross_op_semantic_budget_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", v,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cross_op_semantic_budget_enabled,
    )
    assert cross_op_semantic_budget_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off"])
def test_master_flag_falsy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", v,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cross_op_semantic_budget_enabled,
    )
    assert cross_op_semantic_budget_enabled() is False


# ---------------------------------------------------------------------------
# cosine_distance — pure math correctness
# ---------------------------------------------------------------------------


def test_cosine_distance_identical_zero():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cosine_distance,
    )
    assert cosine_distance([1.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_distance([3.0, 4.0], [3.0, 4.0]) == 0.0


def test_cosine_distance_orthogonal_one():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cosine_distance,
    )
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == 1.0


def test_cosine_distance_opposite_two():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cosine_distance,
    )
    assert cosine_distance([1.0, 0.0], [-1.0, 0.0]) == 2.0


def test_cosine_distance_empty_returns_zero():
    """Empty vectors → 0.0 (no movement, NOT NaN)."""
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cosine_distance,
    )
    assert cosine_distance([], []) == 0.0
    assert cosine_distance([1.0], []) == 0.0
    assert cosine_distance([], [1.0]) == 0.0


def test_cosine_distance_zero_vector_returns_zero():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cosine_distance,
    )
    assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_distance_unequal_lengths_uses_min():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        cosine_distance,
    )
    # Should compare first 2 components; result is identity → 0
    assert (
        cosine_distance([1.0, 0.0, 9.9], [1.0, 0.0]) == 0.0
    )


# ---------------------------------------------------------------------------
# Verdict ladder
# ---------------------------------------------------------------------------


def _make_centroid(op_id: str, *coords: float):
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        OpSemanticCentroid,
    )
    return OpSemanticCentroid(
        op_id=op_id, ts_unix=1.0, centroid=tuple(coords),
    )


def test_verdict_disabled_when_master_off():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    r = compute_semantic_budget([], enabled_override=False)
    assert r.verdict == SemanticBudgetVerdict.DISABLED


def test_verdict_insufficient_with_zero_centroids():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    r = compute_semantic_budget([], enabled_override=True)
    assert r.verdict == SemanticBudgetVerdict.INSUFFICIENT_DATA


def test_verdict_insufficient_with_one_centroid():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    r = compute_semantic_budget(
        [_make_centroid("1", 1.0, 0.0)],
        enabled_override=True,
    )
    assert r.verdict == SemanticBudgetVerdict.INSUFFICIENT_DATA


def test_verdict_within_budget_small_drift():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    centroids = [
        _make_centroid("1", 1.0, 0.0),
        _make_centroid("2", 0.99, 0.14),  # ~1% drift
    ]
    r = compute_semantic_budget(
        centroids, enabled_override=True, threshold=0.30,
    )
    assert r.verdict == SemanticBudgetVerdict.WITHIN_BUDGET
    assert r.integrated_drift < 0.30


def test_verdict_exceeded_orthogonal_jump():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    centroids = [
        _make_centroid("1", 1.0, 0.0),
        _make_centroid("2", 0.0, 1.0),  # 90° rotation
    ]
    r = compute_semantic_budget(
        centroids, enabled_override=True, threshold=0.30,
    )
    assert r.verdict == SemanticBudgetVerdict.EXCEEDED
    assert r.integrated_drift >= r.threshold


def test_verdict_approaching_in_warning_band():
    """drift in [threshold * approaching_ratio, threshold)."""
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    # threshold=0.30, ratio=0.8 → band starts at 0.24
    # Build centroids that integrate to ~0.27.
    import math as _math
    angle = _math.acos(1.0 - 0.27)  # cos⁻¹(0.73) ≈ 43°
    centroids = [
        _make_centroid("1", 1.0, 0.0),
        _make_centroid(
            "2", _math.cos(angle), _math.sin(angle),
        ),
    ]
    r = compute_semantic_budget(
        centroids,
        enabled_override=True,
        threshold=0.30,
        approaching_band_ratio=0.8,
    )
    assert r.verdict == SemanticBudgetVerdict.APPROACHING
    assert 0.24 <= r.integrated_drift < 0.30


def test_integrated_drift_sums_per_op_deltas():
    """Window of 4 → 3 deltas; sum equals integrated_drift."""
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        compute_semantic_budget,
    )
    centroids = [
        _make_centroid("1", 1.0, 0.0),
        _make_centroid("2", 0.95, 0.31),
        _make_centroid("3", 0.85, 0.53),
        _make_centroid("4", 0.7, 0.71),
    ]
    r = compute_semantic_budget(
        centroids, enabled_override=True, threshold=1.0,
    )
    assert len(r.per_op_deltas) == 3
    assert (
        abs(r.integrated_drift - sum(r.per_op_deltas)) < 1e-9
    )


# ---------------------------------------------------------------------------
# OpSemanticCentroid §33.5 contract
# ---------------------------------------------------------------------------


def test_op_semantic_centroid_has_schema_version():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        OP_SEMANTIC_CENTROID_SCHEMA_VERSION,
        OpSemanticCentroid,
    )
    c = _make_centroid("op1", 1.0, 0.0)
    assert c.schema_version == "op_semantic_centroid.1"
    assert (
        OP_SEMANTIC_CENTROID_SCHEMA_VERSION
        == "op_semantic_centroid.1"
    )


def test_op_semantic_centroid_round_trip():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        OpSemanticCentroid,
    )
    c = OpSemanticCentroid(
        op_id="op_x", ts_unix=12345.6,
        centroid=(0.1, 0.2, 0.3),
        centroid_hash="abc12345",
    )
    d = c.to_dict()
    c2 = OpSemanticCentroid.from_dict(d)
    assert c == c2


def test_op_semantic_centroid_from_dict_defensive():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        OpSemanticCentroid,
    )
    assert OpSemanticCentroid.from_dict("nope") is None  # type: ignore
    assert OpSemanticCentroid.from_dict(None) is None  # type: ignore
    # Missing fields should still parse (defaults).
    c = OpSemanticCentroid.from_dict({"op_id": "x"})
    assert c is not None
    assert c.op_id == "x"


def test_op_semantic_centroid_filters_non_numeric():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        OpSemanticCentroid,
    )
    raw = {
        "op_id": "x",
        "ts_unix": 1.0,
        "centroid": [0.5, "garbage", 0.7, None, 1.0],
    }
    c = OpSemanticCentroid.from_dict(raw)
    assert c is not None
    assert c.centroid == (0.5, 0.7, 1.0)


# ---------------------------------------------------------------------------
# SemanticBudgetReport projection
# ---------------------------------------------------------------------------


def test_report_is_frozen():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetReport, SemanticBudgetVerdict,
    )
    r = SemanticBudgetReport(
        verdict=SemanticBudgetVerdict.WITHIN_BUDGET,
        integrated_drift=0.1, threshold=0.3,
        approaching_band=0.24, window_size=2,
        centroids_seen=2,
    )
    with pytest.raises(Exception):
        r.integrated_drift = 0.5  # type: ignore


def test_report_to_dict_projection():
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        CROSS_OP_SEMANTIC_BUDGET_SCHEMA_VERSION,
        compute_semantic_budget,
    )
    r = compute_semantic_budget(
        [_make_centroid("1", 1.0, 0.0)],
        enabled_override=True,
    )
    d = r.to_dict()
    assert (
        d["schema_version"]
        == CROSS_OP_SEMANTIC_BUDGET_SCHEMA_VERSION
    )
    assert d["verdict"] == "insufficient_data"
    assert "elapsed_s" in d


# ---------------------------------------------------------------------------
# Defensive — malformed inputs never raise
# ---------------------------------------------------------------------------


def test_compute_handles_malformed_centroid_in_window():
    """Garbage centroid in middle of window → skip the pair,
    not crash."""
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        compute_semantic_budget, OpSemanticCentroid,
    )
    centroids: Tuple[OpSemanticCentroid, ...] = (
        _make_centroid("1", 1.0, 0.0),
        OpSemanticCentroid(
            op_id="2", ts_unix=1.0, centroid=(),  # empty
        ),
        _make_centroid("3", 0.9, 0.4),
    )
    r = compute_semantic_budget(
        centroids, enabled_override=True, threshold=1.0,
    )
    # Should NOT crash; should integrate over the valid pair(s).
    assert r.verdict.value in (
        "within_budget", "approaching", "exceeded",
    )


def test_compute_with_none_input():
    """None passed for centroids → INSUFFICIENT_DATA, no crash."""
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    r = compute_semantic_budget(None, enabled_override=True)  # type: ignore
    assert r.verdict in (
        SemanticBudgetVerdict.DISABLED,
        SemanticBudgetVerdict.INSUFFICIENT_DATA,
    )


# ---------------------------------------------------------------------------
# Env knob clamps
# ---------------------------------------------------------------------------


def test_window_size_clamped(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        window_size,
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE", "0",
    )
    assert window_size() == 2
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE", "999999",
    )
    assert window_size() == 10_000
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE", "garbage",
    )
    assert window_size() == 50


def test_drift_threshold_clamped(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        drift_threshold,
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_THRESHOLD", "0",
    )
    assert drift_threshold() == 0.001
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_THRESHOLD", "999",
    )
    assert drift_threshold() == 100.0
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_THRESHOLD", "junk",
    )
    assert drift_threshold() == 0.30


def test_approaching_ratio_clamped(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        approaching_ratio,
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_APPROACHING_RATIO", "0.05",
    )
    assert approaching_ratio() == 0.1
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_APPROACHING_RATIO", "5",
    )
    assert approaching_ratio() == 1.0


# ---------------------------------------------------------------------------
# AST pins — auto-registered + green
# ---------------------------------------------------------------------------


_EXPECTED_PIN_NAMES = {
    "cross_op_semantic_budget_master_flag_stays_default_false",
    "cross_op_semantic_budget_authority_asymmetry",
    "cross_op_semantic_budget_verdict_taxonomy_5_values",
}


def test_pins_auto_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    registered = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
    }
    missing = _EXPECTED_PIN_NAMES - registered
    assert not missing, (
        f"missing Move 7 pins: {missing}"
    )


def test_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    violations = validate_all()
    relevant = [
        v for v in violations
        if v.invariant_name in _EXPECTED_PIN_NAMES
    ]
    assert not relevant, (
        "Move 7 pin violations: " + "; ".join(
            f"{v.invariant_name}: {v.detail}"
            for v in relevant
        )
    )


def test_master_flag_pin_blocks_premature_flip():
    """Synthetic source with default-True flips MUST fail the
    pin (operator-binding regression check)."""
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    flag_pin = next(
        p for p in pins
        if p.invariant_name
        == "cross_op_semantic_budget_master_flag_stays_default_false"  # noqa: E501
    )
    import ast
    bad_src = (
        'def cross_op_semantic_budget_enabled():\n'
        '    return True  # graduated default\n'
    )
    tree = ast.parse(bad_src)
    violations = flag_pin.validate(tree, bad_src)
    assert violations  # premature flip detected


# ---------------------------------------------------------------------------
# Authority asymmetry
# ---------------------------------------------------------------------------


def test_authority_asymmetry():
    import ast as _ast
    from pathlib import Path
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/"
        "cross_op_semantic_budget.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator", "iron_gate", "policy", "providers",
        "candidate_generator", "urgency_router",
        "change_engine", "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"cross_op_semantic_budget.py MUST "
                        f"NOT import {module!r}"
                    )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from backend.core.ouroboros.governance import (
        cross_op_semantic_budget as m,
    )
    expected = (
        "SemanticBudgetVerdict",
        "SemanticBudgetReport",
        "OpSemanticCentroid",
        "compute_semantic_budget",
        "cosine_distance",
        "cross_op_semantic_budget_enabled",
        "window_size",
        "drift_threshold",
        "approaching_ratio",
        "register_shipped_invariants",
        "CROSS_OP_SEMANTIC_BUDGET_SCHEMA_VERSION",
        "OP_SEMANTIC_CENTROID_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(m, name), f"missing public symbol: {name}"
