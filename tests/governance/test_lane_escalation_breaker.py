"""Dynamic Lane Escalation (Part 2, Task T4) -- breaker vision fix.

The live C2 soak shows DW generation exhausting: ALL 13 DW models fail
``fsm_exhausted:TIMEOUT`` on the *batch* lane (aegis logs: batches POSTed 201,
polled 200, never completing in deadline). The ``transport_circuit_breaker``
(batch->realtime rotation) is armed but BLIND to these: a batch poll/retrieval
TIMEOUT in pure-DW autarky is wrapped into
``RuntimeError("all_providers_exhausted:...")`` which classifies FSM_EXHAUSTED
-- and FSM_EXHAUSTED is deliberately EXCLUDED from the breaker's record
allowlist (``_BREAKER_RECORD_SOURCES``) to avoid spuriously tripping on
OUR-side faults.

This task makes the breaker SEE a *batch-lane retrieval TIMEOUT* specifically
(lane=batch AND failure_mode=TIMEOUT, origin = the DW batch poll deadline -- NOT
a generic exhaustion, NOT an our-side LOCAL_* fault), record it as a trippable
batch-lane transport failure, trip the batch lane OPEN, and let ``select_lane``
rotate the op to realtime -- WITHOUT spuriously tripping on our-side faults.

These tests exercise the REAL classification predicate
(``dw_fault_taxonomy.is_batch_lane_retrieval_timeout``) and the REAL breaker +
``select_lane``; only the dispatch-loop surroundings are faked.
"""
from __future__ import annotations

import importlib
import random

import pytest


# ---------------------------------------------------------------------------
# Helpers: build the exact exception shapes the dispatch path produces.
# ---------------------------------------------------------------------------

def _make_batch_retrieval_timeout_exc():
    """The wedge: a batch poll/retrieval TIMEOUT in pure-DW autarky.

    ``_generate_via_batch`` raises ``DoublewordInfraError("Batch retrieval
    failed", status_code=0)`` (the poll deadline elapsed; no HTTP error ever
    returned >=300). With no Claude fallback configured, the dispatch loop wraps
    it into ``RuntimeError("all_providers_exhausted:...")`` carrying an
    ``.exhaustion_report`` dict whose ``fsm_failure_mode == "TIMEOUT"`` and
    ``primary_err_class == "DoublewordInfraError"`` / ``primary_err_msg`` =
    "Batch retrieval failed".
    """
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordInfraError,
    )
    primary = DoublewordInfraError("Batch retrieval failed", status_code=0)
    err = RuntimeError("all_providers_exhausted:fallback_skipped:no_fallback_configured")
    setattr(err, "exhaustion_report", {
        "cause": "fallback_skipped",
        "fsm_failure_mode": "TIMEOUT",
        "primary_err_class": "DoublewordInfraError",
        "primary_err_msg": "Batch retrieval failed",
    })
    return err, primary


def _make_generic_fsm_exhaustion_exc():
    """A generic FSM exhaustion that is NOT a batch-retrieval timeout.

    e.g. DW returned no candidate (empty content / parse) with no fallback -- an
    OUR-side dispatch-control exhaustion. NOT a transport rupture; the breaker
    must NOT see it.
    """
    err = RuntimeError("all_providers_exhausted:no_fallback_configured")
    setattr(err, "exhaustion_report", {
        "cause": "no_fallback_configured",
        "fsm_failure_mode": "CONTENT_FAILURE",
        "primary_err_class": "RuntimeError",
        "primary_err_msg": "empty_content",
    })
    return err


def _make_local_egress_overweight_exc():
    """An our-side LOCAL_EGRESS_OVERWEIGHT fault: WE blocked the body before it
    ever left the process. NEVER a transport rupture."""
    from backend.core.ouroboros.governance.dw_egress_interceptor import (
        LocalEgressOverweightError,
    )
    return LocalEgressOverweightError(
        attempted_size=9_900_000,
        max_allowed_size=1_000_000,
        model="qwen3.5-397b",
    )


# ---------------------------------------------------------------------------
# 1. The classification predicate -- the load-bearing decision.
# ---------------------------------------------------------------------------

