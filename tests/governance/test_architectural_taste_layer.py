"""Regression spine for §41.4 Phase 1 — Architectural Taste Layer."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    architectural_taste_layer as atl,
)
from backend.core.ouroboros.governance.architectural_taste_layer import (
    ARCHITECTURAL_TASTE_SCHEMA_VERSION,
    DimensionScore,
    TasteAssessment,
    TasteDimension,
    TasteProfile,
    TasteReport,
    TasteSignal,
    TasteVerdict,
    _ENV_EXCELLENT_THRESHOLD,
    _ENV_GIT_TIMEOUT_S,
    _ENV_GOOD_THRESHOLD,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_COMMITS,
    _ENV_MAX_FILES_PER_COMMIT,
    _ENV_MIN_PROFILE_COMMITS,
    _ENV_PERSIST,
    _ENV_POOR_THRESHOLD,
    _ENV_SIGNAL_TOLERANCE,
    _analyze_file_ast,
    _classify_identifier,
    _overall_signal_for_dimensions,
    _score_dimension,
    _verdict_for_average,
    assess_file,
    build_taste_profile,
    dimension_glyph,
    evaluate_change,
    excellent_threshold,
    format_taste_panel,
    git_timeout_s,
    good_threshold,
    ledger_path,
    master_enabled,
    max_commits_to_scan,
    max_files_per_commit,
    min_profile_commits,
    persistence_enabled,
    poor_threshold,
    register_flags,
    register_shipped_invariants,
    signal_glyph,
    signal_tolerance,
    verdict_glyph,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_MAX_COMMITS,
        _ENV_MIN_PROFILE_COMMITS, _ENV_EXCELLENT_THRESHOLD,
        _ENV_GOOD_THRESHOLD, _ENV_POOR_THRESHOLD,
        _ENV_SIGNAL_TOLERANCE, _ENV_GIT_TIMEOUT_S,
        _ENV_MAX_FILES_PER_COMMIT, _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"),
    )
    yield


# Defaults


def test_schema():
    assert ARCHITECTURAL_TASTE_SCHEMA_VERSION == "architectural_taste.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_max_commits_default():
    assert max_commits_to_scan() == 50


def test_min_profile_default():
    assert min_profile_commits() == 3


def test_excellent_threshold_default():
    assert excellent_threshold() == 0.75


def test_good_threshold_default():
    assert good_threshold() == 0.6


def test_poor_threshold_default():
    assert poor_threshold() == 0.4


def test_thresholds_auto_clamp(monkeypatch):
    monkeypatch.setenv(_ENV_EXCELLENT_THRESHOLD, "0.50")
    monkeypatch.setenv(_ENV_GOOD_THRESHOLD, "0.90")
    monkeypatch.setenv(_ENV_POOR_THRESHOLD, "0.95")
    # good > excellent → auto-clamped to excellent
    assert good_threshold() == 0.50
    # poor > good (clamped) → auto-clamped to good
    assert poor_threshold() == 0.50


def test_signal_tolerance_default():
    assert signal_tolerance() == 0.15


def test_git_timeout_default():
    assert git_timeout_s() == 15


def test_max_files_per_commit_default():
    assert max_files_per_commit() == 30


# Taxonomies


def test_verdict_taxonomy_closed():
    assert {v.value for v in TasteVerdict} == {
        "excellent", "good", "questionable", "poor",
    }


def test_signal_taxonomy_closed():
    assert {s.value for s in TasteSignal} == {
        "consistent", "novel", "drifting", "no_signal",
    }


def test_dimension_taxonomy_closed():
    assert {d.value for d in TasteDimension} == {
        "naming", "cohesion", "composition", "simplicity",
    }


@pytest.mark.parametrize("v", list(TasteVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(TasteSignal))
def test_signal_glyph(s):
    assert signal_glyph(s) != "?"


@pytest.mark.parametrize("d", list(TasteDimension))
def test_dimension_glyph(d):
    assert dimension_glyph(d) != "?"


# Identifier classification


def test_classify_snake_case():
    assert _classify_identifier("snake_case_name") is True


def test_classify_camel_case():
    assert _classify_identifier("camelCase") is False


def test_classify_pascal_case():
    assert _classify_identifier("PascalCase") is False


def test_classify_with_numbers():
    assert _classify_identifier("name_with_2") is True


def test_classify_empty():
    assert _classify_identifier("") is False


# AST analysis


def test_analyze_missing_file(tmp_path):
    result = _analyze_file_ast(
        file_path="nonexistent.py", repo_root=tmp_path,
    )
    assert result is None


def test_analyze_malformed_python(tmp_path):
    target = tmp_path / "bad.py"
    target.write_text("def broken(", encoding="utf-8")
    result = _analyze_file_ast(
        file_path="bad.py", repo_root=tmp_path,
    )
    assert result is None


def test_analyze_via_source_override():
    result = _analyze_file_ast(
        file_path="virtual.py",
        source_override="""
