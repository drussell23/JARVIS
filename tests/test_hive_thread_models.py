"""
Tests for backend.hive.thread_models

Covers:
- MessageType enum entries (HIVE_*)
- CognitiveState, ThreadState, PersonaIntent enum values
- AgentLogMessage creation, auto fields, to_dict/from_dict roundtrip
- PersonaReasoningMessage creation, validate_verdict, to_dict/from_dict roundtrip
- HiveThread creation, add_message, token tracking, consensus detection, roundtrip
"""

from datetime import datetime, timezone

import pytest

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
    _gen_msg_id,
    _gen_thread_id,
    _now_utc,
)
from backend.neural_mesh.data_models import MessageType


# ============================================================================
# MessageType enum entries
# ============================================================================


class TestMessageTypeHiveEntries:
    """Verify the four HIVE_* entries exist on MessageType."""

    def test_hive_agent_log_exists(self):
        assert hasattr(MessageType, "HIVE_AGENT_LOG")

    def test_hive_persona_reasoning_exists(self):
        assert hasattr(MessageType, "HIVE_PERSONA_REASONING")

    def test_hive_thread_lifecycle_exists(self):
        assert hasattr(MessageType, "HIVE_THREAD_LIFECYCLE")

    def test_hive_cognitive_transition_exists(self):
        assert hasattr(MessageType, "HIVE_COGNITIVE_TRANSITION")

    def test_all_hive_entries_are_distinct(self):
        values = [
            MessageType.HIVE_AGENT_LOG.value,
            MessageType.HIVE_PERSONA_REASONING.value,
            MessageType.HIVE_THREAD_LIFECYCLE.value,
            MessageType.HIVE_COGNITIVE_TRANSITION.value,
        ]
        assert len(values) == len(set(values))


# ============================================================================
# Enum value tests
# ============================================================================


class TestCognitiveState:
    def test_values(self):
        assert CognitiveState.BASELINE == "baseline"
        assert CognitiveState.REM == "rem"
        assert CognitiveState.FLOW == "flow"

    def test_member_count(self):
        assert len(CognitiveState) == 3

    def test_from_string(self):
        assert CognitiveState("flow") is CognitiveState.FLOW


class TestThreadState:
    def test_values(self):
        assert ThreadState.OPEN == "open"
        assert ThreadState.DEBATING == "debating"
        assert ThreadState.CONSENSUS == "consensus"
        assert ThreadState.EXECUTING == "executing"
        assert ThreadState.RESOLVED == "resolved"
        assert ThreadState.STALE == "stale"

    def test_member_count(self):
        assert len(ThreadState) == 6


class TestPersonaIntent:
    def test_values(self):
        assert PersonaIntent.OBSERVE == "observe"
        assert PersonaIntent.PROPOSE == "propose"
        assert PersonaIntent.CHALLENGE == "challenge"
        assert PersonaIntent.SUPPORT == "support"
        assert PersonaIntent.VALIDATE == "validate"

    def test_member_count(self):
        assert len(PersonaIntent) == 5


# ============================================================================
# Helper functions
# ============================================================================


class TestHelpers:
    def test_gen_msg_id_format(self):
        mid = _gen_msg_id()
        assert mid.startswith("msg_")
        assert len(mid) == 4 + 12  # "msg_" + 12 hex chars

    def test_gen_msg_id_unique(self):
        ids = {_gen_msg_id() for _ in range(100)}
        assert len(ids) == 100

    def test_gen_thread_id_format(self):
        tid = _gen_thread_id()
        assert tid.startswith("thr_")
        assert len(tid) == 4 + 12

    def test_gen_thread_id_unique(self):
        ids = {_gen_thread_id() for _ in range(100)}
        assert len(ids) == 100

    def test_now_utc_is_aware(self):
        dt = _now_utc()
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc


# ============================================================================
# AgentLogMessage
# ============================================================================


