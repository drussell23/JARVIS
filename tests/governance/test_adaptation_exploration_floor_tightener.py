"""RR Pass C Slice 3 — IronGate exploration-floor tightener regression suite.

Pins:
  * Module constants + master flag default-false-pre-graduation.
  * Frozen dataclasses: ExplorationOutcomeLite + MinedFloorRaise.
  * Env overrides for threshold + raise_pct + window_days.
  * Window filter behavior (epoch=0 retained).
  * Bypass-failure filter (only floor_satisfied=True AND
    verify_outcome IN {regression, failed} count).
  * Weakest-category identification: per-op argmin across category
    scores; tie-break by alpha; group-count winner.
  * compute_proposed_floor: ceil(current * pct/100) + min_nominal_raise
    floor + handles zero/negative current.
  * mine_floor_raises_from_events end-to-end paths:
    - empty input → empty output
    - below threshold → empty output
    - threshold passed but weakest cat below threshold → empty
    - threshold + weakest cat hit → 1 proposal
    - same input twice → idempotent proposal_id
  * propose_floor_raises_from_events:
    - master flag off → empty (no ledger writes)
    - master on + qualifying input → 1 OK
    - re-mine → DUPLICATE_PROPOSAL_ID
  * Surface validator (registered at import):
    - kind != raise_floor → reject
    - non-sha256 hash → reject
    - observation_count below threshold → reject
    - summary missing → indicator → reject
    - all valid → pass
  * Authority invariants (AST grep): substrate+stdlib-only; no
    subprocess/network/env-mutation/LLM tokens.
"""
from __future__ import annotations

