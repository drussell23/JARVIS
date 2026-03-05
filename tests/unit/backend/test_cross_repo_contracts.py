#!/usr/bin/env python3
"""Tests for cross-repo contract validation."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestHealthContractV1:
    """Tests for HealthContractV1 schema parsing."""

    def test_parse_versioned_response(self):
        from core.cross_repo_contracts import HealthContractV1

        data = {
            "contract_version": 1,
            "status": "healthy",
            "model_loaded": True,
            "ready_for_inference": True,
            "trinity_connected": True,
        }
        contract = HealthContractV1.from_response(data)
        assert contract.status == "healthy"
        assert contract.model_loaded is True
        assert contract.contract_version == 1

    def test_parse_legacy_unversioned_response(self):
        from core.cross_repo_contracts import HealthContractV1

        data = {
            "status": "ok",
            "model_loaded": True,
            "ready_for_inference": False,
        }
        contract = HealthContractV1.from_response(data)
        assert contract.contract_version == 0
        assert contract.model_loaded is True
        assert contract.ready_for_inference is False

    def test_unsupported_version_raises(self):
        from core.cross_repo_contracts import HealthContractV1, UnsupportedContractVersion

        data = {"contract_version": 99, "status": "ok"}
        with pytest.raises(UnsupportedContractVersion):
            HealthContractV1.from_response(data)


class TestErrorHierarchy:
    """Tests for typed cross-repo error classification."""

    def test_repo_not_found_is_cross_repo_error(self):
        from core.cross_repo_contracts import RepoNotFoundError, CrossRepoError

        assert issubclass(RepoNotFoundError, CrossRepoError)

    def test_error_types_are_distinct(self):
        from core.cross_repo_contracts import (
            RepoNotFoundError, RepoImportError,
            RepoUnreachableError, RepoContractError,
        )

        errors = [RepoNotFoundError, RepoImportError, RepoUnreachableError, RepoContractError]
        for i, e1 in enumerate(errors):
            for j, e2 in enumerate(errors):
                if i != j:
                    assert not issubclass(e1, e2), f"{e1.__name__} should not be subclass of {e2.__name__}"
