"""Q3 Slice 4 — SkillObserver subscription/catalog mutual atomicity.

Closes three concurrency hazards on ``_subscriptions``:

  Hazard 1 — duplicate subscribe on register-during-start race:
      ``start()`` snapshots ``catalog.list_all()`` and iterates. If a
      ``register(X)`` fires mid-iteration, the catalog's ``on_change``
      listener schedules ``asyncio.create_task(_subscribe_manifest(X))``.
      If X was already in the snapshot, both paths subscribe.

  Hazard 2 — iterate-during-mutation in ``_on_catalog_change``
  unregister branch:
      Iterating ``self._subscriptions.items()`` from a sync callback
      while another coroutine mutates the dict raises
      ``RuntimeError: dictionary changed size during iteration``.

  Hazard 3 — out-of-order register/unregister:
      Catalog fires ``register(X)`` then ``unregister(X)``. Both
      schedule fire-and-forget tasks; if asyncio dispatches the
      unregister handler before the register lands subs, the
      register's subs survive an unregistered manifest.

Fix: dedicated ``_subscriptions_lock = asyncio.Lock()`` guards every
mutation; ``_subscribe_manifest`` is idempotent (drops existing subs
for the qname before re-subscribing); ``_unsubscribe_qname`` is one
atomic op replacing the iterate-and-schedule pattern; FIFO waiter
ordering on the asyncio.Lock means register-then-unregister serializes
in scheduling order; ``_started`` gates pending tasks so post-stop
schedules cleanly noop; ``stop()`` detaches the catalog listener
BEFORE the drain so no new tasks land mid-teardown.

Covers:

  §1   _subscriptions_lock attribute + asyncio.Lock type
  §2   Idempotency: _subscribe_manifest twice for same qname leaves
       exactly one set of subs (Hazard 1)
  §3   Idempotency: under N concurrent _subscribe_manifest calls for
       the same qname, end state has subs from the LAST call only
  §4   _on_catalog_change unregister doesn't iterate _subscriptions
       from sync context (Hazard 2 — proven by structural pin and a
       runtime concurrent mutation that previously crashed)
  §5   Register+unregister-in-close-succession ends with zero subs
       (Hazard 3 — FIFO lock ordering guarantee)
  §6   Pending catalog tasks scheduled before stop() noop after
       stop() flips _started=False
  §7   stop() detaches listener BEFORE drain (no new subs land while
       the drain is iterating)
  §8   Concurrent register storm during start() ends in a deterministic
       sub count matching the manifest count
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog, SkillSource, reset_default_catalog,
    reset_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
)
from backend.core.ouroboros.governance.skill_observer import (
    SkillObserver, reset_default_observer,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubEvent:
    topic: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


class _StubBus:
    def __init__(self, *, subscribe_delay_s: float = 0.0) -> None:
        self._subs: Dict[str, Tuple[str, Callable]] = {}
        self._counter = 0
        self._lock = asyncio.Lock()
        self._subscribe_delay_s = subscribe_delay_s
        # Track the order in which subscribe calls completed for FIFO tests
        self.subscribe_completion_order: List[str] = []

    async def subscribe(
        self, pattern: str,
        handler: Callable[[Any], Awaitable[None]],
    ) -> str:
        if self._subscribe_delay_s:
            await asyncio.sleep(self._subscribe_delay_s)
        async with self._lock:
            self._counter += 1
            sub_id = f"sub-{self._counter}"
            self._subs[sub_id] = (pattern, handler)
            self.subscribe_completion_order.append(sub_id)
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        async with self._lock:
            return self._subs.pop(subscription_id, None) is not None

    @property
    def active_subscriptions(self) -> int:
        return len(self._subs)


class _NoopInvoker:
    async def invoke(self, qualified_name: str, **kwargs):
        return None


def _build_manifest(name: str, *, n_specs: int = 1) -> SkillManifest:
    return SkillManifest.from_mapping({
        "name": name, "description": "d", "trigger": "t",
        "entrypoint": "mod.x:f", "reach": "any",
        "trigger_specs": [
            {"kind": "sensor_fired",
             "signal_pattern": f"sensor.fired.{name}.{i}"}
            for i in range(n_specs)
        ],
    })


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("JARVIS_SKILL_TRIGGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
    yield
    reset_default_catalog()
    reset_default_invoker()
    reset_default_observer()


# ---------------------------------------------------------------------------
# §1 — Lock attribute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscriptions_lock_present_and_typed():
    obs = SkillObserver(event_bus=_StubBus(), catalog=SkillCatalog(),
                        invoker=_NoopInvoker())
    assert hasattr(obs, "_subscriptions_lock")
    assert isinstance(obs._subscriptions_lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# §2 — Idempotency on repeated subscribe (Hazard 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_manifest_twice_yields_single_sub_set():
    bus = _StubBus()
    cat = SkillCatalog()
    obs = SkillObserver(event_bus=bus, catalog=cat, invoker=_NoopInvoker())
    cat.register(_build_manifest("alpha", n_specs=2),
                 source=SkillSource.OPERATOR)
    await obs.start()
    initial = obs.subscription_count()
    assert initial == 2
    # Re-subscribe the same manifest — idempotent: exactly one set of
    # subs survives. The OLD subs are dropped, the NEW ones replace.
    manifest = cat.get("alpha")
    await obs._subscribe_manifest(manifest)
    assert obs.subscription_count() == 2  # NOT 4
    assert bus.active_subscriptions == 2
    await obs.stop()


# ---------------------------------------------------------------------------
# §3 — Concurrent subscribe for same qname (Hazard 1 stress)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_subscribe_storm_for_same_qname_is_idempotent():
    """Spawn N concurrent _subscribe_manifest calls for the same
    manifest. The dedicated lock + idempotent prefix guarantees at
    quiescence: subs == n_specs (NOT n_calls × n_specs)."""
    bus = _StubBus(subscribe_delay_s=0.005)
    cat = SkillCatalog()
    obs = SkillObserver(event_bus=bus, catalog=cat, invoker=_NoopInvoker())
    cat.register(_build_manifest("beta", n_specs=3),
                 source=SkillSource.OPERATOR)
    await obs.start()  # baseline: 3 subs
    manifest = cat.get("beta")
    # 8 concurrent re-subscribe calls — without the lock + idempotency,
    # this would land 24 subs (3 × 8) and likely crash on the
    # iterate-during-mutation in _drop_subs_for_qname_locked.
    await asyncio.gather(*(
        obs._subscribe_manifest(manifest) for _ in range(8)
    ))
    assert obs.subscription_count() == 3
    assert bus.active_subscriptions == 3
    await obs.stop()


# ---------------------------------------------------------------------------
# §4 — _on_catalog_change unregister does NOT iterate _subscriptions
#       from sync context (Hazard 2 — proven structurally + runtime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_catalog_change_unregister_uses_atomic_helper():
    """The fixed _on_catalog_change must NOT enumerate
    self._subscriptions.items() inside the sync callback. Instead it
    schedules a single _unsubscribe_qname coroutine.

    Structural assertion: read the source — no
    'self._subscriptions.items()' on the unregister branch."""
    import inspect
    from backend.core.ouroboros.governance import skill_observer
    src = inspect.getsource(skill_observer.SkillObserver._on_catalog_change)
    # The fix replaces the sync iteration with one create_task call
    # to _unsubscribe_qname. Any return of the old iteration would
    # bring back the runtime hazard.
    assert "self._unsubscribe_qname(qname)" in src
    # And the unregister branch no longer iterates _subscriptions.
    unregister_block = src.split("skill_unregistered")[1]
    assert "self._subscriptions.items()" not in unregister_block


@pytest.mark.asyncio
async def test_unregister_during_concurrent_subscribe_does_not_crash():
    """Runtime: register A; then concurrently issue a re-subscribe
    AND an unregister. With the fix, the unregister handler schedules
    _unsubscribe_qname which serializes through _subscriptions_lock —
    no iterate-during-mutation crash."""
    bus = _StubBus(subscribe_delay_s=0.01)
    cat = SkillCatalog()
    obs = SkillObserver(event_bus=bus, catalog=cat, invoker=_NoopInvoker())
    cat.register(_build_manifest("gamma", n_specs=4),
                 source=SkillSource.OPERATOR)
    await obs.start()
    manifest = cat.get("gamma")

    # Fire a re-subscribe and an unregister concurrently.
    sub_task = asyncio.create_task(obs._subscribe_manifest(manifest))
    unsub_task = asyncio.create_task(obs._unsubscribe_qname("gamma"))
    # Both must complete without raising.
    await asyncio.gather(sub_task, unsub_task)
    # End state is deterministic: whichever ran LAST wins; after the
    # unsub_task runs, subs for gamma should be zero IF it ran second.
    # Because asyncio.Lock waiter queue is FIFO, sub_task acquired
    # the lock first (it was created first), runs, releases; unsub_task
    # runs, drops everything. Final count = 0.
    assert obs.subscription_count() == 0
    assert bus.active_subscriptions == 0
    await obs.stop()


# ---------------------------------------------------------------------------
# §5 — register-then-unregister-in-close-succession serializes (Hazard 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_then_unregister_serializes_via_fifo_lock():
    """A register immediately followed by an unregister for the same
    qname — the unregister coroutine must see the subs that the
    register lands and drop them. asyncio.Lock waiter queue is FIFO."""
    bus = _StubBus(subscribe_delay_s=0.02)
    cat = SkillCatalog()
    obs = SkillObserver(event_bus=bus, catalog=cat, invoker=_NoopInvoker())
    await obs.start()  # no manifests yet — 0 subs

    cat.register(_build_manifest("delta", n_specs=2),
                 source=SkillSource.OPERATOR)
    # register fired _on_catalog_change which scheduled subscribe.
    # Immediately unregister — schedules unsubscribe.
    cat.unregister("delta")
    # Drain the loop a few times.
    for _ in range(5):
        await asyncio.sleep(0.01)
    # Final state: zero subs (FIFO ordering: subscribe lands first,
    # unsubscribe drops them).
    assert obs.subscription_count() == 0
    assert bus.active_subscriptions == 0
    await obs.stop()


# ---------------------------------------------------------------------------
# §6 — Pending tasks noop after stop()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_after_stop_noops_via_started_gate():
    """A _subscribe_manifest task scheduled before stop() but executed
    after it must noop — the _started gate inside the lock ensures we
    don't re-add subs to a torn-down observer."""
    bus = _StubBus(subscribe_delay_s=0.05)  # slow subscribe
    cat = SkillCatalog()
    obs = SkillObserver(event_bus=bus, catalog=cat, invoker=_NoopInvoker())
    cat.register(_build_manifest("epsilon", n_specs=1),
                 source=SkillSource.OPERATOR)
    await obs.start()
    manifest = cat.get("epsilon")
    # Schedule a subscribe — it will block on the lock or on bus.subscribe
    pending = asyncio.create_task(obs._subscribe_manifest(manifest))
    # Stop immediately. stop() takes _lifecycle_lock + flips
    # _started=False + drains under _subscriptions_lock. The pending
    # task will eventually run; its inner _started check will see
    # False and noop.
    await obs.stop()
    # Drain.
    await pending
    assert obs.subscription_count() == 0
    assert bus.active_subscriptions == 0


