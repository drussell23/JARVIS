"""Vision providers are tried cheapest first, each paid lane asking the one
authority: DoubleWord Qwen3-VL-235B -> Claude -> the local model ($0)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from backend.vision.lean_loop import LeanVisionLoop

ANSWER = {"goal_achieved": False, "next_action": {"action": "wait"}}


def _think(allowed, dw=ANSWER, claude=ANSWER, local=ANSWER):
    loop = LeanVisionLoop.__new__(LeanVisionLoop)
    calls = []

    def rec(name, value):
        async def ask(*_a, **_k):
            calls.append(name)
            return value
        return ask

    loop._agentic_ask_doubleword = rec("doubleword", dw)
    loop._agentic_ask_claude = rec("claude", claude)
    loop._agentic_ask_jprime = rec("local", local)
    with patch("backend.core.ouroboros.governance.paid_lanes.paid_lane_allowed",
               side_effect=lambda lane: lane in allowed):
        out = asyncio.run(loop._agentic_think("g", [("AAAA", "")], [], 1))
    return out, calls


def test_doubleword_leads_when_allowed():
    out, calls = _think({"doubleword", "claude"})
    assert calls == ["doubleword"] and out == ANSWER


def test_claude_is_the_paid_fallback():
    _, calls = _think({"doubleword", "claude"}, dw=None)
    assert calls == ["doubleword", "claude"]


def test_a_lane_the_authority_refuses_is_never_called():
    _, calls = _think({"claude"})
    assert calls == ["claude"]
    _, calls = _think(set())
    assert calls == ["local"]


def test_claude_computer_use_runs_only_when_the_agentic_loop_could_not_start():
    loop = LeanVisionLoop.__new__(LeanVisionLoop)
    loop._loop_agentic = AsyncMock(return_value={"success": False, "result": "tried"})
    loop._loop_computer_use = AsyncMock(return_value={"success": True})
    with patch("backend.core.ouroboros.governance.paid_lanes.paid_lane_allowed", return_value=True):
        out = asyncio.run(loop._loop("g"))
    loop._loop_computer_use.assert_not_called()          # never re-acts after a real attempt
    assert out["result"] == "tried"
