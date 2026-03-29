"""Tests for OuroborosDaemon — Zone 7.0 top-level orchestrator (TDD).

All tests are pure-asyncio with zero network, zero model calls, and zero I/O.
Dependencies are fully mocked via MagicMock / AsyncMock.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.daemon import AwakeningReport, OuroborosDaemon
from backend.core.ouroboros.daemon_config import DaemonConfig
from backend.core.ouroboros.spinal_cord import SpinalStatus
from backend.core.ouroboros.vital_scan import VitalReport, VitalStatus


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_oracle() -> MagicMock:
    """Return a minimal oracle mock (find_circular_dependencies → [])."""
    oracle = MagicMock()
    oracle.find_circular_dependencies.return_value = []
    oracle._last_indexed_monotonic_ns = 1
    oracle.index_age_s.return_value = 0.0
    return oracle


def _make_event_stream() -> MagicMock:
    """Return an event stream mock whose broadcast_event always succeeds."""
    stream = MagicMock()
    stream.broadcast_event = AsyncMock(return_value=1)
    return stream


def _make_health_sensor() -> AsyncMock:
    """Return a health sensor mock whose scan_once returns no findings."""
    sensor = AsyncMock()
    sensor.scan_once = AsyncMock(return_value=[])
    return sensor


def _make_config(*, rem_enabled: bool = True) -> DaemonConfig:
    """Build a fast DaemonConfig suitable for tests."""
    return DaemonConfig(
        daemon_enabled=True,
        vital_scan_timeout_s=5.0,
        spinal_timeout_s=5.0,
        rem_enabled=rem_enabled,
        rem_cooldown_s=0.01,
        rem_epoch_timeout_s=5.0,
        rem_max_agents=2,
        rem_max_findings_per_epoch=5,
        rem_idle_eligible_s=0.01,
    )


def _make_daemon(
    *,
    oracle: Any = None,
    event_stream: Any = None,
    health_sensor: Any = None,
    config: DaemonConfig | None = None,
    rem_enabled: bool = True,
) -> OuroborosDaemon:
    """Construct an OuroborosDaemon with all heavyweight deps mocked."""
    return OuroborosDaemon(
        oracle=oracle or _make_oracle(),
        fleet=MagicMock(),
        bg_pool=MagicMock(),
        intake_router=MagicMock(),
        event_stream=event_stream or _make_event_stream(),
        proactive_drive=MagicMock(),
        doubleword=MagicMock(),
        gls=MagicMock(),
        config=config or _make_config(rem_enabled=rem_enabled),
        health_sensor=health_sensor or _make_health_sensor(),
    )


# ---------------------------------------------------------------------------
# AwakeningReport unit tests
# ---------------------------------------------------------------------------


class TestAwakeningReport:
    def test_is_dataclass_with_expected_fields(self):
        """AwakeningReport must expose vital_status, vital_report, spinal_status, rem_started."""
        report = AwakeningReport(
            vital_status=VitalStatus.PASS,
            vital_report=MagicMock(spec=VitalReport),
            spinal_status=SpinalStatus.CONNECTED,
            rem_started=True,
        )
        assert report.vital_status is VitalStatus.PASS
        assert report.spinal_status is SpinalStatus.CONNECTED
        assert report.rem_started is True


# ---------------------------------------------------------------------------
# test_awaken_returns_report
# ---------------------------------------------------------------------------


class TestAwakenReturnsReport:
    @pytest.mark.asyncio
    async def test_awaken_returns_awakening_report(self):
        """awaken() must return an AwakeningReport with correct types."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            report = await daemon.awaken()

        assert isinstance(report, AwakeningReport)
        assert isinstance(report.vital_status, VitalStatus)
        assert isinstance(report.vital_report, VitalReport)
        assert isinstance(report.spinal_status, SpinalStatus)
        assert isinstance(report.rem_started, bool)

    @pytest.mark.asyncio
    async def test_awaken_vital_status_reflects_scan(self):
        """vital_status in report comes from Phase 1 VitalScan."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            report = await daemon.awaken()

        # With no cycles and an empty health sensor, we expect PASS
        assert report.vital_status is VitalStatus.PASS

    @pytest.mark.asyncio
    async def test_awaken_spinal_status_connected(self):
        """spinal_status in report is CONNECTED when event stream is healthy."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            report = await daemon.awaken()

        assert report.spinal_status is SpinalStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_awaken_rem_started_true_when_enabled(self):
        """rem_started is True when config.rem_enabled=True."""
        daemon = _make_daemon(rem_enabled=True)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            report = await daemon.awaken()

        assert report.rem_started is True


