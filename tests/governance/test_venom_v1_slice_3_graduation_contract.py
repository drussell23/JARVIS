"""Venom V1 Slice 3 — graduation contract harness regression
spine.

Pins per operator binding 2026-05-06 + §33.1 canonical-shape:

  * 5-value ToolHooksGraduationVerdict closed taxonomy
  * Frozen ToolHooksGraduationReport (§33.5 versioned)
  * is_ready_for_graduation 3-gate first-match-wins:
      1. ALREADY_GRADUATED — substrate flag flipped
      2. INSUFFICIENT_FIRES — observed < min_required
      3. EXCESSIVE_FAILURES — failure_ratio > max
      4. otherwise → READY_FOR_GRADUATION
  * Harness master flag JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_
    ENABLED default-TRUE per §33.1 separation
  * Substrate flag JARVIS_VENOM_TOOL_HOOKS_ENABLED default-FALSE
  * §33.1 canonical-shape parity AST pin asserts required
    symbols
  * Authority asymmetry — forbids orchestrator/iron_gate imports
  * NEVER raises across all paths
  * Public API stable
  * Pattern parity with cross_op_semantic_budget contract

Verifies (22 tests).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_5_values():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
    )
    assert len(list(ToolHooksGraduationVerdict)) == 5


def test_verdict_values_bytes_pinned():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
    )
    assert {v.value for v in ToolHooksGraduationVerdict} == {
        "ready_for_graduation",
        "insufficient_fires",
        "excessive_failures",
        "already_graduated",
        "disabled",
    }


# ---------------------------------------------------------------------------
# Master flags
# ---------------------------------------------------------------------------


def test_harness_master_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    assert is_harness_enabled() is True


def test_harness_master_explicit_false(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    assert is_harness_enabled() is False


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def test_min_fires_default_50(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_MIN_FIRES",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        min_required_fires_knob,
    )
    assert min_required_fires_knob() == 50


def test_max_failure_ratio_default_0_2(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_MAX_FAILURE_RATIO",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        max_failure_ratio_knob,
    )
    assert abs(max_failure_ratio_knob() - 0.20) < 1e-9


def test_max_failure_ratio_clamps_to_1(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_MAX_FAILURE_RATIO",
        "5.0",
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        max_failure_ratio_knob,
    )
    assert max_failure_ratio_knob() == 1.0


# ---------------------------------------------------------------------------
# Verdict ladder
# ---------------------------------------------------------------------------


def test_returns_disabled_when_harness_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation()
    assert r.verdict == ToolHooksGraduationVerdict.DISABLED
    assert "harness_master_off" in r.detail


def test_returns_already_graduated_when_substrate_flipped(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation()
    assert r.verdict == (
        ToolHooksGraduationVerdict.ALREADY_GRADUATED
    )


def test_returns_insufficient_when_no_evidence(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED", "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
        is_ready_for_graduation,
        _FireSnapshot,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _FireSnapshot(0, 0),
    )
    assert r.verdict == (
        ToolHooksGraduationVerdict.INSUFFICIENT_FIRES
    )


def test_returns_excessive_failures_when_ratio_high(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED", "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_MIN_FIRES", "10",
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_MAX_FAILURE_RATIO",
        "0.20",
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
        is_ready_for_graduation,
        _FireSnapshot,
    )
    # 100 fires, 50 failures → ratio 0.5 > 0.20
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _FireSnapshot(100, 50),
    )
    assert r.verdict == (
        ToolHooksGraduationVerdict.EXCESSIVE_FAILURES
    )


def test_returns_ready_when_evidence_sufficient(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED", "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_MIN_FIRES", "10",
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_MAX_FAILURE_RATIO",
        "0.20",
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
        is_ready_for_graduation,
        _FireSnapshot,
    )
    # 100 fires, 5 failures → ratio 0.05 < 0.20
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _FireSnapshot(100, 5),
    )
    assert r.verdict == (
        ToolHooksGraduationVerdict.READY_FOR_GRADUATION
    )


def test_first_match_wins_already_graduated_precedes_evidence(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        ToolHooksGraduationVerdict,
        is_ready_for_graduation,
        _FireSnapshot,
    )
    # Even with insufficient evidence, ALREADY_GRADUATED wins
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _FireSnapshot(0, 0),
    )
    assert r.verdict == (
        ToolHooksGraduationVerdict.ALREADY_GRADUATED
    )


# ---------------------------------------------------------------------------
# Defensive — never raises
# ---------------------------------------------------------------------------


def test_never_raises_on_bad_snapshot_reader(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED", "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        is_ready_for_graduation,
    )

    def _bad():
        raise RuntimeError("simulated")

    r = is_ready_for_graduation(snapshot_reader=_bad)
    # Returns sane default, doesn't raise
    assert r is not None


# ---------------------------------------------------------------------------
# Round-trip + projection
# ---------------------------------------------------------------------------


def test_report_to_dict_round_trip():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION,
        ToolHooksGraduationReport,
        ToolHooksGraduationVerdict,
    )
    r = ToolHooksGraduationReport(
        schema_version=(
            TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            ToolHooksGraduationVerdict.READY_FOR_GRADUATION
        ),
        observed_fires=120,
        failure_fires=8,
        failure_ratio=0.067,
        detail="green",
    )
    d = r.to_dict()
    assert d["verdict"] == "ready_for_graduation"
    assert d["observed_fires"] == 120


def test_report_detail_truncated_to_256():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION,
        ToolHooksGraduationReport,
        ToolHooksGraduationVerdict,
    )
    r = ToolHooksGraduationReport(
        schema_version=(
            TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=ToolHooksGraduationVerdict.DISABLED,
        observed_fires=0,
        failure_fires=0,
        failure_ratio=0.0,
        detail="x" * 1000,
    )
    assert len(r.to_dict()["detail"]) == 256


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "tool_hooks_graduation_verdict_taxonomy_closed",
        "tool_hooks_graduation_authority_asymmetry",
        "tool_hooks_graduation_pattern_compliance",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "tool_hooks_graduation_contract.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_verdict_pin_fires_on_drift():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
import enum
class ToolHooksGraduationVerdict(str, enum.Enum):
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_FIRES = "insufficient_fires"
    EXCESSIVE_FAILURES = "excessive_failures"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"
    UNAUTHORIZED = "unauthorized"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "verdict_taxonomy_closed" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
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
    from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = """
