"""A1 GraduationAuditor -- provenance-driven, origin-correct pipeline traversal.

The Run-#17 fix: the auditor reads each op's ORIGIN (from the Unified
Provenance Ledger's ``[Provenance]`` line, after the hash-chain verified
intact) and validates the pipeline THAT origin produces:

  * SENSOR  -> ingest -> dequeue -> submit -> accept   (NO emit -- VALID)
  * ROADMAP -> emit -> ingest -> dequeue -> submit -> accept
  * UNKNOWN / broken chain -> UNVERIFIABLE (no fake-pass, no hardcoded bypass).

These tests drive the PURE auditor core (no network) via ``ingest_log_line``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "a1_graduation_auditor.py"
_spec = importlib.util.spec_from_file_location("a1_graduation_auditor", _SCRIPT)
assert _spec is not None and _spec.loader is not None
aud = importlib.util.module_from_spec(_spec)
sys.modules["a1_graduation_auditor"] = aud
_spec.loader.exec_module(aud)


def _fresh_auditor(strict: bool = True):
    # Empty flag set so the trace/origin logic is isolated from the flag audit.
    return aud.A1GraduationAuditor(
        flags=[], strict=strict, chaos_manifest_path=None,
        lineage_scoping_enabled=False,
    )


# --- provenance line parsing -----------------------------------------------


def test_parse_provenance_line():
    out = aud.parse_provenance_line(
        "WARNING [Provenance] op=op-1 origin=test_failure "
        "origin_class=sensor chain_ok=True"
    )
    assert out == ("op-1", "test_failure", "sensor", True)


def test_parse_provenance_line_chain_false():
    out = aud.parse_provenance_line(
        "[Provenance] op=op-9 origin=roadmap origin_class=roadmap chain_ok=False"
    )
    assert out == ("op-9", "roadmap", "roadmap", False)


def test_parse_non_provenance_line_returns_none():
    assert aud.parse_provenance_line("[A1Trace] ingest goal=op-1") is None


# --- THE Run-#17 FIX: sensor op with NO emit is PROVEN ----------------------


def test_sensor_origin_emitless_op_is_in_order():
    a = _fresh_auditor()
    # Provenance stamps op-S as a SENSOR origin (chain intact).
    a.ingest_log_line(
        "[Provenance] op=op-S origin=test_failure origin_class=sensor chain_ok=True"
    )
    # The sensor pipeline -- NO emit hop, by design.
    for hop in ("ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-S")
    assert a.trace.all_hops_in_order() is True
    assert a.trace.winning_goal() == "op-S"


def test_sensor_origin_emitless_op_full_verdict_proven():
    a = _fresh_auditor()
    a.ingest_log_line(
        "[Provenance] op=op-S origin=test_failure origin_class=sensor chain_ok=True"
    )
    for hop in ("ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-S")
    a.ingest_event("fsm_phase_changed", {"phase": "CLASSIFY", "op_id": "op-S"})
    a.ingest_event("fsm_phase_changed", {"phase": "APPLY", "op_id": "op-S"})
    a.ingest_event("operation_terminal", {"op_id": "op-S", "state": "applied"})
    a.ingest_event("review_branch_created", {"op_id": "op-S"})
    v = a.verdict()
    # Trace + fsm + pr all hold; empty flag set -> flag audit passes; no
    # intervention. A1_DISPATCH_PROVEN despite the missing emit hop.
    assert v.criteria["a1trace_5_hops_in_order"] is True
    assert v.proven is True, v.failure_locus


# --- ROADMAP op MISSING emit still FAILS -----------------------------------


def test_roadmap_origin_missing_emit_fails():
    a = _fresh_auditor()
    a.ingest_log_line(
        "[Provenance] op=op-R origin=roadmap origin_class=roadmap chain_ok=True"
    )
    # Roadmap pipeline requires emit -- omit it.
    for hop in ("ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-R")
    assert a.trace.all_hops_in_order() is False
    v = a.verdict()
    assert v.criteria["a1trace_5_hops_in_order"] is False
    assert "emit" in v.failure_locus


def test_roadmap_origin_with_emit_is_in_order():
    a = _fresh_auditor()
    a.ingest_log_line(
        "[Provenance] op=op-R origin=roadmap origin_class=roadmap chain_ok=True"
    )
    for hop in ("emit", "ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-R")
    assert a.trace.all_hops_in_order() is True
    assert a.trace.winning_goal() == "op-R"


# --- tampered / unknown origin -> UNVERIFIABLE (not a fake-pass) ------------


def test_broken_provenance_chain_is_unverifiable():
    a = _fresh_auditor()
    # chain_ok=False -> the origin cannot be trusted.
    a.ingest_log_line(
        "[Provenance] op=op-T origin=test_failure origin_class=sensor chain_ok=False"
    )
    for hop in ("ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-T")
    # Even a perfectly-ordered sensor pipeline does NOT pass on a broken chain.
    assert a.trace.all_hops_in_order() is False
    assert "op-T" in a.trace.broken_chain_goals
    v = a.verdict()
    assert v.criteria["a1trace_5_hops_in_order"] is False


def test_unknown_origin_is_unverifiable():
    a = _fresh_auditor()
    a.ingest_log_line(
        "[Provenance] op=op-U origin=mystery origin_class=unknown chain_ok=True"
    )
    for hop in ("ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-U")
    # Unknown origin -> no required pipeline -> not provable (no fake-pass).
    assert a.trace.all_hops_in_order() is False
    assert a.trace.required_hops("op-U") is None


def test_no_hardcoded_bypass_unknown_never_passes_even_with_all_hops():
    a = _fresh_auditor()
    a.ingest_log_line(
        "[Provenance] op=op-U origin=mystery origin_class=unknown chain_ok=True"
    )
    # Feed EVERY hop including emit -- an unknown origin must still not pass
    # (the pipeline is DERIVED from origin class, never bypassed).
    for hop in aud.A1TRACE_HOPS:
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-U")
    assert a.trace.all_hops_in_order() is False


# --- emit-probe source as a secondary origin witness -----------------------


def test_emit_probe_source_classifies_origin():
    a = _fresh_auditor()
    # No explicit [Provenance] line; the emit-probe MISSING line carries source.
    a.ingest_log_line(
        "[A1Trace][emit-probe] MISSING goal=op-P emit_ts=MISSING ingest_ts=1.0 "
        "ordered=False source=test_failure"
    )
    for hop in ("ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=op-P")
    assert a.trace.origin_by_goal.get("op-P") == "sensor"
    assert a.trace.all_hops_in_order() is True


# --- legacy fallback: no provenance -> byte-identical all-5-hops -----------


def test_no_provenance_legacy_all_five_hops_required():
    a = _fresh_auditor()
    # No [Provenance] line anywhere -> legacy roadmap-shaped check (all 5).
    for hop in aud.A1TRACE_HOPS:
        a.ingest_log_line(f"[A1Trace] {hop} goal=G-LEGACY")
    assert a.trace.provenance_active() is False
    assert a.trace.all_hops_in_order() is True


def test_no_provenance_legacy_missing_emit_fails():
    a = _fresh_auditor()
    for hop in ("ingest", "dequeue", "submit", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=G-LEGACY")
    # Legacy mode (no provenance) still requires emit -> fails (pre-fix behavior).
    assert a.trace.all_hops_in_order() is False
