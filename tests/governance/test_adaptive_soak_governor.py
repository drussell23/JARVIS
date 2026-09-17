"""The brake must scale with the loop it is braking.

Arming JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED turned on the SensorGovernor's
CD-2 brake alongside telemetry backpressure. A fixed trip point is wrong in
both directions: calibrated for a quiet loop it sheds CONTINUOUSLY under a
heavy multi-session soak (baseline lag rises with real concurrent work, so the
constant is crossed permanently and the governor brakes the work the soak
exists to do); raised enough to survive a soak it then sleeps through a stall
on a quiet loop.

The trip point is therefore measured — median + median-absolute-deviation of
this loop's own recent lag — and clamped into a band the operator already
controls.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import control_plane_load_shed as LS


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    LS._reset_for_test()
    for var in (
        "JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS",
        "JARVIS_CONTROL_PLANE_WATCHDOG_THRESHOLD_MS",
        "JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    LS._reset_for_test()


def _history(monkeypatch, values):
    monkeypatch.setattr(LS, "_lag_history_ms", lambda window_s=60.0: list(values))


# --------------------------------------------------------------------------
# It adapts
# --------------------------------------------------------------------------

def test_a_busy_loop_earns_a_higher_bar(monkeypatch):
    """THE calibration: under sustained load the brake must not be permanently
    engaged just because a soak is running."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS", "2000")
    _history(monkeypatch, [400.0, 420.0, 380.0, 410.0, 390.0])
    trip = LS.adaptive_threshold_ms(150.0)
    assert trip > 150.0, "the trip point ignored a raised baseline"
    assert 150.0 < trip <= 2000.0


def test_a_quiet_loop_keeps_the_configured_threshold(monkeypatch):
    """Adaptation may make the brake less eager, never more."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS", "2000")
    _history(monkeypatch, [1.0, 2.0, 1.5, 0.5, 2.0])
    assert LS.adaptive_threshold_ms(150.0) == 150.0


def test_no_history_is_exactly_the_old_behaviour(monkeypatch):
    """An unobserved loop must behave precisely as it did before this existed."""
    _history(monkeypatch, [])
    assert LS.adaptive_threshold_ms(150.0) == 150.0
    _history(monkeypatch, [900.0, 900.0])      # two points is not a distribution
    assert LS.adaptive_threshold_ms(150.0) == 150.0


def test_pathology_cannot_raise_the_bar_forever(monkeypatch):
    """The failure mode every adaptive threshold has: a loop that is always
    starved teaches the brake that starvation is normal. The ceiling is the
    one bound it must not derive from its own input."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS", "1000")
    _history(monkeypatch, [50_000.0, 51_000.0, 49_000.0, 50_500.0])
    assert LS.adaptive_threshold_ms(150.0) == 1000.0


def test_a_single_spike_does_not_move_the_bar(monkeypatch):
    """Median and MAD, not mean and stdev: lag is spiky, and one 1700ms stall
    must not hide every later one behind it."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS", "5000")
    quiet = [5.0, 6.0, 4.0, 5.0, 6.0, 5.0]
    _history(monkeypatch, quiet)
    before = LS.adaptive_threshold_ms(150.0)
    _history(monkeypatch, quiet + [1739.6])
    assert LS.adaptive_threshold_ms(150.0) == before


def test_an_unreadable_history_falls_back_to_the_floor(monkeypatch):
    def _boom(window_s=60.0):
        raise RuntimeError("watchdog gone")

    monkeypatch.setattr(LS, "_lag_history_ms", _boom)
    assert LS.adaptive_threshold_ms(150.0) == 150.0


# --------------------------------------------------------------------------
# Both consumers actually use it
# --------------------------------------------------------------------------

def test_the_sensor_latch_uses_the_adaptive_trip(monkeypatch):
    """The SensorGovernor brake is the consumer this calibrates."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    monkeypatch.setenv("JARVIS_LOAD_SHED_LAG_THRESHOLD_MS", "150")
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS", "2000")
    _history(monkeypatch, [400.0, 420.0, 380.0, 410.0, 390.0])
    LS.stream_begin()
    # 200ms clears the static 150 floor but is ordinary for THIS loop.
    assert LS.evaluate(200.0) is False
    assert LS.evaluate(900.0) is True
    LS.stream_end()


def test_telemetry_backpressure_uses_the_adaptive_trip(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", "500")
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS", "3000")
    _history(monkeypatch, [800.0, 820.0, 790.0, 810.0])
    assert LS.telemetry_shedding(lag_ms=600.0) is False   # normal for this loop
    assert LS.telemetry_shedding(lag_ms=2_500.0) is True  # anomalous


def test_the_flag_still_gates_everything(monkeypatch):
    monkeypatch.delenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", raising=False)
    _history(monkeypatch, [1.0, 1.0, 1.0])
    assert LS.telemetry_shedding(lag_ms=99_999.0) is False
    LS.stream_begin()
    assert LS.evaluate(99_999.0) is False
    LS.stream_end()


def test_no_tuning_constant_was_introduced():
    """'Worse than usual by more than the usual variation' is what an anomaly
    IS. A multiplier here would be exactly the hardcoded sensitivity this
    replaces."""
    import inspect

    src = inspect.getsource(LS.adaptive_threshold_ms)
    body = src.split('"""')[-1]
    assert "centre + spread" in body
    for suspect in ("* 1.5", "* 2", "* 3", "0.9", "p90", "percentile"):
        assert suspect not in body, f"a tuning constant crept in: {suspect}"


# --------------------------------------------------------------------------
# Observability — a counter nobody reads is not an event
# --------------------------------------------------------------------------

def test_the_onset_of_shedding_is_logged_once(caplog, monkeypatch):
    """The first supervised soak could only answer 'did it over-shed?' by
    inference: the tally lived in memory and reached no log or summary."""
    with caplog.at_level("WARNING"):
        for _ in range(5):
            LS.note_shed("autonomy.health_probe_result")
        LS.note_shed("autonomy.op_completed")
    began = [r for r in caplog.records if "shedding BEGAN" in r.getMessage()]
    assert len(began) == 2, "onset must be logged once per topic, not per drop"
    assert LS.shed_counts()["autonomy.health_probe_result"] == 5


def test_a_quiet_session_says_so_explicitly():
    """A soak that sheds nothing and a soak with no instrumentation look
    identical in a log, and only one of them is good news."""
    assert "no telemetry shed" in LS.shed_report()


def test_the_report_names_the_topics():
    LS.note_shed("autonomy.health_probe_result")
    LS.note_shed("autonomy.health_probe_result")
    LS.note_shed("autonomy.saga_state_changed")
    report = LS.shed_report()
    assert "3 telemetry event(s) shed across 2 topic(s)" in report
    assert "autonomy.health_probe_result=2" in report