# ---------------------------------------------------------------------------
# test_awaken_is_idempotent
# ---------------------------------------------------------------------------


class TestAwakenIsIdempotent:
    @pytest.mark.asyncio
    async def test_awaken_is_idempotent(self):
        """Calling awaken() twice must return the exact same AwakeningReport object."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            report1 = await daemon.awaken()
            report2 = await daemon.awaken()

        assert report1 is report2

    @pytest.mark.asyncio
    async def test_awaken_idempotent_does_not_restart_rem(self):
        """Second awaken() call must not create a new RemSleepDaemon."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            await daemon.awaken()
            await daemon.awaken()

        # RemSleepDaemon constructor should be called at most once
        assert MockRem.call_count <= 1


# ---------------------------------------------------------------------------
# test_awaken_with_rem_disabled
# ---------------------------------------------------------------------------


class TestAwakenWithRemDisabled:
    @pytest.mark.asyncio
    async def test_awaken_with_rem_disabled(self):
        """config.rem_enabled=False → rem_started=False in report."""
        daemon = _make_daemon(rem_enabled=False)

        # No patching needed since RemSleepDaemon should NOT be instantiated
        report = await daemon.awaken()

        assert report.rem_started is False

    @pytest.mark.asyncio
    async def test_awaken_rem_disabled_does_not_create_rem_daemon(self):
        """When rem_enabled=False, RemSleepDaemon is never constructed."""
        daemon = _make_daemon(rem_enabled=False)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            await daemon.awaken()

        MockRem.assert_not_called()


# ---------------------------------------------------------------------------
# test_awaken_then_shutdown
# ---------------------------------------------------------------------------


