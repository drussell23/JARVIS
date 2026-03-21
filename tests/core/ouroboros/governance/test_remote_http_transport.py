"""Tests for RemoteHTTPTransport — cross-repo CommProtocol forwarding."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

import pytest

from backend.core.ouroboros.governance.comms.remote_http_transport import (
    RemoteHTTPTransport,
)


@dataclass
class FakeMessage:
    msg_type: str = "INTENT"
    op_id: str = "op-123"
    seq: int = 1
    payload: dict = None
    timestamp: float = 1234567890.0

    def __post_init__(self):
        if self.payload is None:
            self.payload = {"goal": "test"}


class TestRemoteHTTPTransport:
    def test_disabled_when_no_endpoint(self):
        transport = RemoteHTTPTransport(endpoint="")
        assert transport.is_enabled is False

    def test_enabled_with_endpoint(self):
        transport = RemoteHTTPTransport(endpoint="http://10.0.0.5:8000/v1/comm")
        assert transport.is_enabled is True

    @pytest.mark.asyncio
    async def test_send_noop_when_disabled(self):
        transport = RemoteHTTPTransport(endpoint="")
        await transport.send(FakeMessage())  # should not raise

    @pytest.mark.asyncio
    async def test_send_noop_when_circuit_open(self):
        transport = RemoteHTTPTransport(endpoint="http://fake:8000/v1/comm")
        transport._circuit_open = True
        await transport.send(FakeMessage())  # should not raise

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_after_max_failures(self):
        transport = RemoteHTTPTransport(
            endpoint="http://fake:8000/v1/comm",
            max_consecutive_failures=3,
        )
        # Mock _post to always fail
        transport._post = AsyncMock(side_effect=ConnectionError("refused"))

        for _ in range(3):
            await transport.send(FakeMessage())

        assert transport.is_circuit_open is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets(self):
        transport = RemoteHTTPTransport(endpoint="http://fake:8000/v1/comm")
        transport._circuit_open = True
        await transport.reset_circuit()
        assert transport.is_circuit_open is False
        assert transport._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        transport = RemoteHTTPTransport(endpoint="http://fake:8000/v1/comm")
        transport._consecutive_failures = 3
        transport._post = AsyncMock()  # succeeds
        await transport.send(FakeMessage())
        assert transport._consecutive_failures == 0

    def test_serialize_message(self):
        transport = RemoteHTTPTransport(endpoint="http://fake:8000")
        msg = FakeMessage()
        serialized = transport._serialize(msg)
        assert "op-123" in serialized
        assert "INTENT" in serialized

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        transport = RemoteHTTPTransport(endpoint="http://fake:8000")
        await transport.close()  # no session yet — should not raise
        await transport.close()  # again — still safe

    def test_env_var_endpoint(self):
        with patch.dict("os.environ", {"JARVIS_PRIME_COMM_ENDPOINT": "http://10.0.0.5:8000/v1/comm"}):
            transport = RemoteHTTPTransport()
            assert transport._endpoint == "http://10.0.0.5:8000/v1/comm"
            assert transport.is_enabled is True
