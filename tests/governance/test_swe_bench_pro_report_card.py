"""Regression spine - SWE-Bench-Pro Phase F report_card renderer.

Phase F is pure-data aggregation + rendering above Phase D's
EvaluationResultStore. Operators consume aggregate cards for
benchmark-run triage.

Spine invariants
----------------

  1. Empty store renders an empty card cleanly (no crashes).
  2. Mixed records produce correct ScoreOutcome distribution.
  3. Mixed records produce correct EvaluationOutcome distribution.
  4. Overall pass_rate excludes SKIPPED from denominator.
  5. Per-repo aggregation: with problems mapping (authoritative
     repo), without (instance_id-prefix fallback).
  6. Per-difficulty aggregation only present when problems mapping
     supplied; falls back to "unknown" bucket otherwise.
  7. Top-N failure clustering by diagnostic prefix before colon.
  8. Sort order: per_repo by pass_rate desc / total desc / name;
     top_failures by count desc / prefix.
  9. Markdown render contains canonical sections.
 10. JSON render is valid + lossless (roundtrip through from_dict).
 11. write_report_card writes file via canonical Path I/O; parent
     auto-created; markdown vs JSON formats.
 12. All four dataclasses (ReportCard / RepoStats / DifficultyStats
     / FailureCluster) are frozen.

AST pins (composition discipline)
---------------------------------

 13. Composes canonical EvaluationResultStore.query /
     aggregate_score_outcomes / aggregate_evaluation_outcomes /
     pass_rate (substring presence).
 14. Composes ScoreOutcome + EvaluationOutcome enums.
 15. No homegrown statistics (no manual sum/count loops that
     bypass canonical aggregators - tested by ensuring at least
     one canonical method appears in the source).
 16. No master flag in the report_card module - Phase F is
     read-only over Phase D's store.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Dict, Iterator

import pytest

from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
    EvaluationResult,
)
from backend.core.ouroboros.governance.swe_bench_pro.report_card import (
    REPORT_CARD_SCHEMA_VERSION,
    DifficultyStats,
    FailureCluster,
    ReportCard,
    RepoStats,
    build_report_card,
    render_json,
    render_markdown,
    write_report_card,
)
from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
    EvaluationResultStore,
    reset_default_store,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    ScoreOutcome,
    ScoringResult,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_store() -> Iterator[EvaluationResultStore]:
    reset_default_store()
    store = EvaluationResultStore(persistence_enabled=False)
    yield store
    reset_default_store()


def _seed(
    store: EvaluationResultStore,
    instance_id: str,
    score: ScoreOutcome,
    eval_outcome: EvaluationOutcome = EvaluationOutcome.RESOLVED,
    diagnostic: str = "",
) -> None:
    ev = EvaluationResult(
        outcome=eval_outcome,
        problem_instance_id=instance_id,
        op_id=f"op-{instance_id}",
        terminal_state="applied" if eval_outcome == EvaluationOutcome.RESOLVED else "failed",
        captured_patch="dummy",
    )
    sc = ScoringResult(
        outcome=score,
        problem_instance_id=instance_id,
        tests_passed=1 if score == ScoreOutcome.PASS else 0,
        tests_failed=0 if score == ScoreOutcome.PASS else 1,
        tests_total=1,
        pass_rate=1.0 if score == ScoreOutcome.PASS else 0.0,
        diagnostic=diagnostic,
    )
    asyncio.run(store.record(ev, sc))


# ---------------------------------------------------------------------------
# 1. Empty store
# ---------------------------------------------------------------------------


def test_empty_store_renders_empty_card(clean_store: EvaluationResultStore) -> None:
    card = build_report_card(clean_store)
    assert card.total_records == 0
    assert card.overall_pass_rate == 0.0
    assert card.per_repo == ()
    assert card.per_difficulty == ()
    assert card.top_failures == ()
    # Distributions present with all-zeros.
    assert all(v == 0 for v in card.score_distribution.values())
    assert all(v == 0 for v in card.eval_distribution.values())


def test_empty_card_renders_markdown_cleanly(clean_store: EvaluationResultStore) -> None:
    card = build_report_card(clean_store)
    md = render_markdown(card)
    assert "SWE-Bench-Pro Report Card" in md
    assert "Overall" in md
    assert "Score distribution" in md


def test_empty_card_renders_valid_json(clean_store: EvaluationResultStore) -> None:
    card = build_report_card(clean_store)
    payload = render_json(card)
    parsed = json.loads(payload)
    assert parsed["total_records"] == 0
    assert parsed["schema_version"] == REPORT_CARD_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. Distributions
# ---------------------------------------------------------------------------


def test_score_distribution_correct(clean_store: EvaluationResultStore) -> None:
    _seed(clean_store, "a__r-1", ScoreOutcome.PASS)
    _seed(clean_store, "a__r-2", ScoreOutcome.PASS)
    _seed(clean_store, "a__r-3", ScoreOutcome.FAIL, diagnostic="failed_tests=t1")
    _seed(clean_store, "a__r-4", ScoreOutcome.PARTIAL, diagnostic="failed_tests=t2")
    _seed(clean_store, "a__r-5", ScoreOutcome.SCORING_ERROR, diagnostic="apply_failed:bad hunk")
    _seed(clean_store, "a__r-6", ScoreOutcome.SKIPPED)

    card = build_report_card(clean_store)
    assert card.total_records == 6
    assert card.score_distribution["pass"] == 2
    assert card.score_distribution["fail"] == 1
    assert card.score_distribution["partial"] == 1
    assert card.score_distribution["scoring_error"] == 1
    assert card.score_distribution["skipped"] == 1


def test_overall_pass_rate_excludes_skipped(clean_store: EvaluationResultStore) -> None:
    _seed(clean_store, "a__r-1", ScoreOutcome.PASS)
    _seed(clean_store, "a__r-2", ScoreOutcome.FAIL, diagnostic="x")
    _seed(clean_store, "a__r-3", ScoreOutcome.SKIPPED)
    # 1 PASS / (1 PASS + 1 FAIL) = 0.5 (SKIPPED excluded).
    card = build_report_card(clean_store)
    assert card.overall_pass_rate == 0.5


# ---------------------------------------------------------------------------
# 3. Per-repo aggregation
# ---------------------------------------------------------------------------


def test_per_repo_with_problems_mapping_uses_canonical_repo(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "x-1", ScoreOutcome.PASS)
    _seed(clean_store, "x-2", ScoreOutcome.FAIL, diagnostic="d")
    _seed(clean_store, "y-1", ScoreOutcome.PASS)

    problems: Dict[str, ProblemSpec] = {
        "x-1": ProblemSpec(
            instance_id="x-1", repo="alpha/beta",
            base_commit="c", problem_statement="p", test_patch="",
            gold_patch="",
        ),
        "x-2": ProblemSpec(
            instance_id="x-2", repo="alpha/beta",
            base_commit="c", problem_statement="p", test_patch="",
            gold_patch="",
        ),
        "y-1": ProblemSpec(
            instance_id="y-1", repo="gamma/delta",
            base_commit="c", problem_statement="p", test_patch="",
            gold_patch="",
        ),
    }

    card = build_report_card(clean_store, problems=problems)
    repos = {r.repo: r for r in card.per_repo}
    assert "alpha/beta" in repos
    assert "gamma/delta" in repos
    assert repos["alpha/beta"].total == 2
    assert repos["alpha/beta"].pass_count == 1
    assert repos["alpha/beta"].fail_count == 1
    assert repos["alpha/beta"].pass_rate == 0.5
    assert repos["gamma/delta"].pass_rate == 1.0


def test_per_repo_without_problems_uses_instance_id_prefix_fallback(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "octocat__hello-001", ScoreOutcome.PASS)
    _seed(clean_store, "octocat__hello-002", ScoreOutcome.PASS)
    _seed(clean_store, "octocat__hello-003", ScoreOutcome.FAIL, diagnostic="d")
    _seed(clean_store, "foo__bar-001", ScoreOutcome.PASS)

    card = build_report_card(clean_store)
    repos = {r.repo: r for r in card.per_repo}
    assert "octocat/hello" in repos
    assert "foo/bar" in repos
    assert repos["octocat/hello"].total == 3
    assert repos["octocat/hello"].pass_count == 2


def test_per_repo_sort_order_pass_rate_desc(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "a__r-1", ScoreOutcome.PASS)
    _seed(clean_store, "b__r-1", ScoreOutcome.PASS)
    _seed(clean_store, "b__r-2", ScoreOutcome.FAIL, diagnostic="d")
    # a/r has 100% pass; b/r has 50%. a should come first.
    card = build_report_card(clean_store)
    assert card.per_repo[0].repo == "a/r"
    assert card.per_repo[1].repo == "b/r"


# ---------------------------------------------------------------------------
# 4. Per-difficulty aggregation
# ---------------------------------------------------------------------------


def test_per_difficulty_only_present_with_problems_mapping(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "x-1", ScoreOutcome.PASS)
    _seed(clean_store, "x-2", ScoreOutcome.PASS)
    _seed(clean_store, "y-1", ScoreOutcome.FAIL, diagnostic="d")

    problems = {
        "x-1": ProblemSpec(
            instance_id="x-1", repo="r/r", base_commit="c",
            problem_statement="p", test_patch="", gold_patch="",
            difficulty="easy",
        ),
        "x-2": ProblemSpec(
            instance_id="x-2", repo="r/r", base_commit="c",
            problem_statement="p", test_patch="", gold_patch="",
            difficulty="hard",
        ),
        "y-1": ProblemSpec(
            instance_id="y-1", repo="r/r", base_commit="c",
            problem_statement="p", test_patch="", gold_patch="",
            difficulty="hard",
        ),
    }
    card = build_report_card(clean_store, problems=problems)
    diffs = {d.difficulty: d for d in card.per_difficulty}
    assert "easy" in diffs
    assert "hard" in diffs
    assert diffs["easy"].pass_rate == 1.0
    assert diffs["hard"].total == 2
    assert diffs["hard"].pass_rate == 0.5


def test_per_difficulty_falls_back_to_unknown_without_problems(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "x-1", ScoreOutcome.PASS)
    card = build_report_card(clean_store)
    # Without problems mapping, every record buckets under "unknown".
    assert len(card.per_difficulty) == 1
    assert card.per_difficulty[0].difficulty == "unknown"


# ---------------------------------------------------------------------------
# 5. Top-N failure clustering
# ---------------------------------------------------------------------------


def test_top_failures_clusters_by_diagnostic_prefix(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "a-1", ScoreOutcome.SCORING_ERROR, diagnostic="apply_failed:hunk 1")
    _seed(clean_store, "a-2", ScoreOutcome.SCORING_ERROR, diagnostic="apply_failed:hunk 2")
    _seed(clean_store, "a-3", ScoreOutcome.SCORING_ERROR, diagnostic="apply_failed:hunk 3")
    _seed(clean_store, "b-1", ScoreOutcome.FAIL, diagnostic="patch_modified_tests:tests/foo.py")
    _seed(clean_store, "c-1", ScoreOutcome.FAIL, diagnostic="failed_tests=t1,t2")

    card = build_report_card(clean_store)
    by_prefix = {f.diagnostic_prefix: f for f in card.top_failures}
    assert "apply_failed" in by_prefix
    assert by_prefix["apply_failed"].count == 3
    assert "patch_modified_tests" in by_prefix
    assert "failed_tests=t1,t2" in by_prefix


def test_top_failures_sorted_by_count_desc(
    clean_store: EvaluationResultStore,
) -> None:
    for i in range(5):
        _seed(
            clean_store, f"big-{i}", ScoreOutcome.FAIL,
            diagnostic="big_cluster:detail",
        )
    for i in range(2):
        _seed(
            clean_store, f"small-{i}", ScoreOutcome.FAIL,
            diagnostic="small_cluster:detail",
        )
    card = build_report_card(clean_store)
    assert card.top_failures[0].diagnostic_prefix == "big_cluster"
    assert card.top_failures[0].count == 5
    assert card.top_failures[1].diagnostic_prefix == "small_cluster"


def test_top_failures_example_instance_ids_capped(
    clean_store: EvaluationResultStore,
) -> None:
    for i in range(10):
        _seed(
            clean_store, f"inst-{i}", ScoreOutcome.FAIL,
            diagnostic="same:detail",
        )
    card = build_report_card(clean_store)
    cluster = card.top_failures[0]
    assert cluster.count == 10
    # Examples capped at 5.
    assert len(cluster.example_instance_ids) == 5


def test_empty_diagnostic_clusters_as_empty(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "a-1", ScoreOutcome.FAIL, diagnostic="")
    card = build_report_card(clean_store)
    by_prefix = {f.diagnostic_prefix: f for f in card.top_failures}
    assert "(empty)" in by_prefix


# ---------------------------------------------------------------------------
# 6. Renderers
# ---------------------------------------------------------------------------


def test_markdown_contains_canonical_sections(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "octocat__hello-1", ScoreOutcome.PASS)
    _seed(clean_store, "octocat__hello-2", ScoreOutcome.FAIL, diagnostic="apply_failed:x")
    card = build_report_card(clean_store)
    md = render_markdown(card)
    assert "# SWE-Bench-Pro Report Card" in md
    assert "## Overall" in md
    assert "## Score distribution" in md
    assert "## Evaluation distribution" in md
    assert "## Per-repo pass rate" in md
    assert "## Top failure clusters" in md
    assert "octocat/hello" in md
    assert "apply_failed" in md


def test_json_render_roundtrips_through_from_dict(
    clean_store: EvaluationResultStore,
) -> None:
    _seed(clean_store, "a__r-1", ScoreOutcome.PASS)
    _seed(clean_store, "a__r-2", ScoreOutcome.FAIL, diagnostic="d:x")
    card = build_report_card(clean_store)
    payload = render_json(card)
    parsed = json.loads(payload)
    restored = ReportCard.from_dict(parsed)
    assert restored.total_records == card.total_records
    assert restored.overall_pass_rate == card.overall_pass_rate
    assert len(restored.per_repo) == len(card.per_repo)
    assert len(restored.top_failures) == len(card.top_failures)


# ---------------------------------------------------------------------------
# 7. write_report_card disk I/O
# ---------------------------------------------------------------------------


def test_write_report_card_markdown(
    clean_store: EvaluationResultStore, tmp_path: Path,
) -> None:
    _seed(clean_store, "a__r-1", ScoreOutcome.PASS)
    card = build_report_card(clean_store)
    out = tmp_path / "subdir" / "card.md"
    ok = asyncio.run(write_report_card(card, out, format="markdown"))
    assert ok is True
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "SWE-Bench-Pro Report Card" in content


def test_write_report_card_json(
    clean_store: EvaluationResultStore, tmp_path: Path,
) -> None:
    _seed(clean_store, "a__r-1", ScoreOutcome.PASS)
    card = build_report_card(clean_store)
    out = tmp_path / "card.json"
    ok = asyncio.run(write_report_card(card, out, format="json"))
    assert ok is True
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["total_records"] == 1


def test_write_report_card_fails_gracefully_on_bad_path(
    clean_store: EvaluationResultStore,
) -> None:
    card = build_report_card(clean_store)
    # /dev/null/nope would error - write_report_card returns False.
    ok = asyncio.run(write_report_card(
        card, Path("/dev/null/cannot/write/here.md"),
    ))
    assert ok is False


# ---------------------------------------------------------------------------
# 8. Dataclasses are frozen
# ---------------------------------------------------------------------------


def test_report_card_is_frozen() -> None:
    card = ReportCard(
        total_records=0, score_distribution={}, eval_distribution={},
        overall_pass_rate=0.0, per_repo=(), per_difficulty=(),
        top_failures=(), rendered_at_iso="",
    )
    with pytest.raises(Exception):
        card.total_records = 99  # type: ignore[misc]


def test_supporting_dataclasses_are_frozen() -> None:
    rs = RepoStats(
        repo="x/y", total=0, pass_count=0, fail_count=0,
        partial_count=0, error_count=0, skipped_count=0, pass_rate=0.0,
    )
    ds = DifficultyStats(
        difficulty="easy", total=0, pass_count=0, fail_count=0,
        partial_count=0, error_count=0, skipped_count=0, pass_rate=0.0,
    )
    fc = FailureCluster(
        diagnostic_prefix="x", count=0, example_instance_ids=(),
    )
    for obj in (rs, ds, fc):
        with pytest.raises(Exception):
            obj.total = 999  # type: ignore[misc,attr-defined]


# ---------------------------------------------------------------------------
# 9. AST pins - composition discipline
# ---------------------------------------------------------------------------


def _module_source() -> str:
    from backend.core.ouroboros.governance.swe_bench_pro import report_card
    return Path(report_card.__file__).read_text()


def test_ast_pin_imports_canonical_store_surfaces() -> None:
    """Composes Phase D EvaluationResultStore - no parallel store."""
    src = _module_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "result_store" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "EvaluationResultStore":
                        found = True
    assert found, (
        "report_card.py does not import EvaluationResultStore - "
        "risk of parallel store"
    )


def test_ast_pin_imports_canonical_outcome_enums() -> None:
    """Composes ScoreOutcome + EvaluationOutcome - no parallel taxonomy."""
    src = _module_source()
    tree = ast.parse(src)
    needed = {"ScoreOutcome", "EvaluationOutcome"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "scorer" in module or "evaluator" in module:
                for alias in node.names:
                    needed.discard(alias.name)
    assert not needed, (
        f"report_card.py does not import {sorted(needed)} from "
        f"canonical sources"
    )


def test_ast_pin_uses_canonical_store_aggregations() -> None:
    """build_report_card composes Phase D's canonical aggregators
    rather than re-implementing them. Substring check: at least one
    canonical aggregate method must be referenced."""
    src = _module_source()
    # At least one of these canonical methods must appear in the
    # source so we're certain the rendering pipeline goes through
    # Phase D rather than reimplementing counters from scratch.
    aggregators = {
        "aggregate_score_outcomes",
        "aggregate_evaluation_outcomes",
        "pass_rate",
    }
    found = sum(1 for name in aggregators if name in src)
    assert found >= 2, (
        f"report_card.py uses too few canonical store aggregators "
        f"(found {found}; expected >= 2 of {sorted(aggregators)}) - "
        f"composition discipline violated"
    )


def test_ast_pin_no_master_flag_env_var() -> None:
    """Phase F is read-only over Phase D - no enablement flag of
    its own. Operator binding: 'No master flag - composes Phase A
    master via Phase D store contents.'"""
    src = _module_source()
    # Phase F should never reference a master-flag env var. Allow
    # the docstring to mention "JARVIS_SWE_BENCH_PRO_ENABLED" as
    # documentation context but no os.environ lookup against any
    # JARVIS_*_ENABLED string.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "get"
            ):
                value = fn.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "environ"
                ):
                    # Any os.environ.get call would be a master-flag
                    # lookup; Phase F doesn't do any env reads.
                    raise AssertionError(
                        "report_card.py reads os.environ - Phase F "
                        "is supposed to be read-only over Phase D "
                        "with no enablement flag of its own"
                    )
