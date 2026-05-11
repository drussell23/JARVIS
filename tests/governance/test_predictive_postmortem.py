"""Regression spine for §40 Wave 5 #18 — Predictive Postmortem.

Covers §33.1 default-FALSE, closed taxonomies, weighted score
formula, verdict transitions, dominant-factor classifier,
composition with Wave 4 #9/#11/#14, 5 AST pins, FlagRegistry
seeds, SSE event.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    predictive_postmortem as pp,
)
from backend.core.ouroboros.governance.predictive_postmortem import (
    PREDICTIVE_POSTMORTEM_SCHEMA_VERSION,
    ForecastVerdict,
    RiskFactor,
    RiskForecast,
    _ENV_BELIEF_WEIGHT,
    _ENV_CALIB_WEIGHT,
    _ENV_CRITICAL_THRESHOLD,
    _ENV_HIGH_THRESHOLD,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_META_WEIGHT,
    _ENV_MODERATE_THRESHOLD,
    _ENV_PERSIST,
    _classify_factor,
    _verdict_for_score,
    belief_weight,
    calibration_weight,
    critical_threshold,
    factor_glyph,
    forecast_postmortem_risk,
    format_forecast_panel,
    high_threshold,
    ledger_path,
    master_enabled,
    meta_weight,
    moderate_threshold,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    verdict_glyph,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST,
        _ENV_BELIEF_WEIGHT, _ENV_META_WEIGHT, _ENV_CALIB_WEIGHT,
        _ENV_MODERATE_THRESHOLD, _ENV_HIGH_THRESHOLD,
        _ENV_CRITICAL_THRESHOLD, _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "predictive.jsonl"),
    )
    yield


# Defaults / taxonomies


def test_schema():
    assert PREDICTIVE_POSTMORTEM_SCHEMA_VERSION == "predictive_postmortem.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_belief_weight_default():
    assert belief_weight() == 1.0


def test_meta_weight_default():
    assert meta_weight() == 1.5


def test_calibration_weight_default():
    assert calibration_weight() == 1.0


def test_moderate_threshold_default():
    assert moderate_threshold() == 0.25


def test_high_threshold_default():
    assert high_threshold() == 0.50


def test_critical_threshold_default():
    assert critical_threshold() == 0.75


def test_thresholds_auto_clamp(monkeypatch):
    monkeypatch.setenv(_ENV_MODERATE_THRESHOLD, "0.80")
    monkeypatch.setenv(_ENV_HIGH_THRESHOLD, "0.20")
    monkeypatch.setenv(_ENV_CRITICAL_THRESHOLD, "0.10")
    # high < moderate → auto-clamped to moderate
    assert high_threshold() == 0.80
    # critical < high → auto-clamped to high
    assert critical_threshold() == 0.80


def test_verdict_taxonomy_closed():
    assert {v.value for v in ForecastVerdict} == {
        "low", "moderate", "high", "critical",
    }


def test_factor_taxonomy_closed():
    assert {f.value for f in RiskFactor} == {
        "belief_drift", "meta_recurrence",
        "calibration_decay", "none",
    }


@pytest.mark.parametrize("v", list(ForecastVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("f", list(RiskFactor))
def test_factor_glyph(f):
    assert factor_glyph(f) != "?"


# Verdict + factor classifiers


def test_verdict_low_below_moderate():
    assert _verdict_for_score(0.10) is ForecastVerdict.LOW


def test_verdict_moderate():
    assert _verdict_for_score(0.30) is ForecastVerdict.MODERATE


def test_verdict_high():
    assert _verdict_for_score(0.55) is ForecastVerdict.HIGH


def test_verdict_critical():
    assert _verdict_for_score(0.85) is ForecastVerdict.CRITICAL


def test_factor_none_low_signals():
    assert (
        _classify_factor(0.01, 0.01, 0.01)
        is RiskFactor.NONE
    )


def test_factor_belief_dominant():
    assert (
        _classify_factor(0.8, 0.2, 0.1)
        is RiskFactor.BELIEF_DRIFT
    )


def test_factor_meta_dominant():
    assert (
        _classify_factor(0.2, 0.9, 0.1)
        is RiskFactor.META_RECURRENCE
    )


def test_factor_calibration_dominant():
    assert (
        _classify_factor(0.1, 0.2, 0.9)
        is RiskFactor.CALIBRATION_DECAY
    )


# forecast_postmortem_risk


def test_forecast_master_off_returns_low():
    report = forecast_postmortem_risk()
    assert report.master_enabled is False
    assert report.verdict is ForecastVerdict.LOW


def test_forecast_all_zero_yields_low(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = forecast_postmortem_risk(
        belief_drift_score=0.0,
        meta_recurrence_score=0.0,
        calibration_decay_score=0.0,
        falsified_count=0,
        fused_count=0,
        outcome_accuracy=1.0,
    )
    assert report.verdict is ForecastVerdict.LOW
    assert report.dominant_factor is RiskFactor.NONE


def test_forecast_high_belief_drift(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = forecast_postmortem_risk(
        belief_drift_score=1.0,
        meta_recurrence_score=0.0,
        calibration_decay_score=0.0,
        falsified_count=10,
        fused_count=0,
        outcome_accuracy=1.0,
    )
    # weighted = (1.0 + 0 + 0) / (1.0 + 1.5 + 1.0) = 0.286
    # → MODERATE (≥ 0.25)
    assert report.verdict is ForecastVerdict.MODERATE
    assert report.dominant_factor is RiskFactor.BELIEF_DRIFT


def test_forecast_high_meta_recurrence(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = forecast_postmortem_risk(
        belief_drift_score=0.0,
        meta_recurrence_score=1.0,
        calibration_decay_score=0.0,
        falsified_count=0,
        fused_count=5,
        outcome_accuracy=1.0,
    )
    # weighted = 1.5 / 3.5 = 0.429 → MODERATE
    assert report.verdict is ForecastVerdict.MODERATE
    assert report.dominant_factor is RiskFactor.META_RECURRENCE


def test_forecast_all_high_yields_critical(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = forecast_postmortem_risk(
        belief_drift_score=1.0,
        meta_recurrence_score=1.0,
        calibration_decay_score=1.0,
        falsified_count=20,
        fused_count=10,
        outcome_accuracy=0.0,
    )
    # weighted = (1.0 + 1.5 + 1.0) / 3.5 = 1.0 → CRITICAL
    assert report.verdict is ForecastVerdict.CRITICAL


def test_forecast_diagnostic_includes_components(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = forecast_postmortem_risk(
        belief_drift_score=0.5,
        meta_recurrence_score=0.5,
        calibration_decay_score=0.5,
        falsified_count=5,
        fused_count=3,
        outcome_accuracy=0.5,
    )
    assert "forecast=" in report.diagnostic
    assert "belief=" in report.diagnostic


def test_forecast_score_clamped_to_one(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = forecast_postmortem_risk(
        belief_drift_score=10.0,  # overflow
        meta_recurrence_score=10.0,
        calibration_decay_score=10.0,
        falsified_count=99,
        fused_count=99,
        outcome_accuracy=0.0,
    )
    assert report.forecast_score <= 1.0


# Composition with REAL Wave 4 substrates


def test_real_composition_master_off_yields_zero(monkeypatch):
    """All Wave 4 substrates master-off → score 0."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    # No injection — substrate composes real Wave 4 substrates,
    # all of which default master-OFF.
    report = forecast_postmortem_risk()
    assert report.verdict is ForecastVerdict.LOW


