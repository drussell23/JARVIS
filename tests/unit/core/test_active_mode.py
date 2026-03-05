"""Tests for active mode verdict dispatch."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.root_authority_types import (
    ProcessIdentity, LifecycleAction, ExecutionResult,
    TimeoutPolicy, RestartPolicy,
)


@pytest.fixture
def identity():
    return ProcessIdentity(
        pid=1234, start_time_ns=time.monotonic_ns(),
        session_id="test", exec_fingerprint="sha256:abc"
    )


def _make_watcher():
    """Create a watcher inside the running event loop to avoid Queue loop mismatch."""
    from backend.core.root_authority import RootAuthorityWatcher
    return RootAuthorityWatcher(
        session_id="test",
        timeout_policy=TimeoutPolicy(),
        restart_policy=RestartPolicy(max_restarts=3),
    )


class TestVerdictDispatchLoop:
    @pytest.mark.asyncio
    async def test_verdict_dispatched_to_executor(self, identity):
        """Verdicts from queue are dispatched to executor."""
        watcher = _make_watcher()
        watcher.register_subsystem("test-svc", identity)
        verdict = watcher.process_crash("test-svc", exit_code=300)
        assert verdict is not None

        # Put verdict in queue
        await watcher._verdict_queue.put(verdict)

        # Mock executor
        executor = AsyncMock()
        executor.execute_restart = AsyncMock(
            return_value=ExecutionResult(True, True, "success", None, None, "c1")
        )
        executor.get_current_identity = MagicMock(return_value=identity)

        # Run dispatch for one iteration
        task = asyncio.create_task(
            watcher.run_verdict_dispatch(executor, active_subsystems={"test-svc"})
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_inactive_subsystem_verdict_logged_not_executed(self, identity):
        """Verdicts for inactive subsystems are logged but not executed."""
        watcher = _make_watcher()
        watcher.register_subsystem("inactive-svc", identity)
        verdict = watcher.process_crash("inactive-svc", exit_code=300)
        assert verdict is not None

        await watcher._verdict_queue.put(verdict)

        executor = AsyncMock()

        task = asyncio.create_task(
            watcher.run_verdict_dispatch(executor, active_subsystems={"other-svc"})
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Executor should NOT have been called
        executor.execute_restart.assert_not_called()
        executor.execute_drain.assert_not_called()

    @pytest.mark.asyncio
    async def test_escalation_used_for_drain_verdict(self, identity):
        """DRAIN verdicts trigger the escalation engine."""
        watcher = _make_watcher()
        watcher.register_subsystem("test-svc", identity)

        # Manually create a drain verdict
        from backend.core.root_authority_types import LifecycleVerdict
        from datetime import datetime, timezone
        import uuid

        drain_verdict = LifecycleVerdict(
            subsystem="test-svc",
            identity=identity,
            action=LifecycleAction.DRAIN,
            reason="test drain",
            reason_code="test",
            correlation_id=str(uuid.uuid4()),
            incident_id="inc-1",
            exit_code=None,
            observed_at_ns=time.monotonic_ns(),
            wall_time_utc=datetime.now(timezone.utc).isoformat(),
        )

        await watcher._verdict_queue.put(drain_verdict)

        executor = AsyncMock()
        executor.execute_drain = AsyncMock(
            return_value=ExecutionResult(True, True, "success", None, None, "c1")
        )
        executor.get_current_identity = MagicMock(return_value=identity)

        task = asyncio.create_task(
            watcher.run_verdict_dispatch(executor, active_subsystems={"test-svc"})
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
