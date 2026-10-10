# [Ouroboros] Modified by Ouroboros (op=op-01a122f0-) at 2026-10-09 23:13 UTC
# Reason: `backend/contracts/capability_taxonomy.py` has no corresponding test module. CREATE `tests/test_capability_taxonomy.py` 

import pytest


class TestCapabilityTaxonomy:
    """Tests for capability taxonomy module."""

    def test_import_smoke(self):
        """Importing the module should work without errors."""
        from backend.contracts.capability_taxonomy import (
            Capability,
            is_valid_capability,
            get_active_capabilities,
        )
        # Verify we can access the expected symbols
        assert Capability
        assert is_valid_capability
        assert get_active_capabilities

    def test_is_valid_capability_valid(self):
        """is_valid_capability returns True for valid capability IDs."""
        from backend.contracts.capability_taxonomy import is_valid_capability
        
        # Test all known capabilities
        valid_caps = [
            "chat", "reasoning", "code", "tool_use", "embedding",
            "vision", "multimodal", "screen_analysis", "vision_analyze_heavy",
            "object_detection", "ui_detection", "voice_activation",
            "wake_word_detection", "similarity_search", "semantic_search"
        ]
        
        for cap in valid_caps:
            assert is_valid_capability(cap) is True

    def test_is_valid_capability_invalid(self):
        """is_valid_capability returns False for invalid capability IDs."""
        from backend.contracts.capability_taxonomy import is_valid_capability
        
        # Test some invalid capabilities
        invalid_caps = ["nonexistent", "", "chat123", "_invalid", "invalid-cap"]
        
        for cap in invalid_caps:
            assert is_valid_capability(cap) is False

    def test_get_active_capabilities_returns_dict(self):
        """get_active_capabilities returns a dictionary of active capabilities."""
        from backend.contracts.capability_taxonomy import get_active_capabilities
        
        caps = get_active_capabilities()
        assert isinstance(caps, dict)
        assert len(caps) > 0

    def test_get_active_capabilities_excludes_deprecated(self):
        """get_active_capabilities excludes deprecated capabilities."""
        from backend.contracts.capability_taxonomy import get_active_capabilities, Capability
        
        # Create a mock deprecated capability to verify it's excluded
        caps = get_active_capabilities()
        
        # All returned capabilities should be non-deprecated
        for cap in caps.values():
            assert cap.deprecated is False

    def test_capability_dataclass_structure(self):
        """Capability dataclass has expected structure."""
        from backend.contracts.capability_taxonomy import Capability
        
        # Test basic capability creation
        cap = Capability(id="test")
        assert cap.id == "test"
        assert cap.deprecated is False
        assert cap.since_version == "0.1.0"
        
        # Test with explicit deprecated flag
        cap_deprecated = Capability(id="test_deprecated", deprecated=True)
        assert cap_deprecated.deprecated is True
        
        # Test with deprecated_by field
        cap_with_deprecated_by = Capability(
            id="test_with_deprecated_by",
            deprecated=True,
            deprecated_by="new_capability"
        )
        assert cap_with_deprecated_by.deprecated_by == "new_capability"
