"""
Comprehensive tests for BackgroundRecovery with adaptive backoff.

Tests cover:
- Starts in IDLE state
- Calls recover_fn until success
- on_success callback called
- Pauses after max_attempts (safety valve)
- Pauses after max_total_time (safety valve)
- resume() after pause works
- notify_conditions_changed() speeds up next attempt
- stop() cancels gracefully
- Double start/stop is safe
- State transitions
- Exponential backoff with jitter
"""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest
import time

from backend.core.resilience.recovery import (
    RecoveryConfig,
    BackgroundRecovery,
)
from backend.core.resilience.types import RecoveryState


class TestRecoveryConfigDefaults:
    """Tests for RecoveryConfig default values."""

    def test_default_base_delay(self):
        """Default base_delay should be 5.0."""
        config = RecoveryConfig()
        assert config.base_delay == 5.0

    def test_default_max_delay(self):
        """Default max_delay should be 300.0."""
        config = RecoveryConfig()
        assert config.max_delay == 300.0

    def test_default_exponential_base(self):
        """Default exponential_base should be 2.0."""
        config = RecoveryConfig()
        assert config.exponential_base == 2.0

    def test_default_jitter(self):
        """Default jitter should be 0.1."""
        config = RecoveryConfig()
        assert config.jitter == 0.1

    def test_default_timeout(self):
        """Default timeout should be 30.0."""
        config = RecoveryConfig()
        assert config.timeout == 30.0

    def test_default_max_attempts(self):
        """Default max_attempts should be None (unlimited)."""
        config = RecoveryConfig()
        assert config.max_attempts is None

    def test_default_max_total_time(self):
        """Default max_total_time should be None (unlimited)."""
        config = RecoveryConfig()
        assert config.max_total_time is None

    def test_default_speedup_factor(self):
        """Default speedup_factor should be 0.25."""
        config = RecoveryConfig()
        assert config.speedup_factor == 0.25


class TestBackgroundRecoveryState:
    """Tests for BackgroundRecovery state management."""

    def test_starts_in_idle_state(self):
        """BackgroundRecovery should start in IDLE state."""
        recover_fn = AsyncMock(return_value=True)
        recovery = BackgroundRecovery(recover_fn=recover_fn)
        assert recovery.state == RecoveryState.IDLE

    def test_attempt_count_starts_at_zero(self):
        """attempt_count should start at 0."""
        recover_fn = AsyncMock(return_value=True)
        recovery = BackgroundRecovery(recover_fn=recover_fn)
        assert recovery.attempt_count == 0


class TestBackgroundRecoverySuccess:
    """Tests for successful recovery scenarios."""

    @pytest.mark.asyncio
    async def test_calls_recover_fn_until_success(self):
        """Should call recover_fn repeatedly until it returns True."""
        call_count = 0

        async def recover_fn():
            nonlocal call_count
            call_count += 1
            return call_count >= 3  # Succeed on 3rd attempt

        config = RecoveryConfig(base_delay=0.001, jitter=0.0)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()
        # Wait for recovery to complete (with small delays, no need to mock)
        for _ in range(100):
            if recovery.state == RecoveryState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.SUCCEEDED
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_on_success_callback_called(self):
        """on_success callback should be called when recovery succeeds."""
        recover_fn = AsyncMock(return_value=True)
        on_success = AsyncMock()

        config = RecoveryConfig(base_delay=0.001, jitter=0.0)
        recovery = BackgroundRecovery(
            recover_fn=recover_fn,
            config=config,
            on_success=on_success,
        )

        await recovery.start()
        # Wait for recovery to complete
        for _ in range(100):
            if recovery.state == RecoveryState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.SUCCEEDED
        on_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_immediate_success_first_attempt(self):
        """Recovery should succeed immediately if recover_fn returns True on first try."""
        recover_fn = AsyncMock(return_value=True)
        on_success = AsyncMock()

        config = RecoveryConfig(base_delay=0.001, jitter=0.0)
        recovery = BackgroundRecovery(
            recover_fn=recover_fn,
            config=config,
            on_success=on_success,
        )

        await recovery.start()
        # Wait for recovery to complete
        for _ in range(100):
            if recovery.state == RecoveryState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.SUCCEEDED
        assert recovery.attempt_count == 1
        recover_fn.assert_called_once()


