"""Tests for ResourceGovernor — CPU/memory preemption guard.

T21 is the go/no-go test: test_yields_on_high_cpu.
All tests mock psutil to avoid real system calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vmem(percent: float) -> MagicMock:
    vm = MagicMock()
    vm.percent = percent
    return vm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yields_on_high_cpu():
    """T21 (go/no-go): CPU above threshold → should_yield() returns True."""
    from backend.core.ouroboros.governance.autonomy.resource_governor import ResourceGovernor

    gov = ResourceGovernor(preempt_on_cpu_above=80.0, preempt_on_memory_above=85.0)

    with patch("psutil.cpu_percent", return_value=85.0), \
         patch("psutil.virtual_memory", return_value=_make_vmem(50.0)):
        result = await gov.should_yield()

    assert result is True


@pytest.mark.asyncio
async def test_yields_on_high_memory():
    """Memory above threshold → should_yield() returns True."""
    from backend.core.ouroboros.governance.autonomy.resource_governor import ResourceGovernor

    gov = ResourceGovernor(preempt_on_cpu_above=80.0, preempt_on_memory_above=85.0)

    with patch("psutil.cpu_percent", return_value=30.0), \
         patch("psutil.virtual_memory", return_value=_make_vmem(90.0)):
        result = await gov.should_yield()

    assert result is True


@pytest.mark.asyncio
async def test_does_not_yield_when_normal():
    """CPU and memory both below thresholds → should_yield() returns False."""
    from backend.core.ouroboros.governance.autonomy.resource_governor import ResourceGovernor

    gov = ResourceGovernor(preempt_on_cpu_above=80.0, preempt_on_memory_above=85.0)

    with patch("psutil.cpu_percent", return_value=30.0), \
         patch("psutil.virtual_memory", return_value=_make_vmem(50.0)):
        result = await gov.should_yield()

    assert result is False


@pytest.mark.asyncio
async def test_handles_psutil_error():
    """psutil raising RuntimeError → fail-open, should_yield() returns False."""
    from backend.core.ouroboros.governance.autonomy.resource_governor import ResourceGovernor

    gov = ResourceGovernor()

    with patch("psutil.cpu_percent", side_effect=RuntimeError("sensor unavailable")):
        result = await gov.should_yield()

    assert result is False
