"""
Tests for backend.hive.thread_manager

Covers:
- create_thread (OPEN state, in active_threads)
- transition to DEBATING (valid)
- invalid transition raises ValueError (e.g., OPEN -> EXECUTING)
- add_agent_log to thread
- consensus detection: full debate (observe + propose + validate approve) = True
- consensus not reached: missing observe = False
- consensus not reached: reject verdict = False
- check_and_advance transitions to CONSENSUS on detection
- budget exhaustion marks thread STALE
- persist_thread writes JSON, load_threads reads it back
"""

from __future__ import annotations

import json

import pytest

from backend.hive.thread_manager import ThreadManager, _TRANSITIONS, _TERMINAL_STATES
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


# ============================================================================
# Helpers
# ============================================================================


def _observe_msg(thread_id: str, token_cost: int = 50) -> PersonaReasoningMessage:
    """JARVIS OBSERVE message."""
    return PersonaReasoningMessage(
        thread_id=thread_id,
        persona="jarvis",
        role="body",
        intent=PersonaIntent.OBSERVE,
        references=[],
        reasoning="I see the issue",
        confidence=0.9,
        model_used="m",
        token_cost=token_cost,
    )


def _propose_msg(thread_id: str, token_cost: int = 100) -> PersonaReasoningMessage:
    """J-Prime PROPOSE message."""
    return PersonaReasoningMessage(
        thread_id=thread_id,
        persona="j_prime",
        role="mind",
        intent=PersonaIntent.PROPOSE,
        references=[],
        reasoning="Here is the fix",
        confidence=0.92,
        model_used="m",
        token_cost=token_cost,
    )


def _validate_approve_msg(
    thread_id: str, token_cost: int = 80
) -> PersonaReasoningMessage:
    """Reactor VALIDATE with approve verdict."""
    return PersonaReasoningMessage(
        thread_id=thread_id,
        persona="reactor",
        role="immune_system",
        intent=PersonaIntent.VALIDATE,
        references=[],
        reasoning="LGTM",
        confidence=0.99,
        model_used="m",
        token_cost=token_cost,
        validate_verdict="approve",
    )


def _validate_reject_msg(
    thread_id: str, token_cost: int = 80
) -> PersonaReasoningMessage:
    """Reactor VALIDATE with reject verdict."""
    return PersonaReasoningMessage(
        thread_id=thread_id,
        persona="reactor",
        role="immune_system",
        intent=PersonaIntent.VALIDATE,
        references=[],
        reasoning="Too risky",
        confidence=0.95,
        model_used="m",
        token_cost=token_cost,
        validate_verdict="reject",
    )


def _agent_log(thread_id: str) -> AgentLogMessage:
    """Simple agent log message."""
    return AgentLogMessage(
        thread_id=thread_id,
        agent_name="lint_agent",
        trinity_parent="jarvis",
        severity="info",
        category="lint",
        payload={"file": "foo.py"},
    )


# ============================================================================
# Transition Table Sanity
# ============================================================================


class TestTransitionTable:
    """Verify the transition dict is well-formed."""

    def test_all_states_present(self):
        for state in ThreadState:
            assert state in _TRANSITIONS

    def test_terminal_states_have_no_exits(self):
        for state in _TERMINAL_STATES:
            assert _TRANSITIONS[state] == set()

    def test_terminal_states_are_resolved_and_stale(self):
        assert _TERMINAL_STATES == frozenset({ThreadState.RESOLVED, ThreadState.STALE})


# ============================================================================
# ThreadManager — creation
# ============================================================================


class TestCreateThread:
    def test_create_thread_returns_hive_thread(self):
        mgr = ThreadManager()
        t = mgr.create_thread("title", "trigger", CognitiveState.BASELINE)
        assert isinstance(t, HiveThread)

    def test_create_thread_state_is_open(self):
        mgr = ThreadManager()
        t = mgr.create_thread("title", "trigger", CognitiveState.BASELINE)
        assert t.state == ThreadState.OPEN

    def test_create_thread_in_active_threads(self):
        mgr = ThreadManager()
        t = mgr.create_thread("title", "trigger", CognitiveState.FLOW)
        assert t.thread_id in mgr.active_threads

    def test_create_thread_uses_manager_defaults(self):
        mgr = ThreadManager(debate_timeout_s=120.0, token_ceiling=8000)
        t = mgr.create_thread("title", "trigger", CognitiveState.REM)
        assert t.debate_deadline_s == 120.0
        assert t.token_budget == 8000

    def test_create_thread_fields_propagated(self):
        mgr = ThreadManager()
        t = mgr.create_thread("Fix bug", "test_failure:foo.py", CognitiveState.FLOW)
        assert t.title == "Fix bug"
        assert t.trigger_event == "test_failure:foo.py"
        assert t.cognitive_state == CognitiveState.FLOW

    def test_get_thread_found(self):
        mgr = ThreadManager()
        t = mgr.create_thread("title", "trigger", CognitiveState.BASELINE)
        assert mgr.get_thread(t.thread_id) is t

    def test_get_thread_not_found(self):
        mgr = ThreadManager()
        assert mgr.get_thread("thr_nonexistent") is None


