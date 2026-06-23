"""Dynamic Lane Escalation (Part 2, Task T5) -- LANE COLLAPSE + bounded dilation.

T4 made a wedged batch-lane retrieval TIMEOUT trip the transport breaker and
rotate the op to the realtime lane. T5 closes the loop: if the REALTIME lane
ALSO times out for that op (DW under heavy global load), both transport lanes
are now exhausted by timeout -- "lane collapse". Instead of the immortal queue
re-attempting forever at the same too-small deadline, the dispatcher:

  * emits ``[SOVEREIGN YIELD: LANE COLLAPSE]`` telemetry, and
  * records a BOUNDED per-op deadline-dilation hop (reusing the ReductionTracker
    LRU pattern), and
  * dilates the NEXT attempt's generation deadline = base * factor (capped),

up to ``JARVIS_LANE_DILATION_MAX_HOPS`` dilations. Once exceeded, NO further
dilation happens and the op falls through to the existing immortal/DLQ backstop.

These tests exercise the REAL classification predicate
(``dw_fault_taxonomy.is_realtime_lane_timeout``), the REAL bounded counter
(``convergence_watchdog.LaneDilationTracker``), the REAL dilation math, and the
REAL ``_compute_primary_budget`` seam; only the provider/dispatch surroundings
are faked.
"""
from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# Helpers: build the exact exception shapes the dispatch path produces.
# ---------------------------------------------------------------------------

def _make_realtime_timeout_exc():
    """A realtime-lane generation TIMEOUT in pure-DW autarky.

    After T4 rotates off batch, the realtime attempt streams but the per-op
    deadline elapses with no candidate. In autarky the loop wraps it into
    ``RuntimeError("all_providers_exhausted:...")`` carrying an
    ``.exhaustion_report`` whose ``fsm_failure_mode == "TIMEOUT"`` (NOT a "batch
    retrieval" message -- that is the batch class)."""
    err = RuntimeError("all_providers_exhausted:fallback_skipped:no_fallback_configured")
    setattr(err, "exhaustion_report", {
        "cause": "fallback_skipped",
        "fsm_failure_mode": "TIMEOUT",
        "primary_err_class": "TimeoutError",
        "primary_err_msg": "generation deadline elapsed",
    })
    return err


def _make_bare_asyncio_timeout_exc():
    """The bare asyncio.TimeoutError (realtime wait_for budget elapsed,
    recorded before any cascade wrapping)."""
    import asyncio
    return asyncio.TimeoutError()


def _make_batch_retrieval_timeout_exc():
    """A BATCH-lane retrieval timeout (the T4 class) -- must NOT be a realtime
    collapse."""
    err = RuntimeError("all_providers_exhausted:fallback_skipped:no_fallback_configured")
    setattr(err, "exhaustion_report", {
        "cause": "fallback_skipped",
        "fsm_failure_mode": "TIMEOUT",
        "primary_err_class": "DoublewordInfraError",
        "primary_err_msg": "Batch retrieval failed",
    })
    return err


def _make_generic_fsm_exhaustion_exc():
    """A generic FSM exhaustion (no candidate, NOT a timeout)."""
    err = RuntimeError("all_providers_exhausted:no_fallback_configured")
    setattr(err, "exhaustion_report", {
        "cause": "no_fallback_configured",
        "fsm_failure_mode": "CONTENT_FAILURE",
        "primary_err_class": "RuntimeError",
        "primary_err_msg": "empty_content",
    })
    return err


def _make_local_egress_overweight_exc():
    from backend.core.ouroboros.governance.dw_egress_interceptor import (
        LocalEgressOverweightError,
    )
    return LocalEgressOverweightError(
        attempted_size=9_900_000,
        max_allowed_size=1_000_000,
        model="qwen3.5-397b",
    )


# ---------------------------------------------------------------------------
# 1. The classification predicate -- realtime-lane TIMEOUT = lane collapse.
# ---------------------------------------------------------------------------

