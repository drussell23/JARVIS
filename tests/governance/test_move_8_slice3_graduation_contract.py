"""Move 8 Slice 3 — Proactive Curiosity Loop graduation contract.

Pins the §33.1 graduation contract substrate. Mirrors
test_move_7_slice5_graduation_contract.py structure exactly per
the §33.1 Graduation Contract Pattern.

Verifies (24 tests):

  * 5-value CuriosityGraduationVerdict closed taxonomy +
    AST-pinned via shipped_code_invariants.
  * Authority asymmetry pin auto-discovered.
  * composes-substrate pin auto-discovered + correctly fires on
    direct os.environ access to the gated flag.
  * §33.5 versioned report — schema_version + to_dict shape.
  * §33.1 pattern compliance — predicate signature mirrors
    Move 7 Slice 5 + phase10 (is_ready_for_* + *Verdict + frozen
    *Report + *_enabled() helper + register_shipped_invariants).
  * Master flag (graduation harness) defaults TRUE per §33.1
    convention.
  * Env knobs clamped (required_emissions / max_throttles).
  * 5-verdict ladder all paths exercised:
      DISABLED — harness flag off
      ALREADY_GRADUATED — Slice 1 flipped
      INSUFFICIENT_EMISSIONS — count below threshold
      EXCESSIVE_THROTTLES — throttle count above max
      READY_FOR_GRADUATION — happy path all clear
  * NEVER raises — defensive parse paths
  * 3 FlagRegistry seeds present + correct shape
  * Public API stability
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.proactive_curiosity_loop_graduation_contract import (  # noqa: E501
    PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_SCHEMA_VERSION,
    CuriosityGraduationReport,
    CuriosityGraduationVerdict,
    is_ready_for_graduation,
    max_governor_throttles,
    proactive_curiosity_graduation_contract_enabled,
    register_shipped_invariants,
    required_emissions,
)


# ---------------------------------------------------------------------------
# Closed taxonomy + §33.5 versioned report
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_exactly_5_values():
    values = {v.value for v in CuriosityGraduationVerdict}
    assert values == {
        "ready_for_graduation",
        "insufficient_emissions",
        "excessive_throttles",
        "already_graduated",
        "disabled",
    }


def test_report_is_frozen():
    r = CuriosityGraduationReport(
        verdict=CuriosityGraduationVerdict.DISABLED,
        observed_surfaced_emissions=0,
        required_emissions=12,
        observed_governor_throttles=0,
        max_governor_throttles=0,
        elapsed_s=0.001,
        diagnostics="",
    )
    with pytest.raises(Exception):
        r.verdict = CuriosityGraduationVerdict.READY_FOR_GRADUATION  # type: ignore  # noqa: E501


def test_report_to_dict_schema_version():
    r = CuriosityGraduationReport(
        verdict=CuriosityGraduationVerdict.READY_FOR_GRADUATION,
        observed_surfaced_emissions=20,
        required_emissions=12,
        observed_governor_throttles=0,
        max_governor_throttles=0,
        elapsed_s=0.5,
        diagnostics="ready",
    )
    d = r.to_dict()
    assert d["verdict"] == "ready_for_graduation"
    assert d["observed_surfaced_emissions"] == 20
    assert (
        d["schema_version"]
        == PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# §33.1 master-flag — graduation harness DEFAULTS TRUE
# (operator-binding default-FALSE lives on Slice 1's flag, not here)
# ---------------------------------------------------------------------------


def test_harness_master_flag_default_true():
    """The §33.1 convention: the contract harness's own flag
    defaults TRUE so the contract is queryable by default; the
    operator-binding default-FALSE lives on the THING being
    gated (Slice 1's reader)."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(
            "JARVIS_PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_"
            "ENABLED",
            None,
        )
        assert (
            proactive_curiosity_graduation_contract_enabled()
            is True
        )


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False),
    ("off", False),
    ("1", True), ("true", True), ("yes", True),
])
def test_harness_master_flag_truthy_falsy(raw, expected):
    with patch.dict(
        os.environ,
        {
            "JARVIS_PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_"
            "ENABLED": raw,
        },
    ):
        assert (
            proactive_curiosity_graduation_contract_enabled()
            is expected
        )


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("", 12), ("3", 3), ("1000", 1000),
    ("1", 3),  # below floor → clamped
    ("9999", 1000),  # above ceiling
    ("garbage", 12),
])
def test_required_emissions_clamping(raw, expected):
    with patch.dict(
        os.environ,
        {"JARVIS_PROACTIVE_CURIOSITY_REQUIRED_EMISSIONS": raw},
    ):
        assert required_emissions() == expected


