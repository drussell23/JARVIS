"""End-to-end wiring test: ``emit_postmortem`` → ``ConversationBridge`` → prompt.

This is the lightest possible *integration* test (not unit) — it boots a
real :class:`CommProtocol` in-process and calls ``await emit_postmortem``
to exercise the wiring my v1.1 edit added at ``comm_protocol.py:347+``.
If this test passes, the code that runs inside a live battle-test
POSTMORTEM path is proven working without paying provider cost or waiting
for a full 10-minute autonomous session.

What this covers that existing tests do not:
  * ``test_conversation_bridge_v1_1.py::test_cross_op_postmortem_appears_…``
    calls ``bridge.record_turn(source="postmortem", …)`` directly,
    bypassing the ``emit_postmortem`` hook. So it proves the bridge's
    storage shape, not the hook itself.
  * This test calls ``comm.emit_postmortem(…)`` and asserts the bridge
    received the turn — proving the glue code in ``comm_protocol.py``
    that imports/calls ``format_postmortem_payload`` and ``record_turn``
    is correctly wired and non-raising.

If v1.2 or later re-plumbs the hook (e.g. via an event bus), this test
is the canary — it fails fast when the hook path regresses.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from backend.core.ouroboros.governance import conversation_bridge as cb
from backend.core.ouroboros.governance.comm_protocol import CommProtocol


@pytest.fixture(autouse=True)
def _reset_env_and_singleton(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_CONVERSATION_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    cb.reset_default_bridge()
    yield
    cb.reset_default_bridge()


def test_emit_postmortem_captures_to_bridge(monkeypatch):
    """Real CommProtocol → real bridge hook → assertable turn.

    Simulates the POSTMORTEM path an op takes when VERIFY catches a
    regression (the most common non-trivial POSTMORTEM trigger in
    battle-test).
    """
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")

    comm = CommProtocol()  # default LogTransport — no external I/O
    bridge = cb.get_default_bridge()

    # Fire the hook. This is the exact call-shape orchestrator.py uses
    # at line 5266 when VERIFY finds a regression.
    asyncio.run(comm.emit_postmortem(
        op_id="op-e2e-1",
        root_cause="scoped verify: 2/8 tests failing after multi-file apply",
        failed_phase="VERIFY",
        target_files=["backend/foo.py", "backend/bar.py"],
    ))

    # --- Assert: the bridge captured a postmortem turn ---
    snap = bridge.snapshot()
    pm_turns = [t for t in snap if t.source == "postmortem"]
    assert len(pm_turns) == 1, (
        f"expected exactly 1 postmortem turn, got {len(pm_turns)} "
        f"(snap: {[t.source for t in snap]})"
    )

    pm = pm_turns[0]
    assert pm.role == "assistant"
    assert pm.op_id == "op-e2e-1"
    assert "op=op-e2e-1" in pm.text
    assert "outcome=VERIFY" in pm.text
    assert "root_cause=scoped verify" in pm.text
    assert "2/8 tests failing" in pm.text

    # --- Assert: the prompt surfaces it under the right subheader ---
    prompt = bridge.format_for_prompt()
    assert prompt is not None
    assert "### Prior op closure (postmortem)" in prompt
    assert "[postmortem op=op-e2e-1]" in prompt
    assert "2/8 tests failing" in prompt


def test_emit_postmortem_skips_empty_root_cause(monkeypatch, caplog):
    """Clean successes (empty root_cause) produce no conversational turn.

    Verifies the ``format_postmortem_payload`` skip path wires correctly
    through the hook — we should see a DEBUG "skipped postmortem" log
    line, but no turn in the bridge.
    """
    import logging
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    caplog.set_level(logging.DEBUG, logger="backend.core.ouroboros.governance.comm_protocol")

    comm = CommProtocol()
    bridge = cb.get_default_bridge()

    asyncio.run(comm.emit_postmortem(
        op_id="op-e2e-clean",
        root_cause="",  # clean success
        failed_phase="VERIFY",
    ))

    snap = bridge.snapshot()
    assert [t for t in snap if t.source == "postmortem"] == []

    # DEBUG log names the op — proves the skip path ran, not a silent drop.
    debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "skipped postmortem" in m and "op-e2e-clean" in m
        for m in debug_msgs
    ), f"expected 'skipped postmortem op=op-e2e-clean' in DEBUG; saw: {debug_msgs}"


def test_emit_postmortem_no_op_when_bridge_disabled():
    """With master switch off, emit_postmortem completes cleanly + no turn lands.

    Proves the hook itself never raises on the disabled path — no
    ``postmortem capture failed`` DEBUG, no exception escaping back into
    the orchestrator's POSTMORTEM flow.
    """
    # Env intentionally unset — master switch off.
    comm = CommProtocol()
    bridge = cb.get_default_bridge()

    asyncio.run(comm.emit_postmortem(
        op_id="op-e2e-disabled",
        root_cause="doesn't matter",
        failed_phase="VERIFY",
    ))

    # No turn recorded (bridge disabled).
    assert bridge.snapshot() == []
    # No exception propagated from emit_postmortem → we got here.


def test_cross_op_postmortem_emerges_in_next_op_format(monkeypatch):
    """Full loop: op1 emit_postmortem → op2 format_for_prompt sees it.

    This is the battle-test behavior in miniature — op-1 concludes with
    a root_cause, op-2 begins, its CONTEXT_EXPANSION reads the bridge,
    the prompt contains op-1's closure line.
    """
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")

    comm = CommProtocol()
    bridge = cb.get_default_bridge()

    # op-1 concludes.
    asyncio.run(comm.emit_postmortem(
        op_id="op-first",
        root_cause="test passed after adding missing fixture",
        failed_phase="VERIFY",
    ))

    # op-2 begins — orchestrator calls bridge.format_for_prompt at
    # CONTEXT_EXPANSION (reproduced here directly).
    op2_prompt = bridge.format_for_prompt()
    assert op2_prompt is not None
    assert "[postmortem op=op-first]" in op2_prompt
    assert "test passed after adding missing fixture" in op2_prompt
    assert "### Prior op closure (postmortem)" in op2_prompt
    assert "### TUI user intent" not in op2_prompt  # no user turn seeded
    assert "### Clarifications (recent)" not in op2_prompt  # no ask_human
