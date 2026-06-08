"""Slice 155 — OracleSemanticIndex STDLIB in-memory backend (no chromadb).

When chromadb is unavailable, the semantic index now falls back to an in-memory
numpy cosine store (STDLIB) instead of DEGRADED-empty — so embed_nodes stores and
semantic_search returns ranked results without the heavy chromadb/rust-GIL dep.
Gated default-TRUE (failure-path-only). End-to-end with a fake embedder.
"""
from __future__ import annotations

import asyncio
import os
import unittest

import numpy as np

from backend.core.ouroboros.oracle import (
    OracleSemanticIndex,
    OracleSemanticBackendStatus,
    NodeID,
    NodeData,
    NodeType,
    _stdlib_backend_available,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeEmbedder:
    """Keyword-anchored vectors so similarity is deterministic."""

    def _v(self, text: str):
        t = text.lower()
        if "login" in t:
            return np.array([1.0, 0.0, 0.0], dtype="float32")
        if "parse" in t or "json" in t:
            return np.array([0.0, 1.0, 0.0], dtype="float32")
        return np.array([0.0, 0.0, 1.0], dtype="float32")

    async def embed_batch(self, texts):
        return [self._v(t) for t in texts]

    async def embed(self, text):
        return self._v(text)


def _node(name, file_path):
    return NodeData(
        node_id=NodeID(repo="r", file_path=file_path, name=name,
                       node_type=NodeType.FUNCTION),
        signature=f"def {name}(): pass",
    )


class TestGate(unittest.TestCase):
    def test_available_default_true_with_numpy(self):
        os.environ.pop("JARVIS_ORACLE_STDLIB_BACKEND_ENABLED", None)
        self.assertTrue(_stdlib_backend_available())

    def test_disabled_via_env(self):
        os.environ["JARVIS_ORACLE_STDLIB_BACKEND_ENABLED"] = "0"
        try:
            self.assertFalse(_stdlib_backend_available())
        finally:
            os.environ.pop("JARVIS_ORACLE_STDLIB_BACKEND_ENABLED", None)


class TestStdlibBackend(unittest.TestCase):
    def _index(self):
        osi = OracleSemanticIndex()
        osi._status = OracleSemanticBackendStatus.STDLIB
        osi._embedder = _FakeEmbedder()
        osi._init_attempted = True   # skip initialize_backend; keep STDLIB
        osi._stdlib_store = {}
        return osi

    def test_embed_nodes_stores_in_memory(self):
        osi = self._index()
        _run(osi.embed_nodes([_node("login_user", "auth.py"),
                              _node("parse_json", "util.py")]))
        self.assertEqual(len(osi._stdlib_store), 2)

    def test_semantic_search_ranks_by_cosine(self):
        osi = self._index()
        _run(osi.embed_nodes([_node("login_user", "auth.py"),
                              _node("parse_json", "util.py")]))
        results = _run(osi.semantic_search("user login flow", k=2))
        self.assertTrue(results)
        self.assertEqual(results[0][0], "r:auth.py")        # login ranks first
        self.assertGreater(results[0][1], results[-1][1])   # ordered by similarity

    def test_degraded_backend_still_returns_empty(self):
        osi = OracleSemanticIndex()
        osi._status = OracleSemanticBackendStatus.DEGRADED
        osi._embedder = _FakeEmbedder()
        osi._init_attempted = True
        _run(osi.embed_nodes([_node("login_user", "auth.py")]))
        self.assertEqual(len(osi._stdlib_store), 0)           # not stored
        self.assertEqual(_run(osi.semantic_search("login")), [])


if __name__ == "__main__":
    unittest.main()
