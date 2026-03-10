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


class TestFileNeighborhoodSemanticSupport:
    """Tests for FileNeighborhood.semantic_support field."""

    def test_semantic_support_included_in_to_dict(self):
        from backend.core.ouroboros.oracle import FileNeighborhood
        nh = FileNeighborhood(
            target_files=["jarvis:core/foo.py"],
            imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth.py"],
        )
        d = nh.to_dict()
        assert "semantic_support" in d
        assert d["semantic_support"] == ["prime:services/auth.py"]

    def test_semantic_support_omitted_when_empty(self):
        from backend.core.ouroboros.oracle import FileNeighborhood
        nh = FileNeighborhood(
            target_files=["jarvis:core/foo.py"],
            imports=["jarvis:core/bar.py"], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=[],
        )
        d = nh.to_dict()
        assert "semantic_support" not in d

    def test_all_unique_files_includes_semantic_support(self):
        from backend.core.ouroboros.oracle import FileNeighborhood
        nh = FileNeighborhood(
            target_files=["jarvis:core/foo.py"],
            imports=["jarvis:core/bar.py"], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth.py"],
        )
        unique = nh.all_unique_files()
        assert "prime:services/auth.py" in unique
        assert "jarvis:core/bar.py" in unique


class TestGetFusedNeighborhood:
    """Tests for TheOracle.get_fused_neighborhood()."""

    def _make_oracle_with_mocks(self):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood
        oracle = object.__new__(TheOracle)
        oracle._running = True
        oracle._repos = {"jarvis": Path("/repo/jarvis"), "prime": Path("/repo/prime")}
        oracle._graph = MagicMock()
        oracle._semantic_index = MagicMock()
        oracle._semantic_index.is_ready = MagicMock(return_value=True)
        oracle._semantic_index.semantic_search = AsyncMock(return_value=[])
        return oracle

    async def test_returns_empty_when_not_running(self):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood
        oracle = object.__new__(TheOracle)
        oracle._running = False
        oracle._repos = {}
        oracle._graph = MagicMock()
        oracle._semantic_index = MagicMock()
        oracle._semantic_index.is_ready = MagicMock(return_value=False)

        result = await oracle.get_fused_neighborhood([], "some query")
        assert isinstance(result, FileNeighborhood)
        assert result.all_unique_files() == []

    async def test_structural_files_go_into_structural_categories(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        structural_nh = FileNeighborhood(
            target_files=["jarvis:core/service.py"],
            imports=["jarvis:core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)
        oracle._semantic_index.semantic_search = AsyncMock(return_value=[])

        result = await oracle.get_fused_neighborhood([target], "fix the service")

        assert "jarvis:core/base.py" in result.imports
        assert result.semantic_support == []

    async def test_semantic_seeds_go_into_semantic_support(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        empty_nh = FileNeighborhood(
            target_files=[], imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle.get_file_neighborhood = MagicMock(return_value=empty_nh)
        oracle._semantic_index.semantic_search = AsyncMock(
            return_value=[("prime:services/auth.py", 0.91)]
        )

        result = await oracle.get_fused_neighborhood([target], "authentication logic")

        assert "prime:services/auth.py" in result.semantic_support

    async def test_semantic_failure_degrades_to_structural_only(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        structural_nh = FileNeighborhood(
            target_files=["jarvis:core/service.py"],
            imports=["jarvis:core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)
        oracle._semantic_index.semantic_search = AsyncMock(
            side_effect=RuntimeError("chroma connection failed")
        )

        result = await oracle.get_fused_neighborhood([target], "fix service")

        assert "jarvis:core/base.py" in result.imports
        assert result.semantic_support == []

    async def test_fusion_score_ranks_structural_above_low_semantic(self, tmp_path):
        """Structural files (graph_proximity=1.0) must outscore low-similarity seeds."""
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        structural_nh = FileNeighborhood(
            target_files=["jarvis:core/service.py"],
            imports=["jarvis:core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)
        oracle._semantic_index.semantic_search = AsyncMock(
            return_value=[("prime:utils/misc.py", 0.10)]
        )

        result = await oracle.get_fused_neighborhood([target], "fix service")

        # structural base.py score: 0.55*1.0 + 0.35*0.0 + 0.10*1.0 = 0.65
        # semantic misc.py score:   0.55*0.5 + 0.35*0.10 + 0.10*1.0 = 0.375
        # base.py in structural, misc.py in semantic_support
        assert "jarvis:core/base.py" in result.imports
        assert "prime:utils/misc.py" in result.semantic_support


class TestContextExpanderFusedRendering:
    """Tests for dual-section rendering of fused neighborhood."""

    def _make_ctx(self, description="fix the auth service", target_files=("backend/core/service.py",)):
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
        return OperationContext.create(
            op_id="fusion-test",
            description=description,
            target_files=tuple(target_files),
        ).advance(OperationPhase.ROUTE).advance(OperationPhase.CONTEXT_EXPANSION)

    async def test_expand_calls_get_fused_neighborhood(self, tmp_path):
        """expand() must await get_fused_neighborhood, NOT get_file_neighborhood."""
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        fused_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth_service.py"],
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        oracle.index_age_s.return_value = 0.0
        oracle.get_fused_neighborhood = AsyncMock(return_value=fused_nh)
        oracle.get_file_neighborhood = MagicMock()

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        expander._generator = MagicMock()
        expander._generator.plan = AsyncMock(
            return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "none"}'
        )

        from datetime import datetime, timedelta
        deadline = datetime.now() + timedelta(seconds=30)
        ctx = self._make_ctx()
        await expander.expand(ctx, deadline)

        oracle.get_fused_neighborhood.assert_called_once()
        oracle.get_file_neighborhood.assert_not_called()

    async def test_structural_section_labeled_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        fused_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth_service.py"],
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], neighborhood=fused_nh)

        assert "Structural" in prompt or "structural" in prompt
        assert "jarvis:backend/core/base.py" in prompt

    async def test_semantic_support_section_labeled_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        fused_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth_service.py", "reactor:handlers/base.py"],
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], neighborhood=fused_nh)

        assert "Semantic" in prompt or "semantic" in prompt
        assert "prime:services/auth_service.py" in prompt
        assert "reactor:handlers/base.py" in prompt

    async def test_semantic_support_truncated_at_10(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        semantic = [f"prime:services/svc_{i:02d}.py" for i in range(15)]
        fused_nh = FileNeighborhood(
            target_files=[], imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=semantic,
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], neighborhood=fused_nh)

        shown = sum(1 for line in prompt.splitlines() if "svc_" in line and line.strip().startswith("- "))
        assert shown == 10
        assert "and 5 more" in prompt

    async def test_fused_neighborhood_failure_falls_back_to_structural(self, tmp_path):
        """If get_fused_neighborhood raises, falls back to get_file_neighborhood."""
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        structural_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        oracle.index_age_s.return_value = 0.0
        oracle.get_fused_neighborhood = AsyncMock(side_effect=RuntimeError("chroma down"))
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        expander._generator = MagicMock()
        expander._generator.plan = AsyncMock(
            return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "none"}'
        )

        from datetime import datetime, timedelta
        deadline = datetime.now() + timedelta(seconds=30)
        ctx = self._make_ctx()

        # Must not raise — fallback to structural
        await expander.expand(ctx, deadline)
        oracle.get_file_neighborhood.assert_called_once()