import ast as _ast
import math
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
from backend.core.ouroboros.governance.adaptation.exploration_floor_tightener import (
    BYPASS_FAILURE_OUTCOMES,
    DEFAULT_FLOOR_RAISE_PCT,
    DEFAULT_FLOOR_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    ExplorationOutcomeLite,
    MAX_FLOOR_RAISE_PCT,
    MIN_NOMINAL_RAISE,
    MinedFloorRaise,
    _filter_bypass_failures,
    _identify_weakest_category,
    compute_proposed_floor,
    get_floor_raise_pct,
    get_floor_threshold,
    get_window_days,
    install_surface_validator,
    is_enabled,
    mine_floor_raises_from_events,
    propose_floor_raises_from_events,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "exploration_floor_tightener.py"
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


def _ev(
    *,
    op_id="op-1",
    category_scores=None,
    floor_satisfied=True,
    verify_outcome="regression",
    timestamp_unix=None,
):
    return ExplorationOutcomeLite(
        op_id=op_id,
        category_scores=category_scores or {"read_file": 0.2, "search_code": 0.8},
        floor_satisfied=floor_satisfied,
        verify_outcome=verify_outcome,
        timestamp_unix=(
            timestamp_unix if timestamp_unix is not None else time.time()
        ),
    )


def _ledger(tmp_path):
    return AdaptationLedger(tmp_path / "ledger.jsonl")


# ===========================================================================
# A — Module constants + dataclasses + master flag
# ===========================================================================


def test_default_floor_threshold_pinned():
    assert DEFAULT_FLOOR_THRESHOLD == 5


def test_default_floor_raise_pct_pinned():
    assert DEFAULT_FLOOR_RAISE_PCT == 10


def test_default_window_days_pinned():
    assert DEFAULT_WINDOW_DAYS == 7


def test_min_nominal_raise_pinned():
    assert MIN_NOMINAL_RAISE == 1


def test_max_floor_raise_pct_pinned():
    assert MAX_FLOOR_RAISE_PCT == 100


def test_bypass_failure_outcomes_pinned():
    assert BYPASS_FAILURE_OUTCOMES == {"regression", "failed"}


def test_master_flag_default_true_post_graduation(monkeypatch):
    """Graduated 2026-04-29 (Move 1 Pass C cadence) — empty/unset env
    returns True. Asymmetric semantics: explicit falsy hot-reverts."""
    monkeypatch.delenv(
        "JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", raising=False,
    )
    assert is_enabled() is True


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", val)
        assert is_enabled() is True


def test_master_flag_falsy_variants(monkeypatch):
    # Post-graduation: empty/whitespace = unset = graduated default-true.
    # Only explicit falsy tokens hot-revert.
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", val)
        assert is_enabled() is False


def test_master_flag_empty_string_post_graduation(monkeypatch):
    """Asymmetric env semantics — explicit empty string is treated as
    unset and returns the graduated default-true."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "")
    assert is_enabled() is True


def test_exploration_outcome_lite_is_frozen():
    e = _ev()
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.op_id = "x"  # type: ignore[misc]


def test_mined_floor_raise_is_frozen():
    m = MinedFloorRaise(
        category="read_file", current_floor=1.0, proposed_floor=2.0,
        bypass_count=5, source_event_ids=("op-1",), summary="s",
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.proposed_floor = 3.0  # type: ignore[misc]


# ===========================================================================
# B — Env overrides
# ===========================================================================


def test_floor_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_THRESHOLD", "20")
    assert get_floor_threshold() == 20


def test_floor_threshold_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_THRESHOLD", "garbage")
    assert get_floor_threshold() == DEFAULT_FLOOR_THRESHOLD


def test_floor_threshold_env_zero_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_THRESHOLD", "0")
    assert get_floor_threshold() == DEFAULT_FLOOR_THRESHOLD


def test_floor_raise_pct_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_RAISE_PCT", "25")
    assert get_floor_raise_pct() == 25


def test_floor_raise_pct_env_clamped_to_max(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_RAISE_PCT", "500")
    assert get_floor_raise_pct() == MAX_FLOOR_RAISE_PCT


def test_floor_raise_pct_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_RAISE_PCT", "x")
    assert get_floor_raise_pct() == DEFAULT_FLOOR_RAISE_PCT


def test_floor_raise_pct_env_zero_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_RAISE_PCT", "0")
    assert get_floor_raise_pct() == DEFAULT_FLOOR_RAISE_PCT


def test_window_days_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_WINDOW_DAYS", "14")
    assert get_window_days() == 14


# ===========================================================================
# C — compute_proposed_floor (pure math)
# ===========================================================================


def test_compute_basic_10pct_raise_with_min_floor():
    """current=10, pct=10 → ceil(10 * 0.10)=1 → 10+1=11."""
    assert compute_proposed_floor(10.0, raise_pct=10) == 11.0


def test_compute_pct_with_min_nominal_raise_kicks_in():
    """current=1, pct=10 → ceil(1 * 0.10)=1 → still bumps by 1."""
    assert compute_proposed_floor(1.0, raise_pct=10) == 2.0


def test_compute_higher_pct_uses_real_raise():
    """current=10, pct=50 → ceil(10 * 0.50)=5 → 10+5=15."""
    assert compute_proposed_floor(10.0, raise_pct=50) == 15.0


def test_compute_zero_floor_returns_min_nominal_raise():
    assert compute_proposed_floor(0.0, raise_pct=10) == float(MIN_NOMINAL_RAISE)


def test_compute_negative_floor_treated_as_zero():
    """Defensive: negative input still bumps by min_nominal_raise."""
    assert compute_proposed_floor(-5.0, raise_pct=10) == float(MIN_NOMINAL_RAISE)


def test_compute_uses_env_pct_when_unspecified(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_RAISE_PCT", "20")
    # current=10, pct=20 → ceil(2.0)=2 → 12
    assert compute_proposed_floor(10.0) == 12.0


# ===========================================================================
# D — Bypass-failure filter
# ===========================================================================


def test_bypass_filter_keeps_satisfied_and_failed():
    events = [
        _ev(op_id="o1", floor_satisfied=True, verify_outcome="failed"),
        _ev(op_id="o2", floor_satisfied=True, verify_outcome="regression"),
        _ev(op_id="o3", floor_satisfied=True, verify_outcome="pass"),
        _ev(op_id="o4", floor_satisfied=False, verify_outcome="failed"),
    ]
    out = _filter_bypass_failures(events)
    assert {e.op_id for e in out} == {"o1", "o2"}


def test_bypass_filter_l2_recovered_excluded():
    """L2-recovered ops are NOT bypass failures (the cage caught
    it via L2). Pin: only verify_outcome IN {regression, failed}
    qualifies."""
    e = _ev(op_id="o1", floor_satisfied=True, verify_outcome="l2_recovered")
    assert _filter_bypass_failures([e]) == []


# ===========================================================================
# E — Weakest-category identification
# ===========================================================================


def test_weakest_category_per_op_argmin():
    """In each op, the LOWEST-scoring category is "weakest". The
    function then aggregates: which category was weakest most
    often."""
    events = [
        _ev(op_id="o1", category_scores={"a": 0.1, "b": 0.9}),
        _ev(op_id="o2", category_scores={"a": 0.2, "b": 0.8}),
        _ev(op_id="o3", category_scores={"a": 0.9, "b": 0.1}),
    ]
    res = _identify_weakest_category(events)
    assert res is not None
    cat, count, ids = res
    # 'a' wins 2/3 ops; 'b' wins 1/3.
    assert cat == "a"
    assert count == 2
    assert set(ids) == {"o1", "o2"}


def test_weakest_category_alpha_tie_break():
    """Equal counts → alphabetical category-name tie-break."""
    events = [
        _ev(op_id="o1", category_scores={"alpha": 0.1, "beta": 0.9}),
        _ev(op_id="o2", category_scores={"alpha": 0.9, "beta": 0.1}),
    ]
    # Counts are tied (1 each); alpha wins by name.
    res = _identify_weakest_category(events)
    assert res is not None
    assert res[0] == "alpha"


def test_weakest_category_empty_input_returns_none():
    assert _identify_weakest_category([]) is None


def test_weakest_category_skips_ops_with_no_scores():
    """An op with empty category_scores doesn't contribute."""
    events = [
        _ev(op_id="o1", category_scores={}),
        _ev(op_id="o2", category_scores={"a": 0.1, "b": 0.9}),
    ]
    res = _identify_weakest_category(events)
    assert res is not None
    assert res[1] == 1  # only o2 contributes


# ===========================================================================
# F — mine_floor_raises_from_events end-to-end
# ===========================================================================


def test_mine_empty_returns_empty():
    assert mine_floor_raises_from_events([], current_floors={}) == []


def test_mine_below_threshold_returns_empty():
    """Single bypass failure → way below default threshold (5)."""
    events = [_ev()]
    out = mine_floor_raises_from_events(events, current_floors={"read_file": 1.0})
    assert out == []


def test_mine_threshold_passes_but_weakest_below_threshold():
    """5 bypass failures total but weakest category appears in < 5 of them."""
    events = []
    for i in range(2):  # only 2 ops where 'a' is weakest
        events.append(_ev(
            op_id=f"o-a-{i}",
            category_scores={"a": 0.1, "b": 0.9},
        ))
    for i in range(3):  # 3 ops where 'b' is weakest
        events.append(_ev(
            op_id=f"o-b-{i}",
            category_scores={"a": 0.9, "b": 0.1},
        ))
    # Total 5, but neither category alone hits threshold=5.
    out = mine_floor_raises_from_events(
        events, current_floors={"a": 1.0, "b": 1.0}, threshold=5,
    )
    assert out == []


def test_mine_qualifies_proposes_one():
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(5)
    ]
    out = mine_floor_raises_from_events(
        events, current_floors={"weak": 10.0, "strong": 10.0},
        threshold=5,
    )
    assert len(out) == 1
    m = out[0]
    assert m.category == "weak"
    assert m.current_floor == 10.0
    assert m.proposed_floor == 11.0
    assert m.bypass_count == 5


