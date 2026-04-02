"""
Integration tests for the Hive pipeline — agent_log through Ouroboros handoff.

Exercises the full component chain with no mocking:
  CognitiveFsm -> HiveModelRouter -> ThreadManager -> thread_models -> ouroboros_handoff
"""

from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
from backend.hive.cognitive_fsm import CognitiveEvent, CognitiveFsm
from backend.hive.model_router import HiveModelRouter
from backend.hive.ouroboros_handoff import serialize_consensus
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


class TestFullPipeline:
    """End-to-end: agent_log -> Trinity debate -> consensus -> Ouroboros handoff."""

    def test_full_pipeline(self, tmp_path: str) -> None:
        # ── 1. CognitiveFsm starts in BASELINE ──────────────────────
        fsm = CognitiveFsm(state_file=tmp_path / "cognitive_state.json")
        assert fsm.state == CognitiveState.BASELINE

        # ── 2. HiveModelRouter confirms BASELINE = no model ─────────
        router = HiveModelRouter()
        assert router.get_model(CognitiveState.BASELINE) is None

        # ── 3. FLOW_TRIGGER fires, FSM transitions to FLOW ──────────
        decision = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert not decision.noop
        assert decision.to_state == CognitiveState.FLOW
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW

        # ── 4. Model router now returns the 397B model ───────────────
        flow_model = router.get_model(CognitiveState.FLOW)
        assert flow_model is not None
        assert "397B" in flow_model

        # ── 5. ThreadManager creates thread ──────────────────────────
        mgr = ThreadManager(
            storage_dir=tmp_path / "threads",
            debate_timeout_s=120.0,
            token_ceiling=50_000,
        )
        thread = mgr.create_thread(
            title="Fix memory pressure in health monitor",
            trigger_event="test_failure:health_monitor_oom",
            cognitive_state=CognitiveState.FLOW,
        )
        assert thread.state == ThreadState.OPEN

        # ── 6. Thread transitions to DEBATING ────────────────────────
        mgr.transition(thread.thread_id, ThreadState.DEBATING)
        assert thread.state == ThreadState.DEBATING

        # ── 7. Specialist agent_log arrives ──────────────────────────
        agent_log = AgentLogMessage(
            thread_id=thread.thread_id,
            agent_name="health_monitor_agent",
            trinity_parent="jarvis",
            severity="warning",
            category="memory_pressure",
            payload={"rss_mb": 1420, "threshold_mb": 1200},
        )
        mgr.add_message(thread.thread_id, agent_log)
        assert len(thread.messages) == 1
        assert thread.messages[0].type == "agent_log"

        # ── 8. JARVIS observes ───────────────────────────────────────
        jarvis_msg = PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona="jarvis",
            role="body",
            intent=PersonaIntent.OBSERVE,
            references=["backend/core/health_monitor.py"],
            reasoning="RSS at 1420 MB exceeds 1200 MB threshold. Possible leak in audio buffer pool.",
            confidence=0.91,
            model_used=flow_model,
            token_cost=200,
            manifesto_principle="$3 Spinal Cord",
        )
        mgr.add_message(thread.thread_id, jarvis_msg)
        assert thread.has_observe()

        # ── 9. J-Prime proposes ──────────────────────────────────────
        jprime_msg = PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona="j_prime",
            role="mind",
            intent=PersonaIntent.PROPOSE,
            references=[
                "backend/core/health_monitor.py",
                "backend/audio/buffer_pool.py",
            ],
            reasoning="Replace unbounded list with fixed-size ring buffer capped at 64 frames.",
            confidence=0.87,
            model_used=flow_model,
            token_cost=350,
            manifesto_principle="$3 Spinal Cord",
        )
        mgr.add_message(thread.thread_id, jprime_msg)
        assert thread.has_propose()

        # ── 10. Reactor validates with approve verdict ───────────────
        reactor_msg = PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona="reactor",
            role="immune_system",
            intent=PersonaIntent.VALIDATE,
            references=["backend/core/health_monitor.py"],
            reasoning="Ring buffer cap is safe; no data-loss risk at 64 frames. Approved.",
            confidence=0.94,
            model_used=flow_model,
            token_cost=180,
            validate_verdict="approve",
        )
        mgr.add_message(thread.thread_id, reactor_msg)

        # ── 11. check_and_advance detects consensus ──────────────────
        new_state = mgr.check_and_advance(thread.thread_id)
        assert new_state == ThreadState.CONSENSUS
        assert thread.state == ThreadState.CONSENSUS

        # ── 12. serialize_consensus creates OperationContext ──────────
        target_files = (
            "backend/core/health_monitor.py",
            "backend/audio/buffer_pool.py",
        )
        op_ctx = serialize_consensus(thread, target_files=target_files)

        assert isinstance(op_ctx, OperationContext)
        assert op_ctx.phase == OperationPhase.CLASSIFY
        assert op_ctx.causal_trace_id == thread.thread_id
        assert op_ctx.target_files == target_files
        assert "Ring buffer cap is safe" in op_ctx.description
        assert "$3 Spinal Cord" in op_ctx.human_instructions

        # ── 13. Thread linked_op_id set, transitions to EXECUTING ────
        thread.linked_op_id = op_ctx.op_id
        mgr.transition(thread.thread_id, ThreadState.EXECUTING)
        assert thread.state == ThreadState.EXECUTING
        assert thread.linked_op_id == op_ctx.op_id

        # ── 14. Persist thread, verify JSON file exists ──────────────
        mgr.persist_thread(thread.thread_id)
        persisted_path = tmp_path / "threads" / f"{thread.thread_id}.json"
        assert persisted_path.exists()

        persisted_data = json.loads(persisted_path.read_text(encoding="utf-8"))
        assert persisted_data["state"] == "executing"
        assert persisted_data["linked_op_id"] == op_ctx.op_id
        assert len(persisted_data["messages"]) == 4

        # ── 15. FSM spins down -> BASELINE ───────────────────────────
        spindown = fsm.decide(
            CognitiveEvent.SPINDOWN, spindown_reason="pr_merged"
        )
        assert not spindown.noop
        assert spindown.to_state == CognitiveState.BASELINE
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE


