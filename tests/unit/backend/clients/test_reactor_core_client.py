# tests/unit/backend/clients/test_reactor_core_client.py
"""Tests for ReactorCoreClient endpoint paths.

Verifies that every _request() call uses the correct Reactor-Core API path.
asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio required.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.clients.reactor_core_client import (
    ReactorCoreClient,
    ReactorCoreConfig,
    TrainingPriority,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def client() -> ReactorCoreClient:
    """Create a ReactorCoreClient wired for unit testing.

    Bypasses initialize() -- sets internal state directly so that methods
    don't short-circuit with 'offline' / 'no session' guards.
    """
    c = ReactorCoreClient(ReactorCoreConfig())
    c._is_online = True
    c._session = AsyncMock()  # fake session, won't be used directly
    c._requests_made = 0
    c._requests_failed = 0
    c._last_trigger_time = None
    c._training_triggers = 0
    return c


# =========================================================================
# Tests -- The 3 known-wrong paths
# =========================================================================

class TestEndpointPaths:
    """Verify the exact path argument passed to _request()."""

    async def test_stream_experience_uses_v1_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"accepted": True})

        await client.stream_experience({"query": "hello", "response": "world"})

        client._request.assert_called_once()
        call_args = client._request.call_args
        # positional args: (method, path, ...)
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/api/v1/experiences/stream"

    async def test_trigger_training_uses_v1_train_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"job_id": "job-123", "status": "queued"})
        client._emit_event = AsyncMock()
        client._write_bridge_event = AsyncMock()

        await client.trigger_training(
            experience_count=100,
            priority=TrainingPriority.NORMAL,
            force=True,
        )

        client._request.assert_called_once()
        call_args = client._request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/api/v1/train"

    async def test_get_experience_count_uses_v1_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"count": 42})

        result = await client.get_experience_count()

        client._request.assert_called_once()
        call_args = client._request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == "/api/v1/experiences/count"
        assert result == 42


# =========================================================================
# Tests -- Other _request() paths (audit)
# =========================================================================

class TestOtherEndpointPaths:
    """Audit remaining _request() calls for correct paths."""

    async def test_get_status_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"status": "ok"})

        await client.get_status()

        call_args = client._request.call_args
        _method, path = call_args[0][0], call_args[0][1]
        # /api/status does not match any known Reactor-Core route;
        # This test documents the current path (no known correct route).
        assert path == "/api/status"

    async def test_get_pipeline_state_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"stage": "idle"})

        await client.get_pipeline_state()

        call_args = client._request.call_args
        _method, path = call_args[0][0], call_args[0][1]
        assert path == "/api/v1/pipeline/state"

    async def test_cancel_training_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"cancelled": True})

        await client.cancel_training("job-abc")

        call_args = client._request.call_args
        method, path = call_args[0][0], call_args[0][1]
        assert method == "POST"
        assert path == "/api/v1/training/cancel/job-abc"

    async def test_get_training_job_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={
            "job_id": "job-abc",
            "status": "running",
            "stage": "training",
            "created_at": "2025-01-01T00:00:00",
        })

        await client.get_training_job("job-abc")

        call_args = client._request.call_args
        method, path = call_args[0][0], call_args[0][1]
        assert method == "GET"
        assert path == "/api/v1/training/job/job-abc"

    async def test_get_training_history_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value=[])

        await client.get_training_history(limit=5)

        call_args = client._request.call_args
        method, path = call_args[0][0], call_args[0][1]
        assert method == "GET"
        assert path == "/api/v1/training/history"

    async def test_add_learning_topic_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"added": True})

        await client.add_learning_topic("reinforcement learning")

        call_args = client._request.call_args
        method, path = call_args[0][0], call_args[0][1]
        assert method == "POST"
        # /api/scout/topics has no known Reactor-Core route;
        # This test documents the current path.
        assert path == "/api/scout/topics"


# =========================================================================
# Helpers -- mock response builder for health-check tests
# =========================================================================

def _make_health_response(status: int, json_data: dict = None, json_error: Exception = None):
    """Build a mock aiohttp response that works with ``async with session.get(...) as r:``."""
    resp = AsyncMock()
    resp.status = status
    if json_error is not None:
        resp.json = AsyncMock(side_effect=json_error)
    elif json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    else:
        resp.json = AsyncMock(return_value={})
    # Make it usable as ``async with session.get(...) as resp:``
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _wire_session_get(session: MagicMock, response) -> None:
    """Wire ``session.get(...)`` so it returns *response* as a sync context-manager result.

    ``health_check()`` does ``async with self._session.get(url, timeout=...) as r:``.
    ``session.get(...)`` is a plain call (NOT awaited) that must return an async
    context manager.  Using ``MagicMock(return_value=response)`` ensures the call
    returns *response* synchronously; the response already has ``__aenter__`` /
    ``__aexit__`` configured.
    """
    session.get = MagicMock(return_value=response)


@pytest.fixture
def mock_session() -> MagicMock:
    """Standalone mock aiohttp session for health-check tests."""
    return MagicMock()


# =========================================================================
# Tests -- Health Check Enrichment (v2.1)
# =========================================================================

class TestHealthCheckEnrichment:
    """Verify health check parses training readiness from response body."""

    async def test_health_check_parses_training_ready(self, client, mock_session):
        """Health check should store training_ready from response JSON."""
        response = _make_health_response(200, json_data={
            "status": "healthy",
            "phase": "ready",
            "training_ready": True,
            "trinity_connected": True,
        })
        _wire_session_get(mock_session, response)
        client._session = mock_session

        result = await client.health_check()
        assert result is True
        assert client._training_ready is True
        assert client._reactor_phase == "ready"
        assert client._trinity_connected is True

    async def test_health_check_stores_phase_on_not_ready(self, client, mock_session):
        """Health check should store phase even when training not ready."""
        response = _make_health_response(200, json_data={
            "status": "starting",
            "phase": "initializing",
            "training_ready": False,
            "trinity_connected": False,
        })
        _wire_session_get(mock_session, response)
        client._session = mock_session

        result = await client.health_check()
        assert result is True  # HTTP 200 = healthy regardless of training_ready
        assert client._training_ready is False
        assert client._reactor_phase == "initializing"

    async def test_is_training_ready_property(self, client):
        """is_training_ready should require both online and training_ready."""
        client._training_ready = True
        client._is_online = True
        assert client.is_training_ready is True

        client._is_online = False
        assert client.is_training_ready is False

        client._is_online = True
        client._training_ready = False
        assert client.is_training_ready is False

    async def test_health_check_survives_json_parse_failure(self, client, mock_session):
        """Health check should not fail if response JSON is unparseable."""
        response = _make_health_response(200, json_error=Exception("Invalid JSON"))
        _wire_session_get(mock_session, response)
        client._session = mock_session

        result = await client.health_check()
        assert result is True  # Should still succeed (HTTP 200)
        assert client._training_ready is False  # Unchanged from default


# =========================================================================
# Tests -- Health Monitor Auto-Trigger (v2.1)
# =========================================================================

class TestHealthMonitorAutoTrigger:
    """Verify health monitor checks experience count and triggers training."""

    async def test_monitor_triggers_training_when_threshold_met(self, client):
        client._training_ready = True
        client._is_online = True
        client.config.auto_trigger_enabled = True
        client.config.experience_threshold = 100
        client._active_job_id = None

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                mock_trigger.return_value = MagicMock(job_id="test-job")
                await client._check_and_auto_trigger()
                mock_trigger.assert_called_once()
        assert client._active_job_id == "test-job"

    async def test_monitor_skips_trigger_when_not_training_ready(self, client):
        client._training_ready = False
        client._is_online = True
        client.config.auto_trigger_enabled = True
        client._active_job_id = None

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock) as mock_count:
            await client._check_and_auto_trigger()
            mock_count.assert_not_called()

    async def test_monitor_skips_trigger_when_below_threshold(self, client):
        client._training_ready = True
        client._is_online = True
        client.config.auto_trigger_enabled = True
        client.config.experience_threshold = 100
        client._active_job_id = None

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=50):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                await client._check_and_auto_trigger()
                mock_trigger.assert_not_called()

    async def test_monitor_skips_trigger_when_job_active(self, client):
        client._training_ready = True
        client._is_online = True
        client.config.auto_trigger_enabled = True
        client._active_job_id = "existing-job"

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock) as mock_count:
            await client._check_and_auto_trigger()
            mock_count.assert_not_called()

    async def test_monitor_skips_trigger_when_auto_disabled(self, client):
        client._training_ready = True
        client._is_online = True
        client.config.auto_trigger_enabled = False
        client._active_job_id = None

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock) as mock_count:
            await client._check_and_auto_trigger()
            mock_count.assert_not_called()


# =========================================================================
# Tests -- Training Job Polling (v2.1)
# =========================================================================

class TestJobPolling:
    """Verify health monitor polls active training jobs."""

    async def test_poll_detects_completed_job(self, client):
        """Poll should clear active_job_id when job completes."""
        client._active_job_id = "test-job-123"

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.metrics = {"loss": 0.5}
        mock_job.to_dict.return_value = {"job_id": "test-job-123", "status": "completed", "metrics": {"loss": 0.5}}

        with patch.object(client, 'get_training_job', new_callable=AsyncMock, return_value=mock_job):
            with patch.object(client, '_emit_event', new_callable=AsyncMock) as mock_emit:
                with patch.object(client, '_write_bridge_event', new_callable=AsyncMock) as mock_bridge:
                    await client._poll_active_job()
                    mock_emit.assert_called_once_with("training_completed", mock_job.to_dict())
                    mock_bridge.assert_called_once_with("training_completed", mock_job.to_dict())
        assert client._active_job_id is None

    async def test_poll_detects_failed_job(self, client):
        """Poll should clear active_job_id and log error when job fails."""
        client._active_job_id = "test-job-456"

        mock_job = MagicMock()
        mock_job.status = "failed"
        mock_job.error = "OOM"
        mock_job.to_dict.return_value = {"job_id": "test-job-456", "status": "failed", "error": "OOM"}

        with patch.object(client, 'get_training_job', new_callable=AsyncMock, return_value=mock_job):
            with patch.object(client, '_emit_event', new_callable=AsyncMock) as mock_emit:
                with patch.object(client, '_write_bridge_event', new_callable=AsyncMock) as mock_bridge:
                    await client._poll_active_job()
                    mock_emit.assert_called_once_with("training_failed", mock_job.to_dict())
                    mock_bridge.assert_called_once_with("training_failed", mock_job.to_dict())
        assert client._active_job_id is None

    async def test_poll_keeps_running_job(self, client):
        """Poll should keep active_job_id for running jobs."""
        client._active_job_id = "test-job-789"

        mock_job = MagicMock()
        mock_job.status = "running"
        mock_job.stage = MagicMock(value="training")

        with patch.object(client, 'get_training_job', new_callable=AsyncMock, return_value=mock_job):
            await client._poll_active_job()
            assert client._active_job_id == "test-job-789"

    async def test_poll_noop_when_no_active_job(self, client):
        """Poll should do nothing when no active job."""
        client._active_job_id = None
        await client._poll_active_job()  # Should not raise


# =========================================================================
# Tests -- Training Circuit Breaker (v2.1)
# =========================================================================

class TestTrainingCircuitBreaker:
    """Verify circuit breaker protects training triggers."""

    async def test_auto_trigger_skipped_when_circuit_open(self, client):
        """Circuit breaker should prevent auto-trigger when open."""
        client._training_ready = True
        client._is_online = True
        client._training_circuit_breaker = AsyncMock()
        client._training_circuit_breaker.can_execute = AsyncMock(return_value=False)

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock) as mock_count:
            await client._check_and_auto_trigger()
            mock_count.assert_not_called()  # Should skip entirely

    async def test_auto_trigger_allowed_when_circuit_closed(self, client):
        """Circuit breaker should allow auto-trigger when closed."""
        client._training_ready = True
        client._is_online = True
        client._training_circuit_breaker = AsyncMock()
        client._training_circuit_breaker.can_execute = AsyncMock(return_value=True)

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock) as mock_count:
            mock_count.return_value = 50  # Below threshold
            await client._check_and_auto_trigger()
            mock_count.assert_called_once()  # Circuit allowed, continued to count check

    async def test_job_failure_records_circuit_failure(self, client):
        """Failed training job should record circuit breaker failure."""
        client._active_job_id = "failing-job"
        mock_cb = AsyncMock()
        client._training_circuit_breaker = mock_cb

        mock_job = MagicMock()
        mock_job.status = "failed"
        mock_job.error = "OOM"
        mock_job.to_dict.return_value = {"status": "failed", "error": "OOM"}

        with patch.object(client, 'get_training_job', new_callable=AsyncMock, return_value=mock_job):
            with patch.object(client, '_emit_event', new_callable=AsyncMock):
                with patch.object(client, '_write_bridge_event', new_callable=AsyncMock):
                    await client._poll_active_job()

        mock_cb.record_failure.assert_called_once()

    async def test_job_completion_records_circuit_success(self, client):
        """Completed training job should record circuit breaker success."""
        client._active_job_id = "good-job"
        mock_cb = AsyncMock()
        client._training_circuit_breaker = mock_cb

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.metrics = {"loss": 0.3}
        mock_job.to_dict.return_value = {"status": "completed", "metrics": {"loss": 0.3}}

        with patch.object(client, 'get_training_job', new_callable=AsyncMock, return_value=mock_job):
            with patch.object(client, '_emit_event', new_callable=AsyncMock):
                with patch.object(client, '_write_bridge_event', new_callable=AsyncMock):
                    await client._poll_active_job()

        mock_cb.record_success.assert_called_once()


# =========================================================================
# Tests -- Resource-Aware Training Gate (v2.3)
# =========================================================================

class TestResourceAwareTrainingGate:
    """Verify _check_and_auto_trigger respects system memory tier."""

    def _setup_trigger_ready(self, client: ReactorCoreClient, count: int = 150):
        """Helper: put client into a state where auto-trigger would fire
        if not blocked by memory gate.  Returns mock_trigger for assertions."""
        client._training_ready = True
        client._is_online = True
        client.config.auto_trigger_enabled = True
        client.config.experience_threshold = 100
        client._active_job_id = None
        # Disable circuit breaker for these tests
        client._training_circuit_breaker = None

    # ----- ABUNDANT / OPTIMAL: full training --------------------------------

    async def test_abundant_allows_full_training(self, client):
        """ABUNDANT tier should allow training with no restrictions."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "ABUNDANT"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                mock_trigger.return_value = MagicMock(job_id="job-abundant")
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_called_once()
                # Should NOT pass reduced_batch metadata
                call_kwargs = mock_trigger.call_args
                metadata = call_kwargs.kwargs.get("metadata") if call_kwargs.kwargs else None
                assert metadata is None or metadata.get("reduced_batch") is not True

    async def test_optimal_allows_full_training(self, client):
        """OPTIMAL tier should allow training with no restrictions."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "OPTIMAL"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                mock_trigger.return_value = MagicMock(job_id="job-optimal")
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_called_once()

    # ----- ELEVATED: reduced batch ------------------------------------------

    async def test_elevated_triggers_with_reduced_batch(self, client):
        """ELEVATED tier should trigger training with reduced_batch=True metadata."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "ELEVATED"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                mock_trigger.return_value = MagicMock(job_id="job-elevated")
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_called_once()
                call_kwargs = mock_trigger.call_args
                metadata = call_kwargs.kwargs.get("metadata")
                assert metadata is not None
                assert metadata.get("reduced_batch") is True

    # ----- CONSTRAINED: defer to Night Shift --------------------------------

    async def test_constrained_defers_training(self, client):
        """CONSTRAINED tier should defer training (not trigger)."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "CONSTRAINED"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_not_called()

    # ----- CRITICAL / EMERGENCY: skip entirely ------------------------------

    async def test_critical_skips_training(self, client):
        """CRITICAL tier should skip training entirely."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "CRITICAL"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_not_called()

    async def test_emergency_skips_training(self, client):
        """EMERGENCY tier should skip training entirely."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "EMERGENCY"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_not_called()

    # ----- Graceful degradation: MemoryQuantizer unavailable ----------------

    async def test_no_quantizer_allows_training(self, client):
        """When MemoryQuantizer is not available, training should proceed (assume ABUNDANT)."""
        self._setup_trigger_ready(client)

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                mock_trigger.return_value = MagicMock(job_id="job-no-quantizer")
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    None,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_called_once()

    # ----- Below threshold: memory gate should not even be consulted --------

    async def test_below_threshold_skips_without_memory_check(self, client):
        """Below experience threshold, training should not trigger regardless of memory tier."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "ABUNDANT"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=50):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    await client._check_and_auto_trigger()

                mock_trigger.assert_not_called()

    # ----- Configurable tier behavior via env var ---------------------------

    async def test_elevated_behavior_configurable(self, client):
        """ELEVATED tier behavior should be configurable - can be set to 'defer' instead of 'reduced'."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "ELEVATED"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    with patch.dict(os.environ, {"REACTOR_ELEVATED_BEHAVIOR": "defer"}):
                        await client._check_and_auto_trigger()

                mock_trigger.assert_not_called()

    async def test_constrained_behavior_configurable(self, client):
        """CONSTRAINED tier behavior should be configurable - can be set to 'reduced' instead of 'defer'."""
        self._setup_trigger_ready(client)

        mock_tier = MagicMock()
        mock_tier.name = "CONSTRAINED"

        mock_quantizer = MagicMock()
        mock_quantizer.current_tier = mock_tier

        with patch.object(client, 'get_experience_count', new_callable=AsyncMock, return_value=150):
            with patch.object(client, 'trigger_training', new_callable=AsyncMock) as mock_trigger:
                mock_trigger.return_value = MagicMock(job_id="job-reduced-constrained")
                with patch(
                    "backend.clients.reactor_core_client._memory_quantizer_instance",
                    mock_quantizer,
                ):
                    with patch.dict(os.environ, {"REACTOR_CONSTRAINED_BEHAVIOR": "reduced"}):
                        await client._check_and_auto_trigger()

                mock_trigger.assert_called_once()
                call_kwargs = mock_trigger.call_args
                metadata = call_kwargs.kwargs.get("metadata")
                assert metadata is not None
                assert metadata.get("reduced_batch") is True


# =========================================================================
# Tests -- TrinityEventBus Integration (v2.4)
# =========================================================================

class TestTrinityEventBusIntegration:
    """Verify TrinityEventBus events are emitted at pipeline stages."""

    async def test_trigger_training_emits_bus_event(self, client):
        """trigger_training should publish training.started on TrinityEventBus."""
        client._request = AsyncMock(return_value={"job_id": "bus-job-1", "status": "queued"})
        client._emit_event = AsyncMock()
        client._write_bridge_event = AsyncMock()

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(return_value="evt-id-1")

        with patch(
            "backend.clients.reactor_core_client._get_event_bus",
            return_value=mock_bus,
        ):
            job = await client.trigger_training(
                experience_count=100,
                priority=TrainingPriority.NORMAL,
                force=True,
            )

        assert job is not None
        mock_bus.publish.assert_called_once()
        event = mock_bus.publish.call_args[0][0]
        assert event.topic == "training.started"
        assert "bus-job-1" in str(event.payload.get("job_id", ""))

    async def test_poll_completed_emits_bus_event(self, client):
        """_poll_active_job should publish training.completed on TrinityEventBus."""
        client._active_job_id = "bus-job-2"

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.metrics = {"loss": 0.3}
        mock_job.to_dict.return_value = {
            "job_id": "bus-job-2",
            "status": "completed",
            "metrics": {"loss": 0.3},
        }

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(return_value="evt-id-2")

        with patch.object(client, "get_training_job", new_callable=AsyncMock, return_value=mock_job):
            with patch.object(client, "_emit_event", new_callable=AsyncMock):
                with patch.object(client, "_write_bridge_event", new_callable=AsyncMock):
                    with patch(
                        "backend.clients.reactor_core_client._get_event_bus",
                        return_value=mock_bus,
                    ):
                        await client._poll_active_job()

        mock_bus.publish.assert_called_once()
        event = mock_bus.publish.call_args[0][0]
        assert event.topic == "training.completed"

    async def test_poll_failed_emits_bus_event(self, client):
        """_poll_active_job should publish training.failed on TrinityEventBus."""
        client._active_job_id = "bus-job-3"

        mock_job = MagicMock()
        mock_job.status = "failed"
        mock_job.error = "OOM"
        mock_job.to_dict.return_value = {
            "job_id": "bus-job-3",
            "status": "failed",
            "error": "OOM",
        }

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(return_value="evt-id-3")

        with patch.object(client, "get_training_job", new_callable=AsyncMock, return_value=mock_job):
            with patch.object(client, "_emit_event", new_callable=AsyncMock):
                with patch.object(client, "_write_bridge_event", new_callable=AsyncMock):
                    with patch(
                        "backend.clients.reactor_core_client._get_event_bus",
                        return_value=mock_bus,
                    ):
                        await client._poll_active_job()

        mock_bus.publish.assert_called_once()
        event = mock_bus.publish.call_args[0][0]
        assert event.topic == "training.failed"

    async def test_bus_unavailable_does_not_crash(self, client):
        """If TrinityEventBus is unavailable, event emission should be silently skipped."""
        client._request = AsyncMock(return_value={"job_id": "bus-job-4", "status": "queued"})
        client._emit_event = AsyncMock()
        client._write_bridge_event = AsyncMock()

        with patch(
            "backend.clients.reactor_core_client._get_event_bus",
            return_value=None,
        ):
            job = await client.trigger_training(
                experience_count=50,
                priority=TrainingPriority.NORMAL,
                force=True,
            )

        # Should succeed even without bus
        assert job is not None
        assert job.job_id == "bus-job-4"

    async def test_bus_publish_error_does_not_crash(self, client):
        """If bus.publish raises, trigger_training should still succeed."""
        client._request = AsyncMock(return_value={"job_id": "bus-job-5", "status": "queued"})
        client._emit_event = AsyncMock()
        client._write_bridge_event = AsyncMock()

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))

        with patch(
            "backend.clients.reactor_core_client._get_event_bus",
            return_value=mock_bus,
        ):
            job = await client.trigger_training(
                experience_count=50,
                priority=TrainingPriority.NORMAL,
                force=True,
            )

        # Should succeed despite bus error
        assert job is not None

    async def test_event_has_correlation_id(self, client):
        """Events should carry the job_id as correlation_id."""
        client._request = AsyncMock(return_value={"job_id": "corr-job", "status": "queued"})
        client._emit_event = AsyncMock()
        client._write_bridge_event = AsyncMock()

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(return_value="evt-id")

        with patch(
            "backend.clients.reactor_core_client._get_event_bus",
            return_value=mock_bus,
        ):
            await client.trigger_training(
                experience_count=100,
                priority=TrainingPriority.NORMAL,
                force=True,
            )

        event = mock_bus.publish.call_args[0][0]
        assert event.correlation_id == "corr-job"

    async def test_completed_event_has_causation_id(self, client):
        """training.completed causation_id should reference the training.started event."""
        client._active_job_id = "caus-job"
        # Simulate that trigger_training stored the start event id
        client._job_start_event_ids = {"caus-job": "start-evt-abc"}

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.metrics = {}
        mock_job.to_dict.return_value = {"job_id": "caus-job", "status": "completed"}

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(return_value="evt-id")

        with patch.object(client, "get_training_job", new_callable=AsyncMock, return_value=mock_job):
            with patch.object(client, "_emit_event", new_callable=AsyncMock):
                with patch.object(client, "_write_bridge_event", new_callable=AsyncMock):
                    with patch(
                        "backend.clients.reactor_core_client._get_event_bus",
                        return_value=mock_bus,
                    ):
                        await client._poll_active_job()

        event = mock_bus.publish.call_args[0][0]
        assert event.correlation_id == "caus-job"
        # causation_id should reference the start event
        assert event.causation_id == "start-evt-abc"