class TestBackgroundRecoverySafetyValve:
    """Tests for safety valve behavior (max_attempts, max_total_time)."""

    @pytest.mark.asyncio
    async def test_pauses_after_max_attempts(self):
        """Should pause after reaching max_attempts."""
        recover_fn = AsyncMock(return_value=False)
        on_paused = AsyncMock()

        config = RecoveryConfig(
            base_delay=0.001,
            jitter=0.0,
            max_attempts=3,
        )
        recovery = BackgroundRecovery(
            recover_fn=recover_fn,
            config=config,
            on_paused=on_paused,
        )

        await recovery.start()
        # Wait for recovery to pause (small delays, no need to mock)
        for _ in range(200):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.PAUSED
        assert recovery.attempt_count == 3
        on_paused.assert_called_once()

    @pytest.mark.asyncio
    async def test_pauses_after_max_total_time(self):
        """Should pause after exceeding max_total_time."""
        recover_fn = AsyncMock(return_value=False)
        on_paused = AsyncMock()

        config = RecoveryConfig(
            base_delay=0.01,
            jitter=0.0,
            max_total_time=0.05,  # 50ms
        )
        recovery = BackgroundRecovery(
            recover_fn=recover_fn,
            config=config,
            on_paused=on_paused,
        )

        await recovery.start()
        # Wait for recovery to pause
        for _ in range(200):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.PAUSED
        on_paused.assert_called_once()
        # Should have made at least 1 attempt
        assert recovery.attempt_count >= 1


class TestBackgroundRecoveryResume:
    """Tests for resume() functionality."""

    @pytest.mark.asyncio
    async def test_resume_after_pause(self):
        """resume() should restart recovery after pause."""
        call_count = 0

        async def recover_fn():
            nonlocal call_count
            call_count += 1
            # Fail first 3, succeed on attempt 4 (after resume)
            return call_count > 3

        on_paused = AsyncMock()

        config = RecoveryConfig(
            base_delay=0.001,
            jitter=0.0,
            max_attempts=3,  # Will pause after 3 attempts
        )
        recovery = BackgroundRecovery(
            recover_fn=recover_fn,
            config=config,
            on_paused=on_paused,
        )

        # Start and wait for pause (small delays, no mocking needed)
        await recovery.start()
        for _ in range(200):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.PAUSED
        assert call_count == 3

        # Resume
        await recovery.resume()

        # Wait for success
        for _ in range(200):
            if recovery.state == RecoveryState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.SUCCEEDED
        assert call_count == 4  # One more attempt after resume

    @pytest.mark.asyncio
    async def test_resume_resets_attempt_count(self):
        """resume() should reset the attempt count."""
        recover_fn = AsyncMock(return_value=False)
        on_paused = AsyncMock()

        config = RecoveryConfig(
            base_delay=0.001,
            jitter=0.0,
            max_attempts=3,
        )
        recovery = BackgroundRecovery(
            recover_fn=recover_fn,
            config=config,
            on_paused=on_paused,
        )

        # Start and wait for pause (small delays, no mocking needed)
        await recovery.start()
        for _ in range(200):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.01)

        assert recovery.attempt_count == 3

        # Resume
        await recovery.resume()

        # Wait a bit for new attempts
        for _ in range(200):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.01)

        # After resume, attempts should have been reset and then counted again
        assert recovery.attempt_count == 3  # 3 new attempts after reset


class TestBackgroundRecoverySpeedup:
    """Tests for notify_conditions_changed() speedup functionality."""

    @pytest.mark.asyncio
    async def test_notify_conditions_changed_wakes_up_early(self):
        """notify_conditions_changed() should wake up recovery loop early."""
        call_count = 0
        timestamps = []

        async def recover_fn():
            nonlocal call_count
            call_count += 1
            timestamps.append(time.monotonic())
            return call_count >= 3

        config = RecoveryConfig(
            base_delay=10.0,  # Long delay
            jitter=0.0,
            speedup_factor=0.1,
        )
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()

        # Wait for first attempt
        await asyncio.sleep(0.05)
        assert call_count >= 1

        # Notify conditions changed to speed up
        recovery.notify_conditions_changed()

        # Wait a short time - should wake up early
        await asyncio.sleep(0.1)

        # Should have made at least one more attempt due to speedup
        assert call_count >= 2

        # Clean up
        await recovery.stop()

    @pytest.mark.asyncio
    async def test_speedup_factor_reduces_delay(self):
        """Speedup factor should reduce the delay significantly."""
        config = RecoveryConfig(
            base_delay=10.0,
            jitter=0.0,
            speedup_factor=0.25,
        )
        recover_fn = AsyncMock(return_value=False)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        # Calculate delay with speedup
        normal_delay = recovery._calculate_delay()

        # Signal speedup
        recovery._conditions_changed.set()
        speedup_delay = recovery._calculate_delay()

        assert speedup_delay < normal_delay
        assert speedup_delay == pytest.approx(normal_delay * config.speedup_factor, rel=0.01)


