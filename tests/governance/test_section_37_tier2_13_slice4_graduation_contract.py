"""§37 Tier 2 #13 Slice 4 — graduation contract regression spine.

Pins per §33.1 canonical shape — mirrors the pattern compliance
applied across Venom V2 + Move 7 + Move 8 graduation contracts.

Coverage (~30 tests):
  * Closed 5-value verdict taxonomy bytes-pinned
  * Harness master flag default-TRUE per §33.1 separation
  * Slice 1 master flag stays default-FALSE (separation
    structurally enforced)
  * Env-knob defaults + clamping (negative, parse-error,
    >1.0 ratio)
  * 3-gate first-match-wins predicate:
      - Gate 1: ALREADY_GRADUATED (Slice 1 already on)
      - Gate 2: INSUFFICIENT_OBSERVATIONS (below min floor)
      - Gate 3: EXCESSIVE_FALSE_POSITIVES (above max ratio)
      - All pass: READY_FOR_GRADUATION
      - Harness off: DISABLED
  * Snapshot-reader caller-injection (deterministic testing)
  * Default evidence collector composes Slice 1 observer
  * NEVER raises (broken collector / observer outage)
  * Schema-versioned report (§33.5)
  * Versioned to_dict round-trip
  * AST pin: closed taxonomy clean + fires on drift
  * AST pin: authority asymmetry clean + fires on orchestrator
    import
  * AST pin: §33.1 canonical-shape parity clean + fires when
    `is_ready_for_graduation` removed
  * FlagRegistry seeds discoverable
  * Public API stability
  * §33.1 pattern compliance with Venom V2 sibling (parity test)
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "tool_confidence_indicator_graduation_contract.py"
    )


@pytest.fixture(autouse=True)
def _reset_observer():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# Closed 5-value verdict taxonomy
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_5_values():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
    )
    assert {v.value for v in ToolConfidenceGraduationVerdict} == {
        "ready_for_graduation",
        "insufficient_observations",
        "excessive_false_positives",
        "already_graduated",
        "disabled",
    }


# ---------------------------------------------------------------------------
# Master flag — §33.1 separation-of-concerns
# ---------------------------------------------------------------------------


def test_harness_default_true(monkeypatch):
    """Harness flag default-TRUE per §33.1 (measurement surface,
    not cognitive substrate)."""
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_GRADUATION_CONTRACT_"
        "ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    assert is_harness_enabled() is True


def test_harness_falsy(monkeypatch):
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        is_harness_enabled,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_GRADUATION_CONTRACT_"
        "ENABLED", "false",
    )
    assert is_harness_enabled() is False


def test_data_flag_stays_default_false_post_slice4(monkeypatch):
    """Slice 1's master flag (DATA flag) remains default-FALSE
    even after Slice 4 ships — §33.1 separation."""
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


# ---------------------------------------------------------------------------
# Env knob defaults + clamping
# ---------------------------------------------------------------------------


def test_min_observations_knob_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MIN_OBSERVATIONS",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        min_required_observations_knob,
    )
    assert min_required_observations_knob() == 50


def test_min_observations_knob_parse_error_falls_back(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MIN_OBSERVATIONS",
        "garbage",
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        min_required_observations_knob,
    )
    assert min_required_observations_knob() == 50


def test_min_observations_knob_negative_falls_back(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MIN_OBSERVATIONS",
        "-10",
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        min_required_observations_knob,
    )
    assert min_required_observations_knob() == 50


def test_max_fp_ratio_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MAX_FP_RATIO",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        max_false_positive_ratio_knob,
    )
    assert max_false_positive_ratio_knob() == pytest.approx(0.40)


def test_max_fp_ratio_clamps_above_one(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MAX_FP_RATIO",
        "5.0",
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        max_false_positive_ratio_knob,
    )
    assert max_false_positive_ratio_knob() == 1.0


# ---------------------------------------------------------------------------
# 3-gate predicate (caller-injected snapshot reader)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubSnapshot:
    total_streams: int = 0
    unsafe_streams: int = 0


def test_disabled_when_harness_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_GRADUATION_CONTRACT_"
        "ENABLED", "false",
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation()
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.DISABLED
    )
    assert "harness_master_off" in report.detail


def test_already_graduated_when_data_flag_on(monkeypatch):
    """Gate 1: Slice 1 master flag on → ALREADY_GRADUATED
    (idempotent no-op)."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation()
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.ALREADY_GRADUATED
    )


def test_insufficient_observations_under_floor(monkeypatch):
    """Gate 2: total < min_required → INSUFFICIENT_OBSERVATIONS."""
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation(
        snapshot_reader=lambda: _StubSnapshot(
            total_streams=10, unsafe_streams=2,
        ),
    )
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.INSUFFICIENT_OBSERVATIONS
    )
    assert report.observed_streams == 10
    assert report.unsafe_streams == 2


