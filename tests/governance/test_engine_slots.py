"""Local-lane concurrency follows the slots the engine REPORTS.

The pool was sized once, at hydrate time, before the served model's size was
cached, so the VRAM derivation fell back to 1 and was never revisited; the
fleet observer pinned lanes to the NODE count; and the J-Prime path carried a
hardcoded Semaphore(1). A llama.cpp engine launched with --parallel N serves N
(each slot's KV allocated at launch), and J-Prime reports N in /api/ps.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, List, Tuple

import pytest

from backend.core.ouroboros.governance.autonomy import engine_slots as es

EP = "http://127.0.0.1:11435"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    es.reset_for_tests()
    for var in ("JARVIS_LOCAL_PRIMARY_CONCURRENCY", "JARVIS_LOCAL_MAX_CONCURRENCY",
                "JARVIS_ENGINE_SLOTS_TTL_S"):
        monkeypatch.delenv(var, raising=False)
    yield
    es.reset_for_tests()


def _ps(*models: Tuple[int, Any]) -> dict:
    return {"models": [{"name": f"m{i}", "size_vram": vram,
                        **({} if slots is None else {"parallel": slots})}
                       for i, (vram, slots) in enumerate(models)]}


def _fetch(payload):
    async def f(_ep, _t):
        return payload
    return f


# --------------------------------------------------------------------------
# 1. the reading
# --------------------------------------------------------------------------

def test_the_primary_models_slots_are_read() -> None:
    """The model holding the most VRAM is the generation lane (a resident
    vision model's slots are not the coder's)."""
    n = asyncio.run(es.refresh(EP, fetch=_fetch(_ps((5_000, 1), (19_000, 3)))))
    assert n == 3 and es.slots(EP) == 3


def test_an_engine_that_does_not_report_slots_is_unknown() -> None:
    assert asyncio.run(es.refresh(EP, fetch=_fetch(_ps((19_000, None))))) is None
    assert es.slots(EP) is None


def test_an_unreachable_engine_keeps_the_last_reading() -> None:
    asyncio.run(es.refresh(EP, fetch=_fetch(_ps((19_000, 2)))))

    async def down(_ep, _t):
        raise OSError("connection refused")
    assert asyncio.run(es.refresh(EP, fetch=down, force=True)) == 2


def test_a_reading_inside_the_ttl_is_not_refetched() -> None:
    calls: List[int] = []

    async def f(_ep, _t):
        calls.append(1)
        return _ps((19_000, 2))
    asyncio.run(es.refresh(EP, fetch=f))
    asyncio.run(es.refresh(EP, fetch=f))
    assert len(calls) == 1


def test_listeners_hear_changes_only() -> None:
    heard: List[Tuple[str, int]] = []
    es.add_listener(lambda ep, n: heard.append((ep, n)))
    es.record(EP, 2)
    es.record(EP, 2)
    es.record(EP + "/", 3)          # the same endpoint, normalised
    assert heard == [(EP, 2), (EP, 3)]


def test_lanes_sum_slots_and_count_an_unknown_node_as_one() -> None:
    es.record(EP, 3)
    assert es.lanes_for([EP, "http://other:1"]) == 4


# --------------------------------------------------------------------------
# 2. the consumers follow it
# --------------------------------------------------------------------------

class _Pool:
    def __init__(self) -> None:
        self.calls: List[Tuple[int, str, bool]] = []

    def set_target_pool_size(self, n: int, *, source: str, lock: bool) -> bool:
        self.calls.append((n, source, lock))
        return True


def test_fleet_lanes_are_the_engines_slots_not_the_node_count() -> None:
    from backend.core.ouroboros.governance.governed_loop_service import _fleet_lane_sync
    pool = _Pool()
    _fleet_lane_sync(pool, 1, {"local": EP})
    assert pool.calls[-1] == (1, "fleet_topology", True), "unknown engine: one lane, as before"
    es.record(EP, 3)
    _fleet_lane_sync(pool, 1, {"local": EP})
    assert pool.calls[-1] == (3, "fleet_topology", True)


def test_capacity_prefers_the_engines_answer(monkeypatch) -> None:
    from backend.core.ouroboros.governance.autonomy.local_lane_capacity import (
        resolve_primary_concurrency,
    )
    from backend.core.ouroboros.governance.init_guard import mark_hydrated, reset_for_tests
    reset_for_tests()
    mark_hydrated()
    try:
        monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
        es.record(EP, 3)
        cap = resolve_primary_concurrency(cloud_default=6)
        assert (cap.concurrency, cap.basis) == (3, "engine_slots")
        monkeypatch.setenv("JARVIS_LOCAL_MAX_CONCURRENCY", "2")
        assert resolve_primary_concurrency(cloud_default=6).concurrency == 2, \
            "the operator's ceiling still binds"
    finally:
        reset_for_tests()


def test_the_jprime_limit_grows_and_shrinks_without_cutting_a_call() -> None:
    from backend.core.ouroboros.governance._governance_state import JPrimeState

    async def go() -> Tuple[int, int, bool]:
        st = JPrimeState.fresh()
        await st.jprime_sem.acquire()               # a call in flight
        st.resize(3)
        free_after_grow = st.jprime_sem._value       # 2 more may start
        st.resize(1)
        await asyncio.sleep(0)                       # the shrink takes back what is free
        locked_while_busy = st.jprime_sem.locked()
        st.jprime_sem.release()                      # the in-flight call ends normally
        return free_after_grow, st.slot_limit, locked_while_busy
    grown, limit, locked = asyncio.run(go())
    assert grown == 2 and limit == 1 and locked


def test_the_negotiator_reads_slots_and_moves_the_jprime_limit() -> None:
    """Wiring pin: the reading happens where the endpoint is already asked
    about itself, and the client-side limit follows it."""
    from backend.core.ouroboros.governance import candidate_generator as cg
    src = inspect.getsource(cg)
    i = src.index("_engine_slots.refresh(endpoint)")
    assert "self._jprime_state.resize(_slots)" in src[i:i + 200]
    assert src.index("_engine_slots.refresh(endpoint)") < src.index(
        "model_bytes = await _resolve_served_model_bytes(endpoint)")
