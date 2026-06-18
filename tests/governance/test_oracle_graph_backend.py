"""Slice 1 — the GraphBackend traversal seam + live differential parity harness.

Validates the seam against the production CodebaseKnowledgeGraph (no mocks): the in-memory backend's
primitives + algorithms, the DualBackendParityHarness's autonomous-fallback-on-divergence behavior,
and the adaptive working-set cache.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.oracle_graph_backend as GB
from backend.core.ouroboros.oracle import (
    CodebaseKnowledgeGraph,
    EdgeData,
    EdgeType,
    NodeData,
    NodeID,
    NodeType,
)


def _g(n=4):
    """Linear call chain sym0 -> sym1 -> sym2 -> ... across files."""
    g = CodebaseKnowledgeGraph()
    ids = []
    for i in range(n):
        nid = NodeID(repo="jarvis", file_path=f"pkg/m{i}.py", name=f"sym{i}",
                     node_type=NodeType.FUNCTION, line_number=1)
        g.add_node(NodeData(node_id=nid, docstring=f"d{i}"))
        ids.append(nid)
    for i in range(n - 1):
        g.add_edge(ids[i], ids[i + 1], EdgeData(EdgeType.CALLS, line_number=i))
    return g, ids


# --------------------------------------------------------------------------- in-memory primitives
def test_inmemory_primitives_match_graph():
    g, ids = _g(4)
    b = g._backend
    k0, k1 = str(ids[0]), str(ids[1])
    assert b.contains(k0) and not b.contains("nope")
    assert b.get_node(k0)["node_id"]["name"] == "sym0"
    assert b.get_node("nope") is None
    succ = b.successors(k0)
    assert succ == [(k1, {"edge_type": "calls", "line_number": 0, "context": ""})]
    assert b.predecessors(k1) == [(k0, {"edge_type": "calls", "line_number": 0, "context": ""})]
    assert b.node_count() == 4 and b.edge_count() == 3
    assert set(b.all_keys()) == {str(i) for i in ids}


def test_inmemory_shortest_path_and_cycles():
    g, ids = _g(4)
    b = g._backend
    path = b.shortest_path(str(ids[0]), str(ids[3]))
    assert path == [str(i) for i in ids]            # full chain
    assert b.shortest_path(str(ids[3]), str(ids[0])) is None  # no reverse edge
    assert b.simple_cycles() == []                  # acyclic
    # add a back-edge → one cycle
    g.add_edge(ids[3], ids[0], EdgeData(EdgeType.CALLS))
    cycles = g._backend.simple_cycles()
    assert len(cycles) == 1 and set(cycles[0]) == {str(i) for i in ids}


def test_default_algorithms_match_nx_overrides():
    """The ABC's primitive-only shortest_path/simple_cycles (used by the future SQLite backend) must
    agree with the in-memory nx overrides."""
    g, ids = _g(5)
    g.add_edge(ids[4], ids[1], EdgeData(EdgeType.CALLS))  # cycle 1..4

    native = g._backend  # InMemory (nx overrides)

    class _PurePrimitiveView(GB.GraphBackend):
        """Wraps the in-memory primitives but uses the ABC's DEFAULT algorithms (no nx)."""
        def __init__(self, base): self._b = base
        def contains(self, k): return self._b.contains(k)
        def get_node(self, k): return self._b.get_node(k)
        def successors(self, k): return self._b.successors(k)
        def predecessors(self, k): return self._b.predecessors(k)
        def node_count(self): return self._b.node_count()
        def edge_count(self): return self._b.edge_count()
        def all_keys(self): return self._b.all_keys()

    pure = _PurePrimitiveView(native)
    # shortest path identical
    assert pure.shortest_path(str(ids[0]), str(ids[4])) == native.shortest_path(str(ids[0]), str(ids[4]))
    # cycle sets identical (order-insensitive)
    nset = {frozenset(c) for c in native.simple_cycles()}
    pset = {frozenset(c) for c in pure.simple_cycles()}
    assert nset == pset and len(nset) == 1


# --------------------------------------------------------------------------- parity harness
def test_parity_no_shadow_passthrough():
    g, ids = _g(3)
    h = GB.DualBackendParityHarness(primary=g._backend, shadow=None)
    assert h.successors(str(ids[0])) == g._backend.successors(str(ids[0]))
    assert h.comparisons == 0 and h.divergences == 0