# Persistence


def test_persist_low_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    forecast_postmortem_risk(
        belief_drift_score=0.0,
        meta_recurrence_score=0.0,
        calibration_decay_score=0.0,
        falsified_count=0,
        fused_count=0,
        outcome_accuracy=1.0,
    )
    assert not ledger_path().exists()


def test_persist_high_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    forecast_postmortem_risk(
        belief_drift_score=1.0,
        meta_recurrence_score=1.0,
        calibration_decay_score=1.0,
        falsified_count=10,
        fused_count=5,
        outcome_accuracy=0.0,
    )
    assert ledger_path().exists()


def test_persist_disabled_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    forecast_postmortem_risk(
        belief_drift_score=1.0,
        meta_recurrence_score=1.0,
        calibration_decay_score=1.0,
        falsified_count=10,
        fused_count=5,
        outcome_accuracy=0.0,
    )
    assert not ledger_path().exists()


# Renderer


def test_format_panel_master_off():
    out = format_forecast_panel()
    assert "disabled" in out


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = forecast_postmortem_risk(
        belief_drift_score=0.5,
        meta_recurrence_score=0.5,
        calibration_decay_score=0.5,
        falsified_count=5, fused_count=3, outcome_accuracy=0.5,
    )
    out = format_forecast_panel(report)
    assert "Predictive Postmortem" in out


# to_dict


def test_report_to_dict_shape():
    r = RiskForecast(
        evaluated_at_unix=1.0, master_enabled=True,
        forecast_score=0.5, belief_drift_score=0.5,
        meta_recurrence_score=0.5, calibration_decay_score=0.5,
        verdict=ForecastVerdict.HIGH,
        dominant_factor=RiskFactor.BELIEF_DRIFT,
        falsified_belief_count=5, fused_meta_count=3,
        outcome_accuracy=0.5, diagnostic="x", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["verdict"] == "high"
    assert d["schema_version"] == PREDICTIVE_POSTMORTEM_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "predictive_postmortem.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "factor_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_verdict_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class ForecastVerdict(str, enum.Enum):\n"
        "    LOW = 'low'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_factor_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "factor_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class RiskFactor(str, enum.Enum):\n"
        "    BELIEF_DRIFT = 'belief_drift'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(ast.parse("# x\n"), "# x\n")


# Flags + SSE


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 8


def test_flag_master_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_PREDICTIVE_POSTMORTEM_FORECASTED
        == "predictive_postmortem_forecasted"
    )
    assert (
        "predictive_postmortem_forecasted"
        in ios._VALID_EVENT_TYPES
    )
