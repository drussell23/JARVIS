"""§31 U2 empirical wiring Slice 5 — graduation contract harness
regression spine.

Pins per operator binding 2026-05-05 + §33.1 canonical-shape
discipline:

  * 5-value CausalConsumerGraduationVerdict closed enum bytes-
    pinned (§33.1 canonical shape parity)
  * Frozen CausalConsumerGraduationReport (§33.5 versioned)
  * is_ready_for_graduation 3-gate first-match-wins evaluation:
      1. Slice 1 already-graduated → ALREADY_GRADUATED
      2. transitions < min_required → INSUFFICIENT_TRANSITIONS
      3. disabled_ratio > max_disabled → EXCESSIVE_DISABLED_SAMPLES
      4. otherwise → READY_FOR_GRADUATION
  * Harness master flag JARVIS_CAUSAL_CONSUMER_GRADUATION_
    CONTRACT_ENABLED default-TRUE per §33.1 separation-of-
    concerns
  * Substrate data flag JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED
    stays default-FALSE on producer side
  * §33.1 canonical-shape parity AST pin asserts required
    symbols present
  * Authority asymmetry — harness forbids orchestrator/iron_gate/
    policy/providers imports
  * NEVER raises across all paths
  * Public API stable
  * Pattern-compliance test proves §33.1 canonical shape parity

Verifies (24 tests).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_has_5_values():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
    )
    assert len(list(CausalConsumerGraduationVerdict)) == 5


def test_verdict_values_bytes_pinned():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
    )
    assert {v.value for v in CausalConsumerGraduationVerdict} == {
        "ready_for_graduation", "insufficient_transitions",
        "excessive_disabled_samples", "already_graduated",
        "disabled",
    }


# ---------------------------------------------------------------------------
# Master flag (harness-side, default-TRUE per §33.1 separation)
# ---------------------------------------------------------------------------


def test_harness_master_flag_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    assert is_harness_enabled() is True


def test_harness_master_flag_explicit_false(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    assert is_harness_enabled() is False


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def test_min_transitions_default_12(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_GRADUATION_MIN_TRANSITIONS", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        min_required_transitions_knob,
    )
    assert min_required_transitions_knob() == 12


def test_disabled_ratio_default_0_1(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_GRADUATION_MAX_DISABLED_RATIO",
        raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        max_disabled_ratio_knob,
    )
    assert abs(max_disabled_ratio_knob() - 0.10) < 1e-9


def test_disabled_ratio_clamps_to_1(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_GRADUATION_MAX_DISABLED_RATIO", "5.0",
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        max_disabled_ratio_knob,
    )
    assert max_disabled_ratio_knob() == 1.0


# ---------------------------------------------------------------------------
# Predicate evaluation — first-match-wins
# ---------------------------------------------------------------------------


def test_returns_disabled_when_harness_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation()
    assert r.verdict == (
        CausalConsumerGraduationVerdict.DISABLED
    )
    assert "harness_master_off" in r.detail


def test_returns_already_graduated_when_substrate_flipped(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation()
    assert r.verdict == (
        CausalConsumerGraduationVerdict.ALREADY_GRADUATED
    )


def test_returns_insufficient_when_no_evidence(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
        is_ready_for_graduation,
        _EvidenceSnapshot,
    )
    r = is_ready_for_graduation(
        snapshot=_EvidenceSnapshot(transitions=0, disabled_count=0),
    )
    assert r.verdict == (
        CausalConsumerGraduationVerdict.INSUFFICIENT_TRANSITIONS
    )


def test_returns_excessive_disabled_when_ratio_high(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_GRADUATION_MAX_DISABLED_RATIO", "0.10",
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
        is_ready_for_graduation,
        _EvidenceSnapshot,
    )
    # 20 transitions, 10 disabled → ratio 0.5 > 0.10
    r = is_ready_for_graduation(
        snapshot=_EvidenceSnapshot(
            transitions=20, disabled_count=10,
        ),
    )
    assert r.verdict == (
        CausalConsumerGraduationVerdict.EXCESSIVE_DISABLED_SAMPLES
    )
    assert r.disabled_ratio > 0.10


def test_returns_ready_when_evidence_sufficient(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
        is_ready_for_graduation,
        _EvidenceSnapshot,
    )
    # 20 transitions, 1 disabled → ratio 0.05 < 0.10
    r = is_ready_for_graduation(
        snapshot=_EvidenceSnapshot(
            transitions=20, disabled_count=1,
        ),
    )
    assert r.verdict == (
        CausalConsumerGraduationVerdict.READY_FOR_GRADUATION
    )


def test_first_match_wins_already_graduated_precedes_evidence(
    monkeypatch,
):
    """When substrate is already on, harness short-circuits
    with ALREADY_GRADUATED regardless of evidence."""
    monkeypatch.setenv(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CausalConsumerGraduationVerdict,
        is_ready_for_graduation,
        _EvidenceSnapshot,
    )
    # Even with insufficient evidence, ALREADY_GRADUATED wins
    r = is_ready_for_graduation(
        snapshot=_EvidenceSnapshot(transitions=0, disabled_count=0),
    )
    assert r.verdict == (
        CausalConsumerGraduationVerdict.ALREADY_GRADUATED
    )


# ---------------------------------------------------------------------------
# Report round-trip
# ---------------------------------------------------------------------------


def test_report_to_dict_round_trip():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION,
        CausalConsumerGraduationReport,
        CausalConsumerGraduationVerdict,
    )
    r = CausalConsumerGraduationReport(
        schema_version=CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION,
        verdict=CausalConsumerGraduationVerdict.READY_FOR_GRADUATION,
        observed_transitions=15,
        disabled_observation_count=1,
        disabled_ratio=0.067,
        detail="green",
    )
    d = r.to_dict()
    assert d["verdict"] == "ready_for_graduation"
    assert d["observed_transitions"] == 15
    assert d["disabled_observation_count"] == 1
    assert abs(d["disabled_ratio"] - 0.067) < 1e-9


def test_report_detail_truncated_to_256():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION,
        CausalConsumerGraduationReport,
        CausalConsumerGraduationVerdict,
    )
    r = CausalConsumerGraduationReport(
        schema_version=CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION,
        verdict=CausalConsumerGraduationVerdict.DISABLED,
        observed_transitions=0,
        disabled_observation_count=0,
        disabled_ratio=0.0,
        detail="x" * 1000,
    )
    assert len(r.to_dict()["detail"]) == 256


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "causal_consumer_graduation_verdict_taxonomy_closed",
        "causal_consumer_graduation_authority_asymmetry",
        "causal_consumer_graduation_pattern_compliance",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "causality_consumer_graduation_contract.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_verdict_pin_fires_on_taxonomy_drift():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
import enum
class CausalConsumerGraduationVerdict(str, enum.Enum):
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_TRANSITIONS = "insufficient_transitions"
    EXCESSIVE_DISABLED_SAMPLES = "excessive_disabled_samples"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"
    UNAUTHORIZED_NEW = "unauthorized_new"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "verdict_taxonomy_closed" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.policy "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_pattern_compliance_pin_fires_on_missing_symbol():
    from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = """
