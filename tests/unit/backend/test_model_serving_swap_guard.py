"""Tests for PrimeLocalClient.generate() model_swapping guard."""

import asyncio
import os
import sys
import time
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest


@pytest.fixture
def prime_local_client():
    """Build a minimal PrimeLocalClient for testing generate()."""
    from intelligence.unified_model_serving import PrimeLocalClient
    client = PrimeLocalClient.__new__(PrimeLocalClient)
    client.logger = MagicMock()
    client._loaded = True
    client._model = MagicMock()
    client._model_path = MagicMock()
    client._model_path.name = "test-model.gguf"
    client._model_swapping = False
    client._inference_executor = None
    client._current_model_entry = {"name": "test", "quality_rank": 1}
    return client


@pytest.mark.asyncio
async def test_generate_fast_fails_during_model_swap(prime_local_client):
    """generate() should return immediately with error when _model_swapping is True."""
    from intelligence.unified_model_serving import ModelRequest
    prime_local_client._model_swapping = True

    request = ModelRequest(
        messages=[{"role": "user", "content": "test"}],
        max_tokens=10,
    )
    response = await prime_local_client.generate(request)

    assert response.success is False
    assert "model_swap_in_progress" in response.error
    assert response.latency_ms < 100  # Must be fast, not blocked


@pytest.mark.asyncio
async def test_generate_proceeds_when_not_swapping(prime_local_client):
    """generate() should proceed normally when _model_swapping is False."""
    from intelligence.unified_model_serving import ModelRequest
    prime_local_client._model_swapping = False
    # Mock the inference path
    mock_result = {"choices": [{"text": "hello"}], "usage": {"total_tokens": 5}}
    prime_local_client._model.return_value = mock_result
    prime_local_client._inference_executor = None

    request = ModelRequest(
        messages=[{"role": "user", "content": "test"}],
        max_tokens=10,
    )

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(return_value=mock_result)
        response = await prime_local_client.generate(request)

    assert response.success is True
    assert response.content == "hello"