def my_function():
    return 42

import os
from .sibling import helper
""",
    )
    assert result is not None
    assert result["total_identifiers"] >= 1
    assert result["snake_case_identifiers"] >= 1
    assert result["import_count"] >= 2


def test_analyze_counts_imports():
    result = _analyze_file_ast(
        file_path="x.py",
        source_override="""
import os
import sys
from typing import List, Dict
""",
    )
    assert result["import_count"] == 3


def test_analyze_function_lengths():
    result = _analyze_file_ast(
        file_path="x.py",
        source_override="""
def short_fn():
    return 1


def longer_fn():
    x = 1
    y = 2
    z = 3
    return x + y + z
""",
    )
    assert len(result["function_lengths"]) == 2


# Build profile


def test_build_profile_empty_history():
    """No commits override → real git log walk. In sandbox this
    may return empty; substrate degrades gracefully."""
    profile = build_taste_profile(commits_override=[])
    assert profile.commit_count == 0
    assert profile.file_count == 0


def test_build_profile_with_commits(tmp_path):
    # Create a few test files
    (tmp_path / "a.py").write_text(
        "def my_function():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "def another_one():\n    import os\n    return os\n",
        encoding="utf-8",
    )
    commits = [
        ["a.py", "b.py"],
        ["a.py"],
    ]
    profile = build_taste_profile(
        repo_root=tmp_path, commits_override=commits,
    )
    assert profile.commit_count == 2
    assert profile.file_count >= 2
    assert profile.snake_case_ratio > 0


def test_build_profile_skips_non_python(tmp_path):
    (tmp_path / "x.md").write_text("# README", encoding="utf-8")
    (tmp_path / "a.py").write_text(
        "def fn():\n    pass\n", encoding="utf-8",
    )
    profile = build_taste_profile(
        repo_root=tmp_path,
        commits_override=[["x.md", "a.py"]],
    )
    # Only a.py is analyzed
    assert profile.file_count == 1


# Score dimension


def test_score_naming_perfect():
    score, signal, _ = _score_dimension(
        TasteDimension.NAMING, 0.9, 0.9, tolerance=0.15,
    )
    assert score >= 0.99
    assert signal is TasteSignal.CONSISTENT


def test_score_naming_drifting():
    score, signal, _ = _score_dimension(
        TasteDimension.NAMING, 0.3, 0.9, tolerance=0.15,
    )
    assert score < 0.5
    assert signal is TasteSignal.DRIFTING


def test_score_naming_novel():
    score, signal, _ = _score_dimension(
        TasteDimension.NAMING, 0.95, 0.5, tolerance=0.15,
    )
    assert signal is TasteSignal.NOVEL


def test_score_simplicity_lower_is_better():
    # Lower function length is simpler → score 1.0
    score, signal, _ = _score_dimension(
        TasteDimension.SIMPLICITY, 5.0, 20.0, tolerance=0.15,
    )
    assert score == 1.0


def test_score_simplicity_drift_when_higher():
    score, signal, _ = _score_dimension(
        TasteDimension.SIMPLICITY, 100.0, 10.0, tolerance=0.15,
    )
    assert score < 1.0
    assert signal is TasteSignal.DRIFTING


def test_score_no_signal_both_zero():
    score, signal, _ = _score_dimension(
        TasteDimension.NAMING, 0.0, 0.0, tolerance=0.15,
    )
    assert signal is TasteSignal.NO_SIGNAL


# Verdict assignment


def test_verdict_excellent():
    assert _verdict_for_average(0.85) is TasteVerdict.EXCELLENT


def test_verdict_good():
    assert _verdict_for_average(0.65) is TasteVerdict.GOOD


def test_verdict_questionable():
    assert _verdict_for_average(0.5) is TasteVerdict.QUESTIONABLE


def test_verdict_poor():
    assert _verdict_for_average(0.3) is TasteVerdict.POOR


# Overall signal


def test_overall_signal_empty():
    assert (
        _overall_signal_for_dimensions([])
        is TasteSignal.NO_SIGNAL
    )


def test_overall_signal_plurality():
    scores = [
        DimensionScore(
            dimension=TasteDimension.NAMING,
            score=1.0, raw_metric=0, profile_metric=0,
            signal=TasteSignal.CONSISTENT, diagnostic="",
        ),
        DimensionScore(
            dimension=TasteDimension.COHESION,
            score=1.0, raw_metric=0, profile_metric=0,
            signal=TasteSignal.CONSISTENT, diagnostic="",
        ),
        DimensionScore(
            dimension=TasteDimension.COMPOSITION,
            score=0.5, raw_metric=0, profile_metric=0,
            signal=TasteSignal.DRIFTING, diagnostic="",
        ),
        DimensionScore(
            dimension=TasteDimension.SIMPLICITY,
            score=0.5, raw_metric=0, profile_metric=0,
            signal=TasteSignal.NOVEL, diagnostic="",
        ),
    ]
    # 2 CONSISTENT, 1 DRIFTING, 1 NOVEL → plurality = CONSISTENT
    assert (
        _overall_signal_for_dimensions(scores)
        is TasteSignal.CONSISTENT
    )


def test_overall_signal_tie_returns_no_signal():
    scores = [
        DimensionScore(
            dimension=TasteDimension.NAMING,
            score=0.5, raw_metric=0, profile_metric=0,
            signal=TasteSignal.CONSISTENT, diagnostic="",
        ),
        DimensionScore(
            dimension=TasteDimension.COHESION,
            score=0.5, raw_metric=0, profile_metric=0,
            signal=TasteSignal.DRIFTING, diagnostic="",
        ),
    ]
    assert (
        _overall_signal_for_dimensions(scores)
        is TasteSignal.NO_SIGNAL
    )


# Assess file


def test_assess_empty_path():
    assert assess_file("") is None


def test_assess_via_source_override():
    profile = TasteProfile(
        commit_count=10, file_count=10,
        snake_case_ratio=0.9,
        avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )
    a = assess_file(
        "test.py",
        source_override="""