def is_harness_enabled():
    return True
# Missing predicate, verdict enum, report dataclass
"""
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "pattern_compliance" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# §33.1 canonical-shape parity — compliance test
# ---------------------------------------------------------------------------


def test_pattern_parity_with_other_graduation_contracts():
    """§33.1 canonical-shape — this harness mirrors every other
    graduation contract's symbol shape: is_ready_for_graduation
    + is_harness_enabled + *Verdict 5-value enum + *Report
    frozen dataclass + register_shipped_invariants."""
    from backend.core.ouroboros.governance import (
        tool_hooks_graduation_contract as thc,
    )
    assert callable(thc.is_ready_for_graduation)
    assert callable(thc.is_harness_enabled)
    assert hasattr(thc, "ToolHooksGraduationVerdict")
    assert hasattr(thc, "ToolHooksGraduationReport")
    sig = inspect.signature(thc.is_ready_for_graduation)
    assert "snapshot_reader" in sig.parameters


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import (
        tool_hooks_graduation_contract,
    )
    expected = {
        "TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION",
        "ToolHooksGraduationReport",
        "ToolHooksGraduationVerdict",
        "is_harness_enabled",
        "is_ready_for_graduation",
        "max_failure_ratio_knob",
        "min_required_fires_knob",
        "register_shipped_invariants",
    }
    assert (
        set(tool_hooks_graduation_contract.__all__)
        == expected
    )
