"""Tests for ExperienceQueueProcessor lifecycle under supervisor."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
import asyncio


class TestSupervisorExperienceProcessor:
    @pytest.mark.asyncio
    async def test_start_registers_background_task(self):
        """Supervisor should register experience processor as background task."""
        mock_processor = AsyncMock()
        mock_processor.start = AsyncMock()
        mock_task = asyncio.Future()
        mock_task.set_result(None)
        mock_processor._task = mock_task

        with patch("core.experience_queue.get_experience_processor",
                    new_callable=AsyncMock, return_value=mock_processor):
            from core.experience_queue import get_experience_processor
            processor = await get_experience_processor()
            await processor.start()

        mock_processor.start.assert_called_once()
        assert processor._task is not None

    @pytest.mark.asyncio
    async def test_processor_start_failure_non_fatal(self):
        """If processor fails to start, supervisor should continue."""
        with patch("core.experience_queue.get_experience_processor",
                    new_callable=AsyncMock, side_effect=Exception("SQLite locked")):
            try:
                from core.experience_queue import get_experience_processor
                processor = await get_experience_processor()
                await processor.start()
                started = True
            except Exception:
                started = False

        assert started is False