class TestBudgetExhaustionPipeline:
    """Budget safety valve: token overspend -> STALE, serialize raises."""

    def test_budget_exhaustion_pipeline(self, tmp_path: str) -> None:
        # ── 1. FSM to FLOW ───────────────────────────────────────────
        fsm = CognitiveFsm(state_file=tmp_path / "cognitive_state.json")
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW

        # ── 2. ThreadManager with very low token_ceiling ─────────────
        mgr = ThreadManager(
            storage_dir=tmp_path / "threads",
            debate_timeout_s=120.0,
            token_ceiling=1000,
        )

        # ── 3. Create thread, transition to DEBATING ─────────────────
        thread = mgr.create_thread(
            title="Overbudget thread",
            trigger_event="test_failure:budget_test",
            cognitive_state=CognitiveState.FLOW,
        )
        mgr.transition(thread.thread_id, ThreadState.DEBATING)
        assert thread.state == ThreadState.DEBATING

        # ── 4. Add message with token_cost exceeding the budget ──────
        expensive_msg = PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona="jarvis",
            role="body",
            intent=PersonaIntent.OBSERVE,
            references=["backend/expensive.py"],
            reasoning="Very long analysis that blew the budget.",
            confidence=0.80,
            model_used="Qwen/Qwen3.5-397B-A17B-FP8",
            token_cost=1001,
        )
        mgr.add_message(thread.thread_id, expensive_msg)
        assert thread.tokens_consumed == 1001
        assert thread.is_budget_exhausted()

        # ── 5. check_and_advance -> STALE ────────────────────────────
        new_state = mgr.check_and_advance(thread.thread_id)
        assert new_state == ThreadState.STALE
        assert thread.state == ThreadState.STALE

        # ── 6. serialize_consensus on STALE thread raises ValueError ─
        with pytest.raises(ValueError, match="expected state CONSENSUS"):
            serialize_consensus(
                thread, target_files=("backend/expensive.py",)
            )
