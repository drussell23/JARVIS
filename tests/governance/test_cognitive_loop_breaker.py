"""Semantic Loop Breaker -- cognitive armor for the Venom tool loop.

A small (7B) model will confidently hallucinate the SAME broken tool call several
rounds in a row, burning tokens and deadline. The existing budget/iteration
guards are too coarse. This heuristic breaker tracks the SEMANTIC SIMILARITY of
the last N rounds' tool calls; when the model is stuck in a repetition loop it
snaps the circuit so the orchestrator can eject with the best context gathered.

Pure, dependency-free (token-Jaccard, no embeddings) -- fail-soft, gated.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.cognitive_loop_breaker import (
    SemanticLoopBreaker,
    call_signature,
)


class _Call:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


def _round(*calls):
    return [_Call(n, a) for n, a in calls]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_LOOP_BREAKER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VENOM_LOOP_BREAKER_WINDOW", "3")
    monkeypatch.setenv("JARVIS_VENOM_LOOP_BREAKER_SIMILARITY", "0.9")
    yield


def test_identical_call_three_rounds_trips():
    b = SemanticLoopBreaker()
    assert b.observe(_round(("read_file", {"path": "a.py"}))) is False  # round 1
    assert b.observe(_round(("read_file", {"path": "a.py"}))) is False  # round 2
    assert b.observe(_round(("read_file", {"path": "a.py"}))) is True   # round 3 -> LOOP


def test_varied_calls_never_trip():
    b = SemanticLoopBreaker()
    assert b.observe(_round(("read_file", {"path": "a.py"}))) is False
    assert b.observe(_round(("search_code", {"q": "foo"}))) is False
    assert b.observe(_round(("edit_file", {"path": "b.py"}))) is False


def test_below_window_does_not_trip():
    b = SemanticLoopBreaker()
    assert b.observe(_round(("read_file", {"path": "a.py"}))) is False
    assert b.observe(_round(("read_file", {"path": "a.py"}))) is False  # only 2 (<3)


def test_near_identical_trips_above_threshold():
    """Tiny variation (one extra arg) still >0.9 similar -> stuck loop."""
    b = SemanticLoopBreaker()
    b.observe(_round(("bash", {"cmd": "pytest tests/foo.py -x -v -q --tb=short"})))
    b.observe(_round(("bash", {"cmd": "pytest tests/foo.py -x -v -q --tb=long"})))
    assert b.observe(_round(("bash", {"cmd": "pytest tests/foo.py -x -v -q --tb=auto"}))) is True


def test_progress_resets_the_loop():
    b = SemanticLoopBreaker()
    b.observe(_round(("read_file", {"path": "a.py"})))
    b.observe(_round(("read_file", {"path": "a.py"})))
    b.observe(_round(("edit_file", {"path": "a.py"})))  # genuine progress
    # The window no longer has 3 identical -> not looping.
    assert b.observe(_round(("read_file", {"path": "a.py"}))) is False


def test_gate_off_never_trips(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_LOOP_BREAKER_ENABLED", "false")
    b = SemanticLoopBreaker()
    for _ in range(5):
        assert b.observe(_round(("read_file", {"path": "a.py"}))) is False


def test_empty_rounds_are_safe():
    b = SemanticLoopBreaker()
    assert b.observe([]) is False
    assert b.observe(None) is False  # type: ignore[arg-type]


def test_call_signature_is_order_independent():
    s1 = call_signature(_round(("a", {"x": 1}), ("b", {"y": 2})))
    s2 = call_signature(_round(("b", {"y": 2}), ("a", {"x": 1})))
    assert s1 == s2  # same multiset of calls regardless of order
