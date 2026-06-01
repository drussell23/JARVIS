"""Slice 53 — dual-lane total-outage circuit breaker.

Forensic basis (v45/v46 + Slice 51 vendor repro): under DW-only (Claude
disabled) a verified TOTAL vendor blackout (both the streaming preflight AND
the batch generation return empty / live_transport RuntimeError) caused every
GENERATE op to exhaust all DW models and burn retry tokens indefinitely —
``fallback_skipped:no_fallback_configured`` is deliberately NOT counted toward
the existing ProviderExhaustionWatcher, so nothing paused the loop.

Option A (gated dual-lane isolation): trip a global breaker only when
``JARVIS_TOTAL_OUTAGE_THRESHOLD`` (default 3) CONSECUTIVE ops exhaust BOTH
lanes. A working batch lane (single-lane streaming degradation, Slice 41
ACTIVE_BATCH_ONLY) still returns a candidate -> ``record_success`` resets the
counter -> the breaker never trips. So Slice 41's keep-eligible posture is
preserved for single-lane drops; only a genuine both-lanes-empty blackout trips.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dual_lane_breaker import (
    DualLaneOutageBreaker,
)


def _fresh() -> DualLaneOutageBreaker:
    b = DualLaneOutageBreaker()
    b.reset()
    return b


def test_trips_after_threshold_consecutive_total_outages(monkeypatch):
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "3")
    b = _fresh()
    assert b.record_total_outage("qwen:live_transport") is False  # 1
    assert b.is_tripped() is False
    assert b.record_total_outage("qwen:live_transport") is False  # 2
    assert b.is_tripped() is False
    tripped_now = b.record_total_outage("qwen:live_transport")     # 3 -> trip
    assert tripped_now is True
    assert b.is_tripped() is True


def test_record_total_outage_returns_true_only_on_the_tripping_call(monkeypatch):
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "2")
    b = _fresh()
    assert b.record_total_outage("x") is False   # 1
    assert b.record_total_outage("x") is True    # 2 -> trips (edge fires once)
    assert b.record_total_outage("x") is False   # already tripped — no re-fire


def test_single_lane_resilience_success_resets_counter(monkeypatch):
    """Slice 41 preserved: if the batch lane keeps yielding candidates, the
    consecutive counter never reaches the threshold."""
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "3")
    b = _fresh()
    b.record_total_outage("stream down, batch tried")   # 1
    b.record_total_outage("stream down, batch tried")   # 2
    b.record_success()                                   # batch returned a candidate -> reset
    b.record_total_outage("stream down again")           # 1 (post-reset)
    b.record_total_outage("stream down again")           # 2
    assert b.is_tripped() is False, "single-lane drops must never trip the breaker"


def test_three_consecutive_after_a_reset_does_trip(monkeypatch):
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "3")
    b = _fresh()
    b.record_total_outage("x")
    b.record_success()  # reset
    b.record_total_outage("y")
    b.record_total_outage("y")
    assert b.is_tripped() is False
    b.record_total_outage("y")  # 3rd consecutive -> trip
    assert b.is_tripped() is True


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "1")
    b = _fresh()
    assert b.record_total_outage("immediate") is True
    assert b.is_tripped() is True


def test_master_flag_disabled_never_trips(monkeypatch):
    monkeypatch.setenv("JARVIS_DUAL_LANE_BREAKER_ENABLED", "false")
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "1")
    b = _fresh()
    for _ in range(10):
        assert b.record_total_outage("x") is False
    assert b.is_tripped() is False


def test_tripped_breaker_stays_tripped_through_late_success(monkeypatch):
    """Once a total blackout is verified and pause requested, a late
    candidate does not silently un-trip mid-shutdown."""
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "1")
    b = _fresh()
    b.record_total_outage("x")
    assert b.is_tripped() is True
    b.record_success()
    assert b.is_tripped() is True


def test_snapshot_exposes_state(monkeypatch):
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "3")
    b = _fresh()
    b.record_total_outage("qwen397:live_transport:RuntimeError")
    snap = b.snapshot()
    assert snap.consecutive_total_outages == 1
    assert snap.tripped is False
    assert "qwen397" in snap.last_diagnostic


# ---------------------------------------------------------------------------
# Wiring pins — Slice 45 dead-code lesson: the breaker must be wired into the
# real generation error path (candidate_generator) AND the dispatch chokepoint
# (governed_loop_service.submit), or it records/pauses nothing in production.
# ---------------------------------------------------------------------------


def test_recording_wired_in_candidate_generator():
    import inspect

    import backend.core.ouroboros.governance.candidate_generator as cg

    src = inspect.getsource(cg)
    # Outage recorded at the DW-exhaustion raises; success resets on candidate.
    assert "_note_dw_total_outage" in src, "outage recording helper missing"
    assert "_note_dw_candidate_success" in src, "success-reset helper missing"
    assert src.count("_note_dw_total_outage(") >= 4, (
        "expected the helper definition + 3 raise-site calls"
    )
    # Reset must sit on the sentinel success return.
    assert "_note_dw_candidate_success()  # Slice 53" in src


def test_pause_gate_wired_in_governed_loop_submit():
    import inspect

    import backend.core.ouroboros.governance.governed_loop_service as gls

    src = inspect.getsource(gls.GovernedLoopService.submit)
    assert "get_dual_lane_breaker" in src, "submit() does not consult the breaker"
    assert "is_tripped()" in src, "submit() does not check is_tripped()"
    assert "dual_lane_outage_paused" in src, "submit() missing the pause reason_code"


def test_candidate_generator_helpers_never_raise(monkeypatch):
    """Recording sits on the generation error path — it must be inert on any
    internal failure, never perturbing the raise it precedes."""
    import backend.core.ouroboros.governance.candidate_generator as cg

    # Even with a hostile threshold value, the helpers swallow everything.
    monkeypatch.setenv("JARVIS_TOTAL_OUTAGE_THRESHOLD", "not-an-int")
    cg._note_dw_total_outage("x")      # must not raise
    cg._note_dw_candidate_success()    # must not raise
