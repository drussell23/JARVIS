"""Bulletproof spine for the Autonomous Wake-and-Execute (AWE) Trigger.

Mandated assertions, all against REAL infra (real StreamEventBroker, real SQLite
on a temp file, real guarded-UPDATE lock) so the fakes cannot mask a buggy
contract:

  (1) the trigger IDLES while the state is DEGRADED,
  (2) a broker emission of HEALTHY (DEGRADED→HEALTHY edge) INSTANTLY fires it,
  (3) the atomic soak lock is ACQUIRED,
  (4) a flap back to DEGRADED then HEALTHY a moment later does NOT fire a second
      parallel swarm (single-launch under flapping), and
  (5) the injected swarm strategy EXECUTES CLEANLY (terminal breadcrumb emitted).

Only the launch_fn is a fake (a counting coroutine) — everything it touches
(broker delivery, edge detection, the SQLite compare-and-swap) is the real code.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from backend.core.ouroboros.governance.awe_trigger import AWETrigger
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_PROVIDER_STATE_CHANGED,
    get_default_broker,
    reset_default_broker,
)
from backend.core.ouroboros.governance.soak_execution_lock import read_soak_lock


async def _wait_for(cond, timeout: float = 2.0) -> None:
    async def _loop() -> None:
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_loop(), timeout)


def _publish(provider: str, state: str, previous_state: str) -> None:
    """Emit a real provider_state_changed frame onto the real broker — the exact
    delivery path the production ProviderStateWatcher uses."""
    get_default_broker().publish(
        EVENT_TYPE_PROVIDER_STATE_CHANGED,
        provider,
        {"provider": provider, "state": state, "previous_state": previous_state},
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "chunk_strategy.db")


def _make_trigger(db_path, launch_fn, crumbs):
    return AWETrigger(
        launch_fn=launch_fn,
        db_factory=lambda: sqlite3.connect(db_path),
        breadcrumb_fn=lambda et, p: crumbs.append((et, dict(p))),
        cooldown_s=3600.0,           # any flap within an hour is suppressed
        run_id_factory=lambda: "run-fixed",
    )


async def test_awe_full_recovery_lifecycle(db_path):
    reset_default_broker()
    launched: list = []
    launch_started = asyncio.Event()

    async def fake_launch(run_id: str):
        launched.append(run_id)
        launch_started.set()
        await asyncio.sleep(0)         # a real detached coroutine that completes
        return "swarm-ok"

    crumbs: list = []
    trig = _make_trigger(db_path, fake_launch, crumbs)
    trig.start()
    try:
        # Ensure the broker subscription is live before we publish (no race).
        await asyncio.wait_for(trig._subscribed.wait(), timeout=2.0)

        # (1) DEGRADED → the trigger idles: no launch, no lock claim.
        _publish("doubleword", "DEGRADED", "UNKNOWN")
        await asyncio.sleep(0.1)
        assert launched == []
        lock = read_soak_lock(sqlite3.connect(db_path))
        assert lock is None or lock.get("claimed", 0) == 0

        # (2) DEGRADED→HEALTHY edge → fires instantly.
        _publish("doubleword", "HEALTHY", "DEGRADED")
        await asyncio.wait_for(launch_started.wait(), timeout=2.0)
        assert launched == ["run-fixed"]

        # (3) the atomic lock was acquired.
        lock = read_soak_lock(sqlite3.connect(db_path))
        assert lock is not None and lock["claimed"] == 1
        assert lock["run_id"] == "run-fixed"

        # (4) a flap (HEALTHY→DEGRADED→HEALTHY) a moment later must NOT re-fire.
        _publish("doubleword", "DEGRADED", "HEALTHY")   # intermediate, not a launch edge
        _publish("doubleword", "HEALTHY", "DEGRADED")   # redundant recovery edge
        await asyncio.sleep(0.15)
        assert launched == ["run-fixed"], "flap must not launch a second swarm"
        assert trig._launch_count == 1
        # The suppression was observable.
        assert any(et == "awe_soak_suppressed" for et, _ in crumbs)

        # (5) the swarm strategy executed cleanly → terminal breadcrumb emitted.
        await _wait_for(lambda: any(et == "awe_soak_complete" for et, _ in crumbs))
        launched_types = [et for et, _ in crumbs]
        assert "awe_soak_launched" in launched_types
        assert "awe_soak_complete" in launched_types
        assert "awe_soak_failed" not in launched_types
    finally:
        await trig.stop()
        reset_default_broker()


async def test_awe_idles_on_non_healthy_states(db_path):
    """A stricter idle proof: UNKNOWN and HEALTHY→HEALTHY (a non-edge) never
    fire, only a true transition into HEALTHY does."""
    reset_default_broker()
    launched: list = []

    async def fake_launch(run_id: str):
        launched.append(run_id)
        return "ok"

    trig = _make_trigger(db_path, fake_launch, [])
    # Drive on_state_event directly for exhaustive edge coverage.
    assert await trig.on_state_event({"state": "DEGRADED", "previous_state": "HEALTHY"}) is False
    assert await trig.on_state_event({"state": "UNKNOWN", "previous_state": "DEGRADED"}) is False
    assert await trig.on_state_event({"state": "HEALTHY", "previous_state": "HEALTHY"}) is False
    assert await trig.on_state_event({}) is False
    assert launched == []
    # The one true edge fires (the soak is DETACHED, so await it to observe).
    assert await trig.on_state_event({"state": "HEALTHY", "previous_state": "DEGRADED"}) is True
    await _wait_for(lambda: launched == ["run-fixed"])
    await trig.stop()


async def test_awe_lock_is_atomic_single_winner(db_path):
    """Two concurrent trigger instances sharing the DB: the guarded UPDATE lets
    exactly ONE win the claim (compare-and-swap), even racing the same edge."""
    reset_default_broker()
    winners: list = []

    def make(idx):
        async def launch(run_id):
            winners.append(idx)
            return idx
        return _make_trigger(db_path, launch, [])

    t1, t2 = make(1), make(2)
    # Fire the same recovery edge at both, concurrently.
    r1, r2 = await asyncio.gather(
        t1.on_state_event({"state": "HEALTHY", "previous_state": "DEGRADED"}),
        t2.on_state_event({"state": "HEALTHY", "previous_state": "DEGRADED"}),
    )
    assert (r1, r2) in [(True, False), (False, True)], "exactly one claim wins"
    # Let the single winner's detached soak run.
    await _wait_for(lambda: len(winners) == 1)
    assert len(winners) == 1
    lock = read_soak_lock(sqlite3.connect(db_path))
    assert lock is not None and lock["claimed"] == 1
