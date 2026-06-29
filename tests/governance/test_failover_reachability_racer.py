"""The Asynchronous Reachability Racer -- dynamic topology resolution.

Soak bt-2026-06-29-063702 awakened a real node but never reached SERVING: the
node-ready probe used the INTERNAL GCE hostname (``jarvis-prime-failover:11434``),
unreachable from the local-Mac orchestrator. The fix must NOT guess the
environment (no IS_LOCAL flags): it extracts BOTH the internal and external
endpoints and races them concurrently -- whichever returns a healthy 200 FIRST
is bound as the SERVING endpoint. Works on a Mac, a GCP pod, or anywhere, with
ZERO hardcoded environment checks.

TDD with an injected node-ready probe -- ZERO real network.
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _ctrl(ready_fn):
    return FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        node_ready_fn=ready_fn,
        clock_fn=lambda: 1000.0,
    )


INTERNAL = "http://10.128.0.5:11434"
EXTERNAL = "http://35.192.251.243:11434"


async def test_external_wins_when_internal_unreachable():
    """Local Mac: only the external natIP answers -> it's bound (no env flag)."""
    def ready(ep):
        return ep == EXTERNAL  # internal unreachable off-VPC

    ctrl = _ctrl(ready)
    winner = await ctrl._race_node_ready([INTERNAL, EXTERNAL])
    assert winner == EXTERNAL


async def test_internal_wins_when_on_vpc():
    """GCP pod: the internal IP answers -> it's bound (same code, no flag)."""
    def ready(ep):
        return ep == INTERNAL

    ctrl = _ctrl(ready)
    winner = await ctrl._race_node_ready([INTERNAL, EXTERNAL])
    assert winner == INTERNAL


async def test_none_when_all_unreachable():
    """Neither answers (node still booting) -> None (keep waiting next tick)."""
    ctrl = _ctrl(lambda ep: False)
    assert await ctrl._race_node_ready([INTERNAL, EXTERNAL]) is None


async def test_first_healthy_wins_under_race():
    """Both healthy but external answers FAST, internal hangs -> external wins
    the FIRST_COMPLETED race (not a fixed priority order)."""
    async def ready(ep):
        if ep == INTERNAL:
            await asyncio.sleep(0.5)   # slow
            return True
        await asyncio.sleep(0.01)      # fast
        return True

    ctrl = _ctrl(ready)
    winner = await ctrl._race_node_ready([INTERNAL, EXTERNAL])
    assert winner == EXTERNAL  # fastest healthy endpoint, dynamically bound


async def test_racer_failsoft_on_probe_error():
    """A probe that raises is just 'not ready' -- the other candidate can win."""
    def ready(ep):
        if ep == INTERNAL:
            raise RuntimeError("connection refused")
        return ep == EXTERNAL

    ctrl = _ctrl(ready)
    winner = await ctrl._race_node_ready([INTERNAL, EXTERNAL])
    assert winner == EXTERNAL


async def test_empty_candidates_returns_none():
    ctrl = _ctrl(lambda ep: True)
    assert await ctrl._race_node_ready([]) is None
