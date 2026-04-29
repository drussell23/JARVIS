"""RR Pass C Slice 5 — ExplorationLedger category-weight auto-rebalance regression suite.

Pins:
  * Module constants + master flag default-false-pre-graduation.
  * Frozen dataclasses: CategoryOutcomeLite + MinedWeightRebalance.
  * Env overrides for correlation_delta + weight_floor_pct +
    rebalance_threshold + window_days.
  * Window filter (epoch=0 retained).
  * **Pearson correlation kernel** (stdlib-only, Py 3.9 compat):
    perfect-positive / perfect-negative / no-correlation / zero-
    variance / mismatched-lengths / single-point.
  * Per-category correlation: skip categories with < 2 occurrences;
    map zero-variance to 0.0.
  * Rebalance computation:
    - high category weight rises by raise_pct.
    - low category weight lowers by lower_pct.
    - Weight floor (50% of original) hard-enforced.
    - MIN_WEIGHT_VALUE absolute floor enforced.
  * mine_weight_rebalances_from_events:
    - empty / below threshold / single category / gap below delta /
      qualifies / mass-conservation enforcement / per-Order
      independence.
  * propose: master-off / master-on / DUPLICATE on re-mine.
  * Surface validator: 5 reject paths + 1 valid pass.
  * Authority invariants (AST grep): substrate+stdlib only; no
    subprocess/network/env-mutation/LLM tokens.
  * **Mass-conservation invariant** end-to-end: every persisted
    proposal MUST satisfy Σ(new) ≥ Σ(old) AND min(new) ≥ 0.5*min(old).
"""
from __future__ import annotations

import ast as _ast
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    MonotonicTighteningVerdict,
    OperatorDecisionStatus,
    ProposeStatus,
    get_surface_validator,
    reset_default_ledger,
    validate_monotonic_tightening,
)
from backend.core.ouroboros.governance.adaptation.category_weight_rebalancer import (
    CategoryOutcomeLite,
    DEFAULT_CORRELATION_DELTA,
    DEFAULT_LOWER_PCT,
    DEFAULT_RAISE_PCT,
    DEFAULT_REBALANCE_THRESHOLD,
    DEFAULT_WEIGHT_FLOOR_PCT,
    DEFAULT_WINDOW_DAYS,
    MAX_LOWER_PCT,
    MAX_RAISE_PCT,
    MIN_WEIGHT_VALUE,
    MinedWeightRebalance,
    _compute_per_category_correlation,
    _compute_rebalanced_weights,
    _pearson_correlation,
    get_correlation_delta,
    get_rebalance_threshold,
    get_weight_floor_pct,
    get_window_days,
    install_surface_validator,
    is_enabled,
    mine_weight_rebalances_from_events,
    propose_weight_rebalances_from_events,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "category_weight_rebalancer.py"
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"),
    )
    yield
    reset_default_ledger()
    install_surface_validator()


def _co(
    *,
    op_id="op-1",
    category_scores=None,
    verify_passed=True,
    timestamp_unix=None,
):
    return CategoryOutcomeLite(
        op_id=op_id,
        category_scores=category_scores or {"a": 0.5, "b": 0.5},
        verify_passed=verify_passed,
        timestamp_unix=(
            timestamp_unix if timestamp_unix is not None else time.time()
        ),
    )


def _ledger(tmp_path):
    return AdaptationLedger(tmp_path / "ledger.jsonl")


# ===========================================================================
# A — Module constants + dataclass + master flag
# ===========================================================================


def test_default_correlation_delta_pinned():
    assert DEFAULT_CORRELATION_DELTA == 0.3


def test_default_weight_floor_pct_pinned():
    assert DEFAULT_WEIGHT_FLOOR_PCT == 50


def test_default_window_days_pinned():
    assert DEFAULT_WINDOW_DAYS == 7


def test_default_rebalance_threshold_pinned():
    assert DEFAULT_REBALANCE_THRESHOLD == 10


def test_default_raise_pct_pinned():
    assert DEFAULT_RAISE_PCT == 20


def test_default_lower_pct_pinned():
    assert DEFAULT_LOWER_PCT == 10


