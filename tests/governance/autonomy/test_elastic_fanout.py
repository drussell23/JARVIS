"""Tests for elastic_fanout — §3.1 burst / hold / freeze (fail-CLOSED)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from backend.core.ouroboros.governance.autonomy.elastic_fanout import (
    ElasticFanoutDecision,
    FanoutAction,
    PendingFanoutQueue,
    base_floor,
    decide_fanout,
)


@dataclass
class _Probe:
    free_pct: float
    ok: bool = True
    source: str = "fake"


class _FakeGate:
    """Stand-in for MemoryPressureGate exposing probe()."""

    def __init__(self, probe):
        self._probe = probe

    def probe(self):
        if isinstance(self._probe, Exception):
            raise self._probe
        return self._probe


def _decide(free_pct=None, *, ok=True, n_pending=5, current=None, raises=False):
    if raises:
        gate = _FakeGate(RuntimeError("probe boom"))
    elif ok and free_pct is None:
        gate = _FakeGate(_Probe(free_pct=50.0, ok=False))  # not-ok
    else:
        gate = _FakeGate(_Probe(free_pct=float(free_pct or 0.0), ok=ok))
    cur = base_floor() if current is None else current
    return decide_fanout(gate=gate, current_concurrency=cur, n_pending=n_pending)


# ---------------------------------------------------------------------------
# < 65% -> BURST
# ---------------------------------------------------------------------------


def test_low_pressure_bursts():
    # free 80% -> used 20% -> burst.
    d = _decide(free_pct=80.0, n_pending=10)
    assert d.action is FanoutAction.BURST
    assert d.may_spawn_beyond_floor is True
    assert d.permitted_concurrency >= base_floor()


# ---------------------------------------------------------------------------
# 65%..80% -> HOLD
# ---------------------------------------------------------------------------


def test_mid_pressure_holds():
    # free 30% -> used 70% -> in [0.65, 0.80] -> hold.
    d = _decide(free_pct=30.0, n_pending=10)
    assert d.action is FanoutAction.HOLD
    assert d.may_spawn_beyond_floor is False
    # Hold never drops below the floor.
    assert d.permitted_concurrency >= base_floor()


# ---------------------------------------------------------------------------
# > 80% -> FREEZE
# ---------------------------------------------------------------------------


def test_high_pressure_freezes():
    # free 10% -> used 90% -> > 0.80 -> freeze.
    d = _decide(free_pct=10.0, n_pending=10)
    assert d.action is FanoutAction.FREEZE
    assert d.may_spawn_beyond_floor is False


# ---------------------------------------------------------------------------
# Fail-CLOSED: unreadable probe -> FREEZE, never burst
# ---------------------------------------------------------------------------


def test_not_ok_probe_freezes():
    d = _decide(ok=False, free_pct=None, n_pending=10)
    assert d.action is FanoutAction.FREEZE
    assert d.probe_ok is False
    assert d.may_spawn_beyond_floor is False


def test_raising_probe_freezes():
    d = _decide(raises=True, n_pending=10)
    assert d.action is FanoutAction.FREEZE
    assert d.probe_ok is False
    assert "fail-closed" in d.reason.lower()


def test_probe_missing_free_pct_freezes():
    class _BadProbe:
        ok = True
        source = "bad"
        free_pct = "not-a-number"

    gate = _FakeGate(_BadProbe())
    d = decide_fanout(gate=gate, current_concurrency=base_floor(), n_pending=5)
    assert d.action is FanoutAction.FREEZE


# ---------------------------------------------------------------------------
# FIFO pending queue — no drop
# ---------------------------------------------------------------------------


def test_pending_queue_fifo_no_drop():
    q = PendingFanoutQueue()
    for i in range(5):
        q.enqueue("w{0}".format(i))
    assert len(q) == 5
    drained = q.drain(2)
    assert drained == ["w0", "w1"]
    assert q.pending_ids == ["w2", "w3", "w4"]
    # Drain more than present -> returns what's there, no raise.
    rest = q.drain(99)
    assert rest == ["w2", "w3", "w4"]
    assert len(q) == 0


def test_pending_admit_under_freeze_keeps_queue():
    q = PendingFanoutQueue()
    for i in range(4):
        q.enqueue("w{0}".format(i))
    gate = _FakeGate(_Probe(free_pct=5.0, ok=True))  # used 95% -> freeze
    admitted, decision = q.admit(gate=gate, current_concurrency=base_floor())
    assert admitted == []
    assert decision.action is FanoutAction.FREEZE
    assert len(q) == 4  # nothing dropped


def test_pending_admit_under_burst_drains_headroom():
    q = PendingFanoutQueue()
    for i in range(10):
        q.enqueue("w{0}".format(i))
    gate = _FakeGate(_Probe(free_pct=90.0, ok=True))  # used 10% -> burst
    admitted, decision = q.admit(gate=gate, current_concurrency=base_floor())
    assert decision.action is FanoutAction.BURST
    # Admitted up to the headroom (permitted - current), FIFO order.
    assert admitted == ["w{0}".format(i) for i in range(len(admitted))]
    assert len(q) == 10 - len(admitted)
