"""Oracle lazy-traversal — bounded-RAM soak (Slice 3 proof).

Empirically proves the query-time RAM wall is gone: a deep recursive traversal over a large graph
through the SqliteLazyGraphBackend holds a FLAT, bounded resident footprint (the working-set cache
contracts under live pressure) — vs the in-memory backend which must hold the WHOLE graph.

Builds a synthetic graph directly into oracle.db (no AST parsing), then:
  A) loads the full graph in-memory and records its resident cost (the wall we're removing);
  B) runs the SAME deep traversals through the lazy backend under FORCED critical pressure,
     sampling RSS + cache state, and verifies parity on a sample.

Run:  PYTHONPATH=$(pwd) python3 -u scripts/soaks/oracle_lazy_traversal_soak.py [n_nodes]
"""
from __future__ import annotations

import asyncio
import os
import resource
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("JARVIS_ORACLE_TRAVERSAL_PRESSURE_INTERVAL_S", "0")   # probe every frontier
os.environ.setdefault("JARVIS_ORACLE_TRAVERSAL_CACHE_MAX", "2000")

import backend.core.ouroboros.oracle_graph_backend as GB  # noqa: E402
import backend.core.ouroboros.oracle_persistence as P     # noqa: E402
from backend.core.ouroboros.oracle import (                # noqa: E402
    CodebaseKnowledgeGraph, EdgeData, EdgeType, NodeData, NodeID, NodeType,
)


def _rss_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if raw > 10_000_000 else raw / 1024


def _now_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        return _rss_mb()


class _ForceCritical:
    """A gate that always reports CRITICAL — forces maximum cache contraction during the soak."""
    def pressure(self):
        from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
        return PressureLevel.CRITICAL


def build_graph(n: int) -> CodebaseKnowledgeGraph:
    """A wide branching tree (fan-out 6) — deep recursion with large frontiers."""
    g = CodebaseKnowledgeGraph()
    ids = []
    for i in range(n):
        nid = NodeID(repo="jarvis", file_path=f"pkg{i % 200}/m{i}.py", name=f"sym{i}",
                     node_type=NodeType.FUNCTION, line_number=i % 100)
        g.add_node(NodeData(node_id=nid, docstring="d" * 40, signature="(x:int)->int"))
        ids.append(nid)
    for i in range(n):
        for c in (2 * i + 1, 2 * i + 2, 3 * i + 1):   # branching, some shared → real graph shape
            if c < n:
                g.add_edge(ids[i], ids[c], EdgeData(EdgeType.CALLS, line_number=c % 50))
    return g, ids


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60000
    tmp = Path(tempfile.mkdtemp(prefix="lazy_soak_"))
    db = tmp / "oracle.db"

    print(f"# Oracle lazy-traversal bounded-RAM soak  (n={n} nodes)")
    rss0 = _now_rss_mb()
    g, ids = build_graph(n)
    state = P.GraphState(graph=g._graph, node_index=g._node_index, file_index=g._file_index,
                         repo_index=g._repo_index, type_index=g._type_index,
                         metrics=g._metrics, file_hashes={})
    prov = P.AioSqliteProvider(db)
    await prov.save(state)
    await prov.close()
    edges = g._graph.number_of_edges()

    # ---- A) in-memory baseline: the whole graph resident (the wall) ----
    mem = GB.InMemoryGraphBackend(g)
    rss_inmemory = _now_rss_mb()
    roots = [str(ids[i]) for i in range(0, min(n, 2000), 50)]
    for r in roots:
        mem.descendants(r, max_depth=6)
    rss_after_inmemory = _now_rss_mb()

    # free the in-memory graph so its RSS doesn't mask the lazy measurement
    del mem, g, state
    import gc
    gc.collect()
    rss_pre_lazy = _now_rss_mb()

    # ---- B) lazy backend under FORCED critical pressure ----
    sl = GB.SqliteLazyGraphBackend(db, memory_gate=_ForceCritical())
    lazy_peak = {"v": _now_rss_mb()}
    for r in roots:
        sl.descendants(r, max_depth=6)        # deep recursion, per-layer prefetch + contraction
        lazy_peak["v"] = max(lazy_peak["v"], _now_rss_mb())
    rss_after_lazy = _now_rss_mb()

    # ---- parity sample (lazy vs a fresh in-memory of the same db) ----
    fresh = CodebaseKnowledgeGraph()
    st2 = await P.AioSqliteProvider(db).load()
    fresh._graph = st2.graph; fresh._node_index = st2.node_index
    ref = GB.InMemoryGraphBackend(fresh)
    diverged = 0
    for r in roots[:20]:
        if GB._results_equal(sl.successors(r), ref.successors(r)) is False:
            diverged += 1
        if sl.descendants(r, 6) != ref.descendants(r, 6):
            diverged += 1
    sl.close()

    print("=" * 70)
    print(f"  graph                         : {n:,} nodes / {edges:,} edges")
    print(f"  baseline RSS (start)          : {rss0:>8.0f} MB")
    print(f"  A) RSS with FULL graph resident: {rss_after_inmemory:>8.0f} MB   <- the query-time wall")
    print(f"  RSS after freeing graph       : {rss_pre_lazy:>8.0f} MB")
    print(f"  B) peak RSS, LAZY traversal    : {lazy_peak['v']:>8.0f} MB   <- bounded by the cache")
    print(f"  lazy resident delta over base : {lazy_peak['v'] - rss_pre_lazy:>8.0f} MB")
    print(f"  in-memory graph footprint     : {rss_after_inmemory - rss0:>8.0f} MB")
    print(f"  *** lazy footprint vs full     : {100.0 * (lazy_peak['v'] - rss_pre_lazy) / max(rss_after_inmemory - rss0, 1):>7.1f} %")
    print(f"  cache pressure_events         : {sl.pressure_events:>8,}")
    print(f"  succ_cache maxsize (contracted): {sl._succ_cache.maxsize:>8,}  (baseline {GB.traversal_cache_max()})")
    print(f"  sql query_count               : {sl.query_count:>8,}")
    print(f"  parity divergences (sample)   : {diverged:>8,}")
    print("=" * 70)
    ok = (diverged == 0
          and sl._succ_cache.maxsize < GB.traversal_cache_max()
          and (lazy_peak["v"] - rss_pre_lazy) < (rss_after_inmemory - rss0))
    print("VERDICT:", "PASS" if ok else "FAIL")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
