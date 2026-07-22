"""Generation-Prompt Bridge — the intelligent chunk-routing interception layer.

Mandated bulletproof (mocking the GENERATE context builder):
  1. A massive file triggers the strategy selector (chunked, not whole-file).
  2. The prompt is injected with the Dynamic Context Framing instructions.
  3. A simulated bad-stitch response triggers the L2 self-correction retry loop.
  4. The terminal outcome updates the SQLite ML reinforcement weights off-loop.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.chunked_generation import (
    extract_target_chunk,
)
from backend.core.ouroboros.governance.chunked_generation_bridge import (
    MAP_REDUCE_FRAMING,
    StrategyOutcomeLogger,
    frame_for_generation,
    record_pending_strategy,
    stitch_with_l2_recovery,
)
from backend.core.ouroboros.governance.intelligent_chunking import (
    ChunkPlan,
    best_strategy,
)


def _massive(n: int = 600) -> str:
    parts = ['"""M."""', "import collections", ""]
    for i in range(n):
        parts.append(f"class Sibling_{i}:")
        parts.append(f"    def m_{i}(self):\n        return {i}")
    parts.append("class SagaApplyStrategy:")
    parts.append("    def _topological_sort(self, repo_scope, edges):")
    parts.append("        return sorted(repo_scope)")
    return "\n".join(parts)


_MASSIVE = _massive()

_SMALL_FILE = (
    "import collections\n\n\n"
    "class S:\n"
    "    def _topological_sort(self, repo_scope, edges):\n"
    "        return list(collections.OrderedDict.fromkeys(sorted(repo_scope)))\n"
)


# 1 + 2 ── strategy selection + Dynamic Context Framing injection ────────────


def test_massive_file_triggers_strategy_and_injects_framing() -> None:
    framed = frame_for_generation(
        _MASSIVE, "saga_apply_strategy.py", "_topological_sort",
        instruction="use heapq for lexicographic order",
    )
    # (1) Strategy selector fired — chunked, NOT whole-file.
    assert framed.chunked is True
    assert framed.plan.strategy in ("ast", "rag")
    assert framed.plan.forbade_whole_file is True

    # (2) The Dynamic Context Framing / System Instruction Modifier is injected.
    assert framed.instruction_injected is True
    low = framed.prompt.lower()
    assert "constrained map-reduce" in low
    assert "radius of relevance" in low
    assert "do not hallucinate" in low
    assert "only the modified ast node" in low
    # The instruction rode along, and the pruned context is tiny vs the file.
    assert "use heapq" in framed.prompt
    assert len(framed.prompt) < len(_MASSIVE)
    assert "class Sibling_0" not in framed.prompt  # siblings pruned


def test_small_file_is_not_framed() -> None:
    framed = frame_for_generation(_SMALL_FILE, "s.py", "_topological_sort")
    assert framed.chunked is False
    assert framed.instruction_injected is False
    assert MAP_REDUCE_FRAMING not in framed.prompt


# 3 ── L2 Stitch Recovery: bad stitch → retry with error fed back ────────────


async def test_bad_stitch_triggers_l2_retry_then_converges() -> None:
    chunk = extract_target_chunk(_SMALL_FILE, "s.py", "_topological_sort")
    assert chunk is not None
    plan = ChunkPlan(strategy="ast", context="", chunk=chunk)

    # Round 1: a SYNTACTICALLY BROKEN node (missing close paren) → won't parse.
    # Round 2 (after the L2 error is fed back): a clean node.
    bad = "def _topological_sort(self, repo_scope, edges):\n    return sorted(repo_scope"
    good = "def _topological_sort(self, repo_scope, edges):\n    import heapq\n    return sorted(repo_scope)"
    calls = {"n": 0, "prompts": []}

    async def generate_fn(prompt):
        calls["n"] += 1
        calls["prompts"].append(prompt)
        return bad if calls["n"] == 1 else good

    result = await stitch_with_l2_recovery(
        plan, _SMALL_FILE, generate_fn, base_prompt="fix it", max_iters=3,
    )
    # The L2 loop retried and converged.
    assert result.ok is True
    assert result.attempts == 2
    # The SPECIFIC parse error was fed back to the model on the retry.
    assert "L2 CORRECTION" in calls["prompts"][1]
    assert "SyntaxError" in calls["prompts"][1]
    # The stitched file parses and carries the fix.
    import ast as _ast
    _ast.parse(result.stitched)
    assert "heapq" in result.stitched


async def test_l2_gives_up_after_max_iters() -> None:
    chunk = extract_target_chunk(_SMALL_FILE, "s.py", "_topological_sort")
    plan = ChunkPlan(strategy="ast", context="", chunk=chunk)

    async def always_bad(prompt):
        return "def _topological_sort(self, repo_scope, edges):\n    return ("  # never parses

    result = await stitch_with_l2_recovery(
        plan, _SMALL_FILE, always_bad, max_iters=2,
    )
    assert result.ok is False
    assert result.attempts == 3  # initial + 2 retries
    assert result.last_error != ""


# 4 ── Async ML logging: terminal → SQLite weights update ────────────────────


async def test_terminal_outcome_updates_sqlite_weights_offloop() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    outcome_logger = StrategyOutcomeLogger(conn)

    # Frame time recorded which strategy the op used.
    record_pending_strategy("op-massive-1", strategy="ast", file_lines=100000, ext=".py")

    # The op resolves PROMOTED → the bus terminal observer fires.
    event = SimpleNamespace(
        topic="op.terminal.promoted",
        payload={"op_id": "op-massive-1", "state": "promoted"},
    )
    await outcome_logger.on_terminal(event)

    # The outcome landed in the SQLite reinforcement layer, off the main thread.
    n = conn.execute("SELECT COUNT(*) FROM chunk_strategy_outcomes").fetchone()[0]
    assert n == 1
    assert best_strategy(conn, file_lines=100000, ext=".py") == "ast"

    # A terminal for an op that never chunked is ignored (no phantom rows).
    await outcome_logger.on_terminal(SimpleNamespace(
        topic="op.terminal.failed",
        payload={"op_id": "op-not-chunked", "state": "failed"},
    ))
    assert conn.execute("SELECT COUNT(*) FROM chunk_strategy_outcomes").fetchone()[0] == 1
    conn.close()
