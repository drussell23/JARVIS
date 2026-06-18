"""Topological file pruner (Phase 3.3) -- keep a local 3B payload under its
context ceiling by discarding LOW-CENTRALITY files whole (never mid-string).

Pure + deterministic: the caller supplies precomputed per-file token counts and
(optionally) a graph backend. REUSES the existing SqliteLazyGraphBackend degree
signals (nodes_in_file + successor/predecessor_keys); no new graph is built.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def local_max_context_tokens() -> int:
    return int(os.environ.get("JARVIS_LOCAL_MAX_CONTEXT_TOKENS", "8000"))


@dataclass
class PruneResult:
    kept_files: List[str] = field(default_factory=list)
    discarded_files: List[str] = field(default_factory=list)
    tokens_before: int = 0
    tokens_after: int = 0
    pruned: bool = False


def _file_centrality(graph_backend: Any, file_path: str) -> int:
    """Aggregate degree-centrality of a file = sum of node in+out degrees.
    Returns 0 when the backend is absent or the file has no graph nodes."""
    if graph_backend is None:
        return 0
    try:
        total = 0
        for node in graph_backend.nodes_in_file(file_path):
            total += len(graph_backend.successor_keys(node))
            total += len(graph_backend.predecessor_keys(node))
        return total
    except Exception:
        return 0


def prune_files_by_centrality(
    target_files: List[str],
    *,
    file_tokens: Dict[str, int],
    graph_backend: Optional[Any] = None,
    ceiling_tokens: Optional[int] = None,
) -> PruneResult:
    """Keep the most central files whose cumulative token cost fits under the
    ceiling; discard the rest WHOLE. Always retains at least one file (the most
    central) so the local engine still has something to work on.

    Ranking key: (centrality DESC, token_cost ASC, declared-order ASC). With no
    backend, centrality is 0 for all -> falls back to (smaller-first, then order)
    which greedily keeps more files under the ceiling.
    """
    ceiling = ceiling_tokens if ceiling_tokens is not None else local_max_context_tokens()
    files = list(target_files)
    tokens_before = sum(int(file_tokens.get(f, 0)) for f in files)
    if tokens_before <= ceiling or len(files) <= 1:
        return PruneResult(kept_files=files, discarded_files=[],
                           tokens_before=tokens_before, tokens_after=tokens_before,
                           pruned=False)

    ranked = sorted(
        enumerate(files),
        key=lambda iv: (
            -_file_centrality(graph_backend, iv[1]),  # most central first
            int(file_tokens.get(iv[1], 0)),           # then smaller first
            iv[0],                                     # then declared order
        ),
    )
    kept: List[str] = []
    discarded: List[str] = []
    running = 0
    for _idx, f in ranked:
        cost = int(file_tokens.get(f, 0))
        if not kept:
            # always keep the single most-critical file, even if it alone is over
            kept.append(f)
            running += cost
            continue
        if running + cost <= ceiling:
            kept.append(f)
            running += cost
        else:
            discarded.append(f)
    # preserve declared order in the kept list for prompt stability
    kept_in_order = [f for f in files if f in set(kept)]
    return PruneResult(kept_files=kept_in_order, discarded_files=discarded,
                       tokens_before=tokens_before, tokens_after=running,
                       pruned=bool(discarded))
