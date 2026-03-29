"""End-to-end integration tests for OuroborosDaemon lifecycle.

These tests exercise the full awaken → health-check → metrics → shutdown flow
using a complete mock stack.  No network, no model calls, no real I/O — all
collaborators are replaced with MagicMock / AsyncMock.

The file deliberately tests the *assembled system* rather than individual units,
validating that the three phases (VitalScan, SpinalCord, RemSleepDaemon) compose
correctly through the OuroborosDaemon orchestrator.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.daemon import AwakeningReport, OuroborosDaemon
from backend.core.ouroboros.daemon_config import DaemonConfig
from backend.core.ouroboros.rem_sleep import RemState
from backend.core.ouroboros.spinal_cord import SpinalStatus
from backend.core.ouroboros.vital_scan import VitalStatus


# ---------------------------------------------------------------------------
# Mock-stack helpers
# ---------------------------------------------------------------------------


def _full_mock_stack(*, rem_enabled: bool = True) -> dict:
    """Return a fully wired mock dependency stack for OuroborosDaemon.

    All async entry-points are AsyncMock so they are awaitable.
    The oracle returns no circular deps and has a fresh cache, so Phase 1
    produces VitalStatus.PASS by default.
    The event_stream.broadcast_event returns 1 subscriber so Phase 2 reaches
    SpinalStatus.CONNECTED.
    """
    oracle = MagicMock()
    oracle.find_circular_dependencies.return_value = []
    oracle._last_indexed_monotonic_ns = 1          # non-zero → "has been indexed"
    oracle.index_age_s.return_value = 0.0          # fresh cache → no stale warning

    fleet = AsyncMock()
    fleet.deploy = AsyncMock(
        return_value=MagicMock(
            findings=[
                MagicMock(
                    description="Unwired: PredictivePlanningAgent",
                    category="unwired_component",
                    file_path="backend/intelligence/predictive_planning.py",
                    relevance=0.9,
                    repo="jarvis",
                )
            ],
            total_findings=1,
            agents_deployed=5,
            agents_completed=5,
        )
    )

    event_stream = MagicMock()
    event_stream.broadcast_event = AsyncMock(return_value=1)

    intake_router = AsyncMock()
    intake_router.ingest = AsyncMock(return_value="enqueued")

    proactive_drive = MagicMock()
    proactive_drive.on_eligible = MagicMock()

    health_sensor = AsyncMock()
    health_sensor.scan_once = AsyncMock(return_value=[])

    config = DaemonConfig(
        daemon_enabled=True,
        rem_enabled=rem_enabled,
        rem_cooldown_s=0.1,
        vital_scan_timeout_s=5.0,
        spinal_timeout_s=5.0,
        rem_epoch_timeout_s=5.0,
        rem_max_agents=2,
        rem_max_findings_per_epoch=5,
        rem_idle_eligible_s=0.01,
    )

    return dict(
        oracle=oracle,
        fleet=fleet,
        bg_pool=MagicMock(start=AsyncMock()),
        intake_router=intake_router,
        event_stream=event_stream,
        proactive_drive=proactive_drive,
        doubleword=None,
        gls=MagicMock(),
        health_sensor=health_sensor,
        config=config,
    )


def _build_daemon(stack: dict) -> OuroborosDaemon:
    """Construct an OuroborosDaemon from a stack dict."""
    return OuroborosDaemon(
        oracle=stack["oracle"],
        fleet=stack["fleet"],
        bg_pool=stack["bg_pool"],
        intake_router=stack["intake_router"],
        event_stream=stack["event_stream"],
        proactive_drive=stack["proactive_drive"],
        doubleword=stack["doubleword"],
        gls=stack["gls"],
        config=stack["config"],
        health_sensor=stack["health_sensor"],
    )


# ---------------------------------------------------------------------------
# TestFullLifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """Full awaken → health-check → metrics → shutdown integration."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_awaken_returns_pass_and_connected(self):
        """awaken() should yield PASS vital status and CONNECTED spinal status."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {
                "state": RemState.IDLE_WATCH.value,
                "epoch_count": 0,
                "total_findings": 0,
                "total_envelopes": 0,
                "last_epoch": None,
            }

            report = await daemon.awaken()

        assert isinstance(report, AwakeningReport)
        assert report.vital_status is VitalStatus.PASS
        assert report.spinal_status is SpinalStatus.CONNECTED
        assert report.rem_started is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_health_dict_structure_after_awaken(self):
        """health() after awaken() must have awakened=True and correct key types."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {
                "state": RemState.IDLE_WATCH.value,
                "epoch_count": 0,
                "total_findings": 0,
                "total_envelopes": 0,
                "last_epoch": None,
            }

            await daemon.awaken()
            h = daemon.health()

        assert h["awakened"] is True
        assert isinstance(h["vital_status"], str)
        assert isinstance(h["spinal_status"], str)
        assert "rem" in h

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_metrics_epoch_count_zero_before_any_epoch(self):
        """metrics()['epoch_count'] is 0 immediately after awaken() with no epochs run."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {
                "epoch_count": 0,
                "total_findings": 0,
                "total_envelopes": 0,
            }

            await daemon.awaken()
            m = daemon.metrics()

        assert m["epoch_count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_metrics_has_expected_keys(self):
        """metrics() must contain epoch_count, total_findings, total_envelopes, vital_findings."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {
                "epoch_count": 0,
                "total_findings": 0,
                "total_envelopes": 0,
            }

            await daemon.awaken()
            m = daemon.metrics()

        assert "epoch_count" in m
        assert "total_findings" in m
        assert "total_envelopes" in m
        assert "vital_findings" in m

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_shutdown_after_awaken_completes_cleanly(self):
        """shutdown() after awaken() must not raise and must clear the REM reference."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()
            await daemon.shutdown()

        assert daemon._rem is None


# ---------------------------------------------------------------------------
# TestVitalWarnQueuesForRem
# ---------------------------------------------------------------------------


class TestVitalWarnQueuesForRem:
    """Non-kernel circular deps produce WARN, but REM still starts."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_non_kernel_cycle_yields_warn_status(self):
        """A cycle in a non-kernel file → vital_status == WARN."""
        stack = _full_mock_stack()

        # Return a cycle involving a non-kernel file
        cycle_node = MagicMock()
        cycle_node.file_path = "backend/agents/circular.py"
        stack["oracle"].find_circular_dependencies.return_value = [[cycle_node, cycle_node]]

        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            report = await daemon.awaken()

        assert report.vital_status is VitalStatus.WARN

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_vital_warn_does_not_block_rem_start(self):
        """Even with a WARN vital status, rem_started should be True."""
        stack = _full_mock_stack()

        cycle_node = MagicMock()
        cycle_node.file_path = "backend/agents/circular.py"
        stack["oracle"].find_circular_dependencies.return_value = [[cycle_node, cycle_node]]

        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            report = await daemon.awaken()

        assert report.rem_started is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_vital_warn_then_shutdown_is_safe(self):
        """Shutdown after a WARN-vital awaken does not raise."""
        stack = _full_mock_stack()

        cycle_node = MagicMock()
        cycle_node.file_path = "backend/agents/circular.py"
        stack["oracle"].find_circular_dependencies.return_value = [[cycle_node, cycle_node]]

        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()
            await daemon.shutdown()   # must not raise


