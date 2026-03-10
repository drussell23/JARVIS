"""Tests for FileNeighborhood dataclass and TheOracle.get_file_neighborhood."""
from __future__ import annotations

import pytest
from backend.core.ouroboros.oracle import FileNeighborhood


class TestFileNeighborhoodDataclass:
    def test_to_dict_omits_empty_categories(self):
        nh = FileNeighborhood(
            target_files=["jarvis:backend/core/foo.py"],
            imports=["jarvis:backend/core/bar.py"],
            importers=[],
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )
        result = nh.to_dict()
        assert "imports" in result
        assert "importers" not in result
        assert "callers" not in result
        assert result["imports"] == ["jarvis:backend/core/bar.py"]

    def test_all_unique_files_deduplicates_and_excludes_targets(self):
        nh = FileNeighborhood(
            target_files=["jarvis:backend/core/foo.py"],
            imports=["jarvis:backend/core/bar.py", "jarvis:backend/core/baz.py"],
            importers=["jarvis:backend/core/bar.py"],  # duplicate
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=["jarvis:tests/test_foo.py"],
        )
        unique = nh.all_unique_files()
        assert "jarvis:backend/core/bar.py" in unique
        assert "jarvis:backend/core/baz.py" in unique
        assert "jarvis:tests/test_foo.py" in unique
        # targets excluded
        assert "jarvis:backend/core/foo.py" not in unique
        # deduped (bar appears twice but once in result)
        assert unique.count("jarvis:backend/core/bar.py") == 1

    def test_empty_neighborhood_produces_empty_structures(self):
        nh = FileNeighborhood(
            target_files=[],
            imports=[],
            importers=[],
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )
        assert nh.to_dict() == {}
        assert nh.all_unique_files() == []


from pathlib import Path
from unittest.mock import MagicMock


class TestGetFileNeighborhood:
    """Unit tests for TheOracle.get_file_neighborhood."""

    def _make_oracle(self, repos=None):
        """Build a minimal TheOracle-like object with a mock graph."""
        from backend.core.ouroboros.oracle import TheOracle
        oracle = object.__new__(TheOracle)
        oracle._running = True
        oracle._repos = repos or {
            "jarvis": Path("/repo/jarvis"),
            "prime": Path("/repo/prime"),
        }
        oracle._graph = MagicMock()
        return oracle

    def test_returns_empty_neighborhood_when_not_running(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood
        oracle = object.__new__(TheOracle)
        oracle._running = False
        oracle._repos = {"jarvis": tmp_path}
        oracle._graph = MagicMock()

        result = oracle.get_file_neighborhood([tmp_path / "foo.py"])
        assert isinstance(result, FileNeighborhood)
        assert result.target_files == []
        assert result.all_unique_files() == []

    def test_classifies_outgoing_imports_edge(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle({"jarvis": tmp_path})

        # Create a fake file
        target = tmp_path / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        # Mock: find_nodes_in_file returns one node
        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        # Mock: outgoing edge is an "imports" edge to bar.py
        bar_node_key = "jarvis:core/bar.py:bar_module"
        bar_node = MagicMock()
        bar_node.repo = "jarvis"
        bar_node.file_path = "core/bar.py"
        oracle._graph._node_index = {bar_node_key: bar_node}
        oracle._graph.get_edges_from.return_value = [
            (bar_node_key, {"edge_type": "imports"})
        ]
        oracle._graph.get_edges_to.return_value = []
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        assert "jarvis:core/bar.py" in result.imports
        assert result.importers == []

    def test_classifies_incoming_calls_edge(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle

        oracle = self._make_oracle({"jarvis": tmp_path})

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/service.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        caller_node_key = "jarvis:core/main.py:main_func"
        caller_node = MagicMock()
        caller_node.repo = "jarvis"
        caller_node.file_path = "core/main.py"
        oracle._graph._node_index = {caller_node_key: caller_node}
        oracle._graph.get_edges_from.return_value = []
        oracle._graph.get_edges_to.return_value = [
            (caller_node_key, {"edge_type": "calls"})
        ]
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        assert "jarvis:core/main.py" in result.callers
        assert result.callees == []

    def test_cross_repo_edge_uses_repo_prefix(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle

        prime_root = tmp_path / "prime"
        prime_root.mkdir()
        jarvis_root = tmp_path / "jarvis"
        jarvis_root.mkdir()
        oracle = self._make_oracle({"jarvis": jarvis_root, "prime": prime_root})

        target = jarvis_root / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        # Edge goes to a node in the "prime" repo
        prime_node_key = "prime:interfaces/base.py:BaseInterface"
        prime_node = MagicMock()
        prime_node.repo = "prime"
        prime_node.file_path = "interfaces/base.py"
        oracle._graph._node_index = {prime_node_key: prime_node}
        oracle._graph.get_edges_from.return_value = [
            (prime_node_key, {"edge_type": "inherits"})
        ]
        oracle._graph.get_edges_to.return_value = []
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        # Must use "prime:" prefix, NOT "jarvis:"
        assert "prime:interfaces/base.py" in result.base_classes
        assert "jarvis:interfaces/base.py" not in result.base_classes

    def test_target_file_excluded_from_all_categories(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle

        oracle = self._make_oracle({"jarvis": tmp_path})

        target = tmp_path / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        # Self-referential edge (edge to itself)
        self_key = "jarvis:core/foo.py:foo_func"
        self_node = MagicMock()
        self_node.repo = "jarvis"
        self_node.file_path = "core/foo.py"
        oracle._graph._node_index = {self_key: self_node}
        oracle._graph.get_edges_from.return_value = [(self_key, {"edge_type": "calls"})]
        oracle._graph.get_edges_to.return_value = []
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        assert "jarvis:core/foo.py" not in result.callees
        assert "jarvis:core/foo.py" not in result.all_unique_files()

    def test_test_counterpart_detected_by_basename(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle

        oracle = self._make_oracle({"jarvis": tmp_path})

        target = tmp_path / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]
        oracle._graph.get_edges_from.return_value = []
        oracle._graph.get_edges_to.return_value = []

        # _file_index has "tests/test_foo.py"
        oracle._graph._file_index = {
            "tests/test_foo.py": {"jarvis:tests/test_foo.py:test_something"},
            "tests/test_bar.py": {"jarvis:tests/test_bar.py:test_other"},
        }

        result = oracle.get_file_neighborhood([target])

        assert "jarvis:tests/test_foo.py" in result.test_counterparts
        assert "jarvis:tests/test_bar.py" not in result.test_counterparts
