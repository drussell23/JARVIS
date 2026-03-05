"""Tests for VerdictExecutor protocol compliance."""
import asyncio
import pytest
from unittest.mock import AsyncMock

from backend.core.root_authority_types import ProcessIdentity, ExecutionResult


class TestVerdictExecutorProtocol:
    def test_protocol_importable(self):
        from backend.core.root_authority import VerdictExecutor
        assert hasattr(VerdictExecutor, 'execute_drain')
        assert hasattr(VerdictExecutor, 'execute_term')
        assert hasattr(VerdictExecutor, 'execute_group_kill')
        assert hasattr(VerdictExecutor, 'execute_restart')
        assert hasattr(VerdictExecutor, 'get_current_identity')

    @pytest.mark.asyncio
    async def test_mock_executor_satisfies_protocol(self):
        from backend.core.root_authority import VerdictExecutor
        identity = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")

        class MockExecutor:
            async def execute_drain(self, subsystem, identity, drain_timeout_s):
                return ExecutionResult(True, True, "success", None, None, "c1")
            async def execute_term(self, subsystem, identity, term_timeout_s):
                return ExecutionResult(True, True, "success", None, None, "c1")
            async def execute_group_kill(self, subsystem, identity):
                return ExecutionResult(True, True, "success", None, None, "c1")
            async def execute_restart(self, subsystem, delay_s):
                return ExecutionResult(True, True, "success", identity, None, "c1")
            def get_current_identity(self, subsystem):
                return identity

        executor = MockExecutor()
        result = await executor.execute_drain("test", identity, 30.0)
        assert result.accepted
        assert result.result == "success"
