"""
Tests for claude_fallback module
==================================

Coverage:
- test_claude_inference_module_exists: import check
- test_claude_inference_returns_none_without_key: no ANTHROPIC_API_KEY -> None
- test_claude_inference_returns_none_on_empty_key: empty key -> None
- test_claude_inference_returns_text_on_success: mocked SDK -> response text
- test_claude_inference_returns_none_on_sdk_error: SDK raises -> None
- test_claude_inference_returns_none_on_empty_response: empty content -> None
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module import smoke test
# ---------------------------------------------------------------------------


def test_claude_inference_module_exists():
    """The claude_fallback module can be imported without error."""
    from backend.core.ouroboros.claude_fallback import claude_inference
    assert callable(claude_inference)


# ---------------------------------------------------------------------------
# Missing / empty API key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_inference_returns_none_without_key(monkeypatch):
    """Returns None when ANTHROPIC_API_KEY is not set at all."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from backend.core.ouroboros.claude_fallback import claude_inference
    result = await claude_inference("test prompt", caller_id="test")
    assert result is None


@pytest.mark.asyncio
async def test_claude_inference_returns_none_on_empty_key(monkeypatch):
    """Returns None when ANTHROPIC_API_KEY is empty string."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    from backend.core.ouroboros.claude_fallback import claude_inference
    result = await claude_inference("test prompt", caller_id="test")
    assert result is None


# ---------------------------------------------------------------------------
# Successful call (mocked SDK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_inference_returns_text_on_success(monkeypatch):
    """When SDK returns a valid response, claude_inference returns the text."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-123")

    # Build a mock response matching the Anthropic SDK shape
    mock_block = MagicMock()
    mock_block.text = '{"gaps": []}'

    mock_response = MagicMock()
    mock_response.content = [mock_block]

    mock_client_instance = MagicMock()
    mock_client_instance.messages = MagicMock()
    mock_client_instance.messages.create = AsyncMock(return_value=mock_response)

    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client_instance

    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        from backend.core.ouroboros import claude_fallback
        # Force re-import to use mocked anthropic
        import importlib
        importlib.reload(claude_fallback)

        result = await claude_fallback.claude_inference(
            "test prompt", caller_id="test"
        )

    assert result == '{"gaps": []}'


# ---------------------------------------------------------------------------
# SDK error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_inference_returns_none_on_sdk_error(monkeypatch):
    """Returns None when the SDK raises an exception."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-123")

    mock_client_instance = MagicMock()
    mock_client_instance.messages = MagicMock()
    mock_client_instance.messages.create = AsyncMock(
        side_effect=RuntimeError("API error")
    )

    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client_instance

    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        from backend.core.ouroboros import claude_fallback
        import importlib
        importlib.reload(claude_fallback)

        result = await claude_fallback.claude_inference(
            "test prompt", caller_id="test"
        )

    assert result is None


# ---------------------------------------------------------------------------
# Empty response content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_inference_returns_none_on_empty_response(monkeypatch):
    """Returns None when the response has no text blocks."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-123")

    # Response with no text blocks
    mock_response = MagicMock()
    mock_response.content = []

    mock_client_instance = MagicMock()
    mock_client_instance.messages = MagicMock()
    mock_client_instance.messages.create = AsyncMock(return_value=mock_response)

    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client_instance

    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        from backend.core.ouroboros import claude_fallback
        import importlib
        importlib.reload(claude_fallback)

        result = await claude_fallback.claude_inference(
            "test prompt", caller_id="test"
        )

    assert result is None
