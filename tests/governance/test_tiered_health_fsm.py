# tests/governance/test_tiered_health_fsm.py
from __future__ import annotations
import pytest


class _Resp:
    def __init__(self, c): self.content = c


class _Heavy:
    def __init__(self, *, fail=False, healthy=True):
        self.fail = fail; self.healthy = healthy
        self.gen_calls = 0; self.health_calls = 0
    async def generate(self, prompt, **kw):
        self.gen_calls += 1
        if self.fail: raise RuntimeError("heavy down")
        return _Resp(f"HEAVY:{prompt}")
    async def _check_health(self):
        self.health_calls += 1
        class _S: pass
        s = _S(); s.name = "AVAILABLE" if self.healthy else "UNAVAILABLE"
        return s
    async def aclose(self): pass


class _Light:
    def __init__(self): self.gen_calls = 0
    async def generate(self, prompt, **kw):
        self.gen_calls += 1
        return _Resp(f"LIGHT:{prompt}")
    async def _check_health(self):
        class _S: pass
        s = _S(); s.name = "AVAILABLE"; return s
    async def aclose(self): pass


def _mk(heavy, light, t):
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    # injectable clock + tiny cooldown via explicit params
    return TieredPrimeClient(heavy=heavy, light=light, now_fn=t,
                             failure_threshold=2, cooldown_s=100.0)


@pytest.mark.asyncio
async def test_single_failure_does_not_degrade():
    clock = {"t": 0.0}
    heavy = _Heavy(fail=True); light = _Light()
    t = _mk(heavy, light, lambda: clock["t"])
    r = await t.generate(prompt="a")          # heavy fails once -> light, but still HEALTHY
    assert r.content == "LIGHT:a"
    assert t.heavy_state() == "HEALTHY"        # below threshold (2)


@pytest.mark.asyncio
async def test_threshold_failures_degrade_and_skip_heavy():
    clock = {"t": 0.0}
    heavy = _Heavy(fail=True); light = _Light()
    t = _mk(heavy, light, lambda: clock["t"])
    await t.generate(prompt="a")               # fail 1
    await t.generate(prompt="b")               # fail 2 -> DEGRADED
    assert t.heavy_state() == "DEGRADED"
    heavy.gen_calls = 0
    r = await t.generate(prompt="c")           # DEGRADED + within cooldown -> skip heavy entirely
    assert r.content == "LIGHT:c"
    assert heavy.gen_calls == 0                 # heavy NOT called while degraded in cooldown


@pytest.mark.asyncio
async def test_recovery_probe_repromotes_after_cooldown():
    clock = {"t": 0.0}
    heavy = _Heavy(fail=True); light = _Light()
    t = _mk(heavy, light, lambda: clock["t"])
    await t.generate(prompt="a"); await t.generate(prompt="b")   # -> DEGRADED at t=0
    assert t.heavy_state() == "DEGRADED"
    # advance past cooldown; heavy is now actually healthy again
    clock["t"] = 200.0
    heavy.fail = False; heavy.healthy = True
    await t.generate(prompt="c")               # degraded+cooldown elapsed -> schedules bg recovery probe, routes light
    probe = t._pending_recovery_probe          # background task handle (strong ref)
    assert probe is not None
    await probe                                  # await the non-blocking probe deterministically
    assert t.heavy_state() == "HEALTHY"          # clean probe re-promoted
    r = await t.generate(prompt="d")             # now heavy used again
    assert r.content == "HEAVY:d"


@pytest.mark.asyncio
async def test_recovery_probe_failure_keeps_degraded():
    clock = {"t": 0.0}
    heavy = _Heavy(fail=True); light = _Light()
    t = _mk(heavy, light, lambda: clock["t"])
    await t.generate(prompt="a"); await t.generate(prompt="b")   # DEGRADED
    clock["t"] = 200.0
    heavy.healthy = False                        # remote still unhealthy
    await t.generate(prompt="c")                 # schedules probe
    if t._pending_recovery_probe is not None:
        await t._pending_recovery_probe
    assert t.heavy_state() == "DEGRADED"         # probe failed -> stays degraded
