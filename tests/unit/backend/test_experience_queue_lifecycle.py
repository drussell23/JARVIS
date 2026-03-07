"""Tests for ExperienceQueueProcessor lifecycle in agent_runtime."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest


def _build_runtime():
    from autonomy.agent_runtime import UnifiedAgentRuntime
    rt = UnifiedAgentRuntime.__new__(UnifiedAgentRuntime)
    rt._experience_processor = None
    rt._experience_processor_started = False
    return rt


class TestExperienceQueueLifecycle:
    @pytest.mark.asyncio
    async def test_start_experience_processor_sets_flag(self):
        """_start_experience_processor should start the processor and set flag."""
        rt = _build_runtime()

        mock_processor = AsyncMock()
        mock_processor.start = AsyncMock()

        with patch("autonomy.agent_runtime.get_experience_processor",
                    new_callable=AsyncMock, return_value=mock_processor):
            await rt._start_experience_processor()

        assert rt._experience_processor_started is True
        assert rt._experience_processor is mock_processor
        mock_processor.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_experience_processor_idempotent(self):
        """Calling twice should not start twice."""
        rt = _build_runtime()
        rt._experience_processor_started = True
        rt._experience_processor = MagicMock()

        # Should not call get_experience_processor at all
        with patch("autonomy.agent_runtime.get_experience_processor",
                    new_callable=AsyncMock) as mock_get:
            await rt._start_experience_processor()
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_experience_processor_error_non_fatal(self):
        """If processor init fails, runtime continues — non-fatal."""
        rt = _build_runtime()

        with patch("autonomy.agent_runtime.get_experience_processor",
                    new_callable=AsyncMock, side_effect=Exception("DB locked")):
            await rt._start_experience_processor()

        assert rt._experience_processor_started is False
        assert rt._experience_processor is None
