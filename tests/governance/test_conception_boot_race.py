"""Conception boot-ordering race: lifecycle barrier + atomic register-drain.

bt-2026-07-17-085445 dropped the run's ONLY blueprint: the DreamEngine dreamed
at 01:55:39, the ConceptionBridge armed its observer at 01:55:40 — the payload
was produced into a world with no listener. A bare get_blueprints() pull only
moves the race one instruction over (TOCTOU). These prove: (1) a blueprint
produced BEFORE the observer arms is atomically drained to it, (2) a blueprint
firing DURING registration is never dropped, (3) the barrier makes the
DreamEngine wait for the observer before burning inference.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
from backend.core.ouroboros.consciousness.dream_metrics import DreamMetricsTracker
from backend.core.ouroboros.consciousness.types import ConsciousnessConfig
import backend.core.ouroboros.governance.conception_proposal_bridge as cpb


def _cfg():
    return ConsciousnessConfig(
        enabled=True, health_poll_interval_s=30.0, dream_enabled=True,
        dream_idle_threshold_s=300.0, dream_reentry_cooldown_s=60.0,
        dream_max_minutes_per_day=120.0, dream_blueprint_ttl_hours=24.0,
        prophecy_enabled=True, memory_ttl_hours=168.0, briefing_on_startup=True)


def _engine(tmp):
    d = tmp / "dreams"; d.mkdir(exist_ok=True)
    return DreamEngine(
        health_cortex=MagicMock(), memory_engine=MagicMock(),
        activity_monitor=MagicMock(), resource_governor=MagicMock(),
        metrics_tracker=DreamMetricsTracker(), config=_cfg(), persistence_dir=d)


@pytest.fixture(autouse=True)
def _reset_barrier():
    cpb._reset_observers_armed_for_tests()
    yield
    cpb._reset_observers_armed_for_tests()


# ---- atomic register-and-drain (the TOCTOU-proof core) --------------------


async def test_blueprint_produced_before_observer_is_drained(tmp_path):
    """The exact live failure: blueprint stored, THEN observer registers →
    the historical blueprint must be delivered, not dropped."""
    eng = _engine(tmp_path)
    bp = MagicMock(); bp.blueprint_id = "bp-1"
    eng._blueprints["bp-1"] = bp                      # produced pre-arm (01:55:39)

    received = []
    async def observer(b): received.append(b.blueprint_id)
    eng.register_blueprint_observer(observer)         # armed later (01:55:40)
    await asyncio.sleep(0)                             # let the drain task run
    assert received == ["bp-1"]                        # reconciled, not dropped


async def test_blueprint_after_registration_is_notified(tmp_path):
    eng = _engine(tmp_path)
    received = []
    async def observer(b): received.append(b.blueprint_id)
    eng.register_blueprint_observer(observer)
    await asyncio.sleep(0)
    bp = MagicMock(); bp.blueprint_id = "bp-2"
    await eng._notify_blueprint_observers(bp)
    assert received == ["bp-2"]


async def test_simultaneous_fire_during_registration_drops_nothing(tmp_path):
    """TOCTOU: fire notify and register concurrently. Under the lock, the
    payload lands via drain OR notify (or both → bridge dedups) — never zero."""
    eng = _engine(tmp_path)
    bp = MagicMock(); bp.blueprint_id = "bp-race"
    eng._blueprints["bp-race"] = bp                    # already in the store

    seen = []
    async def observer(b): seen.append(b.blueprint_id)

    # register (drains) and notify (delivers) raced on the same loop
    await asyncio.gather(
        asyncio.to_thread(eng.register_blueprint_observer, observer),
        eng._notify_blueprint_observers(bp),
    )
    await asyncio.sleep(0)
    assert "bp-race" in seen                            # AT LEAST once, never zero


async def test_no_drop_across_many_interleavings(tmp_path):
    """Stress the interleaving: every blueprint must reach the observer."""
    eng = _engine(tmp_path)
    for i in range(20):
        b = MagicMock(); b.blueprint_id = f"bp-{i}"
        eng._blueprints[f"bp-{i}"] = b
    seen = set()
    async def observer(b): seen.add(b.blueprint_id)
    eng.register_blueprint_observer(observer)
    await asyncio.sleep(0)
    assert seen == {f"bp-{i}" for i in range(20)}       # all 20 reconciled


def test_register_is_idempotent(tmp_path):
    eng = _engine(tmp_path)
    async def observer(b): ...
    eng.register_blueprint_observer(observer)
    eng.register_blueprint_observer(observer)
    with eng._observer_lock:
        assert eng._blueprint_observers.count(observer) == 1


# ---- lifecycle barrier ----------------------------------------------------


async def test_barrier_blocks_until_observers_armed(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DREAM_INTAKE_BARRIER_TIMEOUT_S", "5")
    eng = _engine(tmp_path)

    task = asyncio.create_task(eng._await_intake_barrier())
    await asyncio.sleep(0.05)
    assert not task.done()                              # blocked — no arm yet
    cpb.mark_observers_armed()                          # the bridge arms
    await asyncio.wait_for(task, timeout=2)
    assert task.done()                                  # released deterministically


async def test_barrier_releases_immediately_when_bridge_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "false")
    eng = _engine(tmp_path)
    await asyncio.wait_for(eng._await_intake_barrier(), timeout=1)  # no block


async def test_barrier_times_out_and_proceeds(tmp_path, monkeypatch):
    """Fail-open: a never-arming intake must not wedge dreaming forever."""
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DREAM_INTAKE_BARRIER_TIMEOUT_S", "1")
    eng = _engine(tmp_path)
    # observers never armed → returns after the bounded timeout, does not hang
    await asyncio.wait_for(eng._await_intake_barrier(), timeout=3)


async def test_barrier_runs_only_once(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "false")
    eng = _engine(tmp_path)
    await eng._await_intake_barrier()
    assert eng._intake_barrier_awaited is True
    await eng._await_intake_barrier()                   # no-op second time


def test_barrier_primitive_mirrors_router_ready(monkeypatch):
    assert cpb.observers_are_armed() is False
    cpb.mark_observers_armed()
    assert cpb.observers_are_armed() is True


async def test_await_observers_armed_returns_bool(monkeypatch):
    cpb._reset_observers_armed_for_tests()
    assert await cpb.await_observers_armed(0.1) is False   # timeout
    cpb.mark_observers_armed()
    assert await cpb.await_observers_armed(0.1) is True    # armed
