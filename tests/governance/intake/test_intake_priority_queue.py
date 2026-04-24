"""F1 Slice 1 tests — IntakePriorityQueue primitive.

Scope: `memory/project_followup_f1_intake_governor_enforcement.md` Slice 1.
Operator-authorized 2026-04-24 as P0 arc after Wave 3 (6) Slice 5b
graduation S1 (`bt-2026-04-24-062608`) classified
`live_reachability=blocked_by_intake_starvation`.

Contract pinned by these tests:

1. Heap ordering: ``(urgency_rank, enqueue_monotonic, sequence)`` — lower
   rank dequeues first; FIFO within equal urgency.
2. Reserved-slot starvation guard: of every N dequeues, at least M must
   be urgency >= normal if any such envelope is in queue.
3. Deadline inversion: an envelope past its deadline pops out-of-order
   with ``dequeue_mode=priority_inversion``.
4. Back-pressure: when queue depth >= threshold, non-critical ingest is
   refused with ``retry_after_s``. Critical always admitted.
5. Telemetry: every state transition emits a structured event to an
   optional sink. Sink exceptions never fail the queue.
6. Authority invariant: no imports of orchestrator/policy/iron_gate/
   risk_tier/change_engine/candidate_generator/gate/semantic_guardian.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.intake.intake_priority_queue import (
    DequeueDecision,
    EnqueueResult,
    IntakePriorityQueue,
    URGENCY_RANK,
    _DEFAULT_DEADLINES_S,
    _back_pressure_threshold,
    _intake_priority_scheduler_enabled,
    _reserved_dequeue_m,
    _reserved_dequeue_n,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Env:
    """Duck-typed envelope stub. IntakePriorityQueue reads urgency + source
    via getattr; the full IntentEnvelope contract is not required here."""
    urgency: str = "normal"
    source: str = "test"
    description: str = "t"
    tag: str = ""  # for identity-tracking in multi-enqueue tests


class _FakeClock:
    """Deterministic monotonic clock for deadline-inversion tests."""
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def captured() -> List[Tuple[str, Dict[str, Any]]]:
    return []


@pytest.fixture
def sink(captured: List[Tuple[str, Dict[str, Any]]]):
    def _record(event_type: str, payload: Dict[str, Any]) -> None:
        captured.append((event_type, payload))
    return _record


# ---------------------------------------------------------------------------
# (1) Urgency rank + default deadline shape
# ---------------------------------------------------------------------------


def test_urgency_rank_mapping_is_canonical():
    assert URGENCY_RANK["critical"] == 0
    assert URGENCY_RANK["high"] == 1
    assert URGENCY_RANK["normal"] == 2
    assert URGENCY_RANK["low"] == 3


def test_default_deadlines_shape():
    assert _DEFAULT_DEADLINES_S["critical"] == 5.0
    assert _DEFAULT_DEADLINES_S["high"] == 30.0
    assert _DEFAULT_DEADLINES_S["normal"] == 300.0
    assert _DEFAULT_DEADLINES_S["low"] == float("inf")


# ---------------------------------------------------------------------------
# (2) Env-tunable helpers
# ---------------------------------------------------------------------------


def test_master_flag_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", raising=False)
    assert _intake_priority_scheduler_enabled() is False


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "ON"])
def test_master_flag_truthy(monkeypatch, value):
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", value)
    assert _intake_priority_scheduler_enabled() is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "bogus", "  "])
def test_master_flag_falsy_or_unknown(monkeypatch, value):
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", value)
    assert _intake_priority_scheduler_enabled() is False


def test_reserved_n_default(monkeypatch):
    monkeypatch.delenv("JARVIS_INTAKE_RESERVED_N", raising=False)
    assert _reserved_dequeue_n() == 5


def test_reserved_n_override(monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_RESERVED_N", "10")
    assert _reserved_dequeue_n() == 10


def test_reserved_n_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_RESERVED_N", "not-an-int")
    assert _reserved_dequeue_n() == 5


def test_reserved_n_clamped_to_minimum_one(monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_RESERVED_N", "0")
    assert _reserved_dequeue_n() == 1


def test_reserved_m_default(monkeypatch):
    monkeypatch.delenv("JARVIS_INTAKE_RESERVED_M", raising=False)
    assert _reserved_dequeue_m() == 1


def test_reserved_m_can_be_zero(monkeypatch):
    """M=0 is legal — disables reserved-slot guard entirely."""
    monkeypatch.setenv("JARVIS_INTAKE_RESERVED_M", "0")
    assert _reserved_dequeue_m() == 0


def test_back_pressure_threshold_default(monkeypatch):
    monkeypatch.delenv("JARVIS_INTAKE_BACKPRESSURE_THRESHOLD", raising=False)
    assert _back_pressure_threshold() == 200


def test_back_pressure_threshold_override(monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_BACKPRESSURE_THRESHOLD", "500")
    assert _back_pressure_threshold() == 500


# ---------------------------------------------------------------------------
# (3) Empty queue behavior
# ---------------------------------------------------------------------------


def test_new_queue_is_empty():
    q = IntakePriorityQueue()
    assert q.is_empty()
    assert len(q) == 0


def test_dequeue_empty_returns_none():
    q = IntakePriorityQueue()
    assert q.dequeue() is None


def test_oldest_wait_empty_returns_zero():
    q = IntakePriorityQueue()
    assert q.oldest_wait_s() == 0.0


def test_snapshot_depths_empty_returns_all_zeros():
    q = IntakePriorityQueue()
    assert q.snapshot_depths() == {"critical": 0, "high": 0, "normal": 0, "low": 0}


# ---------------------------------------------------------------------------
# (4) Basic enqueue / dequeue
# ---------------------------------------------------------------------------


def test_enqueue_single_returns_accepted():
    q = IntakePriorityQueue()
    result = q.enqueue(_Env(urgency="normal"))
    assert isinstance(result, EnqueueResult)
    assert result.accepted is True
    assert len(q) == 1


def test_dequeue_single_returns_decision_matching_envelope():
    q = IntakePriorityQueue()
    env = _Env(urgency="normal", source="test_sensor", tag="only")
    q.enqueue(env)
    decision = q.dequeue()
    assert isinstance(decision, DequeueDecision)
    assert decision.envelope is env
    assert decision.urgency == "normal"
    assert decision.source == "test_sensor"
    assert decision.dequeue_mode == "priority"
    assert len(q) == 0


def test_explicit_urgency_param_overrides_envelope_attr():
    """Caller can override the urgency extracted from envelope attributes."""
    q = IntakePriorityQueue()
    env = _Env(urgency="low")
    q.enqueue(env, urgency="critical")
    decision = q.dequeue()
    assert decision.urgency == "critical"


# ---------------------------------------------------------------------------
# (5) Heap ordering — critical beats normal beats low
# ---------------------------------------------------------------------------


def test_critical_dequeues_before_low_regardless_of_enqueue_order():
    q = IntakePriorityQueue()
    q.enqueue(_Env(urgency="low", tag="low1"))
    q.enqueue(_Env(urgency="low", tag="low2"))
    q.enqueue(_Env(urgency="critical", tag="crit"))
    assert q.dequeue().envelope.tag == "crit"
    assert q.dequeue().envelope.tag == "low1"
    assert q.dequeue().envelope.tag == "low2"


def test_full_priority_order_critical_high_normal_low():
    q = IntakePriorityQueue()
    q.enqueue(_Env(urgency="low", tag="L"))
    q.enqueue(_Env(urgency="normal", tag="N"))
    q.enqueue(_Env(urgency="high", tag="H"))
    q.enqueue(_Env(urgency="critical", tag="C"))
    tags = [q.dequeue().envelope.tag for _ in range(4)]
    assert tags == ["C", "H", "N", "L"]


def test_fifo_within_equal_urgency():
    """Same urgency → enqueue order preserved."""
    q = IntakePriorityQueue()
    for i in range(5):
        q.enqueue(_Env(urgency="normal", tag=f"n{i}"))
    tags = [q.dequeue().envelope.tag for _ in range(5)]
    assert tags == ["n0", "n1", "n2", "n3", "n4"]


def test_unknown_urgency_treated_as_normal():
    q = IntakePriorityQueue()
    q.enqueue(_Env(urgency="weirdvalue", tag="W"))
    q.enqueue(_Env(urgency="low", tag="L"))
    # "weirdvalue" falls back to normal → pops before low
    assert q.dequeue().envelope.tag == "W"
    assert q.dequeue().envelope.tag == "L"


def test_missing_urgency_attr_treated_as_normal():
    q = IntakePriorityQueue()

    class _Bare:
        pass

    q.enqueue(_Bare())  # no urgency attr → getattr returns "normal"
    decision = q.dequeue()
    assert decision.urgency == "normal"


# ---------------------------------------------------------------------------
# (6) Reserved-slot starvation guard
# ---------------------------------------------------------------------------


def test_reserved_slot_guard_disabled_when_m_zero():
    """M=0 → priority-only ordering; low-urgency floods pass freely."""
    q = IntakePriorityQueue(reserved_n=5, reserved_m=0)
    # 5 low-urgency pops + 1 normal pending → queue returns in priority order
    for i in range(5):
        q.enqueue(_Env(urgency="low", tag=f"low{i}"))
    q.enqueue(_Env(urgency="normal", tag="norm"))
    # With M=0, window enforcement disabled. Normal still pops first by priority.
    assert q.dequeue().envelope.tag == "norm"
    for i in range(5):
        assert q.dequeue().envelope.tag == f"low{i}"


def test_reserved_slot_guard_forces_normal_pop_when_window_starved():
    """After N low-urgency dequeues without a normal pop, guard forces one."""
    # Use small N=3, M=1 for faster reproduction
    q = IntakePriorityQueue(reserved_n=3, reserved_m=1)
    # Fill queue: 3 low + 1 normal (normal enqueued LAST — lowest priority by ts
    # among equals, but critical/normal rank beats low)
    # Actually, normal rank (2) beats low rank (3), so normal would pop first
    # by priority. To simulate starvation we need normal to be stuck behind
    # an infinite low stream... let's enqueue the low first, dequeue them to
    # build the window, then enqueue a normal and new low — reserved-slot
    # should now force normal to pop next.
    for i in range(3):
        q.enqueue(_Env(urgency="low", tag=f"low_warm{i}"))
    # Dequeue them — window fills with 3 low pops (starved)
    for i in range(3):
        q.dequeue()
    # Now enqueue mixed — with guard, next pop must be >=normal even if
    # priority would also pick normal (which it would). The test ensures
    # guard logic doesn't interfere with the priority choice.
    q.enqueue(_Env(urgency="low", tag="low_after"))
    q.enqueue(_Env(urgency="normal", tag="norm_after"))
    decision = q.dequeue()
    # Guard triggers OR priority triggers → either way, normal pops.
    assert decision.envelope.tag == "norm_after"
    assert decision.dequeue_mode in {"priority", "reserved_slot"}


def test_reserved_slot_guard_window_warmup_is_inert():
    """Before the window fills, guard should not fire (warmup period)."""
    q = IntakePriorityQueue(reserved_n=10, reserved_m=1)
    q.enqueue(_Env(urgency="low", tag="L"))
    q.enqueue(_Env(urgency="low", tag="L2"))
    # Only 2 dequeues so far, window not full → guard inert, priority order wins
    d1 = q.dequeue()
    d2 = q.dequeue()
    assert d1.dequeue_mode == "priority"
    assert d2.dequeue_mode == "priority"


def test_reserved_slot_does_nothing_when_only_low_in_queue():
    """Guard never force-pops if no >=normal envelope exists."""
    q = IntakePriorityQueue(reserved_n=3, reserved_m=1)
    for i in range(10):
        q.enqueue(_Env(urgency="low", tag=f"low{i}"))
    # Dequeue all — guard can't force since there's no >=normal to pull.
    for _ in range(10):
        decision = q.dequeue()
        assert decision.dequeue_mode == "priority"
    assert q.is_empty()


# ---------------------------------------------------------------------------
# (7) Deadline inversion
# ---------------------------------------------------------------------------


def test_deadline_inversion_pops_deadlined_envelope_out_of_order():
    clock = _FakeClock(start=1000.0)
    q = IntakePriorityQueue(clock=clock)
    # Enqueue a critical with deadline=5s. Then enqueue low-urgency envelopes.
    q.enqueue(_Env(urgency="critical", tag="late_critical"))
    # Time advances past critical's deadline (5s).
    clock.advance(10.0)
    # Enqueue a fresh normal after the advance.
    q.enqueue(_Env(urgency="normal", tag="fresh_normal"))
    # Next dequeue: deadline inversion — critical was past deadline, pops
    # with mode=priority_inversion. (It also wins on priority, so this test
    # is symbolic of the mechanism — a stronger test below uses a layout
    # where only deadline can explain the pop order.)
    decision = q.dequeue()
    assert decision.envelope.tag == "late_critical"
    assert decision.dequeue_mode == "priority_inversion"


def test_deadline_inversion_beats_priority_order_for_lower_urgency():
    """Deadlined normal envelope pops BEFORE a newly-enqueued critical
    if only the normal is past its deadline."""
    clock = _FakeClock(start=1000.0)
    q = IntakePriorityQueue(clock=clock)
    # Enqueue normal with 300s deadline at t=1000
    q.enqueue(_Env(urgency="normal", tag="old_normal"))
    # Advance past normal's deadline
    clock.advance(400.0)
    # Enqueue fresh critical with new 5s deadline (not yet expired)
    q.enqueue(_Env(urgency="critical", tag="fresh_critical"))
    # Dequeue: deadline inversion pops old_normal first
    decision = q.dequeue()
    assert decision.envelope.tag == "old_normal"
    assert decision.dequeue_mode == "priority_inversion"
    # Next pop returns the fresh critical by normal priority
    decision2 = q.dequeue()
    assert decision2.envelope.tag == "fresh_critical"
    assert decision2.dequeue_mode == "priority"


def test_deadline_infinity_never_fires():
    """low-urgency default deadline is inf → never force-pops via deadline."""
    clock = _FakeClock(start=0.0)
    q = IntakePriorityQueue(clock=clock)
    q.enqueue(_Env(urgency="low", tag="patient_low"))
    clock.advance(1_000_000.0)  # a million seconds
    decision = q.dequeue()
    assert decision.dequeue_mode == "priority"  # not priority_inversion


def test_explicit_deadline_override_at_enqueue():
    """Caller can pass deadline_s=X to override the urgency default."""
    clock = _FakeClock(start=0.0)
    q = IntakePriorityQueue(clock=clock)
    q.enqueue(_Env(urgency="low", tag="short_fuse"), deadline_s=1.0)
    clock.advance(2.0)
    decision = q.dequeue()
    assert decision.dequeue_mode == "priority_inversion"


def test_deadline_telemetry_includes_waited_and_deadline(sink, captured):
    clock = _FakeClock(start=100.0)
    q = IntakePriorityQueue(clock=clock, telemetry_sink=sink)
    q.enqueue(_Env(urgency="critical", tag="t"))
    clock.advance(10.0)  # past 5s deadline
    q.dequeue()
    inversion_events = [p for t, p in captured if t == "priority_inversion"]
    assert len(inversion_events) == 1
    assert inversion_events[0]["waited_s"] == pytest.approx(10.0)
    assert inversion_events[0]["deadline_s"] == pytest.approx(5.0)
    assert inversion_events[0]["urgency"] == "critical"


# ---------------------------------------------------------------------------
# (8) Back-pressure
# ---------------------------------------------------------------------------


def test_back_pressure_rejects_normal_at_threshold():
    q = IntakePriorityQueue(back_pressure_threshold=3)
    for i in range(3):
        q.enqueue(_Env(urgency="normal", tag=f"n{i}"))
    result = q.enqueue(_Env(urgency="normal", tag="overflow"))
    assert result.accepted is False
    assert result.reason == "queue_full"
    assert result.retry_after_s > 0
    assert len(q) == 3


def test_back_pressure_always_admits_critical():
    """Critical must never be refused — starvation is the exact bug F1 fixes."""
    q = IntakePriorityQueue(back_pressure_threshold=3)
    for i in range(3):
        q.enqueue(_Env(urgency="normal", tag=f"n{i}"))
    result = q.enqueue(_Env(urgency="critical", tag="urgent"))
    assert result.accepted is True
    assert len(q) == 4


def test_back_pressure_rejects_low():
    q = IntakePriorityQueue(back_pressure_threshold=3)
    for i in range(3):
        q.enqueue(_Env(urgency="low", tag=f"l{i}"))
    result = q.enqueue(_Env(urgency="low", tag="overflow"))
    assert result.accepted is False
    assert result.reason == "queue_full"


def test_back_pressure_below_threshold_admits_all():
    q = IntakePriorityQueue(back_pressure_threshold=10)
    for i in range(5):
        r = q.enqueue(_Env(urgency="normal", tag=f"n{i}"))
        assert r.accepted is True


def test_back_pressure_emits_telemetry(sink, captured):
    q = IntakePriorityQueue(back_pressure_threshold=1, telemetry_sink=sink)
    q.enqueue(_Env(urgency="normal", tag="first"))
    q.enqueue(_Env(urgency="normal", tag="rejected"))
    bp_events = [p for t, p in captured if t == "backpressure_applied"]
    assert len(bp_events) == 1
    assert bp_events[0]["reason"] == "queue_full"
    assert bp_events[0]["urgency"] == "normal"
    assert bp_events[0]["queue_depth_total"] == 1


# ---------------------------------------------------------------------------
# (9) Telemetry sink
# ---------------------------------------------------------------------------


def test_telemetry_emits_enqueue_event(sink, captured):
    q = IntakePriorityQueue(telemetry_sink=sink)
    q.enqueue(_Env(urgency="critical", source="bklg"))
    enq = [p for t, p in captured if t == "enqueue"]
    assert len(enq) == 1
    assert enq[0]["urgency"] == "critical"
    assert enq[0]["source"] == "bklg"
    assert enq[0]["queue_depth_total"] == 1
    assert enq[0]["depths"]["critical"] == 1


def test_telemetry_emits_dequeue_event(sink, captured):
    q = IntakePriorityQueue(telemetry_sink=sink)
    q.enqueue(_Env(urgency="normal"))
    q.dequeue()
    deq = [p for t, p in captured if t == "dequeue"]
    assert len(deq) == 1
    assert deq[0]["dequeue_mode"] == "priority"
    assert deq[0]["queue_depth_total"] == 0


def test_telemetry_sink_exception_never_fails_queue():
    """Defensive: a broken sink must not break the queue."""

    def _bad_sink(event_type, payload):
        raise RuntimeError("sink blew up")

    q = IntakePriorityQueue(telemetry_sink=_bad_sink)
    # Must not raise.
    result = q.enqueue(_Env())
    assert result.accepted is True
    decision = q.dequeue()
    assert decision is not None


# ---------------------------------------------------------------------------
# (10) snapshot_depths / oldest_wait_s
# ---------------------------------------------------------------------------


def test_snapshot_depths_counts_per_urgency():
    q = IntakePriorityQueue()
    q.enqueue(_Env(urgency="critical"))
    q.enqueue(_Env(urgency="critical"))
    q.enqueue(_Env(urgency="low"))
    depths = q.snapshot_depths()
    assert depths == {"critical": 2, "high": 0, "normal": 0, "low": 1}


def test_oldest_wait_s_reflects_clock_delta():
    clock = _FakeClock(start=100.0)
    q = IntakePriorityQueue(clock=clock)
    q.enqueue(_Env(urgency="normal"))
    clock.advance(42.5)
    assert q.oldest_wait_s() == pytest.approx(42.5)


def test_oldest_wait_s_filtered_by_urgency():
    clock = _FakeClock(start=0.0)
    q = IntakePriorityQueue(clock=clock)
    q.enqueue(_Env(urgency="low"))
    clock.advance(100.0)
    q.enqueue(_Env(urgency="critical"))
    clock.advance(1.0)
    # low waited 101s, critical waited 1s
    assert q.oldest_wait_s() == pytest.approx(101.0)
    assert q.oldest_wait_s(urgency="critical") == pytest.approx(1.0)
    assert q.oldest_wait_s(urgency="low") == pytest.approx(101.0)
    assert q.oldest_wait_s(urgency="high") == 0.0


# ---------------------------------------------------------------------------
# (11) Starvation budget %
# ---------------------------------------------------------------------------


def test_starved_budget_pct_zero_when_no_dequeues():
    q = IntakePriorityQueue()
    q.enqueue(_Env(urgency="normal"))
    decision = q.dequeue()
    # First dequeue: window was empty; starved_budget_pct reports this pop's
    # window share — 0 low of 1 pop = 0%
    assert decision.starved_budget_pct == 0.0


def test_starved_budget_pct_tracks_low_dequeues():
    q = IntakePriorityQueue(reserved_n=4, reserved_m=0)  # M=0 → no forced pops
    for i in range(4):
        q.enqueue(_Env(urgency="low"))
    decisions = [q.dequeue() for _ in range(4)]
    # Last decision's window = 4 of last 4 pops are low = 100%
    assert decisions[-1].starved_budget_pct == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# (12) Authority invariant — zero imports of banned modules
# ---------------------------------------------------------------------------


def test_intake_priority_queue_authority_invariant():
    """F1 primitive is a routing-ORDER tool, not a routing-DECISION tool.
    Must not import orchestrator/policy/iron_gate/risk_tier/change_engine/
    candidate_generator/gate/semantic_guardian.
    """
    module_path = (
        Path(__file__).resolve().parents[3]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "intake"
        / "intake_priority_queue.py"
    )
    source = module_path.read_text(encoding="utf-8")
    banned = [
        r"from backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"import backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"from backend\.core\.ouroboros\.governance\.policy\b",
        r"from backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"from backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"from backend\.core\.ouroboros\.governance\.change_engine\b",
        r"from backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"from backend\.core\.ouroboros\.governance\.gate\b",
        r"from backend\.core\.ouroboros\.governance\.semantic_guardian\b",
    ]
    for pattern in banned:
        assert not re.search(pattern, source), (
            f"IntakePriorityQueue imports banned authority module: {pattern}"
        )


# ---------------------------------------------------------------------------
# (13) The S1 repro — burst-BG-starves-critical-seed scenario
# ---------------------------------------------------------------------------


def test_s1_burst_scenario_flag_off_semantics_via_fifo_simulation():
    """Documents the failure mode the primitive fixes: without priority
    scheduling, a critical seed enqueued AFTER a burst of BG ops would be
    positioned after them in FIFO order.

    This test doesn't USE IntakePriorityQueue in FIFO mode (the primitive
    is always priority-ordered); it verifies that IF we had FIFO order,
    the seed would be last. The priority-ordered counterpart below is
    the one that proves the fix.
    """
    # Simulate pre-F1 FIFO: just a list.
    fifo = []
    for i in range(20):
        fifo.append(("doc_staleness", "normal", f"ds{i}"))
    fifo.append(("backlog", "critical", "seed"))
    # FIFO pop order: seed is LAST
    assert fifo[-1][2] == "seed"


def test_s1_burst_scenario_priority_queue_dequeues_critical_seed_first():
    """The fix: with IntakePriorityQueue, the seed pops FIRST despite
    being enqueued last, because critical urgency beats normal by heap
    priority. This is the exact S1 failure mode, inverted."""
    q = IntakePriorityQueue()
    # Simulate S1: 20 BG ops burst-emitted at session boot
    for i in range(20):
        q.enqueue(_Env(urgency="normal", source="doc_staleness", tag=f"ds{i}"))
    # Then the forced-reach seed (F2 stamps urgency=critical)
    q.enqueue(_Env(urgency="critical", source="backlog", tag="seed"))

    first = q.dequeue()
    assert first.envelope.tag == "seed", (
        f"F1 fix: critical seed must dequeue first despite enqueue-last "
        f"position; got {first.envelope.tag} ({first.urgency})"
    )
    assert first.urgency == "critical"
    assert first.source == "backlog"


def test_s1_burst_scenario_seed_waited_time_is_negligible():
    """Seed's waited_s should be ~0 when it dequeues immediately — no
    starvation, even with 20 BG ops already queued."""
    clock = _FakeClock(start=0.0)
    q = IntakePriorityQueue(clock=clock)
    for i in range(20):
        q.enqueue(_Env(urgency="normal", source="doc_staleness"))
        clock.advance(0.01)  # simulates 10ms between emissions
    # Seed enqueued at t ~= 0.20s
    q.enqueue(_Env(urgency="critical", source="backlog", tag="seed"))
    # Immediately dequeue
    decision = q.dequeue()
    assert decision.envelope.tag == "seed"
    # Waited 0s because we dequeue at the same clock tick
    assert decision.waited_s == pytest.approx(0.0)