class TestAgentLogMessage:
    @pytest.fixture()
    def sample_log(self):
        return AgentLogMessage(
            thread_id="thr_abc123",
            agent_name="lint_agent",
            trinity_parent="jarvis",
            severity="warning",
            category="lint",
            payload={"file": "foo.py", "line": 42},
        )

    def test_creation(self, sample_log):
        assert sample_log.thread_id == "thr_abc123"
        assert sample_log.agent_name == "lint_agent"
        assert sample_log.trinity_parent == "jarvis"
        assert sample_log.severity == "warning"
        assert sample_log.category == "lint"
        assert sample_log.payload == {"file": "foo.py", "line": 42}

    def test_auto_type(self, sample_log):
        assert sample_log.type == "agent_log"

    def test_auto_message_id(self, sample_log):
        assert sample_log.message_id.startswith("msg_")

    def test_auto_ts(self, sample_log):
        assert isinstance(sample_log.ts, datetime)
        assert sample_log.ts.tzinfo == timezone.utc

    def test_auto_monotonic_ns(self, sample_log):
        assert isinstance(sample_log.monotonic_ns, int)
        assert sample_log.monotonic_ns > 0

    def test_to_dict(self, sample_log):
        d = sample_log.to_dict()
        assert d["type"] == "agent_log"
        assert d["thread_id"] == "thr_abc123"
        assert d["agent_name"] == "lint_agent"
        assert d["trinity_parent"] == "jarvis"
        assert d["severity"] == "warning"
        assert d["category"] == "lint"
        assert d["payload"] == {"file": "foo.py", "line": 42}
        assert isinstance(d["ts"], str)
        assert isinstance(d["monotonic_ns"], int)

    def test_roundtrip(self, sample_log):
        d = sample_log.to_dict()
        restored = AgentLogMessage.from_dict(d)
        assert restored.message_id == sample_log.message_id
        assert restored.thread_id == sample_log.thread_id
        assert restored.agent_name == sample_log.agent_name
        assert restored.trinity_parent == sample_log.trinity_parent
        assert restored.severity == sample_log.severity
        assert restored.category == sample_log.category
        assert restored.payload == sample_log.payload
        assert restored.monotonic_ns == sample_log.monotonic_ns
        # ts survives roundtrip (ISO parse)
        assert restored.ts.isoformat() == sample_log.ts.isoformat()

    def test_default_payload_empty(self):
        msg = AgentLogMessage(
            thread_id="thr_x",
            agent_name="a",
            trinity_parent="reactor",
            severity="info",
            category="test",
        )
        assert msg.payload == {}


# ============================================================================
# PersonaReasoningMessage
# ============================================================================