# ---------------------------------------------------------------------------
# TestRemDisabled
# ---------------------------------------------------------------------------


class TestRemDisabled:
    """Daemon operates correctly when REM is disabled in config."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rem_disabled_sets_rem_started_false(self):
        """rem_started in AwakeningReport is False when rem_enabled=False."""
        stack = _full_mock_stack(rem_enabled=False)
        daemon = _build_daemon(stack)

        report = await daemon.awaken()

        assert report.rem_started is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rem_disabled_health_rem_key_is_none(self):
        """health()['rem'] is None when REM was not started."""
        stack = _full_mock_stack(rem_enabled=False)
        daemon = _build_daemon(stack)

        await daemon.awaken()
        h = daemon.health()

        assert h["rem"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rem_disabled_shutdown_does_not_raise(self):
        """shutdown() when REM was never started must not raise."""
        stack = _full_mock_stack(rem_enabled=False)
        daemon = _build_daemon(stack)

        await daemon.awaken()
        await daemon.shutdown()   # must not raise

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rem_disabled_vital_and_spinal_still_run(self):
        """Phase 1 and Phase 2 still complete even when Phase 3 is disabled."""
        stack = _full_mock_stack(rem_enabled=False)
        daemon = _build_daemon(stack)

        report = await daemon.awaken()

        assert report.vital_status is VitalStatus.PASS
        assert report.spinal_status is SpinalStatus.CONNECTED


# ---------------------------------------------------------------------------
# TestShutdownBeforeAwaken
# ---------------------------------------------------------------------------


class TestShutdownBeforeAwaken:
    """Calling shutdown before awaken is a documented safe no-op."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_shutdown_before_awaken_does_not_raise(self):
        """shutdown() before awaken() must complete without raising."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        await daemon.shutdown()   # must not raise

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_shutdown_before_awaken_leaves_daemon_pristine(self):
        """After an early shutdown, health() still reports awakened=False."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        await daemon.shutdown()

        h = daemon.health()
        assert h["awakened"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiple_shutdowns_before_awaken_are_safe(self):
        """Calling shutdown twice before awaken is safe (idempotent)."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        await daemon.shutdown()
        await daemon.shutdown()   # second call — must not raise


# ---------------------------------------------------------------------------
# TestIdempotentAwaken
# ---------------------------------------------------------------------------


class TestIdempotentAwaken:
    """awaken() is idempotent — second call returns same cached report."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_awaken_twice_returns_same_vital_status(self):
        """r1.vital_status == r2.vital_status across two awaken() calls."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            r1 = await daemon.awaken()
            r2 = await daemon.awaken()

        assert r1.vital_status == r2.vital_status

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_awaken_twice_returns_same_object(self):
        """awaken() called twice returns the exact same AwakeningReport instance."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            r1 = await daemon.awaken()
            r2 = await daemon.awaken()

        assert r1 is r2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_awaken_twice_does_not_create_second_rem(self):
        """Second awaken() call must not construct a new RemSleepDaemon."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()
            await daemon.shutdown()

            # Rebuild a fresh daemon for second awaken test
            daemon2 = _build_daemon(stack)
            await daemon2.awaken()
            await daemon2.awaken()   # idempotent

        # Constructor should have been called at most once per daemon instance
        assert MockRem.call_count <= 2   # one per daemon, not two per daemon

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_health_consistent_after_double_awaken(self):
        """health()['awakened'] remains True on repeated awaken calls."""
        stack = _full_mock_stack()
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()
            await daemon.awaken()

        assert daemon.health()["awakened"] is True


# ---------------------------------------------------------------------------
# TestDegradedSpinalIntegration
# ---------------------------------------------------------------------------


class TestDegradedSpinalIntegration:
    """Verify degraded event stream does not prevent awakening."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_degraded_stream_yields_degraded_spinal_status(self):
        """When broadcast_event raises, spinal_status == DEGRADED."""
        stack = _full_mock_stack()
        stack["event_stream"].broadcast_event = AsyncMock(
            side_effect=Exception("transport unavailable")
        )
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            report = await daemon.awaken()

        assert report.spinal_status is SpinalStatus.DEGRADED

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_degraded_stream_still_starts_rem(self):
        """Even with DEGRADED spinal status, REM should start (SpinalGate is always set)."""
        stack = _full_mock_stack()
        stack["event_stream"].broadcast_event = AsyncMock(
            side_effect=Exception("transport unavailable")
        )
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            report = await daemon.awaken()

        assert report.rem_started is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_degraded_stream_full_lifecycle(self):
        """Full lifecycle with a broken transport completes without exception."""
        stack = _full_mock_stack()
        stack["event_stream"].broadcast_event = AsyncMock(
            side_effect=Exception("transport unavailable")
        )
        daemon = _build_daemon(stack)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()
            _ = daemon.health()
            _ = daemon.metrics()
            await daemon.shutdown()