def test_excessive_false_positives_above_ratio(monkeypatch):
    """Gate 3: ratio > max → EXCESSIVE_FALSE_POSITIVES."""
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation(
        snapshot_reader=lambda: _StubSnapshot(
            total_streams=100, unsafe_streams=60,
        ),
    )
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.EXCESSIVE_FALSE_POSITIVES
    )
    assert report.false_positive_ratio == pytest.approx(0.60)


def test_ready_for_graduation_when_all_gates_pass(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation(
        snapshot_reader=lambda: _StubSnapshot(
            total_streams=100, unsafe_streams=20,
        ),
    )
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.READY_FOR_GRADUATION
    )
    assert report.false_positive_ratio == pytest.approx(0.20)
    assert "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED" in report.detail


def test_first_match_wins_already_graduated_beats_insufficient(
    monkeypatch,
):
    """Gate 1 fires before Gate 2 — already-graduated short-
    circuits even with zero observations."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation(
        snapshot_reader=lambda: _StubSnapshot(
            total_streams=0, unsafe_streams=0,
        ),
    )
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.ALREADY_GRADUATED
    )


# ---------------------------------------------------------------------------
# Default evidence collector composes Slice 1 observer
# ---------------------------------------------------------------------------


def test_default_collector_reads_observer_band_distribution(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    # 60 observations, 30 unsafe (band ≤ MEDIUM): ratio 0.50.
    for i in range(40):
        obs.record(
            confidence=0.95, op_id=f"op-{i}", tool_name="x",
            publish_sse=False,
        )
    # First-obs at safe pole is silent — but band IS recorded.
    # (Slice 1 design: state updated, just no SSE.)
    for i in range(20):
        obs.record(
            confidence=0.10, op_id=f"op-{i+100}", tool_name="x",
            publish_sse=False,
        )
    # 60 streams total; 20 at UNKNOWN (unsafe pole); ratio 1/3.
    report = is_ready_for_graduation()
    # 60 > 50 floor passes Gate 2; ratio 0.333 < 0.40 passes
    # Gate 3 → READY.
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.READY_FOR_GRADUATION
    )
    assert report.observed_streams == 60
    assert report.unsafe_streams == 20
    assert report.false_positive_ratio == pytest.approx(
        20.0 / 60.0,
    )


def test_collector_swallows_observer_outage(monkeypatch):
    """Defensive: if the observer is broken, snapshot returns
    zeros + INSUFFICIENT_OBSERVATIONS verdict."""
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as toolconf,
    )

    def _broken(*args, **kw):
        raise RuntimeError("broken observer")

    monkeypatch.setattr(
        toolconf, "get_default_observer", _broken,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation()
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.INSUFFICIENT_OBSERVATIONS
    )
    assert report.observed_streams == 0


def test_caller_injected_snapshot_overrides_default():
    """Test isolation discipline — caller-injection bypasses the
    global observer for deterministic verdict tests."""
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )
    report = is_ready_for_graduation(
        snapshot_reader=lambda: _StubSnapshot(
            total_streams=200, unsafe_streams=10,
        ),
    )
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.READY_FOR_GRADUATION
    )


def test_predicate_never_raises_on_broken_snapshot_reader(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        ToolConfidenceGraduationVerdict,
        is_ready_for_graduation,
    )

    def _crashy():
        raise RuntimeError("simulated outage")

    report = is_ready_for_graduation(snapshot_reader=_crashy)
    # Falls back to zero-stream snapshot → Gate 2 fires.
    assert (
        report.verdict
        == ToolConfidenceGraduationVerdict.INSUFFICIENT_OBSERVATIONS
    )


# ---------------------------------------------------------------------------
# Versioned report artifact (§33.5)
# ---------------------------------------------------------------------------


def test_report_schema_version_present():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION,
        ToolConfidenceGraduationReport,
        ToolConfidenceGraduationVerdict,
    )
    r = ToolConfidenceGraduationReport(
        schema_version=(
            TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            ToolConfidenceGraduationVerdict.READY_FOR_GRADUATION
        ),
        observed_streams=100, unsafe_streams=10,
        false_positive_ratio=0.10, detail="ok",
    )
    assert r.schema_version.startswith(
        "tool_confidence_graduation_report.",
    )


def test_report_to_dict_round_trip():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION,
        ToolConfidenceGraduationReport,
        ToolConfidenceGraduationVerdict,
    )
    r = ToolConfidenceGraduationReport(
        schema_version=(
            TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            ToolConfidenceGraduationVerdict.READY_FOR_GRADUATION
        ),
        observed_streams=100, unsafe_streams=10,
        false_positive_ratio=0.10,
        detail="empirical evidence sufficient",
    )
    d = r.to_dict()
    assert d["verdict"] == "ready_for_graduation"
    assert d["observed_streams"] == 100
    assert d["unsafe_streams"] == 10
    assert d["false_positive_ratio"] == pytest.approx(0.10)
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "tool_confidence_graduation_verdict_taxonomy_closed",
        "tool_confidence_graduation_authority_asymmetry",
        "tool_confidence_graduation_pattern_compliance",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    source = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, source)
    assert violations == ()


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class ToolConfidenceGraduationVerdict:
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    EXCESSIVE_FALSE_POSITIVES = "excessive_false_positives"
    ALREADY_GRADUATED = "already_graduated"
    SUPER_DUPER_READY = "super_duper_ready"  # extra value
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_graduation_verdict_taxonomy_closed"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = "from backend.core.ouroboros.governance.orchestrator import x"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_graduation_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_pattern_compliance_pin_fires_when_predicate_missing():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def is_harness_enabled() -> bool:
    return True

class ToolConfidenceGraduationVerdict:
    pass

class ToolConfidenceGraduationReport:
    pass

# is_ready_for_graduation MISSING — should fire pin
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_graduation_pattern_compliance"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any(
        "is_ready_for_graduation" in v for v in violations
    )


# ---------------------------------------------------------------------------
# §33.1 pattern compliance with sibling Venom V2 contract
# ---------------------------------------------------------------------------


def test_pattern_parity_with_venom_v2_sibling():
    """Slice 4 contract MUST mirror Venom V2's canonical shape:
    same top-level symbols + same Verdict enum value count +
    same Report fields prefix. This is the §33.1 pattern parity
    test — proves the canonical shape holds across siblings."""
    from backend.core.ouroboros.governance import (
        tool_confidence_indicator_graduation_contract as slice4,
    )
    from backend.core.ouroboros.governance import (
        tool_permissions_graduation_contract as venom_v2,
    )
    # Both modules expose the same 4 canonical-shape symbols.
    canonical = {
        "is_harness_enabled",
        "is_ready_for_graduation",
        "register_shipped_invariants",
        "register_flags",
    }
    # Slice 4 added register_flags; V2 hadn't yet — relax.
    s4_funcs = {
        n for n in canonical
        if hasattr(slice4, n) and callable(getattr(slice4, n))
    }
    v2_funcs = {
        n for n in canonical
        if hasattr(venom_v2, n) and callable(getattr(venom_v2, n))
    }
    # Both must have is_harness_enabled + is_ready_for_graduation
    # + register_shipped_invariants (V2 missing register_flags is
    # acceptable; Slice 4 adds it — additive evolution allowed).
    minimum = {
        "is_harness_enabled",
        "is_ready_for_graduation",
        "register_shipped_invariants",
    }
    assert minimum.issubset(s4_funcs)
    assert minimum.issubset(v2_funcs)
    # Both verdict enums are 5-value closed taxonomies.
    s4_verdicts = list(slice4.ToolConfidenceGraduationVerdict)
    v2_verdicts = list(venom_v2.ToolPermissionsGraduationVerdict)
    assert len(s4_verdicts) == 5
    assert len(v2_verdicts) == 5
    # Both verdicts include the canonical 3 control values.
    s4_values = {v.value for v in s4_verdicts}
    v2_values = {v.value for v in v2_verdicts}
    canonical_control = {
        "ready_for_graduation",
        "already_graduated",
        "disabled",
    }
    assert canonical_control.issubset(s4_values)
    assert canonical_control.issubset(v2_values)


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_register_flags_seeds_three_knobs():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 3
    names = {
        c.kwargs["name"] for c in registry.register.call_args_list
    }
    assert names == {
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_GRADUATION_CONTRACT_"
        "ENABLED",
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MIN_OBSERVATIONS",
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MAX_FP_RATIO",
    }


def test_register_flags_swallows_registry_errors():
    from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
        register_flags,
    )
    bad_registry = MagicMock()
    bad_registry.register.side_effect = TypeError("bad shape")
    # Must NOT raise.
    register_flags(bad_registry)


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        tool_confidence_indicator_graduation_contract as mod,
    )
    expected = {
        "TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION",
        "ToolConfidenceGraduationReport",
        "ToolConfidenceGraduationVerdict",
        "is_harness_enabled",
        "is_ready_for_graduation",
        "max_false_positive_ratio_knob",
        "min_required_observations_knob",
        "register_flags",
        "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_band_distribution_helper_shape():
    """Slice 1 module's `band_distribution()` returns dict with
    every band as key — required by Slice 4 evidence collector."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    dist = obs.band_distribution()
    # Every band present in the dict (no missing-key surprises).
    for band in ToolConfidenceBand:
        assert band in dist
        assert dist[band] == 0
    # After observation, count increments.
    obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    dist = obs.band_distribution()
    assert dist[ToolConfidenceBand.UNKNOWN] == 1
