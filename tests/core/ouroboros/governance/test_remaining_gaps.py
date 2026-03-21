"""Tests for remaining gaps: Voice→FSM wiring, MCP server extensions, MCP transport."""
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# GAP 6: Voice → FSM wiring verification
# ---------------------------------------------------------------------------

class TestVoiceFsmWiring:
    def test_voice_sensor_accepts_signal_bus(self):
        """VoiceCommandSensor constructor accepts signal_bus parameter."""
        from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
            VoiceCommandSensor,
        )
        bus = MagicMock()
        sensor = VoiceCommandSensor(
            router=MagicMock(),
            repo="jarvis",
            signal_bus=bus,
        )
        assert sensor._signal_bus is bus

    def test_voice_sensor_without_bus_is_safe(self):
        from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
            VoiceCommandSensor,
        )
        sensor = VoiceCommandSensor(
            router=MagicMock(),
            repo="jarvis",
        )
        assert sensor._signal_bus is None

    def test_user_signal_bus_lifecycle(self):
        from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus
        bus = UserSignalBus()
        assert not bus.is_stop_requested()
        bus.request_stop()
        assert bus.is_stop_requested()
        bus.reset()
        assert not bus.is_stop_requested()


# ---------------------------------------------------------------------------
# GAP 5 + 10: MCP server extensions
# ---------------------------------------------------------------------------

class TestMCPServerExtensions:
    def _make_gls(self):
        gls = MagicMock()
        gls._approval_provider = MagicMock()
        gls._approval_provider.reject = AsyncMock(return_value=MagicMock(
            request_id="req-1",
            status=MagicMock(name="REJECTED"),
            approver="test",
            decided_at=datetime.now(tz=timezone.utc),
        ))
        gls._approval_provider._set_elicitation_answer = MagicMock()
        gls.health = MagicMock(return_value={"state": "active"})
        return gls

    @pytest.mark.asyncio
    async def test_reject_operation(self):
        from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer
        gls = self._make_gls()
        server = OuroborosMCPServer(gls)
        result = await server.reject_operation("req-1", "tester", "use generators instead")
        assert result["request_id"] == "req-1"
        assert result["reason"] == "use generators instead"

    @pytest.mark.asyncio
    async def test_elicit_answer(self):
        from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer
        gls = self._make_gls()
        server = OuroborosMCPServer(gls)
        result = await server.elicit_answer("req-1", "option B")
        assert result["status"] == "answered"
        assert result["answer"] == "option B"
        gls._approval_provider._set_elicitation_answer.assert_called_once_with("req-1", "option B")

    @pytest.mark.asyncio
    async def test_elicit_answer_unsupported_provider(self):
        from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer
        gls = MagicMock()
        gls._approval_provider = MagicMock(spec=[])  # no _set_elicitation_answer
        server = OuroborosMCPServer(gls)
        result = await server.elicit_answer("req-1", "answer")
        assert result["status"] == "error"
        assert "does not support" in result["error"]

    @pytest.mark.asyncio
    async def test_reject_operation_error_handling(self):
        from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer
        gls = MagicMock()
        gls._approval_provider = MagicMock()
        gls._approval_provider.reject = AsyncMock(side_effect=RuntimeError("boom"))
        server = OuroborosMCPServer(gls)
        result = await server.reject_operation("req-1", "tester", "reason")
        assert result["status"] == "error"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# GAP 10: MCP HTTP Transport
# ---------------------------------------------------------------------------

class TestMCPTransport:
    def test_create_app_returns_fastapi_or_none(self):
        from backend.core.ouroboros.governance.mcp_http_transport import create_mcp_app
        app = create_mcp_app()
        if app is not None:
            # FastAPI installed — verify routes exist
            routes = [r.path for r in app.routes]
            assert "/mcp/submit_intent" in routes
            assert "/mcp/health" in routes
            assert "/mcp/approve" in routes
            assert "/mcp/reject" in routes
            assert "/mcp/elicit_answer" in routes

    def test_set_gls_initializes_server(self):
        from backend.core.ouroboros.governance import mcp_http_transport
        gls = MagicMock()
        gls._approval_provider = MagicMock()
        mcp_http_transport.set_gls(gls)
        assert mcp_http_transport._mcp_server is not None
        # Cleanup
        mcp_http_transport._mcp_server = None

    def test_get_app_creates_if_needed(self):
        from backend.core.ouroboros.governance.mcp_http_transport import get_app
        app = get_app()
        # May be None if FastAPI not installed, or a FastAPI instance
        # Either way, should not raise
