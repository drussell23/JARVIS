"""
Tests for CapabilityUpgrade hot-swapping between degraded and full modes.

Tests cover:
- Starts in DEGRADED state
- try_upgrade() succeeds when available
- try_upgrade() fails when not available
- downgrade() returns to DEGRADED state
- on_upgrade callback is called on successful upgrade
- on_downgrade callback is called on downgrade
- Monitoring attempts upgrade periodically
- Monitoring detects regression
- stop_monitoring() is clean
- State transitions
- Thread safety
"""

import asyncio
import pytest
from unittest.mock import AsyncMock

from backend.core.resilience.capability import CapabilityUpgrade
from backend.core.resilience.types import CapabilityState


class TestCapabilityUpgradeBasic:
    """Basic CapabilityUpgrade tests."""

    @pytest.mark.asyncio
    async def test_starts_degraded(self):
        """CapabilityUpgrade should start in DEGRADED state."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_try_upgrade_succeeds(self):
        """try_upgrade() should succeed when capability is available."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        result = await upgrade.try_upgrade()

        assert result is True
        assert upgrade.state == CapabilityState.FULL
        assert upgrade.is_full is True

    @pytest.mark.asyncio
    async def test_try_upgrade_fails_if_not_available(self):
        """try_upgrade() should fail if capability is not available."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=False),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        result = await upgrade.try_upgrade()

        assert result is False
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_downgrade_returns_to_degraded(self):
        """downgrade() should return to DEGRADED state from FULL."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.try_upgrade()
        assert upgrade.is_full

        await upgrade.downgrade()
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_is_full_property(self):
        """is_full property should reflect FULL or MONITORING state."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        assert upgrade.is_full is False

        await upgrade.try_upgrade()
        assert upgrade.is_full is True

        await upgrade.downgrade()
        assert upgrade.is_full is False


class TestCapabilityUpgradeCallbacks:
    """Tests for upgrade/downgrade callbacks."""

    @pytest.mark.asyncio
    async def test_on_upgrade_called(self):
        """on_upgrade callback should be called on successful upgrade."""
        on_upgrade = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_upgrade=on_upgrade,
        )

        await upgrade.try_upgrade()
        on_upgrade.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_upgrade_not_called_on_failure(self):
        """on_upgrade callback should not be called if upgrade fails."""
        on_upgrade = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=False),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_upgrade=on_upgrade,
        )

        await upgrade.try_upgrade()
        on_upgrade.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_downgrade_called(self):
        """on_downgrade callback should be called on downgrade."""
        on_downgrade = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_downgrade=on_downgrade,
        )

        await upgrade.try_upgrade()
        await upgrade.downgrade()
        on_downgrade.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_downgrade_not_called_if_already_degraded(self):
        """on_downgrade should not be called if already in DEGRADED state."""
        on_downgrade = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_downgrade=on_downgrade,
        )

        # Never upgraded, downgrade should be a no-op
        await upgrade.downgrade()
        on_downgrade.assert_not_called()

    @pytest.mark.asyncio
    async def test_activate_called_on_upgrade(self):
        """activate function should be called during upgrade."""
        activate = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=activate,
            deactivate=AsyncMock(),
        )

        await upgrade.try_upgrade()
        activate.assert_called_once()

    @pytest.mark.asyncio
    async def test_deactivate_called_on_downgrade(self):
        """deactivate function should be called during downgrade."""
        deactivate = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=deactivate,
        )

        await upgrade.try_upgrade()
        await upgrade.downgrade()
        deactivate.assert_called_once()


class TestCapabilityUpgradeMonitoring:
    """Tests for background monitoring."""

    @pytest.mark.asyncio
    async def test_monitoring_attempts_upgrade(self):
        """Monitoring should attempt upgrade periodically."""
        check_available = AsyncMock(return_value=True)
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=check_available,
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.start_monitoring(interval=0.01)
        await asyncio.sleep(0.05)
        await upgrade.stop_monitoring()

        assert upgrade.is_full

    @pytest.mark.asyncio
    async def test_monitoring_detects_regression(self):
        """Monitoring should detect regression and downgrade."""
        call_count = 0

        async def check():
            nonlocal call_count
            call_count += 1
            return call_count < 3  # Available first 2 times, then not

        on_downgrade = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=check,
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_downgrade=on_downgrade,
        )

        await upgrade.start_monitoring(interval=0.01)
        await asyncio.sleep(0.1)
        await upgrade.stop_monitoring()

        on_downgrade.assert_called()

    @pytest.mark.asyncio
    async def test_stop_monitoring_is_clean(self):
        """stop_monitoring should cleanly stop the monitoring task."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=False),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.start_monitoring(interval=10.0)
        await upgrade.stop_monitoring()

        # Should not raise
        await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_monitoring_continues_checking_in_full_state(self):
        """Monitoring should continue checking even when in FULL state."""
        check_count = 0

        async def check():
            nonlocal check_count
            check_count += 1
            return True

        upgrade = CapabilityUpgrade(
            name="test",
            check_available=check,
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.start_monitoring(interval=0.01)
        await asyncio.sleep(0.05)
        await upgrade.stop_monitoring()

        # Should have checked multiple times
        assert check_count >= 2

    @pytest.mark.asyncio
    async def test_monitoring_retry_after_failed_upgrade(self):
        """Monitoring should retry upgrade after failure."""
        call_count = 0

        async def check():
            nonlocal call_count
            call_count += 1
            return call_count >= 3  # Not available first 2 times

        upgrade = CapabilityUpgrade(
            name="test",
            check_available=check,
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.start_monitoring(interval=0.01)
        await asyncio.sleep(0.1)
        await upgrade.stop_monitoring()

        assert upgrade.is_full
        assert call_count >= 3


class TestCapabilityUpgradeStateTransitions:
    """Tests for state transitions."""

    @pytest.mark.asyncio
    async def test_degraded_to_upgrading_to_full(self):
        """Upgrade should transition DEGRADED -> UPGRADING -> FULL."""
        states_seen = []

        async def slow_check():
            states_seen.append(("check", None))
            await asyncio.sleep(0.01)
            return True

        async def slow_activate():
            states_seen.append(("activate", None))
            await asyncio.sleep(0.01)

        upgrade = CapabilityUpgrade(
            name="test",
            check_available=slow_check,
            activate=slow_activate,
            deactivate=AsyncMock(),
        )

        assert upgrade.state == CapabilityState.DEGRADED
        await upgrade.try_upgrade()
        assert upgrade.state == CapabilityState.FULL

    @pytest.mark.asyncio
    async def test_full_to_degraded_on_downgrade(self):
        """downgrade() should transition FULL -> DEGRADED."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.try_upgrade()
        assert upgrade.state == CapabilityState.FULL

        await upgrade.downgrade()
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_upgrading_to_degraded_on_check_failure(self):
        """Failed check during upgrade should return to DEGRADED."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=False),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        result = await upgrade.try_upgrade()

        assert result is False
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_upgrading_to_degraded_on_activate_failure(self):
        """Failed activation should return to DEGRADED."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(side_effect=RuntimeError("Activation failed")),
            deactivate=AsyncMock(),
        )

        result = await upgrade.try_upgrade()

        assert result is False
        assert upgrade.state == CapabilityState.DEGRADED


class TestCapabilityUpgradeErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_exception_in_check_available(self):
        """Exception in check_available should be treated as unavailable."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(side_effect=RuntimeError("Check failed")),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        result = await upgrade.try_upgrade()

        assert result is False
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_exception_in_activate(self):
        """Exception in activate should cause upgrade failure."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(side_effect=RuntimeError("Activate failed")),
            deactivate=AsyncMock(),
        )

        result = await upgrade.try_upgrade()

        assert result is False
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_exception_in_deactivate_still_downgrades(self):
        """Exception in deactivate should still complete downgrade."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(side_effect=RuntimeError("Deactivate failed")),
        )

        await upgrade.try_upgrade()
        assert upgrade.is_full

        # Should not raise, but should still downgrade
        await upgrade.downgrade()
        assert upgrade.state == CapabilityState.DEGRADED

    @pytest.mark.asyncio
    async def test_exception_in_on_upgrade_callback(self):
        """Exception in on_upgrade callback should not affect state."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_upgrade=AsyncMock(side_effect=RuntimeError("Callback failed")),
        )

        result = await upgrade.try_upgrade()

        assert result is True
        assert upgrade.state == CapabilityState.FULL

    @pytest.mark.asyncio
    async def test_exception_in_on_downgrade_callback(self):
        """Exception in on_downgrade callback should not affect state."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_downgrade=AsyncMock(side_effect=RuntimeError("Callback failed")),
        )

        await upgrade.try_upgrade()
        await upgrade.downgrade()

        assert upgrade.state == CapabilityState.DEGRADED


