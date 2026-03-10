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
