# tests/test_ouroboros_governance/test_blast_radius_adapter.py
"""Tests for the Oracle blast radius adapter."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.blast_radius_adapter import (
    BlastRadiusAdapter,
    BlastRadiusResult,
)
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
)


def _mock_oracle(total_affected: int, risk_level: str):
    """Create a mock CodebaseKnowledgeGraph with preset blast radius."""
    oracle = MagicMock()
    blast = MagicMock()
    blast.total_affected = total_affected
    blast.risk_level = risk_level
    blast.directly_affected = set()
    blast.transitively_affected = set()
    oracle.compute_blast_radius.return_value = blast
    oracle.find_nodes_in_file.return_value = ["node-1"]
    return oracle


class TestBlastRadiusResult:
    def test_result_fields(self):
        """BlastRadiusResult has all required fields."""
        result = BlastRadiusResult(
            total_affected=5,
            risk_level="medium",
            from_oracle=True,
        )
        assert result.total_affected == 5
        assert result.risk_level == "medium"
        assert result.from_oracle is True


class TestBlastRadiusAdapter:
    def test_compute_from_oracle(self):
        """Adapter uses Oracle when available."""
        oracle = _mock_oracle(total_affected=8, risk_level="medium")
        adapter = BlastRadiusAdapter(oracle=oracle)
        result = adapter.compute("backend/core/foo.py")
        assert result.total_affected == 8
        assert result.risk_level == "medium"
        assert result.from_oracle is True

    def test_fallback_without_oracle(self):
        """Adapter returns fallback value when no Oracle provided."""
        adapter = BlastRadiusAdapter(oracle=None)
        result = adapter.compute("backend/core/foo.py")
        assert result.total_affected == 1
        assert result.risk_level == "low"
        assert result.from_oracle is False

    def test_fallback_on_oracle_error(self):
        """Adapter falls back gracefully on Oracle error."""
        oracle = MagicMock()
        oracle.find_nodes_in_file.side_effect = RuntimeError("graph corrupt")
        adapter = BlastRadiusAdapter(oracle=oracle)
        result = adapter.compute("backend/core/foo.py")
        assert result.from_oracle is False
        assert result.total_affected == 1

    def test_no_nodes_in_file(self):
        """File not in Oracle graph returns fallback."""
        oracle = MagicMock()
        oracle.find_nodes_in_file.return_value = []
        adapter = BlastRadiusAdapter(oracle=oracle)
        result = adapter.compute("backend/core/foo.py")
        assert result.from_oracle is False

    def test_enrich_profile_updates_blast_radius(self):
        """enrich_profile() updates profile blast_radius from Oracle."""
        oracle = _mock_oracle(total_affected=12, risk_level="high")
        adapter = BlastRadiusAdapter(oracle=oracle)
        profile = OperationProfile(
            files_affected=[Path("foo.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 12

    def test_enrich_profile_multi_file_takes_max(self):
        """enrich_profile() with multiple files uses max blast radius."""
        oracle = MagicMock()
        blast_a = MagicMock()
        blast_a.total_affected = 5
        blast_a.risk_level = "medium"
        blast_b = MagicMock()
        blast_b.total_affected = 15
        blast_b.risk_level = "high"
        oracle.find_nodes_in_file.return_value = ["node-1"]
        oracle.compute_blast_radius.side_effect = [blast_a, blast_b]
        adapter = BlastRadiusAdapter(oracle=oracle)
        profile = OperationProfile(
            files_affected=[Path("a.py"), Path("b.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 15

    def test_enrich_profile_preserves_other_fields(self):
        """enrich_profile() preserves all non-blast_radius fields."""
        oracle = _mock_oracle(total_affected=3, risk_level="low")
        adapter = BlastRadiusAdapter(oracle=oracle)
        profile = OperationProfile(
            files_affected=[Path("foo.py")],
            change_type=ChangeType.DELETE,
            blast_radius=1,
            crosses_repo_boundary=True,
            touches_security_surface=True,
            touches_supervisor=False,
            test_scope_confidence=0.5,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.change_type == ChangeType.DELETE
        assert enriched.crosses_repo_boundary is True
        assert enriched.touches_security_surface is True
        assert enriched.test_scope_confidence == 0.5