def my_function():
    return 1
""",
        profile=profile,
        siblings_count=1,
    )
    assert a is not None
    assert a.file_path == "test.py"
    assert len(a.dimension_scores) == 4


def test_assess_under_min_profile_returns_questionable(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_PROFILE_COMMITS, "10")
    profile = TasteProfile(
        commit_count=2, file_count=2,  # under min
        snake_case_ratio=0.9,
        avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )
    a = assess_file(
        "test.py",
        source_override="def fn(): pass\n",
        profile=profile,
    )
    assert a.verdict is TasteVerdict.QUESTIONABLE
    assert a.overall_signal is TasteSignal.NO_SIGNAL


def test_assess_with_llm_enricher():
    profile = TasteProfile(
        commit_count=10, file_count=10,
        snake_case_ratio=0.9,
        avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )

    def _enricher(baseline, source):
        # Return an enriched version with verdict = EXCELLENT
        # regardless of baseline.
        return TasteAssessment(
            file_path=baseline.file_path,
            verdict=TasteVerdict.EXCELLENT,
            overall_signal=baseline.overall_signal,
            dimension_scores=baseline.dimension_scores,
            average_score=0.95,
            boundary_crossed=baseline.boundary_crossed,
            llm_enriched=True,
            diagnostic="LLM said it's excellent",
        )

    a = assess_file(
        "test.py",
        source_override="def fn(): pass\n",
        profile=profile,
        llm_evaluator=_enricher,
    )
    assert a.verdict is TasteVerdict.EXCELLENT
    assert a.llm_enriched is True


def test_assess_llm_exception_falls_back_to_baseline():
    profile = TasteProfile(
        commit_count=10, file_count=10,
        snake_case_ratio=0.9,
        avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )

    def _broken_enricher(baseline, source):
        raise RuntimeError("LLM failed")

    a = assess_file(
        "test.py",
        source_override="def fn(): pass\n",
        profile=profile,
        llm_evaluator=_broken_enricher,
    )
    # Baseline assessment returned despite LLM exception
    assert a is not None
    assert a.llm_enriched is False


# evaluate_change


def test_evaluate_master_off():
    report = evaluate_change(["x.py"])
    assert report.master_enabled is False


def test_evaluate_empty_files(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_change([])
    assert report.overall_verdict is TasteVerdict.QUESTIONABLE
    assert report.assessments == ()


def test_evaluate_with_sources_override(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    profile = TasteProfile(
        commit_count=10, file_count=10,
        snake_case_ratio=0.9,
        avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )
    report = evaluate_change(
        ["a.py", "b.py"],
        sources_override={
            "a.py": "def my_fn():\n    return 1\n",
            "b.py": "def another():\n    return 2\n",
        },
        profile_override=profile,
    )
    assert len(report.assessments) == 2
    assert report.overall_verdict in TasteVerdict


def test_evaluate_with_commits_override(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_change(
        ["x.py"],
        sources_override={"x.py": "def fn():\n    pass\n"},
        commits_override=[],  # empty commit history
    )
    # Empty profile → under-sampled → QUESTIONABLE
    assert report.overall_verdict in (
        TasteVerdict.QUESTIONABLE, TasteVerdict.POOR,
    )


# Persistence


def test_persist_no_assessments_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    evaluate_change([])
    assert not ledger_path().exists()


def test_persist_writes_assessment(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    profile = TasteProfile(
        commit_count=10, file_count=10,
        snake_case_ratio=0.9, avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )
    evaluate_change(
        ["a.py"],
        sources_override={"a.py": "def fn(): pass\n"},
        profile_override=profile,
    )
    assert ledger_path().exists()


def test_persist_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    profile = TasteProfile(
        commit_count=10, file_count=10,
        snake_case_ratio=0.9, avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )
    evaluate_change(
        ["a.py"],
        sources_override={"a.py": "def fn(): pass\n"},
        profile_override=profile,
    )
    assert not ledger_path().exists()


# Renderer


def test_format_master_off():
    out = format_taste_panel()
    assert "disabled" in out


def test_format_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    profile = TasteProfile(
        commit_count=10, file_count=5,
        snake_case_ratio=0.9, avg_function_length=10.0,
        avg_imports_per_file=5.0,
        avg_sibling_import_ratio=0.5,
        avg_ast_nodes_per_file=100.0,
        diagnostic="",
    )
    report = evaluate_change(
        ["a.py"],
        sources_override={"a.py": "def fn(): pass\n"},
        profile_override=profile,
    )
    out = format_taste_panel(report)
    assert "Architectural Taste" in out
    assert "a.py" in out


# to_dict


def test_profile_to_dict():
    p = TasteProfile(
        commit_count=1, file_count=1,
        snake_case_ratio=0.5, avg_function_length=5.0,
        avg_imports_per_file=1.0,
        avg_sibling_import_ratio=0.0,
        avg_ast_nodes_per_file=10.0,
        diagnostic="",
    )
    d = p.to_dict()
    assert d["schema_version"] == ARCHITECTURAL_TASTE_SCHEMA_VERSION


def test_assessment_to_dict():
    a = TasteAssessment(
        file_path="x.py",
        verdict=TasteVerdict.GOOD,
        overall_signal=TasteSignal.CONSISTENT,
        dimension_scores=(),
        average_score=0.6,
        boundary_crossed=False,
        llm_enriched=False,
        diagnostic="",
    )
    d = a.to_dict()
    assert d["verdict"] == "good"


def test_report_to_dict():
    r = TasteReport(
        evaluated_at_unix=1.0, master_enabled=True,
        overall_verdict=TasteVerdict.GOOD,
        profile=None, assessments=(),
        diagnostic="", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["schema_version"] == ARCHITECTURAL_TASTE_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "architectural_taste_layer.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 6


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "signal_taxonomy_closed",
        "dimension_taxonomy_closed",
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


def test_pin_authority_forbids_plan_generator():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.plan_generator "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = "# no canonical surfaces\n"
    assert pin.validate(ast.parse(bad), bad)


# Flag registry


class _CapturingRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _CapturingRegistry()
    count = register_flags(reg)
    assert count == 10


def test_flag_master_default_false():
    reg = _CapturingRegistry()
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
    assert (
        ios.EVENT_TYPE_ARCHITECTURAL_TASTE_EVALUATED
        == "architectural_taste_evaluated"
    )
    assert (
        "architectural_taste_evaluated"
        in ios._VALID_EVENT_TYPES
    )
