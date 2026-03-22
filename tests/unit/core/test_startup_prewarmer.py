"""Unit tests for StartupPreWarmer — result cache, staleness, handoff, shutdown."""
import asyncio
import time
import pytest
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor

from backend.core.startup_prewarmer import (
    PreWarmStatus,
    PreWarmResult,
    StartupPreWarmer,
)


class TestPreWarmResult:
    def test_status_enum_values(self):
        assert PreWarmStatus.PENDING.value == "pending"
        assert PreWarmStatus.OK.value == "ok"
        assert PreWarmStatus.FAILED.value == "failed"
        assert PreWarmStatus.SKIPPED.value == "skipped"

    def test_result_age_monotonic(self):
        r = PreWarmResult(status=PreWarmStatus.OK, value=True, timestamp=time.monotonic())
        assert r.age_s < 1.0

    def test_result_default_timestamp_always_stale(self):
        r = PreWarmResult(status=PreWarmStatus.OK, value=True)
        assert r.age_s > 100.0


class TestStartupPreWarmerAPI:
    def test_get_result_returns_none_for_unknown_task(self):
        pw = StartupPreWarmer(config=MagicMock())
        assert pw.get_result("nonexistent") is None

    def test_get_result_returns_none_for_pending(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.PENDING, timestamp=time.monotonic()
        )
        assert pw.get_result("test") is None

    def test_get_result_returns_none_for_failed(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.FAILED, error="boom", timestamp=time.monotonic()
        )
        assert pw.get_result("test") is None

    def test_get_result_returns_result_when_ok_and_fresh(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.OK, value=42, timestamp=time.monotonic()
        )
        r = pw.get_result("test", max_age_s=30.0)
        assert r is not None
        assert r.value == 42

    def test_get_result_returns_none_when_stale(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.OK, value=42, timestamp=time.monotonic() - 60
        )
        assert pw.get_result("test", max_age_s=30.0) is None

    def test_get_status_returns_skipped_for_unknown(self):
        pw = StartupPreWarmer(config=MagicMock())
        assert pw.get_status("nonexistent") == PreWarmStatus.SKIPPED

    def test_get_status_returns_pending(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.PENDING, timestamp=time.monotonic()
        )
        assert pw.get_status("test") == PreWarmStatus.PENDING

    def test_disabled_start_is_noop(self):
        import os
        os.environ["JARVIS_PREWARM_DISABLED"] = "true"
        try:
            pw = StartupPreWarmer(config=MagicMock())
            pw.start()
            assert pw._started is False
            assert pw._executor is None
        finally:
            os.environ.pop("JARVIS_PREWARM_DISABLED", None)


class TestReleaseAndShutdown:
    @pytest.mark.asyncio
    async def test_release_task_returns_task(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True

        async def dummy():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy())
        pw._async_tasks["gcp_vm_start"] = task
        pw._register_pending("gcp_vm_start")

        released = pw.release_task("gcp_vm_start")
        assert released is task
        assert "gcp_vm_start" in pw._released_tasks
        assert pw.release_task("gcp_vm_start") is None

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_release_task_unknown_returns_none(self):
        pw = StartupPreWarmer(config=MagicMock())
        assert pw.release_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_shutdown_cancels_unreleased_tasks(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True

        async def dummy():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy())
        pw._async_tasks["test_task"] = task
        pw._register_pending("test_task")

        pw.shutdown(timeout=1.0)
        # Yield one event-loop iteration so the cancellation propagates from
        # "cancelling" to "cancelled" state — required by CPython asyncio.
        await asyncio.sleep(0)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_shutdown_skips_released_tasks(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True

        async def dummy():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy())
        pw._async_tasks["gcp"] = task
        pw._register_pending("gcp")
        pw.release_task("gcp")

        pw.shutdown(timeout=1.0)
        assert not task.cancelled()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_shutdown_noop_when_not_started(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw.shutdown()

    @pytest.mark.asyncio
    async def test_register_pending_before_submit(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True
        pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

        pw._submit_thread("docker_probe", lambda: True)
        status = pw.get_status("docker_probe")
        assert status in (PreWarmStatus.PENDING, PreWarmStatus.OK)

        pw.shutdown(timeout=2.0)
