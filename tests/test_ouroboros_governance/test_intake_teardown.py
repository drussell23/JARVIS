"""Regression tests for IntakeLayerService + sensor teardown.

Backstory:
    In ``bt-2026-04-13-011909`` the battle test logged "Shutdown complete"
    at 18:28:14 but the process stayed wedged — FileWatchGuard spammed
    "Main event loop not running" every ~20s against the dead loop,
    and asyncio emitted "Task was destroyed but it is pending!" for
    three sensor poll tasks (opportunity_miner_poll, backlog_sensor_poll,
    test_failure_sensor_poll).

    Two root causes:
      1. ``IntakeLayerService.stop()`` never stopped ``self._fs_bridge``.
         Only ``_teardown()`` (failed-start path) did — so normal
         budget_exhausted shutdowns leaked the watchdog Observer thread.
      2. OpportunityMiner / Backlog / TestFailure sensors created their
         poll tasks with ``asyncio.create_task(...)`` and discarded the
         return value. Their ``stop()`` methods only flipped
         ``self._running = False``; the task was still mid-``asyncio.sleep``
         when the event loop closed, producing the orphan-task errors.

    These tests lock down the fix so that:
      - ``IntakeLayerService.stop()`` calls ``fs_bridge.stop()``.
      - Each of the three sensors saves a ``_poll_task`` reference and
        cancels it on ``stop()``.
      - Sensor ``stop()`` is async and awaits the cancelled task so the
        loop closes without orphans.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
)
from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
)
from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    OpportunityMinerSensor,
)
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


# ---------------------------------------------------------------------------
# Sensor poll-task lifecycle — the three orphan-task offenders
# ---------------------------------------------------------------------------


class TestSensorPollTaskLifecycle:
    """Each fixed sensor must save its poll task and cancel it cleanly."""

    @pytest.mark.asyncio
    async def test_opportunity_miner_poll_task_cancelled_on_stop(
        self, tmp_path: Path,
    ) -> None:
        router = MagicMock()
        router.ingest = AsyncMock(return_value="skipped")
        sensor = OpportunityMinerSensor(
            repo_root=tmp_path,
            router=router,
            scan_paths=["."],
            repo="jarvis",
            poll_interval_s=3600.0,  # long — test never waits for it
        )

        assert sensor._poll_task is None, "task must be None before start"
        await sensor.start()
        assert sensor._poll_task is not None
        assert not sensor._poll_task.done(), "poll task must be live after start"

        task_ref = sensor._poll_task
        await sensor.stop()

        assert task_ref.done() or task_ref.cancelled(), (
            "poll task must be finished after stop — leaving it pending was "
            "the exact bug that burned bt-2026-04-13-011909"
        )
        assert sensor._poll_task is None
        assert sensor._running is False

    @pytest.mark.asyncio
    async def test_backlog_sensor_poll_task_cancelled_on_stop(
        self, tmp_path: Path,
    ) -> None:
        router = MagicMock()
        router.ingest = AsyncMock(return_value="skipped")
        sensor = BacklogSensor(
            backlog_path=tmp_path / "nonexistent_backlog.json",
            repo_root=tmp_path,
            router=router,
            poll_interval_s=3600.0,
        )

        assert sensor._poll_task is None
        await sensor.start()
        assert sensor._poll_task is not None
        task_ref = sensor._poll_task
        await sensor.stop()

        assert task_ref.done() or task_ref.cancelled()
        assert sensor._poll_task is None
        assert sensor._running is False

    @pytest.mark.asyncio
    async def test_test_failure_sensor_poll_task_cancelled_on_stop(
        self) -> None:
        router = MagicMock()
        router.ingest = AsyncMock(return_value="skipped")

        watcher = MagicMock()
        watcher.poll_interval_s = 3600.0
        watcher.poll_once = AsyncMock(return_value=[])
        watcher.stop = MagicMock()

        sensor = TestFailureSensor(repo="jarvis", router=router, test_watcher=watcher)
        assert sensor._poll_task is None

        await sensor.start()
        assert sensor._poll_task is not None
        task_ref = sensor._poll_task

        await sensor.stop()

        assert task_ref.done() or task_ref.cancelled()
        assert sensor._poll_task is None
        assert sensor._running is False
        watcher.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_test_failure_sensor_no_op_stop_without_watcher(self) -> None:
        """stop() must not crash when start() short-circuited on missing watcher."""
        router = MagicMock()
        sensor = TestFailureSensor(repo="jarvis", router=router, test_watcher=None)
        await sensor.start()  # returns early, _poll_task stays None
        assert sensor._poll_task is None
        await sensor.stop()  # must not raise
        assert sensor._poll_task is None


# ---------------------------------------------------------------------------
# IntakeLayerService.stop() — fs_bridge must be stopped
# ---------------------------------------------------------------------------


class TestIntakeLayerServiceStopsFsBridge:
    """Normal stop() must stop the FileSystemEventBridge.

    Prior to the fix, only ``_teardown()`` (the failed-start path) stopped
    ``_fs_bridge``. Normal budget_exhausted shutdowns leaked the watchdog
    Observer thread, which then spammed "Main event loop not running"
    every 20s against the dead loop for the lifetime of the process.
    """

    @pytest.mark.asyncio
    async def test_stop_calls_fs_bridge_stop(self, tmp_path: Path) -> None:
        gls = MagicMock()
        config = IntakeLayerConfig(project_root=tmp_path)
        service = IntakeLayerService(gls=gls, config=config, say_fn=None)

        # Fake fs_bridge with async stop()
        fs_bridge = MagicMock()
        fs_bridge.stop = AsyncMock()

        # Inject minimum post-start state: INACTIVE would short-circuit.
        service._state = IntakeServiceState.ACTIVE
        service._fs_bridge = fs_bridge  # type: ignore[attr-defined]

        # Fake sensor with sync stop() — covers the legacy sync path
        legacy_sensor = MagicMock()
        legacy_sensor.stop = MagicMock(return_value=None)

        # Fake sensor with async stop() — covers the new async path
        modern_sensor = MagicMock()
        modern_sensor.stop = AsyncMock()

        service._sensors = [legacy_sensor, modern_sensor]

        # Fake router with async stop()
        router = MagicMock()
        router.stop = AsyncMock()
        service._router = router

        await service.stop()

        fs_bridge.stop.assert_awaited_once()
        legacy_sensor.stop.assert_called_once()
        modern_sensor.stop.assert_awaited_once()
        router.stop.assert_awaited_once()
        assert service._state is IntakeServiceState.INACTIVE

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_from_inactive(self, tmp_path: Path) -> None:
        gls = MagicMock()
        config = IntakeLayerConfig(project_root=tmp_path)
        service = IntakeLayerService(gls=gls, config=config, say_fn=None)
        # INACTIVE → early return, no crash
        await service.stop()
        assert service._state is IntakeServiceState.INACTIVE

    @pytest.mark.asyncio
    async def test_stop_survives_fs_bridge_error(self, tmp_path: Path) -> None:
        """A raising fs_bridge.stop() must not block sensor/router cleanup."""
        gls = MagicMock()
        config = IntakeLayerConfig(project_root=tmp_path)
        service = IntakeLayerService(gls=gls, config=config, say_fn=None)

        fs_bridge = MagicMock()
        fs_bridge.stop = AsyncMock(side_effect=RuntimeError("boom"))
        service._state = IntakeServiceState.ACTIVE
        service._fs_bridge = fs_bridge  # type: ignore[attr-defined]

        router = MagicMock()
        router.stop = AsyncMock()
        service._router = router
        service._sensors = []

        await service.stop()

        fs_bridge.stop.assert_awaited_once()
        router.stop.assert_awaited_once()
        assert service._state is IntakeServiceState.INACTIVE
