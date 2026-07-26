"""Lane tagging — who produced this output, carried not passed.

The session ContextVar answers "who asked?". This one answers "who is doing
it?". Both are read at the same interceptor, because both are questions only
the emitter's execution context can answer.

The thread boundary is the part that needs proving. ``ContextVar`` crosses
``await`` for free, but measured on this interpreter:

    asyncio.to_thread         -> propagates
    loop.run_in_executor      -> LOST
    ThreadPoolExecutor.submit -> LOST

A worker that hands heavy compute or disk I/O to an executor therefore
orphans everything that function emits, and untagged output is
indistinguishable from the organism speaking as itself.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import json
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.battle_test import attach_session
from backend.core.ouroboros.battle_test.attach_session import (
    AMBIENT_LANE,
    current_lane,
    lane_scope,
)
from backend.core.ouroboros.battle_test.lane_rings import (
    ContextAwareThreadPool,
    LaneRegistry,
    get_lane_registry,
    reset_lane_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_lane_registry()
    with lane_scope(None), attach_session.session_scope(None):
        yield
    reset_lane_registry()


class _Writer:
    def __init__(self) -> None:
        self.frames: List[Dict[str, Any]] = []

    def is_closing(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# 1. the tag survives the async graph
# --------------------------------------------------------------------------

def test_default_lane_is_ambient() -> None:
    assert current_lane() == AMBIENT_LANE


async def test_lane_survives_awaits_and_child_tasks() -> None:
    seen = {}

    async def deep() -> None:
        await asyncio.sleep(0)
        seen["lane"] = current_lane()

    with lane_scope("swarm/chunk-7"):
        await asyncio.create_task(deep())
    assert seen["lane"] == "swarm/chunk-7"
    assert current_lane() == AMBIENT_LANE, "the scope leaked past its block"


async def test_lanes_nest_and_restore() -> None:
    with lane_scope("unit/a"):
        assert current_lane() == "unit/a"
        with lane_scope("unit/b"):
            assert current_lane() == "unit/b"
        assert current_lane() == "unit/a", "inner scope clobbered the outer"


async def test_concurrent_workers_do_not_share_a_lane() -> None:
    """The property that makes tagging worth anything under parallelism."""
    out: Dict[str, str] = {}

    async def work(lane: str) -> None:
        with lane_scope(lane):
            await asyncio.sleep(0)
            out[lane] = current_lane()

    await asyncio.gather(work("unit/1"), work("unit/2"), work("unit/3"))
    assert out == {"unit/1": "unit/1", "unit/2": "unit/2", "unit/3": "unit/3"}


def test_lane_scope_restores_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with lane_scope("unit/boom"):
            raise RuntimeError("worker exploded")
    assert current_lane() == AMBIENT_LANE


def test_tagging_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_LANE_TAGGING", "0")
    with lane_scope("unit/x"):
        assert current_lane() == AMBIENT_LANE


# --------------------------------------------------------------------------
# 2. the thread boundary — the requirement
# --------------------------------------------------------------------------

async def test_run_in_executor_loses_the_lane_without_the_wrapper() -> None:
    """Pins the defect the wrapper exists for. If a future Python makes
    run_in_executor context-preserving, this test tells us the wrapper is
    redundant rather than letting it linger as cargo."""
    loop = asyncio.get_running_loop()
    with lane_scope("unit/plain"):
        with cf.ThreadPoolExecutor(1) as ex:
            got = await loop.run_in_executor(ex, current_lane)
    assert got == AMBIENT_LANE, (
        "run_in_executor now preserves context — the wrapper may be dropped"
    )


async def test_context_aware_pool_carries_the_lane_into_the_thread() -> None:
    loop = asyncio.get_running_loop()
    with lane_scope("unit/threaded"):
        with ContextAwareThreadPool(1) as pool:
            via_submit = pool.submit(current_lane).result(timeout=5)
            via_loop = await loop.run_in_executor(pool, current_lane)
    assert via_submit == "unit/threaded"
    assert via_loop == "unit/threaded"


async def test_threaded_work_routes_to_the_right_ring() -> None:
    """Requirement 2 end to end: a function dispatched to a background thread
    retains the lane AND its output lands in that lane's ring."""
    registry = get_lane_registry()

    def _heavy_disk_io() -> str:
        lane = current_lane()
        registry.record(lane, "wrote 4 files")
        return lane

    with lane_scope("unit/io"):
        with ContextAwareThreadPool(2) as pool:
            assert pool.submit(_heavy_disk_io).result(timeout=5) == "unit/io"

    assert [ln.text for ln in registry.history("unit/io")] == ["wrote 4 files"]
    assert registry.history(AMBIENT_LANE) == [], (
        "threaded output was orphaned into the ambient lane"
    )


