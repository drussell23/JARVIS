"""Regression spine for §40 Wave 5 #22 — Meta-Prior Learning.

Covers:
* §33.1 default-FALSE
* Closed 4-value MetaPriorVerdict + LearningStage
* Aggregator + verdict matrix
* Composes Wave 4 #12 Schelling history ledger
* 5 AST pins (canonical pass + synthetic)
* FlagRegistry seeds + SSE event
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    meta_prior_learning as mpl,
)
from backend.core.ouroboros.governance.meta_prior_learning import (
    META_PRIOR_LEARNING_SCHEMA_VERSION,
    LearningStage,
    MetaPriorReport,
    MetaPriorVerdict,
    PriorMetaStats,
    _ENV_DECLINING_TREND,
    _ENV_DOMINANT_RATE,
    _ENV_EMERGING_TREND,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_PRIORS,
    _ENV_PERSIST,
    _ENV_RECENT_WINDOW_S,
    _stage_for_sample,
    _verdict_for_stats,
    compute_meta_distribution,
    declining_trend_threshold,
    dominant_rate_threshold,
    emerging_trend_threshold,
    format_meta_prior_panel,
    master_enabled,
    max_priors,
    recent_window_s,
    register_flags,
    register_shipped_invariants,
    stage_glyph,
    verdict_glyph,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST,
        _ENV_RECENT_WINDOW_S, _ENV_EMERGING_TREND,
        _ENV_DECLINING_TREND, _ENV_DOMINANT_RATE,
        _ENV_MAX_PRIORS, _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "meta_prior.jsonl"),
    )
    yield


def _row(prior_kind: str, accepted: bool, t: float = 10.0) -> dict:
    return {
        "kind": "prior_outcome",
        "prior_kind": prior_kind,
        "was_accepted": accepted,
        "observed_at_unix": t,
    }


# Defaults / taxonomies


def test_schema():
    assert META_PRIOR_LEARNING_SCHEMA_VERSION == "meta_prior_learning.1"


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_verdict_taxonomy_closed():
    assert {v.value for v in MetaPriorVerdict} == {
        "dormant", "emerging", "dominant", "declining",
    }


def test_stage_taxonomy_closed():
    assert {s.value for s in LearningStage} == {
        "cold_start", "bootstrap", "steady", "saturated",
    }


def test_recent_window_default():
    assert recent_window_s() == 86_400


def test_emerging_trend_default():
    assert emerging_trend_threshold() == 0.10


def test_declining_trend_default():
    assert declining_trend_threshold() == -0.10


def test_dominant_rate_default():
    assert dominant_rate_threshold() == 0.75


def test_max_priors_default():
    assert max_priors() == 20


@pytest.mark.parametrize("v", list(MetaPriorVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(LearningStage))
def test_stage_glyph(s):
    assert stage_glyph(s) != "?"


# Stage classifier


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, LearningStage.COLD_START),
        (9, LearningStage.COLD_START),
        (10, LearningStage.BOOTSTRAP),
        (49, LearningStage.BOOTSTRAP),
        (50, LearningStage.STEADY),
        (499, LearningStage.STEADY),
        (500, LearningStage.SATURATED),
    ],
)
def test_stage_for_sample(n, expected):
    assert _stage_for_sample(n) is expected


# Verdict classifier


def test_verdict_dormant_zero_sample():
    assert (
        _verdict_for_stats(0, 0.0, 0.0, 0.0)
        is MetaPriorVerdict.DORMANT
    )


def test_verdict_dominant_high_rate_positive_trend(monkeypatch):
    assert (
        _verdict_for_stats(20, 0.80, 0.70, 0.10)
        is MetaPriorVerdict.DOMINANT
    )


def test_verdict_emerging_positive_trend():
    assert (
        _verdict_for_stats(20, 0.40, 0.20, 0.20)
        is MetaPriorVerdict.EMERGING
    )


def test_verdict_declining_negative_trend():
    assert (
        _verdict_for_stats(20, 0.20, 0.50, -0.30)
        is MetaPriorVerdict.DECLINING
    )


def test_verdict_dormant_low_signal():
    assert (
        _verdict_for_stats(20, 0.30, 0.30, 0.0)
        is MetaPriorVerdict.DORMANT
    )


# compute_meta_distribution


def test_compute_master_off_returns_disabled():
    report = compute_meta_distribution(rows=[])
    assert report.master_enabled is False


def test_compute_empty_history(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = compute_meta_distribution(rows=[])
    assert len(report.per_prior) == 0


def test_compute_aggregates_per_prior(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rows = (
        [_row("A", True) for _ in range(8)]
        + [_row("A", False) for _ in range(2)]
        + [_row("B", False) for _ in range(10)]
    )
    report = compute_meta_distribution(rows=rows, now_unix=100.0)
    by_kind = {s.prior_kind: s for s in report.per_prior}
    assert by_kind["A"].total_sample == 10
    assert by_kind["A"].win_rate_historic == 0.8
    assert by_kind["B"].total_sample == 10
    assert by_kind["B"].win_rate_historic == 0.0


def test_compute_dominant_when_high_rate(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # 8/10 recent + 8/10 historic; rate > 0.75 + trend == 0
    rows = [
        _row("A", i < 8, t=99.0) for i in range(10)
    ]
    report = compute_meta_distribution(rows=rows, now_unix=100.0)
    a = next(s for s in report.per_prior if s.prior_kind == "A")
    assert a.verdict is MetaPriorVerdict.DOMINANT


def test_compute_emerging_rising_trend(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # Window 60s (minimum) so old rows fall outside it.
    monkeypatch.setenv(_ENV_RECENT_WINDOW_S, "60")
    # Historic: 2/10 accepts (old, t=10 outside 60s window).
    # Recent: 8/10 accepts (t=80, inside window).
    old = [_row("A", i < 2, t=10.0) for i in range(10)]
    recent = [_row("A", i < 8, t=80.0) for i in range(10)]
    rows = old + recent
    report = compute_meta_distribution(
        rows=rows, now_unix=100.0,
    )
    a = next(s for s in report.per_prior if s.prior_kind == "A")
    assert a.trend > 0
    # Could be EMERGING or DOMINANT depending on rates
    assert a.verdict in (
        MetaPriorVerdict.EMERGING, MetaPriorVerdict.DOMINANT,
    )


def test_compute_declining_falling_trend(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECENT_WINDOW_S, "60")
    # Historic: 8/10. Recent: 1/10. Window 60s so old (t=10) is outside.
    old = [_row("A", i < 8, t=10.0) for i in range(10)]
    recent = [_row("A", i < 1, t=80.0) for i in range(10)]
    rows = old + recent
    report = compute_meta_distribution(
        rows=rows, now_unix=100.0,
    )
    a = next(s for s in report.per_prior if s.prior_kind == "A")
    assert a.trend < 0
    assert a.verdict is MetaPriorVerdict.DECLINING


def test_compute_max_priors_cap(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MAX_PRIORS, "2")
    rows = [_row(f"P-{i}", True) for i in range(5)]
    report = compute_meta_distribution(rows=rows, now_unix=100.0)
    assert len(report.per_prior) <= 2


def test_compute_skips_malformed_rows(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rows = [
        {"kind": "wrong"},
        _row("A", True),
        {"kind": "prior_outcome", "prior_kind": ""},
    ]
    report = compute_meta_distribution(rows=rows, now_unix=100.0)
    assert len(report.per_prior) == 1


def test_compute_diagnostic_includes_counts(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rows = [_row("A", True) for _ in range(20)]
    report = compute_meta_distribution(rows=rows, now_unix=100.0)
    assert "dominant" in report.diagnostic.lower()


# Persistence


def test_persist_dominant_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    rows = [_row("A", True, t=99.0) for _ in range(20)]
    compute_meta_distribution(rows=rows, now_unix=100.0)
    p = mpl.ledger_path()
    assert p.exists()


def test_persist_dormant_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    rows = [_row("A", False) for _ in range(5)]
    compute_meta_distribution(rows=rows, now_unix=100.0)
    # All dormant → no persist
    assert not mpl.ledger_path().exists()


def test_persist_disabled_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    rows = [_row("A", True, t=99.0) for _ in range(20)]
    compute_meta_distribution(rows=rows, now_unix=100.0)
    assert not mpl.ledger_path().exists()


# Renderer


def test_format_panel_master_off():
    out = format_meta_prior_panel()
    assert "disabled" in out


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rows = [_row("A", True) for _ in range(5)]
    report = compute_meta_distribution(rows=rows, now_unix=100.0)
    out = format_meta_prior_panel(report)
    assert "Meta-Prior Learning" in out


# to_dict


def test_stats_to_dict():
    s = PriorMetaStats(
        prior_kind="A", total_sample=10, recent_sample=5,
        win_rate_historic=0.5, win_rate_recent=0.6,
        trend=0.10, verdict=MetaPriorVerdict.EMERGING,
        stage=LearningStage.BOOTSTRAP,
    )
    d = s.to_dict()
    assert d["verdict"] == "emerging"
    assert d["schema_version"] == META_PRIOR_LEARNING_SCHEMA_VERSION


def test_report_to_dict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = compute_meta_distribution(rows=[], now_unix=100.0)
    d = report.to_dict()
    assert d["schema_version"] == META_PRIOR_LEARNING_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "meta_prior_learning.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "stage_taxonomy_closed",
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
        "class MetaPriorVerdict(str, enum.Enum):\n"
        "    DORMANT = 'dormant'\n"
        "    EMERGING = 'emerging'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_stage_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "stage_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class LearningStage(str, enum.Enum):\n"
        "    COLD = 'cold'\n"
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


def test_pin_master():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad = (
        "def master_enabled():\n"
        "    return _flag('X', default=True)\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(ast.parse("# x\n"), "# x\n")


# Flags


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 7


def test_flag_master_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


# SSE


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert ios.EVENT_TYPE_META_PRIOR_LEARNED == "meta_prior_learned"
    assert "meta_prior_learned" in ios._VALID_EVENT_TYPES