# ============================================================================
# ThreadManager — transitions
# ============================================================================


class TestTransitions:
    def test_transition_open_to_debating(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        assert t.state == ThreadState.DEBATING

    def test_transition_open_to_stale(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.STALE)
        assert t.state == ThreadState.STALE

    def test_invalid_transition_open_to_executing_raises(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        with pytest.raises(ValueError, match="Illegal transition"):
            mgr.transition(t.thread_id, ThreadState.EXECUTING)

    def test_invalid_transition_open_to_resolved_raises(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        with pytest.raises(ValueError, match="Illegal transition"):
            mgr.transition(t.thread_id, ThreadState.RESOLVED)

    def test_invalid_transition_open_to_consensus_raises(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        with pytest.raises(ValueError, match="Illegal transition"):
            mgr.transition(t.thread_id, ThreadState.CONSENSUS)

    def test_transition_debating_to_consensus(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        mgr.transition(t.thread_id, ThreadState.CONSENSUS)
        assert t.state == ThreadState.CONSENSUS

    def test_transition_consensus_to_executing(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        mgr.transition(t.thread_id, ThreadState.CONSENSUS)
        mgr.transition(t.thread_id, ThreadState.EXECUTING)
        assert t.state == ThreadState.EXECUTING

    def test_transition_executing_to_resolved(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        mgr.transition(t.thread_id, ThreadState.CONSENSUS)
        mgr.transition(t.thread_id, ThreadState.EXECUTING)
        mgr.transition(t.thread_id, ThreadState.RESOLVED)
        assert t.state == ThreadState.RESOLVED

    def test_terminal_state_sets_resolved_at(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        assert t.resolved_at is None
        mgr.transition(t.thread_id, ThreadState.STALE)
        assert t.resolved_at is not None

    def test_resolved_thread_not_in_active_threads(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        mgr.transition(t.thread_id, ThreadState.CONSENSUS)
        mgr.transition(t.thread_id, ThreadState.EXECUTING)
        mgr.transition(t.thread_id, ThreadState.RESOLVED)
        assert t.thread_id not in mgr.active_threads

    def test_stale_thread_not_in_active_threads(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.STALE)
        assert t.thread_id not in mgr.active_threads

    def test_transition_unknown_thread_raises_key_error(self):
        mgr = ThreadManager()
        with pytest.raises(KeyError, match="Unknown thread"):
            mgr.transition("thr_fake", ThreadState.DEBATING)

    def test_transition_from_terminal_resolved_raises(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        mgr.transition(t.thread_id, ThreadState.CONSENSUS)
        mgr.transition(t.thread_id, ThreadState.EXECUTING)
        mgr.transition(t.thread_id, ThreadState.RESOLVED)
        with pytest.raises(ValueError, match="Illegal transition"):
            mgr.transition(t.thread_id, ThreadState.STALE)

    def test_transition_from_terminal_stale_raises(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.STALE)
        with pytest.raises(ValueError, match="Illegal transition"):
            mgr.transition(t.thread_id, ThreadState.OPEN)


# ============================================================================
# ThreadManager — messages
# ============================================================================


class TestAddMessage:
    def test_add_agent_log(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        log = _agent_log(t.thread_id)
        mgr.add_message(t.thread_id, log)
        assert len(t.messages) == 1
        assert t.messages[0] is log

    def test_add_persona_reasoning(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        msg = _observe_msg(t.thread_id, token_cost=150)
        mgr.add_message(t.thread_id, msg)
        assert len(t.messages) == 1
        assert t.tokens_consumed == 150

    def test_add_message_unknown_thread_raises(self):
        mgr = ThreadManager()
        with pytest.raises(KeyError, match="Unknown thread"):
            mgr.add_message("thr_fake", _agent_log("thr_fake"))


# ============================================================================
# ThreadManager — consensus
# ============================================================================


class TestConsensus:
    def test_consensus_full_debate_true(self):
        """observe + propose + validate(approve) = consensus."""
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.add_message(t.thread_id, _observe_msg(t.thread_id))
        mgr.add_message(t.thread_id, _propose_msg(t.thread_id))
        mgr.add_message(t.thread_id, _validate_approve_msg(t.thread_id))
        assert mgr.check_consensus(t.thread_id) is True

    def test_consensus_missing_observe_false(self):
        """propose + validate(approve) but no observe = no consensus."""
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.add_message(t.thread_id, _propose_msg(t.thread_id))
        mgr.add_message(t.thread_id, _validate_approve_msg(t.thread_id))
        assert mgr.check_consensus(t.thread_id) is False

    def test_consensus_reject_verdict_false(self):
        """observe + propose + validate(reject) = no consensus."""
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.add_message(t.thread_id, _observe_msg(t.thread_id))
        mgr.add_message(t.thread_id, _propose_msg(t.thread_id))
        mgr.add_message(t.thread_id, _validate_reject_msg(t.thread_id))
        assert mgr.check_consensus(t.thread_id) is False

    def test_consensus_missing_propose_false(self):
        """observe + validate(approve) but no propose = no consensus."""
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.add_message(t.thread_id, _observe_msg(t.thread_id))
        mgr.add_message(t.thread_id, _validate_approve_msg(t.thread_id))
        assert mgr.check_consensus(t.thread_id) is False

    def test_check_consensus_unknown_thread_raises(self):
        mgr = ThreadManager()
        with pytest.raises(KeyError, match="Unknown thread"):
            mgr.check_consensus("thr_fake")


# ============================================================================
# ThreadManager — check_and_advance
# ============================================================================


class TestCheckAndAdvance:
    def test_advances_to_consensus_on_detection(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)

        mgr.add_message(t.thread_id, _observe_msg(t.thread_id))
        mgr.add_message(t.thread_id, _propose_msg(t.thread_id))
        mgr.add_message(t.thread_id, _validate_approve_msg(t.thread_id))

        result = mgr.check_and_advance(t.thread_id)
        assert result == ThreadState.CONSENSUS
        assert t.state == ThreadState.CONSENSUS

    def test_returns_none_when_no_consensus(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        # No messages added — no consensus
        result = mgr.check_and_advance(t.thread_id)
        assert result is None
        assert t.state == ThreadState.DEBATING

    def test_returns_none_for_non_debating_thread(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        # Thread is OPEN, not DEBATING
        result = mgr.check_and_advance(t.thread_id)
        assert result is None

    def test_budget_exhaustion_marks_stale(self):
        mgr = ThreadManager(token_ceiling=200)
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)

        # Exhaust the budget with a single large message
        mgr.add_message(t.thread_id, _propose_msg(t.thread_id, token_cost=250))

        result = mgr.check_and_advance(t.thread_id)
        assert result == ThreadState.STALE
        assert t.state == ThreadState.STALE

    def test_budget_exhaustion_takes_priority_over_consensus(self):
        """Even if consensus is reached, budget exhaustion wins."""
        mgr = ThreadManager(token_ceiling=100)
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)

        # Add all consensus messages but exceed budget
        mgr.add_message(t.thread_id, _observe_msg(t.thread_id, token_cost=40))
        mgr.add_message(t.thread_id, _propose_msg(t.thread_id, token_cost=40))
        mgr.add_message(
            t.thread_id, _validate_approve_msg(t.thread_id, token_cost=40)
        )
        # 40+40+40 = 120 > 100 ceiling

        result = mgr.check_and_advance(t.thread_id)
        assert result == ThreadState.STALE

    def test_check_and_advance_unknown_thread_raises(self):
        mgr = ThreadManager()
        with pytest.raises(KeyError, match="Unknown thread"):
            mgr.check_and_advance("thr_fake")


# ============================================================================
# ThreadManager — persistence
# ============================================================================


class TestPersistence:
    def test_persist_thread_writes_json(self, tmp_path):
        mgr = ThreadManager(storage_dir=tmp_path)
        t = mgr.create_thread("Persist me", "trigger", CognitiveState.BASELINE)
        mgr.add_message(t.thread_id, _observe_msg(t.thread_id))
        mgr.persist_thread(t.thread_id)

        path = tmp_path / f"{t.thread_id}.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["title"] == "Persist me"
        assert data["state"] == "open"
        assert len(data["messages"]) == 1

    def test_load_threads_restores(self, tmp_path):
        # Create and persist two threads
        mgr1 = ThreadManager(storage_dir=tmp_path)
        t1 = mgr1.create_thread("Thread A", "trigger_a", CognitiveState.BASELINE)
        t2 = mgr1.create_thread("Thread B", "trigger_b", CognitiveState.FLOW)
        mgr1.add_message(t1.thread_id, _observe_msg(t1.thread_id))
        mgr1.persist_thread(t1.thread_id)
        mgr1.persist_thread(t2.thread_id)

        # Fresh manager loads from disk
        mgr2 = ThreadManager(storage_dir=tmp_path)
        count = mgr2.load_threads()
        assert count == 2
        assert mgr2.get_thread(t1.thread_id) is not None
        assert mgr2.get_thread(t2.thread_id) is not None
        restored = mgr2.get_thread(t1.thread_id)
        assert restored.title == "Thread A"
        assert len(restored.messages) == 1

    def test_load_threads_returns_count(self, tmp_path):
        mgr = ThreadManager(storage_dir=tmp_path)
        assert mgr.load_threads() == 0  # empty dir

    def test_persist_unknown_thread_raises(self, tmp_path):
        mgr = ThreadManager(storage_dir=tmp_path)
        with pytest.raises(KeyError, match="Unknown thread"):
            mgr.persist_thread("thr_fake")

    def test_persist_without_storage_dir_raises(self):
        mgr = ThreadManager(storage_dir=None)
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        with pytest.raises(RuntimeError, match="no storage_dir"):
            mgr.persist_thread(t.thread_id)

    def test_load_threads_no_storage_dir_returns_zero(self):
        mgr = ThreadManager(storage_dir=None)
        assert mgr.load_threads() == 0

    def test_persist_and_load_preserves_state_transitions(self, tmp_path):
        mgr1 = ThreadManager(storage_dir=tmp_path)
        t = mgr1.create_thread("t", "e", CognitiveState.BASELINE)
        mgr1.transition(t.thread_id, ThreadState.DEBATING)
        mgr1.add_message(t.thread_id, _observe_msg(t.thread_id))
        mgr1.add_message(t.thread_id, _propose_msg(t.thread_id))
        mgr1.add_message(t.thread_id, _validate_approve_msg(t.thread_id))
        mgr1.transition(t.thread_id, ThreadState.CONSENSUS)
        mgr1.persist_thread(t.thread_id)

        mgr2 = ThreadManager(storage_dir=tmp_path)
        mgr2.load_threads()
        restored = mgr2.get_thread(t.thread_id)
        assert restored.state == ThreadState.CONSENSUS
        assert restored.tokens_consumed == 50 + 100 + 80  # observe + propose + validate

    def test_load_ignores_non_thr_files(self, tmp_path):
        """Only thr_*.json files are loaded."""
        # Write a non-thread JSON file
        (tmp_path / "config.json").write_text('{"key": "value"}', encoding="utf-8")

        mgr = ThreadManager(storage_dir=tmp_path)
        count = mgr.load_threads()
        assert count == 0

    def test_load_skips_corrupt_files(self, tmp_path):
        """Corrupt JSON files are logged and skipped."""
        (tmp_path / "thr_corrupt.json").write_text("NOT VALID JSON", encoding="utf-8")
        mgr = ThreadManager(storage_dir=tmp_path)
        count = mgr.load_threads()
        assert count == 0


# ============================================================================
# ThreadManager — active_threads filtering
# ============================================================================


class TestActiveThreads:
    def test_active_threads_excludes_resolved(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.DEBATING)
        mgr.transition(t.thread_id, ThreadState.CONSENSUS)
        mgr.transition(t.thread_id, ThreadState.EXECUTING)
        mgr.transition(t.thread_id, ThreadState.RESOLVED)
        assert len(mgr.active_threads) == 0

    def test_active_threads_excludes_stale(self):
        mgr = ThreadManager()
        t = mgr.create_thread("t", "e", CognitiveState.BASELINE)
        mgr.transition(t.thread_id, ThreadState.STALE)
        assert len(mgr.active_threads) == 0

    def test_active_threads_includes_open_and_debating(self):
        mgr = ThreadManager()
        t1 = mgr.create_thread("open", "e", CognitiveState.BASELINE)
        t2 = mgr.create_thread("debating", "e", CognitiveState.BASELINE)
        mgr.transition(t2.thread_id, ThreadState.DEBATING)
        active = mgr.active_threads
        assert t1.thread_id in active
        assert t2.thread_id in active

    def test_active_threads_mixed(self):
        mgr = ThreadManager()
        t_open = mgr.create_thread("open", "e", CognitiveState.BASELINE)
        t_stale = mgr.create_thread("stale", "e", CognitiveState.BASELINE)
        mgr.transition(t_stale.thread_id, ThreadState.STALE)
        active = mgr.active_threads
        assert t_open.thread_id in active
        assert t_stale.thread_id not in active