class TestBatchLaneTimeoutPredicate:
    def setup_method(self):
        from backend.core.ouroboros.governance import dw_fault_taxonomy
        self.tax = dw_fault_taxonomy

    def test_batch_retrieval_timeout_on_batch_lane_is_trippable(self):
        err, _primary = _make_batch_retrieval_timeout_exc()
        assert self.tax.is_batch_lane_retrieval_timeout(err, lane="batch") is True

    def test_batch_retrieval_timeout_on_realtime_lane_is_NOT_trippable(self):
        # Same exception, but the attempt was on realtime -- batch breaker must
        # not be fed a batch-trip from a non-batch attempt.
        err, _primary = _make_batch_retrieval_timeout_exc()
        assert self.tax.is_batch_lane_retrieval_timeout(err, lane="realtime") is False

    def test_generic_fsm_exhaustion_is_NOT_a_batch_timeout(self):
        err = _make_generic_fsm_exhaustion_exc()
        assert self.tax.is_batch_lane_retrieval_timeout(err, lane="batch") is False

    def test_local_egress_overweight_is_NOT_a_batch_timeout(self):
        exc = _make_local_egress_overweight_exc()
        assert self.tax.is_batch_lane_retrieval_timeout(exc, lane="batch") is False

    def test_tool_loop_deadline_is_NOT_a_batch_timeout(self):
        # A slow GENERATION (Venom tool loop) deadline -- our budget, not the
        # batch poll. Must never trip the transport breaker.
        err = RuntimeError("tool_loop_deadline_exceeded")
        assert self.tax.is_batch_lane_retrieval_timeout(err, lane="batch") is False

    def test_raw_doubleword_batch_retrieval_timeout_non_autarky(self):
        # Non-autarky path: the DoublewordInfraError is NOT wrapped (Claude was
        # configured but the loop chose to record before cascade). The bare
        # batch-retrieval DoublewordInfraError must still be recognized.
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordInfraError,
        )
        exc = DoublewordInfraError("Batch retrieval failed", status_code=0)
        assert self.tax.is_batch_lane_retrieval_timeout(exc, lane="batch") is True

    def test_doubleword_batch_submission_failed_is_NOT_a_retrieval_timeout(self):
        # Batch *submission* failed (a real submit-side fault, possibly a 4xx) --
        # that is a different class; only the *retrieval/poll* deadline trips.
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordInfraError,
        )
        exc = DoublewordInfraError("Batch submission failed", status_code=400)
        assert self.tax.is_batch_lane_retrieval_timeout(exc, lane="batch") is False

    def test_predicate_never_raises(self):
        # Fail-soft contract: any odd input -> False, never raises.
        assert self.tax.is_batch_lane_retrieval_timeout(None, lane="batch") is False
        assert self.tax.is_batch_lane_retrieval_timeout(
            RuntimeError("x"), lane=None,  # type: ignore[arg-type]
        ) is False


# ---------------------------------------------------------------------------
# 2. End-to-end: predicate -> _breaker_record_outcome -> breaker trips ->
#    select_lane rotates to realtime.  Uses the REAL breaker.
# ---------------------------------------------------------------------------