class TestRealtimeLaneTimeoutPredicate:
    def setup_method(self):
        from backend.core.ouroboros.governance import dw_fault_taxonomy
        self.tax = dw_fault_taxonomy

    def test_realtime_timeout_on_realtime_lane_is_collapse(self):
        err = _make_realtime_timeout_exc()
        assert self.tax.is_realtime_lane_timeout(err, lane="realtime") is True

    def test_bare_asyncio_timeout_on_realtime_lane_is_collapse(self):
        exc = _make_bare_asyncio_timeout_exc()
        assert self.tax.is_realtime_lane_timeout(exc, lane="realtime") is True

    def test_realtime_timeout_on_batch_lane_is_NOT_collapse(self):
        # Same exc, wrong lane -- a batch attempt must never feed a realtime
        # collapse signal.
        err = _make_realtime_timeout_exc()
        assert self.tax.is_realtime_lane_timeout(err, lane="batch") is False

    def test_batch_retrieval_timeout_is_NOT_a_realtime_collapse(self):
        # The two predicates must never overlap.
        err = _make_batch_retrieval_timeout_exc()
        assert self.tax.is_realtime_lane_timeout(err, lane="realtime") is False
        # And it IS a batch timeout (sanity: the T4 class still classifies).
        assert self.tax.is_batch_lane_retrieval_timeout(err, lane="batch") is True

    def test_generic_fsm_exhaustion_is_NOT_a_realtime_collapse(self):
        err = _make_generic_fsm_exhaustion_exc()
        assert self.tax.is_realtime_lane_timeout(err, lane="realtime") is False

    def test_tool_loop_deadline_is_NOT_a_realtime_collapse(self):
        err = RuntimeError("tool_loop_deadline_exceeded")
        assert self.tax.is_realtime_lane_timeout(err, lane="realtime") is False

    def test_local_egress_overweight_is_NOT_a_realtime_collapse(self):
        exc = _make_local_egress_overweight_exc()
        assert self.tax.is_realtime_lane_timeout(exc, lane="realtime") is False

    def test_predicate_never_raises(self):
        assert self.tax.is_realtime_lane_timeout(None, lane="realtime") is False
        assert self.tax.is_realtime_lane_timeout(
            RuntimeError("x"), lane=None,  # type: ignore[arg-type]
        ) is False


# ---------------------------------------------------------------------------
# 2. The bounded dilation tracker + dilation math (the termination bound).
# ---------------------------------------------------------------------------

