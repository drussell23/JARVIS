"""Tests for memory pressure admission gate in _maybe_run_email_triage."""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


def _build_runtime():
    """Build a minimal UnifiedAgentRuntime for testing _maybe_run_email_triage."""
    from autonomy.agent_runtime import UnifiedAgentRuntime
    rt = UnifiedAgentRuntime.__new__(UnifiedAgentRuntime)
    rt._last_email_triage_run = 0.0
    rt._triage_disabled_logged = False
    rt._triage_pressure_skip_count = 0
    return rt


@pytest.mark.asyncio
async def test_triage_skipped_when_thrashing():
    """Triage must not launch when memory quantizer reports thrashing."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "thrashing"

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 1


@pytest.mark.asyncio
async def test_triage_skipped_when_emergency():
    """Triage must not launch when memory quantizer reports emergency."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 1


@pytest.mark.asyncio
async def test_triage_proceeds_when_healthy():
    """Triage should proceed normally when memory is healthy (gate passes through)."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "healthy"

    # Patch the runner import to confirm we got past the gate.
    # If the gate blocked, EmailTriageRunner.get_instance would NOT be called.
    mock_runner = MagicMock()
    mock_runner.is_warmed_up = True
    mock_runner_cls = MagicMock()
    mock_runner_cls.get_instance.return_value = mock_runner

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner",
                mock_runner_cls,
            ):
                # Will still fail downstream (lock manager etc), but
                # the key assertion is skip_count stays 0
                await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 0
    # Confirm the runner was actually reached (gate didn't block)
    mock_runner_cls.get_instance.assert_called()


@pytest.mark.asyncio
async def test_consecutive_skips_increase_backoff():
    """Each consecutive skip should increase the backoff interval."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 3

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    before_run = rt._last_email_triage_run

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 4
    assert rt._last_email_triage_run != before_run


@pytest.mark.asyncio
async def test_drift_guard_disables_extraction_after_5_skips():
    """After 5 consecutive pressure blocks, extraction should be auto-disabled."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 4

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    extraction_flag = {}

    def capture_setdefault(key, value):
        if key == "EMAIL_TRIAGE_EXTRACTION_ENABLED":
            extraction_flag["value"] = value

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            # Remove if present so setdefault actually sets it
            os.environ.pop("EMAIL_TRIAGE_EXTRACTION_ENABLED", None)
            await rt._maybe_run_email_triage()
            # Check inside the patch.dict context
            assert os.environ.get("EMAIL_TRIAGE_EXTRACTION_ENABLED") == "false"

    assert rt._triage_pressure_skip_count == 5


@pytest.mark.asyncio
async def test_skip_count_resets_on_healthy():
    """Skip count should reset to 0 when memory returns to healthy."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 3

    mock_mq = MagicMock()
    mock_mq.thrash_state = "healthy"

    mock_runner = MagicMock()
    mock_runner.is_warmed_up = True
    mock_runner_cls = MagicMock()
    mock_runner_cls.get_instance.return_value = mock_runner

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner",
                mock_runner_cls,
            ):
                await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 0
