"""Agentic Super-Agent — goal-bounded autonomous ReAct workers, swarmed.

Mandated bulletproof: 3 Agentic Super-Agents spawned CONCURRENTLY to tackle 3
separate AST nodes; ONE agent makes a syntax error, SELF-CORRECTS within its own
ReAct loop, and returns a valid AST patch that successfully stitches into the
main file — while the other two run in parallel.
"""

from __future__ import annotations

import ast
import asyncio

import pytest

from backend.core.ouroboros.governance.agentic_super_agent import (
    STATUS_UNCONVERGED,
    ChunkTarget,
    run_agentic_repair,
    swarm_agentic_repair,
)
from backend.core.ouroboros.governance.chunked_generation import (
    extract_target_chunk,
)

_FILE = '''"""Module with three buggy functions."""


def alpha(a, b):
    return a - b


def _pad():
    return 0


def beta(a, b):
    return a * a


def _pad2():
    return 0


def gamma(xs):
    return xs[0]
'''

_FIX = {
    "alpha": "def alpha(a, b):\n    return a + b",
    "beta": "def beta(a, b):\n    return a * b",
    "gamma": "def gamma(xs):\n    return sorted(xs)[0]",
}


def _targets():
    ts = []
    for sym in ("alpha", "beta", "gamma"):
        chunk = extract_target_chunk(_FILE, "m.py", sym)
        assert chunk is not None, sym
        ts.append(ChunkTarget(symbol=sym, chunk=chunk, instruction=f"fix {sym}"))
    return ts


async def test_three_agentic_agents_one_self_corrects_and_all_stitch() -> None:
    targets = _targets()
    turns = {}
    feedbacks = {}
    started = {"n": 0}
    all_started = asyncio.Event()

    async def agent_fn(target: ChunkTarget, feedback: str) -> str:
        sym = target.symbol
        turns[sym] = turns.get(sym, 0) + 1
        feedbacks.setdefault(sym, []).append(feedback)
        # Prove concurrency: all 3 agents must reach their first turn before any
        # proceeds. Sequential execution would deadlock this barrier.
        if turns[sym] == 1:
            started["n"] += 1
            if started["n"] == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=5.0)
        # BETA makes a SYNTAX ERROR on turn 1, self-corrects on turn 2.
        if sym == "beta" and turns[sym] == 1:
            return "def beta(a, b):\n    return a * b (((("   # broken node
        return _FIX[sym]

    result = await swarm_agentic_repair(_FILE, "m.py", targets, agent_fn, max_turns=5)

    # (1) 3 Agentic Super-Agents ran CONCURRENTLY (barrier proves parallelism).
    assert result.max_in_flight == 3
    assert result.agents_spawned == 3

    # (2) The erroring agent SELF-CORRECTED within its own ReAct loop (2 turns),
    # and the exact verify error was fed back for the refine.
    assert turns["beta"] == 2
    assert turns["alpha"] == 1 and turns["gamma"] == 1
    assert "SyntaxError" in feedbacks["beta"][1]
    assert "REFINE" in feedbacks["beta"][1]

    # (3) All 3 valid patches stitched into the main file — it parses + runs.
    assert set(result.succeeded) == {"alpha", "beta", "gamma"}
    assert result.failed == []
    ast.parse(result.stitched)
    ns = {}
    exec(compile(result.stitched, "s", "exec"), ns)  # noqa: S102 — test only
    assert ns["alpha"](5, 3) == 8
    assert ns["beta"](5, 3) == 15
    assert ns["gamma"]([3, 1, 2]) == 1


async def test_agent_emits_unconverged_after_max_turns() -> None:
    """A non-converging agent hits its max-turn guardrail and emits
    agent_unconverged instead of hanging or burning tokens forever."""
    target = _targets()[0]

    async def always_broken(target, feedback):
        return "def alpha(a, b):\n    return a + b ((("  # never parses

    outcome = await run_agentic_repair(target, always_broken, max_turns=3)
    assert outcome.status == STATUS_UNCONVERGED
    assert outcome.converged is False
    assert outcome.node is None
    assert outcome.turns == 3
    assert "SyntaxError" in outcome.last_error


async def test_unconverged_agent_isolated_others_still_stitch() -> None:
    """One unconverged agent leaves its node untouched (atomic invariant); the
    converged agents still land."""
    targets = _targets()

    async def agent_fn(target, feedback):
        if target.symbol == "beta":
            return "def beta(a, b):\n    return ((("   # never converges
        return _FIX[target.symbol]

    result = await swarm_agentic_repair(_FILE, "m.py", targets, agent_fn, max_turns=2)
    assert "beta" in result.failed
    assert "alpha" in result.succeeded and "gamma" in result.succeeded
    ast.parse(result.stitched)  # file STILL parses — atomic invariant


async def test_agent_wrong_symbol_name_is_refined() -> None:
    """VERIFY rejects a node whose function name drifted — the agent must
    return the RIGHT symbol."""
    target = _targets()[0]
    calls = {"n": 0}

    async def rename_then_fix(target, feedback):
        calls["n"] += 1
        if calls["n"] == 1:
            return "def WRONG_NAME(a, b):\n    return a + b"
        return _FIX["alpha"]

    outcome = await run_agentic_repair(target, rename_then_fix, max_turns=3)
    assert outcome.converged is True
    assert outcome.turns == 2  # refined after the wrong-name verify failure