class TestLaneDilationTracker:
    def setup_method(self):
        import backend.core.ouroboros.governance.convergence_watchdog as cw
        importlib.reload(cw)
        self.cw = cw
        self.tracker = cw.LaneDilationTracker()

    def test_hop_counter_increments_per_op(self):
        assert self.tracker.hops("op-A") == 0
        assert self.tracker.record_dilation_hop("op-A") == 1
        assert self.tracker.record_dilation_hop("op-A") == 2
        assert self.tracker.hops("op-A") == 2
        # Independent per op.
        assert self.tracker.record_dilation_hop("op-B") == 1

    def test_bounded_lru_eviction(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LANE_DILATION_TRACKER_SIZE", "2")
        t = self.cw.LaneDilationTracker()
        t.record_dilation_hop("op-1")
        t.record_dilation_hop("op-2")
        t.record_dilation_hop("op-3")  # evicts oldest (op-1)
        assert t.hops("op-1") == 0  # evicted
        assert t.hops("op-3") == 1

    def test_dilated_deadline_scales_by_factor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LANE_DILATION_FACTOR", "1.5")
        monkeypatch.delenv("JARVIS_LANE_DILATION_MAX_S", raising=False)
        base = 100.0
        # 0 hops -> unchanged.
        assert self.cw.compute_dilated_deadline(base, 0) == pytest.approx(100.0)
        # 1 hop -> base * 1.5.
        assert self.cw.compute_dilated_deadline(base, 1) == pytest.approx(150.0)
        # 2 hops -> base * 1.5^2 = 225, capped at base*3=300 -> 225.
        assert self.cw.compute_dilated_deadline(base, 2) == pytest.approx(225.0)

    def test_dilated_deadline_capped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LANE_DILATION_FACTOR", "1.5")
        monkeypatch.setenv("JARVIS_LANE_DILATION_MAX_S", "180")
        base = 100.0
        # 2 hops -> 225 raw, but capped at 180.
        assert self.cw.compute_dilated_deadline(base, 2) == pytest.approx(180.0)

    def test_dilation_factor_clamped_to_one(self, monkeypatch):
        # A factor < 1 would SHRINK the deadline -- forbidden; fall to default.
        monkeypatch.setenv("JARVIS_LANE_DILATION_FACTOR", "0.5")
        assert self.cw.lane_dilation_factor() >= 1.0


# ---------------------------------------------------------------------------
# 3. End-to-end: collapse -> [SOVEREIGN YIELD: LANE COLLAPSE] + bounded hops ->
#    dilated next deadline -> over MAX_HOPS -> NO dilation (backstop).
#    Uses the REAL predicates, REAL tracker, REAL _compute_primary_budget.
# ---------------------------------------------------------------------------

class TestLaneCollapseDilationWiring:
    def setup_method(self):
        import backend.core.ouroboros.governance.convergence_watchdog as cw
        importlib.reload(cw)
        cw._LANE_DILATION_TRACKER_SINGLETON = None  # fresh singleton
        self.cw = cw

        import backend.core.ouroboros.governance.candidate_generator as cg
        self.cg = cg

    def _enable(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "true")
        monkeypatch.setenv("JARVIS_LANE_DILATION_FACTOR", "1.5")
        monkeypatch.setenv("JARVIS_LANE_DILATION_MAX_HOPS", "2")
        monkeypatch.delenv("JARVIS_LANE_DILATION_MAX_S", raising=False)

    def test_realtime_collapse_emits_yield_and_records_hop(self, monkeypatch, caplog):
        self._enable(monkeypatch)
        err = _make_realtime_timeout_exc()
        import logging
        with caplog.at_level(logging.WARNING):
            hops = self.cg._record_lane_collapse_dilation("op-X", "realtime", err)
        assert hops == 1
        # The sovereign-yield telemetry fired with the LANE COLLAPSE label.
        assert any(
            "[SOVEREIGN YIELD: LANE COLLAPSE]" in r.getMessage()
            for r in caplog.records
        )
        # Hop is recorded in the real bounded tracker.
        assert self.cw.get_lane_dilation_tracker().hops("op-X") == 1

    def test_next_deadline_is_base_times_factor(self, monkeypatch):
        self._enable(monkeypatch)
        err = _make_realtime_timeout_exc()
        # First collapse -> hop 1.
        self.cg._record_lane_collapse_dilation("op-Y", "realtime", err)
        # The next attempt's budget is dilated: base 100 -> 150.
        # (force_batch + fallback_dead off, model not heavy -> standard path; the
        # dilation rides _apply_lane_dilation on top.)
        base = self.cg.CandidateGenerator._compute_primary_budget(
            1000.0, op_id="op-Y",
        )
        no_dilate = self.cg.CandidateGenerator._compute_primary_budget(
            1000.0, op_id="op-untouched",
        )
        assert base == pytest.approx(no_dilate * 1.5, rel=1e-6)

    def test_second_collapse_dilates_again_capped(self, monkeypatch):
        self._enable(monkeypatch)
        err = _make_realtime_timeout_exc()
        h1 = self.cg._record_lane_collapse_dilation("op-Z", "realtime", err)
        h2 = self.cg._record_lane_collapse_dilation("op-Z", "realtime", err)
        assert h1 == 1
        assert h2 == 2
        # base*1.5^2 = base*2.25, default cap is base*3 -> not clipped here.
        base = 200.0
        dilated = self.cw.compute_dilated_deadline(base, 2)
        assert dilated == pytest.approx(450.0)

    def test_third_collapse_over_max_hops_stops_dilating(self, monkeypatch):
        # rotate -> dilate -> dilate -> GIVE UP to backstop (no 3rd dilation).
        self._enable(monkeypatch)
        err = _make_realtime_timeout_exc()
        self.cg._record_lane_collapse_dilation("op-W", "realtime", err)   # 1
        self.cg._record_lane_collapse_dilation("op-W", "realtime", err)   # 2
        h3 = self.cg._record_lane_collapse_dilation("op-W", "realtime", err)  # 3 > cap
        assert h3 == 0  # over MAX_HOPS -> no dilation, falls to backstop
        # The tracker counted the hop (so it can never wrap around), but the
        # returned dilation is 0 -> the budget is NOT dilated beyond hop 2.
        assert self.cw.get_lane_dilation_tracker().hops("op-W") == 3

    def test_batch_lane_timeout_does_NOT_trigger_collapse(self, monkeypatch):
        self._enable(monkeypatch)
        err = _make_batch_retrieval_timeout_exc()
        # On the batch lane this is a T4 rotation, NOT a T5 collapse.
        hops = self.cg._record_lane_collapse_dilation("op-batch", "batch", err)
        assert hops == 0
        assert self.cw.get_lane_dilation_tracker().hops("op-batch") == 0

    def test_disabled_is_byte_identical_no_dilation(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "false")
        err = _make_realtime_timeout_exc()
        hops = self.cg._record_lane_collapse_dilation("op-off", "realtime", err)
        assert hops == 0
        # Even with a hop somehow present, the budget seam is gated off.
        self.cw.get_lane_dilation_tracker()._hops["op-off"] = 1
        base = self.cg.CandidateGenerator._compute_primary_budget(
            1000.0, op_id="op-off",
        )
        no_dilate = self.cg.CandidateGenerator._compute_primary_budget(
            1000.0, op_id="op-clean",
        )
        assert base == pytest.approx(no_dilate)

    def test_max_hops_zero_disables_dilation_entirely(self, monkeypatch):
        self._enable(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_DILATION_MAX_HOPS", "0")
        err = _make_realtime_timeout_exc()
        hops = self.cg._record_lane_collapse_dilation("op-zero", "realtime", err)
        assert hops == 0

    def test_record_never_raises(self, monkeypatch):
        self._enable(monkeypatch)
        # None op_id / odd inputs -> fail-soft 0, never raises.
        assert self.cg._record_lane_collapse_dilation(None, "realtime", None) == 0
        assert self.cg._record_lane_collapse_dilation("op", "realtime", None) == 0