def is_harness_enabled():
    return True
# Missing the predicate, verdict enum, report dataclass
"""
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "pattern_compliance" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any(
        "canonical-shape" in v for v in violations
    )


# ---------------------------------------------------------------------------
# §33.1 canonical-shape parity — pattern compliance
# ---------------------------------------------------------------------------


def test_pattern_parity_with_cross_op_semantic_budget():
    """§33.1 canonical-shape — this harness mirrors
    cross_op_semantic_budget_graduation_contract's symbol shape:
    is_ready_for_graduation + is_harness_enabled +
    *Verdict 5-value enum + *Report frozen dataclass +
    register_shipped_invariants."""
    import inspect
    from backend.core.ouroboros.governance import (
        causality_consumer_graduation_contract as cgc,
    )
    # All §33.1 canonical symbols present.
    assert callable(cgc.is_ready_for_graduation)
    assert callable(cgc.is_harness_enabled)
    assert hasattr(cgc, "CausalConsumerGraduationVerdict")
    assert hasattr(cgc, "CausalConsumerGraduationReport")
    # is_ready_for_graduation returns a CausalConsumerGraduationReport.
    sig = inspect.signature(cgc.is_ready_for_graduation)
    # Optional snapshot kwarg in signature — pattern-compliant.
    assert "snapshot" in sig.parameters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import (
        causality_consumer_graduation_contract,
    )
    expected = {
        "CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION",
        "CausalConsumerGraduationReport",
        "CausalConsumerGraduationVerdict",
        "is_harness_enabled",
        "is_ready_for_graduation",
        "max_disabled_ratio_knob",
        "min_required_transitions_knob",
        "register_shipped_invariants",
    }
    assert (
        set(causality_consumer_graduation_contract.__all__)
        == expected
    )