class TestBackgroundRecoveryStop:
    """Tests for stop() functionality."""

    @pytest.mark.asyncio
    async def test_stop_cancels_gracefully(self):
        """stop() should cancel the recovery loop gracefully."""
        recover_fn = AsyncMock(return_value=False)

        config = RecoveryConfig(
            base_delay=0.5,
            jitter=0.0,
        )
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()
        assert recovery.state == RecoveryState.RECOVERING

        await recovery.stop()

        assert recovery.state == RecoveryState.IDLE
        assert recovery._task is None

    @pytest.mark.asyncio
    async def test_double_start_is_safe(self):
        """Calling start() twice should be safe (idempotent)."""
        recover_fn = AsyncMock(return_value=False)

        config = RecoveryConfig(base_delay=0.5, jitter=0.0)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()
        task1 = recovery._task

        # Second start should not create a new task
        await recovery.start()
        task2 = recovery._task

        assert task1 is task2

        await recovery.stop()

    @pytest.mark.asyncio
    async def test_double_stop_is_safe(self):
        """Calling stop() twice should be safe (idempotent)."""
        recover_fn = AsyncMock(return_value=False)

        config = RecoveryConfig(base_delay=0.5, jitter=0.0)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()

        # Double stop should be safe
        await recovery.stop()
        await recovery.stop()

        assert recovery.state == RecoveryState.IDLE

    @pytest.mark.asyncio
    async def test_stop_on_idle_is_safe(self):
        """stop() on IDLE state should be safe."""
        recover_fn = AsyncMock(return_value=False)
        recovery = BackgroundRecovery(recover_fn=recover_fn)

        # Should not raise
        await recovery.stop()
        assert recovery.state == RecoveryState.IDLE


class TestBackgroundRecoveryStateTransitions:
    """Tests for state transitions."""

    @pytest.mark.asyncio
    async def test_idle_to_recovering(self):
        """start() should transition IDLE -> RECOVERING."""
        recover_fn = AsyncMock(return_value=False)
        config = RecoveryConfig(base_delay=1.0, jitter=0.0)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        assert recovery.state == RecoveryState.IDLE
        await recovery.start()
        assert recovery.state == RecoveryState.RECOVERING

        await recovery.stop()

    @pytest.mark.asyncio
    async def test_recovering_to_succeeded(self):
        """Successful recovery should transition RECOVERING -> SUCCEEDED."""
        recover_fn = AsyncMock(return_value=True)
        config = RecoveryConfig(base_delay=0.001, jitter=0.0)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()
        for _ in range(50):
            if recovery.state == RecoveryState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.SUCCEEDED

    @pytest.mark.asyncio
    async def test_recovering_to_paused(self):
        """Safety valve should transition RECOVERING -> PAUSED."""
        recover_fn = AsyncMock(return_value=False)
        config = RecoveryConfig(
            base_delay=0.001,
            jitter=0.0,
            max_attempts=2,
        )
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()
        for _ in range(200):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.PAUSED

    @pytest.mark.asyncio
    async def test_recovering_to_idle_on_stop(self):
        """stop() should transition RECOVERING -> IDLE."""
        recover_fn = AsyncMock(return_value=False)
        config = RecoveryConfig(base_delay=1.0, jitter=0.0)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()
        assert recovery.state == RecoveryState.RECOVERING

        await recovery.stop()
        assert recovery.state == RecoveryState.IDLE

    @pytest.mark.asyncio
    async def test_paused_to_recovering_on_resume(self):
        """resume() should transition PAUSED -> RECOVERING."""
        recover_fn = AsyncMock(return_value=False)
        config = RecoveryConfig(
            base_delay=0.001,
            jitter=0.0,
            max_attempts=1,
        )
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        await recovery.start()
        for _ in range(200):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.PAUSED

        await recovery.resume()
        assert recovery.state == RecoveryState.RECOVERING

        await recovery.stop()


