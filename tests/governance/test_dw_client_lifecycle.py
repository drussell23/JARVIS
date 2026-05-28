from __future__ import annotations

from backend.core.ouroboros.governance.dw_client_lifecycle import (
    ClientLifecycleManager,
)


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


def _make_provider_with_session(fake_session):
    """Bypass __init__ and wire up _state so the _session property works."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    from backend.core.ouroboros.governance._governance_state import (
        DoubleWordProviderState,
    )
    prov = DoublewordProvider.__new__(DoublewordProvider)
    state = DoubleWordProviderState()
    state.session = fake_session
    prov._state = state
    return prov


async def test_force_session_reset_closes_and_nulls():
    fake = _FakeSession()
    prov = _make_provider_with_session(fake)

    await prov.force_session_reset()

    assert fake.close_calls == 1
    assert prov._session is None  # next _get_session() rebuilds fresh connector


async def test_force_session_reset_idempotent_when_none():
    prov = _make_provider_with_session(None)
    await prov.force_session_reset()  # must NOT raise
    assert prov._session is None


class _FlushProv:
    def __init__(self):
        self.reset_calls = 0

    async def force_session_reset(self):
        self.reset_calls += 1


async def test_flush_calls_force_reset():
    prov = _FlushProv()
    clock = {"t": 1000.0}
    mgr = ClientLifecycleManager(now_fn=lambda: clock["t"], cooldown_s=60.0)
    flushed = await mgr.flush_transport_pool(prov, reason="pool_stagnation")
    assert flushed is True
    assert prov.reset_calls == 1


async def test_flush_respects_cooldown():
    prov = _FlushProv()
    clock = {"t": 1000.0}
    mgr = ClientLifecycleManager(now_fn=lambda: clock["t"], cooldown_s=60.0)
    assert await mgr.flush_transport_pool(prov, reason="r1") is True
    clock["t"] = 1030.0  # 30s < 60s cooldown
    assert await mgr.flush_transport_pool(prov, reason="r2") is False
    assert prov.reset_calls == 1  # second flush suppressed
    clock["t"] = 1100.0  # past cooldown
    assert await mgr.flush_transport_pool(prov, reason="r3") is True
    assert prov.reset_calls == 2


async def test_flush_disabled_by_env(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_FLUSH_ENABLED", "false")
    prov = _FlushProv()
    mgr = ClientLifecycleManager(now_fn=lambda: 1000.0, cooldown_s=60.0)
    assert await mgr.flush_transport_pool(prov, reason="r") is False
    assert prov.reset_calls == 0