class TestPersonaReasoningMessage:
    @pytest.fixture()
    def sample_reasoning(self):
        return PersonaReasoningMessage(
            thread_id="thr_def456",
            persona="j_prime",
            role="mind",
            intent=PersonaIntent.PROPOSE,
            references=["backend/foo.py", "tests/test_foo.py"],
            reasoning="We should refactor the retry logic.",
            confidence=0.92,
            model_used="qwen-7b",
            token_cost=350,
            manifesto_principle="Progressive Awakening",
        )

    def test_creation(self, sample_reasoning):
        assert sample_reasoning.persona == "j_prime"
        assert sample_reasoning.role == "mind"
        assert sample_reasoning.intent == PersonaIntent.PROPOSE
        assert sample_reasoning.confidence == 0.92
        assert sample_reasoning.token_cost == 350

    def test_auto_type(self, sample_reasoning):
        assert sample_reasoning.type == "persona_reasoning"

    def test_auto_message_id(self, sample_reasoning):
        assert sample_reasoning.message_id.startswith("msg_")

    def test_auto_ts(self, sample_reasoning):
        assert isinstance(sample_reasoning.ts, datetime)

    def test_validate_verdict_default_none(self, sample_reasoning):
        assert sample_reasoning.validate_verdict is None

    def test_validate_verdict_approve(self):
        msg = PersonaReasoningMessage(
            thread_id="thr_x",
            persona="reactor",
            role="immune_system",
            intent=PersonaIntent.VALIDATE,
            references=[],
            reasoning="LGTM",
            confidence=0.99,
            model_used="claude-sonnet-4-20250514",
            token_cost=100,
            validate_verdict="approve",
        )
        assert msg.validate_verdict == "approve"

    def test_validate_verdict_reject(self):
        msg = PersonaReasoningMessage(
            thread_id="thr_x",
            persona="reactor",
            role="immune_system",
            intent=PersonaIntent.VALIDATE,
            references=[],
            reasoning="Security risk",
            confidence=0.95,
            model_used="claude-sonnet-4-20250514",
            token_cost=120,
            validate_verdict="reject",
        )
        assert msg.validate_verdict == "reject"

    def test_manifesto_principle(self, sample_reasoning):
        assert sample_reasoning.manifesto_principle == "Progressive Awakening"

    def test_to_dict(self, sample_reasoning):
        d = sample_reasoning.to_dict()
        assert d["type"] == "persona_reasoning"
        assert d["persona"] == "j_prime"
        assert d["intent"] == "propose"
        assert d["confidence"] == 0.92
        assert d["token_cost"] == 350
        assert d["manifesto_principle"] == "Progressive Awakening"
        assert d["validate_verdict"] is None
        assert isinstance(d["references"], list)

    def test_roundtrip(self, sample_reasoning):
        d = sample_reasoning.to_dict()
        restored = PersonaReasoningMessage.from_dict(d)
        assert restored.message_id == sample_reasoning.message_id
        assert restored.thread_id == sample_reasoning.thread_id
        assert restored.persona == sample_reasoning.persona
        assert restored.role == sample_reasoning.role
        assert restored.intent == sample_reasoning.intent
        assert restored.references == sample_reasoning.references
        assert restored.reasoning == sample_reasoning.reasoning
        assert restored.confidence == sample_reasoning.confidence
        assert restored.model_used == sample_reasoning.model_used
        assert restored.token_cost == sample_reasoning.token_cost
        assert restored.manifesto_principle == sample_reasoning.manifesto_principle
        assert restored.validate_verdict == sample_reasoning.validate_verdict


# ============================================================================
# HiveThread
# ============================================================================