def test_parity_equal_shadow_zero_divergence():
    g, ids = _g(4)
    # shadow is a SECOND in-memory backend over the same graph → must always agree
    h = GB.DualBackendParityHarness(primary=g._backend, shadow=GB.InMemoryGraphBackend(g))
    for nid in ids:
        h.get_node(str(nid)); h.successors(str(nid)); h.predecessors(str(nid))
    h.node_count(); h.edge_count(); h.shortest_path(str(ids[0]), str(ids[3]))
    assert h.divergences == 0 and h.shadow_errors == 0
    assert h.comparisons > 0


def test_parity_divergence_captures_telemetry_and_falls_back():
    """A wrong shadow must NOT corrupt results: harness returns the PRIMARY value, records rich
    telemetry, and never raises."""
    g, ids = _g(3)
    captured = []

    class _WrongShadow(GB.InMemoryGraphBackend):
        def successors(self, key):
            return [("BOGUS:node:x", {"edge_type": "calls", "line_number": 99, "context": "wrong"})]
        def get_node(self, key):
            return {"node_id": {"name": "WRONG"}}

    h = GB.DualBackendParityHarness(
        primary=g._backend, shadow=_WrongShadow(g), on_divergence=captured.append,
    )
    # result is the TRUSTED primary's, despite the lying shadow
    assert h.successors(str(ids[0])) == g._backend.successors(str(ids[0]))
    assert h.get_node(str(ids[0]))["node_id"]["name"] == "sym0"
    assert h.divergences >= 2
    assert captured and captured[0]["kind"] == "divergence"
    assert "method" in captured[0] and "primary" in captured[0] and "shadow" in captured[0]
    assert len(h.records) >= 2


def test_parity_shadow_exception_falls_back_no_crash():
    g, ids = _g(3)

    class _ExplodingShadow(GB.InMemoryGraphBackend):
        def successors(self, key):
            raise RuntimeError("shadow backend on fire")

    h = GB.DualBackendParityHarness(primary=g._backend, shadow=_ExplodingShadow(g))
    # must still return the primary's correct result, counting a shadow_error
    assert h.successors(str(ids[0])) == g._backend.successors(str(ids[0]))
    assert h.shadow_errors == 1 and h.divergences == 0
    assert h.records[-1]["kind"] == "shadow_error"


# --------------------------------------------------------------------------- adaptive cache
def test_adaptive_cache_lru_eviction():
    c = GB.AdaptiveNodeCache(baseline=3)
    for i in range(3):
        c.put(f"k{i}", i)
    assert len(c) == 3
    c.get("k0")            # touch k0 → most-recent
    c.put("k3", 3)         # overflow → evict LRU (k1)
    assert c.get("k1") is None and c.get("k0") == 0 and c.get("k3") == 3
    assert c.evictions == 1


def test_adaptive_cache_contracts_under_pressure():
    c = GB.AdaptiveNodeCache(baseline=100)
    for i in range(100):
        c.put(f"k{i}", i)
    assert len(c) == 100
    evicted = c.apply_pressure("high")     # ×0.5
    assert c.maxsize == 50 and len(c) == 50 and evicted == 50
    evicted2 = c.apply_pressure("critical")  # ×0.1
    assert c.maxsize == 10 and len(c) == 10 and evicted2 == 40
    c.apply_pressure("ok")                  # restore baseline
    assert c.maxsize == 100


def test_cache_default_baseline_is_5000(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_TRAVERSAL_CACHE_MAX", raising=False)
    assert GB.traversal_cache_max() == 5000               # §10 resolution
    assert GB.AdaptiveNodeCache().maxsize == 5000


def test_flags_default_off(monkeypatch):
    for f in ("JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED", "JARVIS_ORACLE_PARITY_HARNESS_ENABLED"):
        monkeypatch.delenv(f, raising=False)
    assert GB.lazy_traversal_enabled() is False
    assert GB.parity_harness_enabled() is False


# --------------------------------------------------------------------------- seam preserves Oracle behavior
def test_oracle_query_methods_still_work_through_seam():
    """The refactored CodebaseKnowledgeGraph query methods (now routing through the seam) return the
    same results — the regression net for the in-place refactor."""
    g, ids = _g(4)
    assert g.get_node(str(ids[0]))["node_id"]["name"] == "sym0"
    assert [k for k, _ in g.get_edges_from(str(ids[0]))] == [str(ids[1])]
    assert [k for k, _ in g.get_edges_to(str(ids[1]))] == [str(ids[0])]
    chain = g.find_call_chain(ids[0], ids[3])
    assert chain is not None and [n.name for n in chain] == ["sym0", "sym1", "sym2", "sym3"]
    assert g.find_circular_dependencies() == []
    g.add_edge(ids[3], ids[0], EdgeData(EdgeType.CALLS))
    assert len(g.find_circular_dependencies()) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
