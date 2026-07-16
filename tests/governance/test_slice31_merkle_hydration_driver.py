"""Slice 31 — Merkle Cartographer hydration driver + runtime reachability.

The Phase 11 merkle stack shipped consumers (OpportunityMiner / TodoScanner /
DocStaleness subtree consults) but `update_full()` had ZERO production
callers — the cartographer was never hydrated, `_root` stayed None, every
`subtree_hash()` returned "", and every consumer fail-safed to legacy O(N)
scans silently (proven live: 5 quiet-soak cycles in bt-2026-07-16-031323,
all full scans). The 22-test consumer spine passed because test fixtures
hydrate the cartographer themselves — the wired-but-inert trap one layer
deeper: consumers wired, DRIVER severed.

This suite pins the driver at RUNTIME (not just source grep): a stubbed
boot actually exercises hydrate → subscribe → update_full, so a future
severed driver fails these tests instead of hiding behind fail-safes.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import backend.core.ouroboros.governance.merkle_cartographer as mc
from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerService,
)


# ── harness ──────────────────────────────────────────────────────────


class _StubBus:
    def __init__(self) -> None:
        self.subscriptions: List[Any] = []

    async def subscribe(self, topic, handler):
        self.subscriptions.append((topic, handler))


class _StubCartographer:
    def __init__(self) -> None:
        self.hydrate_calls = 0
        self.update_full_calls = 0
        self.incremental_batches: List[Any] = []

    def hydrate(self) -> int:
        self.hydrate_calls += 1
        return 42

    async def update_full(self):
        self.update_full_calls += 1
        return {"backend/x.py"}

    async def update_incremental(self, events):
        self.incremental_batches.append(list(events))
        return set()


def _bare_service(tmp_path: Path) -> IntakeLayerService:
    svc = IntakeLayerService.__new__(IntakeLayerService)
    svc._config = SimpleNamespace(project_root=tmp_path)
    return svc


async def _drain_hydration(svc) -> None:
    task = getattr(svc, "_merkle_hydration_task", None)
    if task is not None:
        await task


# ── runtime reachability: the driver actually drives ────────────────


async def test_driver_hydrates_subscribes_and_walks(tmp_path, monkeypatch):
    stub = _StubCartographer()
    monkeypatch.setattr(mc, "get_default_cartographer", lambda repo_root=None: stub)
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    bus = _StubBus()
    svc = _bare_service(tmp_path)

    await svc._start_merkle_hydration(bus)
    await _drain_hydration(svc)

    assert stub.hydrate_calls == 1
    assert stub.update_full_calls == 1
    assert bus.subscriptions and bus.subscriptions[0][0] == "fs.changed.*"
    assert svc._merkle_subscriber is not None


async def test_bus_wired_before_walk_scheduled(tmp_path, monkeypatch):
    """No event gap: the fs.changed subscription must exist by the time the
    boot walk is scheduled."""
    order: List[str] = []

    class _OrderCart(_StubCartographer):
        async def update_full(self):
            order.append("walk")
            return set()

    class _OrderBus(_StubBus):
        async def subscribe(self, topic, handler):
            order.append("subscribe")
            await super().subscribe(topic, handler)

    stub = _OrderCart()
    monkeypatch.setattr(mc, "get_default_cartographer", lambda repo_root=None: stub)
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    svc = _bare_service(tmp_path)
    await svc._start_merkle_hydration(_OrderBus())
    await _drain_hydration(svc)
    assert order.index("subscribe") < order.index("walk")


async def test_boot_walk_is_backgrounded_not_awaited_inline(tmp_path, monkeypatch):
    """Startup-starvation guard: _start_merkle_hydration must return while
    a slow walk is still in flight (the walk runs in its own task)."""
    gate = asyncio.Event()

    class _SlowCart(_StubCartographer):
        async def update_full(self):
            await gate.wait()
            self.update_full_calls += 1
            return set()

    stub = _SlowCart()
    monkeypatch.setattr(mc, "get_default_cartographer", lambda repo_root=None: stub)
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    svc = _bare_service(tmp_path)
    await asyncio.wait_for(svc._start_merkle_hydration(_StubBus()), timeout=2.0)
    assert stub.update_full_calls == 0  # returned while walk still gated
    gate.set()
    await _drain_hydration(svc)
    assert stub.update_full_calls == 1


async def test_master_flag_off_constructs_nothing(tmp_path, monkeypatch):
    calls = {"n": 0}

    def _spy(repo_root=None):
        calls["n"] += 1
        return _StubCartographer()

    monkeypatch.setattr(mc, "get_default_cartographer", _spy)
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "false")
    svc = _bare_service(tmp_path)
    await svc._start_merkle_hydration(_StubBus())
    assert calls["n"] == 0
    assert svc._merkle_hydration_task is None


async def test_driver_never_raises(tmp_path, monkeypatch):
    def _boom(repo_root=None):
        raise RuntimeError("cartographer down")

    monkeypatch.setattr(mc, "get_default_cartographer", _boom)
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    svc = _bare_service(tmp_path)
    await svc._start_merkle_hydration(_StubBus())  # must not raise
    assert svc._merkle_hydration_task is None


# ── incremental conduit: real subscriber end-to-end ──────────────────


async def test_fs_event_reaches_update_incremental(tmp_path, monkeypatch):
    """The full incremental chain with the REAL MerkleEventSubscriber:
    fs.changed event → handle → flush → cartographer.update_incremental."""
    stub = _StubCartographer()
    monkeypatch.setattr(mc, "get_default_cartographer", lambda repo_root=None: stub)
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    bus = _StubBus()
    svc = _bare_service(tmp_path)
    await svc._start_merkle_hydration(bus)
    await _drain_hydration(svc)

    _topic, handler = bus.subscriptions[0]
    # REAL-CONTRACT dispatch: TrinityEventBus calls handler(event) with a
    # SINGLE event object (trinity_event_bus._execute_handler) — the driver's
    # adapter must translate to the subscriber's (topic, payload) shape.
    # (The fakes-must-mirror-real-contract lesson: a two-arg stub dispatch
    # here masked exactly the TypeError this adapter exists to prevent.)
    event = SimpleNamespace(
        topic="fs.changed.modified",
        payload={"relative_path": "backend/x.py", "path": str(tmp_path / "backend/x.py")},
    )
    await handler(event)
    await svc._merkle_subscriber.flush()
    assert stub.incremental_batches, "fs event never reached update_incremental"


# ── anti-severed-driver pins ─────────────────────────────────────────


def test_driver_is_on_the_default_boot_path():
    """_build_components (the DEFAULT event-spine boot path) must call the
    hydration driver — the exact wire whose absence made the whole Phase 11
    merkle stack silently inert."""
    src = inspect.getsource(IntakeLayerService._build_components)
    assert "_start_merkle_hydration" in src


def test_update_full_has_a_production_caller():
    """The grep-callers class test: intake_layer_service must reference
    update_full (via the boot-hydrate closure). If a refactor severs this,
    the runtime tests above fail too — this pin names the invariant."""
    import backend.core.ouroboros.governance.intake.intake_layer_service as ils

    assert "update_full" in inspect.getsource(ils)


def test_stop_retires_the_driver():
    src = inspect.getsource(IntakeLayerService.stop)
    assert "_merkle_hydration_task" in src
    assert "_merkle_subscriber" in src
