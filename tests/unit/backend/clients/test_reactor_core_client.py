# tests/unit/backend/clients/test_reactor_core_client.py
"""Tests for ReactorCoreClient endpoint paths.

Verifies that every _request() call uses the correct Reactor-Core API path.
asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
    c._requests_sent = 0
    c._requests_failed = 0
    c._last_trigger_time = None
    c._training_triggers = 0
    c._callbacks = {}
    c._bridge_dir = None
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
        # /api/pipeline/state does not match any known Reactor-Core route;
        # This test documents the current path.
        assert path == "/api/pipeline/state"

    async def test_cancel_training_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"cancelled": True})

        await client.cancel_training("job-abc")

        call_args = client._request.call_args
        method, path = call_args[0][0], call_args[0][1]
        assert method == "POST"
        assert path == "/api/v1/jobs/job-abc/cancel"

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
        assert path == "/api/v1/jobs/job-abc"

    async def test_get_training_history_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value=[])

        await client.get_training_history(limit=5)

        call_args = client._request.call_args
        method, path = call_args[0][0], call_args[0][1]
        assert method == "GET"
        assert path == "/api/v1/jobs"

    async def test_add_learning_topic_path(self, client: ReactorCoreClient):
        client._request = AsyncMock(return_value={"added": True})

        await client.add_learning_topic("reinforcement learning")

        call_args = client._request.call_args
        method, path = call_args[0][0], call_args[0][1]
        assert method == "POST"
        # /api/scout/topics has no known Reactor-Core route;
        # This test documents the current path.
        assert path == "/api/scout/topics"
