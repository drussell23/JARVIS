"""QoS admission gate — priority-aware bounding of concurrent Aegis forwards.

The shape-aware read budget lifted the upstream read ceiling to 600s, so a single
forward can now hold its socket for minutes. This gate bounds the COUNT of
concurrent in-flight forwards and, only when that bound is saturated, admits
waiters in X-JARVIS-QoS-Tier priority order — critical event-loop traffic
(DreamEngine RT, Claude fallbacks) ahead of bulk background sensors.

These tests pin: the forced collision (a queued critical bypasses a queued bulk),
fail-open safety, master-off pass-through, tier parsing, and cross-module header
agreement.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.aegis import qos_admission as Q


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tier taxonomy + parsing
# ---------------------------------------------------------------------------


def test_tier_ordering_critical_is_highest_priority():
    # asyncio.PriorityQueue / heapq is a min-heap: lower int = dequeued first.
    assert Q.QoSTier.CRITICAL < Q.QoSTier.STANDARD < Q.QoSTier.BULK


@pytest.mark.parametrize("raw,expected", [
    ("critical", Q.QoSTier.CRITICAL), ("CRITICAL", Q.QoSTier.CRITICAL),
    ("immediate", Q.QoSTier.CRITICAL), ("0", Q.QoSTier.CRITICAL),
    ("standard", Q.QoSTier.STANDARD), ("normal", Q.QoSTier.STANDARD),
    ("bulk", Q.QoSTier.BULK), ("background", Q.QoSTier.BULK),
    ("speculative", Q.QoSTier.BULK), ("2", Q.QoSTier.BULK),
])
def test_tier_from_header_value(raw, expected):
    assert Q.tier_from_header_value(raw) == expected


def test_tier_absent_or_unknown_defaults_standard():
    # Never silently critical (would starve others), never silently bulk
    # (would starve itself) — the safe middle.
    assert Q.tier_from_header_value(None) == Q.QoSTier.STANDARD
    assert Q.tier_from_header_value("") == Q.QoSTier.STANDARD
    assert Q.tier_from_header_value("garbage") == Q.QoSTier.STANDARD


# ---------------------------------------------------------------------------
# THE forced collision — a queued critical bypasses a queued bulk
# ---------------------------------------------------------------------------


def test_qos_collision_critical_bypasses_bulk():
    async def scenario():
        gate = Q.ForwardAdmissionGate(max_concurrent=1)   # force saturation at 1
        order: list[str] = []
        release_holder = asyncio.Event()

        async def holder():
            async with gate.admit(Q.QoSTier.BULK):
                order.append("holder-start")
                await release_holder.wait()   # occupy the only slot
            order.append("holder-done")

        async def worker(name: str, tier: Q.QoSTier, started: asyncio.Event):
            started.set()                     # signal we're about to queue
            async with gate.admit(tier):
                order.append(name)
                await asyncio.sleep(0)        # touch the loop, then release

        h = asyncio.create_task(holder())
        # Wait until the holder owns the slot.
        while "holder-start" not in order:
            await asyncio.sleep(0)

        # Enqueue a BULK waiter FIRST, then a CRITICAL waiter. If admission were
        # FIFO, bulk would win; priority ordering must let critical jump ahead.
        b_started, c_started = asyncio.Event(), asyncio.Event()
        wb = asyncio.create_task(worker("bulk", Q.QoSTier.BULK, b_started))
        await b_started.wait()
        await asyncio.sleep(0.02)             # ensure bulk is parked in the heap
        wc = asyncio.create_task(worker("critical", Q.QoSTier.CRITICAL, c_started))
        await c_started.wait()
        await asyncio.sleep(0.02)             # ensure critical is parked too

        assert gate.snapshot()["waiting"] == 2   # both queued behind the holder

        release_holder.set()                  # free the slot
        await asyncio.gather(h, wb, wc)
        return order

    order = _run(scenario())
    # Critical was admitted before the earlier-queued bulk.
    assert order.index("critical") < order.index("bulk"), order


def test_gate_dormant_under_normal_load():
    async def scenario():
        gate = Q.ForwardAdmissionGate(max_concurrent=8)
        ran = []

        async def w(i):
            async with gate.admit(Q.QoSTier.STANDARD):
                ran.append(i)
                await asyncio.sleep(0)

        # 5 concurrent < limit 8 → nobody ever queues.
        await asyncio.gather(*(w(i) for i in range(5)))
        assert gate.snapshot() == {"in_flight": 0, "waiting": 0, "limit": 8}
        return ran

    assert sorted(_run(scenario())) == [0, 1, 2, 3, 4]


def test_slot_released_on_exception_no_leak():
    async def scenario():
        gate = Q.ForwardAdmissionGate(max_concurrent=1)

        async def boom():
            async with gate.admit(Q.QoSTier.STANDARD):
                raise RuntimeError("work blew up")

        with pytest.raises(RuntimeError):
            await boom()
        # The slot must be back — a leaked slot would wedge the next forward.
        assert gate.snapshot()["in_flight"] == 0

        ok = []
        async def after():
            async with gate.admit(Q.QoSTier.STANDARD):
                ok.append(True)
        await asyncio.wait_for(after(), timeout=1.0)
        return ok

    assert _run(scenario()) == [True]


# ---------------------------------------------------------------------------
# Master switch + robustness
# ---------------------------------------------------------------------------


def test_master_switch_default_on(monkeypatch):
    monkeypatch.delenv(Q._ENABLED_ENV_VAR, raising=False)
    assert Q.admission_enabled() is True
    monkeypatch.setenv(Q._ENABLED_ENV_VAR, "false")
    assert Q.admission_enabled() is False


def test_limit_env_tunable_and_safe(monkeypatch):
    monkeypatch.setenv(Q._MAX_CONCURRENT_ENV_VAR, "3")
    assert Q._max_concurrent() == 3
    monkeypatch.setenv(Q._MAX_CONCURRENT_ENV_VAR, "bad")
    assert Q._max_concurrent() == Q._DEFAULT_MAX_CONCURRENT
    monkeypatch.setenv(Q._MAX_CONCURRENT_ENV_VAR, "-1")
    assert Q._max_concurrent() == Q._DEFAULT_MAX_CONCURRENT


def test_singleton_reset():
    g1 = Q.get_admission_gate()
    assert Q.get_admission_gate() is g1
    Q.reset_gate_for_tests()
    assert Q.get_admission_gate() is not g1


# ---------------------------------------------------------------------------
# Cross-module header agreement (client ↔ gate ↔ forwarding strip)
# ---------------------------------------------------------------------------


def test_qos_header_agrees_across_modules():
    from backend.core.ouroboros.aegis import forwarding as F
    from backend.core.ouroboros.governance.aegis_provider_bridge import (
        QOS_TIER_HEADER_NAME as client_name,
    )
    assert Q.QOS_TIER_HEADER == F._QOS_TIER_HEADER == client_name == "X-JARVIS-QoS-Tier"


def test_forwarding_strips_qos_header_from_outbound():
    import inspect
    from backend.core.ouroboros.aegis import forwarding as F
    src = inspect.getsource(F.forward_request)
    assert "_QOS_TIER_HEADER.lower()" in src


def test_client_route_to_tier_mapping():
    from backend.core.ouroboros.governance.doubleword_provider import (
        _qos_tier_for_route,
    )
    assert _qos_tier_for_route("immediate") == "critical"
    assert _qos_tier_for_route("standard") == "standard"
    assert _qos_tier_for_route("complex") == "standard"
    assert _qos_tier_for_route("background") == "bulk"
    assert _qos_tier_for_route("speculative") == "bulk"
    assert _qos_tier_for_route("") == "standard"        # unknown → safe middle
    assert _qos_tier_for_route("nonsense") == "standard"
