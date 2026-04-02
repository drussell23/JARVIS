"""
Integration tests for HiveService — agent_log arrival through Ouroboros submit.

Exercises the full HiveService orchestrator end-to-end with mocked external
dependencies (bus, governed_loop, doubleword) to verify:
  1. An agent_log with warning severity triggers the full pipeline:
     FLOW -> debate (observe/propose/validate) -> consensus -> submit -> EXECUTING
     -> spindown to BASELINE
  2. An info-severity agent_log does NOT trigger FLOW or any debate.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.hive.hive_service import HiveService
from backend.hive.thread_models import CognitiveState, ThreadState
from backend.neural_mesh.data_models import AgentMessage, MessageType


# ============================================================================
# HELPERS
# ============================================================================


def _response(
    reasoning: str,
    confidence: float = 0.87,
    verdict: str | None = None,
    principle: str | None = None,
) -> str:
    """Build a JSON string mimicking a Doubleword persona response."""
    d: dict = {"reasoning": reasoning, "confidence": confidence}
    if verdict:
        d["validate_verdict"] = verdict
    if principle:
        d["manifesto_principle"] = principle
    return json.dumps(d)


def _make_agent_message(
    severity: str = "warning",
    category: str = "memory_pressure",
    agent_name: str = "health_monitor",
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
            "data": {"rss_mb": 1420, "threshold_mb": 1200},
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
    result.op_id = "op_integration_001"
    gl.submit = AsyncMock(return_value=result)
    return gl


@pytest.fixture
def mock_doubleword() -> AsyncMock:
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(
        return_value=_response("default fallback", confidence=0.50)
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
        state_dir=tmp_path / "hive_integration_state",
    )


# ============================================================================
# TEST 1: agent_log with warning triggers full pipeline
# ============================================================================


class TestAgentLogTriggersFullPipeline:
    """End-to-end: warning agent_log -> FLOW -> debate -> consensus -> submit -> BASELINE."""

    @pytest.mark.asyncio
    async def test_agent_log_triggers_full_pipeline(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
        mock_governed_loop: AsyncMock,
    ) -> None:
        # Configure doubleword to return 3 sequential responses:
        # observe, propose, validate(approve)
        observe_resp = _response(
            reasoning="Memory pressure detected: RSS 1420 MB exceeds 1200 MB threshold.",
            confidence=0.90,
            principle="Absolute Observability",
        )
        propose_resp = _response(
            reasoning="Cap audio buffer pool at 64 frames via ring buffer in backend/audio/buffer_pool.py.",
            confidence=0.88,
            principle="Progressive Awakening",
        )
        validate_resp = _response(
            reasoning="Ring buffer cap is safe, minimal blast radius. Approved.",
            confidence=0.92,
            verdict="approve",
            principle="Iron Gate",
        )

        mock_doubleword.prompt_only = AsyncMock(
            side_effect=[observe_resp, propose_resp, validate_resp]
        )

        # Start the service (subscribes to bus, starts REM poll)
        await service.start()

        try:
            # Simulate an AgentMessage arriving with warning severity
            msg = _make_agent_message(
                severity="warning", category="memory_pressure"
            )

            # Call _on_agent_log directly (in production the bus delivers this)
            await service._on_agent_log(msg)

            # Give the event loop a moment to start the background debate task
            await asyncio.sleep(0.05)

            # Find and await any pending tasks (the debate is spawned via create_task)
            tasks = [
                t
                for t in asyncio.all_tasks()
                if not t.done() and t is not asyncio.current_task()
            ]
            for t in tasks:
                # Skip the REM poll loop — it runs forever
                if "rem_poll" in str(t.get_coro()):
                    continue
                try:
                    await asyncio.wait_for(t, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            # Verify: doubleword.prompt_only called exactly 3 times
            # (observe, propose, validate)
            assert mock_doubleword.prompt_only.call_count == 3

            # Verify: governed_loop.submit called exactly once
            mock_governed_loop.submit.assert_awaited_once()
            call_args = mock_governed_loop.submit.call_args
            trigger = call_args.kwargs.get(
                "trigger_source",
                call_args.args[1] if len(call_args.args) > 1 else None,
            )
            assert trigger == "hive_consensus"

            # Verify: a thread exists in EXECUTING state
            threads = service.thread_manager._threads
            assert len(threads) >= 1
            executing_threads = [
                t for t in threads.values() if t.state == ThreadState.EXECUTING
            ]
            assert len(executing_threads) == 1
            assert executing_threads[0].linked_op_id == "op_integration_001"

            # Verify: FSM returned to BASELINE (all flow threads resolved,
            # SPINDOWN fired automatically in _check_flow_completion)
            assert service.fsm.state == CognitiveState.BASELINE
        finally:
            await service.stop()


# ============================================================================
# TEST 2: info severity does not trigger FLOW
# ============================================================================


class TestInfoSeverityDoesNotTriggerFlow:
    """An info-severity agent_log should NOT escalate to FLOW or spawn debate."""

    @pytest.mark.asyncio
    async def test_info_severity_does_not_trigger_flow(
        self,
        service: HiveService,
        mock_doubleword: AsyncMock,
    ) -> None:
        await service.start()

        try:
            # Send an info-severity message
            msg = _make_agent_message(severity="info", category="heartbeat")
            await service._on_agent_log(msg)

            # Brief pause in case anything was spawned
            await asyncio.sleep(0.05)

            # FSM should remain in BASELINE — no FLOW trigger for "info"
            assert service.fsm.state == CognitiveState.BASELINE

            # No doubleword calls should have been made (no debate)
            assert mock_doubleword.prompt_only.call_count == 0

            # A thread was created (agent_log always creates a thread),
            # but it stays OPEN — never transitions to DEBATING
            threads = service.thread_manager.active_threads
            assert len(threads) == 1
            thread = next(iter(threads.values()))
            assert thread.state == ThreadState.OPEN
        finally:
            await service.stop()
