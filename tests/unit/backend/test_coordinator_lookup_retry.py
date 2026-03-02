"""Tests for coordinator lookup retry state machine."""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


def _make_processor():
    """Create a minimal UnifiedCommandProcessor for testing coordinator lookup."""
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    with patch.object(UnifiedCommandProcessor, '__init__', lambda self: None):
        proc = UnifiedCommandProcessor.__new__(UnifiedCommandProcessor)
        proc._neural_mesh_coordinator = None
        proc._coordinator_state = "UNRESOLVED"
        proc._coordinator_last_lookup = 0.0
        proc._coordinator_lookup_failures = 0
        proc._coordinator_max_retries = 5
        proc._coordinator_cooldown_seconds = 300.0
        proc._coordinator_lock = asyncio.Lock()
        proc._v242_metrics = {
            "coordinator_lookups": 0,
            "coordinator_hits": 0,
            "coordinator_misses": 0,
            "coordinator_stale": 0,
        }
        return proc


class TestCoordinatorLookupStates:

    @pytest.mark.asyncio
    async def test_initial_state_is_unresolved(self):
        proc = _make_processor()
        assert proc._coordinator_state == "UNRESOLVED"

    @pytest.mark.asyncio
    async def test_successful_lookup_transitions_to_resolved(self):
        proc = _make_processor()
        mock_coord = MagicMock()
        mock_coord._running = True
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=mock_coord,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is mock_coord
        assert proc._coordinator_state == "RESOLVED"

    @pytest.mark.asyncio
    async def test_failed_lookup_transitions_to_backing_off(self):
        proc = _make_processor()
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=None,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is None
        assert proc._coordinator_state == "BACKING_OFF"
        assert proc._coordinator_lookup_failures == 1

    @pytest.mark.asyncio
    async def test_backoff_prevents_immediate_retry(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 1
        proc._coordinator_last_lookup = time.monotonic()
        result = await proc._get_neural_mesh_coordinator()
        assert result is None

    @pytest.mark.asyncio
    async def test_backoff_allows_retry_after_delay(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 1
        proc._coordinator_last_lookup = time.monotonic() - 10.0
        mock_coord = MagicMock()
        mock_coord._running = True
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=mock_coord,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is mock_coord
        assert proc._coordinator_state == "RESOLVED"

    @pytest.mark.asyncio
    async def test_max_retries_transitions_to_cooldown(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 4
        proc._coordinator_last_lookup = time.monotonic() - 120.0
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=None,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is None
        assert proc._coordinator_state == "COOLDOWN"

    @pytest.mark.asyncio
    async def test_resolved_returns_cached(self):
        proc = _make_processor()
        mock_coord = MagicMock()
        mock_coord._running = True
        proc._neural_mesh_coordinator = mock_coord
        proc._coordinator_state = "RESOLVED"
        result = await proc._get_neural_mesh_coordinator()
        assert result is mock_coord

    @pytest.mark.asyncio
    async def test_stale_coordinator_invalidated(self):
        proc = _make_processor()
        mock_coord = MagicMock()
        mock_coord._running = False
        proc._neural_mesh_coordinator = mock_coord
        proc._coordinator_state = "RESOLVED"
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=None,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is None
        # After staleness invalidation, the method attempts a fresh lookup.
        # Since the lookup returns None, state transitions to BACKING_OFF.
        assert proc._coordinator_state == "BACKING_OFF"
        assert proc._neural_mesh_coordinator is None
        assert proc._v242_metrics["coordinator_stale"] == 1

    @pytest.mark.asyncio
    async def test_notify_coordinator_ready_clears_backoff(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 3
        await proc.notify_coordinator_ready()
        assert proc._coordinator_state == "UNRESOLVED"
        assert proc._coordinator_lookup_failures == 0

    @pytest.mark.asyncio
    async def test_notify_coordinator_ready_clears_cooldown(self):
        proc = _make_processor()
        proc._coordinator_state = "COOLDOWN"
        proc._coordinator_lookup_failures = 5
        await proc.notify_coordinator_ready()
        assert proc._coordinator_state == "UNRESOLVED"
        assert proc._coordinator_lookup_failures == 0
