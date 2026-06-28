"""Slice 48 — Semantic Target Stratification regression spine.

Pins (shared scoring substrate + OpportunityMiner wiring):
  §1  file_has_test_coverage — global AST-aware resolver (suffix + AST-import)
  §1a  exact test_<stem>.py still detected
  §1b  suffix variant test_<stem>_slice4.py detected (was the bug)
  §1c  test in nested subdirectory detected
  §1d  AST-importing test detected via Strategy 2
  §1e  genuinely-untested file → False
  §1f  Advisor _compute_test_coverage returns 1.0 for suffix-tested file
  §2  penalty multiplier — covered file is never penalized (== 1.0)
  §3  penalty multiplier — suppress=True bypasses penalty (test-gen escape)
  §4  penalty multiplier — huge zero-coverage file is heavily down-ranked
  §5  penalty multiplier — small zero-coverage file is barely touched
  §6  penalty multiplier — monotonic in line-count; clamped to [1-alpha, 1]
  §7  penalty multiplier — env defaults (ALPHA / MAX_LINES) honoured
  §8  _FileAnalysis.stratification_penalty / stratified_score wiring
  §9  Advisor delegates _compute_test_coverage to the shared definition
  §10 _select_diverse_candidates down-ranks huge-untested vs small-tested
  §11 _analyze_file stamps has_test_coverage from repo_root (None → safe 1.0)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.target_stratification import (
    DEFAULT_PENALTY_ALPHA,
    DEFAULT_PENALTY_MAX_LINES,
    _strat_ast_cache,
    file_has_test_coverage,
    stratification_penalty_multiplier,
)


# ── §1a exact test_<stem>.py detected (unchanged behavior) ──────────────
def test_file_has_test_coverage_detects_test_file(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_widget.py").write_text("def test_x(): pass\n")
    assert file_has_test_coverage("backend/core/widget.py", tmp_path) is True


# ── §1e genuinely-untested file → False ─────────────────────────────────
def test_file_has_test_coverage_false_when_absent(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    assert file_has_test_coverage("backend/core/widget.py", tmp_path) is False


# ── §1b suffix variant test_<stem>_slice4.py detected (was the bug) ─────
def test_file_has_test_coverage_suffix_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """repl_input_polish.py style: test lives as test_<stem>_slice4.py in a subdir."""
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "false")
    (tmp_path / "tests" / "battle_test").mkdir(parents=True)
    (tmp_path / "tests" / "battle_test" / "test_repl_input_polish_slice4.py").write_text(
        "def test_x(): pass\n"
    )
    assert file_has_test_coverage(
        "backend/core/ouroboros/battle_test/repl_input_polish.py", tmp_path
    ) is True


# ── §1c test in nested subdirectory detected ─────────────────────────────
def test_file_has_test_coverage_nested_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """test_<stem>.py anywhere under a test root is sufficient for coverage."""
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "false")
    (tmp_path / "tests" / "governance" / "autonomy").mkdir(parents=True)
    (tmp_path / "tests" / "governance" / "autonomy" / "test_widget.py").write_text(
        "def test_y(): pass\n"
    )
    assert file_has_test_coverage("backend/core/widget.py", tmp_path) is True


# ── §1d AST-importing test (non-conventional name) detected ─────────────
def test_file_has_test_coverage_ast_import_detects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test file that AST-imports the module counts as coverage even when
    neither exact nor suffix filename conventions are matched."""
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "true")
    # Ensure no suffix-match file exists so only AST path fires.
    (tmp_path / "tests").mkdir()
    test_content = (
        "from backend.core.special_util import some_func\n"
        "def test_it(): assert some_func() is not None\n"
    )
    (tmp_path / "tests" / "test_integration_suite.py").write_text(test_content)
    # Clear cache for this tmp_path so Strategy 2 rebuilds fresh.
    _strat_ast_cache.pop(tmp_path.resolve(), None)
    result = file_has_test_coverage("backend/core/special_util.py", tmp_path)
    _strat_ast_cache.pop(tmp_path.resolve(), None)  # cleanup
    assert result is True


# ── §1e genuinely-untested file → False (explicit named case) ───────────
def test_file_has_test_coverage_genuinely_untested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file with NO matching test (by name or AST) must return False."""
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "true")
    (tmp_path / "tests").mkdir()
    # test_other.py imports something else — not orphan.py
    (tmp_path / "tests" / "test_other.py").write_text(
        "from backend.core.other import foo\ndef test_foo(): pass\n"
    )
    _strat_ast_cache.pop(tmp_path.resolve(), None)
    result = file_has_test_coverage("backend/core/orphan.py", tmp_path)
    _strat_ast_cache.pop(tmp_path.resolve(), None)  # cleanup
    assert result is False


# ── §1f Advisor _compute_test_coverage returns 1.0 for suffix-tested file ─
def test_advisor_compute_coverage_suffix_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OperationAdvisor._compute_test_coverage must not return 0% for a file
    whose test uses a suffix variant name (the iteration-13 spurious BLOCK)."""
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "false")
    from backend.core.ouroboros.governance.operation_advisor import OperationAdvisor
    (tmp_path / "tests" / "battle_test").mkdir(parents=True)
    (tmp_path / "tests" / "battle_test" / "test_repl_input_polish_slice4.py").write_text(
        "def test_x(): pass\n"
    )
    adv = OperationAdvisor.__new__(OperationAdvisor)
    adv._project_root = tmp_path  # type: ignore[attr-defined]
    cov = adv._compute_test_coverage(
        ("backend/core/ouroboros/battle_test/repl_input_polish.py",),
        root=tmp_path,
    )
    assert cov == pytest.approx(1.0), (
        f"Expected coverage=1.0 for suffix-tested file, got {cov:.2%}. "
        "This is the iteration-13 spurious BLOCK bug."
    )


