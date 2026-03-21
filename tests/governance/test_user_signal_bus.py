import asyncio
import pytest
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus


def test_initial_state_not_set():
    bus = UserSignalBus()
    assert not bus.is_stop_requested()


def test_request_stop_sets_flag():
    bus = UserSignalBus()
    bus.request_stop()
    assert bus.is_stop_requested()


def test_reset_clears_flag():
    bus = UserSignalBus()
    bus.request_stop()
    bus.reset()
    assert not bus.is_stop_requested()


@pytest.mark.asyncio
async def test_wait_for_stop_resolves_after_request_stop():
    bus = UserSignalBus()

    async def trigger():
        await asyncio.sleep(0.01)
        bus.request_stop()

    asyncio.create_task(trigger())
    await asyncio.wait_for(bus.wait_for_stop(), timeout=1.0)
    assert bus.is_stop_requested()


@pytest.mark.asyncio
async def test_wait_for_stop_does_not_resolve_without_stop():
    bus = UserSignalBus()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.wait_for_stop(), timeout=0.05)
