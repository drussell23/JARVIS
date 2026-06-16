"""Telemetry Harvester — parse + certification-gate regression suite.

The load-bearing assertion: a clean run where the live-fire gate never triggered must NOT
be FIELD-CERTIFIED (deployment ≠ self-heal). Plus every anomaly/incomplete path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import importlib.util
import sys
_SPEC = importlib.util.spec_from_file_location(
    "telemetry_harvester",
    str(Path(__file__).resolve().parents[2] / "scripts" / "telemetry_harvester.py"),
)
H = importlib.util.module_from_spec(_SPEC)
sys.modules["telemetry_harvester"] = H   # register before exec so @dataclass introspection works
_SPEC.loader.exec_module(H)

_COMPLETE = {"session_outcome": "complete", "stop_reason": "idle_timeout",
             "cost_total": 0.12, "duration_s": 940}

# a fully self-healed trajectory
_HEALED_LOG = """
phase=CLASSIFY op=op-abc
phase=GENERATE op=op-abc
[LiveFire] candidate FAILED live-fire boot: NameError: name 'collections' is not defined
[Orchestrator] op=op-abc failure_class=build GENERATE_RETRY
phase=GENERATE op=op-abc
state=applied op=op-abc
phase=COMPLETE op=op-abc
"""

_CLEAN_NO_FIRE = """
phase=CLASSIFY op=op-xyz
phase=GENERATE op=op-xyz
state=applied op=op-xyz
phase=COMPLETE op=op-xyz
"""


def _c(log, summary=_COMPLETE, dep=""):
    return H.certify(H.parse_metrics(log, summary, dep))


def test_field_certified_on_full_selfheal():
    cert = _c(_HEALED_LOG)
    assert cert.verdict == H.FIELD_CERTIFIED
    assert "READY FOR SOVEREIGN TASKING" in cert.headline


def test_clean_run_without_firing_is_NOT_certified():
    # THE guardrail: completed cleanly, but validator never fired → not a self-heal cert.
    cert = _c(_CLEAN_NO_FIRE)
    assert cert.verdict == H.OPERATIONAL_UNEXERCISED
    assert "UNEXERCISED" in cert.headline
    assert cert.verdict != H.FIELD_CERTIFIED


def test_gate_inert_is_anomaly():
    cert = _c(_HEALED_LOG + "\n[LiveFire] could not mark validation failed — GATE INERT")
    assert cert.verdict == H.ANOMALY and "GATE INERT" in cert.headline


def test_oom_is_anomaly():
    # Real ProcessMemoryWatchdog FIRE log line ("Session X stopping: ...").
    cert = _c(_HEALED_LOG + "\nSession bt-x stopping: process_memory_cap — RSS exceeded")
    assert cert.verdict == H.ANOMALY and ("OOM" in cert.headline.upper() or "memory" in cert.headline.lower())


def test_oom_via_summary_stop_reason_is_anomaly():
    # Authoritative path: the watchdog stamps summary.stop_reason on a graceful stop.
    summary = {**_COMPLETE, "stop_reason": "process_memory_cap"}
    cert = H.certify(H.parse_metrics(_HEALED_LOG, summary, ""))
    assert cert.verdict == H.ANOMALY and "memory" in cert.headline.lower()


def test_armed_watchdog_is_NOT_oom():
    # Slice 257 regression: the ProcessMemoryWatchdog *arming* line merely
    # describes the cap (it contains "process_memory_cap" and "OOM-kill") — it
    # must NOT be read as an OOM event. A clean wall_clock_cap run that armed
    # the watchdog but never tripped (rss far below cap) must stay non-anomalous.
    arming = ("[ProcessMemoryWatchdog] armed: warn=10445MB cap=12288MB interval=15s "
              "— graceful stop_reason=process_memory_cap before OS OOM-kill.")
    m = H.parse_metrics(_HEALED_LOG + "\n" + arming, _COMPLETE, "")
    assert m.oom is False


def test_boot_check_failed_is_anomaly():
    cert = _c(_HEALED_LOG, dep="BOOT CHECK FAILED → auto-reverted")
    assert cert.verdict == H.ANOMALY


def test_incomplete_when_not_terminal():
    cert = _c(_HEALED_LOG, summary={"session_outcome": "in_flight", "stop_reason": ""})
    assert cert.verdict == H.INCOMPLETE


def test_incomplete_kill():
    cert = _c(_HEALED_LOG, summary={"session_outcome": "incomplete_kill", "stop_reason": "sigterm"})
    assert cert.verdict == H.INCOMPLETE


def test_fired_but_no_routeback_is_anomaly():
    log = "phase=GENERATE\n[LiveFire] candidate FAILED live-fire boot: ImportError\nstate=applied\nphase=COMPLETE"
    cert = _c(log)
    assert cert.verdict == H.ANOMALY
    assert "did not route back" in cert.headline


def test_fired_routed_but_no_recovery_is_partial():
    log = ("phase=GENERATE\n[LiveFire] candidate FAILED live-fire boot: NameError\n"
           "failure_class=build GENERATE_RETRY\nphase=GENERATE\n")  # no state=applied/complete
    cert = _c(log)
    assert cert.verdict == H.OPERATIONAL_UNEXERCISED and "PARTIAL" in cert.headline


def test_no_phase_activity_is_anomaly():
    cert = _c("startup\nnothing ran\n")
    assert cert.verdict == H.ANOMALY


def test_parse_metrics_extracts_fields():
    m = H.parse_metrics(_HEALED_LOG, _COMPLETE, "BOOT CHECK PASSED\nBOOT CHECK PASSED")
    assert m.booted and m.boot_check_passed == 2 and m.routed_build and m.retried and m.recovered
    assert m.livefire_fired and m.session_outcome == "complete"


def test_render_report_never_crashes():
    txt = H.render_report(H.parse_metrics(_HEALED_LOG, _COMPLETE), _c(_HEALED_LOG))
    assert "Sovereign Telemetry Harvest Report" in txt and "VERDICT" in txt


def test_find_latest_session_and_summary(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    old = sdir / "bt-2026-06-01-000000"; old.mkdir()
    since = time.time()
    new = sdir / "bt-2026-06-15-120000"; new.mkdir()
    (new / "summary.json").write_text(json.dumps(_COMPLETE))
    found = H.find_latest_session(sdir, since)
    assert found is not None and found.name == new.name
    assert H.read_summary(found)["session_outcome"] == "complete"
    assert H._is_terminal(_COMPLETE) and not H._is_terminal({"session_outcome": "in_flight"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