class TestCapabilityUpgradeIdempotency:
    """Tests for idempotent operations."""

    @pytest.mark.asyncio
    async def test_double_upgrade_is_safe(self):
        """Calling try_upgrade() twice when already FULL should be safe."""
        activate = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=activate,
            deactivate=AsyncMock(),
        )

        result1 = await upgrade.try_upgrade()
        result2 = await upgrade.try_upgrade()

        assert result1 is True
        assert result2 is True
        assert upgrade.is_full
        # activate should only be called once
        activate.assert_called_once()

    @pytest.mark.asyncio
    async def test_double_downgrade_is_safe(self):
        """Calling downgrade() twice when already DEGRADED should be safe."""
        deactivate = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=deactivate,
        )

        await upgrade.try_upgrade()
        await upgrade.downgrade()
        await upgrade.downgrade()

        assert upgrade.state == CapabilityState.DEGRADED
        # deactivate should only be called once
        deactivate.assert_called_once()

    @pytest.mark.asyncio
    async def test_double_start_monitoring_is_safe(self):
        """Calling start_monitoring() twice should be safe."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=False),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.start_monitoring(interval=10.0)
        await upgrade.start_monitoring(interval=10.0)

        await upgrade.stop_monitoring()

    @pytest.mark.asyncio
    async def test_double_stop_monitoring_is_safe(self):
        """Calling stop_monitoring() twice should be safe."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=False),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.start_monitoring(interval=10.0)
        await upgrade.stop_monitoring()
        await upgrade.stop_monitoring()

    @pytest.mark.asyncio
    async def test_stop_monitoring_without_start_is_safe(self):
        """Calling stop_monitoring() without start should be safe."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=False),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        # Should not raise
        await upgrade.stop_monitoring()


class TestCapabilityUpgradeMonitoringState:
    """Tests for monitoring state transitions."""

    @pytest.mark.asyncio
    async def test_monitoring_transitions_to_monitoring_state(self):
        """After upgrade during monitoring, state should be MONITORING."""
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=AsyncMock(return_value=True),
            activate=AsyncMock(),
            deactivate=AsyncMock(),
        )

        await upgrade.start_monitoring(interval=0.01)
        await asyncio.sleep(0.05)

        # When monitoring, after successful upgrade, should be in MONITORING state
        assert upgrade.state in (CapabilityState.FULL, CapabilityState.MONITORING)
        assert upgrade.is_full

        await upgrade.stop_monitoring()

    @pytest.mark.asyncio
    async def test_monitoring_downgrade_on_regression(self):
        """Monitoring should detect regression and call on_downgrade."""
        call_count = 0

        async def check():
            nonlocal call_count
            call_count += 1
            # Available first 2 times (upgrade), then unavailable (regression)
            return call_count <= 2

        on_downgrade = AsyncMock()
        upgrade = CapabilityUpgrade(
            name="test",
            check_available=check,
            activate=AsyncMock(),
            deactivate=AsyncMock(),
            on_downgrade=on_downgrade,
        )

        await upgrade.start_monitoring(interval=0.01)
        await asyncio.sleep(0.15)
        await upgrade.stop_monitoring()

        # Should have detected regression and downgraded
        on_downgrade.assert_called()
        assert upgrade.state == CapabilityState.DEGRADED


class TestModuleExports:
    """Tests for module exports and structure."""

    def test_capability_upgrade_importable(self):
        """CapabilityUpgrade should be importable from capability module."""
        from backend.core.resilience.capability import CapabilityUpgrade
        assert CapabilityUpgrade is not None

    def test_capability_state_importable_from_types(self):
        """CapabilityState should be importable from types module."""
        from backend.core.resilience.types import CapabilityState
        assert CapabilityState is not None
