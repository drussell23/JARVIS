"""Slice 76 Phase 3 — programmatic dual-metric categorization.

Adds a derived ``EvaluationCategory`` (RESOLVED / CAPABILITY_MISS /
INFRASTRUCTURE_EXCLUSION) to the durable ledger so the strict vs.
fairly-attempted rates (PRD §50.11) are computed natively, never by manual text.

Honesty contract (the load-bearing distinction):
  * RESOLVED               — eval=resolved AND score=pass.
  * INFRASTRUCTURE_EXCLUSION — the op never got a fair attempt: eval
    prepare_failed / terminal_timeout, or eval=resolved but scoring_error.
    These are excluded from the OPERATIONAL denominator (not capability).
  * CAPABILITY_MISS        — the model got a fair shot and did not pass:
    eval=resolved with fail/partial, OR eval=unresolved (failed to produce a
    working fix). Counts against capability — NEVER flatters the model.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationResult,
    EvaluationOutcome,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    ScoringResult,
    ScoreOutcome,
)
from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
    EvaluationRecord,
    EvaluationCategory,
)


def _rec(eo: EvaluationOutcome, so: ScoreOutcome) -> EvaluationRecord:
    return EvaluationRecord(
        evaluation=EvaluationResult(outcome=eo, problem_instance_id="i", op_id="o"),
        scoring=ScoringResult(outcome=so, problem_instance_id="i"),
        recorded_at_iso="2026-06-03T00:00:00+00:00",
    )


# --- the three categories, mapped to the exact EVAL-2 rows (§50.11) ---

def test_resolved_category():
    rec = _rec(EvaluationOutcome.RESOLVED, ScoreOutcome.PASS)  # qutebrowser
    assert rec.category is EvaluationCategory.RESOLVED
    assert rec.to_dict()["category"] == "resolved"


def test_capability_miss_on_resolved_fail():
    rec = _rec(EvaluationOutcome.RESOLVED, ScoreOutcome.FAIL)  # element-web, NodeBB-76c6
    assert rec.category is EvaluationCategory.CAPABILITY_MISS
    assert rec.to_dict()["category"] == "capability_miss"


def test_capability_miss_on_resolved_partial():
    assert _rec(EvaluationOutcome.RESOLVED, ScoreOutcome.PARTIAL).category \
        is EvaluationCategory.CAPABILITY_MISS


def test_capability_miss_on_unresolved_never_flatters_model():
    # model failed to produce a working fix — a capability failure, NOT excluded
    assert _rec(EvaluationOutcome.UNRESOLVED, ScoreOutcome.SKIPPED).category \
        is EvaluationCategory.CAPABILITY_MISS


def test_infra_exclusion_on_prepare_failed():
    rec = _rec(EvaluationOutcome.PREPARE_FAILED, ScoreOutcome.SKIPPED)  # ansible×2
    assert rec.category is EvaluationCategory.INFRASTRUCTURE_EXCLUSION
    assert rec.to_dict()["category"] == "infrastructure_exclusion"


def test_infra_exclusion_on_terminal_timeout():
    rec = _rec(EvaluationOutcome.TERMINAL_TIMEOUT, ScoreOutcome.SKIPPED)  # NodeBB-04998908
    assert rec.category is EvaluationCategory.INFRASTRUCTURE_EXCLUSION


def test_infra_exclusion_on_scoring_error_despite_resolved():
    # the patch existed but scoring infra broke — not a capability verdict
    assert _rec(EvaluationOutcome.RESOLVED, ScoreOutcome.SCORING_ERROR).category \
        is EvaluationCategory.INFRASTRUCTURE_EXCLUSION


def test_category_is_consistent_with_resolved_bool():
    # category==RESOLVED iff the Slice 75 resolved bool is True
    for eo in EvaluationOutcome:
        for so in ScoreOutcome:
            rec = _rec(eo, so)
            assert (rec.category is EvaluationCategory.RESOLVED) == rec.resolved, (eo, so)


# --- dual-metric aggregation helper (strict vs operational) ---

def test_dual_metric_rates_match_eval2():
    rows = [
        _rec(EvaluationOutcome.RESOLVED, ScoreOutcome.PASS),          # resolved
        _rec(EvaluationOutcome.RESOLVED, ScoreOutcome.FAIL),          # cap miss
        _rec(EvaluationOutcome.RESOLVED, ScoreOutcome.FAIL),          # cap miss
        _rec(EvaluationOutcome.TERMINAL_TIMEOUT, ScoreOutcome.SKIPPED),  # infra
        _rec(EvaluationOutcome.PREPARE_FAILED, ScoreOutcome.SKIPPED),    # infra
        _rec(EvaluationOutcome.PREPARE_FAILED, ScoreOutcome.SKIPPED),    # infra
    ]
    from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
        dual_metric_rates,
    )
    m = dual_metric_rates(rows)
    assert m["resolved"] == 1
    assert m["capability_miss"] == 2
    assert m["infrastructure_exclusion"] == 3
    assert m["total"] == 6
    assert m["fairly_attempted"] == 3
    assert abs(m["strict_rate"] - (1 / 6)) < 1e-9
    assert abs(m["operational_rate"] - (1 / 3)) < 1e-9


def test_dual_metric_handles_empty():
    from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
        dual_metric_rates,
    )
    m = dual_metric_rates([])
    assert m["total"] == 0 and m["strict_rate"] == 0.0 and m["operational_rate"] == 0.0
