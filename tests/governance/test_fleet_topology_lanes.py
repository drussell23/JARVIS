"""Dynamic Fleet Registry Service Discovery + the Immutability Lock.

bt-iso-1782982736: 6 pool workers serialized on ONE remote L4 — each op's
effective round wall multiplied by queue depth, violating the exclusive-access
physics every derived budget assumes. The hard-assign fix (#69824) is
DEPRECATED as static config. The pool must TRACK the mesh topology live:

  * The FleetRegistry (the existing class->endpoint map the failover FSM
    already registers winners into) grows observer hooks -- extensible
    payloads (the snapshot dict rides the channel; future golden-image
    VRAM/tier metadata extends it without API change).
  * The pool gains cooperative live resizing: lanes strictly equal the
    number of registered sovereign endpoints while the mesh serves; the
    configured size governs only the mesh-dormant (DW-era) regime.
  * The Immutability Lock: while topology governs, lower-ranked writers
    (env/config/manifest) are silently REJECTED -- hardware-derived truth
    is mathematically authoritative.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, cast

import pytest

from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)
from backend.core.ouroboros.governance.fleet_registry import (
    get_fleet_registry,
    reset_fleet_registry,
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_fleet_registry()
    yield
    reset_fleet_registry()


class _IdleOrch:
    async def run(self, ctx: Any) -> Any:  # pragma: no cover
        await asyncio.sleep(0.01)
        return ctx


def _live_workers(pool) -> int:
    return sum(1 for t in pool._workers if not t.done())


async def _converge(pool, n, timeout_s=6.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if _live_workers(pool) == n:
            return True
        await asyncio.sleep(0.1)
    return _live_workers(pool) == n


class TestRegistryObservers:
    def test_subscribe_fires_on_register_and_unregister(self):
        reg = get_fleet_registry()
        seen: List[dict] = []
        reg.subscribe(lambda snap: seen.append(dict(snap)))
        reg.register("gpu", "http://n1:11434")
        reg.register("gpu2", "http://n2:11434")
        reg.unregister("gpu")
        assert seen == [
            {"gpu": "http://n1:11434"},
            {"gpu": "http://n1:11434", "gpu2": "http://n2:11434"},
            {"gpu2": "http://n2:11434"},
        ]

    def test_broken_observer_never_breaks_registry(self):
        reg = get_fleet_registry()

        def _boom(snap):
            raise RuntimeError("observer bug")

        reg.subscribe(_boom)
        reg.register("gpu", "http://n1:11434")     # must not raise
        assert reg.is_registered("gpu")


class TestPoolLiveResize:
    async def test_shrink_to_topology_then_grow_back(self):
        pool = BackgroundAgentPool(orchestrator=cast(Any, _IdleOrch()),
                                   pool_size=3, queue_size=4)
        await pool.start()
        try:
            assert _live_workers(pool) == 3
            assert pool.set_target_pool_size(1, source="fleet_topology", lock=True)
            assert await _converge(pool, 1)
            assert pool.set_target_pool_size(3, source="fleet_topology", lock=False)
            assert await _converge(pool, 3)
        finally:
            await pool.stop()

    async def test_immutability_lock_rejects_lower_rank(self):
        pool = BackgroundAgentPool(orchestrator=cast(Any, _IdleOrch()),
                                   pool_size=3, queue_size=4)
        await pool.start()
        try:
            assert pool.set_target_pool_size(1, source="fleet_topology", lock=True)
            assert await _converge(pool, 1)
            # env/config/manifest writers are silently rejected while locked
            assert pool.set_target_pool_size(6, source="config") is False
            await asyncio.sleep(0.3)
            assert _live_workers(pool) == 1
        finally:
            await pool.stop()

    async def test_unlocked_config_resize_allowed(self):
        pool = BackgroundAgentPool(orchestrator=cast(Any, _IdleOrch()),
                                   pool_size=2, queue_size=4)
        await pool.start()
        try:
            assert pool.set_target_pool_size(3, source="config")
            assert await _converge(pool, 3)
        finally:
            await pool.stop()


class TestGlsLaneSync:
    def test_serving_fleet_locks_lanes_to_node_count(self):
        from backend.core.ouroboros.governance import governed_loop_service as gls
        calls = {}

        class _FakePool:
            def set_target_pool_size(self, n, *, source="config", lock=False):
                calls.update(n=n, source=source, lock=lock)
                return True

        gls._fleet_lane_sync(_FakePool(), 6, {"gpu": "http://n1:11434"})
        assert calls == {"n": 1, "source": "fleet_topology", "lock": True}

    def test_empty_fleet_restores_configured_unlocked(self):
        from backend.core.ouroboros.governance import governed_loop_service as gls
        calls = {}

        class _FakePool:
            def set_target_pool_size(self, n, *, source="config", lock=False):
                calls.update(n=n, source=source, lock=lock)
                return True

        gls._fleet_lane_sync(_FakePool(), 6, {})
        assert calls == {"n": 6, "source": "fleet_topology", "lock": False}


def test_driver_hard_assign_deprecated():
    """Source pin: the driver no longer statically assigns the pool size --
    topology discovery owns the lane count."""
    import pathlib
    src = pathlib.Path("scripts/isomorphic_a1_local.py").read_text()
    assert 'env["JARVIS_BG_POOL_SIZE"]' not in src


def test_gls_subscribes_lane_sync():
    """Source pin: GLS wires the registry observer to the pool after start."""
    import pathlib
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = pathlib.Path(gls.__file__).read_text()
    assert "_fleet_lane_sync" in src and ".subscribe(" in src
