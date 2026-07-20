"""Actor-Model Fault Isolation for OuroborosDaemon (Phase 12, Slice C)."""
from __future__ import annotations

import asyncio

import pytest

from backend.api import ouroboros_actor as oa
from backend.api import progressive_hydration as ph


# ---------------------------------------------------------------------------
# DAG topology (mandate 2a)
# ---------------------------------------------------------------------------

def test_topological_order_respects_dependencies():
    async def _n(): return None
    subs = [
        ph.Subsystem("ouroboros", _n, depends_on=("governed_loop", "oracle")),
        ph.Subsystem("oracle", _n),
        ph.Subsystem("governed_loop", _n, depends_on=("oracle",)),
    ]
    order = [s.name for s in ph.topological_order(subs)]
    assert order.index("oracle") < order.index("governed_loop")
    assert order.index("governed_loop") < order.index("ouroboros")   # O+V last


def test_topological_order_detects_cycle():
    async def _n(): return None
    subs = [ph.Subsystem("a", _n, depends_on=("b",)),
            ph.Subsystem("b", _n, depends_on=("a",))]
    with pytest.raises(ValueError):
        ph.topological_order(subs)


@pytest.mark.asyncio
async def test_hydrate_skips_ouroboros_when_dependency_degrades():
    """DAG gate: OuroborosDaemon must NOT run if oracle/governed_loop
    aren't READY — it is skipped fail-soft, server stays up."""
    events = []
    async def _bus(t, d): events.append(d)
    async def _ok(): return None
    async def _fail(): raise MemoryError("oracle OOM")
    awakened = []
    async def _ouroboros(): awakened.append(1)

    subs = [
        ph.Subsystem("oracle", _fail, label="Oracle"),               # OOMs
        ph.Subsystem("governed_loop", _ok, depends_on=("oracle",)),  # dep failed
        ph.Subsystem("ouroboros", _ouroboros,
                     depends_on=("oracle", "governed_loop")),        # gated
    ]
    orch = ph.HydrationOrchestrator(subs, bus_publish=_bus)
    state = await orch.hydrate()

    assert state is ph.HydrationState.DEGRADED
    assert awakened == []                          # O+V NEVER ran against un-ready kernel
    assert orch.results["ouroboros"].startswith("skipped")
    assert orch.results["governed_loop"].startswith("skipped")


# ---------------------------------------------------------------------------
# MANDATE 4 — Actor: awaken() RuntimeError → isolated, OUROBOROS_FAULT,
# server up, exponential backoff retry queued
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_awaken_runtimeerror_is_isolated_and_backs_off():
    events = []
    async def _bus(topic, data): events.append((topic, data))
    slept = []
    async def _sleep(s): slept.append(s)

    class _FaultyDaemon:
        async def awaken(self):
            raise RuntimeError("subprocess panic in autonomous loop")

    actor = oa.OuroborosActor(
        lambda: _FaultyDaemon(), bus_publish=_bus,
        max_restarts=2, base_backoff_s=1.0, cap_backoff_s=60.0, sleeper=_sleep)

    # run() NEVER raises despite the RuntimeError (fault isolation).
    final = await actor.run()

    assert final is oa.ActorState.FAULTED          # settled, not crashed
    faults = [d for t, d in events if d["type"] == "OUROBOROS_FAULT"]
    assert faults, "no OUROBOROS_FAULT dispatched"
    assert all(t == oa.FAULT_TOPIC for t, _ in events)
    assert any("RuntimeError" in f["fault"] for f in faults)
    # Exponential backoff retries were queued: 1s, 2s.
    assert slept == [1.0, 2.0]
    assert faults[-1]["exhausted"] is True         # budget exhausted, still no crash


@pytest.mark.asyncio
async def test_actor_awakes_cleanly_when_daemon_ok():
    async def _bus(t, d): pass
    class _GoodDaemon:
        async def awaken(self): return "awake"
    actor = oa.OuroborosActor(lambda: _GoodDaemon(), bus_publish=_bus,
                              sleeper=lambda s: asyncio.sleep(0))
    assert await actor.run() is oa.ActorState.AWAKE
    assert actor.restart_count == 0


def test_actor_wired_in_app_keeps_server_up_on_ouroboros_fault():
    """Full mandate-4 integration: the Actor faults in the background; the
    ASGI server stays up and /api/command still returns 200."""
    from fastapi.testclient import TestClient
    from backend.api import headless_app

    started = {}
    async def _load_ouroboros():
        # A subsystem that starts the Actor with a faulty daemon.
        class _Faulty:
            async def awaken(self): raise RuntimeError("O+V panic")
        actor = oa.OuroborosActor(lambda: _Faulty(), bus_publish=lambda t, d: _noop(),
                                  max_restarts=1, base_backoff_s=0.01, sleeper=lambda s: asyncio.sleep(0))
        started["actor"] = actor
        actor.start()                              # isolated background task

    async def _noop(): return None

    subs = [ph.Subsystem("ouroboros", _load_ouroboros, label="OuroborosDaemon")]
    app = headless_app.create_headless_app(subsystems=subs, mount_router=False)

    @app.post("/api/command")
    async def _cmd():
        return {"status": "accepted"}

    with TestClient(app) as client:
        assert client.get("/api/hydration/status").status_code == 200
        # Server did NOT crash on the O+V fault.
        assert client.post("/api/command").status_code == 200
