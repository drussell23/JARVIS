"""Tests for restart coordinator backoff and quarantine."""
import asyncio
import time
import pytest
from unittest.mock import patch

from backend.core.supervisor.restart_coordinator import (
    RestartCoordinator,
    RestartSource,
    RestartUrgency,
)


@pytest.fixture
def coordinator():
    c = RestartCoordinator()
    c._grace_period_ended = True  # Skip startup grace period in tests
    c._startup_time = time.time() - 9999  # Ensure grace period has elapsed
    return c


@pytest.mark.asyncio
class TestRestartBackoff:
    async def test_first_restart_accepted(self, coordinator):
        """First restart should always be accepted."""
        await coordinator.initialize()
        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="test",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result is True

    async def test_rapid_restart_blocked_by_cooldown(self, coordinator):
        """Second rapid restart within cooldown should be blocked."""
        await coordinator.initialize()
        result1 = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="crash 1",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result1 is True

        # Reset internal state so it's not "already restarting"
        coordinator._is_restarting = False
        coordinator._current_request = None

        # Second restart immediately -- should be blocked by cooldown
        result2 = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="crash 2",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result2 is False  # Blocked by cooldown

    async def test_critical_bypasses_cooldown(self, coordinator):
        """CRITICAL urgency bypasses all backoff and quarantine."""
        await coordinator.initialize()
        await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="crash 1",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        coordinator._is_restarting = False
        coordinator._current_request = None

        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="security emergency",
            urgency=RestartUrgency.CRITICAL,
            countdown_seconds=0,
        )
        assert result is True

    async def test_user_request_no_backoff(self, coordinator):
        """USER_REQUEST source should not have backoff."""
        await coordinator.initialize()
        await coordinator.request_restart(
            source=RestartSource.USER_REQUEST,
            reason="user restart 1",
            urgency=RestartUrgency.MEDIUM,
            countdown_seconds=0,
        )
        coordinator._is_restarting = False
        coordinator._current_request = None

        result = await coordinator.request_restart(
            source=RestartSource.USER_REQUEST,
            reason="user restart 2",
            urgency=RestartUrgency.MEDIUM,
            countdown_seconds=0,
        )
        assert result is True  # User requests bypass backoff

    async def test_quarantine_after_many_restarts(self, coordinator):
        """After QUARANTINE_THRESHOLD restarts, system enters quarantine."""
        await coordinator.initialize()
        coordinator._backoff_reset_healthy_s = 9999  # Prevent reset

        # Simulate multiple rapid restarts
        from backend.core.time_utils import monotonic_s
        coordinator._restart_count_total = coordinator._quarantine_threshold
        coordinator._last_restart_mono = monotonic_s()

        coordinator._is_restarting = False
        coordinator._current_request = None
        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="one too many",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result is False
        assert coordinator._quarantine_until > 0

    async def test_quarantine_expires(self, coordinator):
        """Quarantine should expire after duration."""
        await coordinator.initialize()
        from backend.core.time_utils import monotonic_s
        coordinator._quarantine_until = monotonic_s() - 1.0  # In the past

        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="after quarantine",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result is True

    async def test_classify_crash_source(self, coordinator):
        """HEALTH_CHECK source should classify as 'crash'."""
        category = coordinator._classify_restart_source(RestartSource.HEALTH_CHECK)
        assert category == "crash"

    async def test_classify_user_source(self, coordinator):
        """USER_REQUEST source should classify as 'user'."""
        category = coordinator._classify_restart_source(RestartSource.USER_REQUEST)
        assert category == "user"

    async def test_classify_dependency_source(self, coordinator):
        """DEPENDENCY_UPDATE source should classify as 'dependency'."""
        category = coordinator._classify_restart_source(RestartSource.DEPENDENCY_UPDATE)
        assert category == "dependency"
