"""The time-windowed ledger filter, and what it does when time is unreliable.

The reconciliation ledger is written by other processes — subprocess workers,
and on this host a WSL guest whose clock can step relative to the Windows host
— so two stamps taken "at the same time" are not guaranteed to be ordered.

The filter exists to stop a PREVIOUS session's verdict resolving this
dispatch. It must not, in doing so, let a clock step hide a REAL landing.
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance.autonomy import sentinel_loop as SL
from backend.core.ouroboros.governance.autonomy.sentinel_loop import (
    _record_is_ours,
    _skew_tolerance_s,
)


class _Rec:
    def __init__(self, ts):
        self.ts = ts
        self.goal_id = "g"
        self.event = "terminal"
        self.op_id = "op-x"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_LEDGER_SKEW_TOLERANCE_S", raising=False)
    SL._SKEW_WARNED.clear()
    yield
    SL._SKEW_WARNED.clear()


# --------------------------------------------------------------------------
# The three answers — and why there are three
# --------------------------------------------------------------------------

def test_a_record_after_the_dispatch_is_ours():
    now = time.time()
    assert _record_is_ours(_Rec(now + 1), now, "g") is True


def test_a_clearly_older_record_is_not_ours():
    """The bug this closes: a goal dispatched in an earlier run leaves a
    TERMINAL record behind forever, and reading it resolved every retry
    instantly against a verdict that had already been counted."""
    now = time.time()
    assert _record_is_ours(_Rec(now - 10_000), now, "g") is False


def test_a_record_just_inside_the_skew_window_is_AMBIGUOUS_not_rejected():
    """"Cannot tell" is not the same answer as "no". Collapsing them would let
    a clock step hide a real landing."""
    now = time.time()
    verdict = _record_is_ours(_Rec(now - 1.0), now, "g")
    assert verdict is None, "a boundary record was discarded outright"


def test_an_undatable_record_is_ambiguous_not_rejected():
    now = time.time()
    assert _record_is_ours(_Rec(None), now, "g") is None
    assert _record_is_ours(_Rec("not-a-time"), now, "g") is None


# --------------------------------------------------------------------------
# The tolerance is derived, and bounded on BOTH sides
# --------------------------------------------------------------------------

def test_the_tolerance_is_derived_from_the_loop_cadence(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_INTERVAL_S", "120")
    assert _skew_tolerance_s() == 30.0        # 120/4, at the ceiling
    monkeypatch.setenv("JARVIS_SENTINEL_INTERVAL_S", "20")
    assert _skew_tolerance_s() == 5.0         # 20/4


def test_the_tolerance_never_exceeds_the_gap_between_passes(monkeypatch):
    """A tolerance larger than the interval would start admitting the PREVIOUS
    pass's verdict — reintroducing the bug at a smaller scale."""
    for interval in ("20", "60", "120", "600"):
        monkeypatch.setenv("JARVIS_SENTINEL_INTERVAL_S", interval)
        assert _skew_tolerance_s() < float(interval)


def test_the_tolerance_has_a_floor(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_INTERVAL_S", "5")
    assert _skew_tolerance_s() >= 2.0


def test_an_operator_override_is_honoured(monkeypatch):
    monkeypatch.setenv("JARVIS_LEDGER_SKEW_TOLERANCE_S", "45")
    assert _skew_tolerance_s() == 45.0


@pytest.mark.parametrize("junk", ["", "  ", "banana", "-5"])
def test_a_junk_tolerance_falls_back_to_the_derivation(monkeypatch, junk):
    monkeypatch.setenv("JARVIS_LEDGER_SKEW_TOLERANCE_S", junk)
    monkeypatch.setenv("JARVIS_SENTINEL_INTERVAL_S", "40")
    assert _skew_tolerance_s() in (10.0, 0.0)   # derived, or an explicit zero


# --------------------------------------------------------------------------
# LedgerTimeSkewWarning
# --------------------------------------------------------------------------

def test_skew_is_warned_once_per_cause(caplog):
    """A skewed clock produces the same warning on every poll, several times a
    second. A warning that floods is a warning nobody reads."""
    now = time.time()
    with caplog.at_level("WARNING"):
        for _ in range(20):
            _record_is_ours(_Rec(now - 1.0), now, "g")
    warnings = [r for r in caplog.records if "LedgerTimeSkewWarning" in r.getMessage()]
    assert len(warnings) == 1


def test_different_goals_each_get_their_warning(caplog):
    now = time.time()
    with caplog.at_level("WARNING"):
        _record_is_ours(_Rec(now - 1.0), now, "goal-a")
        _record_is_ours(_Rec(now - 1.0), now, "goal-b")
    warnings = [r for r in caplog.records if "LedgerTimeSkewWarning" in r.getMessage()]
    assert len(warnings) == 2


def test_the_filter_never_raises():
    class _Hostile:
        @property
        def ts(self):
            raise RuntimeError("boom")

    assert _record_is_ours(_Hostile(), time.time(), "g") in (True, False, None)


def test_an_ambiguous_record_is_still_evaluated():
    """Structural: the caller must fall through on None, not `continue`."""
    import inspect

    src = inspect.getsource(SL.SentinelLoop._goal_verdict)
    assert "is False" in src, "the caller collapses ambiguity into rejection"
    assert "must not be able to HIDE a landing" in src
