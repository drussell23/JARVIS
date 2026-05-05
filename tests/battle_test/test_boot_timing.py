"""Tests for boot_timing — phase-timing instrumentation."""
from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.boot_timing import (
    BOOT_TIMING_SCHEMA_VERSION,
    BootTimer,
    MASTER_FLAG_ENV_VAR,
    PhaseRecord,
    get_default_timer,
    is_boot_timing_enabled,
    reset_default_timer_for_tests,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    reset_default_timer_for_tests()
    yield
    reset_default_timer_for_tests()


# ===========================================================================
# Schema + master flag
# ===========================================================================


def test_schema_version_pinned():
    assert BOOT_TIMING_SCHEMA_VERSION == "boot_timing.v1"


def test_master_flag_default_on():
    """Recording is cheap; default-on so operators get measurements
    automatically. Setting =false yields zero overhead."""
    assert is_boot_timing_enabled() is True


def test_master_flag_explicit_off(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert is_boot_timing_enabled() is False


# ===========================================================================
# PhaseRecord — frozen + projection
# ===========================================================================


def test_phase_record_elapsed():
    r = PhaseRecord(
        name="x", started_at=1.0, ended_at=1.5, parent="",
    )
    assert abs(r.elapsed_s - 0.5) < 1e-6


def test_phase_record_in_flight():
    r = PhaseRecord(
        name="x", started_at=1.0, ended_at=0.0, parent="",
    )
    assert r.is_in_flight is True
    assert r.elapsed_s == 0.0


def test_phase_record_frozen():
    r = PhaseRecord(name="x", started_at=0.0, ended_at=0.0, parent="")
    with pytest.raises(Exception):
        r.name = "tampered"  # type: ignore[misc]


# ===========================================================================
# BootTimer — phase context manager
# ===========================================================================


def test_phase_records_start_and_end():
    timer = BootTimer()
    with timer.phase("test_phase"):
        time.sleep(0.01)
    records = timer.records()
    assert len(records) == 1
    assert records[0].name == "test_phase"
    assert records[0].elapsed_s > 0.005  # at least 5ms (slept 10ms)


def test_phase_context_manager_pairs_correctly():
    timer = BootTimer()
    with timer.phase("outer"):
        with timer.phase("inner"):
            pass
    records = timer.records()
    # ended in stack order: inner ends first, then outer
    assert len(records) == 2
    assert records[0].name == "inner"
    assert records[0].parent == "outer"
    assert records[1].name == "outer"
    assert records[1].parent == ""


def test_phase_records_even_on_exception():
    timer = BootTimer()
    try:
        with timer.phase("crashing"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # The phase still recorded (end called via finally semantics in __exit__)
    records = timer.records()
    assert any(r.name == "crashing" for r in records)


def test_mark_records_zero_duration():
    timer = BootTimer()
    timer.mark("milestone")
    records = timer.records()
    assert len(records) == 1
    assert records[0].name == "milestone"
    assert records[0].started_at == records[0].ended_at


def test_mark_with_empty_name_no_op():
    timer = BootTimer()
    timer.mark("")
    timer.mark(None)  # type: ignore[arg-type]
    assert len(timer.records()) == 0


def test_mark_includes_parent_when_inside_phase():
    timer = BootTimer()
    with timer.phase("outer"):
        timer.mark("midway")
    records = timer.records()
    midway = [r for r in records if r.name == "midway"]
    assert len(midway) == 1
    assert midway[0].parent == "outer"


# ===========================================================================
# Defensive: disabled flag and exceptions
# ===========================================================================


def test_no_recording_when_disabled(monkeypatch):
    """Master flag off → mark/begin/end are no-ops."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    timer = BootTimer()
    timer.mark("ignored")
    with timer.phase("ignored_phase"):
        pass
    assert len(timer.records()) == 0


def test_end_without_matching_begin_no_op():
    """Calling end without begin doesn't crash."""
    timer = BootTimer()
    timer.end("never_started")  # NEVER raises
    assert len(timer.records()) == 0


def test_total_elapsed_increases_monotonically():
    timer = BootTimer()
    e1 = timer.total_elapsed_s()
    time.sleep(0.01)
    e2 = timer.total_elapsed_s()
    assert e2 > e1


# ===========================================================================
# emit_summary — formatted output
# ===========================================================================


def test_emit_summary_returns_formatted_text():
    timer = BootTimer()
    with timer.phase("alpha"):
        time.sleep(0.011)
    with timer.phase("beta"):
        time.sleep(0.012)
    text = timer.emit_summary()
    assert "Boot timing" in text
    assert "alpha" in text
    assert "beta" in text


def test_emit_summary_filters_below_threshold():
    timer = BootTimer()
    timer.mark("instant")  # 0ms
    with timer.phase("real"):
        time.sleep(0.012)
    text = timer.emit_summary(threshold_ms=5.0)
    assert "real" in text
    assert "instant" not in text


def test_emit_summary_sorts_by_elapsed():
    timer = BootTimer()
    with timer.phase("fast"):
        time.sleep(0.011)
    with timer.phase("slow"):
        time.sleep(0.025)
    text = timer.emit_summary(sort_by="elapsed", threshold_ms=5.0)
    # "slow" should appear before "fast" in text
    assert text.index("slow") < text.index("fast")


def test_emit_summary_to_console():
    timer = BootTimer()
    with timer.phase("alpha"):
        time.sleep(0.011)
    fake_console = mock.Mock()
    timer.emit_summary(console=fake_console, threshold_ms=5.0)
    fake_console.print.assert_called_once()


def test_emit_summary_handles_console_exception():
    timer = BootTimer()
    timer.mark("anything")
    bad = mock.Mock()
    bad.print.side_effect = RuntimeError("blew up")
    # NEVER raises
    text = timer.emit_summary(console=bad)
    assert isinstance(text, str)


# ===========================================================================
# Singleton + reset
# ===========================================================================


def test_default_timer_singleton():
    a = get_default_timer()
    b = get_default_timer()
    assert a is b


def test_reset_drops_singleton():
    a = get_default_timer()
    reset_default_timer_for_tests()
    b = get_default_timer()
    assert a is not b


# ===========================================================================
# Source-level regression — instrumentation wired in script
# ===========================================================================


def test_battle_test_script_imports_boot_timer():
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    assert "boot_timing" in src
    assert "get_default_timer" in src


def test_battle_test_script_records_harness_phases():
    """Critical landmarks must be timed: harness_module_import,
    harness_construct, harness_run_started. These give operators
    visibility into the heavy boot phases."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    assert "harness_module_import" in src
    assert "harness_construct" in src
    assert "harness_run_started" in src


def test_battle_test_script_emits_summary_in_verbose():
    """Verbose mode should print the boot timing summary so operators
    see where time goes without needing extra flags."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    assert "_emit_boot_timing_after_settle" in src
    assert "emit_summary" in src
