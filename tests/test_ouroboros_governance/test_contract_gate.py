"""Tests for the Ouroboros Contract Gate.

The contract gate enforces N/N-1 schema version compatibility across
JARVIS/Prime/Reactor at boot and before cross-repo operations.
No LLM calls.  Pure deterministic version checking.
"""

import pytest

from backend.core.ouroboros.governance.contract_gate import (
    REQUIRED_SERVICES,
    BootCheckResult,
    CompatibilityResult,
    ContractGate,
    ContractVersion,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gate() -> ContractGate:
    """Return a default ContractGate."""
    return ContractGate()


# ---------------------------------------------------------------------------
# TestVersionCompatibility
# ---------------------------------------------------------------------------


class TestVersionCompatibility:
    """Pairwise version compatibility rules."""

    def test_same_version_compatible(self, gate: ContractGate) -> None:
        local = ContractVersion(major=2, minor=1, patch=0)
        remote = ContractVersion(major=2, minor=1, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is True

    def test_n_minus_1_minor_compatible(self, gate: ContractGate) -> None:
        local = ContractVersion(major=2, minor=1, patch=0)
        remote = ContractVersion(major=2, minor=0, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is True

    def test_n_minus_2_minor_incompatible(self, gate: ContractGate) -> None:
        local = ContractVersion(major=2, minor=2, patch=0)
        remote = ContractVersion(major=2, minor=0, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is False
        assert "minor" in result.reason

    def test_major_mismatch_incompatible(self, gate: ContractGate) -> None:
        local = ContractVersion(major=2, minor=0, patch=0)
        remote = ContractVersion(major=3, minor=0, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is False
        assert "major" in result.reason

    def test_patch_difference_always_compatible(self, gate: ContractGate) -> None:
        local = ContractVersion(major=2, minor=0, patch=0)
        remote = ContractVersion(major=2, minor=0, patch=99)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is True


# ---------------------------------------------------------------------------
# TestBootGate
# ---------------------------------------------------------------------------


class TestBootGate:
    """Boot-time compatibility gate across all required services."""

    @pytest.mark.asyncio
    async def test_boot_check_all_compatible(self, gate: ContractGate) -> None:
        versions = {
            "jarvis": ContractVersion(major=2, minor=1, patch=0),
            "prime": ContractVersion(major=2, minor=1, patch=0),
            "reactor": ContractVersion(major=2, minor=0, patch=3),
        }
        result = await gate.boot_check(versions)
        assert result.autonomy_allowed is True
        assert result.interactive_allowed is True

    @pytest.mark.asyncio
    async def test_boot_check_one_incompatible(self, gate: ContractGate) -> None:
        versions = {
            "jarvis": ContractVersion(major=2, minor=1, patch=0),
            "prime": ContractVersion(major=3, minor=0, patch=0),
            "reactor": ContractVersion(major=2, minor=0, patch=3),
        }
        result = await gate.boot_check(versions)
        assert result.autonomy_allowed is False
        assert result.interactive_allowed is True
        assert "prime" in result.details

    @pytest.mark.asyncio
    async def test_boot_check_missing_service(self, gate: ContractGate) -> None:
        versions = {
            "jarvis": ContractVersion(major=2, minor=1, patch=0),
        }
        result = await gate.boot_check(versions)
        assert result.autonomy_allowed is False
        assert result.interactive_allowed is True
