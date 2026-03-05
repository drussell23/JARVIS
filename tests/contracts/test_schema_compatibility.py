"""
Schema compatibility tests — validate contract structures locally.
"""
import pytest
from backend.contracts.manifest_schema import ProviderManifest


class TestProviderManifest:
    """Enforce: manifest serialization roundtrips correctly."""

    def test_roundtrip(self):
        manifest = ProviderManifest(
            provider_id="jprime",
            capabilities=frozenset(["vision", "chat", "multimodal"]),
            contract_version=(0, 3, 0),
            policy_hash="abcdef01",
            timestamp=1000.0,
        )
        data = manifest.to_dict()
        restored = ProviderManifest.from_dict(data)
        assert restored.provider_id == manifest.provider_id
        assert restored.capabilities == manifest.capabilities
        assert restored.contract_version == manifest.contract_version

    def test_supports_capability(self):
        manifest = ProviderManifest(
            provider_id="jprime",
            capabilities=frozenset(["vision", "chat"]),
            contract_version=(0, 3, 0),
            policy_hash="abc",
            timestamp=0,
        )
        assert manifest.supports("vision")
        assert manifest.supports("chat")
        assert not manifest.supports("embedding")

    def test_empty_capabilities(self):
        manifest = ProviderManifest(
            provider_id="empty",
            capabilities=frozenset(),
            contract_version=(0, 1, 0),
            policy_hash="",
            timestamp=0,
        )
        assert not manifest.supports("vision")
