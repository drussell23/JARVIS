"""The capability-gap consumer survives a loop change and never starves its loop.

Found when the A/B run for the pipeline stack died at ~2%: the process-wide
GapSignalBus held an ``asyncio.Queue`` bound to an earlier test's loop, ``get()``
raised on every call without suspending, and the sensor's poll loop retried at
once — 5.5 M logged exceptions in 4 s with its own event loop frozen.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    capability_gap_sensor as S,
)
from backend.neural_mesh.synthesis.gap_signal_bus import (
    CapabilityGapEvent,
    GapSignalBus,
)


def _event(n: int = 0) -> CapabilityGapEvent:
    return CapabilityGapEvent(
        goal=f"g{n}", task_type="Browser Navigation", target_app=f"app{n}",
        source="test",
    )


def _bind_to_a_first_loop(bus: GapSignalBus) -> None:
    """Wait on the bus under one loop, the way an earlier consumer would."""
    async def _wait_then_leave() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.get(), timeout=0.05)
    asyncio.run(_wait_then_leave())


@pytest.mark.timeout(15, method="signal")
def test_a_consumer_under_a_new_loop_receives_events():
    bus = GapSignalBus()
    _bind_to_a_first_loop(bus)

    async def _consume() -> CapabilityGapEvent:
        # The consumer must be WAITING when the event arrives: a get() on a
        # non-empty queue returns via get_nowait and never checks the loop,
        # which is why the bug hid from any test that emitted first.
        waiting = asyncio.ensure_future(bus.get())
        await asyncio.sleep(0)
        bus.emit(_event(1))
        return await asyncio.wait_for(waiting, timeout=2)

    assert asyncio.run(_consume()).goal == "g1"


@pytest.mark.timeout(15, method="signal")
def test_events_pending_across_a_loop_change_are_carried_over():
    """Guards the rebind's migration: replacing the queue must not drop what
    was emitted before the new consumer arrived."""
    bus = GapSignalBus()
    _bind_to_a_first_loop(bus)
    bus.emit(_event(1))           # emitted between loops, with no loop running
    bus.emit(_event(2))

    async def _drain() -> list:
        return [
            (await asyncio.wait_for(bus.get(), timeout=2)).goal for _ in range(2)
        ]

    assert asyncio.run(_drain()) == ["g1", "g2"]


class _AlwaysFails:
    """A bus whose ``get`` raises without ever suspending."""

    def __init__(self) -> None:
        self.calls = 0

    async def get(self) -> CapabilityGapEvent:
        self.calls += 1
        raise RuntimeError("bound to a different event loop")


class _Router:
    async def submit(self, envelope) -> None:  # pragma: no cover — never reached
        pass


@pytest.mark.timeout(15, method="signal")
def test_a_persistent_failure_backs_off_instead_of_starving_the_loop(monkeypatch):
    monkeypatch.setattr(S, "_BACKOFF_BASE_S", 0.01, raising=False)
    bus = _AlwaysFails()

    async def _run() -> float:
        sensor = S.CapabilityGapSensor(_Router(), "jarvis", bus=bus)
        await sensor.start()
        t0 = time.monotonic()
        try:
            await asyncio.sleep(0.2)   # never returns if the poll loop spins
        finally:
            await sensor.stop()
        return time.monotonic() - t0

    elapsed = asyncio.run(_run())
    assert elapsed < 1.0
    # Doubling from 10 ms: a handful of retries in 200 ms, not millions.
    assert 1 <= bus.calls <= 10
