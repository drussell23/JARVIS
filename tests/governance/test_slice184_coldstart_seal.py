"""Slice 184 — the cold-start seal (fail-SAFE, not fail-RT).

Slice 183 traced the bleed to its root: at fresh-process boot the warm-boot + predictor are
BLIND (warm_degraded=False, rupture_risk=False) — no durable degradation signal survives the
restart, so the first ops walk RT and rupture before the ledger learns. This seal is
DETERMINISTIC, not predictive: for a bounded window after process start, force batch for
standard/complex ops — the first ops can't rupture on an unproven stream. No ML needed; you
KNOW you just booted and have no signal, so you fail SAFE (batch is stream-free + stable).
"""
from __future__ import annotations

import time

from backend.core.ouroboros.governance import doubleword_provider as DW


class _Ctx:
    def __init__(self, route="standard"):
        self.provider_route = route


def _blind_signals(monkeypatch):
    # the EXACT Slice-183 telemetry state: every degradation signal is False/blind
    monkeypatch.setattr(DW, "_dw_streaming_warm_degraded", lambda: False)
    monkeypatch.setattr(DW, "_dw_rupture_risk_high", lambda *a, **k: False)
    monkeypatch.setattr(DW, "_dw_batch_lane_healthy", lambda: True)
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: False)
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)  # Claude "available"
    monkeypatch.setattr(DW, "_claude_breaker_open", lambda *a, **k: False)


def test_cold_start_forces_batch_despite_blind_signals(monkeypatch):
    # THE fix: blind signals (warm=False, risk=False) BUT fresh process → force batch
    _blind_signals(monkeypatch)
    monkeypatch.setenv("JARVIS_DW_COLDSTART_WINDOW_S", "90")
    monkeypatch.setattr(DW, "_PROCESS_START", time.monotonic())  # just booted
    assert DW._slice36_should_force_batch(_Ctx("standard"), model_id="m") is True


def test_cold_start_expires_then_normal_routing(monkeypatch):
    # after the window, blind signals + Claude available → RT allowed again (fail-safe ends)
    _blind_signals(monkeypatch)
    monkeypatch.setenv("JARVIS_DW_COLDSTART_WINDOW_S", "90")
    monkeypatch.setattr(DW, "_PROCESS_START", time.monotonic() - 1000.0)  # window long past
    assert DW._slice36_should_force_batch(_Ctx("standard"), model_id="m") is False


def test_cold_start_respects_batch_health(monkeypatch):
    # never force into a broken batch lane even during cold-start
    _blind_signals(monkeypatch)
    monkeypatch.setattr(DW, "_dw_batch_lane_healthy", lambda: False)
    monkeypatch.setenv("JARVIS_DW_COLDSTART_WINDOW_S", "90")
    monkeypatch.setattr(DW, "_PROCESS_START", time.monotonic())
    assert DW._slice36_should_force_batch(_Ctx("standard"), model_id="m") is False


def test_cold_start_kill_switch(monkeypatch):
    _blind_signals(monkeypatch)
    monkeypatch.setenv("JARVIS_DW_COLDSTART_ENABLED", "0")
    monkeypatch.setattr(DW, "_PROCESS_START", time.monotonic())
    assert DW._slice36_should_force_batch(_Ctx("standard"), model_id="m") is False


def test_in_cold_start_helper(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_COLDSTART_WINDOW_S", "90")
    monkeypatch.delenv("JARVIS_DW_COLDSTART_ENABLED", raising=False)
    monkeypatch.setattr(DW, "_PROCESS_START", time.monotonic())
    assert DW._dw_in_cold_start() is True
    monkeypatch.setattr(DW, "_PROCESS_START", time.monotonic() - 1000.0)
    assert DW._dw_in_cold_start() is False


def test_sentinel_enforcement_includes_cold_start(monkeypatch):
    # the sentinel's Gap-1 enforcement condition must include cold-start as a trigger
    import importlib.util
    spec = importlib.util.find_spec("backend.core.ouroboros.governance.candidate_generator")
    with open(spec.origin) as fh:
        src = fh.read()
    assert "_dw_in_cold_start" in src


if __name__ == "__main__":
    import unittest
    unittest.main()