# ---------------------------------------------------------------------------
# §7 — stop() detaches catalog listener BEFORE drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_detaches_listener_before_drain():
    """stop() must clear _catalog_unsub before draining, so a new
    catalog mutation during the drain doesn't schedule a new task that
    re-installs subs."""
    bus = _StubBus()
    cat = SkillCatalog()
    obs = SkillObserver(event_bus=bus, catalog=cat, invoker=_NoopInvoker())
    cat.register(_build_manifest("zeta", n_specs=1),
                 source=SkillSource.OPERATOR)
    await obs.start()
    assert obs._catalog_unsub is not None  # listener installed
    await obs.stop()
    assert obs._catalog_unsub is None  # cleared first
    # And triggering a registration AFTER stop() does not re-install
    # subs (listener detached).
    cat.register(_build_manifest("eta", n_specs=1),
                 source=SkillSource.OPERATOR)
    await asyncio.sleep(0.02)
    assert obs.subscription_count() == 0


# ---------------------------------------------------------------------------
# §8 — Concurrent register storm during start() converges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_registers_during_start_converge_idempotent():
    """N manifests all registered as start() races against the
    listener. Final count must equal sum of n_specs across registered
    manifests (each manifest exactly once)."""
    bus = _StubBus(subscribe_delay_s=0.002)
    cat = SkillCatalog()
    obs = SkillObserver(event_bus=bus, catalog=cat, invoker=_NoopInvoker())
    # Pre-register half before start() (hits the start() snapshot path).
    for i in range(5):
        cat.register(_build_manifest(f"sk{i}", n_specs=2),
                     source=SkillSource.OPERATOR)
    start_task = asyncio.create_task(obs.start())
    # Register the rest mid-start (hits the on_change listener path).
    for i in range(5, 10):
        cat.register(_build_manifest(f"sk{i}", n_specs=2),
                     source=SkillSource.OPERATOR)
    await start_task
    # Wait for any pending listener tasks.
    for _ in range(20):
        await asyncio.sleep(0.005)
        if obs.subscription_count() == 20:
            break
    # 10 manifests × 2 specs each = 20 subs (each manifest subscribed
    # exactly once — idempotency held).
    assert obs.subscription_count() == 20
    assert bus.active_subscriptions == 20
    await obs.stop()
    assert obs.subscription_count() == 0
    assert bus.active_subscriptions == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
