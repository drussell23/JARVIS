"""AwakeningConductor: mounts the EXISTING WakeSequenceRenderer (Mandate 3),
ignites exactly once, skips only on Esc/Enter, buffers typed input, cools
down exactly once, and NEVER raises (spec §3.2, §9.5)."""
from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from backend.core.ouroboros.ui.awakening import AwakeningConductor
from backend.core.ouroboros.ui import theme


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t


class FakeTimer:
    """Duck-typed BootTimer: captures the observer for manual driving."""
    def __init__(self):
        self.observers = []
    def add_observer(self, cb):
        self.observers.append(cb)
    def emit(self, name, in_flight):
        class R:  # PhaseRecord duck
            pass
        r = R(); r.name = name; r.is_in_flight = in_flight
        for cb in self.observers:
            cb(r)


def make_conductor(**kw):
    console = Console(file=open("/dev/null", "w"), force_terminal=True,
                      width=80, color_system="truecolor")
    theme.ensure_theme(console)
    clock = kw.pop("clock", FakeClock())
    timer = kw.pop("timer", FakeTimer())
    c = AwakeningConductor(console, timer=timer, clock=clock, **kw)
    return c, clock, timer


@pytest.mark.asyncio
async def test_ignition_fires_exactly_once():
    fired = []
    c, clock, timer = make_conductor(on_ignition=lambda: fired.append(1))
    timer.emit("sensors online", False)
    task = asyncio.create_task(c.run())
    for _ in range(400):
        clock.t += 0.05
        await asyncio.sleep(0.001)
        if fired:
            break
    clock.t += 30.0                      # blow past hold guard + cool-down
    await asyncio.wait_for(task, timeout=5.0)
    assert fired == [1]


@pytest.mark.asyncio
async def test_esc_skips_and_enter_skips_but_other_keys_buffer():
    for skip_byte in (b"\x1b", b"\r", b"\n"):
        feed = [b"g", b"i", skip_byte]
        c, clock, timer = make_conductor(
            key_source=lambda f=feed: f.pop(0) if f else b"")
        timer.emit("loop armed", False)
        task = asyncio.create_task(c.run())
        for _ in range(200):
            clock.t += 0.05
            await asyncio.sleep(0.001)
            if task.done():
                break
        clock.t += 30.0
        await asyncio.wait_for(task, timeout=5.0)
        assert c.typed_prefix == "gi"     # buffered, not swallowed


@pytest.mark.asyncio
async def test_live_honesty_holds_until_model_live():
    c, clock, timer = make_conductor()
    timer.emit("venom priming", True)     # still in flight
    task = asyncio.create_task(c.run())
    clock.t += 3.0                        # trace done, but NOT live
    await asyncio.sleep(0.01)
    assert not task.done()                # holding (breathing)
    timer.emit("venom priming", False)    # now live
    clock.t += 30.0
    await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_render_failure_never_raises(monkeypatch):
    c, clock, timer = make_conductor()
    monkeypatch.setattr(c, "_render_crest_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    timer.emit("sensors online", False)
    clock.t += 60.0
    await asyncio.wait_for(c.run(), timeout=5.0)   # must complete silently


@pytest.mark.asyncio
async def test_awakening_disabled_env_goes_plain(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_AWAKENING_ENABLED", "false")
    c, clock, timer = make_conductor()
    timer.emit("sensors online", False)
    clock.t += 60.0
    await asyncio.wait_for(c.run(), timeout=5.0)
    assert c.used_plain_path is True