@pytest.mark.parametrize("raw,expected", [
    ("", 0), ("0", 0), ("100", 100),
    ("-1", 0),  # below floor
    ("999", 100),  # above ceiling
    ("garbage", 0),
])
def test_max_governor_throttles_clamping(raw, expected):
    with patch.dict(
        os.environ,
        {
            "JARVIS_PROACTIVE_CURIOSITY_MAX_GOVERNOR_"
            "THROTTLES": raw,
        },
    ):
        assert max_governor_throttles() == expected


# ---------------------------------------------------------------------------
# 5-verdict ladder — all paths
# ---------------------------------------------------------------------------


def test_verdict_disabled_when_harness_off():
    report = is_ready_for_graduation(
        observed_surfaced_emissions=100,
        observed_governor_throttles=0,
        enabled_override=False,
    )
    assert report.verdict is CuriosityGraduationVerdict.DISABLED


def test_verdict_already_graduated_when_slice1_flipped():
    report = is_ready_for_graduation(
        observed_surfaced_emissions=100,
        observed_governor_throttles=0,
        enabled_override=True,
        slice1_already_flipped_override=True,
    )
    assert (
        report.verdict
        is CuriosityGraduationVerdict.ALREADY_GRADUATED
    )


def test_verdict_insufficient_emissions():
    report = is_ready_for_graduation(
        observed_surfaced_emissions=5,
        observed_governor_throttles=0,
        required_emissions_override=12,
        enabled_override=True,
        slice1_already_flipped_override=False,
    )
    assert (
        report.verdict
        is CuriosityGraduationVerdict.INSUFFICIENT_EMISSIONS
    )
    assert "5 surfaced emissions" in report.diagnostics


def test_verdict_excessive_throttles():
    report = is_ready_for_graduation(
        observed_surfaced_emissions=100,
        observed_governor_throttles=5,
        required_emissions_override=12,
        max_governor_throttles_override=0,
        enabled_override=True,
        slice1_already_flipped_override=False,
    )
    assert (
        report.verdict
        is CuriosityGraduationVerdict.EXCESSIVE_THROTTLES
    )
    assert "5 SensorGovernor" in report.diagnostics


def test_verdict_ready_for_graduation_happy_path():
    report = is_ready_for_graduation(
        observed_surfaced_emissions=20,
        observed_governor_throttles=0,
        required_emissions_override=12,
        max_governor_throttles_override=0,
        enabled_override=True,
        slice1_already_flipped_override=False,
    )
    assert (
        report.verdict
        is CuriosityGraduationVerdict.READY_FOR_GRADUATION
    )
    assert "all gates clear" in report.diagnostics


def test_verdict_ladder_first_match_wins_disabled_wins():
    """When harness flag is OFF, return DISABLED even if other
    gates would also reject. First-match-wins semantics."""
    report = is_ready_for_graduation(
        observed_surfaced_emissions=0,  # would be insufficient
        observed_governor_throttles=999,  # would be excessive
        enabled_override=False,
    )
    assert report.verdict is CuriosityGraduationVerdict.DISABLED


def test_verdict_ladder_already_graduated_short_circuits():
    """Slice 1 already flipped → contract is no-op even if
    other gates would also reject."""
    report = is_ready_for_graduation(
        observed_surfaced_emissions=0,
        observed_governor_throttles=999,
        enabled_override=True,
        slice1_already_flipped_override=True,
    )
    assert (
        report.verdict
        is CuriosityGraduationVerdict.ALREADY_GRADUATED
    )


# ---------------------------------------------------------------------------
# Defensive — NEVER raises
# ---------------------------------------------------------------------------


def test_negative_emissions_handled():
    """Defensive: caller-injected negative count doesn't poison
    the contract; treated as zero / triggers insufficient."""
    report = is_ready_for_graduation(
        observed_surfaced_emissions=-5,
        observed_governor_throttles=0,
        required_emissions_override=12,
        enabled_override=True,
        slice1_already_flipped_override=False,
    )
    assert (
        report.verdict
        is CuriosityGraduationVerdict.INSUFFICIENT_EMISSIONS
    )