# ── §2 covered never penalized ──────────────────────────────────────────
def test_covered_file_multiplier_is_one() -> None:
    assert stratification_penalty_multiplier(9999, has_test_coverage=True) == 1.0


# ── §3 suppress (test-gen escape hatch) ─────────────────────────────────
def test_suppress_bypasses_penalty() -> None:
    # huge, uncovered — but suppress wins
    assert stratification_penalty_multiplier(
        9999, has_test_coverage=False, suppress=True
    ) == 1.0


# ── §4 huge uncovered heavily down-ranked ───────────────────────────────
def test_huge_uncovered_file_is_penalized() -> None:
    m = stratification_penalty_multiplier(
        5000, has_test_coverage=False, alpha=0.75, max_lines=2000,
    )
    # lines >> max_lines → saturates → 1 - alpha
    assert m == pytest.approx(0.25, abs=1e-9)


# ── §5 small uncovered barely touched ───────────────────────────────────
def test_small_uncovered_file_barely_penalized() -> None:
    m = stratification_penalty_multiplier(
        100, has_test_coverage=False, alpha=0.75, max_lines=2000,
    )
    # 1 - 0.75 * (100/2000) = 1 - 0.0375 = 0.9625
    assert m == pytest.approx(0.9625, abs=1e-9)
    assert m > 0.95


# ── §6 monotonic + clamp ────────────────────────────────────────────────
def test_multiplier_monotonic_and_clamped() -> None:
    a = stratification_penalty_multiplier(0, has_test_coverage=False)
    b = stratification_penalty_multiplier(500, has_test_coverage=False)
    c = stratification_penalty_multiplier(50_000, has_test_coverage=False)
    assert a == 1.0            # zero lines → no penalty
    assert a >= b >= c         # bigger files → smaller multiplier
    assert c >= (1.0 - DEFAULT_PENALTY_ALPHA) - 1e-9   # clamp floor


# ── §7 env defaults honoured ────────────────────────────────────────────
def test_env_defaults_used_when_args_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_STRATIFICATION_PENALTY_ALPHA", "0.5")
    monkeypatch.setenv("JARVIS_STRATIFICATION_MAX_LINES", "1000")
    # 1000 lines, alpha 0.5, max 1000 → saturates → 1 - 0.5 = 0.5
    m = stratification_penalty_multiplier(1000, has_test_coverage=False)
    assert m == pytest.approx(0.5, abs=1e-9)


# ── §8 _FileAnalysis wiring ─────────────────────────────────────────────
def test_file_analysis_stratified_score_wiring() -> None:
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
        _FileAnalysis,
    )
    covered = _FileAnalysis(
        file_path="a.py", cyclomatic_complexity=100, total_lines=5000,
        has_test_coverage=True,
    )
    uncovered = _FileAnalysis(
        file_path="b.py", cyclomatic_complexity=100, total_lines=5000,
        has_test_coverage=False,
    )
    assert covered.stratification_penalty == 1.0
    assert covered.stratified_score == pytest.approx(covered.composite_score)
    assert uncovered.stratification_penalty < 1.0
    assert uncovered.stratified_score < uncovered.composite_score


# ── §9 Advisor delegation (no behavior change) ──────────────────────────
def test_advisor_coverage_matches_shared_definition(tmp_path: Path) -> None:
    from backend.core.ouroboros.governance.operation_advisor import OperationAdvisor
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_covered.py").write_text("def test_x(): pass\n")
    adv = OperationAdvisor.__new__(OperationAdvisor)  # avoid heavy __init__
    adv._project_root = tmp_path  # type: ignore[attr-defined]
    cov = adv._compute_test_coverage(("covered.py", "uncovered.py"), root=tmp_path)
    # one of two covered → 0.5, same as shared util would yield
    assert cov == pytest.approx(0.5)


# ── §10 selection down-ranks huge-untested ──────────────────────────────
def test_selection_prefers_small_tested_over_huge_untested() -> None:
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
        OpportunityMinerSensor,
        _FileAnalysis,
    )
    sensor = OpportunityMinerSensor(repo_root=Path("/tmp"), router=object())
    # Equal strategy metric (cyclomatic_complexity); differ only in size+coverage
    huge_untested = _FileAnalysis(
        file_path="core/huge.py", cyclomatic_complexity=200, total_lines=5000,
        has_test_coverage=False,
    )
    small_tested = _FileAnalysis(
        file_path="leaf/small.py", cyclomatic_complexity=200, total_lines=80,
        has_test_coverage=True,
    )
    sensor._max_per_scan = 1
    sensor._explore_ratio = 0.0  # pure exploit so the assertion is deterministic
    _eligible, selected = sensor._select_diverse_candidates(
        [huge_untested, small_tested], "cyclomatic_complexity",
    )
    assert len(selected) == 1
    assert selected[0].file_path == "leaf/small.py"


# ── §11 _analyze_file stamps coverage ───────────────────────────────────
def test_analyze_file_stamps_coverage(tmp_path: Path) -> None:
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
        _analyze_file,
    )
    src = "x = 1\n"
    import ast as _ast
    tree = _ast.parse(src)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
    covered = _analyze_file("pkg/thing.py", src, tree, repo_root=tmp_path)
    uncovered = _analyze_file("pkg/other.py", src, tree, repo_root=tmp_path)
    none_root = _analyze_file("pkg/thing.py", src, tree)  # repo_root None → safe
    assert covered.has_test_coverage is True
    assert uncovered.has_test_coverage is False
    assert none_root.has_test_coverage is True  # unknown → no penalty
