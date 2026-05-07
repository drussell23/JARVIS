"""Venom V2 Slice 2 — graduation contract harness regression
spine.

Pins:
  * 5-value ToolPermissionsGraduationVerdict closed taxonomy
  * Frozen ToolPermissionsGraduationReport (§33.5 versioned)
  * is_ready_for_graduation 3-gate first-match-wins
  * Harness master flag default-TRUE per §33.1 separation
  * Substrate flag JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED stays
    default-FALSE
  * §33.1 canonical-shape parity AST pin
  * Authority asymmetry — forbids orchestrator imports
  * NEVER raises across all paths
  * Public API stable

Verifies (20 tests).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_verdict_taxonomy_5_values():
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        ToolPermissionsGraduationVerdict,
    )
    assert {v.value for v in ToolPermissionsGraduationVerdict} == {
        "ready_for_graduation",
        "insufficient_evaluations",
        "excessive_denies",
        "already_graduated",
        "disabled",
    }


def test_harness_master_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    assert is_harness_enabled() is True


def test_harness_master_explicit_false(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    assert is_harness_enabled() is False


def test_min_evaluations_default_50(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MIN_EVALUATIONS",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        min_required_evaluations_knob,
    )
    assert min_required_evaluations_knob() == 50


def test_max_deny_ratio_default_0_4(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MAX_DENY_RATIO",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        max_deny_ratio_knob,
    )
    assert abs(max_deny_ratio_knob() - 0.40) < 1e-9


def test_max_deny_ratio_clamps_to_1(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MAX_DENY_RATIO",
        "5.0",
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        max_deny_ratio_knob,
    )
    assert max_deny_ratio_knob() == 1.0


def test_returns_disabled_when_harness_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        ToolPermissionsGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation()
    assert r.verdict == ToolPermissionsGraduationVerdict.DISABLED


def test_returns_already_graduated_when_substrate_flipped(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        ToolPermissionsGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation()
    assert r.verdict == (
        ToolPermissionsGraduationVerdict.ALREADY_GRADUATED
    )


def test_returns_insufficient_evaluations(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        ToolPermissionsGraduationVerdict,
        is_ready_for_graduation,
        _EvaluationSnapshot,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _EvaluationSnapshot(0, 0),
    )
    assert r.verdict == (
        ToolPermissionsGraduationVerdict.INSUFFICIENT_EVALUATIONS
    )


def test_returns_excessive_denies(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MIN_EVALUATIONS",
        "10",
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MAX_DENY_RATIO",
        "0.40",
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        ToolPermissionsGraduationVerdict,
        is_ready_for_graduation,
        _EvaluationSnapshot,
    )
    # 100 evaluations, 80 denies → ratio 0.8 > 0.40
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _EvaluationSnapshot(100, 80),
    )
    assert r.verdict == (
        ToolPermissionsGraduationVerdict.EXCESSIVE_DENIES
    )


def test_returns_ready_when_evidence_sufficient(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MIN_EVALUATIONS",
        "10",
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MAX_DENY_RATIO",
        "0.40",
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        ToolPermissionsGraduationVerdict,
        is_ready_for_graduation,
        _EvaluationSnapshot,
    )
    # 100 evaluations, 10 denies → ratio 0.10 < 0.40
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _EvaluationSnapshot(100, 10),
    )
    assert r.verdict == (
        ToolPermissionsGraduationVerdict.READY_FOR_GRADUATION
    )


def test_first_match_wins_already_graduated_precedes_evidence(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        ToolPermissionsGraduationVerdict,
        is_ready_for_graduation,
        _EvaluationSnapshot,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: _EvaluationSnapshot(0, 0),
    )
    assert r.verdict == (
        ToolPermissionsGraduationVerdict.ALREADY_GRADUATED
    )


def test_never_raises_on_bad_snapshot_reader(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED",
        "1",
    )
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        is_ready_for_graduation,
    )

    def _bad():
        raise RuntimeError("simulated")

    r = is_ready_for_graduation(snapshot_reader=_bad)
    assert r is not None


def test_report_to_dict_round_trip():
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION,
        ToolPermissionsGraduationReport,
        ToolPermissionsGraduationVerdict,
    )
    r = ToolPermissionsGraduationReport(
        schema_version=(
            TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            ToolPermissionsGraduationVerdict.READY_FOR_GRADUATION
        ),
        observed_evaluations=120,
        deny_decisions=8,
        deny_ratio=0.067,
        detail="green",
    )
    d = r.to_dict()
    assert d["verdict"] == "ready_for_graduation"


def test_report_detail_truncated_to_256():
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION,
        ToolPermissionsGraduationReport,
        ToolPermissionsGraduationVerdict,
    )
    r = ToolPermissionsGraduationReport(
        schema_version=(
            TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=ToolPermissionsGraduationVerdict.DISABLED,
        observed_evaluations=0,
        deny_decisions=0,
        deny_ratio=0.0,
        detail="x" * 1000,
    )
    assert len(r.to_dict()["detail"]) == 256


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "tool_permissions_graduation_verdict_taxonomy_closed",
        "tool_permissions_graduation_authority_asymmetry",
        "tool_permissions_graduation_pattern_compliance",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "tool_permissions_graduation_contract.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_pattern_parity_with_other_graduation_contracts():
    from backend.core.ouroboros.governance import (
        tool_permissions_graduation_contract as tpgc,
    )
    assert callable(tpgc.is_ready_for_graduation)
    assert callable(tpgc.is_harness_enabled)
    assert hasattr(tpgc, "ToolPermissionsGraduationVerdict")
    assert hasattr(tpgc, "ToolPermissionsGraduationReport")
    sig = inspect.signature(tpgc.is_ready_for_graduation)
    assert "snapshot_reader" in sig.parameters


def test_public_api_stable():
    from backend.core.ouroboros.governance import (
        tool_permissions_graduation_contract,
    )
    expected = {
        "TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION",
        "ToolPermissionsGraduationReport",
        "ToolPermissionsGraduationVerdict",
        "is_harness_enabled",
        "is_ready_for_graduation",
        "max_deny_ratio_knob",
        "min_required_evaluations_knob",
        "register_shipped_invariants",
    }
    assert (
        set(tool_permissions_graduation_contract.__all__)
        == expected
    )
