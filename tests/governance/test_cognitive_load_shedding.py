"""Regression spine for §40 Wave 5 #21 — Cognitive Load Shedding.

Covers §33.1 default-FALSE, closed taxonomies, verdict +
shed-kind matrix, weighted score, threshold auto-clamp,
composition with Wave 4 #13 + Wave 5 #18, persistence, 5 AST
pins, FlagRegistry seeds, SSE event.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    cognitive_load_shedding as cls,
)
from backend.core.ouroboros.governance.cognitive_load_shedding import (
    COGNITIVE_LOAD_SHEDDING_SCHEMA_VERSION,
    LoadShedReport,
    LoadVerdict,
    ShedKind,
    _ENV_ELEVATED_THRESHOLD,
    _ENV_FORECAST_WEIGHT,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_OVERLOADED_THRESHOLD,
    _ENV_PERSIST,
    _ENV_STRESS_WEIGHT,
    _shed_for_verdict,
    _verdict_for_score,
    elevated_threshold,
    evaluate_cognitive_load,
    forecast_weight,
    format_load_panel,
    ledger_path,
    master_enabled,
    overloaded_threshold,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    shed_glyph,
    stress_weight,
    verdict_glyph,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_STRESS_WEIGHT,
        _ENV_FORECAST_WEIGHT, _ENV_ELEVATED_THRESHOLD,
        _ENV_OVERLOADED_THRESHOLD, _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "load_shed.jsonl"),
    )
    yield


# Defaults


def test_schema():
    assert COGNITIVE_LOAD_SHEDDING_SCHEMA_VERSION == "cognitive_load_shedding.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_stress_weight_default():
    assert stress_weight() == 1.0


def test_forecast_weight_default():
    assert forecast_weight() == 1.0


def test_elevated_default():
    assert elevated_threshold() == 0.30


def test_overloaded_default():
    assert overloaded_threshold() == 0.65


def test_overloaded_auto_clamps_above_elevated(monkeypatch):
    monkeypatch.setenv(_ENV_ELEVATED_THRESHOLD, "0.80")
    monkeypatch.setenv(_ENV_OVERLOADED_THRESHOLD, "0.20")
    assert overloaded_threshold() == 0.80


# Taxonomies


def test_verdict_taxonomy_closed():
    assert {v.value for v in LoadVerdict} == {
        "normal", "elevated", "overloaded", "disabled",
    }


def test_shed_taxonomy_closed():
    assert {s.value for s in ShedKind} == {
        "no_shed", "speculative_shed", "background_shed",
        "full_shed",
    }


@pytest.mark.parametrize("v", list(LoadVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(ShedKind))
def test_shed_glyph(s):
    assert shed_glyph(s) != "?"


# Verdict + shed classifiers


def test_verdict_normal_low_score():
    assert _verdict_for_score(0.10) is LoadVerdict.NORMAL


def test_verdict_elevated():
    assert _verdict_for_score(0.40) is LoadVerdict.ELEVATED


def test_verdict_overloaded():
    assert _verdict_for_score(0.80) is LoadVerdict.OVERLOADED


def test_shed_for_normal_is_no_shed():
    assert _shed_for_verdict(LoadVerdict.NORMAL) is ShedKind.NO_SHED


def test_shed_for_elevated_is_speculative():
    assert (
        _shed_for_verdict(LoadVerdict.ELEVATED)
        is ShedKind.SPECULATIVE_SHED
    )


def test_shed_for_overloaded_is_full():
    assert (
        _shed_for_verdict(LoadVerdict.OVERLOADED)
        is ShedKind.FULL_SHED
    )


# evaluate_cognitive_load


def test_evaluate_master_off_disabled():
    report = evaluate_cognitive_load()
    assert report.master_enabled is False
    assert report.verdict is LoadVerdict.DISABLED
    assert report.shed_kind is ShedKind.NO_SHED


def test_evaluate_all_zero_normal(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_cognitive_load(
        stress_score_override=0.0,
        forecast_score_override=0.0,
        forecast_verdict_override="low",
        stressed_count_override=0,
        exhausted_count_override=0,
    )
    assert report.verdict is LoadVerdict.NORMAL
    assert report.shed_kind is ShedKind.NO_SHED


def test_evaluate_elevated(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_cognitive_load(
        stress_score_override=0.40,
        forecast_score_override=0.40,
        forecast_verdict_override="moderate",
        stressed_count_override=2,
        exhausted_count_override=0,
    )
    assert report.verdict is LoadVerdict.ELEVATED
    assert report.shed_kind is ShedKind.SPECULATIVE_SHED


def test_evaluate_overloaded(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_cognitive_load(
        stress_score_override=0.80,
        forecast_score_override=0.80,
        forecast_verdict_override="critical",
        stressed_count_override=5,
        exhausted_count_override=3,
    )
    assert report.verdict is LoadVerdict.OVERLOADED
    assert report.shed_kind is ShedKind.FULL_SHED


def test_evaluate_weights_change_score(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FORECAST_WEIGHT, "5.0")
    # forecast dominates
    report = evaluate_cognitive_load(
        stress_score_override=0.0,
        forecast_score_override=0.80,
        forecast_verdict_override="critical",
        stressed_count_override=0,
        exhausted_count_override=0,
    )
    # (0×1 + 0.80×5)/6 = 0.667 → OVERLOADED
    assert report.verdict is LoadVerdict.OVERLOADED


def test_evaluate_real_composition(monkeypatch):
    """All Wave 4/5 substrates master-off → NORMAL."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_cognitive_load(modules=["dummy"])
    # All component substrates are master-off → score 0
    assert report.verdict is LoadVerdict.NORMAL


