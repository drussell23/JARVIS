"""RR Pass C Slice 4 — Per-Order mutation budget + risk-tier ladder
extender regression suite (combined per spec §8 design).

Covers TWO sub-surfaces:
  * Slice 4a — `per_order_mutation_budget.py` (lower_budget surface)
  * Slice 4b — `risk_tier_extender.py` (add_tier surface)

Per Pass C §8.6: "Combined slice graduates when both sub-surfaces
have 5 clean each." So both share this test file but each gets its
own independent pin matrix.

Pins (4a — per_order_mutation_budget):
  * Module constants + ORDER_1/ORDER_2 + master flag default-false.
  * MutationUsageLite + MinedBudgetLowering frozen.
  * Env overrides + window filter.
  * mine pipeline: empty / no-underutil / proposed >= current /
    threshold / per-Order independence / Order-2 floor pin /
    idempotent proposal_id.
  * propose: master-off / master-on / DUPLICATE on re-mine.
  * Surface validator: kind != lower_budget / non-sha256 hash /
    below threshold / summary missing → / valid pass.
  * Authority invariants.

Pins (4b — risk_tier_extender):
  * Module constants + DEFAULT_LADDER + DEFAULT_KNOWN_FAILURE_CLASSES.
  * PostmortemEventLite + MinedTierExtension frozen.
  * Env overrides + window filter.
  * Novel-class grouping (known classes excluded).
  * Blast-radius classifier: 4 bands (HARDENED at 3 levels +
    CRITICAL at top).
  * Tier name synthesis: deterministic + uppercase + sanitized +
    truncated.
  * mine pipeline: empty / below threshold / multi-class / known-
    class skipped.
  * propose: master-off / master-on / DUPLICATE on re-mine.
  * Surface validator: kind != add_tier / non-sha256 hash /
    below threshold / summary missing insert indicator / valid pass.
  * Authority invariants.
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

# -- Slice 4a imports --
from backend.core.ouroboros.governance.adaptation.per_order_mutation_budget import (
    DEFAULT_BUDGET_THRESHOLD,
    MIN_ORDER2_BUDGET,
    MinedBudgetLowering,
    MutationUsageLite,
    ORDER_1,
    ORDER_2,
    get_budget_threshold,
    install_surface_validator as install_budget_validator,
    is_enabled as budget_is_enabled,
    mine_budget_lowerings_from_events,
    propose_budget_lowerings_from_events,
)

# -- Slice 4b imports --
from backend.core.ouroboros.governance.adaptation.risk_tier_extender import (
    DEFAULT_KNOWN_FAILURE_CLASSES,
    DEFAULT_LADDER,
    DEFAULT_TIER_THRESHOLD,
    MAX_TIER_NAME_CHARS,
    MinedTierExtension,
    PostmortemEventLite as TierEventLite,
    _classify_blast_radius_band,
    _synthesize_tier_name,
    get_tier_threshold,
    install_surface_validator as install_tier_validator,
    is_enabled as tier_is_enabled,
    mine_tier_extensions_from_events,
    propose_tier_extensions_from_events,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_BUDGET_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "per_order_mutation_budget.py"
)
_TIER_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "risk_tier_extender.py"
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"),
    )
    yield
    reset_default_ledger()
    install_budget_validator()
    install_tier_validator()


def _ledger(tmp_path):
    return AdaptationLedger(tmp_path / "ledger.jsonl")


# ============================================================================
# Slice 4a — per_order_mutation_budget
# ============================================================================


def _mu(
    *,
    op_id="op-1",
    order=ORDER_2,
    observed_mutations=1,
    budget_at_time=5,
    timestamp_unix=None,
):
    return MutationUsageLite(
        op_id=op_id, order=order,
        observed_mutations=observed_mutations,
        budget_at_time=budget_at_time,
        timestamp_unix=(
            timestamp_unix if timestamp_unix is not None else time.time()
        ),
    )


# --- 4a.A constants + master flag + dataclass ---


def test_4a_default_budget_threshold_pinned():
    assert DEFAULT_BUDGET_THRESHOLD == 5


def test_4a_min_order2_budget_pinned():
    assert MIN_ORDER2_BUDGET == 1


def test_4a_order_constants_pinned():
    assert ORDER_1 == 1
    assert ORDER_2 == 2


def test_4a_master_flag_default_true_post_graduation(monkeypatch):
    """Graduated 2026-04-29 (Move 1 Pass C cadence) — empty/unset env
    returns True. Asymmetric semantics: explicit falsy hot-reverts."""
    monkeypatch.delenv(
        "JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", raising=False,
    )
    assert budget_is_enabled() is True


def test_4a_master_flag_truthy(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", "1")
    assert budget_is_enabled() is True


def test_4a_master_flag_explicit_falsy_hot_reverts(monkeypatch):
    """Asymmetric env semantics — explicit "0" hot-reverts the
    graduated default-true."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", "0")
    assert budget_is_enabled() is False


