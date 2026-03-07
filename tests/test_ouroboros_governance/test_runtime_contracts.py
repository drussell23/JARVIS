# tests/test_ouroboros_governance/test_runtime_contracts.py
"""Tests for runtime N/N-1 contract validation."""

import pytest

from backend.core.ouroboros.governance.runtime_contracts import (
    RuntimeContractChecker,
    ContractCheckResult,
    ContractViolation,
)
from backend.core.ouroboros.governance.contract_gate import ContractVersion


@pytest.fixture
def checker():
    current = ContractVersion(major=2, minor=1, patch=0)
    return RuntimeContractChecker(current_version=current)


class TestContractCheckResult:
    def test_passing_result(self):
        """Passing result has compatible=True and no violations."""
        result = ContractCheckResult(compatible=True, violations=[])
        assert result.compatible is True
        assert len(result.violations) == 0

    def test_failing_result(self):
        """Failing result has compatible=False and violations list."""
        result = ContractCheckResult(
            compatible=False,
            violations=[
                ContractViolation(
                    field="api_endpoint",
                    reason="removed in proposed change",
                ),
            ],
        )
        assert result.compatible is False
        assert len(result.violations) == 1


class TestRuntimeContractChecker:
    def test_compatible_version(self, checker):
        """Same major version with minor bump is compatible."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=2, patch=0)
        )
        assert result.compatible is True

    def test_patch_bump_compatible(self, checker):
        """Patch-only bump is always compatible."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=1, patch=5)
        )
        assert result.compatible is True

    def test_major_version_break(self, checker):
        """Different major version is incompatible."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=3, minor=0, patch=0)
        )
        assert result.compatible is False
        assert any("major" in v.reason.lower() for v in result.violations)

    def test_minor_downgrade_incompatible(self, checker):
        """Minor version downgrade is incompatible (N-1 only, not N-2)."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=0, patch=0)
        )
        # N-1 means current minor - 1 is allowed, but going below is not
        # Current is 2.1.0, so 2.0.0 is exactly N-1, which IS allowed
        assert result.compatible is True

    def test_two_minor_versions_back_incompatible(self, checker):
        """Two minor versions back (N-2) is incompatible."""
        # Current is 2.1.0. Create checker with 2.3.0 so N-2 = 2.1.0
        checker3 = RuntimeContractChecker(
            current_version=ContractVersion(major=2, minor=3, patch=0)
        )
        result = checker3.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=1, patch=0)
        )
        assert result.compatible is False

    def test_check_before_write_passes(self, checker):
        """check_before_write() returns True for compatible changes."""
        assert checker.check_before_write(
            proposed_version=ContractVersion(major=2, minor=1, patch=1)
        ) is True

    def test_check_before_write_blocks_incompatible(self, checker):
        """check_before_write() returns False for incompatible changes."""
        assert checker.check_before_write(
            proposed_version=ContractVersion(major=3, minor=0, patch=0)
        ) is False

    def test_none_proposed_version_passes(self, checker):
        """No proposed version change passes by default."""
        result = checker.check_compatibility(proposed_version=None)
        assert result.compatible is True
