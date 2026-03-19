"""Tests for MindClient — JARVIS's connection to the J-Prime Mind."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from backend.core.mind_client import MindClient, OperationalLevel


@pytest.fixture
def client():
    return MindClient(mind_host="127.0.0.1", mind_port=8000)


class TestOperationalLevels:
    def test_starts_at_level_0(self, client):
        assert client.current_level == OperationalLevel.LEVEL_0

    def test_degrade_to_level_1_after_3_failures(self, client):
        for _ in range(3):
            client._record_failure()
        assert client.current_level == OperationalLevel.LEVEL_1

    def test_single_failure_stays_level_0(self, client):
        client._record_failure()
        assert client.current_level == OperationalLevel.LEVEL_0

    def test_degrade_to_level_2(self, client):
        for _ in range(3):
            client._record_failure()
        client._record_claude_failure()
        assert client.current_level == OperationalLevel.LEVEL_2

    def test_recovery_requires_3_consecutive_successes(self, client):
        for _ in range(3):
            client._record_failure()
        assert client.current_level == OperationalLevel.LEVEL_1
        client._record_success()
        assert client.current_level == OperationalLevel.LEVEL_1
        client._record_success()
        assert client.current_level == OperationalLevel.LEVEL_1
        client._record_success()
        assert client.current_level == OperationalLevel.LEVEL_0

    def test_failure_resets_success_counter(self, client):
        for _ in range(3):
            client._record_failure()
        # 2 successes then a failure
        client._record_success()
        client._record_success()
        client._record_failure()
        # back to needing 3 consecutive
        assert client.current_level == OperationalLevel.LEVEL_1


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_success_records_success(self, client):
        mock_resp = {"status": "ready", "protocol_version": "1.0.0", "brains_loaded": ["qwen_coder"]}
        with patch.object(client, "_http_get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.check_health()
            assert result["status"] == "ready"

    @pytest.mark.asyncio
    async def test_health_failure_records_failure(self, client):
        with patch.object(client, "_http_get", new_callable=AsyncMock, side_effect=Exception("unreachable")):
            with pytest.raises(Exception):
                await client.check_health()
        assert client._consecutive_failures == 1


class TestBrainSelect:
    @pytest.mark.asyncio
    async def test_select_brain_returns_classification(self, client):
        mock_resp = {
            "request_id": "req-001", "session_id": "sess-001", "trace_id": "trace-001",
            "status": "plan_ready", "served_mode": "LEVEL_0_PRIMARY",
            "classification": {"intent": "classification", "complexity": "light",
                             "confidence": 0.95, "brain_used": "qwen_coder", "graph_depth": "fast"},
        }
        with patch.object(client, "_http_post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.select_brain(command="check email", task_type="classification")
            assert result["classification"]["brain_used"] == "qwen_coder"

    @pytest.mark.asyncio
    async def test_select_brain_returns_none_at_level_2(self, client):
        # Force to level 2
        for _ in range(3):
            client._record_failure()
        client._record_claude_failure()
        assert client.current_level == OperationalLevel.LEVEL_2
        result = await client.select_brain(command="test")
        assert result is None

    @pytest.mark.asyncio
    async def test_select_brain_failure_degrades(self, client):
        with patch.object(client, "_http_post", new_callable=AsyncMock, side_effect=Exception("timeout")):
            result = await client.select_brain(command="test")
            assert result is None
        assert client._consecutive_failures == 1
