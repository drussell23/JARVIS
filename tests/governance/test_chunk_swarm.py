"""Ephemeral Super-Agent Swarm — parallel scatter-gather chunk repair.

Mandated bulletproof: a massive file requiring 3 distinct AST-node repairs must
spawn 3 CONCURRENT Super Agents hitting the (mocked) DW provider in parallel,
gather all 3 payloads, and stitch them SEQUENTIALLY without a file-lock
conflict — the stitched file parsing with all 3 fixes applied.
"""

from __future__ import annotations

import ast
import asyncio

import pytest

from backend.core.ouroboros.governance.chunk_swarm import (
    ChunkTarget,
    swarm_repair,
)
from backend.core.ouroboros.governance.chunked_generation import (
    extract_target_chunk,
)

# A file with 3 independent repair sites at DIFFERENT line ranges.
_MASSIVE_MULTI = '''"""Massive module with three buggy functions."""


def alpha(a, b):
    return a - b


def _pad_1():
    return 1


def beta(a, b):
    return a * a


def _pad_2():
    return 2


def gamma(xs):
    return xs[0]
'''


async def test_three_concurrent_super_agents_gather_and_stitch() -> None:
    targets = []
    for sym in ("alpha", "beta", "gamma"):
        chunk = extract_target_chunk(_MASSIVE_MULTI, "m.py", sym)
        assert chunk is not None, sym
        targets.append(ChunkTarget(symbol=sym, chunk=chunk, instruction=f"fix {sym}"))

    # Each fix, keyed by symbol — the model's returned node.
    fixes = {
        "alpha": "def alpha(a, b):\n    return a + b",          # - -> +
        "beta": "def beta(a, b):\n    return a * b",            # a*a -> a*b
        "gamma": "def gamma(xs):\n    return sorted(xs)[0]",    # xs[0] -> min
    }

    # Concurrency barrier: every agent increments; all proceed only once all 3
    # are simultaneously in-flight. If the swarm ran SEQUENTIALLY, the barrier
    # would never reach 3 and each agent would time out — so a clean pass PROVES
    # true parallelism.
    all_in_flight = asyncio.Event()
    count = {"n": 0}

    async def generate_fn(target: ChunkTarget) -> str:
        count["n"] += 1
        if count["n"] == 3:
            all_in_flight.set()
        # Wait until all 3 Super Agents are concurrently hitting DW.
        await asyncio.wait_for(all_in_flight.wait(), timeout=5.0)
        await asyncio.sleep(0)  # yield — real network I/O interleave point
        return fixes[target.symbol]

    result = await swarm_repair(
        _MASSIVE_MULTI, "m.py", targets, generate_fn, max_concurrency=4,
    )

    # (1) 3 Super Agents spawned and hit DW CONCURRENTLY (proven by the barrier).
    assert result.agents_spawned == 3
    assert result.max_in_flight == 3, "agents must run in parallel, not serially"

    # (2) All 3 payloads gathered + stitched, no conflict.
    assert set(result.succeeded) == {"alpha", "beta", "gamma"}
    assert result.failed == []

    # (3) The stitched file parses and carries ALL 3 fixes — the descending-line
    # fan-in kept every chunk's line range valid.
    tree = ast.parse(result.stitched)
    assert tree is not None
    ns = {}
    exec(compile(result.stitched, "stitched", "exec"), ns)  # noqa: S102 — test
    assert ns["alpha"](5, 3) == 8       # + not -
    assert ns["beta"](5, 3) == 15       # a*b not a*a
    assert ns["gamma"]([3, 1, 2]) == 1  # min via sorted
    # The unrelated padding is preserved.
    assert ns["_pad_1"]() == 1 and ns["_pad_2"]() == 2


async def test_bounded_concurrency_caps_in_flight() -> None:
    """The M1 memory bound: with a concurrency cap of 1, agents never overlap."""
    targets = [
        ChunkTarget(symbol=s, chunk=extract_target_chunk(_MASSIVE_MULTI, "m.py", s))
        for s in ("alpha", "beta", "gamma")
    ]
    peak = {"cur": 0, "max": 0}

    async def generate_fn(target: ChunkTarget) -> str:
        peak["cur"] += 1
        peak["max"] = max(peak["max"], peak["cur"])
        await asyncio.sleep(0.01)
        peak["cur"] -= 1
        return f"def {target.symbol}(*a, **k):\n    return None"

    result = await swarm_repair(
        _MASSIVE_MULTI, "m.py", targets, generate_fn, max_concurrency=1,
    )
    assert peak["max"] == 1              # semaphore held the M1 bound
    assert result.max_in_flight == 1
    assert len(result.succeeded) == 3   # still all repaired, just serialized


async def test_one_bad_agent_isolated_others_still_stitch() -> None:
    """A failing/corrupt agent is isolated — the atomic invariant holds (file
    always parses) and the other repairs still land."""
    targets = [
        ChunkTarget(symbol=s, chunk=extract_target_chunk(_MASSIVE_MULTI, "m.py", s))
        for s in ("alpha", "beta", "gamma")
    ]

    async def generate_fn(target: ChunkTarget) -> str:
        if target.symbol == "beta":
            return "def beta(a, b):\n    return a * b (((("  # corrupt → won't parse
        return f"def {target.symbol}(a, b):\n    return a + b"

    result = await swarm_repair(_MASSIVE_MULTI, "m.py", targets, generate_fn)
    assert "beta" in result.failed          # the corrupt graft is rejected
    assert "alpha" in result.succeeded
    assert "gamma" in result.succeeded
    ast.parse(result.stitched)              # file STILL parses (atomic invariant)


async def test_empty_targets_is_noop() -> None:
    result = await swarm_repair(_MASSIVE_MULTI, "m.py", [], lambda t: None)
    assert result.stitched == _MASSIVE_MULTI
    assert result.agents_spawned == 0
