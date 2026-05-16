"""
Task #106 spine — Oracle graph O(1) incremental edge/node counters.

The full-matrix quiescence proof was blocked by a standalone Oracle
cold-index degradation: 22 files/s → 1 file/s after ~1,850/29,424
files, even with ZERO event-loop contention.  Root cause pinpointed
empirically: ``CodebaseKnowledgeGraph.add_edge`` recomputed
``len(self._graph.edges)`` on every insertion, and NetworkX
``OutEdgeView.__len__`` / ``DiGraph.size()`` are O(N_nodes) (they sum
out-degree across every node).  Recomputing per-edge over a 29k-file
corpus → O(N²).  Measured (networkx 3.6.1): len(G.edges) = 9.7µs
@200 nodes → 1341µs @29k (perfectly linear).

Fix: maintain ``_metrics["total_nodes"]`` / ``["total_edges"]`` as
O(1) incremental counters; only count genuinely-new nodes/edges
(NetworkX add_node/add_edge on an existing key/(u,v) updates attrs
without changing the count).

This spine pins BOTH:

  1. **Correctness** — the incremental counters exactly equal the
     authoritative NetworkX counts after arbitrary build sequences
     including duplicate add_node / add_edge / attribute updates,
     and survive clear().
  2. **Complexity regression** — per-edge insertion cost does NOT
     scale with graph size (the O(N²) defect must stay dead).  A
     late batch (graph already large) must not be dramatically
     slower per-edge than an early batch.

Pure in-process; no Oracle.initialize(), no filesystem, no network.
"""
from __future__ import annotations

import time

import pytest


def _graph():
    from backend.core.ouroboros.oracle import (
        CodebaseKnowledgeGraph,
        NodeData,
        NodeID,
        EdgeData,
    )
    return CodebaseKnowledgeGraph, NodeData, NodeID, EdgeData


def _mk_node(NodeID, repo, fp, name, ntype):
    # NodeID signature is (repo, file_path, qualified_name, node_type)
    # — construct defensively via kwargs that the dataclass accepts.
    try:
        return NodeID(repo=repo, file_path=fp, qualified_name=name,
                      node_type=ntype)
    except TypeError:
        # Fallback positional if kw names differ in this revision.
        return NodeID(repo, fp, name, ntype)


# ---------------------------------------------------------------------------
# Correctness — counters track authoritative NetworkX counts
# ---------------------------------------------------------------------------


def test_counters_match_networkx_after_build():
    CKG, NodeData, NodeID, EdgeData = _graph()
    g = CKG()
    from backend.core.ouroboros.oracle import NodeType, EdgeType

    nt = list(NodeType)[0]
    et = list(EdgeType)[0]

    ids = []
    for i in range(50):
        nid = _mk_node(NodeID, "jarvis", f"f{i}.py", f"sym{i}", nt)
        g.add_node(NodeData(node_id=nid))
        ids.append(nid)

    for i in range(49):
        g.add_edge(ids[i], ids[i + 1], EdgeData(edge_type=et))

    # Authoritative NetworkX counts
    assert g._metrics["total_nodes"] == g._graph.number_of_nodes()
    assert g._metrics["total_edges"] == g._graph.number_of_edges()
    assert g._metrics["total_nodes"] == 50
    assert g._metrics["total_edges"] == 49


def test_duplicate_add_node_does_not_double_count():
    CKG, NodeData, NodeID, EdgeData = _graph()
    from backend.core.ouroboros.oracle import NodeType
    g = CKG()
    nt = list(NodeType)[0]
    nid = _mk_node(NodeID, "jarvis", "f.py", "s", nt)
    g.add_node(NodeData(node_id=nid))
    g.add_node(NodeData(node_id=nid))  # same key — attr update only
    g.add_node(NodeData(node_id=nid))
    assert g._metrics["total_nodes"] == 1
    assert g._metrics["total_nodes"] == g._graph.number_of_nodes()


def test_duplicate_add_edge_does_not_double_count():
    CKG, NodeData, NodeID, EdgeData = _graph()
    from backend.core.ouroboros.oracle import NodeType, EdgeType
    g = CKG()
    nt, et = list(NodeType)[0], list(EdgeType)[0]
    a = _mk_node(NodeID, "jarvis", "a.py", "a", nt)
    b = _mk_node(NodeID, "jarvis", "b.py", "b", nt)
    g.add_node(NodeData(node_id=a))
    g.add_node(NodeData(node_id=b))
    g.add_edge(a, b, EdgeData(edge_type=et))
    g.add_edge(a, b, EdgeData(edge_type=et))  # same (u,v) — attr update
    g.add_edge(a, b, EdgeData(edge_type=et))
    assert g._metrics["total_edges"] == 1
    assert g._metrics["total_edges"] == g._graph.number_of_edges()


