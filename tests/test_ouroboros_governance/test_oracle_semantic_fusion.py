"""Tests for OracleSemanticIndex and semantic fusion."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class TestOracleSemanticIndex:
    """Unit tests for OracleSemanticIndex."""

    def test_is_ready_false_when_unavailable(self):
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = False
        assert idx.is_ready() is False

    def test_is_ready_true_when_available(self):
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = True
        assert idx.is_ready() is True

    async def test_semantic_search_returns_empty_when_not_available(self):
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = False
        result = await idx.semantic_search("find authentication code", k=5)
        assert result == []

    async def test_embed_nodes_skips_when_not_available(self):
        """embed_nodes must not raise when unavailable."""
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = False
        node = MagicMock()
        # Should not raise
        await idx.embed_nodes([node])

    async def test_embed_nodes_skips_nodes_without_content(self):
        """Nodes with no docstring AND no signature are skipped."""
        from backend.core.ouroboros.oracle import OracleSemanticIndex, NodeData, NodeID, NodeType
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = True
        idx._collection = MagicMock()
        idx._embedder = AsyncMock()
        idx._embedder.embed_batch = AsyncMock(return_value=[])

        node_id = NodeID(repo="jarvis", file_path="core/foo.py", name="foo",
                         node_type=NodeType.FUNCTION, line_number=1)
        node = NodeData(node_id=node_id, docstring=None, signature=None)

        # Should not raise, and embedder should not be called
        await idx.embed_nodes([node])
        idx._embedder.embed_batch.assert_not_called()

    async def test_semantic_search_returns_file_score_tuples(self):
        """semantic_search returns List[Tuple[repo:file_path, float]]."""
        import numpy as np
        from backend.core.ouroboros.oracle import OracleSemanticIndex

        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = True
        idx._embedder = MagicMock()
        idx._embedder.embed = AsyncMock(return_value=np.zeros(384))

        mock_collection = MagicMock()
        mock_collection.query.return_value = {
            "ids": [["id1", "id2"]],
            "distances": [[0.1, 0.3]],
            "metadatas": [[
                {"repo": "jarvis", "file_path": "core/auth.py"},
                {"repo": "prime", "file_path": "services/auth.py"},
            ]],
        }
        idx._collection = mock_collection

        results = await idx.semantic_search("authentication logic", k=2)

        assert len(results) == 2
        assert results[0][0] == "jarvis:core/auth.py"
        assert results[1][0] == "prime:services/auth.py"
        # distance 0.1 → similarity 0.9; distance 0.3 → similarity 0.7
        assert abs(results[0][1] - 0.9) < 0.01
        assert abs(results[1][1] - 0.7) < 0.01


class TestOracleEmbeddingLifecycle:
    """Tests that embedding is called during oracle indexing."""

    def _make_oracle(self):
        import asyncio
        from backend.core.ouroboros.oracle import TheOracle
        oracle = object.__new__(TheOracle)
        oracle._running = False
        oracle._lock = asyncio.Lock()
        oracle._file_hashes = {}
        oracle._graph = MagicMock()
        oracle._graph._metrics = {
            "total_nodes": 0, "total_edges": 0,
            "files_indexed": 0, "last_full_index": 0.0,
            "last_incremental_update": 0.0,
        }
        oracle._repos = {}
        oracle._semantic_index = MagicMock()
        oracle._semantic_index.embed_nodes = AsyncMock(return_value=None)
        oracle._semantic_index.is_ready = MagicMock(return_value=True)
        return oracle

    async def test_full_index_calls_embed_nodes(self):
        """full_index() must call _semantic_index.embed_nodes after indexing."""
        oracle = self._make_oracle()

        oracle._index_repository = AsyncMock(return_value=None)
        oracle._save_cache = AsyncMock(return_value=None)
        oracle._graph.get_all_nodes = MagicMock(return_value=[MagicMock(), MagicMock()])

        await oracle.full_index()

        oracle._semantic_index.embed_nodes.assert_called_once()
        nodes_arg = oracle._semantic_index.embed_nodes.call_args[0][0]
        assert len(nodes_arg) == 2

    async def test_full_index_embed_failure_does_not_raise(self):
        """embed_nodes failure during full_index must not propagate."""
        oracle = self._make_oracle()
        oracle._index_repository = AsyncMock(return_value=None)
        oracle._save_cache = AsyncMock(return_value=None)
        oracle._graph.get_all_nodes = MagicMock(return_value=[MagicMock()])
        oracle._semantic_index.embed_nodes = AsyncMock(side_effect=RuntimeError("chroma down"))

        # Must not raise
        await oracle.full_index()

    async def test_semantic_index_initialized_in_constructor(self):
        """TheOracle.__init__ must create _semantic_index attribute."""
        from backend.core.ouroboros.oracle import TheOracle, OracleSemanticIndex
        oracle = object.__new__(TheOracle)
        TheOracle.__init__(oracle)
        assert hasattr(oracle, "_semantic_index")
        assert isinstance(oracle._semantic_index, OracleSemanticIndex)
