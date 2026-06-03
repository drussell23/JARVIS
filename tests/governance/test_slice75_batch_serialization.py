"""Slice 75 — Schema serialization parity + multi-instance batch parsing.

Two additive, verify-first-corrected pieces (the runbook premises were off):
  * The durable record had NO `resolved` field at all (the earlier `resolved:
    None` was a parse artifact, not a bug). We ADD a derived top-level
    `resolved: bool` so the ledger is directly queryable for pass-rate %.
  * Multi-instance was already CSV-supported; we widen the parser to accept
    comma AND/OR whitespace delimiters (order-preserving dedup).
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
)
from backend.core.ouroboros.governance.swe_bench_pro.harness_inject import (
    inject_instance_ids,
    INJECT_INSTANCE_IDS_ENV_VAR,
)


def _record(eval_outcome: EvaluationOutcome, score_outcome: ScoreOutcome) -> EvaluationRecord:
    return EvaluationRecord(
        evaluation=EvaluationResult(
            outcome=eval_outcome, problem_instance_id="inst-x", op_id="op-1",
        ),
        scoring=ScoringResult(outcome=score_outcome, problem_instance_id="inst-x"),
        recorded_at_iso="2026-06-03T00:00:00+00:00",
    )


# --- Phase 1: derived `resolved` boolean parity ---

def test_resolved_true_only_on_eval_resolved_and_score_pass():
    rec = _record(EvaluationOutcome.RESOLVED, ScoreOutcome.PASS)
    assert rec.resolved is True
    assert rec.to_dict()["resolved"] is True


def test_resolved_false_on_non_pass_scores():
    for sc in (ScoreOutcome.FAIL, ScoreOutcome.PARTIAL,
               ScoreOutcome.SKIPPED, ScoreOutcome.SCORING_ERROR):
        rec = _record(EvaluationOutcome.RESOLVED, sc)
        assert rec.resolved is False, sc
        assert rec.to_dict()["resolved"] is False, sc


def test_resolved_false_when_eval_unresolved():
    # Defensive: a 'pass' with an 'unresolved' eval should never count resolved.
    rec = _record(EvaluationOutcome.UNRESOLVED, ScoreOutcome.PASS)
    assert rec.resolved is False


def test_resolved_is_always_concrete_bool_never_none():
    rec = _record(EvaluationOutcome.UNRESOLVED, ScoreOutcome.SKIPPED)
    val = rec.to_dict()["resolved"]
    assert val is False and isinstance(val, bool)


# --- Phase 2: tolerant multi-instance delimiter parsing ---

def test_comma_delimited(monkeypatch):
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a,b,c")
    assert inject_instance_ids() == ["a", "b", "c"]


def test_space_delimited(monkeypatch):
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a b c")
    assert inject_instance_ids() == ["a", "b", "c"]


def test_mixed_comma_and_space(monkeypatch):
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a, b,  c   d")
    assert inject_instance_ids() == ["a", "b", "c", "d"]


def test_order_preserving_dedup(monkeypatch):
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a,b,a,c,b")
    assert inject_instance_ids() == ["a", "b", "c"]


def test_empty_and_whitespace_only(monkeypatch):
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "   ")
    assert inject_instance_ids() == []
    monkeypatch.delenv(INJECT_INSTANCE_IDS_ENV_VAR, raising=False)
    assert inject_instance_ids() == []


def test_single_instance_unchanged(monkeypatch):
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "instance_qutebrowser__foo")
    assert inject_instance_ids() == ["instance_qutebrowser__foo"]