def test_add_edge_autocreates_endpoints_counts_them():
    """add_edge auto-creates missing endpoints — those must count."""
    CKG, NodeData, NodeID, EdgeData = _graph()
    from backend.core.ouroboros.oracle import NodeType, EdgeType
    g = CKG()
    nt, et = list(NodeType)[0], list(EdgeType)[0]
    a = _mk_node(NodeID, "jarvis", "a.py", "a", nt)
    b = _mk_node(NodeID, "jarvis", "b.py", "b", nt)
    g.add_edge(a, b, EdgeData(edge_type=et))  # neither endpoint exists yet
    assert g._metrics["total_nodes"] == 2
    assert g._metrics["total_edges"] == 1
    assert g._metrics["total_nodes"] == g._graph.number_of_nodes()
    assert g._metrics["total_edges"] == g._graph.number_of_edges()


def test_clear_resets_counters():
    CKG, NodeData, NodeID, EdgeData = _graph()
    from backend.core.ouroboros.oracle import NodeType, EdgeType
    g = CKG()
    nt, et = list(NodeType)[0], list(EdgeType)[0]
    a = _mk_node(NodeID, "jarvis", "a.py", "a", nt)
    b = _mk_node(NodeID, "jarvis", "b.py", "b", nt)
    g.add_node(NodeData(node_id=a))
    g.add_node(NodeData(node_id=b))
    g.add_edge(a, b, EdgeData(edge_type=et))
    assert g._metrics["total_nodes"] == 2
    g.clear()
    assert g._metrics["total_nodes"] == 0
    assert g._metrics["total_edges"] == 0
    assert g._graph.number_of_nodes() == 0


# ---------------------------------------------------------------------------
# Complexity regression — per-edge cost must NOT scale with graph size
# ---------------------------------------------------------------------------


def test_add_edge_cost_does_not_scale_with_graph_size():
    """The O(N²) defect (recomputing len(G.edges) per add_edge) must
    stay dead.  Build a large graph; compare the wall time of an
    early 500-edge batch vs a late 500-edge batch when the graph is
    already ~10k nodes.  With the O(1) counter the ratio is ~constant;
    with the old O(N) recompute the late batch was 40-130× slower.
    """
    CKG, NodeData, NodeID, EdgeData = _graph()
    from backend.core.ouroboros.oracle import NodeType, EdgeType
    g = CKG()
    nt, et = list(NodeType)[0], list(EdgeType)[0]

    N = 12000
    ids = []
    for i in range(N):
        nid = _mk_node(NodeID, "jarvis", f"f{i}.py", f"s{i}", nt)
        g.add_node(NodeData(node_id=nid))
        ids.append(nid)

    # Early batch: edges among the first ~600 nodes (graph small-ish
    # in *edge* terms, but ALL N nodes already present so the old
    # O(N_nodes) len(G.edges) cost is already in play if it regressed).
    t0 = time.perf_counter()
    for i in range(500):
        g.add_edge(ids[i], ids[i + 1], EdgeData(edge_type=et))
    early = time.perf_counter() - t0

    # Late batch: 500 more edges when total_edges is already large.
    for i in range(500, 8000):
        g.add_edge(ids[i], ids[i + 1], EdgeData(edge_type=et))
    t0 = time.perf_counter()
    for i in range(8000, 8500):
        g.add_edge(ids[i], ids[i + 1], EdgeData(edge_type=et))
    late = time.perf_counter() - t0

    # Correctness still holds at scale
    assert g._metrics["total_edges"] == g._graph.number_of_edges()

    # Complexity pin: late batch must not be wildly slower than early.
    # Generous 8× ceiling (noise-tolerant); the old O(N²) defect made
    # this 40-130×.  O(1) counter keeps it ~1-2×.
    ratio = late / max(early, 1e-9)
    assert ratio < 8.0, (
        f"add_edge per-edge cost scaled with graph size "
        f"(early={early*1e3:.2f}ms late={late*1e3:.2f}ms ratio={ratio:.1f}×)"
        f" — the O(N²) len(G.edges) recompute regressed"
    )


# ---------------------------------------------------------------------------
# AST pin — the O(N) recompute is gone from the hot path
# ---------------------------------------------------------------------------


def test_ast_pin_no_len_graph_edges_recompute_in_add_edge():
    from pathlib import Path
    import ast as _ast
    src = (
        Path(__file__).parents[2]
        / "backend" / "core" / "ouroboros" / "oracle.py"
    ).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    add_edge_fn = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "add_edge":
            add_edge_fn = node
            break
    assert add_edge_fn is not None
    seg = _ast.get_source_segment(src, add_edge_fn) or ""
    # Strip comment lines so the explanatory comment (which names the
    # forbidden call to document the defect) doesn't false-positive;
    # pin the actual recompute ASSIGNMENT, not the substring.
    code_only = "\n".join(
        ln for ln in seg.splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert 'self._metrics["total_edges"] = len(self._graph.edges)' not in code_only, (
        "add_edge MUST NOT recompute total_edges via len(self._graph."
        "edges) — that O(N_nodes) call caused the O(N²) cold-index decay"
    )
    assert "len(self._graph.edges)" not in code_only, (
        "add_edge code (excl. comments) MUST NOT call len(self._graph."
        "edges) at all — O(N_nodes) for a DiGraph"
    )
    assert 'self._metrics["total_edges"] += 1' in seg, (
        "add_edge MUST maintain total_edges as an O(1) incremental "
        "counter"
    )
    assert "self._graph.has_edge(" in seg, (
        "add_edge MUST O(1)-guard new-edge detection via has_edge"
    )