class TestBreakerVisionWiring:
    def setup_method(self, _method):
        # Fresh breaker singleton + seeded RNG for deterministic recovery window.
        import backend.core.ouroboros.governance.transport_circuit_breaker as tcb
        importlib.reload(tcb)
        self.tcb = tcb
        self.breaker = tcb.TransportCircuitBreaker(rng=random.Random(0))
        tcb._SINGLETON = self.breaker  # make get_transport_breaker() return it

        import backend.core.ouroboros.governance.candidate_generator as cg
        self.cg = cg

    def _enable_breaker(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "true")
        # Small window so a handful of timeouts trips deterministically.
        monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_MIN_SAMPLES", "3")
        monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_FAIL_RATIO", "0.5")
        monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_WINDOW", "10")

    def _record_batch_timeout(self):
        """Drive the production record helper with a batch-lane retrieval TIMEOUT."""
        err, _primary = _make_batch_retrieval_timeout_exc()
        # Mirror the dispatch site: failure_source.name == "FSM_EXHAUSTED",
        # lane == "batch", exc carries the exhaustion_report.
        self.cg._breaker_record_outcome(
            "batch",
            ok=False,
            failure_mode="FSM_EXHAUSTED",
            exc=err,
        )

    def test_batch_timeouts_trip_batch_lane_and_rotate_to_realtime(self, monkeypatch):
        self._enable_breaker(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "true")

        for _ in range(4):
            self._record_batch_timeout()

        assert self.breaker.state("batch") is self.tcb.BreakerState.OPEN
        # select_lane rotates an op preferring batch -> realtime (sibling CLOSED).
        chosen = self.breaker.select_lane("batch", now=1000.0)
        assert chosen == "realtime"

    def test_generic_fsm_exhaustion_does_NOT_trip(self, monkeypatch):
        self._enable_breaker(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "true")

        err = _make_generic_fsm_exhaustion_exc()
        for _ in range(8):
            self.cg._breaker_record_outcome(
                "batch", ok=False, failure_mode="FSM_EXHAUSTED", exc=err,
            )

        assert self.breaker.state("batch") is self.tcb.BreakerState.CLOSED
        assert self.breaker.select_lane("batch", now=1000.0) == "batch"

    def test_local_egress_overweight_does_NOT_trip(self, monkeypatch):
        self._enable_breaker(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "true")

        exc = _make_local_egress_overweight_exc()
        for _ in range(8):
            self.cg._breaker_record_outcome(
                "batch", ok=False,
                failure_mode="LOCAL_EGRESS_OVERWEIGHT", exc=exc,
            )

        assert self.breaker.state("batch") is self.tcb.BreakerState.CLOSED

    def test_lane_escalation_disabled_is_byte_identical_blind(self, monkeypatch):
        # OFF -> the breaker stays blind exactly as before: a batch-lane
        # FSM_EXHAUSTED timeout is dropped by the legacy _BREAKER_RECORD_SOURCES
        # filter and never recorded.
        self._enable_breaker(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "false")

        for _ in range(8):
            self._record_batch_timeout()

        assert self.breaker.state("batch") is self.tcb.BreakerState.CLOSED
        assert self.breaker.select_lane("batch", now=1000.0) == "batch"

    def test_both_lanes_open_stops_rotating(self, monkeypatch):
        # Dual-lane safety: if BOTH lanes are OPEN, select_lane returns preferred
        # (dual_lane_breaker owns the total-outage pause). The vision fix must
        # not break that.
        self._enable_breaker(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "true")

        # Trip batch via the vision path.
        for _ in range(4):
            self._record_batch_timeout()
        # Trip realtime via a normal LIVE_TRANSPORT signal (legacy allowlist).
        for _ in range(4):
            self.cg._breaker_record_outcome(
                "realtime", ok=False, failure_mode="LIVE_TRANSPORT", exc=None,
            )

        assert self.breaker.state("batch") is self.tcb.BreakerState.OPEN
        assert self.breaker.state("realtime") is self.tcb.BreakerState.OPEN
        # No rotation onto a second dead lane.
        assert self.breaker.select_lane("batch", now=1000.0) == "batch"

    def test_legacy_live_transport_still_records(self, monkeypatch):
        # Regression: the existing LIVE_TRANSPORT allowlist path is untouched.
        self._enable_breaker(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "true")
        for _ in range(4):
            self.cg._breaker_record_outcome(
                "batch", ok=False, failure_mode="LIVE_TRANSPORT", exc=None,
            )
        assert self.breaker.state("batch") is self.tcb.BreakerState.OPEN

    def test_record_outcome_accepts_no_exc_kwarg_legacy(self, monkeypatch):
        # Back-compat: callers that don't pass exc= still work (legacy signature).
        self._enable_breaker(monkeypatch)
        monkeypatch.setenv("JARVIS_LANE_ESCALATION_ENABLED", "true")
        # Should not raise.
        self.cg._breaker_record_outcome(
            "batch", ok=False, failure_mode="FSM_EXHAUSTED",
        )
        # No exc -> can't be a batch timeout -> not recorded.
        assert self.breaker.state("batch") is self.tcb.BreakerState.CLOSED
