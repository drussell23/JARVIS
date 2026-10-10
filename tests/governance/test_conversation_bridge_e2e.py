"""End-to-end: ``emit_postmortem`` does NOT copy an op's closure into the
ConversationBridge.

This file used to pin the opposite (v1.1 copied every postmortem into the
bridge). Since ffc3bb1c15 (memory hygiene, 2026-10-09) failure memory has
ONE owner each: PostmortemRecall (similarity-matched, newest sessions first)
and LessonMemory (module-keyed, reinforced on a later pass). The bridge
carries DIALOGUE (operator, ask_human, voice), and the copy put the same
failure into the next op's prompt twice.

These boot a real :class:`CommProtocol` and call ``emit_postmortem`` so the
contract is asserted on the live path, with the bridge ENABLED (the case in
which the old hook would have written).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import conversation_bridge as cb
from backend.core.ouroboros.governance.comm_protocol import CommProtocol


@pytest.fixture(autouse=True)
def _fresh_bridge():
    cb.reset_default_bridge()
    yield
    cb.reset_default_bridge()


def test_a_postmortem_is_not_copied_into_the_bridge(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    bridge = cb.get_default_bridge()
    before = list(bridge.snapshot())
    asyncio.run(CommProtocol().emit_postmortem(
        op_id="op-e2e-1", root_cause="VERIFY failed: 2 tests red",
        failed_phase="VERIFY",
    ))
    after = bridge.snapshot()
    assert [t for t in after if t not in before and getattr(t, "source", "") == "postmortem"] == []


def test_the_next_ops_bridge_prompt_does_not_repeat_the_failure(monkeypatch):
    """The duplication the removal fixed: op-1's closure must not reach op-2
    through the bridge (PostmortemRecall carries it once)."""
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    asyncio.run(CommProtocol().emit_postmortem(
        op_id="op-first", root_cause="test passed after adding missing fixture",
        failed_phase="VERIFY",
    ))
    prompt = cb.get_default_bridge().format_for_prompt() or ""
    assert "adding missing fixture" not in prompt


def test_emit_postmortem_completes_with_the_bridge_disabled():
    """The hook's absence must not change the emit path: no exception escapes
    into the orchestrator's POSTMORTEM flow."""
    asyncio.run(CommProtocol().emit_postmortem(
        op_id="op-e2e-disabled", root_cause="doesn't matter", failed_phase="VERIFY",
    ))