def test_mine_skips_non_bypass_events():
    """Mix of bypass + non-bypass; only bypass count toward threshold."""
    events = []
    for i in range(5):
        events.append(_ev(
            op_id=f"bp-{i}",
            category_scores={"weak": 0.1, "strong": 0.9},
        ))
    # Add 10 non-bypass events with same category profile — should
    # NOT contribute.
    for i in range(10):
        events.append(_ev(
            op_id=f"pass-{i}",
            category_scores={"weak": 0.1, "strong": 0.9},
            verify_outcome="pass",
        ))
    out = mine_floor_raises_from_events(
        events, current_floors={"weak": 5.0, "strong": 5.0},
        threshold=5,
    )
    assert len(out) == 1
    assert out[0].bypass_count == 5  # only bypass-failures counted


def test_mine_window_filter_drops_old_events():
    now = time.time()
    old = [
        _ev(op_id=f"old-{i}", category_scores={"weak": 0.1, "strong": 0.9},
            timestamp_unix=now - (8 * 86_400))
        for i in range(5)
    ]
    fresh = [
        _ev(op_id=f"fresh-{i}", category_scores={"weak": 0.1, "strong": 0.9},
            timestamp_unix=now - (i * 60))
        for i in range(5)
    ]
    out = mine_floor_raises_from_events(
        old + fresh, current_floors={"weak": 1.0},
        threshold=5, window_days=7, now_unix=now,
    )
    # Only fresh count → exactly 5; old dropped
    assert len(out) == 1
    assert out[0].bypass_count == 5
    for src_id in out[0].source_event_ids:
        assert src_id.startswith("fresh-")


def test_mine_proposal_id_stable_across_calls():
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(5)
    ]
    out1 = mine_floor_raises_from_events(
        events, current_floors={"weak": 10.0}, threshold=5,
    )
    out2 = mine_floor_raises_from_events(
        events, current_floors={"weak": 10.0}, threshold=5,
    )
    assert out1[0].proposal_id() == out2[0].proposal_id()


