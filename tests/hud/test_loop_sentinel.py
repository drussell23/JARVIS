"""Was JARVIS there, or only appearing to be?

An operator said "lock my screen" at 01:40:34 and the router did not begin
until 01:40:49. Fifteen seconds in which the assistant was, from the room,
absent. These pin the witness that can tell the two shapes of that apart:

  * a SLOW boot that yields — the loop stays responsive, commands still land
  * a BLOCKED loop — nothing runs at all

They produce identical wall-clock timelines and need opposite fixes.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.hud.loop_sentinel import (
    LoopSentinel, get_loop_sentinel, reset_loop_sentinel,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("JARVIS_LOOP_SENTINEL_INTERVAL_S", "0.05")
    monkeypatch.setenv("JARVIS_LOOP_SENTINEL_STALL_S", "0.20")
    reset_loop_sentinel()
    yield
    reset_loop_sentinel()


# ── The mandate: heavy init must not stop Tier 0 ────────────────────────────

@pytest.mark.asyncio
async def test_a_slow_dependency_does_not_stop_the_loop():
    """THE ASSERTION THAT MATTERS.

    A `LearningDB` that takes ten seconds to initialise must not cost the
    operator a single millisecond, PROVIDED it yields. This is the proof that
    a slow boot and a starved boot are different things: the dependency here
    is twenty times slower than the stall threshold and the loop never
    notices.
    """
    s = LoopSentinel()
    s.start()

    async def slow_learning_db():
        await asyncio.sleep(1.0)          # stands in for the measured 15s+
        return "ready"

    boot = asyncio.create_task(slow_learning_db())

    # Tier 0 work, interleaved with the heavy boot.
    latencies = []
    for _ in range(20):
        t0 = time.monotonic()
        await asyncio.sleep(0)            # one full trip through the loop
        latencies.append(time.monotonic() - t0)

    assert await boot == "ready"
    await s.stop()

    assert max(latencies) < 0.20, f"Tier 0 was delayed: {max(latencies):.3f}s"
    assert s.health()["stalls"] == 0, (
        "an awaiting dependency was reported as starvation — the sentinel "
        "cannot tell slow from blocked, which is its whole job")


@pytest.mark.asyncio
async def test_a_synchronous_call_IS_caught():
    """The other half. `time.sleep` on the loop is what starvation looks like,
    and it must be impossible to mistake for a slow await."""
    s = LoopSentinel()
    s.start()
    await asyncio.sleep(0.15)
    time.sleep(0.6)                       # a blocking call, on the loop
    await asyncio.sleep(0.15)
    await s.stop()

    h = s.health()
    assert h["stalls"] >= 1, "a blocking call went unnoticed"
    assert h["worst_lag_s"] >= 0.4


@pytest.mark.asyncio
async def test_a_capability_still_resolves_while_a_dependency_boots():
    """End to end, in the shape of the actual failure.

    A PURE, deterministic capability resolution must complete instantly while
    a heavy dependency is mid-initialisation — that is the whole promise of
    the reflex arc, and the property the 15s gap violated.
    """
    from backend.system_control.capability_reflex import get_capability_reflex

    rx = get_capability_reflex()
    rx.resolve("warm the lexicon")        # pay any one-time import first

    booting = asyncio.create_task(asyncio.sleep(0.8))

    t0 = time.monotonic()
    out = rx.resolve("lock my screen")
    elapsed = time.monotonic() - t0

    assert out.resolved and out.capability == "lock_screen"
    assert elapsed < 0.05, f"resolution took {elapsed:.3f}s during boot"
    assert not booting.done(), "the dependency finished early; test is vacuous"
    await booting


# ── The witness itself ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_availability_reflects_time_spent_absent():
    s = LoopSentinel()
    s.start()
    await asyncio.sleep(0.1)
    time.sleep(0.5)
    await asyncio.sleep(0.1)
    await s.stop()
    h = s.health()
    assert 0.0 <= h["availability"] <= 1.0
    assert h["availability"] < 1.0, "a half-second absence left no trace"


@pytest.mark.asyncio
async def test_stalls_are_reported_with_a_wall_clock_time():
    """The point is to go and LOOK at the log around that moment, so a lag
    number without a timestamp is a fact nobody can use."""
    s = LoopSentinel()
    s.start()
    await asyncio.sleep(0.1)
    time.sleep(0.5)
    await asyncio.sleep(0.1)
    await s.stop()
    recent = s.health()["recent"]
    assert recent and ":" in recent[0] and "for" in recent[0]


@pytest.mark.asyncio
async def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_LOOP_SENTINEL_ENABLED", "false")
    s = LoopSentinel()
    s.start()
    assert s.health()["watching"] is False


@pytest.mark.asyncio
async def test_starting_twice_watches_once():
    s = LoopSentinel()
    s.start()
    first = s._task
    s.start()
    assert s._task is first
    await s.stop()


@pytest.mark.asyncio
async def test_stopping_when_never_started_is_fine():
    await LoopSentinel().stop()


@pytest.mark.asyncio
async def test_the_singleton_is_shared():
    assert get_loop_sentinel() is get_loop_sentinel()
