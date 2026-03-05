"""Tests for active crash detection and health polling."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import time

from backend.core.root_authority_types import (
    ProcessIdentity, LifecycleAction, SubsystemState,
    TimeoutPolicy, RestartPolicy, LifecycleVerdict,
)


@pytest.fixture
def identity():
    return ProcessIdentity(
        pid=1234, start_time_ns=time.monotonic_ns(),
        session_id="test-session", exec_fingerprint="sha256:abc123"
    )


@pytest.fixture
def watcher():
    from backend.core.root_authority import RootAuthorityWatcher
    return RootAuthorityWatcher(
        session_id="test-session",
        timeout_policy=TimeoutPolicy(
            health_poll_interval_s=0.1,
            health_timeout_s=1.0,
        ),
        restart_policy=RestartPolicy(max_restarts=3),
    )


class TestWatchProcess:
    @pytest.mark.asyncio
    async def test_crash_detected_and_queued(self, watcher, identity):
        """process.wait() exit triggers verdict in queue."""
        watcher.register_subsystem("test-svc", identity)

        # Mock a process that exits with code 300
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=300)

        await watcher.watch_process("test-svc", mock_proc)

        # Verdict should be in the queue
        assert not watcher._verdict_queue.empty()
        verdict = await watcher._verdict_queue.get()
        assert verdict.action == LifecycleAction.RESTART
        assert verdict.reason_code == "crash_exit_300"

    @pytest.mark.asyncio
    async def test_clean_exit_no_verdict(self, watcher, identity):
        """Clean exit (code 0) does not produce a restart verdict."""
        watcher.register_subsystem("test-svc", identity)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        await watcher.watch_process("test-svc", mock_proc)

        # No verdict for clean exit
        assert watcher._verdict_queue.empty()


class TestPollHealth:
    @pytest.mark.asyncio
    async def test_healthy_response_updates_state(self, watcher, identity):
        """Healthy response transitions state from STARTING to ALIVE."""
        watcher.register_subsystem("test-svc", identity)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "liveness": "up", "readiness": "ready",
            "session_id": "test-session", "pid": 1234,
            "start_time_ns": identity.start_time_ns,
            "exec_fingerprint": "sha256:abc123",
            "schema_version": "1.0.0",
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        # Run one poll iteration then cancel
        poll_task = asyncio.create_task(
            watcher.poll_health("test-svc", "http://localhost:8000/health", mock_session)
        )
        await asyncio.sleep(0.3)  # Let at least one poll happen
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

        # State should have advanced from STARTING
        state = watcher.get_state("test-svc")
        assert state in (SubsystemState.ALIVE, SubsystemState.READY)

    @pytest.mark.asyncio
    async def test_verdict_queue_exists(self, watcher):
        """Watcher has a verdict queue for async consumers."""
        assert hasattr(watcher, '_verdict_queue')
        assert isinstance(watcher._verdict_queue, asyncio.Queue)
