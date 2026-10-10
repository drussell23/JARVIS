# [Ouroboros] Modified by Ouroboros (op=op-01a122fe-) at 2026-10-09 23:28 UTC
# Reason: `backend/contracts/manifest_schema.py` has no corresponding test module. CREATE `tests/test_manifest_schema.py` containi

import pytest
from pathlib import Path
import sys

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent / ".." / "backend"))


class TestProviderManifest:
    """Tests for ProviderManifest schema parsing and behavior."""

    def test_import_smoke(self):
        """Test that ProviderManifest can be imported without errors."""
        from backend.contracts.manifest_schema import ProviderManifest
        assert ProviderManifest is not None

    def test_from_dict_with_complete_data(self):
        """Test from_dict with complete data including all fields."""
        from backend.contracts.manifest_schema import ProviderManifest

        data = {
            "provider_id": "test-provider",
            "capabilities": ["chat", "embed"],
            "contract_version": [1, 0, 0],
            "policy_hash": "abc123",
            "timestamp": 1634567890.0,
        }
        manifest = ProviderManifest.from_dict(data)
        assert manifest.provider_id == "test-provider"
        assert manifest.capabilities == frozenset(["chat", "embed"])
        assert manifest.contract_version == (1, 0, 0)
        assert manifest.policy_hash == "abc123"
        assert manifest.timestamp == 1634567890.0

    def test_from_dict_with_missing_optional_fields(self):
        """Test from_dict with missing optional fields defaults."""
        from backend.contracts.manifest_schema import ProviderManifest

        data = {
            "provider_id": "test-provider",
            "capabilities": [],
        }
        manifest = ProviderManifest.from_dict(data)
        assert manifest.provider_id == "test-provider"
        assert manifest.capabilities == frozenset([])
        assert manifest.contract_version == (0, 0, 0)
        assert manifest.policy_hash == ""
        assert manifest.timestamp == 0.0

    def test_supports_method(self):
        """Test the supports method for capability checking."""
        from backend.contracts.manifest_schema import ProviderManifest

        manifest = ProviderManifest(
            provider_id="test-provider",
            capabilities=frozenset(["chat", "embed"]),
            contract_version=(1, 0, 0),
            policy_hash="abc123",
            timestamp=1634567890.0,
        )
        assert manifest.supports("chat") is True
        assert manifest.supports("embed") is True
        assert manifest.supports("generate") is False

    def test_to_dict_method(self):
        """Test the to_dict method for serialization."""
        from backend.contracts.manifest_schema import ProviderManifest

        manifest = ProviderManifest(
            provider_id="test-provider",
            capabilities=frozenset(["chat", "embed"]),
            contract_version=(1, 0, 0),
            policy_hash="abc123",
            timestamp=1634567890.0,
        )
        data = manifest.to_dict()
        assert data["provider_id"] == "test-provider"
        assert set(data["capabilities"]) == {"chat", "embed"}
        assert data["contract_version"] == [1, 0, 0]
        assert data["policy_hash"] == "abc123"
        assert data["timestamp"] == 1634567890.0

    def test_to_dict_sorted_capabilities(self):
        """Test that capabilities are sorted in to_dict output."""
        from backend.contracts.manifest_schema import ProviderManifest

        manifest = ProviderManifest(
            provider_id="test-provider",
            capabilities=frozenset(["embed", "chat"]),
            contract_version=(1, 0, 0),
            policy_hash="abc123",
            timestamp=1634567890.0,
        )
        data = manifest.to_dict()
        assert data["capabilities"] == ["chat", "embed"]  # Should be sorted