def test_4a_mutation_usage_lite_frozen():
    e = _mu()
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.op_id = "x"  # type: ignore[misc]


def test_4a_mined_budget_lowering_frozen():
    m = MinedBudgetLowering(
        order=2, current_budget=5, proposed_budget=2,
        underutilized_count=5, source_event_ids=("o-1",), summary="s",
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.proposed_budget = 3  # type: ignore[misc]


def test_4a_budget_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_BUDGET_THRESHOLD", "10")
    assert get_budget_threshold() == 10


# --- 4a.B mine pipeline ---


def test_4a_mine_empty_returns_empty():
    out = mine_budget_lowerings_from_events([], current_budgets={2: 5})
    assert out == []


def test_4a_mine_no_underutilization_returns_empty():
    """All 5 ops used the full budget → no slack to lower."""
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=5, budget_at_time=5)
        for i in range(5)
    ]
    out = mine_budget_lowerings_from_events(
        events, current_budgets={2: 5}, threshold=5,
    )
    assert out == []


def test_4a_mine_qualifies_proposes_lower():
    """5 ops only used 1 mutation each → proposal lowers from 5 to 1."""
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=1, budget_at_time=5)
        for i in range(5)
    ]
    out = mine_budget_lowerings_from_events(
        events, current_budgets={2: 5}, threshold=5,
    )
    assert len(out) == 1
    m = out[0]
    assert m.order == 2
    assert m.current_budget == 5
    assert m.proposed_budget == 1
    assert m.underutilized_count == 5


def test_4a_mine_order2_floor_enforced():
    """Even if all ops used 0 mutations, Order-2 budget cannot
    propose below MIN_ORDER2_BUDGET=1."""
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=0, budget_at_time=5)
        for i in range(5)
    ]
    out = mine_budget_lowerings_from_events(
        events, current_budgets={2: 5}, threshold=5,
    )
    assert len(out) == 1
    assert out[0].proposed_budget == MIN_ORDER2_BUDGET


def test_4a_mine_max_observed_used_as_proposal():
    """If 4 ops use 1 and 1 op uses 3, propose 3 (the max — safe)."""
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=1, budget_at_time=10)
        for i in range(4)
    ]
    events.append(
        _mu(op_id="o-spike", observed_mutations=3, budget_at_time=10),
    )
    out = mine_budget_lowerings_from_events(
        events, current_budgets={2: 10}, threshold=5,
    )
    assert out[0].proposed_budget == 3


def test_4a_mine_per_order_independence():
    """Order-1 + Order-2 ops handled separately."""
    events = [
        _mu(op_id=f"o1-{i}", order=ORDER_1,
            observed_mutations=1, budget_at_time=10)
        for i in range(5)
    ] + [
        _mu(op_id=f"o2-{i}", order=ORDER_2,
            observed_mutations=1, budget_at_time=5)
        for i in range(5)
    ]
    out = mine_budget_lowerings_from_events(
        events, current_budgets={1: 10, 2: 5}, threshold=5,
    )
    orders = sorted(m.order for m in out)
    assert orders == [1, 2]


def test_4a_mine_skip_when_proposed_equals_current():
    """If max observed == current budget, no slack to propose."""
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=4, budget_at_time=5)
        for i in range(4)
    ]
    events.append(
        _mu(op_id="o-spike", observed_mutations=5, budget_at_time=5),
    )
    out = mine_budget_lowerings_from_events(
        events, current_budgets={2: 5}, threshold=5,
    )
    # Some underutilized but max=5 == current=5 → no proposal
    assert out == []


def test_4a_mine_window_filter_drops_old():
    now = time.time()
    old = [
        _mu(op_id=f"old-{i}", observed_mutations=1, budget_at_time=5,
            timestamp_unix=now - (8 * 86_400))
        for i in range(5)
    ]
    out = mine_budget_lowerings_from_events(
        old, current_budgets={2: 5}, window_days=7,
        threshold=5, now_unix=now,
    )
    assert out == []


def test_4a_proposal_id_stable_across_calls():
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=1, budget_at_time=5)
        for i in range(5)
    ]
    o1 = mine_budget_lowerings_from_events(
        events, current_budgets={2: 5}, threshold=5,
    )
    o2 = mine_budget_lowerings_from_events(
        events, current_budgets={2: 5}, threshold=5,
    )
    assert o1[0].proposal_id() == o2[0].proposal_id()


