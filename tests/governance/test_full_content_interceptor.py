"""Full-Content Generation Interceptor — the live-seam entry point.

Mandated bulletproof on a 10,000+ line full_content generation:
  1. The entry point triggers the Agentic Swarm bridge (not whole-file).
  2. One sub-agent emits agent_unconverged → its node is ISOLATED (routed to RAG)
     while the successful nodes stitch atomically.
  3. The standard route (small files) executes cleanly — no regression.
  4. StrategyOutcomeLogger receives the terminal event → writes the outcome to
     SQLite.
"""

from __future__ import annotations

import ast
import sqlite3
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.chunked_generation_bridge import (
    StrategyOutcomeLogger,
)
from backend.core.ouroboros.governance.full_content_interceptor import (
    intercept_full_content,
)
from backend.core.ouroboros.governance.intelligent_chunking import best_strategy


def _make_10k_file() -> str:
    parts = ['"""Enterprise module."""', ""]
    parts.append("def alpha(a, b):\n    return a - b\n")
    for i in range(1800):
        parts.append(f"def _pad_{i}():\n    return {i}\n")
    parts.append("def beta(a, b):\n    return a * a\n")
    for i in range(1800, 3600):
        parts.append(f"def _pad_{i}():\n    return {i}\n")
    parts.append("def gamma(xs):\n    return xs[0]\n")
    return "\n".join(parts)


_BIG = _make_10k_file()

_FIX = {
    "alpha": "def alpha(a, b):\n    return a + b",
    "beta": "def beta(a, b):\n    return a * b",
    "gamma": "def gamma(xs):\n    return sorted(xs)[0]",
}


async def test_massive_full_content_triggers_swarm_unconverged_isolated() -> None:
    assert _BIG.count("\n") > 10000, "fixture must be 10k+ lines"

    # beta's agent NEVER converges (always a syntax error) → agent_unconverged.
    async def agent_fn(target: ChunkTarget, feedback: str) -> str:
        if target.symbol == "beta":
            return "def beta(a, b):\n    return a * b ((("   # never parses
        return _FIX[target.symbol]

    # The RAG fallback repairs beta cleanly.
    async def rag_agent_fn(target: ChunkTarget, rag_ctx: str) -> str:
        return _FIX[target.symbol]

    result = await intercept_full_content(
        _BIG, "enterprise.py", ["alpha", "beta", "gamma"], agent_fn,
        rag_agent_fn=rag_agent_fn, op_id="op-10k-1", max_turns=2,
    )

    # (1) The entry point routed to the Agentic Swarm — NOT whole-file.
    assert result.strategy == "agentic_swarm"
    assert result.stitched is True

    # (2) beta's unconverged node was ISOLATED → RAG-recovered; the others
    # converged via the swarm; nothing dropped; the file stitches atomically.
    assert set(result.converged_nodes) == {"alpha", "gamma"}
    assert result.rag_recovered_nodes == ["beta"]
    assert result.dropped_nodes == []

    # (3) The final content parses + every fix landed correctly.
    ast.parse(result.content)
    ns = {}
    exec(compile(result.content, "big", "exec"), ns)  # noqa: S102 — test only
    assert ns["alpha"](5, 3) == 8
    assert ns["beta"](5, 3) == 15   # RAG-recovered node
    assert ns["gamma"]([3, 1, 2]) == 1
    assert ns["_pad_0"]() == 0 and ns["_pad_3199"]() == 3199  # padding preserved


async def test_unconverged_with_no_rag_drops_only_that_node() -> None:
    async def agent_fn(target, feedback):
        if target.symbol == "beta":
            return "def beta(a, b):\n    return ((("
        return _FIX[target.symbol]

    result = await intercept_full_content(
        _BIG, "enterprise.py", ["alpha", "beta", "gamma"], agent_fn,
        rag_agent_fn=None, op_id="op-10k-2", max_turns=2,
    )
    assert set(result.converged_nodes) == {"alpha", "gamma"}
    assert result.dropped_nodes == ["beta"]         # isolated, not fatal
    ast.parse(result.content)                        # file STILL parses


async def test_small_file_no_regression() -> None:
    small = "def f(a, b):\n    return a - b\n"
    called = {"whole": False}

    async def whole_file_fn():
        called["whole"] = True
        return "def f(a, b):\n    return a + b\n"

    async def agent_fn(target, feedback):
        raise AssertionError("swarm must NOT run for a small file")

    result = await intercept_full_content(
        small, "s.py", ["f"], agent_fn, whole_file_fn=whole_file_fn, op_id="op-small",
    )
    # (3) Standard route: whole-file generator ran, no swarm.
    assert result.strategy == "whole"
    assert called["whole"] is True
    assert result.content == "def f(a, b):\n    return a + b\n"


async def test_strategy_outcome_logger_writes_sqlite_on_terminal() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    outcome_logger = StrategyOutcomeLogger(conn)

    # The interceptor records the pending strategy at frame time.
    async def agent_fn(target, feedback):
        return _FIX[target.symbol]

    result = await intercept_full_content(
        _BIG, "enterprise.py", ["alpha", "beta", "gamma"], agent_fn,
        op_id="op-10k-3", max_turns=2,
    )
    assert result.strategy == "agentic_swarm"

    # (4) The op resolves PROMOTED → the boot-attached observer writes SQLite.
    await outcome_logger.on_terminal(SimpleNamespace(
        topic="op.terminal.promoted",
        payload={"op_id": "op-10k-3", "state": "promoted"},
    ))
    n = conn.execute("SELECT COUNT(*) FROM chunk_strategy_outcomes").fetchone()[0]
    assert n == 1
    assert best_strategy(conn, file_lines=_BIG.count("\n") + 1, ext=".py") == "agentic_swarm"
    conn.close()