def test_evaluate_diagnostic_includes_components(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_cognitive_load(
        stress_score_override=0.5,
        forecast_score_override=0.5,
        forecast_verdict_override="high",
        stressed_count_override=2,
        exhausted_count_override=1,
    )
    assert "load=" in report.diagnostic
    assert "stress=" in report.diagnostic


# Persistence


def test_persist_normal_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    evaluate_cognitive_load(
        stress_score_override=0.0,
        forecast_score_override=0.0,
        forecast_verdict_override="low",
        stressed_count_override=0,
        exhausted_count_override=0,
    )
    assert not ledger_path().exists()


def test_persist_overloaded_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    evaluate_cognitive_load(
        stress_score_override=0.9,
        forecast_score_override=0.9,
        forecast_verdict_override="critical",
        stressed_count_override=5,
        exhausted_count_override=3,
    )
    assert ledger_path().exists()


def test_persist_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    evaluate_cognitive_load(
        stress_score_override=0.9,
        forecast_score_override=0.9,
        forecast_verdict_override="critical",
        stressed_count_override=5,
        exhausted_count_override=3,
    )
    assert not ledger_path().exists()


# Renderer


def test_format_panel_master_off():
    assert "disabled" in format_load_panel()


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_cognitive_load(
        stress_score_override=0.5,
        forecast_score_override=0.5,
        forecast_verdict_override="high",
        stressed_count_override=2,
        exhausted_count_override=1,
    )
    out = format_load_panel(report)
    assert "Cognitive Load" in out


# to_dict


def test_report_to_dict_shape():
    r = LoadShedReport(
        evaluated_at_unix=1.0, master_enabled=True,
        load_score=0.5, stress_score=0.5, forecast_score=0.5,
        verdict=LoadVerdict.ELEVATED,
        shed_kind=ShedKind.SPECULATIVE_SHED,
        stressed_count=2, exhausted_count=1,
        forecast_verdict="moderate",
        diagnostic="x", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["verdict"] == "elevated"
    assert d["shed_kind"] == "speculative_shed"
    assert d["schema_version"] == COGNITIVE_LOAD_SHEDDING_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "cognitive_load_shedding.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "shed_taxonomy_closed",
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
        "class LoadVerdict(str, enum.Enum):\n"
        "    NORMAL = 'normal'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_sensor_governor_forbidden():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.sensor_governor "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


# Flags + SSE


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 6


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
        ios.EVENT_TYPE_COGNITIVE_LOAD_SHED_TRIGGERED
        == "cognitive_load_shed_triggered"
    )
    assert (
        "cognitive_load_shed_triggered"
        in ios._VALID_EVENT_TYPES
    )
