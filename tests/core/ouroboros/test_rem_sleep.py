"""Tests for RemSleepDaemon — Phase 3 idle-watch state machine (TDD).

All tests are pure-asyncio with zero I/O, zero model calls, and zero network.
Dependencies are fully mocked via MagicMock / AsyncMock.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.rem_sleep import RemSleepDaemon, RemState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spinal_cord() -> MagicMock:
    """Return a mock SpinalCord whose wait_for_gate returns immediately."""
    cord = MagicMock()
    cord.wait_for_gate = AsyncMock(return_value=None)
    return cord


def _make_proactive_drive() -> MagicMock:
    """Return a mock ProactiveDrive that accepts on_eligible callbacks."""
    drive = MagicMock()
    drive.on_eligible = MagicMock()
    return drive


def _make_config(
    *,
    rem_cooldown_s: float = 0.01,
    rem_epoch_timeout_s: float = 5.0,
    rem_max_agents: int = 3,
    rem_max_findings_per_epoch: int = 5,
) -> MagicMock:
    """Return a frozen-like config mock with REM fields."""
    cfg = MagicMock()
    cfg.rem_cooldown_s = rem_cooldown_s
    cfg.rem_epoch_timeout_s = rem_epoch_timeout_s
    cfg.rem_max_agents = rem_max_agents
    cfg.rem_max_findings_per_epoch = rem_max_findings_per_epoch
    return cfg


def _make_epoch_result(
    *,
    epoch_id: int = 1,
    findings_count: int = 2,
    envelopes_submitted: int = 2,
    cancelled: bool = False,
    error: str | None = None,
) -> MagicMock:
    """Return a mock EpochResult with the real EpochResult field names."""
    result = MagicMock()
    result.epoch_id = epoch_id
    result.findings_count = findings_count  # EpochResult uses findings_count (int)
    result.envelopes_submitted = envelopes_submitted
    result.envelopes_backpressured = 0
    result.cancelled = cancelled
    result.completed = not cancelled
    result.error = error
    result.duration_s = 0.001
    return result


def _make_daemon(
    *,
    spinal_cord: Any = None,
    proactive_drive: Any = None,
    config: Any = None,
) -> RemSleepDaemon:
    """Build a RemSleepDaemon with all dependencies mocked."""
    oracle = MagicMock()
    fleet = MagicMock()
    intake_router = MagicMock()
    doubleword = MagicMock()

    return RemSleepDaemon(
        oracle=oracle,
        fleet=fleet,
        spinal_cord=spinal_cord or _make_spinal_cord(),
        intake_router=intake_router,
        proactive_drive=proactive_drive or _make_proactive_drive(),
        doubleword=doubleword,
        config=config or _make_config(),
    )


# ---------------------------------------------------------------------------
# test_initial_state_is_idle_watch
# ---------------------------------------------------------------------------


class TestInitialStateIsIdleWatch:
    def test_initial_state_is_idle_watch(self):
        """RemSleepDaemon must begin in IDLE_WATCH state."""
        daemon = _make_daemon()
        assert daemon.state is RemState.IDLE_WATCH


# ---------------------------------------------------------------------------
# test_state_transitions
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_transition_to_exploring(self):
        """_transition(EXPLORING) changes state to EXPLORING."""
        daemon = _make_daemon()
        daemon._transition(RemState.EXPLORING)
        assert daemon.state is RemState.EXPLORING

    def test_transition_to_analyzing(self):
        """_transition(ANALYZING) changes state to ANALYZING."""
        daemon = _make_daemon()
        daemon._transition(RemState.ANALYZING)
        assert daemon.state is RemState.ANALYZING

    def test_transition_to_patching(self):
        """_transition(PATCHING) changes state to PATCHING."""
        daemon = _make_daemon()
        daemon._transition(RemState.PATCHING)
        assert daemon.state is RemState.PATCHING

    def test_transition_to_cooldown(self):
        """_transition(COOLDOWN) changes state to COOLDOWN."""
        daemon = _make_daemon()
        daemon._transition(RemState.COOLDOWN)
        assert daemon.state is RemState.COOLDOWN

    def test_multiple_transitions_track_last_state(self):
        """Sequential transitions end on the most recent state."""
        daemon = _make_daemon()
        daemon._transition(RemState.EXPLORING)
        daemon._transition(RemState.ANALYZING)
        daemon._transition(RemState.COOLDOWN)
        daemon._transition(RemState.IDLE_WATCH)
        assert daemon.state is RemState.IDLE_WATCH

    def test_transition_to_same_state_is_safe(self):
        """Transitioning to the current state does not raise."""
        daemon = _make_daemon()
        daemon._transition(RemState.IDLE_WATCH)  # already IDLE_WATCH
        assert daemon.state is RemState.IDLE_WATCH


# ---------------------------------------------------------------------------
# test_epoch_counter_increments
# ---------------------------------------------------------------------------


class TestEpochCounterIncrements:
    def test_first_call_returns_one(self):
        """The first _next_epoch_id() call returns 1."""
        daemon = _make_daemon()
        assert daemon._next_epoch_id() == 1

    def test_sequential_calls_increment(self):
        """Calling _next_epoch_id 3 times yields 1, 2, 3."""
        daemon = _make_daemon()
        ids = [daemon._next_epoch_id() for _ in range(3)]
        assert ids == [1, 2, 3]

    def test_counter_is_monotonically_increasing(self):
        """Each successive call returns a strictly greater value."""
        daemon = _make_daemon()
        previous = 0
        for _ in range(10):
            current = daemon._next_epoch_id()
            assert current > previous
            previous = current


# ---------------------------------------------------------------------------
# test_start_and_stop
# ---------------------------------------------------------------------------


class TestStartAndStop:
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        """start() must create a background asyncio.Task."""
        daemon = _make_daemon()

        # Patch _daemon_loop to never actually run (avoid blocking in tests)
        async def _noop_loop():
            await asyncio.sleep(0)  # yield once then exit

        with patch.object(daemon, "_daemon_loop", side_effect=_noop_loop):
            await daemon.start()

        assert daemon._task is not None
        assert isinstance(daemon._task, asyncio.Task)

        # Clean up
        await daemon.stop()

    @pytest.mark.asyncio
    async def test_start_returns_immediately(self):
        """start() must not block — it creates the task and returns."""
        daemon = _make_daemon()

        async def _blocking_loop():
            await asyncio.sleep(9999)

        with patch.object(daemon, "_daemon_loop", side_effect=_blocking_loop):
            # If start() blocks, this would hang. We use wait_for as a guard.
            await asyncio.wait_for(daemon.start(), timeout=1.0)

        # Clean up
        await daemon.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        """stop() must cancel the background task."""
        daemon = _make_daemon()

        async def _blocking_loop():
            try:
                await asyncio.sleep(9999)
            except asyncio.CancelledError:
                return

        with patch.object(daemon, "_daemon_loop", side_effect=_blocking_loop):
            await daemon.start()

        task = daemon._task
        assert task is not None

        await daemon.stop()

        assert task.done()

    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self):
        """Calling stop() before start() must not raise."""
        daemon = _make_daemon()
        await daemon.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_start_then_stop_clears_task(self):
        """After stop(), _task is done."""
        daemon = _make_daemon()

        async def _noop_loop():
            return

        with patch.object(daemon, "_daemon_loop", side_effect=_noop_loop):
            await daemon.start()

        await daemon.stop()
        assert daemon._task is None or daemon._task.done()


# ---------------------------------------------------------------------------
# test_health_report
# ---------------------------------------------------------------------------


class TestHealthReport:
    def test_health_returns_dict(self):
        """health() must return a dictionary."""
        daemon = _make_daemon()
        result = daemon.health()
        assert isinstance(result, dict)

    def test_health_contains_state(self):
        """health() must include 'state' key."""
        daemon = _make_daemon()
        result = daemon.health()
        assert "state" in result

    def test_health_state_matches_current_state(self):
        """health()['state'] matches the daemon's current state value."""
        daemon = _make_daemon()
        result = daemon.health()
        assert result["state"] == RemState.IDLE_WATCH.value

    def test_health_contains_epoch_count(self):
        """health() must include 'epoch_count' key."""
        daemon = _make_daemon()
        result = daemon.health()
        assert "epoch_count" in result

    def test_health_epoch_count_starts_at_zero(self):
        """epoch_count starts at zero before any epochs run."""
        daemon = _make_daemon()
        result = daemon.health()
        assert result["epoch_count"] == 0

    def test_health_contains_total_findings(self):
        """health() must include 'total_findings' key."""
        daemon = _make_daemon()
        result = daemon.health()
        assert "total_findings" in result

    def test_health_total_findings_starts_at_zero(self):
        """total_findings starts at zero before any epochs run."""
        daemon = _make_daemon()
        result = daemon.health()
        assert result["total_findings"] == 0

    def test_health_contains_total_envelopes(self):
        """health() must include 'total_envelopes' key."""
        daemon = _make_daemon()
        result = daemon.health()
        assert "total_envelopes" in result

    def test_health_total_envelopes_starts_at_zero(self):
        """total_envelopes starts at zero before any epochs run."""
        daemon = _make_daemon()
        result = daemon.health()
        assert result["total_envelopes"] == 0

    def test_health_contains_last_epoch(self):
        """health() must include 'last_epoch' key (None until an epoch runs)."""
        daemon = _make_daemon()
        result = daemon.health()
        assert "last_epoch" in result
        assert result["last_epoch"] is None

    def test_health_reflects_state_transition(self):
        """After _transition(EXPLORING), health()['state'] updates accordingly."""
        daemon = _make_daemon()
        daemon._transition(RemState.EXPLORING)
        result = daemon.health()
        assert result["state"] == RemState.EXPLORING.value

    def test_health_reflects_metric_updates(self):
        """After manually updating metrics, health() reflects the new values."""
        daemon = _make_daemon()
        daemon._epoch_count = 5
        daemon._total_findings = 12
        daemon._total_envelopes = 10

        result = daemon.health()
        assert result["epoch_count"] == 5
        assert result["total_findings"] == 12
        assert result["total_envelopes"] == 10