async def test_two_pooled_workers_keep_separate_lanes() -> None:
    registry = get_lane_registry()

    def _emit() -> None:
        registry.record(current_lane(), f"work in {current_lane()}")

    with ContextAwareThreadPool(2) as pool:
        futures = []
        for lane in ("swarm/a", "swarm/b"):
            with lane_scope(lane):
                futures.append(pool.submit(_emit))
        for f in futures:
            f.result(timeout=5)

    assert len(registry.history("swarm/a")) == 1
    assert len(registry.history("swarm/b")) == 1
    assert "swarm/a" in registry.history("swarm/a")[0].text


# --------------------------------------------------------------------------
# 3. the IPC bridge carries the tag (requirement 1)
# --------------------------------------------------------------------------

async def test_swarm_output_carries_its_lane_across_the_bridge() -> None:
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )

    w = _Writer()
    bridge = CockpitAttachBridge()
    bridge._loop = asyncio.get_running_loop()   # type: ignore[attr-defined]
    bridge._clients.add(w)                      # type: ignore[arg-type]

    with lane_scope("swarm/chunk-3"):
        bridge.publish_markup("⏺ Update(saga.py)")

    assert w.frames, "nothing reached the client"
    assert w.frames[0].get("lane") == "swarm/chunk-3"
    assert [ln.text for ln in get_lane_registry().history("swarm/chunk-3")] == [
        "⏺ Update(saga.py)"
    ]


async def test_ambient_output_carries_no_lane_key() -> None:
    """Absence is meaningful: the deck already treats untagged output as the
    organism itself, so stamping `lane: "ambient"` would make every frame
    look worker-produced."""
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )

    w = _Writer()
    bridge = CockpitAttachBridge()
    bridge._loop = asyncio.get_running_loop()   # type: ignore[attr-defined]
    bridge._clients.add(w)                      # type: ignore[arg-type]

    bridge.publish_markup("⏺ Bash(ls)")
    assert "lane" not in w.frames[0]


# --------------------------------------------------------------------------
# 4. the rings
# --------------------------------------------------------------------------

def test_ring_is_bounded_and_reports_what_it_dropped() -> None:
    """A truncated pane must say it is truncated."""
    reg = LaneRegistry(ring=5)
    for i in range(20):
        reg.record("unit/x", f"line {i}")
    hist = reg.history("unit/x")
    assert len(hist) == 5
    assert hist[-1].text == "line 19", "the ring kept the wrong end"
    assert reg.dropped("unit/x") == 15


def test_registry_evicts_the_least_recently_active_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_LANE_MAX", "2")
    clock = {"t": 0.0}
    reg = LaneRegistry(ring=10, clock=lambda: clock["t"])
    reg.record("old", "a")
    clock["t"] = 10.0
    reg.record("mid", "b")
    clock["t"] = 20.0
    reg.record("new", "c")
    assert "old" not in reg.lanes(), "a runaway spawner can grow the registry"
    assert reg.evicted_lanes == 1


def test_history_is_oldest_first_for_hydration() -> None:
    """D3 hydrates a focused pane from this — order is the contract."""
    reg = LaneRegistry(ring=10)
    for i in range(3):
        reg.record("unit/x", f"line {i}")
    assert [ln.text for ln in reg.history("unit/x")] == [
        "line 0", "line 1", "line 2",
    ]


def test_record_never_raises_on_junk() -> None:
    reg = LaneRegistry(ring=3)
    for bad in (None, "", 123, object()):
        reg.record(bad, "x")        # type: ignore[arg-type]
        reg.record("unit/x", bad)   # type: ignore[arg-type]
    assert reg.history("nonexistent") == []
