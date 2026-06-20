"""Sovereign Autarky Backoff-Wait — pure decision tests (2026-06-20).

Live bug: in DW-only autarky, a transient DW TIMEOUT (recovery_eta=+39s) routed a
STANDARD op to the absent Claude fallback → fallback_skipped:no_fallback_configured
EXHAUSTION, despite remaining_s=410 (ample budget to wait 39s + re-attempt DW).
"""
from __future__ import annotations

from backend.core.ouroboros.governance.candidate_generator import (
    autarky_backoff_wait_enabled,
    autarky_should_wait_and_retry as W,
    _autarky_backoff_max_wait_s,
    _autarky_retry_margin_s,
)


def test_live_case_waits_out_transient_backoff():
    # eta 39s, remaining 410s, no fallback, margin 30 → wait 39s + re-attempt.
    assert W(has_fallback=False, enabled=True, eta_s=39, remaining_s=410,
             max_wait_s=90, margin_s=30) == 39.0


def test_fallback_present_never_waits():
    # A real fallback exists → legacy route-to-fallback (None = don't wait).
    assert W(has_fallback=True, enabled=True, eta_s=39, remaining_s=410,
             max_wait_s=90, margin_s=30) is None


def test_disabled_never_waits():
    assert W(has_fallback=False, enabled=False, eta_s=39, remaining_s=410,
             max_wait_s=90, margin_s=30) is None


def test_eta_plus_margin_exceeds_budget_degrades():
    # Not enough budget to wait AND still have margin to call → degrade (None).
    assert W(has_fallback=False, enabled=True, eta_s=400, remaining_s=410,
             max_wait_s=500, margin_s=30) is None


def test_wait_capped_by_max_wait():
    # eta 120 capped to 90; 90+30 < 410 → wait the cap.
    assert W(has_fallback=False, enabled=True, eta_s=120, remaining_s=410,
             max_wait_s=90, margin_s=30) == 90.0


def test_capped_wait_still_budget_checked():
    # eta 120 → cap 90, but 90+30 = 120 not < 110 → degrade.
    assert W(has_fallback=False, enabled=True, eta_s=120, remaining_s=110,
             max_wait_s=90, margin_s=30) is None


def test_zero_or_negative_inputs_safe():
    assert W(has_fallback=False, enabled=True, eta_s=0, remaining_s=410,
             max_wait_s=90, margin_s=30) is None
    assert W(has_fallback=False, enabled=True, eta_s=39, remaining_s=0,
             max_wait_s=90, margin_s=30) is None


def test_exact_boundary_is_strict_less_than():
    # wait+margin must be STRICTLY less than remaining (reserve real call time).
    # eta 40, margin 30 → 70; remaining exactly 70 → NOT < 70 → None.
    assert W(has_fallback=False, enabled=True, eta_s=40, remaining_s=70,
             max_wait_s=90, margin_s=30) is None
    # remaining 70.1 → 70 < 70.1 → wait.
    assert W(has_fallback=False, enabled=True, eta_s=40, remaining_s=70.1,
             max_wait_s=90, margin_s=30) == 40.0


def test_never_raises_on_garbage():
    assert W(has_fallback=False, enabled=True, eta_s="x", remaining_s=410,  # type: ignore
             max_wait_s=90, margin_s=30) is None


def test_defaults():
    assert autarky_backoff_wait_enabled() is True   # failure-path-only default-on
    assert _autarky_backoff_max_wait_s() == 90.0
    assert _autarky_retry_margin_s() == 30.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTARKY_BACKOFF_WAIT_ENABLED", "false")
    assert autarky_backoff_wait_enabled() is False
    monkeypatch.setenv("JARVIS_AUTARKY_BACKOFF_MAX_WAIT_S", "120")
    assert _autarky_backoff_max_wait_s() == 120.0
    monkeypatch.setenv("JARVIS_AUTARKY_RETRY_MARGIN_S", "15")
    assert _autarky_retry_margin_s() == 15.0
    # garbage → defensive default
    monkeypatch.setenv("JARVIS_AUTARKY_BACKOFF_MAX_WAIT_S", "nonsense")
    assert _autarky_backoff_max_wait_s() == 90.0