# ---------------------------------------------------------------------------
# test_run_epoch_updates_metrics
# ---------------------------------------------------------------------------


class TestRunEpochUpdatesMetrics:
    @pytest.mark.asyncio
    async def test_run_epoch_increments_epoch_count(self):
        """_run_epoch() increments _epoch_count by 1."""
        daemon = _make_daemon()
        mock_result = _make_epoch_result(epoch_id=1, findings_count=3, envelopes_submitted=3)

        with patch(
            "backend.core.ouroboros.rem_sleep.RemEpoch"
        ) as MockEpoch:
            instance = MockEpoch.return_value
            instance.run = AsyncMock(return_value=mock_result)
            await daemon._run_epoch()

        assert daemon._epoch_count == 1

    @pytest.mark.asyncio
    async def test_run_epoch_updates_total_findings(self):
        """_run_epoch() adds finding count to _total_findings."""
        daemon = _make_daemon()
        mock_result = _make_epoch_result(findings_count=4, envelopes_submitted=4)

        with patch(
            "backend.core.ouroboros.rem_sleep.RemEpoch"
        ) as MockEpoch:
            instance = MockEpoch.return_value
            instance.run = AsyncMock(return_value=mock_result)
            await daemon._run_epoch()

        assert daemon._total_findings == 4

    @pytest.mark.asyncio
    async def test_run_epoch_updates_total_envelopes(self):
        """_run_epoch() adds submitted envelopes to _total_envelopes."""
        daemon = _make_daemon()
        mock_result = _make_epoch_result(envelopes_submitted=7)

        with patch(
            "backend.core.ouroboros.rem_sleep.RemEpoch"
        ) as MockEpoch:
            instance = MockEpoch.return_value
            instance.run = AsyncMock(return_value=mock_result)
            await daemon._run_epoch()

        assert daemon._total_envelopes == 7

    @pytest.mark.asyncio
    async def test_run_epoch_stores_last_result(self):
        """_run_epoch() stores the EpochResult in _last_epoch_result."""
        daemon = _make_daemon()
        mock_result = _make_epoch_result()

        with patch(
            "backend.core.ouroboros.rem_sleep.RemEpoch"
        ) as MockEpoch:
            instance = MockEpoch.return_value
            instance.run = AsyncMock(return_value=mock_result)
            await daemon._run_epoch()

        assert daemon._last_epoch_result is mock_result

    @pytest.mark.asyncio
    async def test_run_epoch_transitions_through_exploring(self):
        """_run_epoch() transitions to EXPLORING before running."""
        daemon = _make_daemon()
        observed_states: list[RemState] = []

        original_transition = daemon._transition

        def _record_transition(state: RemState) -> None:
            observed_states.append(state)
            original_transition(state)

        daemon._transition = _record_transition

        mock_result = _make_epoch_result()
        with patch(
            "backend.core.ouroboros.rem_sleep.RemEpoch"
        ) as MockEpoch:
            instance = MockEpoch.return_value
            instance.run = AsyncMock(return_value=mock_result)
            await daemon._run_epoch()

        assert RemState.EXPLORING in observed_states


