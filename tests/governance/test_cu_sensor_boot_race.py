"""The HUD's first failures must survive governance boot.

THE RACE
----------
`main.py`'s HUD dispatch reaches this sensor through
`get_cu_execution_sensor()`, which constructs the singleton with **no
router**. `IntakeLayerService` attaches the real router later, during
progressive governance boot (`hud_governance_boot` Step 3). A HUD action that
failed in that window reached `_emit_envelope`, found `_router is None`, logged
a warning, and **dropped the emission** — while `_failure_window` kept the very
evidence that justified it.

The data was never lost. What was lost is subtler and worse: the *decision* was
evaluated exactly once, at a moment chosen by a producer that has no knowledge
of consumer readiness. If the third occurrence of a pattern landed pre-boot and
a fourth never came, the signal was gone despite having crossed the threshold.

THE FIX IS NOT A QUEUE
------------------------
The rolling window is already the buffer. Making the emission decision
**re-enterable** — one `_maybe_emit`, called from `record()` and again from
`_reconcile()` when a router attaches — is the whole repair. No second store,
no persistence layer, no new subsystem to keep in step.

The only genuinely missing state was `_latest_by_sig`: the window holds
timestamps, which is enough to COUNT a pattern and not enough to DESCRIBE one,
and an envelope needs the record.

WHY THIS SENSOR AND NOT THE OTHER
-----------------------------------
`deep_analysis_sensor` has the same `router is None` guard and does NOT need
this: it polls, so the next tick re-evaluates. `CUExecutionSensor` is
event-driven — "async start() — no-op" — so a missed evaluation is only
retried if the same real-world action fails again. Polling sensors self-heal;
event-driven ones cannot. That is the principled boundary for this fix.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from backend.core.ouroboros.governance.intake.sensors import cu_execution_sensor as ces
from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
    CUExecutionRecord,
    CUExecutionSensor,
)


class _Router:
    """Minimal router. Records what it was handed."""

    def __init__(self):
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return "accepted"


class _FlakyRouter(_Router):
    """Fails N times, then accepts."""

    def __init__(self, failures: int):
        super().__init__()
        self._left = failures

    async def ingest(self, envelope):
        if self._left > 0:
            self._left -= 1
            raise RuntimeError("router transient")
        return await super().ingest(envelope)


@pytest.fixture(autouse=True)
def _fresh_singleton():
    """The sensor is a process singleton; every test needs a clean one."""
    CUExecutionSensor._instance = None
    yield
    CUExecutionSensor._instance = None


def _fail(goal: str = "message alice saying hi", app: str = "Messages",
          ts: Optional[float] = None) -> CUExecutionRecord:
    rec = CUExecutionRecord(
        goal=goal, success=False, steps_completed=1, steps_total=3,
        elapsed_s=1.0, error="target not found", is_messaging=True,
        contact="alice", app=app,
    )
    if ts is not None:
        rec.timestamp = ts
    return rec


async def _settle(sensor=None):
    """Await the reconcile sweep.

    `asyncio.sleep(0)` used to be enough — reconcile finished inside one
    tick. It now awaits a journal write on a worker thread, so a single
    tick returns before the emission lands. `drain()` is deterministic;
    sleeping longer would only be flaky in a slower place.
    """
    s = sensor or CUExecutionSensor()
    await s.drain()


async def _fail_n(sensor, n: int, **kw):
    for _ in range(n):
        await sensor.record(_fail(**kw))


class TestTheRaceItself:
    @pytest.mark.asyncio
    async def test_pre_boot_failures_are_deferred_not_dropped(self):
        """THE regression. Three failures with no router used to log a warning
        and vanish."""
        sensor = CUExecutionSensor()                     # router=None
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        assert sensor.get_stats()["deferred_emissions"] >= 1
        assert sensor.get_stats()["pending_reconcile"] is True

        router = _Router()
        CUExecutionSensor(router=router)                  # governance boots
        await asyncio.sleep(0)                            # let the task run
        assert router.ingested, "the deferred pattern never reconciled"

    @pytest.mark.asyncio
    async def test_the_reconciled_envelope_says_it_was_deferred(self):
        """A late signal that renders identically to a fresh one is the
        provenance failure this codebase keeps finding."""
        sensor = CUExecutionSensor()
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        router = _Router()
        CUExecutionSensor(router=router)
        await _settle()
        ev = router.ingested[0]
        evidence = getattr(ev, "evidence", None) or ev["evidence"]
        assert evidence["deferred_by_boot"] is True
        assert evidence["age_s"] >= 0

    @pytest.mark.asyncio
    async def test_the_normal_path_is_not_marked_deferred(self):
        router = _Router()
        sensor = CUExecutionSensor(router=router)
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        ev = router.ingested[0]
        evidence = getattr(ev, "evidence", None) or ev["evidence"]
        assert evidence["deferred_by_boot"] is False

    @pytest.mark.asyncio
    async def test_below_threshold_stays_pending_across_the_attach(self):
        """Reconcile must not lower the bar. Two failures is not three."""
        sensor = CUExecutionSensor()
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD - 1)
        router = _Router()
        CUExecutionSensor(router=router)
        await _settle()
        assert router.ingested == []
        await sensor.record(_fail())          # the one that qualifies
        assert len(router.ingested) == 1


class TestItCannotStorm:
    @pytest.mark.asyncio
    async def test_a_cold_start_burst_emits_once_per_pattern(self):
        """Twenty pre-boot failures of one pattern is still one envelope —
        the cooldown is the guard and reconcile respects it."""
        sensor = CUExecutionSensor()
        await _fail_n(sensor, 20)
        router = _Router()
        CUExecutionSensor(router=router)
        await _settle()
        assert len(router.ingested) == 1

    @pytest.mark.asyncio
    async def test_distinct_patterns_each_get_one(self):
        sensor = CUExecutionSensor()
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD, app="Messages")
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD, app="Slack")
        router = _Router()
        CUExecutionSensor(router=router)
        await _settle()
        assert len(router.ingested) == 2

    @pytest.mark.asyncio
    async def test_reconcile_is_idempotent(self):
        """Re-attaching the same router must not re-emit."""
        sensor = CUExecutionSensor()
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        router = _Router()
        for _ in range(4):
            CUExecutionSensor(router=router)
            await _settle()
        assert len(router.ingested) == 1

    @pytest.mark.asyncio
    async def test_reentrancy_is_guarded(self):
        """Concurrent reconciles must not double-emit."""
        sensor = CUExecutionSensor()
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        sensor._router = _Router()
        await asyncio.gather(*(sensor._reconcile() for _ in range(5)))
        assert len(sensor._router.ingested) == 1


class TestTheWindowStaysHonest:
    @pytest.mark.asyncio
    async def test_expired_occurrences_do_not_reconcile(self):
        """A pattern that crossed the threshold yesterday must not emit today
        on the strength of entries that have since expired."""
        sensor = CUExecutionSensor()
        old = time.time() - (ces._WINDOW_S + 60)
        for _ in range(ces._GRADUATION_THRESHOLD):
            await sensor.record(_fail(ts=old))
        router = _Router()
        CUExecutionSensor(router=router)
        await _settle()
        assert router.ingested == []

    @pytest.mark.asyncio
    async def test_the_record_timestamp_is_used_not_arrival(self):
        """The HUD replays queued events when IPC reconnects. A 24h window
        judged on ARRIVAL would be judging when the socket came back, not when
        the action failed."""
        sensor = CUExecutionSensor()
        old = time.time() - (ces._WINDOW_S + 60)
        await sensor.record(_fail(ts=old))
        sig = _fail().failure_signature
        assert sensor._failure_window.get(sig, []) == [] or \
            all(t <= old for t in sensor._failure_window[sig])

    @pytest.mark.asyncio
    async def test_pruning_drops_the_record_with_the_window(self):
        """`_latest_by_sig` must not outlive the window it is indexed by, or
        it is an unbounded leak keyed on an unbounded signature space."""
        sensor = CUExecutionSensor()
        old = time.time() - (ces._WINDOW_S + 60)
        await sensor.record(_fail(ts=old))
        sensor._live_count(_fail().failure_signature, time.time())
        assert sensor._latest_by_sig == {}
        assert sensor._failure_window == {}


class TestDegradedPaths:
    @pytest.mark.asyncio
    async def test_a_transient_router_error_is_retried_immediately(self):
        """Stamping the cooldown on a failed ingest would serve a one-hour
        silence for an envelope nobody received. Instead the failure sets
        `_needs_reconcile`, and the sweep at the end of `record()` retries
        within the same call — so a one-off router blip costs nothing.

        This test originally asserted the weaker guarantee (retry on the NEXT
        occurrence) and failed because the implementation is better than the
        expectation.
        """
        router = _FlakyRouter(failures=1)
        sensor = CUExecutionSensor(router=router)
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        assert len(router.ingested) == 1, "the transient blip was not retried"

    @pytest.mark.asyncio
    async def test_a_persistently_failing_router_does_not_spin(self):
        """The other side of it: retry must be BOUNDED. The re-entry guard
        means one extra attempt per record, never a loop."""
        router = _FlakyRouter(failures=10_000)
        sensor = CUExecutionSensor(router=router)
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        assert router.ingested == []
        assert sensor._reconciling is False       # unwound cleanly
        assert sensor._needs_reconcile is True    # still eligible

    @pytest.mark.asyncio
    async def test_a_failed_ingest_never_starts_a_cooldown(self):
        router = _FlakyRouter(failures=10_000)
        sensor = CUExecutionSensor(router=router)
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        sig = _fail().failure_signature
        assert sig not in sensor._last_emitted

    def test_attaching_with_no_running_loop_does_not_raise(self):
        """The lazy half. A sync caller (CLI, test, odd boot order) must not
        crash, and must leave the flag for `record()` to pick up."""
        sensor = CUExecutionSensor()
        sensor._needs_reconcile = False
        CUExecutionSensor(router=_Router())      # no loop running here
        assert sensor._needs_reconcile is True

    @pytest.mark.asyncio
    async def test_the_lazy_path_reconciles_on_the_next_record(self):
        sensor = CUExecutionSensor()
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD, app="Messages")
        # Attach WITHOUT letting the eager task run.
        sensor._router = _Router()
        sensor._needs_reconcile = True
        await sensor.record(_fail(app="Slack"))   # unrelated pattern
        sigs = {e.evidence["signature"] if hasattr(e, "evidence")
                else e["evidence"]["signature"] for e in sensor._router.ingested}
        assert any("messages" in s for s in sigs), (
            "the pre-boot pattern never reconciled on the lazy path")

    @pytest.mark.asyncio
    async def test_a_window_entry_with_no_record_is_skipped_not_faked(self):
        """Pre-upgrade state has timestamps and no record. Emitting would mean
        describing a pattern we cannot describe."""
        sensor = CUExecutionSensor()
        sig = "orphan:target_miss"
        sensor._failure_window[sig] = [time.time()] * ces._GRADUATION_THRESHOLD
        sensor._router = _Router()
        await sensor._reconcile()
        assert sensor._router.ingested == []

    @pytest.mark.asyncio
    async def test_successes_never_emit(self):
        """Unchanged behaviour, pinned: the sensor learns from failure only."""
        router = _Router()
        sensor = CUExecutionSensor(router=router)
        for _ in range(10):
            await sensor.record(CUExecutionRecord(
                goal="g", success=True, steps_completed=3, steps_total=3,
                elapsed_s=1.0))
        assert router.ingested == []
        assert sensor.get_stats()["total_records"] == 10

    @pytest.mark.asyncio
    async def test_stats_expose_the_race(self, ):
        """§7 — the counter is how you learn the race is happening at all."""
        sensor = CUExecutionSensor()
        await _fail_n(sensor, ces._GRADUATION_THRESHOLD)
        s = sensor.get_stats()
        assert s["router_attached"] is False
        assert s["deferred_emissions"] >= 1
        CUExecutionSensor(router=_Router())
        await _settle()
        s2 = sensor.get_stats()
        assert s2["router_attached"] is True
        assert s2["reconciled_emissions"] >= 1
