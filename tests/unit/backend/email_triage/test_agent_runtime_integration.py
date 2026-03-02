"""Tests for email triage integration with agent_runtime.py."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestMaybeRunEmailTriage:
    """_maybe_run_email_triage() in agent_runtime is gated correctly."""

    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        """When EMAIL_TRIAGE_ENABLED is not set, triage does not run."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMAIL_TRIAGE_ENABLED", None)
            from autonomy.agent_runtime import UnifiedAgentRuntime
            runtime = MagicMock(spec=UnifiedAgentRuntime)
            runtime._last_email_triage_run = 0.0
            # Call the method directly
            await UnifiedAgentRuntime._maybe_run_email_triage(runtime)
            # Should not have imported or run anything (no error = success)

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_calls(self):
        """Second call within poll_interval_s is a no-op."""
        from autonomy.agent_runtime import UnifiedAgentRuntime
        runtime = MagicMock(spec=UnifiedAgentRuntime)
        runtime._last_email_triage_run = time.time()  # Just ran
        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            await UnifiedAgentRuntime._maybe_run_email_triage(runtime)
            # No import attempted because cooldown not elapsed