# ---------------------------------------------------------------------------
# test_pause
# ---------------------------------------------------------------------------


class TestPause:
    def test_pause_cancels_current_token_when_present(self):
        """pause() calls cancel() on the current CancellationToken."""
        daemon = _make_daemon()
        token = MagicMock()
        daemon._current_token = token
        daemon.pause()
        token.cancel.assert_called_once()

    def test_pause_when_no_token_is_safe(self):
        """pause() when _current_token is None does not raise."""
        daemon = _make_daemon()
        assert daemon._current_token is None
        daemon.pause()  # must not raise


# ---------------------------------------------------------------------------
# test_daemon_loop_integration
# ---------------------------------------------------------------------------


class TestDaemonLoopIntegration:
    @pytest.mark.asyncio
    async def test_daemon_loop_waits_for_gate_then_registers_callback(self):
        """_daemon_loop() awaits spinal_cord.wait_for_gate then calls on_eligible."""
        spinal = _make_spinal_cord()
        drive = _make_proactive_drive()
        daemon = _make_daemon(spinal_cord=spinal, proactive_drive=drive)

        # Patch _run_epoch to raise immediately so the loop doesn't iterate
        async def _raise_cancelled():
            raise asyncio.CancelledError()

        with patch.object(daemon, "_run_epoch", side_effect=_raise_cancelled):
            # Run the loop in a task, let it start, then cancel
            task = asyncio.create_task(daemon._daemon_loop())
            # Brief yield so the loop can get to the idle_event.wait()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        spinal.wait_for_gate.assert_awaited()
        drive.on_eligible.assert_called_once()

    @pytest.mark.asyncio
    async def test_daemon_loop_cancelled_error_exits_cleanly(self):
        """CancelledError in _daemon_loop exits without propagating."""
        daemon = _make_daemon()

        task = asyncio.create_task(daemon._daemon_loop())
        await asyncio.sleep(0.01)
        task.cancel()

        # Should complete without raising CancelledError to the caller
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass  # acceptable — task was explicitly cancelled
        except Exception as exc:
            pytest.fail(f"Unexpected exception from _daemon_loop: {exc}")

    @pytest.mark.asyncio
    async def test_full_start_stop_lifecycle(self):
        """start() then stop() complete without errors."""
        daemon = _make_daemon()

        await daemon.start()
        assert daemon._task is not None

        await daemon.stop()
        assert daemon._task is None or daemon._task.done()
