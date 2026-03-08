"""Test self-dev-status gracefully degrades when service unavailable."""
import pytest
from backend.core.ouroboros.governance.loop_cli import handle_status


@pytest.mark.asyncio
async def test_status_returns_string_when_service_none():
    result = await handle_status(None)
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_status_mentions_not_active_when_none():
    result = await handle_status(None)
    assert "not" in result.lower()
