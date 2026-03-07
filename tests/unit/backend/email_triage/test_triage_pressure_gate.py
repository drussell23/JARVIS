"""Tests for memory pressure routing gate in _maybe_run_email_triage.

Under memory pressure, PrimeRouter routes inference to GCP_PRIME.
Extraction stays ENABLED so the router gets called.  Concurrency
and timeout are bumped to handle GCP latency.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


def _build_runtime():
    """Build a minimal UnifiedAgentRuntime for testing _maybe_run_email_triage."""
    import asyncio
    from autonomy.agent_runtime import UnifiedAgentRuntime
    rt = UnifiedAgentRuntime.__new__(UnifiedAgentRuntime)
    rt._last_email_triage_run = 0.0
    rt._triage_disabled_logged = False
    rt._triage_pressure_skip_count = 0
    rt._experience_processor = None
    rt._experience_processor_started = False
    rt._triage_lock = asyncio.Lock()
    return rt


@pytest.mark.asyncio
async def test_triage_routes_to_gcp_when_thrashing():
    """Under thrashing, concurrency/timeout bump but extraction stays on."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "thrashing"

    mock_runner = MagicMock()
    mock_runner.is_warmed_up = True
    mock_runner_cls = MagicMock()
    mock_runner_cls.get_instance.return_value = mock_runner

    captured_env = {}

    original_get_instance = mock_runner_cls.get_instance
    def capture_env(*a, **kw):
        captured_env["concurrency"] = os.environ.get("EMAIL_TRIAGE_EXTRACTION_CONCURRENCY")
        captured_env["timeout"] = os.environ.get("EMAIL_TRIAGE_CYCLE_TIMEOUT_S")
        captured_env["extraction"] = os.environ.get("EMAIL_TRIAGE_EXTRACTION_ENABLED")
        return original_get_instance(*a, **kw)
    mock_runner_cls.get_instance.side_effect = capture_env

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}, clear=False):
        os.environ.pop("EMAIL_TRIAGE_EXTRACTION_CONCURRENCY", None)
        os.environ.pop("EMAIL_TRIAGE_CYCLE_TIMEOUT_S", None)
        os.environ.pop("EMAIL_TRIAGE_EXTRACTION_ENABLED", None)
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner",
                mock_runner_cls,
            ):
                await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 1
    # Extraction NOT disabled (must stay on for PrimeRouter to be called)
    assert captured_env.get("extraction") is None
    # Concurrency bumped, timeout extended
    assert captured_env.get("concurrency") == "5"
    assert captured_env.get("timeout") == "120.0"
    mock_runner_cls.get_instance.assert_called()


@pytest.mark.asyncio
async def test_triage_routes_to_gcp_when_emergency():
    """Under emergency, same GCP routing behavior as thrashing."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    mock_runner = MagicMock()
    mock_runner.is_warmed_up = True
    mock_runner_cls = MagicMock()
    mock_runner_cls.get_instance.return_value = mock_runner

    captured_env = {}

    original_get_instance = mock_runner_cls.get_instance
    def capture_env(*a, **kw):
        captured_env["concurrency"] = os.environ.get("EMAIL_TRIAGE_EXTRACTION_CONCURRENCY")
        captured_env["timeout"] = os.environ.get("EMAIL_TRIAGE_CYCLE_TIMEOUT_S")
        return original_get_instance(*a, **kw)
    mock_runner_cls.get_instance.side_effect = capture_env

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}, clear=False):
        os.environ.pop("EMAIL_TRIAGE_EXTRACTION_CONCURRENCY", None)
        os.environ.pop("EMAIL_TRIAGE_CYCLE_TIMEOUT_S", None)
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner",
                mock_runner_cls,
            ):
                await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 1
    assert captured_env.get("concurrency") == "5"
    assert captured_env.get("timeout") == "120.0"


@pytest.mark.asyncio
async def test_triage_proceeds_when_healthy():
    """Triage should proceed normally when memory is healthy."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "healthy"

    mock_runner = MagicMock()
    mock_runner.is_warmed_up = True
    mock_runner_cls = MagicMock()
    mock_runner_cls.get_instance.return_value = mock_runner

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}, clear=False):
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
    mock_runner_cls.get_instance.assert_called()


@pytest.mark.asyncio
async def test_defaults_restored_after_pressure_resolves():
    """When memory returns to healthy, concurrency/timeout env vars are removed."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 3

    mock_mq = MagicMock()
    mock_mq.thrash_state = "healthy"

    mock_runner = MagicMock()
    mock_runner.is_warmed_up = True
    mock_runner_cls = MagicMock()
    mock_runner_cls.get_instance.return_value = mock_runner

    captured_env = {}

    original_get_instance = mock_runner_cls.get_instance
    def capture_env(*a, **kw):
        captured_env["concurrency"] = os.environ.get("EMAIL_TRIAGE_EXTRACTION_CONCURRENCY")
        captured_env["timeout"] = os.environ.get("EMAIL_TRIAGE_CYCLE_TIMEOUT_S")
        return original_get_instance(*a, **kw)
    mock_runner_cls.get_instance.side_effect = capture_env

    with patch.dict(os.environ, {
        "EMAIL_TRIAGE_ENABLED": "true",
        "EMAIL_TRIAGE_EXTRACTION_CONCURRENCY": "5",
        "EMAIL_TRIAGE_CYCLE_TIMEOUT_S": "120.0",
    }, clear=False):
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
    # Env vars removed (fall back to config defaults)
    assert captured_env.get("concurrency") is None
    assert captured_env.get("timeout") is None


@pytest.mark.asyncio
async def test_consecutive_pressure_increments_count():
    """Each consecutive pressure cycle increments the skip count."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 3

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}, clear=False):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 4


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

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}, clear=False):
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
