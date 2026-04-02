"""CUExecutionSensor spine tests — envelope flows sensor → router."""
import pytest

from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP


def test_cu_execution_has_explicit_priority():
    """cu_execution must have an explicit priority, not fallback 99."""
    assert "cu_execution" in _PRIORITY_MAP
    assert _PRIORITY_MAP["cu_execution"] == 5
