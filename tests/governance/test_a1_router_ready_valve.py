"""A1-T2 — event-driven router-ready valve regression spine.

Proves the race-free subscribe-then-check readiness gate that stops the
roadmap-ignition daemon from emitting a strategic GOAL before the intake
router is attached + its dispatch loop is running.

The race-critical unit is ``unified_intake_router.await_router_ready`` plus
the process-global readiness flag (``mark_router_ready`` / ``router_is_ready``).
The daemon's timeout->DLQ->no-emit response is covered by the T5 integration
test (the daemon is a nested closure in GovernedLoopService.start()).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.intake import unified_intake_router as uir


@pytest.fixture(autouse=True)
def _reset_ready():
    """Clear the process-global readiness flag around every test."""
    uir._reset_router_ready_for_tests()
    yield
    uir._reset_router_ready_for_tests()


class _FakeBus:
    """Minimal async pub/sub matching TrinityEventBus.subscribe/publish."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    async def subscribe(self, pattern, handler, **_kw):
        self._handlers.setdefault(pattern, []).append(handler)
        return "sub-1"

    async def publish(self, event, persist: bool = True):
        for h in list(self._handlers.get(getattr(event, "topic", ""), [])):
            await h(event)
        return "evt-1"


class _Evt:
    def __init__(self, topic: str) -> None:
        self.topic = topic


async def test_ready_before_subscribe_no_deadlock():
    """Flag set BEFORE the valve runs -> proceeds immediately, no hang."""
    bus = _FakeBus()
    uir.mark_router_ready()
    res = await asyncio.wait_for(uir.await_router_ready(bus, 5.0), timeout=2.0)
    assert res is True


async def test_ready_after_subscribe_event_wakes():
    """Flag set + event published AFTER the valve subscribes -> event wakes it."""
    bus = _FakeBus()

    async def _later():
        await asyncio.sleep(0.05)
        uir.mark_router_ready()
        await bus.publish(_Evt(uir.EVENT_ROUTER_READY))

    task = asyncio.create_task(_later())
    res = await uir.await_router_ready(bus, 5.0)
    await task
    assert res is True


async def test_timeout_returns_false_with_bus():
    """Never ready within the timeout -> returns False (daemon then DLQs)."""
    bus = _FakeBus()
    res = await uir.await_router_ready(bus, 0.1)
    assert res is False


async def test_no_bus_flag_poll_detects_ready():
    """Bus absent -> degraded bounded flag-poll still detects readiness."""

    async def _later():
        await asyncio.sleep(0.05)
        uir.mark_router_ready()

    task = asyncio.create_task(_later())
    res = await uir.await_router_ready(None, 5.0)
    await task
    assert res is True


async def test_no_bus_timeout_returns_false():
    """Bus absent + never ready -> bounded, returns False (no infinite poll)."""
    res = await uir.await_router_ready(None, 0.1)
    assert res is False


def test_mark_ready_idempotent():
    uir.mark_router_ready()
    uir.mark_router_ready()
    assert uir.router_is_ready() is True


def test_event_constant_value():
    assert uir.EVENT_ROUTER_READY == "intake.router.ready"