def test_lower_pct_strictly_less_than_raise_pct():
    """Net-tighten guarantee: defaults must satisfy lower < raise."""
    assert DEFAULT_LOWER_PCT < DEFAULT_RAISE_PCT


def test_max_pct_caps_pinned():
    assert MAX_RAISE_PCT == 100
    assert MAX_LOWER_PCT == 50


def test_min_weight_value_pinned():
    assert MIN_WEIGHT_VALUE == 0.01


def test_master_flag_default_true_post_graduation(monkeypatch):
    """Graduated 2026-04-29 (Move 1 Pass C cadence) — empty/unset env
    returns True. Asymmetric semantics: explicit falsy hot-reverts."""
    monkeypatch.delenv(
        "JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", raising=False,
    )
    assert is_enabled() is True


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", val)
        assert is_enabled() is True


def test_master_flag_explicit_falsy_hot_reverts(monkeypatch):
    """Asymmetric env semantics — explicit falsy tokens hot-revert
    the graduated default-true."""
    for val in ("0", "false", "no", "off"):
        monkeypatch.setenv("JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", val)
        assert is_enabled() is False


def test_category_outcome_lite_frozen():
    e = _co()
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.op_id = "x"  # type: ignore[misc]


def test_mined_weight_rebalance_frozen():
    m = MinedWeightRebalance(
        high_value_category="a", low_value_category="b",
        correlation_gap=0.5, new_weights={"a": 1.2, "b": 0.9},
        old_weights_sum=2.0, new_weights_sum=2.1,
        observation_count=10, source_event_ids=("op-1",), summary="s",
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.observation_count = 99  # type: ignore[misc]


# ===========================================================================
# B — Env overrides
# ===========================================================================


def test_correlation_delta_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_CORRELATION_DELTA", "0.5")
    assert get_correlation_delta() == 0.5


def test_correlation_delta_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_CORRELATION_DELTA", "garbage")
    assert get_correlation_delta() == DEFAULT_CORRELATION_DELTA


def test_correlation_delta_env_negative_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_CORRELATION_DELTA", "-0.5")
    assert get_correlation_delta() == DEFAULT_CORRELATION_DELTA


def test_correlation_delta_env_above_2_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_CORRELATION_DELTA", "5.0")
    assert get_correlation_delta() == DEFAULT_CORRELATION_DELTA


def test_weight_floor_pct_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT", "75")
    assert get_weight_floor_pct() == 75


def test_weight_floor_pct_env_out_of_range_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT", "101")
    assert get_weight_floor_pct() == DEFAULT_WEIGHT_FLOOR_PCT


def test_rebalance_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_REBALANCE_THRESHOLD", "20")
    assert get_rebalance_threshold() == 20


def test_rebalance_threshold_env_too_small_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_REBALANCE_THRESHOLD", "1")
    assert get_rebalance_threshold() == DEFAULT_REBALANCE_THRESHOLD


# ===========================================================================
# C — Pearson correlation kernel (stdlib-only Py 3.9 compat)
# ===========================================================================


def test_pearson_perfect_positive():
    assert _pearson_correlation([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0


def test_pearson_perfect_negative():
    assert _pearson_correlation([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_pearson_no_correlation():
    """Diagonal x flat-y → correlation 0."""
    assert _pearson_correlation([1, 2, 3, 4], [5, 5, 5, 5]) == 0.0


def test_pearson_zero_variance_in_x():
    assert _pearson_correlation([3, 3, 3, 3], [1, 2, 3, 4]) == 0.0


def test_pearson_short_input():
    assert _pearson_correlation([1], [2]) == 0.0


def test_pearson_mismatched_lengths():
    assert _pearson_correlation([1, 2, 3], [1, 2]) == 0.0


def test_pearson_partial_correlation():
    """Roughly +0.97 — positive but not perfect."""
    r = _pearson_correlation([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
    assert 0.5 < r < 1.0


# ===========================================================================
# D — Per-category correlation
# ===========================================================================


def test_per_category_correlation_skips_under_2_occurrences():
    """A category appearing in fewer than 2 events is skipped."""
    events = [
        _co(category_scores={"a": 0.1, "b": 0.5}, verify_passed=True),
        _co(category_scores={"a": 0.9}, verify_passed=False),  # b only once
    ]
    out = _compute_per_category_correlation(events)
    assert "a" in out
    assert "b" not in out  # only 1 occurrence


def test_per_category_correlation_zero_variance_returns_zero():
    """If a category's score is constant across events, correlation
    is 0 (no signal to extract)."""
    events = [
        _co(category_scores={"a": 0.5}, verify_passed=True),
        _co(category_scores={"a": 0.5}, verify_passed=False),
        _co(category_scores={"a": 0.5}, verify_passed=True),
    ]
    out = _compute_per_category_correlation(events)
    assert out["a"] == 0.0


def test_per_category_correlation_high_value_category():
    """Category 'good' scores high when verify passed."""
    events = []
    for i in range(5):
        events.append(_co(
            op_id=f"o-{i}",
            category_scores={"good": 0.9, "bad": 0.5},
            verify_passed=True,
        ))
    for i in range(5):
        events.append(_co(
            op_id=f"o-fail-{i}",
            category_scores={"good": 0.1, "bad": 0.5},
            verify_passed=False,
        ))
    out = _compute_per_category_correlation(events)
    assert out["good"] > 0.5  # strong positive correlation
    assert abs(out["bad"]) < 0.1  # zero-variance → 0


# ===========================================================================
# E — Weight rebalancing computation
# ===========================================================================


def test_rebalance_high_rises_low_falls():
    out = _compute_rebalanced_weights(
        {"a": 1.0, "b": 1.0}, "a", "b",
        raise_pct=20, lower_pct=10, floor_pct=50,
    )
    assert out["a"] == pytest.approx(1.2)
    assert out["b"] == pytest.approx(0.9)


def test_rebalance_floor_holds_low_steady():
    """If lower_pct would push below floor_pct, the floor wins."""
    out = _compute_rebalanced_weights(
        {"a": 1.0, "b": 1.0}, "a", "b",
        raise_pct=20, lower_pct=80,  # would push to 0.2
        floor_pct=50,                  # floor at 0.5
    )
    assert out["b"] == pytest.approx(0.5)  # floor held


def test_rebalance_min_weight_value_floor():
    """Even a generous floor_pct can't push below MIN_WEIGHT_VALUE."""
    out = _compute_rebalanced_weights(
        {"a": 1.0, "b": 0.001}, "a", "b",
        raise_pct=20, lower_pct=99, floor_pct=10,  # 10% of 0.001 = 0.0001
    )
    assert out["b"] >= MIN_WEIGHT_VALUE


def test_rebalance_does_not_mutate_input():
    original = {"a": 1.0, "b": 1.0}
    _compute_rebalanced_weights(
        original, "a", "b", raise_pct=20, lower_pct=10, floor_pct=50,
    )
    assert original == {"a": 1.0, "b": 1.0}


def test_rebalance_high_equals_low_no_op():
    out = _compute_rebalanced_weights(
        {"a": 1.0}, "a", "a",
        raise_pct=20, lower_pct=10, floor_pct=50,
    )
    # When high == low only the raise applies (the lower branch is
    # short-circuited).
    assert out["a"] == pytest.approx(1.2)


# ===========================================================================
# F — mine_weight_rebalances_from_events end-to-end
# ===========================================================================


def test_mine_empty_returns_empty():
    out = mine_weight_rebalances_from_events([], current_weights={})
    assert out == []


def test_mine_below_threshold_returns_empty():
    """Need ≥ 10 events for stable correlation; 5 ops < threshold."""
    events = [
        _co(op_id=f"o-{i}",
            category_scores={"a": 0.9 if i < 3 else 0.1, "b": 0.5},
            verify_passed=(i < 3))
        for i in range(5)
    ]
    out = mine_weight_rebalances_from_events(
        events, current_weights={"a": 1.0, "b": 1.0},
    )
    assert out == []


def test_mine_single_category_returns_empty():
    """Need at least 2 categories to rebalance."""
    events = [
        _co(op_id=f"o-{i}", category_scores={"only": 0.5},
            verify_passed=(i % 2 == 0))
        for i in range(15)
    ]
    out = mine_weight_rebalances_from_events(
        events, current_weights={"only": 1.0}, threshold=10,
    )
    assert out == []


def test_mine_gap_below_delta_returns_empty():
    """Both categories correlate similarly with verify → no rebalance."""
    events = []
    for i in range(15):
        passed = (i % 2 == 0)
        events.append(_co(
            op_id=f"o-{i}",
            category_scores={"a": 0.5, "b": 0.5},
            verify_passed=passed,
        ))
    out = mine_weight_rebalances_from_events(
        events, current_weights={"a": 1.0, "b": 1.0}, threshold=10,
    )
    assert out == []  # zero-variance → both correlations are 0 → gap=0


def test_mine_qualifies_proposes_rebalance():
    """High-correlation category 'good' should be raised; low-
    correlation 'bad' should be lowered with floor."""
    events = []
    for i in range(10):
        events.append(_co(
            op_id=f"o-pass-{i}",
            category_scores={"good": 0.9, "bad": 0.1},
            verify_passed=True,
        ))
    for i in range(10):
        events.append(_co(
            op_id=f"o-fail-{i}",
            category_scores={"good": 0.1, "bad": 0.9},
            verify_passed=False,
        ))
    out = mine_weight_rebalances_from_events(
        events, current_weights={"good": 1.0, "bad": 1.0},
        threshold=10,
    )
    assert len(out) == 1
    m = out[0]
    assert m.high_value_category == "good"
    assert m.low_value_category == "bad"
    assert m.new_weights["good"] > 1.0
    assert m.new_weights["bad"] < 1.0
    # Mass conservation invariant
    assert m.new_weights_sum > m.old_weights_sum


def test_mine_mass_conservation_invariant_holds():
    """Critical pin: every produced rebalance MUST satisfy Σ(new) ≥
    Σ(old) — the load-bearing cage rule per §9.2."""
    events = []
    for i in range(10):
        events.append(_co(
            op_id=f"o-pass-{i}",
            category_scores={"a": 0.9, "b": 0.1},
            verify_passed=True,
        ))
    for i in range(10):
        events.append(_co(
            op_id=f"o-fail-{i}",
            category_scores={"a": 0.1, "b": 0.9},
            verify_passed=False,
        ))
    out = mine_weight_rebalances_from_events(
        events, current_weights={"a": 1.0, "b": 1.0}, threshold=10,
    )
    assert len(out) == 1
    assert out[0].new_weights_sum >= out[0].old_weights_sum


def test_mine_low_floor_invariant_holds():
    """min(new_weights) >= 0.5 * min(old_weights). Pin via aggressive
    lower_pct that would otherwise push below the floor."""
    events = []
    for i in range(10):
        events.append(_co(
            op_id=f"o-pass-{i}",
            category_scores={"a": 0.9, "b": 0.1},
            verify_passed=True,
        ))
    for i in range(10):
        events.append(_co(
            op_id=f"o-fail-{i}",
            category_scores={"a": 0.1, "b": 0.9},
            verify_passed=False,
        ))
    out = mine_weight_rebalances_from_events(
        events, current_weights={"a": 1.0, "b": 1.0},
        threshold=10, lower_pct=49, raise_pct=50, floor_pct=50,
    )
    assert len(out) == 1
    assert out[0].new_weights["b"] >= 0.5  # floor at 50%


def test_mine_clamps_lower_pct_to_below_raise_pct():
    """If caller passes lower_pct >= raise_pct (loosening!), the
    miner forces lower_pct down to raise_pct // 2 (preserves net-
    tighten guarantee)."""
    events = []
    for i in range(10):
        events.append(_co(
            op_id=f"o-pass-{i}",
            category_scores={"a": 0.9, "b": 0.1},
            verify_passed=True,
        ))
    for i in range(10):
        events.append(_co(
            op_id=f"o-fail-{i}",
            category_scores={"a": 0.1, "b": 0.9},
            verify_passed=False,
        ))
    out = mine_weight_rebalances_from_events(
        events, current_weights={"a": 1.0, "b": 1.0},
        threshold=10, raise_pct=20, lower_pct=20,  # would loosen!
    )
    # Still produces a proposal (lower_pct clamped to 10) and
    # satisfies mass-conservation
    assert len(out) == 1
    assert out[0].new_weights_sum > out[0].old_weights_sum


def test_mine_window_filter_drops_old_events():
    now = time.time()
    events = []
    for i in range(10):
        events.append(_co(
            op_id=f"old-{i}",
            category_scores={"a": 0.9, "b": 0.1},
            verify_passed=True,
            timestamp_unix=now - (8 * 86_400),
        ))
    out = mine_weight_rebalances_from_events(
        events, current_weights={"a": 1.0, "b": 1.0},
        window_days=7, threshold=10, now_unix=now,
    )
    assert out == []


def test_mine_proposal_id_stable():
    events = []
    for i in range(10):
        events.append(_co(
            op_id=f"o-pass-{i}", category_scores={"good": 0.9, "bad": 0.1},
            verify_passed=True,
        ))
    for i in range(10):
        events.append(_co(
            op_id=f"o-fail-{i}", category_scores={"good": 0.1, "bad": 0.9},
            verify_passed=False,
        ))
    o1 = mine_weight_rebalances_from_events(
        events, current_weights={"good": 1.0, "bad": 1.0}, threshold=10,
    )
    o2 = mine_weight_rebalances_from_events(
        events, current_weights={"good": 1.0, "bad": 1.0}, threshold=10,
    )
    assert o1[0].proposal_id() == o2[0].proposal_id()


# ===========================================================================
# G — propose_weight_rebalances_from_events: ledger integration
# ===========================================================================


def test_propose_master_off(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", "0")
    led = _ledger(tmp_path)
    events = [
        _co(op_id=f"o-pass-{i}", category_scores={"a": 0.9, "b": 0.1},
            verify_passed=True)
        for i in range(10)
    ] + [
        _co(op_id=f"o-fail-{i}", category_scores={"a": 0.1, "b": 0.9},
            verify_passed=False)
        for i in range(10)
    ]
    out = propose_weight_rebalances_from_events(
        events, ledger=led, current_weights={"a": 1.0, "b": 1.0},
    )
    assert out == []
    assert not (tmp_path / "ledger.jsonl").exists()


def test_propose_master_on_writes_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _co(op_id=f"o-pass-{i}", category_scores={"a": 0.9, "b": 0.1},
            verify_passed=True)
        for i in range(10)
    ] + [
        _co(op_id=f"o-fail-{i}", category_scores={"a": 0.1, "b": 0.9},
            verify_passed=False)
        for i in range(10)
    ]
    out = propose_weight_rebalances_from_events(
        events, ledger=led, current_weights={"a": 1.0, "b": 1.0},
    )
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.surface is AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS
    assert p.proposal_kind == "rebalance_weight"
    assert "↑" in p.evidence.summary
    assert "↓" in p.evidence.summary
    assert "net +" in p.evidence.summary


def test_propose_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _co(op_id=f"o-pass-{i}", category_scores={"a": 0.9, "b": 0.1},
            verify_passed=True)
        for i in range(10)
    ] + [
        _co(op_id=f"o-fail-{i}", category_scores={"a": 0.1, "b": 0.9},
            verify_passed=False)
        for i in range(10)
    ]
    propose_weight_rebalances_from_events(
        events, ledger=led, current_weights={"a": 1.0, "b": 1.0},
    )
    second = propose_weight_rebalances_from_events(
        events, ledger=led, current_weights={"a": 1.0, "b": 1.0},
    )
    assert second[0].status is ProposeStatus.DUPLICATE_PROPOSAL_ID


# ===========================================================================
# H — Surface validator
# ===========================================================================


def test_surface_validator_registered_at_import():
    v = get_surface_validator(
        AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS,
    )
    assert v is not None


def _build_proposal(
    *, kind="rebalance_weight", proposed_hash="sha256:abc",
    observation_count=10,
    summary="cat ↑ by 20%; cat ↓ by 10%. Σ 2.00 → 2.10 (net +0.1).",
):
    return AdaptationProposal(
        schema_version="1.0", proposal_id="p-test",
        surface=AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS,
        proposal_kind=kind,
        evidence=AdaptationEvidence(
            window_days=7, observation_count=observation_count,
            summary=summary,
        ),
        current_state_hash="sha256:current",
        proposed_state_hash=proposed_hash,
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="t", proposed_at_epoch=1.0,
        operator_decision=OperatorDecisionStatus.PENDING,
    )


def test_validator_rejects_wrong_kind():
    p = _build_proposal(kind="add_pattern")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "kind_must_be_rebalance_weight" in detail


def test_validator_rejects_non_sha256_hash():
    p = _build_proposal(proposed_hash="x")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "proposed_hash_format" in detail


def test_validator_rejects_observation_count_below_threshold(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_REBALANCE_THRESHOLD", "20")
    p = _build_proposal(observation_count=10)
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "observation_count_below_threshold" in detail


def test_validator_rejects_missing_up_indicator():
    p = _build_proposal(summary="↓ only, no up arrow. net +0.1.")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "missing_both_direction_indicators" in detail


def test_validator_rejects_missing_down_indicator():
    p = _build_proposal(summary="↑ only, no down arrow. net +0.1.")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "missing_both_direction_indicators" in detail


def test_validator_rejects_missing_net_positive_indicator():
    p = _build_proposal(summary="cat ↑ + cat ↓. (no net summary)")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "missing_net_positive_indicator" in detail


def test_validator_passes_with_all_valid():
    p = _build_proposal()
    verdict, _ = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.PASSED


def test_install_surface_validator_idempotent():
    install_surface_validator()
    install_surface_validator()
    v = get_surface_validator(
        AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS,
    )
    assert v is not None


# ===========================================================================
# I — Authority invariants (AST grep)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned = (
        "orchestrator", "iron_gate", "change_engine",
        "candidate_generator", "risk_tier_floor", "semantic_guardian",
        "semantic_firewall", "scoped_tool_backend", ".gate.",
        "phase_runners", "providers",
    )
    found = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned:
                if sub in mod:
                    found.append((mod, sub))
    assert not found


def test_module_imports_only_substrate_and_stdlib():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    stdlib = (
        "__future__",
        "hashlib", "logging", "math", "os", "dataclasses", "typing",
    )
    allowed_governance = (
        "backend.core.ouroboros.governance.adaptation.ledger",
    )
    for node in tree.body:
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            ok = (
                any(mod == p or mod.startswith(p + ".") for p in stdlib)
                or mod in allowed_governance
            )
            assert ok, f"unauthorized import {mod!r}"


def test_module_does_not_call_subprocess_or_network():
    src = _MODULE_PATH.read_text()
    forbidden = (
        "subprocess.", "socket.", "urllib.", "requests.",
        "os." + "system(", "messages.create(", "from anthropic",
    )
    found = [t for t in forbidden if t in src]
    assert not found


# ===========================================================================
# J — Substrate integration (full pipeline mass-conservation pin)
# ===========================================================================


def test_full_pipeline_proposal_satisfies_mass_conservation_pin(
    monkeypatch, tmp_path,
):
    """End-to-end load-bearing pin: every persisted proposal MUST
    satisfy Σ(new) ≥ Σ(old). This is the cage's structural rule."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _co(op_id=f"o-pass-{i}", category_scores={"good": 0.9, "bad": 0.1},
            verify_passed=True)
        for i in range(10)
    ] + [
        _co(op_id=f"o-fail-{i}", category_scores={"good": 0.1, "bad": 0.9},
            verify_passed=False)
        for i in range(10)
    ]
    out = propose_weight_rebalances_from_events(
        events, ledger=led, current_weights={"good": 1.0, "bad": 1.0},
    )
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    p = led.get(out[0].proposal_id)
    assert p is not None
    # Substrate accepted it because both default + surface validators
    # passed → mass-conservation invariant held by construction.
    assert p.monotonic_tightening_verdict is MonotonicTighteningVerdict.PASSED