class TestAwakenThenShutdown:
    @pytest.mark.asyncio
    async def test_awaken_then_shutdown_no_errors(self):
        """awaken() followed by shutdown() must complete without raising."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            await daemon.awaken()
            await daemon.shutdown()  # must not raise

    @pytest.mark.asyncio
    async def test_shutdown_stops_rem_daemon(self):
        """shutdown() must call stop() on the RemSleepDaemon if it was started."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            await daemon.awaken()
            await daemon.shutdown()

        instance.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_before_awaken_is_safe(self):
        """Calling shutdown() before awaken() must not raise."""
        daemon = _make_daemon()
        await daemon.shutdown()  # must not raise

    @pytest.mark.asyncio
    async def test_shutdown_clears_rem_reference(self):
        """After shutdown(), the internal _rem reference is set to None."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            await daemon.awaken()
            await daemon.shutdown()

        assert daemon._rem is None


# ---------------------------------------------------------------------------
# test_health_after_awaken
# ---------------------------------------------------------------------------


class TestHealthAfterAwaken:
    @pytest.mark.asyncio
    async def test_health_after_awaken_has_expected_keys(self):
        """health() after awaken() must include awakened, vital_status, spinal_status, rem."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {
                "state": "idle_watch",
                "epoch_count": 0,
                "total_findings": 0,
                "total_envelopes": 0,
                "last_epoch": None,
            }

            await daemon.awaken()
            h = daemon.health()

        assert "awakened" in h
        assert "vital_status" in h
        assert "spinal_status" in h
        assert "rem" in h

    @pytest.mark.asyncio
    async def test_health_awakened_true_after_awaken(self):
        """health()['awakened'] must be True after awaken()."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()

        assert daemon.health()["awakened"] is True

    def test_health_awakened_false_before_awaken(self):
        """health()['awakened'] must be False before awaken() is called."""
        daemon = _make_daemon()
        assert daemon.health()["awakened"] is False

    @pytest.mark.asyncio
    async def test_health_vital_status_is_string(self):
        """health()['vital_status'] must be a string (the enum value)."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()

        h = daemon.health()
        assert isinstance(h["vital_status"], str)

    @pytest.mark.asyncio
    async def test_health_spinal_status_is_string(self):
        """health()['spinal_status'] must be a string (the enum value)."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()

        h = daemon.health()
        assert isinstance(h["spinal_status"], str)


# ---------------------------------------------------------------------------
# test_health_before_awaken
# ---------------------------------------------------------------------------


class TestHealthBeforeAwaken:
    def test_health_before_awaken_returns_dict(self):
        """health() must return a dict even before awaken() is called."""
        daemon = _make_daemon()
        h = daemon.health()
        assert isinstance(h, dict)

    def test_health_before_awaken_vital_status_none(self):
        """vital_status must be None before awaken() runs Phase 1."""
        daemon = _make_daemon()
        assert daemon.health()["vital_status"] is None

    def test_health_before_awaken_spinal_status_none(self):
        """spinal_status must be None before awaken() runs Phase 2."""
        daemon = _make_daemon()
        assert daemon.health()["spinal_status"] is None

    def test_health_before_awaken_rem_is_none(self):
        """rem must be None before awaken() runs Phase 3."""
        daemon = _make_daemon()
        assert daemon.health()["rem"] is None


# ---------------------------------------------------------------------------
# test_metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_returns_dict(self):
        """metrics() must always return a dict."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {
                "epoch_count": 3,
                "total_findings": 10,
                "total_envelopes": 8,
            }

            await daemon.awaken()

        m = daemon.metrics()
        assert isinstance(m, dict)

    @pytest.mark.asyncio
    async def test_metrics_has_expected_keys(self):
        """metrics() must include epoch_count, total_findings, total_envelopes, vital_findings."""
        daemon = _make_daemon()

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

    def test_metrics_before_awaken_returns_zeros(self):
        """metrics() before awaken() returns dict with zero values."""
        daemon = _make_daemon()
        m = daemon.metrics()
        assert m["epoch_count"] == 0
        assert m["total_findings"] == 0
        assert m["total_envelopes"] == 0
        assert m["vital_findings"] == 0

    @pytest.mark.asyncio
    async def test_metrics_vital_findings_reflects_report(self):
        """vital_findings count should match the VitalReport findings length after awaken."""
        daemon = _make_daemon()

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)
            instance.health.return_value = {}

            await daemon.awaken()

        m = daemon.metrics()
        # With pass-through oracle and empty sensor, findings == 0
        assert m["vital_findings"] == 0


# ---------------------------------------------------------------------------
# test_degraded_spinal_still_awakens
# ---------------------------------------------------------------------------


class TestDegradedSpinalStillAwakens:
    @pytest.mark.asyncio
    async def test_degraded_event_stream_awakens_with_degraded_status(self):
        """Even with a broken event stream, awaken() completes and returns DEGRADED spinal."""
        stream = MagicMock()
        stream.broadcast_event = AsyncMock(side_effect=Exception("stream down"))
        daemon = _make_daemon(event_stream=stream)

        with patch("backend.core.ouroboros.daemon.RemSleepDaemon") as MockRem:
            instance = MockRem.return_value
            instance.start = AsyncMock(return_value=None)
            instance.stop = AsyncMock(return_value=None)

            report = await daemon.awaken()

        assert report.spinal_status is SpinalStatus.DEGRADED
        assert isinstance(report, AwakeningReport)
