"""Loopback Self-Test Engine + converged SYSTEM_READY (Phase 12, Slice D)."""
from __future__ import annotations

import pytest

from backend.api import loopback_selftest as lst
from backend.core import active_failover as af


def _dw_provider(answer=None, exc=None):
    async def _call(ctx):
        if exc is not None:
            raise exc
        return answer
    return af.Provider(name="doubleword", call=_call)


# ---------------------------------------------------------------------------
# the self-test proves the failover WITHOUT a human at the HUD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_selftest_proves_failover_when_doubleword_answers():
    events = []
    async def _bus(topic, data): events.append(data)
    st = lst.LoopbackSelfTest(
        dw_provider=_dw_provider(answer="Good to see you, Sir."),
        bus_publish=_bus)
    result = await st.run()
    assert result.state is lst.SelfTestState.FAILOVER_PROVEN
    assert result.provider == "doubleword"
    assert result.ledger["doubleword"] == "ready"
    # FAILOVER_PROVEN telemetry was dispatched.
    assert any(d["type"] == "FAILOVER_PROVEN" for d in events)


@pytest.mark.asyncio
async def test_selftest_degrades_on_entitlement_error_not_fatal():
    """DoubleWord auth/entitlement error → DEGRADED (flag ledger), NOT a
    boot failure — the primary command loop stays live."""
    events = []
    async def _bus(topic, data): events.append(data)
    st = lst.LoopbackSelfTest(
        dw_provider=_dw_provider(exc=Exception("401 Unauthorized: invalid api key")),
        bus_publish=_bus)
    result = await st.run()
    assert result.state is lst.SelfTestState.DEGRADED
    assert result.ledger["doubleword"] == "degraded:entitlement"
    assert any(d["type"] == "PROVIDER_DEGRADED" for d in events)


@pytest.mark.asyncio
async def test_selftest_degrades_on_unavailable_backend_not_fatal():
    """DoubleWord backend NOT PROVISIONED in this runtime (missing transitive
    dep — e.g. a lean venv without uuid6) → DEGRADED, NOT a boot failure. An
    environment gap is not a failover-logic fault; the primary loop stays live."""
    events = []
    async def _bus(topic, data): events.append(data)
    st = lst.LoopbackSelfTest(
        dw_provider=_dw_provider(exc=ModuleNotFoundError("No module named 'uuid6'")),
        bus_publish=_bus)
    result = await st.run()
    assert result.state is lst.SelfTestState.DEGRADED
    assert result.ledger["doubleword"] == "degraded:unavailable"
    assert any(d["type"] == "PROVIDER_DEGRADED"
               and d["reason"] == "unavailable" for d in events)


@pytest.mark.asyncio
async def test_selftest_non_entitlement_failure_is_surfaced_non_fatal():
    async def _bus(t, d): pass
    st = lst.LoopbackSelfTest(
        dw_provider=_dw_provider(exc=RuntimeError("connection reset")),
        bus_publish=_bus)
    result = await st.run()
    assert result.state is lst.SelfTestState.FAILED     # surfaced, non-fatal
    assert result.ledger["doubleword"] == "failed"


@pytest.mark.asyncio
async def test_selftest_simulated_primary_is_credit_400_shape():
    """The self-test must simulate the EXACT live primary fault so the real
    classifier pivots — proven by the failover reaching DoubleWord."""
    reached = []
    async def _dw(ctx):
        reached.append(ctx)
        return "ok"
    st = lst.LoopbackSelfTest(dw_provider=af.Provider("doubleword", _dw),
                              bus_publish=lambda t, d: _noop())
    r = await st.run()
    assert r.state is lst.SelfTestState.FAILOVER_PROVEN
    assert reached, "failover never reached DoubleWord"   # primary DID fail


async def _noop(): return None


# ---------------------------------------------------------------------------
# MANDATE 4 — converged boot: hydration OK + self-test proven → SYSTEM_READY
# ---------------------------------------------------------------------------

def test_converged_boot_reaches_system_ready():
    from fastapi.testclient import TestClient
    from backend.api import converged_headless as ch
    from backend.api import progressive_hydration as ph

    async def _ok(): return None
    subs = [ph.Subsystem("stub", _ok)]

    app = ch.create_converged_app(
        subsystems=subs,
        dw_provider=_dw_provider(answer="Good to see you, Sir."),
        run_selftest=True, mount_router=False)

    @app.post("/api/command")
    async def _cmd():
        return {"status": "accepted"}

    with TestClient(app) as client:
        # binds + serves instantly
        assert client.post("/api/command").status_code == 200
        # let the background converged boot (hydrate → self-test) complete
        import time; deadline = time.time() + 3
        final = {}
        while time.time() < deadline:
            final = client.get("/api/system/status").json()
            if final["system_state"] == "ready":
                break
            time.sleep(0.1)
        assert final["system_state"] == "ready"          # global SYSTEM_READY
        assert final["selftest"] == "failover_proven"    # DW self-test proven
        assert final["hydration"]["state"] == "ready"