class TestBackgroundRecoveryDelayCalculation:
    """Tests for delay calculation with exponential backoff and jitter."""

    def test_exponential_backoff(self):
        """Delay should grow exponentially with attempts."""
        config = RecoveryConfig(
            base_delay=1.0,
            exponential_base=2.0,
            jitter=0.0,
            max_delay=100.0,
        )
        recover_fn = AsyncMock(return_value=False)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        # Attempt 1: 1.0 * 2^0 = 1.0
        recovery._attempt_count = 1
        assert recovery._calculate_delay() == 1.0

        # Attempt 2: 1.0 * 2^1 = 2.0
        recovery._attempt_count = 2
        assert recovery._calculate_delay() == 2.0

        # Attempt 3: 1.0 * 2^2 = 4.0
        recovery._attempt_count = 3
        assert recovery._calculate_delay() == 4.0

    def test_max_delay_caps_growth(self):
        """Delay should be capped at max_delay."""
        config = RecoveryConfig(
            base_delay=1.0,
            exponential_base=2.0,
            max_delay=5.0,
            jitter=0.0,
        )
        recover_fn = AsyncMock(return_value=False)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)

        # Attempt 4: 1.0 * 2^3 = 8.0, capped to 5.0
        recovery._attempt_count = 4
        assert recovery._calculate_delay() == 5.0

    def test_jitter_adds_randomness(self):
        """Jitter should add randomness to the delay."""
        config = RecoveryConfig(
            base_delay=1.0,
            jitter=0.5,
            max_delay=100.0,
        )
        recover_fn = AsyncMock(return_value=False)
        recovery = BackgroundRecovery(recover_fn=recover_fn, config=config)
        recovery._attempt_count = 1

        delays = [recovery._calculate_delay() for _ in range(100)]
        # With 0.5 jitter around 1.0, range should be [0.5, 1.5]
        assert min(delays) >= 0.5
        assert max(delays) <= 1.5
        # Verify variation
        assert len(set(delays)) > 1


class TestBackgroundRecoveryTimeout:
    """Tests for per-attempt timeout behavior."""

    @pytest.mark.asyncio
    async def test_timeout_on_slow_recover_fn(self):
        """Slow recover_fn should be timed out."""
        async def slow_recover():
            await asyncio.sleep(10.0)  # Very slow
            return True

        config = RecoveryConfig(
            base_delay=0.001,
            jitter=0.0,
            timeout=0.05,  # 50ms timeout
            max_attempts=2,
        )
        on_paused = AsyncMock()
        recovery = BackgroundRecovery(
            recover_fn=slow_recover,
            config=config,
            on_paused=on_paused,
        )

        await recovery.start()
        for _ in range(100):
            if recovery.state == RecoveryState.PAUSED:
                break
            await asyncio.sleep(0.05)

        # Should have paused due to max_attempts after timeouts
        assert recovery.state == RecoveryState.PAUSED
        assert recovery.attempt_count == 2


class TestBackgroundRecoveryExceptionHandling:
    """Tests for exception handling in recover_fn."""

    @pytest.mark.asyncio
    async def test_exception_in_recover_fn_treated_as_failure(self):
        """Exception in recover_fn should be treated as failure."""
        call_count = 0

        async def failing_recover():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("Recovery failed")
            return True

        config = RecoveryConfig(base_delay=0.001, jitter=0.0)
        recovery = BackgroundRecovery(recover_fn=failing_recover, config=config)

        await recovery.start()
        for _ in range(200):
            if recovery.state == RecoveryState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

        assert recovery.state == RecoveryState.SUCCEEDED
        assert call_count == 3


class TestModuleExports:
    """Tests for module exports and structure."""

    def test_recovery_config_importable(self):
        """RecoveryConfig should be importable from recovery module."""
        from backend.core.resilience.recovery import RecoveryConfig
        assert RecoveryConfig is not None

    def test_background_recovery_importable(self):
        """BackgroundRecovery should be importable from recovery module."""
        from backend.core.resilience.recovery import BackgroundRecovery
        assert BackgroundRecovery is not None

    def test_exports_from_resilience_package(self):
        """RecoveryConfig and BackgroundRecovery should be exported from resilience package."""
        from backend.core.resilience import RecoveryConfig, BackgroundRecovery
        assert RecoveryConfig is not None
        assert BackgroundRecovery is not None
