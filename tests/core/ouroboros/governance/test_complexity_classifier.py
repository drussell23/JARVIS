"""Tests for OperationComplexityClassifier + Assimilation Gate."""
import pytest
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.complexity_classifier import (
    ClassificationResult,
    ComplexityClass,
    OperationComplexityClassifier,
    PersistenceClass,
)
from backend.core.topology.topology_map import CapabilityNode, TopologyMap


class TestComplexityClassification:
    def test_trivial_typo_fix(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix typo in readme", ["README.md"])
        assert result.complexity == ComplexityClass.TRIVIAL

    def test_trivial_comment_change(self):
        c = OperationComplexityClassifier()
        result = c.classify("update comment in parser", ["parser.py"])
        assert result.complexity == ComplexityClass.TRIVIAL

    def test_trivial_config_file(self):
        c = OperationComplexityClassifier()
        result = c.classify("update config", ["config.yaml"])
        assert result.complexity == ComplexityClass.TRIVIAL

    def test_simple_single_file(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix authentication bug", ["auth.py"])
        assert result.complexity == ComplexityClass.SIMPLE

    def test_moderate_multi_file(self):
        c = OperationComplexityClassifier()
        result = c.classify("refactor user module", ["user.py", "user_test.py", "models.py"])
        assert result.complexity == ComplexityClass.MODERATE

    def test_complex_many_files(self):
        c = OperationComplexityClassifier()
        result = c.classify("update API layer", ["api.py", "routes.py", "models.py", "tests.py"])
        assert result.complexity == ComplexityClass.COMPLEX

    def test_architectural_keyword_overrides(self):
        c = OperationComplexityClassifier()
        result = c.classify("new capability for PDF parsing", ["parser.py"])
        assert result.complexity == ComplexityClass.ARCHITECTURAL

    def test_architectural_design_keyword(self):
        c = OperationComplexityClassifier()
        result = c.classify("architecture change for event system", ["events.py"])
        assert result.complexity == ComplexityClass.ARCHITECTURAL


class TestPersistenceClassification:
    def test_existing_capability_matches(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="voice_auth_ecapa", domain="voice", repo_owner="jarvis", active=True))
        c = OperationComplexityClassifier(topology=topo)
        result = c.classify("fix voice auth ecapa verification", ["auth.py"])
        assert result.persistence == PersistenceClass.EXISTING
        assert result.matched_capability == "voice_auth_ecapa"

    def test_ephemeral_no_history(self):
        c = OperationComplexityClassifier()
        result = c.classify("one-off data migration", ["migrate.py"])
        assert result.persistence == PersistenceClass.EPHEMERAL
        assert result.frequency_count == 0

    def test_persistent_high_frequency(self):
        # Simulate 5 similar historical operations
        history = []
        for i in range(5):
            entry = MagicMock()
            entry.data = {"description": "fix data migration script"}
            history.append(entry)
        c = OperationComplexityClassifier()
        result = c.classify("fix data migration script", ["migrate.py"], op_history=history)
        assert result.persistence == PersistenceClass.PERSISTENT
        assert result.frequency_count >= 3

    def test_ephemeral_low_frequency(self):
        history = []
        for i in range(2):
            entry = MagicMock()
            entry.data = {"description": "fix data migration"}
            history.append(entry)
        c = OperationComplexityClassifier()
        result = c.classify("fix data migration", ["migrate.py"], op_history=history)
        assert result.persistence == PersistenceClass.EPHEMERAL


class TestAutoApproveEligibility:
    def test_trivial_ephemeral_auto_approve(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix typo", ["readme.md"])
        assert result.auto_approve_eligible is True

    def test_simple_ephemeral_auto_approve(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix null check", ["handler.py"])
        assert result.auto_approve_eligible is True

    def test_architectural_never_auto_approve(self):
        c = OperationComplexityClassifier()
        result = c.classify("new capability for PDF parsing", ["parser.py"])
        assert result.auto_approve_eligible is False

    def test_complex_not_auto_approve(self):
        c = OperationComplexityClassifier()
        result = c.classify("refactor entire API", ["a.py", "b.py", "c.py", "d.py"])
        assert result.auto_approve_eligible is False

    def test_persistent_not_auto_approve(self):
        history = [MagicMock(data={"description": "parse CSV data"})] * 5
        c = OperationComplexityClassifier()
        result = c.classify("parse CSV data", ["csv_parser.py"], op_history=history)
        assert result.auto_approve_eligible is False


class TestFastPathEligibility:
    def test_trivial_is_fast_path(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix typo", ["readme.md"])
        assert result.fast_path_eligible is True

    def test_simple_is_fast_path(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix bug", ["handler.py"])
        assert result.fast_path_eligible is True

    def test_moderate_not_fast_path(self):
        c = OperationComplexityClassifier()
        result = c.classify("refactor", ["a.py", "b.py", "c.py"])
        assert result.fast_path_eligible is False


class TestClassificationResult:
    def test_frozen(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix typo", ["readme.md"])
        with pytest.raises(AttributeError):
            result.complexity = ComplexityClass.COMPLEX

    def test_rationale_contains_signals(self):
        c = OperationComplexityClassifier()
        result = c.classify("fix typo in docstring", ["parser.py"])
        assert "typo" in result.rationale.lower() or "docstring" in result.rationale.lower()
