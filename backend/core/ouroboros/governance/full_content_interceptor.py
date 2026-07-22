"""Full-Content Generation Interceptor — the egress entry point for big files.

Intercepts the ``full_content`` generation request at the egress seam
(providers.py / candidate_generator) — NOT a provider-level hack:

  * SMALL file → passes through to the legacy whole-file generator, byte-for-byte
    no regression.
  * MASSIVE file → whole-file ingestion is FORBIDDEN. The request is routed
    through the AGENTIC SWARM (``swarm_agentic_repair`` — goal-bounded ReAct
    workers per AST node, #70024), stitched atomically under a PER-FILE async
    lock (one writer → no concurrent-mutation collision on the same class /
    module during Fan-In).
  * LOCALIZED NODE FALLBACK — a sub-agent that emits ``agent_unconverged`` after
    breaching ``max_turns`` does NOT drop the whole op: its node is isolated and
    routed to the RAG fallback (#70021), while every converged node still lands.
    The fallback RE-EXTRACTS the node's line range from the already-stitched
    source (converged grafts shift line numbers), so the graft is always exact.
  * Reinforcement — the chosen strategy is recorded (#70022) so the boot-time
    ``StrategyOutcomeLogger`` updates the SQLite weights on op terminal.

Composes #70020-#70024 + the Fleet Commander chassis (ToolLoopCoordinator /
ScopedToolBackend / WorktreeManager flow into ``agent_fn``). No new dispatcher /
parser / listener. Env-driven; never raises on the hot path.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.agentic_super_agent import (
    swarm_agentic_repair,
)
from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.chunked_generation import (
    extract_target_chunk,
    stitch_replacement,
)
from backend.core.ouroboros.governance.chunked_generation_bridge import (
    record_pending_strategy,
)
from backend.core.ouroboros.governance.intelligent_chunking import (
    exceeds_ceiling,
    keyword_rag_chunks,
)

logger = logging.getLogger("Ouroboros.FullContentInterceptor")

# Per-file atomic-graft locks — the Fan-In writer lock (one per file path) that
# guarantees concurrent sub-agents can never collide on the same module/class.
_file_locks: Dict[str, asyncio.Lock] = {}
_registry_lock = asyncio.Lock()


async def _lock_for(file_path: str) -> asyncio.Lock:
    async with _registry_lock:
        lk = _file_locks.get(file_path)
        if lk is None:
            lk = asyncio.Lock()
            _file_locks[file_path] = lk
        return lk


def _enabled() -> bool:
    return os.environ.get(
        "JARVIS_FULL_CONTENT_INTERCEPT_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


# whole_file_fn() -> Awaitable[str]  — the legacy whole-file generator.
WholeFileFn = Callable[[], Awaitable[str]]
# rag_agent_fn(target, rag_context) -> Awaitable[str] — the RAG-fallback repair.
RagAgentFn = Callable[[ChunkTarget, str], Awaitable[str]]


@dataclass
class InterceptResult:
    strategy: str                                  # "whole" | "agentic_swarm" | "rag"
    content: str                                   # the final file content
    stitched: bool = False
    converged_nodes: List[str] = field(default_factory=list)
    rag_recovered_nodes: List[str] = field(default_factory=list)
    dropped_nodes: List[str] = field(default_factory=list)


async def intercept_full_content(
    source: str,
    file_path: str,
    symbols: List[str],
    agent_fn,
    *,
    whole_file_fn: Optional[WholeFileFn] = None,
    rag_agent_fn: Optional[RagAgentFn] = None,
    op_id: str = "",
    max_turns: Optional[int] = None,
    max_concurrency: Optional[int] = None,
) -> InterceptResult:
    """The egress interception point. Never raises."""
    ext = os.path.splitext(file_path or "")[1] or ""
    file_lines = (source.count("\n") + 1) if source else 0

    # ── SMALL file (or intercept disabled): legacy whole-file, no regression ──
    if not _enabled() or not exceeds_ceiling(source):
        content = (await whole_file_fn()) if whole_file_fn else source
        return InterceptResult(strategy="whole", content=content, stitched=False)

    # ── MASSIVE file: whole-file FORBIDDEN — build AST targets ──
    targets: List[ChunkTarget] = []
    for sym in symbols or []:
        try:
            chunk = extract_target_chunk(source, file_path, sym)
        except Exception:  # noqa: BLE001
            chunk = None
        if chunk is not None:
            targets.append(ChunkTarget(symbol=sym, chunk=chunk, instruction=f"repair {sym}"))

    if not targets:
        # No AST-resolvable symbol → whole-file is STILL forbidden → RAG only.
        record_pending_strategy(op_id, strategy="rag", file_lines=file_lines, ext=ext)
        logger.info(
            "[FullContentInterceptor] %s (%d lines): no resolvable symbol — "
            "RAG-only (whole-file blocked)", file_path, file_lines,
        )
        return InterceptResult(
            strategy="rag", content=source, stitched=False,
            dropped_nodes=list(symbols or []),
        )

    record_pending_strategy(
        op_id, strategy="agentic_swarm", file_lines=file_lines, ext=ext,
    )
    lock = await _lock_for(file_path)

    # SCATTER/GATHER + atomic stitch — under the per-file writer lock.
    async with lock:
        swarm = await swarm_agentic_repair(
            source, file_path, targets, agent_fn,
            max_concurrency=max_concurrency, max_turns=max_turns,
        )
    stitched = swarm.stitched
    converged = list(swarm.succeeded)
    unconverged = list(swarm.failed)
    rag_recovered: List[str] = []
    dropped: List[str] = []

    # ── LOCALIZED NODE FALLBACK: unconverged nodes → RAG, isolated ──
    for sym in unconverged:
        if rag_agent_fn is None:
            dropped.append(sym)
            continue
        # RE-EXTRACT the node from the CURRENT stitched source — converged grafts
        # shifted line numbers, so the original chunk's range is stale.
        fresh = extract_target_chunk(stitched, file_path, sym)
        if fresh is None:
            dropped.append(sym)
            continue
        rag_ctx = "\n".join(keyword_rag_chunks(source, sym))
        try:
            node = await rag_agent_fn(
                ChunkTarget(symbol=sym, chunk=fresh, instruction=f"repair {sym}"),
                rag_ctx,
            )
        except Exception:  # noqa: BLE001 — an isolated RAG failure never drops the op
            node = ""
        recovered = False
        async with lock:
            candidate = stitch_replacement(stitched, fresh, node) if node else None
            if candidate is not None:
                try:
                    ast.parse(candidate)
                    stitched = candidate
                    recovered = True
                except SyntaxError:
                    recovered = False
        (rag_recovered if recovered else dropped).append(sym)

    logger.info(
        "[FullContentInterceptor] %s (%d lines): agentic swarm — converged=%d "
        "rag_recovered=%d dropped=%d (atomic, isolated)",
        file_path, file_lines, len(converged), len(rag_recovered), len(dropped),
    )
    return InterceptResult(
        strategy="agentic_swarm", content=stitched, stitched=True,
        converged_nodes=converged, rag_recovered_nodes=rag_recovered,
        dropped_nodes=dropped,
    )


__all__ = [
    "InterceptResult",
    "RagAgentFn",
    "WholeFileFn",
    "intercept_full_content",
]
