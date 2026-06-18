# tests/governance/test_tiered_memory_and_leaks.py
from __future__ import annotations
import asyncio
import pytest


def _local_with_critical_governor(monkeypatch):
    """Build a REAL LocalPrimeClient (light tier) with a CRITICAL-gate governor."""
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalConfig, LocalPrimeClient, LocalInferenceDirector)
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel

    class _EvictSession:
        closed = False
        def __init__(self): self.posts = []
        def post(self, url, **kw):
            self.posts.append((url, kw))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return {"choices": [{"message": {"content": "x"}}], "status": "ok"}
            return _R()
        async def close(self): self.closed = True

    class _CriticalGate:
        def pressure(self): return PressureLevel.CRITICAL

    sess = _EvictSession()
    light = LocalPrimeClient(LocalConfig.from_env(), session=sess)
    director = LocalInferenceDirector(LocalConfig.from_env(), client=light, gate=_CriticalGate())
    light.attach_governor(director)
    return light, sess


class _FailingHeavy:
    def __init__(self, *, exc=None): self._exc = exc or RuntimeError("heavy quota exhausted")
    async def generate(self, prompt, **kw): raise self._exc
    async def _check_health(self):
        class _S: pass
        s = _S(); s.name = "UNAVAILABLE"; return s
    async def aclose(self): pass


@pytest.mark.asyncio
async def test_memory_valve_overrides_when_composite_routes_to_light(monkeypatch):
    """heavy fails -> composite routes to light -> light's memory_guard fires at CRITICAL."""
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    from backend.core.ouroboros.governance.local_inference_director import LocalMemoryCritical
    light, sess = _local_with_critical_governor(monkeypatch)
    t = TieredPrimeClient(heavy=_FailingHeavy(), light=light)  # hedge off
    with pytest.raises(LocalMemoryCritical):
        await t.generate(prompt="x", system_prompt="s", max_tokens=32)
    # the valve evicted + refused before any chat/completions inference on the light tier
    assert all("/v1/chat/completions" not in url for url, _ in sess.posts)
    assert any(kw.get("json", {}).get("keep_alive") == 0 for _, kw in sess.posts)


@pytest.mark.asyncio
async def test_heavy_quota_exhaustion_cascades_to_light_cleanly(monkeypatch):
    """With memory OK, heavy quota-exhaustion cascades to a working local light tier."""
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")  # valve pass-through
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalConfig, LocalPrimeClient)

    class _OkSession:
        closed = False
        def __init__(self): self.posts = []
        def post(self, url, **kw):
            self.posts.append((url, kw))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return {"choices": [{"message": {"content": "LOCAL_OK"}}],
                                               "usage": {"completion_tokens": 3}}
            return _R()
        async def close(self): self.closed = True

    light = LocalPrimeClient(LocalConfig.from_env(), session=_OkSession())
    t = TieredPrimeClient(heavy=_FailingHeavy(exc=RuntimeError("terminal_quota")), light=light)
    resp = await t.generate(prompt="x", system_prompt="s", max_tokens=16)
    assert resp.content == "LOCAL_OK"
    assert t.heavy_state() in ("HEALTHY", "DEGRADED")  # FSM recorded the failure


@pytest.mark.asyncio
async def test_hedge_cancellation_leaves_no_dangling_tasks():
    """Hedge path cancels the laggard with no leaked tasks (reinforces Task 3)."""
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient

    class _Resp:
        def __init__(self, c): self.content = c

    class _Delayed:
        def __init__(self, tag, delay): self.tag = tag; self.delay = delay; self.cancelled = 0
        async def generate(self, prompt, **kw):
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled += 1; raise
            return _Resp(f"{self.tag}")
        async def _check_health(self):
            class _S: pass
            s = _S(); s.name = "AVAILABLE"; return s
        async def aclose(self): pass

    heavy = _Delayed("HEAVY", 5.0); light = _Delayed("LIGHT", 0.02)
    t = TieredPrimeClient(heavy=heavy, light=light, hedge_enabled=True, hedge_window_s=0.05)
    before = len(asyncio.all_tasks())
    r = await t.generate(prompt="x")
    await asyncio.sleep(0.05)
    after = len(asyncio.all_tasks())
    assert r.content == "LIGHT"
    assert heavy.cancelled == 1
    assert after <= before
