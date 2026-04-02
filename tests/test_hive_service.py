"""Tests for backend.hive.hive_service — HiveService orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.hive.cognitive_fsm import CognitiveEvent
from backend.hive.hive_service import HiveService
from backend.hive.thread_models import (
    CognitiveState,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)
from backend.neural_mesh.data_models import AgentMessage, MessageType


# ============================================================================
# HELPERS
# ============================================================================


def _make_persona_response(
    reasoning: str,
    confidence: float,
    verdict: str | None = None,
    principle: str | None = None,
) -> str:
    """Build a JSON string mimicking a Doubleword persona response."""
    data: dict = {
        "reasoning": reasoning,
        "confidence": confidence,
    }
    if principle is not None:
        data["manifesto_principle"] = principle
    if verdict is not None:
        data["validate_verdict"] = verdict
    return json.dumps(data)


def _make_agent_message(
    severity: str = "warning",
    category: str = "build",
    agent_name: str = "build_monitor",
) -> AgentMessage:
    """Build an AgentMessage simulating a HIVE_AGENT_LOG event."""
    return AgentMessage(
        from_agent=agent_name,
        message_type=MessageType.HIVE_AGENT_LOG,
        payload={
            "agent_name": agent_name,
            "severity": severity,
            "category": category,
            "trinity_parent": "jarvis",
            "data": {"exit_code": 1},
        },
    )


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_bus() -> AsyncMock:
    bus = AsyncMock()
    bus.subscribe_broadcast = AsyncMock()
    return bus


@pytest.fixture
def mock_governed_loop() -> AsyncMock:
    gl = AsyncMock()
    result = MagicMock()
    result.op_id = "op_test_123"
    gl.submit = AsyncMock(return_value=result)
    return gl


@pytest.fixture
def mock_doubleword() -> AsyncMock:
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(
        return_value=_make_persona_response(
            reasoning="Default observe reasoning.",
            confidence=0.85,
            principle="Absolute Observability",
        )
    )
    return dw


@pytest.fixture
def service(
    mock_bus: AsyncMock,
    mock_governed_loop: AsyncMock,
    mock_doubleword: AsyncMock,
    tmp_path: Path,
) -> HiveService:
    return HiveService(
        bus=mock_bus,
        governed_loop=mock_governed_loop,
        doubleword=mock_doubleword,
        state_dir=tmp_path / "hive_state",
    )


# ============================================================================
# TEST 1: Bus subscription
# ============================================================================


class TestBusSubscription:
    """start() calls bus.subscribe_broadcast with the right args."""

    @pytest.mark.asyncio
    async def test_start_subscribes_to_hive_agent_log(
        self, service: HiveService, mock_bus: AsyncMock
    ) -> None:
        await service.start()
        mock_bus.subscribe_broadcast.assert_awaited_once_with(
            MessageType.HIVE_AGENT_LOG, service._on_agent_log
        )
        await service.stop()


# ============================================================================
# TEST 2: Agent log creates thread
# ============================================================================


class TestAgentLogCreatesThread:
    """_on_agent_log with warning severity creates a thread."""

    @pytest.mark.asyncio
    async def test_warning_creates_thread(
        self, service: HiveService, mock_doubleword: AsyncMock
    ) -> None:
        # Prevent debate from running (we only want to test thread creation)
        service._run_debate_round = AsyncMock()

        msg = _make_agent_message(severity="warning", category="build")
        await service._on_agent_log(msg)

        threads = service.thread_manager.active_threads
        assert len(threads) == 1
        thread = next(iter(threads.values()))
        assert thread.trigger_event == "build"
        assert len(thread.messages) == 1

    @pytest.mark.asyncio
    async def test_info_severity_creates_thread_but_no_escalation(
        self, service: HiveService
    ) -> None:
        service._run_debate_round = AsyncMock()

        msg = _make_agent_message(severity="info", category="test")
        await service._on_agent_log(msg)

        # Thread created, but FSM stays BASELINE (no FLOW trigger)
        assert service.fsm.state == CognitiveState.BASELINE
        threads = service.thread_manager.active_threads
        assert len(threads) == 1


# ============================================================================
# TEST 3: Full debate reaches consensus
# ============================================================================


class TestFullDebateConsensus:
    """Mock doubleword returns observe/propose/approve -> CONSENSUS -> EXECUTING."""

    @pytest.mark.asyncio
    async def test_debate_reaches_consensus(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
        mock_governed_loop: AsyncMock,
    ) -> None:
        observe_resp = _make_persona_response(
            reasoning="Build failure detected in backend.core.",
            confidence=0.90,
            principle="Absolute Observability",
        )
        propose_resp = _make_persona_response(
            reasoning="Fix the import in backend/core/router.py.",
            confidence=0.88,
            principle="Progressive Awakening",
        )
        approve_resp = _make_persona_response(
            reasoning="Proposal is safe, minimal blast radius.",
            confidence=0.92,
            principle="Iron Gate",
            verdict="approve",
        )

        mock_doubleword.prompt_only = AsyncMock(
            side_effect=[observe_resp, propose_resp, approve_resp]
        )

        # Create a thread manually in DEBATING state within FLOW
        decision = service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        thread = service.thread_manager.create_thread(
            title="build regression",
            trigger_event="build",
            cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        await service._run_debate_round(thread.thread_id)

        # Thread should now be EXECUTING (CONSENSUS -> governed_loop.submit -> EXECUTING)
        updated = service.thread_manager.get_thread(thread.thread_id)
        assert updated.state == ThreadState.EXECUTING
        assert updated.linked_op_id is not None
        mock_governed_loop.submit.assert_awaited_once()


# ============================================================================
# TEST 4: Reject triggers retry
# ============================================================================


class TestRejectTriggersRetry:
    """First validate rejects, J-Prime proposes again, second validate approves."""

    @pytest.mark.asyncio
    async def test_reject_then_approve(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
        mock_governed_loop: AsyncMock,
    ) -> None:
        observe_resp = _make_persona_response(
            reasoning="Observing the build failure.",
            confidence=0.85,
        )
        propose_resp_1 = _make_persona_response(
            reasoning="First proposal: add missing import.",
            confidence=0.80,
        )
        reject_resp = _make_persona_response(
            reasoning="Missing rollback strategy.",
            confidence=0.70,
            verdict="reject",
        )
        propose_resp_2 = _make_persona_response(
            reasoning="Revised proposal: add import with rollback.",
            confidence=0.88,
        )
        approve_resp = _make_persona_response(
            reasoning="Revised proposal is safe.",
            confidence=0.91,
            verdict="approve",
        )

        mock_doubleword.prompt_only = AsyncMock(
            side_effect=[
                observe_resp,
                propose_resp_1,
                reject_resp,
                propose_resp_2,
                approve_resp,
            ]
        )

        # Setup FLOW state + thread
        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        thread = service.thread_manager.create_thread(
            title="build regression",
            trigger_event="build",
            cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        await service._run_debate_round(thread.thread_id)

        updated = service.thread_manager.get_thread(thread.thread_id)
        assert updated.state == ThreadState.EXECUTING

        # 5 persona messages: observe + propose + reject + propose + approve
        persona_msgs = [
            m for m in updated.messages if isinstance(m, PersonaReasoningMessage)
        ]
        assert len(persona_msgs) == 5
        assert mock_doubleword.prompt_only.call_count == 5


# ============================================================================
# TEST 5: Max rejects goes STALE
# ============================================================================


class TestMaxRejectsStale:
    """All validates reject -> thread goes STALE after MAX_REJECTS."""

    @pytest.mark.asyncio
    async def test_max_rejects_marks_stale(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
    ) -> None:
        observe_resp = _make_persona_response(
            reasoning="Observing failure.",
            confidence=0.85,
        )
        propose_resp = _make_persona_response(
            reasoning="Proposal attempt.",
            confidence=0.80,
        )
        reject_resp = _make_persona_response(
            reasoning="Not safe enough.",
            confidence=0.60,
            verdict="reject",
        )

        # observe, then 2 rounds of (propose, reject)
        mock_doubleword.prompt_only = AsyncMock(
            side_effect=[
                observe_resp,
                propose_resp,
                reject_resp,
                propose_resp,
                reject_resp,
            ]
        )

        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        thread = service.thread_manager.create_thread(
            title="stubborn issue",
            trigger_event="build",
            cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        await service._run_debate_round(thread.thread_id)

        updated = service.thread_manager.get_thread(thread.thread_id)
        assert updated.state == ThreadState.STALE
        assert thread.thread_id not in service._flow_thread_ids


# ============================================================================
# TEST 6: Consensus submits to governed loop
# ============================================================================


class TestConsensusSubmitsToGovernedLoop:
    """Verify governed_loop.submit() called with correct OperationContext."""

    @pytest.mark.asyncio
    async def test_submit_called_with_context(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
        mock_governed_loop: AsyncMock,
    ) -> None:
        observe_resp = _make_persona_response(
            reasoning="Observing regression in backend/core/router.py.",
            confidence=0.90,
        )
        propose_resp = _make_persona_response(
            reasoning="Fix import in backend/core/router.py.",
            confidence=0.88,
        )
        approve_resp = _make_persona_response(
            reasoning="Approved: minimal blast radius.",
            confidence=0.92,
            verdict="approve",
        )

        mock_doubleword.prompt_only = AsyncMock(
            side_effect=[observe_resp, propose_resp, approve_resp]
        )

        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        thread = service.thread_manager.create_thread(
            title="router regression",
            trigger_event="build",
            cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        await service._run_debate_round(thread.thread_id)

        mock_governed_loop.submit.assert_awaited_once()
        call_args = mock_governed_loop.submit.call_args
        ctx = call_args.args[0] if call_args.args else call_args.kwargs.get("ctx")
        trigger = call_args.kwargs.get(
            "trigger_source",
            call_args.args[1] if len(call_args.args) > 1 else None,
        )
        assert trigger == "hive_consensus"
        # Context should have the thread_id as correlation_id
        assert ctx.correlation_id == thread.thread_id


# ============================================================================
# TEST 7: Thread transitions to EXECUTING after consensus
# ============================================================================


class TestThreadExecutingAfterConsensus:
    """Verify linked_op_id set and thread reaches EXECUTING."""

    @pytest.mark.asyncio
    async def test_executing_with_linked_op_id(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
        mock_governed_loop: AsyncMock,
    ) -> None:
        observe_resp = _make_persona_response(
            reasoning="Observed.", confidence=0.85
        )
        propose_resp = _make_persona_response(
            reasoning="Proposed.", confidence=0.88
        )
        approve_resp = _make_persona_response(
            reasoning="Approved.", confidence=0.92, verdict="approve"
        )

        mock_doubleword.prompt_only = AsyncMock(
            side_effect=[observe_resp, propose_resp, approve_resp]
        )

        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        thread = service.thread_manager.create_thread(
            title="linked op test",
            trigger_event="build",
            cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        await service._run_debate_round(thread.thread_id)

        updated = service.thread_manager.get_thread(thread.thread_id)
        assert updated.state == ThreadState.EXECUTING
        assert updated.linked_op_id == "op_test_123"


# ============================================================================
# TEST 8: All threads resolved fires SPINDOWN
# ============================================================================


class TestSpindownOnAllResolved:
    """Empty _flow_thread_ids -> FSM returns to BASELINE."""

    @pytest.mark.asyncio
    async def test_spindown_after_all_threads_resolved(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
        mock_governed_loop: AsyncMock,
    ) -> None:
        observe_resp = _make_persona_response(
            reasoning="Observed.", confidence=0.85
        )
        propose_resp = _make_persona_response(
            reasoning="Proposed.", confidence=0.88
        )
        approve_resp = _make_persona_response(
            reasoning="Approved.", confidence=0.92, verdict="approve"
        )

        mock_doubleword.prompt_only = AsyncMock(
            side_effect=[observe_resp, propose_resp, approve_resp]
        )

        # Move to FLOW
        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()
        assert service.fsm.state == CognitiveState.FLOW

        thread = service.thread_manager.create_thread(
            title="last thread",
            trigger_event="build",
            cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        await service._run_debate_round(thread.thread_id)

        # After the only flow thread reaches consensus, FSM should spin down
        assert service.fsm.state == CognitiveState.BASELINE
        assert len(service._flow_thread_ids) == 0