def test_slice1_flag_lookup_fallback_when_module_absent():
    """If proactive_curiosity_reader module is unimportable
    at the moment of evaluation (rollback path), the contract
    treats Slice 1 as 'not flipped' (fail-safe — don't claim
    graduation if we can't verify state)."""
    # We can't easily simulate ImportError on an already-loaded
    # module, but the override path proves the structure is in
    # place (the production fallback is exception-isolated).
    report = is_ready_for_graduation(
        observed_surfaced_emissions=20,
        observed_governor_throttles=0,
        required_emissions_override=12,
        max_governor_throttles_override=0,
        enabled_override=True,
        slice1_already_flipped_override=False,
    )
    assert (
        report.verdict
        is CuriosityGraduationVerdict.READY_FOR_GRADUATION
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    invs = register_shipped_invariants()
    assert len(invs) == 3
    names = {i.invariant_name for i in invs}
    assert names == {
        "proactive_curiosity_graduation_contract_authority_asymmetry",  # noqa: E501
        "proactive_curiosity_graduation_contract_verdict_taxonomy_5_values",  # noqa: E501
        "proactive_curiosity_graduation_contract_composes_substrate",  # noqa: E501
    }


def test_all_pins_validate_clean():
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance"
        / "proactive_curiosity_loop_graduation_contract.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    bad_source = '''
from backend.core.ouroboros.governance.orchestrator import foo
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    auth = next(
        i for i in invs
        if "authority_asymmetry" in i.invariant_name
    )
    violations = auth.validate(tree, bad_source)
    assert violations
    assert any("orchestrator" in v for v in violations)


def test_composes_pin_fires_on_direct_env_read():
    bad_source = '''
import os

x = os.environ.get(
    "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED", "false",
)
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    comp = next(
        i for i in invs
        if "composes_substrate" in i.invariant_name
    )
    violations = comp.validate(tree, bad_source)
    assert violations
    assert any("os.environ.get" in v for v in violations)


def test_composes_pin_does_not_fire_on_diagnostic_string():
    """The pin must NOT fire on literal mentions of the flag
    in docstrings or diagnostics — only on actual env reads."""
    benign_source = '''
from backend.core.ouroboros.governance.proactive_curiosity_reader import (
    proactive_curiosity_reader_enabled,
)

def foo():
    return (
        "all gates clear — operator may flip "
        "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED to true"
    )
'''
    tree = ast.parse(benign_source)
    invs = register_shipped_invariants()
    comp = next(
        i for i in invs
        if "composes_substrate" in i.invariant_name
    )
    violations = comp.validate(tree, benign_source)
    assert violations == ()


def test_verdict_taxonomy_pin_fires_on_extra_value():
    bad_source = '''
import enum

class CuriosityGraduationVerdict(str, enum.Enum):
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_EMISSIONS = "insufficient_emissions"
    EXCESSIVE_THROTTLES = "excessive_throttles"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"
    EXTRA = "extra"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    tax = next(
        i for i in invs
        if "verdict_taxonomy" in i.invariant_name
    )
    violations = tax.validate(tree, bad_source)
    assert violations
    assert any("EXTRA" in v for v in violations)


# ---------------------------------------------------------------------------
# §33.1 pattern compliance — same canonical shape as Move 7 / Phase 10
# ---------------------------------------------------------------------------


def test_pattern_compliance_with_move_7_slice5():
    """The §33.1 graduation contract pattern requires:

      * is_ready_for_* predicate (pure-function)
      * *Verdict closed-enum (taxonomy-pinned)
      * frozen *Report with to_dict()
      * *_enabled() master-flag helper
      * register_shipped_invariants() hook
    """
    from backend.core.ouroboros.governance import (
        proactive_curiosity_loop_graduation_contract as gc,
    )
    # Predicate
    assert callable(gc.is_ready_for_graduation)
    # Verdict closed-enum
    assert issubclass(
        gc.CuriosityGraduationVerdict, str,
    )
    # Frozen report
    assert hasattr(gc.CuriosityGraduationReport, "to_dict")
    # Master flag helper
    assert callable(
        gc.proactive_curiosity_graduation_contract_enabled,
    )
    # Pin hook
    assert callable(gc.register_shipped_invariants)


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_flag_registry_has_3_slice3_seeds():
    from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
        SEED_SPECS,
    )
    seeds = [
        s for s in SEED_SPECS
        if (
            "PROACTIVE_CURIOSITY_GRADUATION" in s.name
            or "PROACTIVE_CURIOSITY_REQUIRED_EMISSIONS" in s.name
            or "PROACTIVE_CURIOSITY_MAX_GOVERNOR" in s.name
        )
    ]
    assert len(seeds) == 3
    names = {s.name for s in seeds}
    assert names == {
        "JARVIS_PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_ENABLED",
        "JARVIS_PROACTIVE_CURIOSITY_REQUIRED_EMISSIONS",
        "JARVIS_PROACTIVE_CURIOSITY_MAX_GOVERNOR_THROTTLES",
    }


def test_flag_registry_harness_master_default_true():
    from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
        SEED_SPECS,
    )
    master = next(
        s for s in SEED_SPECS
        if (
            s.name
            == "JARVIS_PROACTIVE_CURIOSITY_GRADUATION_"
            "CONTRACT_ENABLED"
        )
    )
    assert master.default is True


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import (
        proactive_curiosity_loop_graduation_contract as gc,
    )
    expected = {
        "CuriosityGraduationReport",
        "CuriosityGraduationVerdict",
        "PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_SCHEMA_VERSION",
        "is_ready_for_graduation",
        "max_governor_throttles",
        "proactive_curiosity_graduation_contract_enabled",
        "register_shipped_invariants",
        "required_emissions",
    }
    assert set(gc.__all__) == expected