def test_mine_proposal_id_differs_for_different_proposed_floor():
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(5)
    ]
    out_a = mine_floor_raises_from_events(
        events, current_floors={"weak": 10.0}, threshold=5,
    )
    out_b = mine_floor_raises_from_events(
        events, current_floors={"weak": 20.0}, threshold=5,
    )
    assert out_a[0].proposal_id() != out_b[0].proposal_id()


# ===========================================================================
# G — propose_floor_raises_from_events: ledger integration
# ===========================================================================


def test_propose_master_off_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "0")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(5)
    ]
    out = propose_floor_raises_from_events(
        events, ledger=led, current_floors={"weak": 10.0},
    )
    assert out == []
    assert not (tmp_path / "ledger.jsonl").exists()


def test_propose_master_on_writes_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(5)
    ]
    out = propose_floor_raises_from_events(
        events, ledger=led, current_floors={"weak": 10.0},
    )
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.surface is AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS
    assert p.proposal_kind == "raise_floor"
    assert "→" in p.evidence.summary  # raise indicator


def test_propose_idempotent_on_same_events(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(5)
    ]
    propose_floor_raises_from_events(events, ledger=led, current_floors={"weak": 10.0})
    second = propose_floor_raises_from_events(
        events, ledger=led, current_floors={"weak": 10.0},
    )
    assert len(second) == 1
    assert second[0].status is ProposeStatus.DUPLICATE_PROPOSAL_ID


def test_propose_evidence_observation_count_matches_bypass_count(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(7)
    ]
    out = propose_floor_raises_from_events(
        events, ledger=led, current_floors={"weak": 10.0},
    )
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.evidence.observation_count == 7


# ===========================================================================
# H — Surface validator
# ===========================================================================


def test_surface_validator_registered_at_import():
    v = get_surface_validator(AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS)
    assert v is not None


def _build_proposal(
    *,
    kind="raise_floor",
    proposed_hash="sha256:abc",
    observation_count=5,
    summary="weak floor 10 → 11",
):
    return AdaptationProposal(
        schema_version="1.0", proposal_id="p-test",
        surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
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


def test_validator_rejects_kind_other_than_raise_floor():
    p = _build_proposal(kind="add_pattern")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "kind_must_be_raise_floor" in detail


def test_validator_rejects_non_sha256_hash():
    p = _build_proposal(proposed_hash="plain_string")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "proposed_hash_format" in detail


def test_validator_rejects_observation_count_below_threshold(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_FLOOR_THRESHOLD", "10")
    p = _build_proposal(observation_count=5)
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "observation_count_below_threshold" in detail


def test_validator_rejects_summary_without_raise_indicator():
    p = _build_proposal(summary="this summary lacks the raise indicator")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "summary_missing_raise_indicator" in detail


def test_validator_passes_with_all_valid():
    p = _build_proposal()
    verdict, _ = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.PASSED


def test_install_surface_validator_idempotent():
    install_surface_validator()
    install_surface_validator()
    v = get_surface_validator(AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS)
    assert v is not None


# ===========================================================================
# I — Authority invariants (AST grep)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "semantic_guardian",
        "semantic_firewall",
        "scoped_tool_backend",
        ".gate.",
        "phase_runners",
        "providers",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
        elif isinstance(node, _ast.Import):
            for n in node.names:
                for sub in banned_substrings:
                    if sub in n.name:
                        found_banned.append((n.name, sub))
    assert not found_banned, (
        f"exploration_floor_tightener.py contains banned imports: "
        f"{found_banned}"
    )


def test_module_imports_only_substrate_and_stdlib():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    stdlib_prefixes = (
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
                any(mod == p or mod.startswith(p + ".") for p in stdlib_prefixes)
                or mod in allowed_governance
            )
            assert ok, f"unauthorized import {mod!r}"


def test_module_does_not_call_subprocess_or_network():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found


def test_module_does_not_call_llm():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden_tokens = (
        "messages.create(",
        "anthropic.Anthropic(",
        "ClaudeProvider(",
        "from openai",
    )
    found = [tok for tok in forbidden_tokens if tok in src]
    assert not found


# ===========================================================================
# J — Integration with substrate
# ===========================================================================


def test_full_pipeline_proposal_passes_substrate_validator(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"o-{i}", category_scores={"weak": 0.1, "strong": 0.9})
        for i in range(5)
    ]
    out = propose_floor_raises_from_events(
        events, ledger=led, current_floors={"weak": 10.0},
    )
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.monotonic_tightening_verdict is MonotonicTighteningVerdict.PASSED
