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
# Slice E — Dynamic Package Recovery drives ModuleNotFoundError → FAILOVER_PROVEN
# ---------------------------------------------------------------------------

def _healing_dw_provider():
    """A DoubleWord provider that raises ModuleNotFoundError('uuid6') on the
    FIRST attempt (dep missing) and answers on the SECOND (dep self-healed)."""
    state = {"n": 0}
    async def _call(ctx):
        state["n"] += 1
        if state["n"] == 1:
            raise ModuleNotFoundError("No module named 'uuid6'")
        return "Good to see you, Sir."
    return af.Provider(name="doubleword", call=_call), state


def _faked_recovery():
    """A fully-faked Self-Healing engine: records the pip argv, never touches
    the network, and reports the module importable after 'install'."""
    from backend.api import package_recovery as prmod
    calls = []
    class _Run:
        returncode = 0
        stderr = ""
    def _runner(argv, **kw):
        calls.append(argv)
        return _Run()
    eng = prmod.DynamicPackageRecovery(runner=_runner, import_probe=lambda m: True)
    return eng, calls


@pytest.mark.asyncio
async def test_selftest_self_heals_missing_dep_then_proves_failover():
    """The engine catches ModuleNotFoundError, triggers the isolated injection
    subprocess, hot-reloads, and the SECOND DoubleWord attempt proves the
    failover — no human, no manual pip."""
    events = []
    async def _bus(topic, data): events.append(data)
    dw, dw_state = _healing_dw_provider()
    recovery, pip_calls = _faked_recovery()

    st = lst.LoopbackSelfTest(dw_provider=dw, recovery=recovery, bus_publish=_bus)
    result = await st.run()

    assert result.state is lst.SelfTestState.FAILOVER_PROVEN
    assert result.recovered is True
    assert result.ledger["doubleword"] == "ready:recovered"
    assert dw_state["n"] == 2                       # failed once, then answered
    assert pip_calls and "uuid6" in pip_calls[0]    # injection subprocess fired
    # telemetry surfaced BOTH the recovery and the proof.
    assert any(d["type"] == "PACKAGE_RECOVERY" for d in events)
    assert any(d["type"] == "FAILOVER_PROVEN" and d.get("recovered") for d in events)


@pytest.mark.asyncio
async def test_selftest_degrades_when_dep_not_in_allowlist():
    """A missing dep NOT in the governed allowlist is refused (supply-chain
    guard) → no install, graceful degrade — the primary loop stays live."""
    async def _call(ctx):
        raise ModuleNotFoundError("No module named 'sketchypkg'")
    dw = af.Provider("doubleword", _call)
    from backend.api import package_recovery as prmod
    pip_calls = []
    recovery = prmod.DynamicPackageRecovery(
        runner=lambda argv, **kw: pip_calls.append(argv),
        import_probe=lambda m: True)
    st = lst.LoopbackSelfTest(dw_provider=dw, recovery=recovery, bus_publish=lambda t, d: _noop())
    result = await st.run()
    assert result.state is lst.SelfTestState.DEGRADED
    assert not pip_calls                            # NEVER installed an unknown pkg


def test_converged_boot_self_heals_to_system_ready():
    """MANDATE 4 — end-to-end: the converged --headless boot catches the
    missing uuid6, self-heals via the injection subprocess, reloads, completes
    the DoubleWord self-test, and transitions to SYSTEM_READY / FAILOVER_PROVEN."""
    from fastapi.testclient import TestClient
    from backend.api import converged_headless as ch
    from backend.api import progressive_hydration as ph

    async def _ok(): return None
    subs = [ph.Subsystem("stub", _ok)]
    dw, _ = _healing_dw_provider()
    recovery, pip_calls = _faked_recovery()

    app = ch.create_converged_app(
        subsystems=subs, dw_provider=dw, recovery=recovery,
        run_selftest=True, mount_router=False)

    @app.post("/api/command")
    async def _cmd():
        return {"status": "accepted"}

    with TestClient(app) as client:
        assert client.post("/api/command").status_code == 200   # binds instantly
        import time; deadline = time.time() + 3
        final = {}
        while time.time() < deadline:
            final = client.get("/api/system/status").json()
            if final["system_state"] == "ready":
                break
            time.sleep(0.1)
        assert final["system_state"] == "ready"           # SYSTEM_READY after heal
        assert final["selftest"] == "failover_proven"     # DW proven post-recovery
    assert pip_calls and "uuid6" in pip_calls[0]           # the heal really ran


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
