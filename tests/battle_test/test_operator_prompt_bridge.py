"""Operator Prompt Bridge — attached cockpits ANSWER the organism's
[Y/n] gates (cockpit-completeness follow-up, 2026-07-23).

Covers: the single-slot registry (begin/resolve/end/supersede), the
harness input-handler precedence (answer BEFORE REPL chat), and the
Iron Gate race (cockpit answer wins, local surface never lost, decision
mirrored with its source, slot always disarmed).
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.operator_prompt_bridge import (
    OperatorPromptBridge,
    get_operator_prompt_bridge,
    reset_bridge_for_tests,
)


@pytest.fixture(autouse=True)
def _fresh_bridge():
    reset_bridge_for_tests()
    yield
    reset_bridge_for_tests()


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


async def test_begin_resolve_roundtrip():
    b = OperatorPromptBridge()
    fut = b.begin("iron-gate:abc")
    assert fut is not None and b.waiting
    assert b.resolve("  Y  ") is True
    assert await asyncio.wait_for(fut, 1.0) == "Y"
    assert b.waiting is False


async def test_resolve_declines_without_pending_prompt():
    b = OperatorPromptBridge()
    assert b.resolve("hello organism") is False   # flows to REPL untouched


async def test_second_begin_supersedes_first():
    b = OperatorPromptBridge()
    f1 = b.begin("p1")
    f2 = b.begin("p2")
    await asyncio.sleep(0)
    assert f1.cancelled()                          # superseded → local fallback
    assert b.resolve("n") is True
    assert await asyncio.wait_for(f2, 1.0) == "n"


async def test_end_disarms_only_own_slot():
    b = OperatorPromptBridge()
    f1 = b.begin("p1")
    f2 = b.begin("p2")                             # owns the slot now
    b.end(f1)                                      # stale end — must not disarm p2
    assert b.waiting
    b.end(f2)
    assert not b.waiting


async def test_master_gate_off_disables(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATOR_PROMPT_BRIDGE_ENABLED", "0")
    b = OperatorPromptBridge()
    assert b.begin("p") is None
    assert b.resolve("y") is False


# ---------------------------------------------------------------------------
# Harness input precedence (wiring invariant + behavior)
# ---------------------------------------------------------------------------


def test_harness_consults_bridge_before_repl():
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/battle_test/harness.py"
    ).read_text()
    on_input = src.split("def _on_input(")[1].split("def ")[0]
    assert on_input.index("get_operator_prompt_bridge") < on_input.index(
        "_handle_repl_command"
    )


# ---------------------------------------------------------------------------
# Iron Gate race
# ---------------------------------------------------------------------------


def _gate_flow():
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    sf = SerpentFlow(session_id="t", branch_name="b")
    sf._mirrored = []
    sf.markup_mirror = sf._mirrored.append
    return sf


async def test_iron_gate_cockpit_answer_wins(monkeypatch):
    """An attached operator's 'y' resolves the gate; the decision and its
    SOURCE mirror back to every cockpit."""
    import backend.core.ouroboros.battle_test.serpent_flow as sfm
    monkeypatch.setattr(sfm, "_headless_auto_approve_reason", lambda: None)
    flow = _gate_flow()

    async def _answer_soon():
        for _ in range(200):
            await asyncio.sleep(0.02)
            if get_operator_prompt_bridge().waiting:
                assert get_operator_prompt_bridge().resolve("y")
                return
        raise AssertionError("gate never armed the bridge")

    answerer = asyncio.ensure_future(_answer_soon())
    approved = await asyncio.wait_for(
        flow.request_execution_permission(
            op_id="op-abc123", description="test change",
            target_files=["a.py"],
        ),
        timeout=10.0,
    )
    await answerer
    assert approved is True
    joined = "\n".join(str(m) for m in flow._mirrored)
    assert "Iron Gate" in joined
    assert "Apply this change?" in joined          # answerable prompt line
    assert "via cockpit" in joined                 # §7: source visible
    assert not get_operator_prompt_bridge().waiting  # slot disarmed


async def test_iron_gate_cockpit_rejection(monkeypatch):
    import backend.core.ouroboros.battle_test.serpent_flow as sfm
    monkeypatch.setattr(sfm, "_headless_auto_approve_reason", lambda: None)
    flow = _gate_flow()

    async def _answer_soon():
        while not get_operator_prompt_bridge().waiting:
            await asyncio.sleep(0.02)
        get_operator_prompt_bridge().resolve("n")

    answerer = asyncio.ensure_future(_answer_soon())
    approved = await asyncio.wait_for(
        flow.request_execution_permission(
            op_id="op-abc123", description="risky change",
            target_files=["a.py"],
        ),
        timeout=10.0,
    )
    await answerer
    assert approved is False
    assert any("rejected" in str(m) for m in flow._mirrored)
