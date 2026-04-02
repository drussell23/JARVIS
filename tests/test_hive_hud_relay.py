"""Tests for HudRelayAgent — bus-to-IPC projection layer."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.thread_models import (
    AgentLogMessage,
    PersonaIntent,
    PersonaReasoningMessage,
)


# ── helpers ────────────────────────────────────────────────────────────


def _make_agent_log(**overrides) -> AgentLogMessage:
    defaults = dict(
        thread_id="thr_test123",
        agent_name="build_watcher",
        trinity_parent="jarvis",
        severity="info",
        category="build",
        payload={"status": "ok"},
    )
    defaults.update(overrides)
    return AgentLogMessage(**defaults)


def _make_persona_reasoning(**overrides) -> PersonaReasoningMessage:
    defaults = dict(
        thread_id="thr_test456",
        persona="j_prime",
        role="mind",
        intent=PersonaIntent.PROPOSE,
        references=["backend/foo.py"],
        reasoning="We should refactor foo.",
        confidence=0.92,
        model_used="qwen-7b",
        token_cost=340,
    )
    defaults.update(overrides)
    return PersonaReasoningMessage(**defaults)


# ── projection tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_agent_log():
    """project_message with AgentLogMessage produces event_type='agent_log' and includes agent_name."""
    sender = AsyncMock()
    relay = HudRelayAgent(ipc_send=sender)

    msg = _make_agent_log(agent_name="lint_checker")
    await relay.project_message(msg)

    sender.assert_awaited_once()
    envelope = sender.call_args[0][0]
    assert envelope["event_type"] == "agent_log"
    assert envelope["data"]["agent_name"] == "lint_checker"
    assert "_seq" in envelope["data"]


@pytest.mark.asyncio
async def test_project_persona_reasoning():
    """project_message with PersonaReasoningMessage produces event_type='persona_reasoning'."""
    sender = AsyncMock()
    relay = HudRelayAgent(ipc_send=sender)

    msg = _make_persona_reasoning()
    await relay.project_message(msg)

    sender.assert_awaited_once()
    envelope = sender.call_args[0][0]
    assert envelope["event_type"] == "persona_reasoning"
    assert envelope["data"]["persona"] == "j_prime"


@pytest.mark.asyncio
async def test_project_thread_lifecycle():
    """project_lifecycle sends event_type='thread_lifecycle' with thread_id and state."""
    sender = AsyncMock()
    relay = HudRelayAgent(ipc_send=sender)

    await relay.project_lifecycle("thr_abc", "resolved", metadata={"reason": "consensus"})

    sender.assert_awaited_once()
    envelope = sender.call_args[0][0]
    assert envelope["event_type"] == "thread_lifecycle"
    assert envelope["data"]["thread_id"] == "thr_abc"
    assert envelope["data"]["state"] == "resolved"
    assert envelope["data"]["reason"] == "consensus"
    assert "_seq" in envelope["data"]


@pytest.mark.asyncio
async def test_project_cognitive_transition():
    """project_cognitive_transition sends from_state, to_state, reason_code."""
    sender = AsyncMock()
    relay = HudRelayAgent(ipc_send=sender)

    await relay.project_cognitive_transition("baseline", "flow", "deep_focus_detected")

    sender.assert_awaited_once()
    envelope = sender.call_args[0][0]
    assert envelope["event_type"] == "cognitive_transition"
    assert envelope["data"]["from_state"] == "baseline"
    assert envelope["data"]["to_state"] == "flow"
    assert envelope["data"]["reason_code"] == "deep_focus_detected"
    assert "_seq" in envelope["data"]


@pytest.mark.asyncio
async def test_ipc_failure_does_not_raise():
    """ConnectionError from _ipc_send is caught — relay never propagates transport errors."""
    sender = AsyncMock(side_effect=ConnectionError("IPC down"))
    relay = HudRelayAgent(ipc_send=sender)

    # Must not raise
    await relay.project_message(_make_agent_log())
    await relay.project_lifecycle("thr_x", "stale")
    await relay.project_cognitive_transition("rem", "baseline", "timeout")

    assert sender.await_count == 3


@pytest.mark.asyncio
async def test_monotonic_sequence():
    """Two successive messages have strictly incrementing _seq values."""
    sender = AsyncMock()
    relay = HudRelayAgent(ipc_send=sender)

    await relay.project_message(_make_agent_log())
    await relay.project_message(_make_persona_reasoning())

    first_seq = sender.call_args_list[0][0][0]["data"]["_seq"]
    second_seq = sender.call_args_list[1][0][0]["data"]["_seq"]
    assert second_seq > first_seq
    assert first_seq == 1
    assert second_seq == 2
