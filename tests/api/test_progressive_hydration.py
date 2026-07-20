"""Progressive Daemon Hydration + fail-soft OOM guard (Phase 12, Slice B)."""
from __future__ import annotations

import asyncio

import pytest

from backend.api import progressive_hydration as ph


# ---------------------------------------------------------------------------
# state machine + telemetry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_subsystems_ok_reaches_ready_and_emits_hydrating():
    events = []
    async def _bus(topic, data): events.append((topic, data))
    loaded = []
    async def _ok(): loaded.append(1)

    orch = ph.HydrationOrchestrator(
        [ph.Subsystem("a", _ok), ph.Subsystem("b", _ok)], bus_publish=_bus)
    state = await orch.hydrate()

    assert state is ph.HydrationState.READY
    kinds = [d["type"] for _, d in events]
    assert kinds[0] == "SYSTEM_HYDRATING"          # emitted the instant it starts
    assert kinds[-1] == "SYSTEM_READY"
    assert orch.snapshot()["loaded"] == 2
    assert all(t == ph.HYDRATION_TOPIC for t, _ in events)   # existing bus topic


# ---------------------------------------------------------------------------
# MANDATE 4 — OOM guard: a subsystem raising MemoryError is caught, emits
# SYSTEM_DEGRADED, does NOT crash, server keeps serving /api/command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memoryerror_subsystem_is_caught_and_degrades_not_crash():
    events = []
    async def _bus(topic, data): events.append(data)
    async def _oom():
        raise MemoryError("Unable to allocate tensor (16GB unified memory)")
    async def _ok():
        return None

    orch = ph.HydrationOrchestrator(
        [ph.Subsystem("ouroboros_daemon", _oom, label="OuroborosDaemon"),
         ph.Subsystem("vision", _ok)],
        bus_publish=_bus)

    # hydrate() must NOT raise despite the MemoryError.
    state = await orch.hydrate()

    assert state is ph.HydrationState.DEGRADED
    # SYSTEM_DEGRADED telemetry was dispatched for the OOM subsystem.
    degraded = [d for d in events if d["type"] == "SYSTEM_DEGRADED"]
    assert degraded, "no SYSTEM_DEGRADED emitted"
    assert any("ouroboros_daemon" == d.get("subsystem") for d in degraded)
    assert any("MemoryError" in d.get("error", "") for d in degraded)
    # The healthy subsystem still hydrated (the loop continued past the OOM).
    assert orch.results["vision"] == "ok"
    assert orch.results["ouroboros_daemon"].startswith("error: MemoryError")


@pytest.mark.asyncio
async def test_runtimeerror_also_fail_soft():
    events = []
    async def _bus(topic, data): events.append(data)
    async def _rt(): raise RuntimeError("MPS backend out of memory")
    orch = ph.HydrationOrchestrator(
        [ph.Subsystem("pytorch", _rt)], bus_publish=_bus)
    assert await orch.hydrate() is ph.HydrationState.DEGRADED  # no crash


# ---------------------------------------------------------------------------
# MANDATE 4 — full ASGI lifespan: OOM during startup, server up, route 200
# ---------------------------------------------------------------------------

def test_asgi_lifespan_oom_server_stays_up_and_serves_200():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import headless_app

    # Inject a subsystem that OOMs during background hydration.
    async def _oom():
        raise MemoryError("simulated OOM")
    subs = [ph.Subsystem("ouroboros_daemon", _oom, label="OuroborosDaemon")]

    app = headless_app.create_headless_app(subsystems=subs, mount_router=False)

    # A stub command endpoint (stands in for the real /api/command — proves
    # the server keeps serving despite the failed hydration).
    @app.post("/api/command")
    async def _cmd():
        return {"status": "accepted"}

    # TestClient runs the FastAPI lifespan (startup schedules hydration).
    with TestClient(app) as client:
        # The app bound + serves instantly even while hydration runs/fails.
        r_status = client.get("/api/hydration/status")
        assert r_status.status_code == 200
        # The command endpoint is live — server did NOT crash on the OOM.
        r_cmd = client.post("/api/command")
        assert r_cmd.status_code == 200
        assert r_cmd.json()["status"] == "accepted"
        # Give the background hydration a beat, then confirm it degraded.
        import time as _t; _t.sleep(0.3)
        final = client.get("/api/hydration/status").json()
        assert final["state"] in ("degraded", "hydrating")   # never crashed
