"""Tests for MindClient — JARVIS's connection to the J-Prime Mind."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch

from backend.core.mind_client import MindClient, OperationalLevel, _CircuitBreaker, _CircuitState


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


class TestCircuitBreaker:
    """Tests for the 3-state circuit breaker embedded in MindClient."""

    def test_circuit_starts_closed(self, client):
        assert client._circuit.state.value == "closed"

    def test_circuit_opens_after_failures(self, client):
        for _ in range(3):
            client._circuit.record_failure()
        assert client._circuit.state.value == "open"

    def test_circuit_blocks_when_open(self, client):
        for _ in range(3):
            client._circuit.record_failure()
        assert client._circuit.can_execute() is False

    def test_circuit_half_open_after_cooldown(self, client):
        for _ in range(3):
            client._circuit.record_failure()
        # Simulate cooldown elapsed
        client._circuit._last_failure_time = time.monotonic() - 31
        assert client._circuit.can_execute() is True
        assert client._circuit.state.value == "half_open"

    def test_circuit_closes_on_success_from_half_open(self, client):
        for _ in range(3):
            client._circuit.record_failure()
        client._circuit._last_failure_time = time.monotonic() - 31
        client._circuit.can_execute()  # transitions to HALF_OPEN
        client._circuit.record_success()
        assert client._circuit.state.value == "closed"

    def test_circuit_back_to_open_after_half_open_failure(self, client):
        for _ in range(3):
            client._circuit.record_failure()
        client._circuit._last_failure_time = time.monotonic() - 31
        client._circuit.can_execute()  # → HALF_OPEN
        client._circuit.record_failure()
        assert client._circuit.state.value == "open"

    def test_circuit_failure_count_resets_on_success(self, client):
        client._circuit.record_failure()
        client._circuit.record_failure()
        client._circuit.record_success()
        assert client._circuit._failure_count == 0

    def test_select_brain_returns_none_when_circuit_open(self, client):
        """select_brain() must short-circuit when the breaker is OPEN."""
        for _ in range(3):
            client._circuit.record_failure()
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            client.select_brain(command="test command")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_circuit_breaker_standalone(self):
        cb = _CircuitBreaker(failure_threshold=2, cooldown_s=1.0)
        assert cb.state == _CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == _CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == _CircuitState.OPEN
        assert cb.can_execute() is False
        # after cooldown
        cb._last_failure_time = time.monotonic() - 2
        assert cb.can_execute() is True
        assert cb.state == _CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == _CircuitState.CLOSED


class TestHealthMonitor:
    """Tests for the background health check task."""

    @pytest.mark.asyncio
    async def test_health_monitor_starts_and_stops(self, client):
        """start_health_monitor() creates a task; stop cancels it cleanly."""
        await client.start_health_monitor()
        assert client._health_task is not None
        assert not client._health_task.done()
        await client.stop_health_monitor()
        assert client._health_task is None

    @pytest.mark.asyncio
    async def test_health_monitor_is_idempotent(self, client):
        """Calling start_health_monitor() twice keeps the same task."""
        await client.start_health_monitor()
        task_first = client._health_task
        await client.start_health_monitor()
        assert client._health_task is task_first
        await client.stop_health_monitor()

    @pytest.mark.asyncio
    async def test_close_stops_monitor(self, client):
        """close() must stop the health monitor before closing the session."""
        await client.start_health_monitor()
        assert client._health_task is not None
        await client.close()
        assert client._health_task is None


class TestSendCommand:
    @pytest.mark.asyncio
    async def test_send_command_returns_plan(self, client):
        mock_resp = {
            "protocol_version": "1.0.0",
            "request_id": "req-001",
            "session_id": "sess-001",
            "trace_id": "trace-001",
            "status": "plan_ready",
            "served_mode": "LEVEL_0_PRIMARY",
            "classification": {
                "intent": "system_command",
                "complexity": "trivial",
                "confidence": 0.95,
                "brain_used": "phi3_lightweight",
                "graph_depth": "fast",
            },
            "plan": {
                "plan_id": "plan-abc123",
                "plan_hash": "a" * 64,
                "sub_goals": [
                    {"step_id": "s1", "goal": "open Safari", "tool_required": "app_control"}
                ],
                "execution_strategy": "sequential",
                "approval_required": False,
            },
        }
        with patch.object(client, "_http_post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.send_command("open Safari")
            assert result is not None
            assert result["status"] == "plan_ready"
            assert len(result["plan"]["sub_goals"]) == 1

    @pytest.mark.asyncio
    async def test_send_command_returns_none_at_level_2(self, client):
        # Force to level 2
        for _ in range(3):
            client._record_failure()
        client._record_claude_failure()
        assert client.current_level == OperationalLevel.LEVEL_2
        result = await client.send_command("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_send_command_failure_returns_none(self, client):
        with patch.object(client, "_http_post", new_callable=AsyncMock, side_effect=Exception("timeout")):
            result = await client.send_command("test")
            assert result is None

    @pytest.mark.asyncio
    async def test_send_command_failure_degrades(self, client):
        with patch.object(client, "_http_post", new_callable=AsyncMock, side_effect=Exception("timeout")):
            await client.send_command("test")
        assert client._consecutive_failures >= 1

    @pytest.mark.asyncio
    async def test_send_command_circuit_blocks(self, client):
        # Open the circuit
        for _ in range(3):
            client._circuit.record_failure()
        assert not client._circuit.can_execute()
        result = await client.send_command("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_send_command_with_context(self, client):
        mock_resp = {
            "request_id": "r1", "session_id": "s1", "trace_id": "t1",
            "status": "plan_ready", "served_mode": "LEVEL_0_PRIMARY",
            "plan": {"plan_id": "p1", "plan_hash": "h" * 64, "sub_goals": []},
            "classification": {},
        }
        with patch.object(client, "_http_post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await client.send_command("test", context={"speaker": "Derek"})
            # Verify context was included in the request
            call_args = mock_post.call_args
            payload = call_args[1]["data"] if "data" in call_args[1] else call_args[0][1]
            assert payload.get("context", {}).get("speaker") == "Derek"
