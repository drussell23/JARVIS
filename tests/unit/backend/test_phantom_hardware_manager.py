"""Unit tests for PhantomHardwareManager concurrency and compatibility paths."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from backend.system import phantom_hardware_manager as phm


def _fresh_manager() -> phm.PhantomHardwareManager:
    """Return an isolated singleton instance for each test."""
    phm.PhantomHardwareManager._instance = None
    phm._phantom_manager_instance = None
    return phm.get_phantom_manager()


@pytest.mark.asyncio
async def test_ensure_ghost_display_is_single_flight(monkeypatch):
    """Concurrent ensure calls should share one in-flight operation."""
    manager = _fresh_manager()
    calls = 0
    gate = asyncio.Event()

    async def _fake_impl(self, wait_for_registration=True, max_wait_seconds=15.0):
        nonlocal calls
        calls += 1
        await gate.wait()
        return True, None

    monkeypatch.setattr(
        phm.PhantomHardwareManager,
        "_ensure_ghost_display_exists_impl",
        _fake_impl,
    )

    t1 = asyncio.create_task(manager.ensure_ghost_display_exists_async())
    t2 = asyncio.create_task(manager.ensure_ghost_display_exists_async())
    for _ in range(100):
        if manager._ensure_inflight is not None:
            break
        await asyncio.sleep(0.01)
    for _ in range(100):
        if calls > 0:
            break
        await asyncio.sleep(0.01)

    assert calls == 1
    gate.set()

    assert await t1 == (True, None)
    assert await t2 == (True, None)
    assert manager._ensure_inflight is None


@pytest.mark.asyncio
async def test_ensure_ghost_display_retries_after_failure(monkeypatch):
    """A failed ensure should clear inflight state so later calls can retry."""
    manager = _fresh_manager()
    calls = 0

    async def _failing_impl(self, wait_for_registration=True, max_wait_seconds=15.0):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(
        phm.PhantomHardwareManager,
        "_ensure_ghost_display_exists_impl",
        _failing_impl,
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        await manager.ensure_ghost_display_exists_async()

    assert manager._ensure_inflight is None

    with pytest.raises(RuntimeError, match="simulated failure"):
        await manager.ensure_ghost_display_exists_async()

    assert calls == 2


@pytest.mark.asyncio
async def test_get_display_status_async_alias(monkeypatch):
    """Compatibility alias should forward to get_status_async."""
    manager = _fresh_manager()
    expected = phm.PhantomHardwareStatus(
        cli_available=True,
        ghost_display_active=True,
    )

    async def _fake_status():
        return expected

    monkeypatch.setattr(manager, "get_status_async", _fake_status)

    result = await manager.get_display_status_async()
    assert result is expected


def test_analyze_yabai_spaces_identifies_registered_display_without_ghost_space():
    manager = _fresh_manager()
    analysis = manager._analyze_yabai_spaces_for_registration(
        [
            {
                "space_id": 1,
                "is_current": True,
                "is_visible": True,
                "display": 1,
                "window_count": 2,
            },
            {
                "space_id": 2,
                "is_current": False,
                "is_visible": False,
                "display": 2,
                "window_count": 0,
            },
        ]
    )

    assert analysis["ghost_space"] is None
    assert analysis["display_count"] == 2
    assert analysis["recognized_without_space"] is True


def test_analyze_yabai_spaces_prefers_virtual_display_ghost_candidate():
    manager = _fresh_manager()
    analysis = manager._analyze_yabai_spaces_for_registration(
        [
            {
                "space_id": 1,
                "is_current": True,
                "is_visible": True,
                "display": 1,
                "window_count": 3,
            },
            {
                "space_id": 4,
                "is_current": False,
                "is_visible": True,
                "display": 2,
                "window_count": 1,
            },
        ]
    )

    assert analysis["ghost_space"] == 4
    assert analysis["recognized_without_space"] is False


@pytest.mark.asyncio
async def test_ensure_logs_info_when_yabai_recognized_without_stable_ghost_space(
    monkeypatch,
    caplog,
):
    manager = _fresh_manager()

    async def _none(*_args, **_kwargs):
        return None

    async def _true(*_args, **_kwargs):
        return True

    async def _create(*_args, **_kwargs):
        return True, None

    async def _wait(*_args, **_kwargs):
        manager._last_registration_state = {
            "recognized_without_space": True,
            "display_count": 2,
            "ghost_space": None,
            "elapsed_seconds": 3.2,
        }
        return None

    monkeypatch.setattr(manager, "_find_display_via_system_profiler", _none)
    monkeypatch.setattr(manager, "_discover_cli_path_async", AsyncMock(return_value="/tmp/betterdisplaycli"))
    monkeypatch.setattr(manager, "_check_app_running_async", _true)
    monkeypatch.setattr(manager, "_find_existing_ghost_display_async", _none)
    monkeypatch.setattr(manager, "_create_virtual_display_async", _create)
    monkeypatch.setattr(manager, "_wait_for_display_registration_async", _wait)

    caplog.set_level(logging.INFO)
    success, error = await manager._ensure_ghost_display_exists_impl(
        wait_for_registration=True,
        max_wait_seconds=15.0,
    )

    assert success is True
    assert error is None
    assert any(
        "Display recognized by yabai" in record.message
        for record in caplog.records
    )
    assert not any(
        "hasn't recognized it yet" in record.message and record.levelno >= logging.WARNING
        for record in caplog.records
    )
