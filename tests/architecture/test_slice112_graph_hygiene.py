"""Slice 112 Phase 3 — graph hygiene: isolated-node pruning.

Proves the prune removes pure-bloat (degree-0) nodes while leaving every
connected node + every traversal result (shortest_path) invariant, and keeps
the indices + metrics consistent.
"""

from __future__ import annotations

import networkx as nx
import pytest

from backend.core.ouroboros.oracle import (
    CodebaseKnowledgeGraph,
    NodeData,
    NodeID,
    NodeType,
)


def _node(name: str) -> NodeID:
    return NodeID(repo="jarvis", file_path=f"backend/{name}.py", name=name,
                  node_type=NodeType.FUNCTION)


def _build_graph_with_isolate():
    g = CodebaseKnowledgeGraph()
    a, b, iso = _node("a"), _node("b"), _node("orphan")
    for nid in (a, b, iso):
        g.add_node(NodeData(node_id=nid))
    # Connect a → b; leave `orphan` isolated (degree 0).
    g._graph.add_edge(str(a), str(b))
    return g, a, b, iso


class TestGraphHygiene:
    def test_prune_removes_only_isolated_nodes(self):
        g, a, b, iso = _build_graph_with_isolate()
        assert g._graph.number_of_nodes() == 3
        pruned = g.prune_isolated_nodes()
        assert pruned == 1
        assert str(iso) not in g._graph
        assert str(a) in g._graph and str(b) in g._graph

    def test_traversal_results_invariant_after_prune(self):
        g, a, b, iso = _build_graph_with_isolate()
        path_before = nx.shortest_path(g._graph, str(a), str(b))
        g.prune_isolated_nodes()
        path_after = nx.shortest_path(g._graph, str(a), str(b))
        assert path_before == path_after  # pruning isolated nodes never changes a path

    def test_indices_and_metrics_consistent_after_prune(self):
        g, a, b, iso = _build_graph_with_isolate()
        g.prune_isolated_nodes()
        # node_index no longer references the orphan.
        assert str(iso) not in g._node_index
        assert str(a) in g._node_index
        # file_index bucket for the orphan's file is dropped (now empty).
        assert iso.file_path not in g._file_index
        # metrics recomputed from the live graph.
        assert g._metrics["total_nodes"] == g._graph.number_of_nodes() == 2

    def test_prune_is_idempotent_and_safe_on_empty(self):
        g, a, b, iso = _build_graph_with_isolate()
        assert g.prune_isolated_nodes() == 1
        assert g.prune_isolated_nodes() == 0  # nothing left to prune
        empty = CodebaseKnowledgeGraph()
        assert empty.prune_isolated_nodes() == 0  # never raises on empty
