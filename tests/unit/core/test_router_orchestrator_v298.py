# tests/unit/core/test_router_orchestrator_v298.py
"""v298.0: prime_router + startup_orchestrator lifecycle integration."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def test_prime_router_has_set_lifecycle_manager():
    """PrimeRouter must expose set_lifecycle_manager()."""
    from backend.core.prime_router import PrimeRouter
    router = PrimeRouter.__new__(PrimeRouter)
    assert hasattr(router, "set_lifecycle_manager")


def test_startup_orchestrator_has_set_lifecycle_manager():
    """StartupOrchestrator must expose set_lifecycle_manager()."""
    from backend.core.startup_orchestrator import StartupOrchestrator
    orch = StartupOrchestrator.__new__(StartupOrchestrator)
    assert hasattr(orch, "set_lifecycle_manager")


def test_startup_orchestrator_has_boot_mode_record_property():
    """StartupOrchestrator must expose boot_mode_record property."""
    from backend.core.startup_orchestrator import StartupOrchestrator
    orch = StartupOrchestrator.__new__(StartupOrchestrator)
    assert hasattr(StartupOrchestrator, "boot_mode_record")
