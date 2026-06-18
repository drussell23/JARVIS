# tests/governance/test_tiered_hedging.py
from __future__ import annotations
import asyncio
import pytest


class _Resp:
    def __init__(self, c): self.content = c


class _DelayedClient:
    """Generates after `delay` seconds; tracks cancellation cleanliness."""
    def __init__(self, tag, delay):
        self.tag = tag; self.delay = delay
        self.started = 0; self.completed = 0; self.cancelled = 0
    async def generate(self, prompt, **kw):
        self.started += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.completed += 1
        return _Resp(f"{self.tag}:{prompt}")
    async def _check_health(self):
        class _S: pass
        s = _S(); s.name = "AVAILABLE"; return s
    async def aclose(self): pass


def _mk(heavy, light, *, hedge_window_s):
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    return TieredPrimeClient(heavy=heavy, light=light, hedge_enabled=True,
                             hedge_window_s=hedge_window_s)


@pytest.mark.asyncio
async def test_hedge_disabled_by_default_uses_heavy_only(monkeypatch):
    monkeypatch.delenv("JARVIS_TIERED_HEDGE_ENABLED", raising=False)
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    heavy = _DelayedClient("HEAVY", 0.0); light = _DelayedClient("LIGHT", 0.0)
    t = TieredPrimeClient(heavy=heavy, light=light)   # hedge defaults OFF
    r = await t.generate(prompt="x")
    assert r.content == "HEAVY:x"
    assert light.started == 0


@pytest.mark.asyncio
async def test_hedge_heavy_fast_wins_no_light():
    heavy = _DelayedClient("HEAVY", 0.0); light = _DelayedClient("LIGHT", 5.0)
    t = _mk(heavy, light, hedge_window_s=0.5)
    r = await t.generate(prompt="x")
    assert r.content == "HEAVY:x"
    assert light.started == 0           # heavy finished within window -> no hedge spawned


@pytest.mark.asyncio
async def test_hedge_heavy_slow_light_wins_and_heavy_cancelled_cleanly():
    heavy = _DelayedClient("HEAVY", 5.0); light = _DelayedClient("LIGHT", 0.05)
    t = _mk(heavy, light, hedge_window_s=0.1)
    r = await t.generate(prompt="x")
    assert r.content == "LIGHT:x"        # light won
    assert light.completed == 1
    assert heavy.cancelled == 1          # laggard heavy cancelled CLEANLY (CancelledError observed)
    assert heavy.completed == 0
    # heavy slow -> recorded as a soft failure for the FSM
    assert t._consecutive_failures >= 1


@pytest.mark.asyncio
async def test_hedge_no_leaked_tasks_after_completion():
    heavy = _DelayedClient("HEAVY", 5.0); light = _DelayedClient("LIGHT", 0.05)
    t = _mk(heavy, light, hedge_window_s=0.1)
    before = len(asyncio.all_tasks())
    await t.generate(prompt="x")
    await asyncio.sleep(0.05)            # let any teardown settle
    after = len(asyncio.all_tasks())
    assert after <= before              # no dangling hedge/laggard tasks leaked
