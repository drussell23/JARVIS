"""Ignition must wait out a transient single-flight refusal.

`pkill` sends SIGTERM and the organism drains for several seconds. Throughout
that drain the kernel still holds its flock, so an ignition fired in that
window is refused — for a condition that resolves on its own. The cockpit used
to report that as a hard failure ending in "retry shortly", which reads as an
instruction and is really a description of a race the cockpit can wait out.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.cli import thin_client as tc


async def test_waits_for_a_dying_incumbent_then_ignites(monkeypatch):
    """THE REGRESSION: lock held by a shutting-down organism, then released."""
    state = {"lock_held": True, "serving": False, "spawns": 0}

    async def _probe(_p, **_k):
        return state["serving"]

    def _spawn(**_k):
        state["spawns"] += 1
        state["serving"] = True
        return type("P", (), {"poll": lambda self: None})()

    async def _await(_p, **_k):
        return state["serving"]

    monkeypatch.setattr(tc, "probe_socket", _probe)
    monkeypatch.setattr(tc, "spawn_daemon", _spawn)
    monkeypatch.setattr(tc, "await_socket", _await)
    monkeypatch.setattr(
        tc, "_live_incumbent", lambda: 999 if state["lock_held"] else None,
    )

    async def _release():
        await asyncio.sleep(0.3)
        state["lock_held"] = False

    asyncio.get_running_loop().create_task(_release())
    assert await tc._await_ignition_window("/tmp/x.sock", say=lambda _s: None) is True
    assert state["spawns"] == 1, "did not re-ignite once the lock cleared"


async def test_attaches_when_the_incumbent_simply_finishes_booting(monkeypatch):
    """The OTHER way the window closes: nothing was wrong, the incumbent was
    slow. Waiting only on the lock would hang here forever."""
    state = {"n": 0}

    async def _probe(_p, **_k):
        state["n"] += 1
        return state["n"] > 2

    monkeypatch.setattr(tc, "probe_socket", _probe)
    monkeypatch.setattr(tc, "_live_incumbent", lambda: 999)   # never releases
    monkeypatch.setattr(
        tc, "spawn_daemon",
        lambda **_k: pytest.fail("raced a live incumbent"),
    )

    assert await tc._await_ignition_window("/tmp/x.sock", say=lambda _s: None) is True


async def test_a_wedged_incumbent_still_returns_the_prompt(monkeypatch):
    """Bounded: the operator is never stranded waiting on a hung organism."""
    monkeypatch.setenv("JARVIS_OV_IGNITION_RETRY_S", "1")

    async def _probe(_p, **_k):
        return False

    monkeypatch.setattr(tc, "probe_socket", _probe)
    monkeypatch.setattr(tc, "_live_incumbent", lambda: 999)
    assert await tc._await_ignition_window("/tmp/x.sock", say=lambda _s: None) is False


async def test_the_wait_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_IGNITION_RETRY_S", "0")
    assert await tc._await_ignition_window("/tmp/x.sock", say=lambda _s: None) is False


def test_the_refusal_path_calls_the_waiter():
    """Structural pin: without it the cockpit tells the operator to retry a
    race it could have waited out itself."""
    import inspect

    src = inspect.getsource(tc.ensure_daemon)
    i = src.index("rc == 75")
    assert "_await_ignition_window" in src[i:i + 1500]
