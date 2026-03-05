#!/usr/bin/env python3
"""
Governance compliance tests for enterprise organ classes.

Run: python3 -m pytest tests/unit/backend/test_enterprise_organ_governance.py -v
"""
import asyncio
import sys
import threading
from pathlib import Path
from typing import Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestMLOpsModelRegistryGovernance:
    """MLOpsModelRegistry must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import MLOpsModelRegistry, SystemService
        assert issubclass(MLOpsModelRegistry, SystemService)

    def test_constructor_purity(self):
        """__init__ must not perform I/O."""
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        assert hasattr(registry, '_initialized')
        assert registry._initialized is False

    def test_capability_contract_valid(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        contract = registry.capability_contract()
        assert contract.name == "MLOpsModelRegistry"
        assert contract.version != "0.0.0"
        assert "writes_model_registry" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        triggers = registry.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        healthy, msg = await registry.health_check()
        assert healthy is False
        assert "not initialized" in msg

    @pytest.mark.asyncio
    async def test_lifecycle_initialize_cleanup(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        await registry.initialize()
        assert registry._initialized is True
        healthy, msg = await registry.health_check()
        assert healthy is True
        await registry.cleanup()
