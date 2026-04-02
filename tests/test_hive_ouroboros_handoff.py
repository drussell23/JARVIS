"""
Tests for backend.hive.ouroboros_handoff — thread consensus to OperationContext.
"""

from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.hive.ouroboros_handoff import (
    _extract_consensus_description,
    serialize_consensus,
)
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_consensus_thread() -> HiveThread:
    """Build a minimal CONSENSUS thread with the four expected messages."""
    thread = HiveThread(
        title="Refactor audio pipeline for latency",
        trigger_event="test_failure:audio_latency_regression",
        cognitive_state=CognitiveState.FLOW,
        token_budget=10_000,
        debate_deadline_s=120.0,
    )

    # 1. Tier 1: agent telemetry
    thread.add_message(
        AgentLogMessage(
            thread_id=thread.thread_id,
            agent_name="health_monitor_agent",
            trinity_parent="jarvis",
            severity="warning",
            category="test",
            payload={"test": "test_audio_latency", "latency_ms": 312},
        )
    )

    # 2. JARVIS observes
    thread.add_message(
        PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona="jarvis",
            role="body",
            intent=PersonaIntent.OBSERVE,
            references=["backend/audio/pipeline.py"],
            reasoning="Audio latency spiked to 312ms, above 200ms threshold.",
            confidence=0.92,
            model_used="claude-sonnet-4-20250514",
            token_cost=150,
        )
    )

    # 3. J-Prime proposes (with manifesto principle)
    thread.add_message(
        PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona="j_prime",
            role="mind",
            intent=PersonaIntent.PROPOSE,
            references=["backend/audio/pipeline.py", "backend/audio/buffer.py"],
            reasoning="Replace blocking buffer flush with async ring buffer.",
            confidence=0.88,
            model_used="qwen-7b",
            token_cost=320,
            manifesto_principle="$3 Spinal Cord",
        )
    )

    # 4. Reactor validates (approve)
    thread.add_message(
        PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona="reactor",
            role="immune_system",
            intent=PersonaIntent.VALIDATE,
            references=["backend/audio/pipeline.py"],
            reasoning="Proposal is safe: async ring buffer reduces latency without data loss risk. Approved.",
            confidence=0.95,
            model_used="claude-sonnet-4-20250514",
            token_cost=200,
            validate_verdict="approve",
        )
    )

    # Transition to CONSENSUS
    thread.state = ThreadState.CONSENSUS

    return thread


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractConsensusDescription:
    """Tests for _extract_consensus_description helper."""

    def test_returns_reactor_approval_reasoning(self) -> None:
        thread = _make_consensus_thread()
        desc = _extract_consensus_description(thread)
        assert "async ring buffer" in desc
        assert "Approved" in desc

    def test_fallback_to_title_when_no_reactor_approve(self) -> None:
        thread = HiveThread(
            title="Fallback title",
            trigger_event="manual",
            cognitive_state=CognitiveState.BASELINE,
            token_budget=1000,
            debate_deadline_s=60.0,
        )
        thread.state = ThreadState.CONSENSUS
        desc = _extract_consensus_description(thread)
        assert desc == "Fallback title"


class TestSerializeConsensus:
    """Tests for serialize_consensus."""

    def test_returns_classify_phase(self) -> None:
        thread = _make_consensus_thread()
        ctx = serialize_consensus(thread, target_files=("backend/audio/pipeline.py",))
        assert ctx.phase == OperationPhase.CLASSIFY

    def test_description_contains_reactor_approval(self) -> None:
        thread = _make_consensus_thread()
        ctx = serialize_consensus(thread, target_files=("backend/audio/pipeline.py",))
        assert "async ring buffer" in ctx.description
        assert "Approved" in ctx.description

    def test_target_files_mapped(self) -> None:
        thread = _make_consensus_thread()
        files = ("backend/audio/pipeline.py", "backend/audio/buffer.py")
        ctx = serialize_consensus(thread, target_files=files)
        assert ctx.target_files == files

    def test_causal_trace_id_matches_thread(self) -> None:
        thread = _make_consensus_thread()
        ctx = serialize_consensus(thread, target_files=("a.py",))
        assert ctx.causal_trace_id == thread.thread_id

    def test_correlation_id_matches_thread(self) -> None:
        thread = _make_consensus_thread()
        ctx = serialize_consensus(thread, target_files=("a.py",))
        assert ctx.correlation_id == thread.thread_id

    def test_strategic_memory_prompt_contains_thread_history(self) -> None:
        thread = _make_consensus_thread()
        ctx = serialize_consensus(thread, target_files=("a.py",))

        history = json.loads(ctx.strategic_memory_prompt)
        assert history["thread_id"] == thread.thread_id
        assert history["title"] == thread.title
        assert history["trigger_event"] == thread.trigger_event
        # 4 messages: 1 agent_log + 3 persona_reasoning
        assert len(history["messages"]) == 4

    def test_human_instructions_contains_manifesto_principles(self) -> None:
        thread = _make_consensus_thread()
        ctx = serialize_consensus(thread, target_files=("a.py",))
        assert "$3 Spinal Cord" in ctx.human_instructions
        assert "Manifesto principles" in ctx.human_instructions

    def test_rejects_non_consensus_thread(self) -> None:
        thread = _make_consensus_thread()
        thread.state = ThreadState.OPEN
        with pytest.raises(ValueError, match="expected state CONSENSUS"):
            serialize_consensus(thread, target_files=("a.py",))

    def test_rejects_debating_thread(self) -> None:
        thread = _make_consensus_thread()
        thread.state = ThreadState.DEBATING
        with pytest.raises(ValueError, match="expected state CONSENSUS"):
            serialize_consensus(thread, target_files=("a.py",))

    def test_hash_chain_valid(self) -> None:
        thread = _make_consensus_thread()
        ctx = serialize_consensus(thread, target_files=("a.py",))
        # Initial context: non-empty hash, no previous hash
        assert ctx.context_hash
        assert len(ctx.context_hash) == 64  # SHA-256 hex
        assert ctx.previous_hash is None

    def test_hash_changes_with_different_threads(self) -> None:
        thread1 = _make_consensus_thread()
        thread2 = _make_consensus_thread()  # different thread_id
        ctx1 = serialize_consensus(thread1, target_files=("a.py",))
        ctx2 = serialize_consensus(thread2, target_files=("a.py",))
        # Different thread_ids produce different hashes
        assert ctx1.context_hash != ctx2.context_hash
