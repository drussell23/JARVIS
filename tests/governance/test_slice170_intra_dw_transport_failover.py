"""Slice 170 — intra-DW transport failover precedes the cross-provider cascade.

The economic leak (operator, 2026-06-08): DW is the FUNDED primary, yet a DW streaming
rupture (TransferEncodingError, mislabeled live_transport) cascades to the expensive
Claude fallback and burns money — DW is only "primary" when Claude is broke. Root cause
in _slice36_should_force_batch:

    if not _claude_unavailable:
        return False   # Claude available → RT failures cascade to Claude

So DW only fails over to its OWN batch transport when Claude is dead. But a rupture is
transport-specific: DW's batch lane serves the identical request stream-free.

Fix: when the surface-health ledger shows the streaming wire degraded AND the batch lane
healthy (the existing _slice41 signal), force DW-batch REGARDLESS of Claude availability
— a transport rupture fails over WITHIN DW (the funded primary), not to Claude. Claude
remains the fallback for genuine DW-WIDE outages (both surfaces degraded), not blips.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.doubleword_provider as DW


class _Ctx:
    def __init__(self, route):
        self.provider_route = route


def _claude_available(monkeypatch):
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    monkeypatch.setattr(DW, "_claude_breaker_open", lambda *a, **k: False)


def test_rupture_forces_dw_batch_even_when_claude_available(monkeypatch):
    _claude_available(monkeypatch)
    monkeypatch.delenv("JARVIS_DW_INTRA_TRANSPORT_FAILOVER_ENABLED", raising=False)
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: True)  # stream degraded, batch healthy
    # NEW: intra-DW failover keeps the op on DW instead of cascading to Claude
    assert DW._slice36_should_force_batch(_Ctx("standard")) is True
    assert DW._slice36_should_force_batch(_Ctx("complex")) is True


def test_healthy_stream_with_claude_available_stays_rt(monkeypatch):
    _claude_available(monkeypatch)
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: False)  # stream healthy
    # unchanged: no degradation → RT path (Claude is a fine safety net for a true outage)
    assert DW._slice36_should_force_batch(_Ctx("standard")) is False


def test_route_gate_still_applies_to_failover(monkeypatch):
    _claude_available(monkeypatch)
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: True)
    # IMMEDIATE/BG/SPECULATIVE are not batch-routed even on degradation
    assert DW._slice36_should_force_batch(_Ctx("immediate")) is False


def test_failover_respects_kill_switch(monkeypatch):
    _claude_available(monkeypatch)
    monkeypatch.setenv("JARVIS_DW_INTRA_TRANSPORT_FAILOVER_ENABLED", "0")
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: True)
    # gated off → legacy behaviour (cascade to Claude)
    assert DW._slice36_should_force_batch(_Ctx("standard")) is False


def test_claude_dead_path_unchanged(monkeypatch):
    # regression: the existing pure-DW path (Claude disabled) still forces batch
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: False)
    assert DW._slice36_should_force_batch(_Ctx("standard")) is True