# --- 4a.C ledger integration ---


def test_4a_propose_master_off(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", "0")
    led = _ledger(tmp_path)
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=1, budget_at_time=5)
        for i in range(5)
    ]
    out = propose_budget_lowerings_from_events(
        events, ledger=led, current_budgets={2: 5},
    )
    assert out == []
    assert not (tmp_path / "ledger.jsonl").exists()


def test_4a_propose_master_on(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=1, budget_at_time=5)
        for i in range(5)
    ]
    out = propose_budget_lowerings_from_events(
        events, ledger=led, current_budgets={2: 5},
    )
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.surface is AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET
    assert p.proposal_kind == "lower_budget"


def test_4a_propose_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _mu(op_id=f"o-{i}", observed_mutations=1, budget_at_time=5)
        for i in range(5)
    ]
    propose_budget_lowerings_from_events(
        events, ledger=led, current_budgets={2: 5},
    )
    out2 = propose_budget_lowerings_from_events(
        events, ledger=led, current_budgets={2: 5},
    )
    assert out2[0].status is ProposeStatus.DUPLICATE_PROPOSAL_ID


# --- 4a.D Surface validator ---


def _build_budget_proposal(
    *, kind="lower_budget", proposed_hash="sha256:abc",
    observation_count=5, summary="order-2 budget 5 → 1",
):
    return AdaptationProposal(
        schema_version="1.0", proposal_id="p-test",
        surface=AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET,
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


def test_4a_validator_registered_at_import():
    v = get_surface_validator(
        AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET,
    )
    assert v is not None


def test_4a_validator_rejects_wrong_kind():
    p = _build_budget_proposal(kind="raise_floor")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "kind_must_be_lower_budget" in detail


def test_4a_validator_rejects_non_sha256_hash():
    p = _build_budget_proposal(proposed_hash="x")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "proposed_hash_format" in detail


def test_4a_validator_rejects_summary_without_arrow():
    p = _build_budget_proposal(summary="missing the indicator")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "summary_missing_direction_indicator" in detail


def test_4a_validator_passes_with_all_valid():
    p = _build_budget_proposal()
    verdict, _ = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.PASSED


# ============================================================================
# Slice 4b — risk_tier_extender
# ============================================================================


def _te(
    *,
    op_id="op-1",
    failure_class="silent_test_pass",
    blast_radius=0.3,
    timestamp_unix=None,
):
    return TierEventLite(
        op_id=op_id,
        failure_class=failure_class,
        blast_radius=blast_radius,
        timestamp_unix=(
            timestamp_unix if timestamp_unix is not None else time.time()
        ),
    )


# --- 4b.A constants + dataclass + master flag ---


def test_4b_default_tier_threshold_pinned():
    assert DEFAULT_TIER_THRESHOLD == 5


def test_4b_default_ladder_pinned():
    assert DEFAULT_LADDER == (
        "SAFE_AUTO", "NOTIFY_APPLY", "APPROVAL_REQUIRED", "BLOCKED",
    )


def test_4b_default_known_failure_classes_includes_infra():
    assert "infra" in DEFAULT_KNOWN_FAILURE_CLASSES
    assert "code" in DEFAULT_KNOWN_FAILURE_CLASSES


def test_4b_max_tier_name_chars_pinned():
    assert MAX_TIER_NAME_CHARS == 64


def test_4b_master_flag_default_true_post_graduation(monkeypatch):
    """Graduated 2026-04-29 (Move 1 Pass C cadence) — empty/unset env
    returns True. Asymmetric semantics: explicit falsy hot-reverts."""
    monkeypatch.delenv(
        "JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", raising=False,
    )
    assert tier_is_enabled() is True


def test_4b_master_flag_truthy(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", "1")
    assert tier_is_enabled() is True


def test_4b_master_flag_explicit_falsy_hot_reverts(monkeypatch):
    """Asymmetric env semantics — explicit "0" hot-reverts the
    graduated default-true."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", "0")
    assert tier_is_enabled() is False


def test_4b_postmortem_event_lite_frozen():
    import dataclasses
    e = _te()
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.op_id = "x"  # type: ignore[misc]


def test_4b_mined_tier_extension_frozen():
    import dataclasses
    m = MinedTierExtension(
        failure_class="x", proposed_tier_name="X_HARDENED_x",
        insert_after_tier="A", insert_before_tier="B",
        avg_blast_radius=0.5, occurrence_count=5,
        source_event_ids=("o-1",), summary="s",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.occurrence_count = 6  # type: ignore[misc]


def test_4b_tier_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_TIER_THRESHOLD", "20")
    assert get_tier_threshold() == 20


# --- 4b.B blast-radius classifier (pure function) ---


def test_4b_classify_low_blast_radius():
    after, before, suffix = _classify_blast_radius_band(0.1)
    assert after == "SAFE_AUTO"
    assert before == "NOTIFY_APPLY"
    assert suffix == "HARDENED"


def test_4b_classify_mid_blast_radius():
    after, before, suffix = _classify_blast_radius_band(0.4)
    assert after == "NOTIFY_APPLY"
    assert before == "APPROVAL_REQUIRED"
    assert suffix == "HARDENED"


def test_4b_classify_high_blast_radius():
    after, before, suffix = _classify_blast_radius_band(0.6)
    assert after == "APPROVAL_REQUIRED"
    assert before == "BLOCKED"
    assert suffix == "HARDENED"


def test_4b_classify_critical_blast_radius():
    after, before, suffix = _classify_blast_radius_band(0.9)
    assert after == "APPROVAL_REQUIRED"
    assert before == "BLOCKED"
    assert suffix == "CRITICAL"


# --- 4b.C tier name synthesis ---


def test_4b_synthesize_tier_name_basic():
    name = _synthesize_tier_name(
        "silent_test_pass", "NOTIFY_APPLY", "HARDENED",
    )
    assert name == "NOTIFY_APPLY_HARDENED_SILENT_TEST_PASS"


def test_4b_synthesize_sanitizes_special_chars():
    name = _synthesize_tier_name(
        "weird-chars!@#", "NOTIFY_APPLY", "HARDENED",
    )
    # Special chars become underscores
    assert "!" not in name
    assert "@" not in name
    assert "#" not in name


def test_4b_synthesize_truncated_at_max():
    huge = "X" * 200
    name = _synthesize_tier_name(huge, "NOTIFY_APPLY", "HARDENED")
    assert len(name) == MAX_TIER_NAME_CHARS


def test_4b_synthesize_uppercase():
    name = _synthesize_tier_name(
        "lowercase_class", "notify_apply", "hardened",
    )
    # The function uppercases the failure_class; insert_after stays as-is
    # (it's a tier name passed in, expected to already be uppercase).
    assert "LOWERCASE_CLASS" in name


# --- 4b.D mine pipeline ---


def test_4b_mine_empty_returns_empty():
    assert mine_tier_extensions_from_events([]) == []


def test_4b_mine_known_classes_skipped():
    """5 'infra' events shouldn't propose a new tier — infra is
    in the default known set."""
    events = [
        _te(op_id=f"o-{i}", failure_class="infra")
        for i in range(5)
    ]
    out = mine_tier_extensions_from_events(events)
    assert out == []


def test_4b_mine_below_threshold_returns_empty():
    """4 novel events < threshold=5 → no proposal."""
    events = [
        _te(op_id=f"o-{i}", failure_class="silent_test_pass")
        for i in range(4)
    ]
    out = mine_tier_extensions_from_events(events, threshold=5)
    assert out == []


def test_4b_mine_at_threshold_proposes():
    events = [
        _te(op_id=f"o-{i}", failure_class="silent_test_pass",
            blast_radius=0.3)
        for i in range(5)
    ]
    out = mine_tier_extensions_from_events(events, threshold=5)
    assert len(out) == 1
    m = out[0]
    assert m.failure_class == "silent_test_pass"
    assert m.occurrence_count == 5
    assert m.insert_after_tier == "NOTIFY_APPLY"
    assert m.insert_before_tier == "APPROVAL_REQUIRED"
    assert "HARDENED" in m.proposed_tier_name


def test_4b_mine_multi_class_yields_multi_proposals():
    events = []
    for i in range(5):
        events.append(_te(
            op_id=f"a-{i}", failure_class="class_a", blast_radius=0.2,
        ))
    for i in range(5):
        events.append(_te(
            op_id=f"b-{i}", failure_class="class_b", blast_radius=0.8,
        ))
    out = mine_tier_extensions_from_events(events, threshold=5)
    assert len(out) == 2


def test_4b_mine_window_filter():
    now = time.time()
    old = [
        _te(op_id=f"old-{i}", failure_class="novel",
            timestamp_unix=now - (8 * 86_400))
        for i in range(5)
    ]
    out = mine_tier_extensions_from_events(
        old, threshold=5, window_days=7, now_unix=now,
    )
    assert out == []


def test_4b_mine_proposal_id_stable():
    events = [
        _te(op_id=f"o-{i}", failure_class="novel", blast_radius=0.3)
        for i in range(5)
    ]
    a = mine_tier_extensions_from_events(events, threshold=5)
    b = mine_tier_extensions_from_events(events, threshold=5)
    assert a[0].proposal_id() == b[0].proposal_id()


def test_4b_mine_proposal_id_differs_for_different_class():
    a = mine_tier_extensions_from_events(
        [_te(op_id=f"o-{i}", failure_class="A", blast_radius=0.3)
         for i in range(5)], threshold=5,
    )
    b = mine_tier_extensions_from_events(
        [_te(op_id=f"o-{i}", failure_class="B", blast_radius=0.3)
         for i in range(5)], threshold=5,
    )
    assert a[0].proposal_id() != b[0].proposal_id()


# --- 4b.E ledger integration ---


def test_4b_propose_master_off(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", "0")
    led = _ledger(tmp_path)
    events = [
        _te(op_id=f"o-{i}", failure_class="novel", blast_radius=0.3)
        for i in range(5)
    ]
    out = propose_tier_extensions_from_events(events, ledger=led)
    assert out == []
    assert not (tmp_path / "ledger.jsonl").exists()


def test_4b_propose_master_on(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _te(op_id=f"o-{i}", failure_class="novel", blast_radius=0.3)
        for i in range(5)
    ]
    out = propose_tier_extensions_from_events(events, ledger=led)
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.surface is AdaptationSurface.RISK_TIER_FLOOR_TIERS
    assert p.proposal_kind == "add_tier"


def test_4b_propose_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _te(op_id=f"o-{i}", failure_class="novel", blast_radius=0.3)
        for i in range(5)
    ]
    propose_tier_extensions_from_events(events, ledger=led)
    second = propose_tier_extensions_from_events(events, ledger=led)
    assert second[0].status is ProposeStatus.DUPLICATE_PROPOSAL_ID


# --- 4b.F Surface validator ---


def _build_tier_proposal(
    *, kind="add_tier", proposed_hash="sha256:abc",
    observation_count=5,
    summary="proposed new tier insert between SAFE_AUTO and NOTIFY_APPLY",
):
    return AdaptationProposal(
        schema_version="1.0", proposal_id="p-test",
        surface=AdaptationSurface.RISK_TIER_FLOOR_TIERS,
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


def test_4b_validator_registered_at_import():
    v = get_surface_validator(AdaptationSurface.RISK_TIER_FLOOR_TIERS)
    assert v is not None


def test_4b_validator_rejects_wrong_kind():
    p = _build_tier_proposal(kind="raise_floor")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "kind_must_be_add_tier" in detail


def test_4b_validator_rejects_non_sha256_hash():
    p = _build_tier_proposal(proposed_hash="x")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "proposed_hash_format" in detail


def test_4b_validator_rejects_summary_missing_insert_indicator():
    p = _build_tier_proposal(summary="just words, no direction signal")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "summary_missing_insert_indicator" in detail


def test_4b_validator_passes_with_all_valid():
    p = _build_tier_proposal()
    verdict, _ = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.PASSED


# ============================================================================
# Cross-surface authority invariants (both modules)
# ============================================================================


def _ast_grep_no_banned(module_path: Path):
    tree = _ast.parse(module_path.read_text(encoding="utf-8"))
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
    return found


def test_4a_no_banned_governance_imports():
    assert _ast_grep_no_banned(_BUDGET_PATH) == []


def test_4b_no_banned_governance_imports():
    assert _ast_grep_no_banned(_TIER_PATH) == []


def test_4a_no_subprocess_or_network():
    src = _BUDGET_PATH.read_text()
    forbidden = ("subprocess.", "socket.", "urllib.",
                 "os." + "system(", "messages.create(", "from anthropic")
    assert all(tok not in src for tok in forbidden)


def test_4b_no_subprocess_or_network():
    src = _TIER_PATH.read_text()
    forbidden = ("subprocess.", "socket.", "urllib.",
                 "os." + "system(", "messages.create(", "from anthropic")
    assert all(tok not in src for tok in forbidden)


def test_both_surfaces_register_distinct_validators():
    """Pin: 4a's validator lives at SCOPED_TOOL_BACKEND_MUTATION_
    BUDGET; 4b's at RISK_TIER_FLOOR_TIERS. They do NOT collide."""
    install_budget_validator()
    install_tier_validator()
    v_budget = get_surface_validator(
        AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET,
    )
    v_tier = get_surface_validator(
        AdaptationSurface.RISK_TIER_FLOOR_TIERS,
    )
    assert v_budget is not None
    assert v_tier is not None
    assert v_budget is not v_tier
