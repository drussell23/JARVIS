"""Tests for stagnation_detector — Jaccard early-break, false-trigger guard, fail-closed."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.autonomy.stagnation_detector import (
    SemanticStagnationDetector,
    jaccard_similarity,
    stagnation_threshold,
    stagnation_window,
)


def test_jaccard_identical():
    assert jaccard_similarity("the quick brown fox", "the quick brown fox") == 1.0


def test_jaccard_disjoint():
    assert jaccard_similarity("alpha beta gamma", "delta epsilon zeta") == 0.0


def test_jaccard_partial():
    # {a,b,c} vs {b,c,d} -> intersection 2, union 4 -> 0.5
    assert jaccard_similarity("a b c", "b c d") == pytest.approx(0.5)


def test_jaccard_empty_both_treated_similar():
    assert jaccard_similarity("", "") == 1.0


def test_defaults():
    assert stagnation_threshold() == 0.85
    assert stagnation_window() == 2


def test_stagnation_fires_on_repeated_turns():
    det = SemanticStagnationDetector(threshold=0.85, window=2)
    corr = "c1"
    # Turn 1 — no prior, never stagnant.
    assert det.observe(corr, "we should refactor the parser module now") is False
    # Turn 2 — near-identical -> 1 consecutive stagnant (window=2 not met).
    assert det.observe(corr, "we should refactor the parser module now") is False
    # Turn 3 — near-identical again -> 2 consecutive -> STAGNATION.
    assert det.observe(corr, "we should refactor the parser module now") is True


def test_distinct_turns_no_false_trigger():
    det = SemanticStagnationDetector(threshold=0.85, window=2)
    corr = "c2"
    assert det.observe(corr, "investigate the authentication failure") is False
    assert det.observe(corr, "now check the database connection pool") is False
    assert det.observe(corr, "the cache eviction policy seems wrong") is False
    # All distinct -> never stagnant.


def test_stagnation_resets_on_novelty():
    det = SemanticStagnationDetector(threshold=0.85, window=2)
    corr = "c3"
    det.observe(corr, "same thing again and again here")
    det.observe(corr, "same thing again and again here")  # consecutive=1
    # Novel turn resets the counter.
    assert det.observe(corr, "completely different idea about networking") is False
    det.observe(corr, "completely different idea about networking")  # consecutive=1
    assert det.observe(corr, "yet another new concept entirely fresh") is False


def test_exact_repeat_intent_hash():
    det = SemanticStagnationDetector(threshold=0.99, window=2)
    corr = "c4"
    # Same tokens, different order -> intent hash matches even above a high
    # Jaccard threshold (Jaccard would also be 1.0 here, but the intent hash
    # backstops reordering).
    det.observe(corr, "fix the bug now")
    det.observe(corr, "now fix the bug")  # consecutive=1
    assert det.observe(corr, "the bug fix now") is True  # consecutive=2 -> stagnation


def test_fail_closed_on_garbage():
    det = SemanticStagnationDetector(threshold=0.85, window=2)
    # A None turn is coerced; force a hard failure by passing an object whose
    # str() raises.
    class _Boom:
        def __str__(self):
            raise RuntimeError("boom")

    # Fail-CLOSED -> treated as stagnation (break), never "keep talking".
    assert det.observe("cX", _Boom()) is True


def test_per_correlation_isolation():
    det = SemanticStagnationDetector(threshold=0.85, window=2)
    det.observe("A", "loop loop loop loop")
    det.observe("A", "loop loop loop loop")
    # Different correlation id is independent — not yet stagnant.
    assert det.observe("B", "loop loop loop loop") is False


def test_turn_count_and_reset():
    det = SemanticStagnationDetector(threshold=0.85, window=2)
    det.observe("c", "hello world")
    det.observe("c", "hello there world")
    assert det.turn_count("c") == 2
    det.reset("c")
    assert det.turn_count("c") == 0
