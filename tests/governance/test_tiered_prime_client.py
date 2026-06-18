# tests/governance/test_tiered_prime_client.py
from __future__ import annotations
import pytest


class _FakeResp:
    def __init__(self, content): self.content = content; self.name = "AVAILABLE"


class _FakeClient:
    """Duck-typed PrimeClient: configurable generate result / health / failure."""
    def __init__(self, tag, *, healthy=True, fail=False):
        self.tag = tag; self._healthy = healthy; self._fail = fail
        self.gen_calls = 0; self.closed = False

    async def generate(self, prompt, **kw):
        self.gen_calls += 1
        if self._fail:
            raise RuntimeError(f"{self.tag} boom")
        return _FakeResp(f"{self.tag}:{prompt}")

    async def _check_health(self):
        class _S:  # mimic PrimeStatus member
            pass
        s = _S()
        s.name = "AVAILABLE" if self._healthy else "UNAVAILABLE"
        return s

    async def aclose(self): self.closed = True


@pytest.mark.asyncio
async def test_tiered_prefers_heavy_when_healthy():
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    heavy = _FakeClient("HEAVY"); light = _FakeClient("LIGHT")
    t = TieredPrimeClient(heavy=heavy, light=light)
    r = await t.generate(prompt="x")
    assert r.content == "HEAVY:x"
    assert heavy.gen_calls == 1 and light.gen_calls == 0


@pytest.mark.asyncio
async def test_tiered_falls_to_light_on_heavy_failure():
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    heavy = _FakeClient("HEAVY", fail=True); light = _FakeClient("LIGHT")
    t = TieredPrimeClient(heavy=heavy, light=light)
    r = await t.generate(prompt="x")
    assert r.content == "LIGHT:x"
    assert heavy.gen_calls == 1 and light.gen_calls == 1


@pytest.mark.asyncio
async def test_tiered_health_available_if_either_available():
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    heavy = _FakeClient("HEAVY", healthy=False); light = _FakeClient("LIGHT", healthy=True)
    t = TieredPrimeClient(heavy=heavy, light=light)
    s = await t._check_health()
    assert s.name == "AVAILABLE"


@pytest.mark.asyncio
async def test_tiered_aclose_closes_both():
    from backend.core.ouroboros.governance.tiered_prime_client import TieredPrimeClient
    heavy = _FakeClient("HEAVY"); light = _FakeClient("LIGHT")
    t = TieredPrimeClient(heavy=heavy, light=light)
    await t.aclose()
    assert heavy.closed and light.closed


def test_factory_both_returns_tiered_only_one_returns_that_one():
    from backend.core.ouroboros.governance.tiered_prime_client import (
        build_tiered_prime_client, TieredPrimeClient)
    heavy = _FakeClient("HEAVY"); light = _FakeClient("LIGHT")
    assert isinstance(build_tiered_prime_client(heavy=heavy, light=light), TieredPrimeClient)
    assert build_tiered_prime_client(heavy=None, light=light) is light   # only light -> passthrough
    assert build_tiered_prime_client(heavy=heavy, light=None) is heavy   # only heavy -> passthrough
    assert build_tiered_prime_client(heavy=None, light=None) is None     # neither -> None (legacy)


def test_tiered_enabled_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_JPRIME_TIERED_ENABLED", raising=False)
    from backend.core.ouroboros.governance.tiered_prime_client import tiered_enabled
    assert tiered_enabled() is False
    monkeypatch.setenv("JARVIS_JPRIME_TIERED_ENABLED", "true")
    assert tiered_enabled() is True
