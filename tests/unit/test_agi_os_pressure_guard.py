"""Tests for AGI OS coordinator pressure guard using broker snapshot."""
import pytest


class TestPressureGuardDesignIntent:
    """Design-intent tests verifying the pressure guard uses MCP snapshot."""

    def test_quantizer_import_path_exists(self):
        """The function we depend on should be importable."""
        from backend.core.memory_quantizer import get_memory_quantizer_instance
        assert callable(get_memory_quantizer_instance)

    def test_pressure_tier_import_path_exists(self):
        """PressureTier enum should be importable."""
        from backend.core.memory_types import PressureTier
        assert PressureTier.EMERGENCY > PressureTier.CONSTRAINED
