"""Slice 154 — lazy OracleSemanticIndex (embed_nodes no longer NoneType-crashes).

Slice 153 fixed the embedder (fastembed fallback), but the live soak still showed
`[Oracle] Semantic embedding after full_index failed: 'NoneType' object has no
attribute embed_nodes` — because full_index/incremental_update call
self._semantic_index.embed_nodes while _semantic_index is still None (it's
constructed only in initialize_backend, which can run after full_index). A lazy
getter ensures the index exists wherever embed_nodes is called.
"""
from __future__ import annotations

import inspect
import unittest

from backend.core.ouroboros.oracle import TheOracle, OracleSemanticIndex


class TestLazySemanticIndex(unittest.TestCase):
    def test_ensure_constructs_when_none(self):
        o = TheOracle()
        o._semantic_index = None  # type: ignore[assignment]
        idx = o._ensure_semantic_index()
        self.assertIsNotNone(idx)
        self.assertIsInstance(idx, OracleSemanticIndex)
        self.assertIs(o._semantic_index, idx)

    def test_ensure_idempotent(self):
        o = TheOracle()
        o._semantic_index = None  # type: ignore[assignment]
        a = o._ensure_semantic_index()
        b = o._ensure_semantic_index()
        self.assertIs(a, b)

    def test_call_sites_use_ensure_not_bare_index(self):
        src = (
            inspect.getsource(TheOracle.full_index)
            + inspect.getsource(TheOracle.incremental_update)
        )
        self.assertNotIn("self._semantic_index.embed_nodes", src)
        self.assertIn("_ensure_semantic_index()", src)


if __name__ == "__main__":
    unittest.main()
