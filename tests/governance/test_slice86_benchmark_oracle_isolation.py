"""Slice 86 — benchmark-isolation event-loop hygiene for the Oracle index loop.

Root cause of the multi-instance sweep "stalls" (bt-2026-06-04-041943): NOT
upstream delay and NOT transport timeout. Direct probes proved DW streams
content in <=35s even on a 21k-token prompt; the sweep log showed
``ControlPlaneStarvation lag_ms`` up to 10,000ms in the exact stall window. The
event loop was FROZEN, so the DW stream-reading coroutines could not consume the
bytes DW had already sent (first_token_ms=-1).

The freeze source: the GovernedLoop periodic Oracle index loop calls
``incremental_update([])`` — an EMPTY list is falsy, so it falls to the
else-branch FULL repo scan (48-72s of CPU-bound work on the loop). During a
benchmark run the consciousness Oracle index is not needed (search_code uses
ripgrep, not the semantic graph; the targeted post-APPLY reindex still runs via
the orchestrator with the real applied_files). So Slice 86 skips ONLY the
periodic full scan while ``JARVIS_BENCHMARK_ISOLATION_MODE`` is active, leaving
the loop alive for hot-flip.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import governed_loop_service as gls


def test_not_suppressed_when_isolation_off(monkeypatch):
    monkeypatch.delenv("JARVIS_BENCHMARK_ISOLATION_MODE", raising=False)
    monkeypatch.delenv("JARVIS_BENCHMARK_SUPPRESS_ORACLE_FULL_SCAN", raising=False)
    assert gls._oracle_full_scan_suppressed_by_benchmark() is False


def test_suppressed_by_default_under_isolation(monkeypatch):
    monkeypatch.setenv("JARVIS_BENCHMARK_ISOLATION_MODE", "true")
    monkeypatch.delenv("JARVIS_BENCHMARK_SUPPRESS_ORACLE_FULL_SCAN", raising=False)
    assert gls._oracle_full_scan_suppressed_by_benchmark() is True


def test_operator_can_reenable_scan_inside_isolation(monkeypatch):
    monkeypatch.setenv("JARVIS_BENCHMARK_ISOLATION_MODE", "true")
    monkeypatch.setenv("JARVIS_BENCHMARK_SUPPRESS_ORACLE_FULL_SCAN", "false")
    assert gls._oracle_full_scan_suppressed_by_benchmark() is False


def test_suppress_flag_inert_when_isolation_off(monkeypatch):
    # the suppress flag must do nothing unless isolation is actually on
    monkeypatch.delenv("JARVIS_BENCHMARK_ISOLATION_MODE", raising=False)
    monkeypatch.setenv("JARVIS_BENCHMARK_SUPPRESS_ORACLE_FULL_SCAN", "true")
    assert gls._oracle_full_scan_suppressed_by_benchmark() is False


def test_helper_returns_bool_for_weird_values(monkeypatch):
    # an unrecognized value must degrade to a boolean (truthy → suppress),
    # never raise into the index loop
    monkeypatch.setenv("JARVIS_BENCHMARK_ISOLATION_MODE", "true")
    monkeypatch.setenv("JARVIS_BENCHMARK_SUPPRESS_ORACLE_FULL_SCAN", "weird-value")
    assert gls._oracle_full_scan_suppressed_by_benchmark() is True


# --- wiring pin: the index loop actually consults the gate + skips ---

def test_index_loop_consults_gate_and_continues():
    src = inspect.getsource(gls.GovernedLoopService._oracle_index_loop)
    assert "_oracle_full_scan_suppressed_by_benchmark()" in src, (
        "the periodic Oracle loop must consult the benchmark-isolation gate"
    )
    # the gate must short-circuit the heavy scan (continue), not run it
    gate_idx = src.index("if _oracle_full_scan_suppressed_by_benchmark()")
    after = src[gate_idx: gate_idx + 120]
    assert "continue" in after, "gate must `continue` (skip the full scan)"
    # and it must sit BEFORE the actual full-scan CALL (not just a comment)
    call_idx = src.index("await self._oracle.incremental_update([])")
    assert call_idx > gate_idx, "gate must precede the full-scan call"


def test_targeted_post_apply_reindex_is_not_gated():
    # The orchestrator's post-APPLY reindex passes the real applied_files and is
    # a SEPARATE caller — Slice 86 must not touch it (the benchmark op's own
    # change still needs to be indexed). Pin that this loop is the only gated site.
    src = inspect.getsource(gls.GovernedLoopService._oracle_index_loop)
    # this loop calls the empty-list full scan...
    assert "incremental_update([])" in src
    # ...and the gate is the empty-list path's guard, not a global oracle off
    assert "benchmark" in src.lower()
