"""Ephemeral Super-Agent Swarm — parallel scatter-gather chunk repair.

A massive file needing MULTIPLE independent AST-node repairs must not be
processed sequentially — synchronous per-chunk network I/O is the wall-clock
killer. This weaponizes DoubleWord's cost-efficiency: fan out one ephemeral
"Super Agent" per target — a lightweight ``asyncio`` task that handles ONLY its
own DW round — so the network legs run CONCURRENTLY.

Bounded for a 16GB M1: the agents are pure ``asyncio`` tasks gated by a
semaphore (no threads, no multiprocessing, no RAM spike). The fan-in (gather)
awaits every network response, then stitches them SEQUENTIALLY and ATOMICALLY —
in DESCENDING start-line order, so an early graft can never shift the line
numbers a later chunk still points at, and one writer means no file-lock race /
AST corruption.

Scatter (concurrent network) → Gather (await all) → Fan-in (serial atomic
stitch). Composes PR #70020 (stitch) + #70022 (L2 recovery). Env-driven; never
raises on the hot path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

from backend.core.ouroboros.governance.chunked_generation import (
    stitch_replacement,
)

logger = logging.getLogger("Ouroboros.ChunkSwarm")

_CONCURRENCY_ENV = "JARVIS_CHUNK_SWARM_MAX_CONCURRENCY"
_DEFAULT_CONCURRENCY = 4  # bounded for a 16GB M1 — concurrent DW network legs


def swarm_concurrency() -> int:
    """Max concurrent Super Agents (env ``JARVIS_CHUNK_SWARM_MAX_CONCURRENCY``,
    default 4). Clamped [1, 16] so the M1 never spikes on a huge fan-out."""
    try:
        return max(1, min(16, int(os.environ.get(_CONCURRENCY_ENV, str(_DEFAULT_CONCURRENCY)))))
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY


@dataclass
class ChunkTarget:
    """One independent repair site: a symbol + its AST chunk + the fix ask.

    ``chunk`` is a #70020 CodeChunk (carries ``start_line`` / ``end_line`` /
    ``source_code``)."""
    symbol: str
    chunk: object
    instruction: str = ""
    prompt: str = ""


@dataclass
class SwarmResult:
    stitched: str
    succeeded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    agents_spawned: int = 0
    max_in_flight: int = 0


# generate_fn(target: ChunkTarget) -> Awaitable[str]  — the DW round; returns
# the model's replacement node source for that target.
AgentGenerateFn = Callable[[ChunkTarget], Awaitable[str]]


async def swarm_repair(
    full_source: str,
    file_path: str,
    targets: List[ChunkTarget],
    generate_fn: AgentGenerateFn,
    *,
    max_concurrency: Optional[int] = None,
) -> SwarmResult:
    """Repair MANY chunks of *full_source* in parallel, then stitch atomically.

    SCATTER: spawn one ephemeral Super Agent (asyncio task) per target, capped
    by a semaphore, each awaiting ONLY its own DW round — the network legs run
    concurrently. GATHER: await all. FAN-IN: stitch the returned nodes back
    SEQUENTIALLY, in DESCENDING start-line order (so earlier stitches don't
    shift later chunks' line ranges) and validating each graft parses — one
    writer, no race. Never raises."""
    if not targets:
        return SwarmResult(stitched=full_source)

    limit = max_concurrency if max_concurrency is not None else swarm_concurrency()
    sem = asyncio.Semaphore(limit)
    in_flight = {"cur": 0, "max": 0}

    async def _super_agent(target: ChunkTarget):
        # Bounded multiplexing: never more than ``limit`` DW legs at once.
        async with sem:
            in_flight["cur"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["cur"])
            try:
                node = await generate_fn(target)
                return (target, node, None)
            except Exception as exc:  # noqa: BLE001 — an agent failure is isolated
                return (target, None, exc)
            finally:
                in_flight["cur"] -= 1

    # asyncio.gather = structured concurrent fan-out (Python 3.9-safe; the
    # TaskGroup equivalent without the 3.11 floor the codebase forbids). Each
    # coroutine is a lightweight task — no threads, no processes.
    gathered = await asyncio.gather(
        *[_super_agent(t) for t in targets], return_exceptions=False,
    )

    # ── FAN-IN: sequential, atomic stitch in DESCENDING line order ──
    ok_results = [
        (t, node) for (t, node, err) in gathered if err is None and node
    ]
    ok_results.sort(
        key=lambda tn: int(getattr(tn[0].chunk, "start_line", 0)), reverse=True,
    )
    stitched = full_source
    succeeded: List[str] = []
    failed: List[str] = [
        t.symbol for (t, node, err) in gathered if err is not None or not node
    ]
    import ast as _ast
    for target, node in ok_results:
        candidate = stitch_replacement(stitched, target.chunk, node)
        if candidate is None:
            failed.append(target.symbol)
            continue
        try:
            _ast.parse(candidate)
        except SyntaxError:
            # A graft that breaks the file is rejected — the atomic invariant
            # (the file always parses) is never violated by a bad agent.
            failed.append(target.symbol)
            continue
        stitched = candidate
        succeeded.append(target.symbol)

    logger.info(
        "[ChunkSwarm] fan-out=%d max_in_flight=%d → stitched %d, failed %d "
        "(atomic, descending-line, no race)",
        len(targets), in_flight["max"], len(succeeded), len(failed),
    )
    return SwarmResult(
        stitched=stitched, succeeded=succeeded, failed=failed,
        agents_spawned=len(targets), max_in_flight=in_flight["max"],
    )


__all__ = [
    "AgentGenerateFn",
    "ChunkTarget",
    "SwarmResult",
    "swarm_concurrency",
    "swarm_repair",
]
