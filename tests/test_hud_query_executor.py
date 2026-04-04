import pytest
from unittest.mock import AsyncMock
from backend.hud.query_executor import QueryExecutor


@pytest.mark.asyncio
async def test_answer_returns_response():
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(return_value="The answer is 42.")
    executor = QueryExecutor(dw)
    result = await executor.answer("What is the meaning of life?")
    assert result == "The answer is 42."
    dw.prompt_only.assert_called_once()


@pytest.mark.asyncio
async def test_answer_handles_failure():
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(side_effect=Exception("API error"))
    executor = QueryExecutor(dw)
    result = await executor.answer("Test question")
    assert "sorry" in result.lower() or "couldn't" in result.lower()