class TestHiveThread:
    @pytest.fixture()
    def empty_thread(self):
        return HiveThread(
            title="Fix retry bug",
            trigger_event="test_failure:backend/core/retry.py",
            cognitive_state=CognitiveState.BASELINE,
            token_budget=5000,
            debate_deadline_s=120.0,
        )

    def test_creation(self, empty_thread):
        assert empty_thread.title == "Fix retry bug"
        assert empty_thread.trigger_event == "test_failure:backend/core/retry.py"
        assert empty_thread.cognitive_state == CognitiveState.BASELINE
        assert empty_thread.token_budget == 5000
        assert empty_thread.debate_deadline_s == 120.0

    def test_auto_thread_id(self, empty_thread):
        assert empty_thread.thread_id.startswith("thr_")

    def test_default_state_open(self, empty_thread):
        assert empty_thread.state == ThreadState.OPEN

    def test_default_messages_empty(self, empty_thread):
        assert empty_thread.messages == []

    def test_default_tokens_consumed_zero(self, empty_thread):
        assert empty_thread.tokens_consumed == 0

    def test_default_linked_fields_none(self, empty_thread):
        assert empty_thread.linked_op_id is None
        assert empty_thread.linked_pr_url is None

    def test_created_at_utc(self, empty_thread):
        assert empty_thread.created_at.tzinfo == timezone.utc

    def test_resolved_at_none(self, empty_thread):
        assert empty_thread.resolved_at is None

    # --- add_message ---

    def test_add_agent_log(self, empty_thread):
        log = AgentLogMessage(
            thread_id=empty_thread.thread_id,
            agent_name="test_agent",
            trinity_parent="jarvis",
            severity="info",
            category="test",
        )
        empty_thread.add_message(log)
        assert len(empty_thread.messages) == 1
        assert empty_thread.tokens_consumed == 0  # AgentLog has no token_cost

    def test_add_persona_reasoning_tracks_tokens(self, empty_thread):
        msg = PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="j_prime",
            role="mind",
            intent=PersonaIntent.PROPOSE,
            references=[],
            reasoning="refactor",
            confidence=0.9,
            model_used="qwen-7b",
            token_cost=200,
        )
        empty_thread.add_message(msg)
        assert empty_thread.tokens_consumed == 200

    def test_add_multiple_messages_cumulative_tokens(self, empty_thread):
        for cost in (100, 250, 150):
            msg = PersonaReasoningMessage(
                thread_id=empty_thread.thread_id,
                persona="j_prime",
                role="mind",
                intent=PersonaIntent.PROPOSE,
                references=[],
                reasoning="step",
                confidence=0.8,
                model_used="m",
                token_cost=cost,
            )
            empty_thread.add_message(msg)
        assert empty_thread.tokens_consumed == 500

    def test_add_message_collects_manifesto_principles(self, empty_thread):
        msg = PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="jarvis",
            role="body",
            intent=PersonaIntent.OBSERVE,
            references=[],
            reasoning="observing",
            confidence=0.85,
            model_used="m",
            token_cost=50,
            manifesto_principle="Unified Organism",
        )
        empty_thread.add_message(msg)
        assert "Unified Organism" in empty_thread.manifesto_principles

    def test_add_message_no_duplicate_principles(self, empty_thread):
        for _ in range(3):
            msg = PersonaReasoningMessage(
                thread_id=empty_thread.thread_id,
                persona="jarvis",
                role="body",
                intent=PersonaIntent.OBSERVE,
                references=[],
                reasoning="obs",
                confidence=0.85,
                model_used="m",
                token_cost=10,
                manifesto_principle="Unified Organism",
            )
            empty_thread.add_message(msg)
        assert empty_thread.manifesto_principles.count("Unified Organism") == 1

    # --- Consensus helpers ---

    def test_has_observe_false_initially(self, empty_thread):
        assert empty_thread.has_observe() is False

    def test_has_observe_true(self, empty_thread):
        msg = PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="jarvis",
            role="body",
            intent=PersonaIntent.OBSERVE,
            references=[],
            reasoning="I see the bug",
            confidence=0.9,
            model_used="m",
            token_cost=50,
        )
        empty_thread.add_message(msg)
        assert empty_thread.has_observe() is True

    def test_has_propose_false_initially(self, empty_thread):
        assert empty_thread.has_propose() is False

    def test_has_propose_true(self, empty_thread):
        msg = PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="j_prime",
            role="mind",
            intent=PersonaIntent.PROPOSE,
            references=[],
            reasoning="Let's fix it",
            confidence=0.9,
            model_used="m",
            token_cost=100,
        )
        empty_thread.add_message(msg)
        assert empty_thread.has_propose() is True

    def test_is_consensus_ready_true(self, empty_thread):
        """Full consensus: observe + propose + approve."""
        # JARVIS observes
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="jarvis", role="body",
            intent=PersonaIntent.OBSERVE, references=[],
            reasoning="bug confirmed", confidence=0.9,
            model_used="m", token_cost=50,
        ))
        # J-Prime proposes
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[],
            reasoning="fix plan", confidence=0.9,
            model_used="m", token_cost=100,
        ))
        # Reactor approves
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="reactor", role="immune_system",
            intent=PersonaIntent.VALIDATE, references=[],
            reasoning="LGTM", confidence=0.99,
            model_used="m", token_cost=80,
            validate_verdict="approve",
        ))
        assert empty_thread.is_consensus_ready() is True

    def test_is_consensus_ready_missing_observe(self, empty_thread):
        """Missing observe -> not ready."""
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[],
            reasoning="fix", confidence=0.9,
            model_used="m", token_cost=100,
        ))
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="reactor", role="immune_system",
            intent=PersonaIntent.VALIDATE, references=[],
            reasoning="ok", confidence=0.99,
            model_used="m", token_cost=80,
            validate_verdict="approve",
        ))
        assert empty_thread.is_consensus_ready() is False

    def test_is_consensus_ready_reject_verdict(self, empty_thread):
        """Reactor rejects -> not ready even with observe + propose."""
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="jarvis", role="body",
            intent=PersonaIntent.OBSERVE, references=[],
            reasoning="bug", confidence=0.9,
            model_used="m", token_cost=50,
        ))
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[],
            reasoning="fix", confidence=0.9,
            model_used="m", token_cost=100,
        ))
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="reactor", role="immune_system",
            intent=PersonaIntent.VALIDATE, references=[],
            reasoning="too risky", confidence=0.95,
            model_used="m", token_cost=80,
            validate_verdict="reject",
        ))
        assert empty_thread.is_consensus_ready() is False

    # --- Budget ---

    def test_is_budget_exhausted_false(self, empty_thread):
        assert empty_thread.is_budget_exhausted() is False

    def test_is_budget_exhausted_true(self, empty_thread):
        msg = PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[],
            reasoning="big proposal", confidence=0.9,
            model_used="m", token_cost=5000,
        )
        empty_thread.add_message(msg)
        assert empty_thread.is_budget_exhausted() is True

    def test_is_budget_exhausted_over(self, empty_thread):
        msg = PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[],
            reasoning="huge", confidence=0.9,
            model_used="m", token_cost=9999,
        )
        empty_thread.add_message(msg)
        assert empty_thread.is_budget_exhausted() is True

    # --- Serialization roundtrip ---

    def test_to_dict(self, empty_thread):
        d = empty_thread.to_dict()
        assert d["title"] == "Fix retry bug"
        assert d["cognitive_state"] == "baseline"
        assert d["state"] == "open"
        assert d["token_budget"] == 5000
        assert d["messages"] == []
        assert isinstance(d["created_at"], str)
        assert d["resolved_at"] is None

    def test_roundtrip_empty(self, empty_thread):
        d = empty_thread.to_dict()
        restored = HiveThread.from_dict(d)
        assert restored.thread_id == empty_thread.thread_id
        assert restored.title == empty_thread.title
        assert restored.trigger_event == empty_thread.trigger_event
        assert restored.cognitive_state == empty_thread.cognitive_state
        assert restored.token_budget == empty_thread.token_budget
        assert restored.debate_deadline_s == empty_thread.debate_deadline_s
        assert restored.state == empty_thread.state
        assert len(restored.messages) == 0

    def test_roundtrip_with_messages(self, empty_thread):
        # Add a mix of message types
        empty_thread.add_message(AgentLogMessage(
            thread_id=empty_thread.thread_id,
            agent_name="build", trinity_parent="jarvis",
            severity="info", category="build",
            payload={"exit_code": 0},
        ))
        empty_thread.add_message(PersonaReasoningMessage(
            thread_id=empty_thread.thread_id,
            persona="jarvis", role="body",
            intent=PersonaIntent.OBSERVE, references=["a.py"],
            reasoning="Build passed", confidence=0.95,
            model_used="claude-sonnet-4-20250514", token_cost=200,
            manifesto_principle="Absolute Observability",
        ))

        d = empty_thread.to_dict()
        restored = HiveThread.from_dict(d)

        assert len(restored.messages) == 2
        assert isinstance(restored.messages[0], AgentLogMessage)
        assert isinstance(restored.messages[1], PersonaReasoningMessage)
        assert restored.messages[0].agent_name == "build"
        assert restored.messages[1].reasoning == "Build passed"
        # Note: from_dict does not re-run add_message, so tokens_consumed
        # and manifesto_principles are restored from the serialized data.
        assert restored.tokens_consumed == empty_thread.tokens_consumed
        assert restored.manifesto_principles == empty_thread.manifesto_principles

    def test_roundtrip_preserves_linked_fields(self, empty_thread):
        empty_thread.linked_op_id = "op_abc123"
        empty_thread.linked_pr_url = "https://github.com/foo/bar/pull/42"
        d = empty_thread.to_dict()
        restored = HiveThread.from_dict(d)
        assert restored.linked_op_id == "op_abc123"
        assert restored.linked_pr_url == "https://github.com/foo/bar/pull/42"
