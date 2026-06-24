"""Tests for deadlock_breaker — stagnation OR max-turn -> interrupt + kill + dissolve + yield."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.autonomy.deadlock_breaker import (
    DeadlockInterruptedException,
    EpistemicDeadlockBreaker,
    max_turn_budget,
)
from backend.core.ouroboros.governance.autonomy.stagnation_detector import (
    SemanticStagnationDetector,
)


@pytest.fixture
def yield_spy(monkeypatch):
    calls = []
    import backend.core.ouroboros.governance.ide_observability_stream as stream
    monkeypatch.setattr(
        stream, "publish_sovereign_yield", lambda op, r: calls.append((op, r))
    )
    return calls


def _killtracker():
    killed = []
    return killed, (lambda wid: killed.append(wid))


def test_max_turn_default():
    assert max_turn_budget() == 3


def test_semantic_stagnation_triggers_interrupt(yield_spy):
    killed, kill = _killtracker()
    breaker = EpistemicDeadlockBreaker(
        correlation_id="c1", worker_a="w1", worker_b="w2", op_id="op1",
        detector=SemanticStagnationDetector(threshold=0.85, window=2),
        kill_unit=kill,
    )
    # Two near-identical turns after the first -> stagnation -> interrupt.
    breaker.observe_turn("we keep saying the same plan over again")
    breaker.observe_turn("we keep saying the same plan over again")
    with pytest.raises(DeadlockInterruptedException) as exc:
        breaker.observe_turn("we keep saying the same plan over again")
    # Both workers killed + dissolved.
    assert set(killed) == {"w1", "w2"}
    assert set(exc.value.dissolved_units) == {"w1", "w2"}
    assert exc.value.trigger == "semantic_stagnation"
    # Bounded sanitized transcript present.
    assert "same plan" in exc.value.transcript
    # SovereignYield emitted.
    assert any(r == "epistemic_deadlock" for _, r in yield_spy)


def test_max_turn_budget_backstop(yield_spy, monkeypatch):
    # Use distinct turns so the stagnation detector never fires — only the dumb
    # backstop (max_turn_budget + 1) trips.
    monkeypatch.setenv("JARVIS_SWARM_CLARIFICATION_MAX_TURNS", "3")
    killed, kill = _killtracker()
    breaker = EpistemicDeadlockBreaker(
        correlation_id="c2", worker_a="a", worker_b="b",
        detector=SemanticStagnationDetector(threshold=0.99, window=99),
        kill_unit=kill,
    )
    breaker.observe_turn("first distinct point about parsing")
    breaker.observe_turn("second different point about caching")
    breaker.observe_turn("third unique point about networking")
    # Turn 4 == max_turn_budget(3) + 1 -> backstop interrupt.
    with pytest.raises(DeadlockInterruptedException) as exc:
        breaker.observe_turn("fourth fresh point about logging")
    assert exc.value.trigger == "max_turn_budget"
    assert set(killed) == {"a", "b"}


def test_verified_artifact_resolves_no_interrupt(yield_spy):
    breaker = EpistemicDeadlockBreaker(
        correlation_id="c3", worker_a="w1", worker_b="w2",
        detector=SemanticStagnationDetector(threshold=0.85, window=2),
    )
    # Even near-identical turns do NOT deadlock if an artifact was verified.
    breaker.observe_turn("repeat repeat repeat", verified_artifact=True)
    breaker.observe_turn("repeat repeat repeat", verified_artifact=True)
    # No raise.
    breaker.observe_turn("repeat repeat repeat", verified_artifact=True)


def test_fail_closed_on_observe_error(yield_spy):
    breaker = EpistemicDeadlockBreaker(
        correlation_id="c4", worker_a="w1", worker_b="w2",
    )

    class _Boom:
        def __str__(self):
            raise RuntimeError("boom")

    # A turn that cannot be processed -> fail-CLOSED -> interrupt.
    with pytest.raises(DeadlockInterruptedException) as exc:
        breaker.observe_turn(_Boom())
    assert exc.value.trigger == "fail_closed"


def test_kill_unit_failure_does_not_block_dissolve(yield_spy):
    def _bad_kill(wid):
        raise RuntimeError("process already gone")

    breaker = EpistemicDeadlockBreaker(
        correlation_id="c5", worker_a="w1", worker_b="w2",
        detector=SemanticStagnationDetector(threshold=0.85, window=2),
        kill_unit=_bad_kill,
    )
    breaker.observe_turn("looping forever here we go")
    breaker.observe_turn("looping forever here we go")
    # kill_unit raising must NOT prevent the dissolve + yield.
    with pytest.raises(DeadlockInterruptedException) as exc:
        breaker.observe_turn("looping forever here we go")
    assert set(exc.value.dissolved_units) == {"w1", "w2"}
    assert any(r == "epistemic_deadlock" for _, r in yield_spy)


def test_transcript_redacts_secrets(yield_spy):
    breaker = EpistemicDeadlockBreaker(
        correlation_id="c6", worker_a="w1", worker_b="w2",
        detector=SemanticStagnationDetector(threshold=0.85, window=2),
    )
    secret_line = "use this key sk-" + "B" * 30 + " to authenticate"
    breaker.observe_turn(secret_line)
    breaker.observe_turn(secret_line)
    with pytest.raises(DeadlockInterruptedException) as exc:
        breaker.observe_turn(secret_line)
    assert "sk-BBBB" not in exc.value.transcript
    assert "REDACTED" in exc.value.transcript
