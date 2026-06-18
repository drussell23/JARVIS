"""Slice 2 — SqliteLazyGraphBackend: predictive lazy traversal over the canonical oracle.db.

Validates against a REAL aiosqlite-built db + the in-memory baseline (no mocks): exact parity on
every primitive incl. stub-node semantics, the predictive prefetch's N+1 reduction, and the live
DualBackendParityHarness reporting zero divergence + latency deltas.
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.oracle_graph_backend as GB
import backend.core.ouroboros.oracle_persistence as P
from backend.core.ouroboros.oracle import (
    CodebaseKnowledgeGraph,
    EdgeData,
    EdgeType,
    NodeData,
    NodeID,
    NodeType,
)


def _graph_with_stub():
    """Real graph: a chain m0->m1->m2->m3 PLUS an edge m3 -> EXTERNAL (a stub: edge endpoint with no
    NodeData row), exercising the stub-parity path."""
    g = CodebaseKnowledgeGraph()
    ids = []
    for i in range(4):
        nid = NodeID(repo="jarvis", file_path=f"pkg/m{i}.py", name=f"sym{i}",
                     node_type=NodeType.FUNCTION, line_number=1)
        g.add_node(NodeData(node_id=nid, docstring=f"d{i}", decorators=["@x"]))
        ids.append(nid)
    for i in range(3):
        g.add_edge(ids[i], ids[i + 1], EdgeData(EdgeType.CALLS, line_number=i, context=f"c{i}"))
    stub = NodeID(repo="jarvis", file_path="ext/lib.py", name="external", node_type=NodeType.FUNCTION)
    g.add_edge(ids[3], stub, EdgeData(EdgeType.IMPORTS, line_number=9))  # stub: no add_node
    return g, ids, stub


def _build_db(tmp_path, g) -> "P.AioSqliteProvider":
    db = tmp_path / "oracle.db"
    prov = P.AioSqliteProvider(db)
    state = P.GraphState(
        graph=g._graph, node_index=g._node_index, file_index=g._file_index,
        repo_index=g._repo_index, type_index=g._type_index, metrics=g._metrics, file_hashes={},
    )
    asyncio.run(prov.save(state))
    asyncio.run(prov.close())
    return db


# --------------------------------------------------------------------------- primitive parity
def test_sqlite_backend_primitive_parity(tmp_path):
    g, ids, stub = _graph_with_stub()
    db = _build_db(tmp_path, g)
    mem = GB.InMemoryGraphBackend(g)
    sl = GB.SqliteLazyGraphBackend(db)
    try:
        keys = [str(i) for i in ids] + [str(stub)]
        for k in keys:
            assert sl.contains(k) == mem.contains(k), f"contains {k}"
            assert GB._results_equal(sl.successors(k), mem.successors(k)), f"successors {k}"
            assert GB._results_equal(sl.predecessors(k), mem.predecessors(k)), f"predecessors {k}"
            assert sl.get_node(k) == mem.get_node(k), f"get_node {k}"
        assert sl.contains("totally-absent") == mem.contains("totally-absent") == False
        assert sl.get_node("totally-absent") is None
        # counts match the in-memory graph (incl. the stub node)
        assert sl.node_count() == mem.node_count() == 5    # 4 real + 1 stub
        assert sl.edge_count() == mem.edge_count() == 4
        assert set(sl.all_keys()) == set(mem.all_keys())
    finally:
        sl.close()


def test_sqlite_stub_node_semantics(tmp_path):
    """A genuine bare stub = an edge endpoint with NO node row (arises on warm-load when an edge
    references an un-committed key; networkx auto-vivifies it as an empty-attr node). The SQLite
    backend must replicate that: contains=True, get_node={} — matching what the in-memory warm-load
    produces, so the parity harness sees no divergence."""
    import sqlite3
    g, ids, _ = _graph_with_stub()
    db = _build_db(tmp_path, g)
    # inject an orphan edge: real src -> a dst key that has NO row in the nodes table
    orphan = "jarvis:nowhere/ghost.py:ghost"
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO edges (src_key,dst_key,edge_type,line_number,context) VALUES (?,?,?,?,?)",
                (str(ids[0]), orphan, "calls", 0, ""))
    con.commit(); con.close()

    # the in-memory equivalent of a warm-load: a DiGraph with that same orphan edge → bare stub
    import networkx as nx
    mg = nx.DiGraph(); mg.add_edge(str(ids[0]), orphan)
    mem_ckg = CodebaseKnowledgeGraph(); mem_ckg._graph = mg
    mem = GB.InMemoryGraphBackend(mem_ckg)

    sl = GB.SqliteLazyGraphBackend(db)
    try:
        assert mem.contains(orphan) is True and mem.get_node(orphan) == {}
        assert sl.contains(orphan) is True and sl.get_node(orphan) == {}   # SQLite replicates it
    finally:
        sl.close()


def test_sqlite_algorithms_match_inmemory(tmp_path):
    g, ids, _ = _graph_with_stub()
    g.add_edge(ids[2], ids[0], EdgeData(EdgeType.CALLS))  # cycle 0-1-2
    db = _build_db(tmp_path, g)
    mem = GB.InMemoryGraphBackend(g)   # nx overrides
    sl = GB.SqliteLazyGraphBackend(db)  # default primitive algorithms
    try:
        # shortest path: same LENGTH (multiple optima allowed)
        mp = mem.shortest_path(str(ids[0]), str(ids[3]))
        sp = sl.shortest_path(str(ids[0]), str(ids[3]))
        assert mp is not None and sp is not None and len(mp) == len(sp)
        # cycles: same SET (rotation/order-insensitive)
        assert {frozenset(c) for c in mem.simple_cycles()} == {frozenset(c) for c in sl.simple_cycles()}
    finally:
        sl.close()


# --------------------------------------------------------------------------- predictive prefetch (N+1 armor)
def test_prefetch_batches_the_frontier(tmp_path):
    """Prefetching a frontier must collapse N per-node lookups into ~1 batched IN query."""
    g = CodebaseKnowledgeGraph()
    roots = []
    for i in range(30):
        r = NodeID(repo="jarvis", file_path=f"p/r{i}.py", name=f"r{i}", node_type=NodeType.FUNCTION)
        g.add_node(NodeData(node_id=r))
        c = NodeID(repo="jarvis", file_path=f"p/c{i}.py", name=f"c{i}", node_type=NodeType.FUNCTION)
        g.add_node(NodeData(node_id=c))
        g.add_edge(r, c, EdgeData(EdgeType.CALLS))
        roots.append(str(r))
    db = _build_db(tmp_path, g)

    # WITHOUT prefetch: one query per node
    sl1 = GB.SqliteLazyGraphBackend(db)
    try:
        for k in roots:
            sl1.successors(k)
        naive = sl1.query_count
    finally:
        sl1.close()

    # WITH prefetch: one batched IN query for the whole frontier, then cache hits
    sl2 = GB.SqliteLazyGraphBackend(db)
    try:
        sl2.prefetch_successors(roots)
        before = sl2.query_count
        for k in roots:
            sl2.successors(k)            # all served from cache
        assert sl2.query_count == before    # zero extra round-trips after prefetch
        assert before <= 2                  # the 30-key frontier fetched in 1 IN sweep
        assert before < naive               # strictly fewer than the atom-by-atom path
    finally:
        sl2.close()


# --------------------------------------------------------------------------- live parity harness
def test_harness_zero_divergence_and_latency(tmp_path):
    g, ids, stub = _graph_with_stub()
    g.add_edge(ids[2], ids[0], EdgeData(EdgeType.CALLS))
    db = _build_db(tmp_path, g)
    h = GB.DualBackendParityHarness(
        primary=GB.InMemoryGraphBackend(g), shadow=GB.SqliteLazyGraphBackend(db))
    try:
        keys = [str(i) for i in ids] + [str(stub)]
        for k in keys:
            h.contains(k); h.get_node(k); h.successors(k); h.predecessors(k)
        h.node_count(); h.edge_count()
        h.shortest_path(str(ids[0]), str(ids[3]))
        h.simple_cycles()
        st = h.stats()
        assert st["divergences"] == 0 and st["shadow_errors"] == 0
        assert st["comparisons"] > 0
        # latency payload present with per-method primary/shadow deltas
        assert "latency" in st and "successors" in st["latency"]
        assert "primary_ms" in st["latency"]["successors"] and "shadow_ms" in st["latency"]["successors"]
    finally:
        h.shadow.close()


# --------------------------------------------------------------------------- adaptive cache contraction
def test_sqlite_backend_apply_pressure_contracts_caches(tmp_path):
    g, ids, _ = _graph_with_stub()
    db = _build_db(tmp_path, g)
    sl = GB.SqliteLazyGraphBackend(db)
    try:
        for i in ids:
            sl.successors(str(i)); sl.get_node(str(i))
        sl.apply_pressure("critical")   # must not raise; shrinks all three caches
        assert sl._succ_cache.maxsize == int(GB.traversal_cache_max() * 0.1)
    finally:
        sl.close()


# --------------------------------------------------------------------------- factory wiring
def test_factory_returns_harness_when_flags_on(monkeypatch, tmp_path):
    g, ids, _ = _graph_with_stub()
    db = _build_db(tmp_path, g)
    monkeypatch.setenv("JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ORACLE_PARITY_HARNESS_ENABLED", "1")
    b = GB.build_graph_backend(g, db_path=db)
    try:
        assert isinstance(b, GB.DualBackendParityHarness)
        assert isinstance(b.shadow, GB.SqliteLazyGraphBackend)
    finally:
        if hasattr(b, "shadow"):
            b.shadow.close()


def test_factory_inmemory_when_off(monkeypatch, tmp_path):
    g, _, _ = _graph_with_stub()
    db = _build_db(tmp_path, g)
    monkeypatch.delenv("JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED", raising=False)
    b = GB.build_graph_backend(g, db_path=db)
    assert isinstance(b, GB.InMemoryGraphBackend)


# --------------------------------------------------------------------------- Slice 3: live pressure governance
class _FakeGate:
    """MemoryPressureGate stand-in returning a scripted level sequence; counts probe calls."""
    def __init__(self, levels):
        self._levels = list(levels)
        self._i = 0
        self.calls = 0

    def pressure(self):
        from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
        self.calls += 1
        lvl = self._levels[min(self._i, len(self._levels) - 1)]
        self._i += 1
        return lvl


def _chain_db(tmp_path, n=30):
    g = CodebaseKnowledgeGraph()
    ids = []
    for i in range(n):
        nid = NodeID(repo="jarvis", file_path=f"p/m{i}.py", name=f"s{i}", node_type=NodeType.FUNCTION)
        g.add_node(NodeData(node_id=nid))
        ids.append(nid)
    for i in range(n - 1):
        g.add_edge(ids[i], ids[i + 1], EdgeData(EdgeType.CALLS))
    return _build_db(tmp_path, g), g, ids


def test_deep_traversal_contracts_cache_under_critical(monkeypatch, tmp_path):
    """A deep recursive traversal under sustained CRITICAL pressure must CONTRACT the working-set
    cache (not grow it) — and still return the correct full result."""
    monkeypatch.setenv("JARVIS_ORACLE_TRAVERSAL_CACHE_MAX", "20")   # small baseline → visible contraction
    monkeypatch.setenv("JARVIS_ORACLE_TRAVERSAL_PRESSURE_INTERVAL_S", "0")  # probe every frontier
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel as L
    db, g, ids = _chain_db(tmp_path, 30)
    sl = GB.SqliteLazyGraphBackend(db, memory_gate=_FakeGate([L.CRITICAL]))
    try:
        desc = sl.descendants(str(ids[0]))                  # 29-layer recursion w/ per-layer prefetch
        assert desc == set(str(i) for i in ids[1:])         # correctness preserved under pressure
        assert sl.pressure_events > 0                        # the cache contracted on live pressure
        assert sl._succ_cache.maxsize == int(20 * 0.1)       # CRITICAL ×0.1 → 2
        assert len(sl._succ_cache) <= sl._succ_cache.maxsize  # resident working set bounded
    finally:
        sl.close()


def test_pressure_probe_is_throttled(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel as L
    db, g, ids = _chain_db(tmp_path, 6)
    # large interval → at most ONE probe across rapid prefetches
    monkeypatch.setenv("JARVIS_ORACLE_TRAVERSAL_PRESSURE_INTERVAL_S", "9999")
    gate = _FakeGate([L.HIGH])
    sl = GB.SqliteLazyGraphBackend(db, memory_gate=gate)
    try:
        sl.prefetch_successors([str(i) for i in ids])
        sl.prefetch_predecessors([str(i) for i in ids])
        assert gate.calls == 1   # throttled — only the first frontier probed within the window
    finally:
        sl.close()


def test_pressure_clears_restores_baseline(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORACLE_TRAVERSAL_CACHE_MAX", "50")
    monkeypatch.setenv("JARVIS_ORACLE_TRAVERSAL_PRESSURE_INTERVAL_S", "0")
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel as L
    db, g, ids = _chain_db(tmp_path, 8)
    # CRITICAL first (contract), then OK (restore) — adaptive BOTH ways
    sl = GB.SqliteLazyGraphBackend(db, memory_gate=_FakeGate([L.CRITICAL, L.OK]))
    try:
        sl.prefetch_successors([str(ids[0])])      # probe #1 → CRITICAL → maxsize 5
        assert sl._succ_cache.maxsize == 5
        sl.prefetch_successors([str(ids[1])])      # probe #2 → OK → restore baseline 50
        assert sl._succ_cache.maxsize == 50
    finally:
        sl.close()


def test_apply_pressure_returns_evicted_and_gc_on_critical(tmp_path):
    db, g, ids = _chain_db(tmp_path, 12)
    sl = GB.SqliteLazyGraphBackend(db)
    try:
        for i in ids:
            sl.successors(str(i))
        # critical contraction must evict + not raise (exercises the GC path)
        evicted = sl.apply_pressure("critical")
        assert evicted >= 0 and sl.last_pressure == "critical"
    finally:
        sl.close()


def test_harness_zero_divergence_under_pressure(monkeypatch, tmp_path):
    """Parity must hold WHILE the shadow's cache is being contracted under pressure."""
    monkeypatch.setenv("JARVIS_ORACLE_TRAVERSAL_CACHE_MAX", "8")
    monkeypatch.setenv("JARVIS_ORACLE_TRAVERSAL_PRESSURE_INTERVAL_S", "0")
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel as L
    db, g, ids = _chain_db(tmp_path, 20)
    shadow = GB.SqliteLazyGraphBackend(db, memory_gate=_FakeGate([L.CRITICAL]))
    h = GB.DualBackendParityHarness(primary=GB.InMemoryGraphBackend(g), shadow=shadow)
    try:
        for i in ids:
            h.successors(str(i)); h.get_node(str(i)); h.predecessors(str(i))
        h.shortest_path(str(ids[0]), str(ids[19]))
        assert h.stats()["divergences"] == 0 and h.stats()["shadow_errors"] == 0
        assert shadow.pressure_events > 0   # contraction happened during the verified run
    finally:
        shadow.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
