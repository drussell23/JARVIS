"""A1 deep emit-hop telemetry — ``[A1Trace][emit-probe]`` regression spine.

Run #17 failed the A1 auditor on ``a1trace:missing_or_out_of_order:emit``:
the first hop (the roadmap orchestrator emitting a strategic GOAL) was
missing or out of order vs the subsequent ``ingest`` hop. These tests pin
the diagnostic probe that makes the locus self-explaining:

- emit-then-ingest  -> ordered probe (emit_ts < ingest_ts, source=roadmap)
- ingest-without-emit -> ``MISSING`` probe (the Run-#17 mode, non-roadmap)
- orchestrator-off   -> probe records ``orchestrator_enabled=False``
- probe exception    -> swallowed, emit/ingest unaffected (fail-soft)
- gated OFF          -> no probe lines (byte-identical)

The probe is observe-only: it never changes emit/ingest behaviour.
"""
from __future__ import annotations

import logging

import pytest

from backend.core.ouroboros.governance import a1_trace


@pytest.fixture(autouse=True)
def _reset_probe_state(monkeypatch):
    # Probe defaults ON; clear the per-goal emit-ledger between tests so
    # ordering assertions are isolated.
    monkeypatch.delenv("JARVIS_A1_EMIT_PROBE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_A1_TRACE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_ROADMAP_ORCHESTRATOR_ENABLED", raising=False)
    a1_trace.reset_emit_probe()
    yield
    a1_trace.reset_emit_probe()


def _probe_lines(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if "[A1Trace][emit-probe]" in r.getMessage()
    ]


# --- (a) emit-then-ingest -> ordered probe --------------------------------


def test_emit_then_ingest_records_ordered_probe(caplog):
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.emit_probe("g-1", source="roadmap")
        a1_trace.probe_ingest_order("g-1")
    lines = _probe_lines(caplog)
    assert any("goal=g-1" in m for m in lines), lines
    final = [m for m in lines if "ingest_ts=" in m]
    assert final, f"no ordering line emitted: {lines}"
    msg = final[-1]
    assert "ordered=True" in msg, msg
    assert "source=roadmap" in msg, msg
    assert "emit_ts=" in msg and "MISSING" not in msg, msg


# --- (b) ingest-without-emit -> MISSING probe (Run-#17 mode) ---------------


def test_ingest_without_emit_logs_missing(caplog):
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        # No prior emit_probe for this goal — the sensor-op / Run-#17 mode.
        a1_trace.probe_ingest_order("g-sensor")
    lines = _probe_lines(caplog)
    assert any("MISSING" in m and "goal=g-sensor" in m for m in lines), lines
    miss = [m for m in lines if "MISSING" in m][-1]
    # Self-explaining: the line names the likely cause.
    assert "emit_ts=MISSING" in miss, miss
    assert "ordered=False" in miss, miss


# --- (c) orchestrator-off captured ----------------------------------------


def test_orchestrator_off_is_captured(caplog, monkeypatch):
    monkeypatch.setenv("JARVIS_ROADMAP_ORCHESTRATOR_ENABLED", "false")
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.emit_probe("g-2", source="roadmap")
    lines = _probe_lines(caplog)
    assert any("orchestrator_enabled=False" in m for m in lines), lines


def test_orchestrator_on_is_captured(caplog, monkeypatch):
    monkeypatch.setenv("JARVIS_ROADMAP_ORCHESTRATOR_ENABLED", "true")
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.emit_probe("g-3", source="roadmap")
    lines = _probe_lines(caplog)
    assert any("orchestrator_enabled=True" in m for m in lines), lines


# --- (d) fail-soft + observe-only -----------------------------------------


def test_probe_exception_is_swallowed(monkeypatch):
    # Break the internal clock so the probe body raises; it must not escape.
    monkeypatch.setattr(
        a1_trace.time, "monotonic", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    # Neither call may raise.
    a1_trace.emit_probe("g-x", source="roadmap")
    a1_trace.probe_ingest_order("g-x")


def test_emit_probe_returns_none_observe_only():
    # The probe is diagnostic — it returns nothing the caller can act on.
    assert a1_trace.emit_probe("g-4", source="roadmap") is None
    assert a1_trace.probe_ingest_order("g-4") is None


# --- (e) gated OFF -> byte-identical (no probe lines) ----------------------


def test_gated_off_emits_no_probe_lines(caplog, monkeypatch):
    monkeypatch.setenv("JARVIS_A1_EMIT_PROBE_ENABLED", "false")
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.emit_probe("g-5", source="roadmap")
        a1_trace.probe_ingest_order("g-5")
    assert not _probe_lines(caplog)


def test_master_trace_off_also_silences_probe(caplog, monkeypatch):
    # The probe rides the same surface; killing the master A1Trace flag
    # also silences the probe (defence in depth).
    monkeypatch.setenv("JARVIS_A1_TRACE_ENABLED", "false")
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.emit_probe("g-6", source="roadmap")
        a1_trace.probe_ingest_order("g-6")
    assert not _probe_lines(caplog)


# --- bounded ledger: no unbounded growth ----------------------------------


def test_emit_ledger_is_bounded():
    for i in range(5000):
        a1_trace.emit_probe(f"g-{i}", source="roadmap")
    # Bounded ring must not retain every goal forever.
    assert a1_trace._emit_ledger_size() <= a1_trace._EMIT_LEDGER_MAX
