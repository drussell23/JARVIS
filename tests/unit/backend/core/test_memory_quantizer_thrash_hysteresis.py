"""Tests for thrash_state property and hysteresis exit thresholds."""

import asyncio
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


@pytest.fixture
def quantizer():
    """Build a MemoryQuantizer with monitoring disabled."""
    with patch("core.memory_quantizer.psutil"):
        from core.memory_quantizer import MemoryQuantizer
        mq = MemoryQuantizer.__new__(MemoryQuantizer)
        # Minimal init for testing _check_thrash_state
        mq._thrash_state = "healthy"
        mq._thrash_callbacks = []
        mq._recovery_callbacks = []
        mq._thrash_warning_since = 0.0
        mq._thrash_emergency_since = 0.0
        mq._thrash_recovery_since = 0.0
        mq._pagein_rate = 0.0
        mq._pagein_rate_ema = 0.0
        mq.current_metrics = None
        return mq


def test_thrash_state_property_returns_current_state(quantizer):
    """thrash_state property exposes the internal _thrash_state."""
    assert quantizer.thrash_state == "healthy"
    quantizer._thrash_state = "emergency"
    assert quantizer.thrash_state == "emergency"


@pytest.mark.asyncio
async def test_emergency_holds_until_exit_threshold(quantizer):
    """Emergency state should NOT drop to thrashing when rate is above exit threshold."""
    import time
    quantizer._thrash_state = "emergency"
    # Rate below emergency entry (2000) but above exit (2000 * 0.7 = 1400)
    quantizer._pagein_rate = 1600.0
    quantizer._pagein_rate_ema = 1600.0
    await quantizer._check_thrash_state()
    # Should HOLD emergency, not drop to thrashing
    assert quantizer.thrash_state == "emergency"


@pytest.mark.asyncio
async def test_emergency_drops_to_thrashing_below_exit_threshold(quantizer):
    """Emergency state drops to thrashing when rate falls below exit threshold."""
    import time
    quantizer._thrash_state = "emergency"
    # Rate below exit threshold (1400) but above healthy (100)
    quantizer._pagein_rate = 300.0
    quantizer._pagein_rate_ema = 300.0
    await quantizer._check_thrash_state()
    assert quantizer.thrash_state == "thrashing"
